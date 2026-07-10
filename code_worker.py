"""Code-evaluation worker (S3 + SQS) — Advanced Training coding assignments.

NEW, ADDITIVE SERVICE. Runs on the EC2 next to the transcript/LLM workers and
consumes the `notebook-jobs` queue fed by the Router Lambda. It REUSES the
existing modules unchanged:
  - llm_processor.evaluate  → the OpenAI call, retries, and JSON parsing
  - salesforce.notify       → the Salesforce push (same Apex endpoint/schema)
  - llm_s3                   → S3 download / list / tag helpers
  - code_processor          → reads the notebook/script into model input (NEW)
  - code_config             → settings + Day→prompt mapping (NEW)

Message shape produced by the Router Lambda (kind = notebook | python):
  { "bucket", "key", "kind", "file_extension",
    "event_name", "etag", "sequencer", "version_id", "size" }

Per message the worker:
  1. skips already-tagged files (earlier attempts) — defence in depth vs the Router
  2. duplicate-event guard: if this exact upload was already processed, ack & stop
     (a re-delivered event must NOT create a new attempt)
  3. downloads the .ipynb/.py from S3
  4. parses it (code + outputs + charts) — charts go to the vision model
  5. picks the Day's prompt and calls OpenAI via llm_processor.evaluate
  6. writes <name>_<id>_result.json(Pass|Fail)(Attempt-N), tags the source file,
     pushes to Salesforce (if enabled), writes an _sf_log, drops a dedup marker
  7. deletes the SQS message on success (failure → SQS retry → notebook-jobs-dlq)

Candidate code is never executed, so no sandbox is required. Attempt numbering is
max(existing Attempt-N) + 1 (gap-safe). No engine tags apply (Whisper/AssemblyAI
are transcription-only), so results use the original untagged naming.
"""

import json
import re
import signal
import tempfile
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

import boto3
import requests

import llm_s3
import salesforce
import llm_processor
import code_processor
from code_config import CODE_SETTINGS, match_code_prompt

_stop = threading.Event()

# Salesforce record id baked into a file/folder name, e.g. "...(a1UO1000002Rd0vMAC)".
# Mirrors llm_worker's extraction so code results reach the same record convention.
_SF_ID_RE = re.compile(r"\(([A-Za-z0-9]{15,18})\)")
_ATTEMPT_RE = re.compile(r"\(Attempt-(\d+)\)")

_WINDOWS_FORBIDDEN = re.compile(r'[<>:"/\\|?*]')


def _sf_id_from_name(name: str):
    m = _SF_ID_RE.search(name or "")
    return m.group(1) if m else None


def _safe_filename(name: str, fallback: str = "submission") -> str:
    name = _WINDOWS_FORBIDDEN.sub("_", (name or "").strip()) or fallback
    return name


# ── attempt numbering (gap-safe: max existing Attempt-N + 1) ────────────────────

def _attempt_number(s3, deliverable_prefix: str) -> int:
    highest = 0
    for k in llm_s3.list_prefix(s3, CODE_SETTINGS.bucket, deliverable_prefix):
        rel = k[len(deliverable_prefix):]
        if "/" in rel:                      # nested (e.g. _processed/ markers)
            continue
        if "OVERALL" in rel:
            continue
        if CODE_SETTINGS.result_suffix not in rel:
            continue
        m = _ATTEMPT_RE.search(rel)
        if m:
            highest = max(highest, int(m.group(1)))
    return highest + 1


# ── duplicate-event protection (per-upload marker in S3) ────────────────────────

def _dedup_identity(body: dict) -> str:
    """Identity of THIS upload. A re-delivered event repeats it; a genuine
    re-upload changes it (new version_id/etag)."""
    return body.get("version_id") or body.get("etag") or body.get("sequencer") or ""


def _marker_key(deliverable_prefix: str, identity: str) -> str:
    safe = re.sub(r"[^A-Za-z0-9_.-]", "_", identity)[:120]
    return f"{deliverable_prefix}_processed/{safe}"


def _already_processed(s3, deliverable_prefix: str, identity: str) -> bool:
    if not identity:
        return False
    try:
        s3.head_object(Bucket=CODE_SETTINGS.bucket,
                       Key=_marker_key(deliverable_prefix, identity))
        return True
    except Exception:
        return False


def _mark_processed(s3, deliverable_prefix: str, identity: str, body: dict) -> None:
    if not identity:
        return
    payload = {"processedAt": datetime.now(timezone.utc).isoformat(),
               "key": body.get("key"), "etag": body.get("etag"),
               "version_id": body.get("version_id"), "sequencer": body.get("sequencer")}
    s3.put_object(Bucket=CODE_SETTINGS.bucket,
                  Key=_marker_key(deliverable_prefix, identity),
                  Body=json.dumps(payload).encode("utf-8"),
                  ContentType="application/json")


