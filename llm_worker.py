"""LLM worker (S3 + SQS) — full pipeline.

Triggers (each is one SQS message produced by the LLM trigger Lambda):
  { "bucket", "key", "kind": "transcript" }  ← a *_transcripts.txt was written
  { "bucket", "key", "kind": "image" }       ← a .png/.jpg landed in an image-only deliverable
  { "bucket", "key", "kind": "text" }        ← a .txt landed (non-transcript), e.g. JD text

For each, the worker:
  1. resolves the deliverable folder + name
  2. skips folders that are "combined inputs" (their content is pulled by a sibling)
  3. matches the deliverable → prompt + extras
  4. gathers inputs: transcript / resume / reference PDF / sibling image / sibling text
  5. calls OpenAI → JSON result → writes <name>_result.json in the deliverable folder
  6. after writing, checks if the day is complete → writes Day overall → then candidate overall
"""

import json
import signal
import threading
import time
from pathlib import Path

import boto3
import requests

from llm_config import LLM_SETTINGS, match_rule, is_combined_input_only
import llm_processor
import llm_s3
import llm_overall
import salesforce

_stop = threading.Event()
_lock = threading.Lock()   # serialize overall-writing to avoid races


# ── input gathering ───────────────────────────────────────────────────────────

def _gather_and_score(s3, client, *, deliverable_prefix, deliverable_name, transcript_key=None):
    rule = match_rule(deliverable_name)
    if not rule:
        print(f"[SKIP] no rule: {deliverable_name}")
        return None
    prompt_file, extras = rule

    transcript_text = None
    if transcript_key:
        transcript_text = llm_s3.read_text(LLM_SETTINGS.bucket, transcript_key) \
            if False else llm_s3.read_text(s3, LLM_SETTINGS.bucket, transcript_key)
        if not transcript_text.strip():
            print(f"[SKIP] empty transcript: {transcript_key}")
            return None

    ts_prefix = llm_s3.training_steps_prefix(deliverable_prefix)
    day_prefix = llm_s3.parent_prefix(deliverable_prefix)

    resume_text = reference_pdf_text = extra_text = None
    image_urls = []

    for extra in extras:
        if extra == "resume":
            rk = llm_s3.find_resume_pdf(s3, LLM_SETTINGS.bucket, ts_prefix)
            if rk:
                tmp = Path("/tmp") / Path(rk).name
                llm_s3.download(s3, LLM_SETTINGS.bucket, rk, tmp)
                resume_text = llm_processor.extract_pdf_text(tmp)
                tmp.unlink(missing_ok=True)
            else:
                print(f"[WARN] no resume under {ts_prefix}resume pdf/")

        elif extra.startswith("pdf:"):
            pdf_path = Path(LLM_SETTINGS.pdf_dir) / extra.split(":", 1)[1]
            if pdf_path.exists():
                reference_pdf_text = llm_processor.extract_pdf_text(pdf_path)
            else:
                print(f"[WARN] reference PDF missing: {pdf_path}")

        elif extra == "sibling_image":
            sib = _find_sibling(s3, day_prefix, deliverable_name, want="image")
            if sib:
                img = llm_s3.find_first_image(s3, LLM_SETTINGS.bucket, sib)
                if img:
                    tmp = Path("/tmp") / Path(img).name
                    llm_s3.download(s3, LLM_SETTINGS.bucket, img, tmp)
                    image_urls.append(llm_processor.image_to_data_url(tmp))
                    tmp.unlink(missing_ok=True)

        elif extra == "sibling_text":
            sib = _find_sibling(s3, day_prefix, deliverable_name, want="text")
            if sib:
                txt = llm_s3.find_first_text(
                    s3, LLM_SETTINGS.bucket, sib,
                    exclude_suffixes=(LLM_SETTINGS.transcript_suffix.lower(),),
                )
                if txt:
                    extra_text = llm_s3.read_text(s3, LLM_SETTINGS.bucket, txt)

        elif extra == "own_image":
            # standalone image deliverable: score the image in THIS folder
            img = llm_s3.find_first_image(s3, LLM_SETTINGS.bucket, deliverable_prefix)
            if img:
                tmp = Path("/tmp") / Path(img).name
                llm_s3.download(s3, LLM_SETTINGS.bucket, img, tmp)
                image_urls.append(llm_processor.image_to_data_url(tmp))
                tmp.unlink(missing_ok=True)
            else:
                print(f"[WARN] no image under {deliverable_prefix}")

        elif extra == "own_text":
            # standalone text deliverable (e.g. JD text): score this folder's text
            txt = llm_s3.find_first_text(
                s3, LLM_SETTINGS.bucket, deliverable_prefix,
                exclude_suffixes=(LLM_SETTINGS.transcript_suffix.lower(),),
            )
            if txt:
                extra_text = llm_s3.read_text(s3, LLM_SETTINGS.bucket, txt)
            else:
                print(f"[WARN] no text under {deliverable_prefix}")

    # standalone image/text deliverables must have their own content to score;
    # without it the model would have nothing to evaluate.
    if "own_image" in extras and not image_urls and not transcript_text:
        print(f"[SKIP] no image to score: {deliverable_name}")
        return None
    if "own_text" in extras and not extra_text and not transcript_text and not image_urls:
        print(f"[SKIP] no text to score: {deliverable_name}")
        return None

    system_prompt = llm_processor.load_prompt(LLM_SETTINGS, prompt_file)

    print(f"[LLM] scoring {deliverable_name} ({prompt_file})"
          f"{' +image' if image_urls else ''}{' +jdtext' if extra_text else ''}")

    result = llm_processor.evaluate(
        LLM_SETTINGS, client,
        system_prompt=system_prompt,
        transcript_text=transcript_text,
        resume_text=resume_text,
        reference_pdf_text=reference_pdf_text,
        extra_text=extra_text,
        image_data_urls=image_urls or None,
    )
    result["deliverable"] = deliverable_name
    return result


