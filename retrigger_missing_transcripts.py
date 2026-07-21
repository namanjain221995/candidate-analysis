"""Backlog recovery — re-trigger transcription for videos that have NO transcript.

Why this exists: while the AssemblyAI `universal-3-pro` model was deprecated, every
transcript job was rejected with HTTP 400 and DROPPED (a 400 is treated as
permanent, so the SQS message was deleted, not dead-lettered). Those videos still
sit in S3 with no `_transcripts.txt`. Fixing the model only helps NEW jobs, so the
already-dropped ones need to be re-queued once. This script finds them and does so.

It is SAFE to run repeatedly:
  * It only touches videos that are genuinely missing a transcript (it checks for an
    existing transcript — untagged AND legacy '(A)', including ones already tagged
    with '(Pass)/(Attempt-N)').
  * The transcript worker itself also skips any video whose transcript already
    exists, so a re-queue can never double-write.
  * Re-queuing transcription auto-cascades to scoring: when the transcript lands in
    S3 it fires the LLM trigger Lambda -> scoring -> result -> Salesforce.

Two ways to re-trigger (pick based on your EC2 IAM permissions):
  DEFAULT (SQS): sends {"bucket","video_key"} straight to the transcript queue.
                 Needs sqs:SendMessage on the queue (see README note below).
  --via-s3     : re-copies each video onto itself (metadata touch) so S3 fires a
                 fresh ObjectCreated event -> your existing trigger Lambda enqueues
                 the job. Needs only S3 read/write, which the box already has.
                 (Requires your bucket notification to cover ObjectCreated:* / :Copy.)

Usage (run from the repo folder so .env loads):
  python retrigger_missing_transcripts.py                 # DRY RUN: list only, nothing changed
  python retrigger_missing_transcripts.py --run           # queue via SQS
  python retrigger_missing_transcripts.py --run --via-s3  # queue via S3 re-copy (no SQS perm needed)
  python retrigger_missing_transcripts.py --run --limit 5 # do just the first 5 (safe test)
"""

import json
import os
import sys
from collections import defaultdict

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

BUCKET = os.environ.get("BUCKET", "candidate-deliverables")
QUEUE_URL = os.environ.get("TRANSCRIPT_QUEUE_URL")
TRANSCRIPT_SUFFIX = os.environ.get("TRANSCRIPT_SUFFIX", "_transcripts.txt")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpeg", ".mpg")

# Match the app's S3 client (s3_store.build_s3_client): explicit region + SigV4, so
# signing/region behaviour is identical to the running services.
_S3_CONFIG = Config(signature_version="s3v4", retries={"max_attempts": 8, "mode": "standard"})

RUN = "--run" in sys.argv
VIA_S3 = "--via-s3" in sys.argv
LIMIT = None
if "--limit" in sys.argv:
    try:
        LIMIT = int(sys.argv[sys.argv.index("--limit") + 1])
    except (IndexError, ValueError):
        sys.exit("--limit needs a number, e.g. --limit 5")


def find_missing(s3):
    """Return [video_key, ...] for every video with no transcript beside it."""
    by_prefix = defaultdict(set)   # folder prefix -> {basename, ...}
    total = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            total += 1
            prefix, _, base = key.rpartition("/")
            by_prefix[(prefix + "/") if prefix else ""].add(base)

    missing = []
    for prefix, names in by_prefix.items():
        for base in names:
            if not base.lower().endswith(VIDEO_EXTS):
                continue  # not a raw video (tagged '.mp4(Pass)...' files are skipped too)
            stem = base.rsplit(".", 1)[0]
            t_plain = f"{stem}{TRANSCRIPT_SUFFIX}"        # AssemblyAI (untagged) name
            t_legacy = f"{stem}(A){TRANSCRIPT_SUFFIX}"    # old two-engine '(A)' name
            has_transcript = any(
                n.startswith(t_plain) or n.startswith(t_legacy) for n in names
            )
            if not has_transcript:
                missing.append(prefix + base)
    return total, sorted(missing)


def requeue_via_sqs(sqs, keys):
    for i, k in enumerate(keys, 1):
        try:
            sqs.send_message(
                QueueUrl=QUEUE_URL,
                MessageBody=json.dumps({"bucket": BUCKET, "video_key": k}),
            )
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("AccessDenied", "AccessDeniedException", "KMS.AccessDeniedException"):
                sys.exit(
                    f"\n[STOP] The EC2 role lacks sqs:SendMessage on the transcript queue "
                    f"(after {i - 1} sent).\n"
                    f"       Either attach the IAM policy from the README note, OR re-run "
                    f"this with --via-s3 (needs no SQS permission)."
                )
            raise
        print(f"  QUEUED (sqs)  {i}/{len(keys)}  {k}")


def requeue_via_s3(s3, keys):
    """Re-copy each video onto itself (metadata touch) so S3 re-fires ObjectCreated."""
    for i, k in enumerate(keys, 1):
        head = s3.head_object(Bucket=BUCKET, Key=k)
        meta = dict(head.get("Metadata", {}))
        meta["retrigger"] = "1"
        s3.copy_object(
            Bucket=BUCKET, Key=k,
            CopySource={"Bucket": BUCKET, "Key": k},
            MetadataDirective="REPLACE",
            Metadata=meta,
            ContentType=head.get("ContentType", "video/mp4"),
        )
        print(f"  RE-COPIED (s3)  {i}/{len(keys)}  {k}")


def main():
    if RUN and not VIA_S3 and not QUEUE_URL:
        sys.exit("TRANSCRIPT_QUEUE_URL is not set. Run from the candidate-analysis folder "
                 "so .env loads, or use --via-s3.")

    s3 = boto3.client("s3", region_name=AWS_REGION, config=_S3_CONFIG)
    total, missing = find_missing(s3)

    print(f"Scanned {total} objects in s3://{BUCKET}")
    print(f"Videos with NO transcript: {len(missing)}")
    for k in missing:
        print("  MISSING:", k)

    if not missing:
        print("Nothing to re-queue. Everything has a transcript.")
        return

    targets = missing[:LIMIT] if LIMIT else missing

    if not RUN:
        print(f"\nDRY RUN — nothing changed. {len(targets)} video(s) would be re-queued.")
        print("Re-run with --run (add --via-s3 if the box lacks sqs:SendMessage).")
        return

    print(f"\nRe-queuing {len(targets)} video(s) via {'S3 re-copy' if VIA_S3 else 'SQS'}...")
    if VIA_S3:
        requeue_via_s3(s3, targets)
    else:
        requeue_via_sqs(boto3.client("sqs", region_name=AWS_REGION), targets)
    print(f"\nDone. {len(targets)} video(s) re-queued for transcription.")
    print("Watch progress:  sudo journalctl -u transcript-service -f")


if __name__ == "__main__":
    main()
