"""Trigger Lambda — S3 ObjectCreated → SQS transcript job.

Deploy separately as a Lambda function (Python 3.12). It fires on every new
object in the candidate-deliverables bucket and enqueues a transcript job for
real video files, ignoring transcripts, metadata, and folder markers so the
pipeline does not re-trigger itself.

Env var required: TRANSCRIPT_QUEUE_URL
"""

import json
import os
import urllib.parse

import boto3

SQS = boto3.client("sqs")
TRANSCRIPT_QUEUE_URL = os.environ["TRANSCRIPT_QUEUE_URL"]

VIDEO_EXTENSIONS = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpeg", ".mpg")


def _is_video(key: str) -> bool:
    lower = key.lower()
    if lower.endswith("/"):
        return False
    return lower.endswith(VIDEO_EXTENSIONS)


def lambda_handler(event, context):
    enqueued = 0
    for record in event.get("Records", []):
        bucket = record["s3"]["bucket"]["name"]
        key = urllib.parse.unquote_plus(record["s3"]["object"]["key"])

        if not _is_video(key):
            print(f"[SKIP] not a video: {key}")
            continue

        SQS.send_message(
            QueueUrl=TRANSCRIPT_QUEUE_URL,
            MessageBody=json.dumps({"bucket": bucket, "video_key": key}),
        )
        enqueued += 1
        print(f"[ENQUEUED] s3://{bucket}/{key}")

    return {"enqueued": enqueued}