def _find_sibling(s3, day_prefix, deliverable_name, *, want):
    """Find the sibling folder (image/text companion) in the same day.

    Pairs by the shared base + number. e.g.
      'System Design Problem 1 Video' → look for '...Problem 1 Image'
      'Job Description Alignment  1 video' → '...1 text' or '...1 Image'
      'Team Structure Video' → 'Team Structure Diagram'
    """
    name_l = deliverable_name.lower()
    # build a "stem" to match siblings: strip the trailing role word + id
    siblings = set()
    for k in llm_s3.list_prefix(s3, LLM_SETTINGS.bucket, day_prefix):
        rel = k[len(day_prefix):]
        if "/" in rel:
            siblings.add(rel.split("/", 1)[0])  # immediate child folder name

    def base_tokens(n):
        n = n.lower()
        for w in ("video", "image", "diagram", "text", "recording"):
            n = n.replace(w, "")
        # drop trailing (id)
        if "(" in n:
            n = n[: n.find("(")]
        return [t for t in n.replace("-", " ").split() if t]

    target = set(base_tokens(name_l))
    best = None
    for sib in siblings:
        sib_l = sib.lower()
        if sib_l == name_l:
            continue
        if want == "image" and not any(w in sib_l for w in ("image", "diagram")):
            continue
        if want == "text" and "text" not in sib_l:
            continue
        if set(base_tokens(sib_l)) & target:
            best = day_prefix + sib + "/"
            break
    return best


# ── overall coordination ───────────────────────────────────────────────────────

def _is_result_name(name: str) -> bool:
    """A per-deliverable result file (possibly pass/fail-tagged), not an overall."""
    return LLM_SETTINGS.result_suffix in name and "OVERALL" not in name