# ── deterministic FAIL (empty / unreadable submission) ──────────────────────────

def _bad_submission_result(reason: str) -> dict:
    return {
        "score": 0,
        "result": "FAIL",
        "reasoning": reason,
        "positives": [],
        "negatives": [
            f"{reason} | Suggestion: re-run all cells so outputs are saved, "
            "then re-upload a valid .ipynb (or a readable .py) file."
        ],
    }


# ── grade one submission ────────────────────────────────────────────────────────

def _grade(client, *, kind: str, deliverable_name: str, prompt_file: str, content: str, images):
    if not (content or "").strip() and not images:
        return _bad_submission_result(
            "The submission appears empty — no code or outputs were found.")

    system_prompt = code_processor.load_prompt(CODE_SETTINGS, prompt_file)
    header = ("CANDIDATE CODE SUBMISSION "
              + ("(Jupyter notebook: code + saved outputs)"
                 if kind == "notebook" else "(Python script: code only, no outputs)")
              + ":\n")

    print(f"[LLM] grading {deliverable_name} ({prompt_file})"
          f"{' +' + str(len(images)) + 'img' if images else ''}")

    result = llm_processor.evaluate(
        CODE_SETTINGS, client,
        system_prompt=system_prompt,
        deliverable_name=deliverable_name,
        extra_text=header + content,
        image_data_urls=images or None,
    )
    result["deliverable"] = deliverable_name
    return result


# ── persist result + tag + Salesforce ───────────────────────────────────────────

def _finalize(s3, *, deliverable_prefix, deliverable_name, fname, kind, body, result, identity):
    result_id = Path(fname).stem
    deliverable_result_id = (_sf_id_from_name(result_id)
                             or _sf_id_from_name(deliverable_name)
                             or _sf_id_from_name(deliverable_prefix.split("/", 1)[0])
                             or result_id)
    attempt = _attempt_number(s3, deliverable_prefix)

    r = dict(result)
    r["deliverableResultId"] = deliverable_result_id
    r["attempt"] = attempt
    r["kind"] = kind
    r["file_extension"] = body.get("file_extension")
    r["source"] = result_id
    r["video"] = result_id   # mirror the existing result schema for Salesforce compat

    base_marker = CODE_SETTINGS.pass_marker if r.get("result") == "PASS" else CODE_SETTINGS.fail_marker
    marker = f"{base_marker}(Attempt-{attempt})"

    result_key = (f"{deliverable_prefix}{deliverable_name}_{result_id}"
                  f"{CODE_SETTINGS.result_suffix}{marker}")
    s3.put_object(Bucket=CODE_SETTINGS.bucket, Key=result_key,
                  Body=json.dumps(r, indent=2).encode("utf-8"),
                  ContentType="application/json")
    print(f"[DONE] {result_key} → {r.get('result')} {r.get('score')} (attempt {attempt})")

    # Tag the untagged files in this folder (the source .ipynb/.py) with the same
    # marker so a tagged file matches no Router route and is never re-processed.
    # The result file is already tagged (skipped); _processed/ markers are nested
    # (skipped). Requires s3:DeleteObject (already granted for the LLM worker).
    try:
        tagged = llm_s3.tag_folder_files(
            s3, CODE_SETTINGS.bucket, deliverable_prefix, marker,
            pass_marker=CODE_SETTINGS.pass_marker, fail_marker=CODE_SETTINGS.fail_marker)
        print(f"[TAG] {deliverable_name} → {marker} ({tagged} files)")
    except Exception as exc:
        print(f"[TAG-ERROR] {deliverable_name}: {exc}")

    # Salesforce push (reuses salesforce.notify — never raises).
    if CODE_SETTINGS.sf_enabled:
        sf_log = salesforce.notify(CODE_SETTINGS, r)
    else:
        sf_log = {"skipped": True, "reason": "SF_ENABLED=false",
                  "deliverableResultId": deliverable_result_id, "success": False}
        print("[SF] disabled (SF_ENABLED=false), skip")
    try:
        log_key = (f"{deliverable_prefix}{deliverable_name}_{result_id}"
                   f"_sf_log(Attempt-{attempt}).json")
        s3.put_object(Bucket=CODE_SETTINGS.bucket, Key=log_key,
                      Body=json.dumps(sf_log, indent=2, default=str).encode("utf-8"),
                      ContentType="application/json")
        print(f"[SF-LOG] wrote {log_key} (success={sf_log.get('success')})")
    except Exception as exc:
        print(f"[SF-LOG-ERROR] {deliverable_name}: {exc}")

    # Record this upload as processed so a duplicate event won't re-grade it.
    if CODE_SETTINGS.dedup_enabled:
        try:
            _mark_processed(s3, deliverable_prefix, identity, body)
        except Exception as exc:
            print(f"[DEDUP-MARK-ERROR] {deliverable_name}: {exc}")


