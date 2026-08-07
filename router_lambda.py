"""
Router Lambda - single S3 ObjectCreated handler that routes an uploaded file to the
correct SQS queue. Deployed in AWS as the `transcript-enqueue-trigger` function.

Keep only ONE S3 ObjectCreated notification pointing to this Lambda (avoids
overlapping-notification errors).

Routing by file type:
  1. Video (.mp4/.mov/.avi/.mkv/.webm/.m4v/.mpeg/.mpg)      -> transcript-jobs
        { "bucket", "video_key" }
  2. Transcript (*_transcripts.txt)                          -> llm-jobs
        { "bucket", "key", "kind": "transcript" }
  3. Image (.png/.jpg/.jpeg/.webp/.gif)                      -> llm-jobs  (kind=image)
  4. Other .txt (not a transcript)                           -> llm-jobs  (kind=text)
  5. Advanced-Training code (.ipynb/.py/.pyw/.ipy) that sits inside
        trainingSteps/ .../ ...Coding Assignment(...)/       -> notebook-jobs
        { "bucket", "key", "kind": "notebook"|"python", "file_extension", +dedup }

Ignored: folder markers, generated result/sf_log/OVERALL files, already-tagged
Pass/Fail files, unsupported files, and code files outside a Coding Assignment path.

RELIABILITY (this is the hardening):
  - Every SQS send is retried a few times on a transient failure (_send_with_retry),
    so a momentary throttle/network blip does not drop the job.
  - Each S3 record is routed in isolation (_route_record in try/except), so one bad
    file can never stop the other files in the same event.
  - If any record still fails, the handler RAISES at the end, so S3/Lambda async-retry
    (and the configured dead-letter queue) recover it instead of silently losing it.

Required env vars:  TRANSCRIPT_QUEUE_URL, LLM_QUEUE_URL, NOTEBOOK_QUEUE_URL
Optional env vars:  TRANSCRIPT_SUFFIX (default _transcripts.txt),
                    RESULT_SUFFIX (default _result.json),
                    MAX_SEND_ATTEMPTS (default 4)
"""

import json
import os
import time
import urllib.parse

import boto3


# ---------- AWS client ----------
SQS = boto3.client("sqs")

# ---------- queue URLs (required) ----------
TRANSCRIPT_QUEUE_URL = os.environ["TRANSCRIPT_QUEUE_URL"]
LLM_QUEUE_URL = os.environ["LLM_QUEUE_URL"]
NOTEBOOK_QUEUE_URL = os.environ["NOTEBOOK_QUEUE_URL"]

# ---------- optional settings ----------
TRANSCRIPT_SUFFIX = os.environ.get("TRANSCRIPT_SUFFIX", "_transcripts.txt")
RESULT_SUFFIX = os.environ.get("RESULT_SUFFIX", "_result.json")
# how many times to retry a single SQS send before giving up on that one record
MAX_SEND_ATTEMPTS = int(os.environ.get("MAX_SEND_ATTEMPTS", "4"))

# ---------- supported extensions ----------
VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpeg", ".mpg")
IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")
PYTHON_CODE_EXTS = (".ipynb", ".py", ".pyw", ".ipy")


# =========================================================
# HELPERS (routing decisions - unchanged from the original)
# =========================================================
def is_generated_or_processed_file(low_key):
    """True when the object must NOT be routed again (folders, results, tagged files)."""
    filename = low_key.rsplit("/", 1)[-1]
    if low_key.endswith("/"):
        return True
    if low_key.endswith(RESULT_SUFFIX.lower()):
        return True
    if "_result" in filename and ".json" in filename:
        return True
    if "_sf_log" in filename and ".json" in filename:
        return True
    if "overall" in filename:
        return True
    if "(pass)" in filename or "(fail)" in filename:
        return True
    return False


def is_code_submission(low_key):
    """True only for a supported code file inside a trainingSteps/.../Coding Assignment path."""
    if not low_key.endswith(PYTHON_CODE_EXTS):
        return False
    path_parts = [part for part in low_key.split("/") if part]
    if len(path_parts) < 2:
        return False
    inside_training_steps = "trainingsteps" in path_parts
    inside_coding_assignment = any(
        "coding assignment" in folder_name for folder_name in path_parts[:-1]
    )
    return inside_training_steps and inside_coding_assignment


def get_code_kind(low_key):
    return "notebook" if low_key.endswith(".ipynb") else "python"


def get_file_extension(key):
    return os.path.splitext(key)[1].lower()


def get_queue_name(queue_url):
    return queue_url.rstrip("/").rsplit("/", 1)[-1]


# =========================================================
# RELIABILITY: send one message, retrying transient failures
# =========================================================
def _send_with_retry(queue_url, message_body, key):
    """Send one SQS message, retrying transient failures (throttling, brief network
    or 5xx errors) a few times with a short backoff. Raises the last error ONLY if
    every attempt fails, so the caller can surface it instead of swallowing it."""
    body = json.dumps(message_body)
    last_err = None
    for attempt in range(1, MAX_SEND_ATTEMPTS + 1):
        try:
            return SQS.send_message(QueueUrl=queue_url, MessageBody=body)
        except Exception as exc:
            last_err = exc
            print(f"[SQS SEND RETRY {attempt}/{MAX_SEND_ATTEMPTS}] "
                  f"QueueName={get_queue_name(queue_url)} Key={key} error={exc}")
            time.sleep(min(2.0, 0.25 * attempt))
    raise last_err


