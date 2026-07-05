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
  5. calls OpenAI → JSON result → writes the per-engine result in the folder
     (Whisper: <name>_result.json … ; AssemblyAI: <name>_result(A).json …)
  6. after writing, checks if the day is complete FOR THAT ENGINE → writes that
     engine's day overall (Whisper: DayOVERALL_result.json ; AssemblyAI:
     DayOVERALL(A)_result.json). Candidate overall stays disabled.

Two transcription engines run in parallel (see engines.py):
  Whisper      — UNTAGGED, the original convention: scored genuinely; results push
                 to Salesforce and are stored in S3 (byte-identical to before).
  (A) AssemblyAI — tagged '(A)': scored genuinely; results stored in S3 ONLY
                 (never Salesforce).
A video produces two transcripts → two genuine scorings. An image/text
deliverable is scored ONCE and saved as two copies of the identical verdict (one
LLM cost). Engine scoping is end-to-end: attempt numbers, folder tagging, and day
rollups never mix the two engines.
"""

import json
import re
import signal
import threading
import time
from pathlib import Path

import boto3
import requests

import engines
from llm_config import LLM_SETTINGS, match_rule, is_combined_input_only
import llm_processor
import llm_s3
import llm_overall
import salesforce

_stop = threading.Event()

# Per-(day, engine) locks: two same-engine results finishing at once must not
# both write the same DayOVERALL file, but (W) and (A) rollups never block each
# other (they write different files).
_overall_locks = {}
_overall_locks_guard = threading.Lock()


def _overall_lock(key: str):
    with _overall_locks_guard:
        lk = _overall_locks.get(key)
        if lk is None:
            lk = threading.Lock()
            _overall_locks[key] = lk
        return lk


# Salesforce record id embedded in a folder name, e.g. "...Image(a12O10000071verIAA)" → a12...
_SF_ID_RE = re.compile(r"\(([A-Za-z0-9]{15,18})\)")


def _sf_id_from_name(name: str):
    m = _SF_ID_RE.search(name or "")
    return m.group(1) if m else None


# ── engine-tagged result naming ────────────────────────────────────────────────

def _engine_result_suffix(engine: str) -> str:
    """The per-engine result suffix, engine tag before the extension. Whisper is
    untagged (original convention), AssemblyAI carries '(A)':
      Whisper    -> '_result.json'
      AssemblyAI -> '_result(A).json'."""
    stem, dot, ext = LLM_SETTINGS.result_suffix.rpartition(".")
    return f"{stem}{engines.engine_tag(engine)}{dot}{ext}"


# ── input gathering ───────────────────────────────────────────────────────────

def _gather_and_score(s3, client, *, deliverable_prefix, deliverable_name, transcript_key=None):
    rule = match_rule(deliverable_name)
    if not rule:
        print(f"[SKIP] no rule: {deliverable_name}")
        return None
    prompt_file, extras = rule

    transcript_text = None
    if transcript_key:
        transcript_text = llm_s3.read_text(s3, LLM_SETTINGS.bucket, transcript_key)
        # Muted/empty video: Whisper produced no usable speech. Don't waste an
        # LLM call — return a deterministic 0/FAIL so the candidate gets clear
        # feedback through the normal result → Salesforce flow.
        if len((transcript_text or "").strip()) < 20:
            print(f"[EMPTY-TRANSCRIPT] no usable speech, scoring 0/FAIL: {transcript_key}")
            return {
                "score": 0,
                "result": "FAIL",
                "reasoning": "No usable speech was found in the submitted video — the recording appears muted or empty.",
                "positives": [],
                "negatives": [
                    "No speech detected in your video — the recording appears muted or empty | "
                    "Proof: the transcript generated from your video contains no words | "
                    "Suggestion: check that your microphone is on and not muted, re-record the video, "
                    "play it back to confirm the audio is clearly audible, then upload it again."
                ],
            }

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

        elif extra == "day_image":
            # Cross-folder pull: a spoken-design video (Day 6 'System Design
            # Problem 1') is judged against the diagram that lives in the SIBLING
            # problem folder ('System Design Problem 2'). Take the newest image
            # anywhere under the day, excluding this deliverable's own folder.
            img = llm_s3.find_first_image(
                s3, LLM_SETTINGS.bucket, day_prefix, exclude_prefix=deliverable_prefix
            )
            if img:
                tmp = Path("/tmp") / Path(img).name
                llm_s3.download(s3, LLM_SETTINGS.bucket, img, tmp)
                image_urls.append(llm_processor.image_to_data_url(tmp))
                tmp.unlink(missing_ok=True)
                print(f"[DAY-IMAGE] {deliverable_name} <- {img}")
            else:
                print(f"[WARN] no sibling image under day {day_prefix} for {deliverable_name}")

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
        deliverable_name=deliverable_name,
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
        return [t for t in n.replace("-", " ").replace("_", " ").split() if t]

    target = base_tokens(name_l)
    t_words = {t for t in target if not t.isdigit()}
    t_nums = {t for t in target if t.isdigit()}
    best = None
    for sib in siblings:
        sib_l = sib.lower()
        if sib_l == name_l:
            continue
        if want == "image" and not any(w in sib_l for w in ("image", "diagram")):
            continue
        if want == "text" and "text" not in sib_l:
            continue
        s_tokens = base_tokens(sib_l)
        s_words = {t for t in s_tokens if not t.isdigit()}
        s_nums = {t for t in s_tokens if t.isdigit()}
        # Words must overlap AND the deliverable numbers must match exactly, so
        # 'JD Alignment 1 video' pairs only with '_1 text' / '_1 Image' (never
        # set 2), and 'System Design Problem 1' never pulls Problem 2's diagram.
        if (s_words & t_words) and s_nums == t_nums:
            best = day_prefix + sib + "/"
            break
    return best


# ── overall coordination ───────────────────────────────────────────────────────

def _is_result_name_for_engine(name: str, engine: str) -> bool:
    """A per-deliverable result file for exactly ONE engine (not an overall).

    Whisper looks for the plain '_result.json'; AssemblyAI for '_result(A).json'.
    These never cross-match: '_result(A).json' does not contain '_result.json'
    (an '(A)' sits between), and vice versa. The OVERALL guard drops the day files
    (e.g. DayOVERALL_result.json / DayOVERALL(A)_result.json)."""
    if "OVERALL" in name:
        return False
    return _engine_result_suffix(engine) in name


def _maybe_write_overall(s3, deliverable_prefix, engine):
    """After a per-deliverable result is written, check if the day is complete
    FOR THIS ENGINE; if so, write that engine's day overall. Rollups never mix
    engines: (W) rolls up only (W) results, (A) only (A), each against the same
    expected-N from metadata.json. (Candidate overall stays disabled.)"""
    day_prefix = llm_s3.parent_prefix(deliverable_prefix)
    day_name = llm_s3.deliverable_name_from_prefix(day_prefix)

    # collect the LATEST same-engine result per deliverable folder under this day.
    # A deliverable can have several result files (one per attempt); keep max attempt.
    latest = {}   # deliverable subfolder prefix -> result dict
    for k in llm_s3.list_prefix(s3, LLM_SETTINGS.bucket, day_prefix):
        name = k.rsplit("/", 1)[-1]
        if not _is_result_name_for_engine(name, engine):
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
        print(f"[OVERALL] ({engine}) day '{day_name}' not complete ({len(results)}/{len(expected)})")
        return

    day_doc = llm_overall.combine(results, label_fields={
        "candidate": (deliverable_prefix.split("/", 1)[0]),
        "day": day_name,
        "engine": engine,
    })
    # Whisper keeps the original 'DayOVERALL_result.json'; AssemblyAI is tagged.
    day_key = day_prefix + f"DayOVERALL{engines.engine_tag(engine)}" + LLM_SETTINGS.result_suffix
    s3.put_object(Bucket=LLM_SETTINGS.bucket, Key=day_key,
                  Body=json.dumps(day_doc, indent=2).encode("utf-8"),
                  ContentType="application/json")
    print(f"[OVERALL] ({engine}) wrote {day_key} → {day_doc['result']} {day_doc['overallScore']}")

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

def _attempt_number(s3, deliverable_prefix, engine) -> int:
    """Attempt = number of THIS ENGINE's existing result files in the folder + 1.

    Engine-scoped: (A) never counts (W)'s files and vice versa, so each engine
    numbers its own attempts from 1."""
    suffix = _engine_result_suffix(engine)
    n = 0
    for k in llm_s3.list_prefix(s3, LLM_SETTINGS.bucket, deliverable_prefix):
        rel = k[len(deliverable_prefix):]
        if "/" in rel:
            continue
        if "OVERALL" in rel:
            continue
        if suffix in rel:
            n += 1
    return n + 1


def _finalize_engine_result(s3, *, deliverable_prefix, deliverable_name, result_id,
                            engine, result, attempt):
    """Persist ONE engine's result, stamp that engine's lineage, gate Salesforce
    (W only), log the outcome, and refresh that engine's day rollup.

    The result-dict SHAPE is unchanged — the engine lives in the FILENAME only —
    so the Salesforce payload for (W) stays byte-identical to today."""
    # the deliverable-result id is the Salesforce record id (prefix a1U...) baked
    # into the uploaded FILE name, e.g. "HR Questions Recording-(a1UO1000002Rd0vMAC).mp4".
    # Fall back to the folder id, then the raw stem, if no parenthesised id is found.
    deliverable_result_id = (_sf_id_from_name(result_id)
                             or _sf_id_from_name(deliverable_name)
                             or result_id)
    # copy so the two engines never share a mutated dict (image/text score-once)
    r = dict(result)
    r["deliverableResultId"] = deliverable_result_id
    r["attempt"] = attempt
    r["video"] = result_id   # the uploaded source file stem

    base_marker = LLM_SETTINGS.pass_marker if r.get("result") == "PASS" else LLM_SETTINGS.fail_marker
    # label includes the attempt number, e.g. (Fail)(Attempt-1), (Pass)(Attempt-2).
    # base marker stays a contiguous substring so is_tagged/skip detection still works.
    marker = f"{base_marker}(Attempt-{attempt})"

    # write the result file ALREADY labeled — engine tag before .json (none for
    # Whisper, '(A)' for AssemblyAI), state tag after:
    #   Whisper:    <name>_<id>_result.json(Pass)(Attempt-1)      ← original convention
    #   AssemblyAI: <name>_<id>_result(A).json(Pass)(Attempt-1)
    result_key = (f"{deliverable_prefix}{deliverable_name}_{result_id}"
                  f"{_engine_result_suffix(engine)}{marker}")
    s3.put_object(Bucket=LLM_SETTINGS.bucket, Key=result_key,
                  Body=json.dumps(r, indent=2).encode("utf-8"),
                  ContentType="application/json")
    print(f"[DONE] ({engine}) {result_key} → {r.get('result')} {r.get('score')} (attempt {attempt})")

    # Stamp this engine's lineage with the verdict. The result file is already
    # labeled above, so tag_folder_files skips it. AssemblyAI stamps ONLY its own
    # '(A)' files. Whisper (untagged) stamps every NON-'(A)' file — its transcript
    # plus the shared video/image, exactly like the original pipeline — while never
    # renaming AssemblyAI's in-flight '(A)' transcript.
    # NOTE: renaming is S3 copy+delete — the EC2 role needs s3:DeleteObject or the
    # originals stay behind as duplicates.
    a_tag = engines.engine_tag(engines.ASSEMBLYAI)   # "(A)"
    tag_kwargs = ({"only_tag": a_tag} if engine == engines.ASSEMBLYAI
                  else {"exclude_tag": a_tag})
    try:
        tagged = llm_s3.tag_folder_files(
            s3, LLM_SETTINGS.bucket, deliverable_prefix, marker,
            pass_marker=LLM_SETTINGS.pass_marker, fail_marker=LLM_SETTINGS.fail_marker,
            **tag_kwargs,
        )
        print(f"[TAG] ({engine}) {deliverable_name} → {marker} ({tagged} files)")
    except Exception as exc:
        print(f"[TAG-ERROR] ({engine}) {deliverable_name}: {exc}")

    # Salesforce gate: ONLY the production engine (W) pushes the full result.json;
    # (A) is S3-only and never calls Salesforce. notify() never raises and returns
    # a log dict; (A) writes a parallel "skipped" log so the A/B lineage matches.
    # Both logs are plain .json, so neither trigger Lambda re-processes them.
    if engine == engines.SF_ENGINE:
        sf_log = salesforce.notify(LLM_SETTINGS, r)
    else:
        sf_log = {
            "skipped": True,
            "engine": engine,
            "reason": "S3-only A/B engine — Salesforce push is Whisper-only",
            "deliverableResultId": deliverable_result_id,
            "success": False,
        }
        print(f"[SF] ({engine}) skipped Salesforce — S3-only engine")
    try:
        # Whisper sf_log keeps the original name '..._sf_log(Attempt-N).json';
        # AssemblyAI's is '..._sf_log(A)(Attempt-N).json'.
        log_key = (f"{deliverable_prefix}{deliverable_name}_{result_id}"
                   f"_sf_log{engines.engine_tag(engine)}(Attempt-{attempt}).json")
        s3.put_object(Bucket=LLM_SETTINGS.bucket, Key=log_key,
                      Body=json.dumps(sf_log, indent=2, default=str).encode("utf-8"),
                      ContentType="application/json")
        print(f"[SF-LOG] ({engine}) wrote {log_key} (success={sf_log.get('success')})")
    except Exception as exc:
        print(f"[SF-LOG-ERROR] ({engine}) {deliverable_name}: {exc}")

    # engine-scoped day rollup, under a per-(day, engine) lock so W and A never
    # block each other yet same-engine writers serialize.
    day_prefix = llm_s3.parent_prefix(deliverable_prefix)
    with _overall_lock(f"{day_prefix}|{engine}"):
        _maybe_write_overall(s3, deliverable_prefix, engine)


def _handle_transcript(s3, client, key, fname, deliverable_prefix, deliverable_name):
    """A video deliverable: each engine's transcript triggers its own genuine
    scoring (different transcript → potentially different verdict per engine)."""
    stem = fname[:-len(LLM_SETTINGS.transcript_suffix)]   # video stem (+ engine tag)
    engine, result_id = engines.split_engine_tag(stem)
    if engine is None:                       # legacy untagged transcript → Whisper
        engine, result_id = engines.DEFAULT_ENGINE, stem

    attempt = _attempt_number(s3, deliverable_prefix, engine)

    result = _gather_and_score(
        s3, client,
        deliverable_prefix=deliverable_prefix,
        deliverable_name=deliverable_name,
        transcript_key=key,
    )
    if result is None:
        return

    _finalize_engine_result(
        s3, deliverable_prefix=deliverable_prefix, deliverable_name=deliverable_name,
        result_id=result_id, engine=engine, result=result, attempt=attempt,
    )


def _handle_image_text(s3, client, fname, deliverable_prefix, deliverable_name):
    """An image/text deliverable (diagram, JD doc): score ONCE, then save the
    identical verdict as two engine-tagged copies sharing one attempt number — no
    second LLM call. Only the (W) copy reaches Salesforce."""
    result_id = Path(fname).stem
    # mirror the transcript stage exactly: only emit (A) copies when AssemblyAI is
    # actually enabled, so a no-key deploy produces zero (A) artifacts anywhere.
    target_engines = engines.configured_engines()
    if not LLM_SETTINGS.assemblyai_api_key:
        target_engines = [e for e in target_engines if e != engines.ASSEMBLYAI] or [engines.WHISPER]

    result = _gather_and_score(
        s3, client,
        deliverable_prefix=deliverable_prefix,
        deliverable_name=deliverable_name,
        transcript_key=None,
    )
    if result is None:
        return

    # one shared attempt number so the two copies stay in lockstep
    attempt = max(_attempt_number(s3, deliverable_prefix, e) for e in target_engines)

    for engine in target_engines:
        _finalize_engine_result(
            s3, deliverable_prefix=deliverable_prefix, deliverable_name=deliverable_name,
            result_id=result_id, engine=engine, result=result, attempt=attempt,
        )


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

    # An image/text is scored standalone ONLY when its deliverable rule asks for
    # the folder's OWN content (own_image / own_text) — e.g. Day 6 'System Design
    # Problem 2' diagram, JD '… Image'/'… Text', Team Structure 'Diagram'. An
    # image/text dropped into a video-only folder is a combined INPUT (pulled by a
    # sibling/day rule, e.g. Problem 1's video pulling Problem 2's diagram) and must
    # NOT spawn its own result, or attempt numbering and the day overall break.
    if kind in ("image", "text"):
        _rule = match_rule(deliverable_name)
        _extras = _rule[1] if _rule else []
        if kind == "image" and "own_image" not in _extras:
            print(f"[SKIP] image is combined input only (no own_image rule): {key}")
            return
        if kind == "text" and "own_text" not in _extras:
            print(f"[SKIP] text is combined input only (no own_text rule): {key}")
            return

    if kind == "transcript":
        # one transcript = one engine = one genuine scoring
        _handle_transcript(s3, client, key, fname, deliverable_prefix, deliverable_name)
    else:
        # image/text = one scoring, two engine-tagged copies
        _handle_image_text(s3, client, fname, deliverable_prefix, deliverable_name)


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