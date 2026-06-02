"""LLM trigger Lambda — S3 ObjectCreated → SQS llm-jobs.

Decides the job "kind" from the file that landed:
  - *_transcripts.txt        → kind=transcript   (video deliverable, scored from transcript)
  - .png/.jpg/.jpeg/.webp/.gif → kind=image       (image-only deliverable, scored via vision)
  - other .txt (not transcript) → kind=text        (e.g. JD text)
Videos (.mp4 etc.) and result.json files are ignored here — videos are handled
by the transcript stage, and results must not re-trigger scoring.

Env var: LLM_QUEUE_URL
Optional: TRANSCRIPT_SUFFIX (default _transcripts.txt), RESULT_SUFFIX (default _result.json)
"""

import json
import os
import urllib.parse

import boto3

SQS = boto3.client("sqs")
LLM_QUEUE_URL = os.environ["LLM_QUEUE_URL"]
TRANSCRIPT_SUFFIX = os.environ.get("TRANSCRIPT_SUFFIX", "_transcripts.txt")
RESULT_SUFFIX = os.environ.get("RESULT_SUFFIX", "_result.json")

IMAGE_EXTS = (".png", ".jpg", ".jpeg", ".webp", ".gif")


def _kind_for(key: str):
    low = key.lower()
    if low.endswith("/"):
        return None
    if low.endswith(RESULT_SUFFIX.lower()) or "overall" in low:
        return None
    if low.endswith(TRANSCRIPT_SUFFIX.lower()):
        return "transcript"
    if low.endswith(IMAGE_EXTS):
        return "image"
    if low.endswith(".txt"):
        return "text"
    return None  # videos and everything else ignored here


def lambda_handler(event, context):
    enqueued = 0
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])
        kind = _kind_for(key)
        if not kind:
            print(f"[SKIP] {key}")
            continue
        SQS.send_message(
            QueueUrl=LLM_QUEUE_URL,
            MessageBody=json.dumps({"bucket": bucket, "key": key, "kind": kind}),
        )
        enqueued += 1
        print(f"[ENQUEUED {kind}] {key}")
    return {"enqueued": enqueued}