def send_sqs_message(route_name, queue_url, message_body, key):
    """Send one message (with retries) and print detailed routing logs."""
    queue_name = get_queue_name(queue_url)
    print(f"[QUEUE SELECTED] Route={route_name} QueueName={queue_name} "
          f"QueueUrl={queue_url} Key={key}")
    print(f"[SQS SEND START] QueueName={queue_name} "
          f"MessageBody={json.dumps(message_body, default=str)}")

    response = _send_with_retry(queue_url, message_body, key)

    print(f"[SQS SEND SUCCESS] Route={route_name} QueueName={queue_name} "
          f"QueueUrl={queue_url} MessageId={response.get('MessageId')} "
          f"MD5OfMessageBody={response.get('MD5OfMessageBody')} "
          f"HTTPStatusCode={response.get('ResponseMetadata', {}).get('HTTPStatusCode')} "
          f"Key={key}")
    return response


# =========================================================
# Route ONE S3 record (isolated so one bad file can't stop the rest)
# =========================================================
def _route_record(record, routed):
    bucket = record["s3"]["bucket"]["name"]
    object_data = record["s3"]["object"]
    key = urllib.parse.unquote_plus(object_data["key"])   # S3 keys are URL-encoded
    low = key.lower()

    print(f"[RECEIVED] s3://{bucket}/{key}")

    # 1. Ignore generated / tagged / folder objects
    if is_generated_or_processed_file(low):
        routed["skipped"] += 1
        print(f"[SKIP generated/processed] {key}")
        return

    # 2. Advanced Training code -> notebook-jobs
    if is_code_submission(low):
        code_kind = get_code_kind(low)
        file_extension = get_file_extension(key)
        code_message = {
            "bucket": bucket,
            "key": key,
            "kind": code_kind,
            "file_extension": file_extension,
            # help the worker detect duplicate S3/SQS delivery of the same upload
            "event_name": record.get("eventName"),
            "etag": object_data.get("eTag"),
            "sequencer": object_data.get("sequencer"),
            "version_id": object_data.get("versionId"),
            "size": object_data.get("size"),
        }
        response = send_sqs_message("code", NOTEBOOK_QUEUE_URL, code_message, key)
        routed["code"] += 1
        print(f"[CODE] Kind={code_kind} Extension={file_extension} "
              f"MessageId={response.get('MessageId')} Key={key}")
        return

    # 3. Video -> transcript-jobs
    if low.endswith(VIDEO_EXTS):
        transcript_message = {"bucket": bucket, "video_key": key}
        response = send_sqs_message("transcript", TRANSCRIPT_QUEUE_URL, transcript_message, key)
        routed["transcript"] += 1
        print(f"[TRANSCRIPT] MessageId={response.get('MessageId')} Key={key}")
        return

    # 4. Transcript file -> llm-jobs
    if low.endswith(TRANSCRIPT_SUFFIX.lower()):
        llm_message = {"bucket": bucket, "key": key, "kind": "transcript"}
        response = send_sqs_message("llm", LLM_QUEUE_URL, llm_message, key)
        routed["llm"] += 1
        print(f"[LLM transcript] MessageId={response.get('MessageId')} Key={key}")
        return

    # 5. Image -> llm-jobs
    if low.endswith(IMAGE_EXTS):
        llm_message = {"bucket": bucket, "key": key, "kind": "image"}
        response = send_sqs_message("llm", LLM_QUEUE_URL, llm_message, key)
        routed["llm"] += 1
        print(f"[LLM image] MessageId={response.get('MessageId')} Key={key}")
        return

    # 6. Other text -> llm-jobs
    if low.endswith(".txt"):
        llm_message = {"bucket": bucket, "key": key, "kind": "text"}
        response = send_sqs_message("llm", LLM_QUEUE_URL, llm_message, key)
        routed["llm"] += 1
        print(f"[LLM text] MessageId={response.get('MessageId')} Key={key}")
        return

    # 7. Unsupported object
    routed["skipped"] += 1
    print(f"[SKIP unsupported] {key}")


# =========================================================
# MAIN HANDLER
# =========================================================
def lambda_handler(event, context):
    routed = {"transcript": 0, "llm": 0, "code": 0, "skipped": 0}
    records = event.get("Records", [])

    print(f"[EVENT] Number of S3 records received: {len(records)}")
    print("[QUEUE CONFIG] "
          f"TranscriptQueueName={get_queue_name(TRANSCRIPT_QUEUE_URL)} "
          f"TranscriptQueueUrl={TRANSCRIPT_QUEUE_URL} | "
          f"LLMQueueName={get_queue_name(LLM_QUEUE_URL)} "
          f"LLMQueueUrl={LLM_QUEUE_URL} | "
          f"CodeQueueName={get_queue_name(NOTEBOOK_QUEUE_URL)} "
          f"CodeQueueUrl={NOTEBOOK_QUEUE_URL}")

    failed = []
    for record in records:
        try:
            _route_record(record, routed)
        except Exception as exc:
            raw = record.get("s3", {}).get("object", {}).get("key", "<unknown>")
            key = urllib.parse.unquote_plus(raw)
            failed.append(key)
            print(f"[RECORD FAILED] Key={key} error={exc}")

    routing_summary = {
        "counts": routed,
        "failed": failed,
        "queues": {
            "transcript": {"name": get_queue_name(TRANSCRIPT_QUEUE_URL), "url": TRANSCRIPT_QUEUE_URL},
            "llm": {"name": get_queue_name(LLM_QUEUE_URL), "url": LLM_QUEUE_URL},
            "code": {"name": get_queue_name(NOTEBOOK_QUEUE_URL), "url": NOTEBOOK_QUEUE_URL},
        },
    }
    print(f"[ROUTED] {json.dumps(routing_summary)}")

    # If anything failed, raise so S3/Lambda async-retry (and the DLQ, if configured)
    # can recover it. A single-file event just retries that one file (no duplicates).
    if failed:
        raise RuntimeError(f"{len(failed)} record(s) failed to route: {failed}")

    return routed