# ── message handling ────────────────────────────────────────────────────────────

def _handle(s3, client, body: dict):
    key = body["key"]
    kind = body.get("kind", "notebook")
    fname = key.rsplit("/", 1)[-1]

    # tagged files are earlier attempts — never re-process
    if CODE_SETTINGS.pass_marker in fname or CODE_SETTINGS.fail_marker in fname:
        print(f"[SKIP] already-tagged file: {fname}")
        return

    deliverable_prefix = llm_s3.prefix_of(key)
    deliverable_name = llm_s3.deliverable_name_from_prefix(deliverable_prefix)
    # The Day (training-step) folder carries the day number + topic, e.g.
    # "Day 2 - Regression(...)"; combine it with the deliverable name so the rubric
    # prompt is picked reliably even if the assignment folder omits either.
    day_name = llm_s3.deliverable_name_from_prefix(llm_s3.parent_prefix(deliverable_prefix))
    prompt_file = match_code_prompt(f"{day_name} {deliverable_name}")

    # duplicate-event guard BEFORE any attempt/grading
    identity = _dedup_identity(body)
    if CODE_SETTINGS.dedup_enabled and _already_processed(s3, deliverable_prefix, identity):
        print(f"[DUP] upload already processed ({identity}) — ack without new attempt: {key}")
        return

    with tempfile.TemporaryDirectory() as td:
        local = Path(td) / _safe_filename(fname)
        print(f"[DL] s3://{CODE_SETTINGS.bucket}/{key}")
        llm_s3.download(s3, CODE_SETTINGS.bucket, key, local)
        raw = local.read_bytes()

    try:
        if kind == "notebook":
            content, images = code_processor.parse_notebook(raw, CODE_SETTINGS)
        else:
            content, images = code_processor.read_python(raw, CODE_SETTINGS)
    except ValueError as exc:
        print(f"[BAD-SUBMISSION] {key}: {exc}")
        content, images = "", []
        result = _bad_submission_result(f"Could not read the submission: {exc}")
        result["deliverable"] = deliverable_name
    else:
        result = _grade(client, kind=kind, deliverable_name=deliverable_name,
                        prompt_file=prompt_file, content=content, images=images)

    _finalize(s3, deliverable_prefix=deliverable_prefix, deliverable_name=deliverable_name,
              fname=fname, kind=kind, body=body, result=result, identity=identity)


def _worker(n: int):
    sqs = boto3.client("sqs", region_name=CODE_SETTINGS.aws_region)
    s3 = llm_s3.build_s3(CODE_SETTINGS)
    client = requests.Session()
    client.headers.update({"Authorization": f"Bearer {CODE_SETTINGS.openai_api_key}"})
    print(f"[CODE-WORKER {n}] started")

    while not _stop.is_set():
        try:
            resp = sqs.receive_message(
                QueueUrl=CODE_SETTINGS.notebook_queue_url, MaxNumberOfMessages=1,
                WaitTimeSeconds=CODE_SETTINGS.sqs_wait_seconds,
                VisibilityTimeout=CODE_SETTINGS.sqs_visibility_timeout)
        except Exception as exc:
            print(f"[CODE-WORKER {n}][SQS-ERROR] {exc}")
            time.sleep(CODE_SETTINGS.failure_sleep_seconds)
            continue

        msgs = resp.get("Messages", [])
        if not msgs:
            continue
        msg = msgs[0]
        receipt = msg["ReceiptHandle"]
        try:
            _handle(s3, client, json.loads(msg["Body"]))
            sqs.delete_message(QueueUrl=CODE_SETTINGS.notebook_queue_url, ReceiptHandle=receipt)
        except Exception as exc:
            print(f"[CODE-WORKER {n}][ERROR] {exc}  (will retry via SQS → DLQ after max receives)")
            time.sleep(CODE_SETTINGS.failure_sleep_seconds)

    print(f"[CODE-WORKER {n}] stopped")


def main():
    CODE_SETTINGS.validate()
    signal.signal(signal.SIGINT, lambda *a: _stop.set())
    signal.signal(signal.SIGTERM, lambda *a: _stop.set())
    print(f"[CODE-MAIN] bucket={CODE_SETTINGS.bucket} queue={CODE_SETTINGS.notebook_queue_url}")
    print(f"[CODE-MAIN] threads={CODE_SETTINGS.worker_threads} model={CODE_SETTINGS.openai_model} "
          f"sf_enabled={CODE_SETTINGS.sf_enabled}")
    threads = [threading.Thread(target=_worker, args=(i + 1,), daemon=True)
               for i in range(CODE_SETTINGS.worker_threads)]
    for t in threads:
        t.start()
    while not _stop.is_set():
        time.sleep(1)


if __name__ == "__main__":
    main()