def _maybe_write_overall(s3, deliverable_prefix):
    """After a per-deliverable result is written, check if the day is complete;
    if so, write the day overall. (Candidate overall is disabled.)"""
    day_prefix = llm_s3.parent_prefix(deliverable_prefix)
    day_name = llm_s3.deliverable_name_from_prefix(day_prefix)

    # collect the LATEST result per deliverable folder under this day.
    # A deliverable can have several result files (one per attempt); keep max attempt.
    latest = {}   # deliverable subfolder prefix -> result dict
    for k in llm_s3.list_prefix(s3, LLM_SETTINGS.bucket, day_prefix):
        name = k.rsplit("/", 1)[-1]
        if not _is_result_name(name):
            continue
        deliv = k.rsplit("/", 1)[0] + "/"
        if deliv == day_prefix:          # DayOVERALL etc. sit directly in the day folder
            continue
        try:
            r = json.loads(llm_s3.read_text(s3, LLM_SETTINGS.bucket, k))
        except Exception:
            continue
        if deliv not in latest or r.get("attempt", 0) > latest[deliv].get("attempt", 0):
            latest[deliv] = r
    results = list(latest.values())

    metadata = llm_overall.read_metadata(s3, LLM_SETTINGS.bucket, deliverable_prefix)
    expected = llm_overall.expected_deliverables_for_day(metadata, day_name)

    if expected is not None and len(results) < len(expected):
        print(f"[OVERALL] day '{day_name}' not complete ({len(results)}/{len(expected)})")
        return

    day_doc = llm_overall.combine(results, label_fields={
        "candidate": (deliverable_prefix.split("/", 1)[0]),
        "day": day_name,
    })
    day_key = day_prefix + "DayOVERALL" + LLM_SETTINGS.result_suffix
    s3.put_object(Bucket=LLM_SETTINGS.bucket, Key=day_key,
                  Body=json.dumps(day_doc, indent=2).encode("utf-8"),
                  ContentType="application/json")
    print(f"[OVERALL] wrote {day_key} → {day_doc['result']} {day_doc['overallScore']}")

    # Candidate overall disabled — do not store CANDIDATE_OVERALL_result.json in S3.
    # _refresh_candidate_overall(s3, deliverable_prefix)


def _refresh_candidate_overall(s3, any_prefix):
    root = any_prefix.split("/", 1)[0] + "/"
    # find all DayOVERALL files
    day_overalls = [k for k in llm_s3.list_prefix(s3, LLM_SETTINGS.bucket, root)
                    if k.endswith("DayOVERALL" + LLM_SETTINGS.result_suffix)]
    docs = []
    for k in day_overalls:
        try:
            d = json.loads(llm_s3.read_text(s3, LLM_SETTINGS.bucket, k))
            docs.append({"deliverable": d.get("day"), "score": d.get("overallScore"),
                         "result": d.get("result"),
                         "positives": d.get("positives", []), "negatives": d.get("negatives", [])})
        except Exception:
            pass
    if not docs:
        return
    cand_doc = llm_overall.combine(docs, label_fields={"candidate": root.rstrip("/")})
    cand_doc["days"] = cand_doc.pop("deliverables")
    key = root + "CANDIDATE_OVERALL" + LLM_SETTINGS.result_suffix
    s3.put_object(Bucket=LLM_SETTINGS.bucket, Key=key,
                  Body=json.dumps(cand_doc, indent=2).encode("utf-8"),
                  ContentType="application/json")
    print(f"[OVERALL] wrote {key} → {cand_doc['result']} {cand_doc['overallScore']}")


# ── message handling ───────────────────────────────────────────────────────────

def _attempt_number(s3, deliverable_prefix) -> int:
    """Attempt = number of existing result files in the folder + 1.
    Counts tagged and untagged results (each prior attempt left one)."""
    n = 0
    for k in llm_s3.list_prefix(s3, LLM_SETTINGS.bucket, deliverable_prefix):
        rel = k[len(deliverable_prefix):]
        if "/" in rel:
            continue
        if _is_result_name(rel):
            n += 1
    return n + 1


def _handle(s3, client, body: dict):
    kind = body.get("kind", "transcript")
    key = body["key"]
    deliverable_prefix = llm_s3.prefix_of(key)
    deliverable_name = llm_s3.deliverable_name_from_prefix(deliverable_prefix)

    # combined-input folders (diagram/text companions) are not scored directly
    if is_combined_input_only(deliverable_name):
        print(f"[SKIP] combined-input folder, handled by sibling: {deliverable_name}")
        return

    # tagged files are earlier attempts — never re-process them
    fname = key.rsplit("/", 1)[-1]
    if LLM_SETTINGS.pass_marker in fname or LLM_SETTINGS.fail_marker in fname:
        print(f"[SKIP] already-tagged file: {fname}")
        return

    # the deliverable-result id rides the filename (video stem → transcript stem)
    if kind == "transcript":
        result_id = fname[:-len(LLM_SETTINGS.transcript_suffix)]
    else:
        result_id = Path(fname).stem

    transcript_key = key if kind == "transcript" else None
    attempt = _attempt_number(s3, deliverable_prefix)

    # for image/text-only deliverables there is no transcript; still score
    result = _gather_and_score(
        s3, client,
        deliverable_prefix=deliverable_prefix,
        deliverable_name=deliverable_name,
        transcript_key=transcript_key,
    )
    if result is None:
        return

    result["deliverableResultId"] = result_id
    result["attempt"] = attempt
    result["video"] = result_id

    base_marker = LLM_SETTINGS.pass_marker if result.get("result") == "PASS" else LLM_SETTINGS.fail_marker
    # label includes the attempt number, e.g. (Fail)(Attempt-1), (Pass)(Attempt-2).
    # base marker stays a contiguous substring so is_tagged/skip detection still works.
    marker = f"{base_marker}(Attempt-{attempt})"

    # write the result file ALREADY labeled — one file, no untagged duplicate
    result_key = (f"{deliverable_prefix}{deliverable_name}_{result_id}"
                  f"{LLM_SETTINGS.result_suffix}{marker}")
    s3.put_object(Bucket=LLM_SETTINGS.bucket, Key=result_key,
                  Body=json.dumps(result, indent=2).encode("utf-8"),
                  ContentType="application/json")
    print(f"[DONE] {result_key} → {result.get('result')} {result.get('score')} (attempt {attempt})")

    # label the remaining files (transcript, video, image, ...) with the same status.
    # the result file is already labeled above, so tag_folder_files skips it.
    # NOTE: renaming is S3 copy+delete — the EC2 role needs s3:DeleteObject or the
    # originals stay behind as duplicates.
    try:
        tagged = llm_s3.tag_folder_files(
            s3, LLM_SETTINGS.bucket, deliverable_prefix, marker,
            pass_marker=LLM_SETTINGS.pass_marker, fail_marker=LLM_SETTINGS.fail_marker,
        )
        print(f"[TAG] {deliverable_name} → {marker} ({tagged} files)")
    except Exception as exc:
        print(f"[TAG-ERROR] {deliverable_name}: {exc}")

    # send result back to Salesforce (no-op if SF disabled; never raises)
    salesforce.notify(LLM_SETTINGS, {
        "deliverableResultId": result_id,
        "result": result.get("result"),
        "score": result.get("score"),
        "attempt": attempt,
        "reasoning": result.get("reasoning", ""),
        "positives": result.get("positives", []),
        "negatives": result.get("negatives", []),
    })

    with _lock:
        _maybe_write_overall(s3, deliverable_prefix)


def _worker(n):
    sqs = boto3.client("sqs", region_name=LLM_SETTINGS.aws_region)
    s3 = llm_s3.build_s3(LLM_SETTINGS)
    client = requests.Session()
    client.headers.update({"Authorization": f"Bearer {LLM_SETTINGS.openai_api_key}"})
    print(f"[LLM-WORKER {n}] started")

    while not _stop.is_set():
        try:
            resp = sqs.receive_message(
                QueueUrl=LLM_SETTINGS.llm_queue_url, MaxNumberOfMessages=1,
                WaitTimeSeconds=LLM_SETTINGS.sqs_wait_seconds,
                VisibilityTimeout=LLM_SETTINGS.sqs_visibility_timeout)
        except Exception as exc:
            print(f"[LLM-WORKER {n}][SQS-ERROR] {exc}")
            time.sleep(LLM_SETTINGS.failure_sleep_seconds); continue

        msgs = resp.get("Messages", [])
        if not msgs:
            continue
        msg = msgs[0]; receipt = msg["ReceiptHandle"]
        try:
            _handle(s3, client, json.loads(msg["Body"]))
            sqs.delete_message(QueueUrl=LLM_SETTINGS.llm_queue_url, ReceiptHandle=receipt)
        except Exception as exc:
            print(f"[LLM-WORKER {n}][ERROR] {exc}  (will retry via SQS)")
            time.sleep(LLM_SETTINGS.failure_sleep_seconds)


def main():
    LLM_SETTINGS.validate()
    signal.signal(signal.SIGINT, lambda *a: _stop.set())
    signal.signal(signal.SIGTERM, lambda *a: _stop.set())
    print(f"[LLM-MAIN] bucket={LLM_SETTINGS.bucket} queue={LLM_SETTINGS.llm_queue_url}")
    print(f"[LLM-MAIN] threads={LLM_SETTINGS.worker_threads} model={LLM_SETTINGS.openai_model}")
    threads = [threading.Thread(target=_worker, args=(i + 1,), daemon=True)
               for i in range(LLM_SETTINGS.worker_threads)]
    for t in threads:
        t.start()
    while not _stop.is_set():
        time.sleep(1)


if __name__ == "__main__":
    main()
