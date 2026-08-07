"""Backlog recovery — re-trigger stuck deliverables.

TWO MODES:

1) TRANSCRIPTS (default): re-transcribe videos that have NO transcript.
   While the AssemblyAI `universal-3-pro` model was deprecated, transcript jobs
   were rejected with HTTP 400 and DROPPED (permanent -> message deleted). Those
   videos still sit in S3 with no `_transcripts.txt`. This finds and re-queues them.
   Re-queuing transcription auto-cascades to scoring (transcript write -> LLM trigger).

2) LINKS (--links): acknowledge Github/Kaggle LINK submissions (.txt) that were
   uploaded before the link handler existed, so they get their PASS + attempt in
   Salesforce. It finds link `.txt` files with no `(Pass)/(Fail)` tag and re-queues
   them to the LLM queue (kind="text") -> the worker's link handler does the rest.

Both modes are SAFE to run repeatedly: they only touch items that are still pending
(no transcript / no Pass tag), and the workers themselves skip already-done items.

Two ways to re-trigger (pick based on your EC2 IAM permissions):
  DEFAULT (SQS): sends a message straight to the queue. Needs sqs:SendMessage on it.
  --via-s3     : re-copies each object onto itself (metadata touch) so S3 fires a
                 fresh ObjectCreated event -> your existing trigger Lambda enqueues
                 the job. Needs only S3 read/write, which the box already has.
                 (Requires your bucket notification to cover ObjectCreated:* / :Copy.)

Usage (run from the repo folder so .env loads):
  # transcripts
  python retrigger_missing_transcripts.py                    # DRY RUN
  python retrigger_missing_transcripts.py --run              # queue via SQS
  python retrigger_missing_transcripts.py --run --via-s3     # queue via S3 re-copy
  python retrigger_missing_transcripts.py --run --limit 5    # just the first 5
  # links
  python retrigger_missing_transcripts.py --links            # DRY RUN (link .txt)
  python retrigger_missing_transcripts.py --links --run      # acknowledge them
  python retrigger_missing_transcripts.py --links --run --via-s3
  # sweep — SELF-HEALING: re-queue ANY deliverable missing its next artifact
  #   (video->transcript, transcript->result, code->result) across ALL stages.
  #   Grace-windowed (SWEEP_GRACE_MINUTES, default 15) so in-flight jobs are left
  #   alone. Meant to run on a schedule (systemd timer / cron) in --run mode.
  python retrigger_missing_transcripts.py --sweep            # DRY RUN (all stages)
  python retrigger_missing_transcripts.py --sweep --run      # re-queue everything pending
"""

import json
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone, timedelta

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

import boto3
from botocore.config import Config
from botocore.exceptions import ClientError

BUCKET = os.environ.get("BUCKET", "candidate-deliverables")
TRANSCRIPT_QUEUE_URL = os.environ.get("TRANSCRIPT_QUEUE_URL")
LLM_QUEUE_URL = os.environ.get("LLM_QUEUE_URL")
TRANSCRIPT_SUFFIX = os.environ.get("TRANSCRIPT_SUFFIX", "_transcripts.txt")
AWS_REGION = os.environ.get("AWS_REGION", "us-east-1")
PASS_MARKER = os.environ.get("LLM_PASS_MARKER", "(Pass)")
FAIL_MARKER = os.environ.get("LLM_FAIL_MARKER", "(Fail)")
LINK_KEYWORDS = [k.strip().lower() for k in
                 os.environ.get("LINK_KEYWORDS", "github link,kaggle link").split(",") if k.strip()]
VIDEO_EXTS = (".mp4", ".mov", ".avi", ".mkv", ".webm", ".m4v", ".mpeg", ".mpg")
# --sweep additions (self-healing reconciliation). CODE_EXTS + the code queue let
# the sweep also cover ungraded notebooks/scripts; RESULT_MARKER is how it tells a
# deliverable was already scored; SWEEP_GRACE_MINUTES is a safety window so a job
# whose source file only just landed (still in flight) is never double-queued.
CODE_EXTS = (".ipynb", ".py", ".pyw", ".ipy")
RESULT_MARKER = os.environ.get("RESULT_SUFFIX", "_result.json")
NOTEBOOK_QUEUE_URL = os.environ.get("NOTEBOOK_QUEUE_URL")
try:
    SWEEP_GRACE_MINUTES = int(os.environ.get("SWEEP_GRACE_MINUTES", "15"))
except ValueError:
    SWEEP_GRACE_MINUTES = 15

# Match the app's S3 client (s3_store.build_s3_client): explicit region + SigV4, so
# signing/region behaviour is identical to the running services.
_S3_CONFIG = Config(signature_version="s3v4", retries={"max_attempts": 8, "mode": "standard"})

RUN = "--run" in sys.argv
VIA_S3 = "--via-s3" in sys.argv
LINKS = "--links" in sys.argv
SWEEP = "--sweep" in sys.argv
LIMIT = None
if "--limit" in sys.argv:
    try:
        LIMIT = int(sys.argv[sys.argv.index("--limit") + 1])
    except (IndexError, ValueError):
        sys.exit("--limit needs a number, e.g. --limit 5")


def find_missing_transcripts(s3):
    """(total, [video_key, ...]) for every video with no transcript beside it."""
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


def find_link_submissions(s3):
    """(total, [txt_key, ...]) for link .txt files not yet acknowledged.

    A processed link is tagged '..._link.txt(Pass)(Attempt-N)' (ends in the marker,
    not '.txt'), so an untagged '.txt' under a link deliverable is still pending.
    """
    pending = []   # (last_modified, key)
    total = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            total += 1
            low = key.lower()
            if not low.endswith(".txt"):
                continue
            if PASS_MARKER.lower() in low or FAIL_MARKER.lower() in low:
                continue  # already acknowledged
            if not any(kw in low for kw in LINK_KEYWORDS):
                continue  # not a link deliverable
            pending.append((obj.get("LastModified"), key))
    # OLDEST-upload first, so attempts are acknowledged in submission order
    # (Attempt-1, -2, …) and the LATEST submission gets the highest attempt.
    pending.sort(key=lambda t: t[0].timestamp() if t[0] is not None else 0.0)
    return total, [k for _, k in pending]


def _requeue_sqs(queue_url, queue_label, keys, body_fn):
    sqs = boto3.client("sqs", region_name=AWS_REGION)
    for i, k in enumerate(keys, 1):
        try:
            sqs.send_message(QueueUrl=queue_url, MessageBody=json.dumps(body_fn(k)))
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code")
            if code in ("AccessDenied", "AccessDeniedException", "KMS.AccessDeniedException"):
                sys.exit(
                    f"\n[STOP] The EC2 role lacks sqs:SendMessage on the {queue_label} queue "
                    f"(after {i - 1} sent).\n"
                    f"       Attach an sqs:SendMessage policy for it, OR re-run with --via-s3 "
                    f"(needs no SQS permission)."
                )
            raise
        print(f"  QUEUED (sqs)  {i}/{len(keys)}  {k}")


def requeue_via_s3(s3, keys):
    """Re-copy each object onto itself (metadata touch) so S3 re-fires ObjectCreated.
    Works for both videos and link .txt — the object's own ContentType is preserved."""
    for i, k in enumerate(keys, 1):
        head = s3.head_object(Bucket=BUCKET, Key=k)
        meta = dict(head.get("Metadata", {}))
        meta["retrigger"] = "1"
        s3.copy_object(
            Bucket=BUCKET, Key=k,
            CopySource={"Bucket": BUCKET, "Key": k},
            MetadataDirective="REPLACE",
            Metadata=meta,
            ContentType=head.get("ContentType") or "application/octet-stream",
        )
        print(f"  RE-COPIED (s3)  {i}/{len(keys)}  {k}")


def _run_mode(s3, *, finder, noun, queue_url, queue_label, body_fn, watch_cmd):
    if RUN and not VIA_S3 and not queue_url:
        sys.exit(f"The {queue_label} queue URL is not set in the environment "
                 f"(run from the candidate-analysis folder so .env loads, or use --via-s3).")

    total, items = finder(s3)
    print(f"Scanned {total} objects in s3://{BUCKET}")
    print(f"Pending {noun}(s): {len(items)}")
    for k in items:
        print("  ->", k)

    if not items:
        print(f"Nothing to do — no pending {noun}s.")
        return

    targets = items[:LIMIT] if LIMIT else items

    if not RUN:
        print(f"\nDRY RUN — nothing changed. {len(targets)} {noun}(s) would be re-queued.")
        print("Re-run with --run (add --via-s3 if the box lacks sqs:SendMessage).")
        return

    print(f"\nRe-queuing {len(targets)} {noun}(s) via {'S3 re-copy' if VIA_S3 else 'SQS'}...")
    if VIA_S3:
        requeue_via_s3(s3, targets)
    else:
        _requeue_sqs(queue_url, queue_label, targets, body_fn)
    print(f"\nDone. {len(targets)} {noun}(s) re-queued.")
    print(f"Watch progress:  {watch_cmd}")


def _find_pending_for_sweep(s3):
    """ONE bucket pass → (total, videos_missing_transcript, transcripts_missing_result,
    code_missing_result).

    - Only source files OLDER than SWEEP_GRACE_MINUTES are returned, so a job that
      is still in flight (its file just landed) is never double-queued.
    - Detection is per-folder and per-submission (stem match).
    - Only UNTAGGED source files are considered: once a deliverable is scored, its
      files end in a (Pass)/(Fail) marker (so they no longer end in a video ext /
      _transcripts.txt / code ext) and are skipped automatically.
    """
    folders = defaultdict(dict)   # folder prefix -> {basename: LastModified}
    total = 0
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=BUCKET):
        for obj in page.get("Contents", []):
            key = obj["Key"]
            total += 1
            prefix, _, base = key.rpartition("/")
            folders[(prefix + "/") if prefix else ""][base] = obj.get("LastModified")

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=SWEEP_GRACE_MINUTES)
    videos, transcripts, code = [], [], []

    for prefix, names in folders.items():
        result_bases = [n for n in names if RESULT_MARKER in n]
        for base, last_modified in names.items():
            low = base.lower()
            # inside the grace window → maybe still processing, leave it alone
            if last_modified is not None and last_modified > cutoff:
                continue

            # 1) raw video with NO transcript beside it → needs transcription
            if low.endswith(VIDEO_EXTS):
                stem = base.rsplit(".", 1)[0]
                t_plain = f"{stem}{TRANSCRIPT_SUFFIX}"
                t_legacy = f"{stem}(A){TRANSCRIPT_SUFFIX}"
                if not any(n.startswith(t_plain) or n.startswith(t_legacy) for n in names):
                    videos.append(prefix + base)
                continue

            # 2) untagged transcript with NO result for its submission → needs scoring
            if low.endswith(TRANSCRIPT_SUFFIX.lower()):
                stem = base[: -len(TRANSCRIPT_SUFFIX)]
                if not any(stem in rb for rb in result_bases):
                    transcripts.append(prefix + base)
                continue

            # 3) untagged code file with NO result for its submission → needs grading
            if low.endswith(CODE_EXTS):
                stem = base.rsplit(".", 1)[0]
                if not any(stem in rb for rb in result_bases):
                    code.append(prefix + base)
                continue

    return total, sorted(videos), sorted(transcripts), sorted(code)


def _run_sweep(s3):
    """Self-healing reconciliation: re-queue every deliverable missing its next
    artifact to the correct queue. Safe to run on a schedule — idempotent,
    grace-windowed, and it only touches items still genuinely pending."""
    if RUN:
        missing = [lbl for lbl, url in (
            ("transcript-jobs", TRANSCRIPT_QUEUE_URL),
            ("llm-jobs", LLM_QUEUE_URL),
            ("notebook-jobs", NOTEBOOK_QUEUE_URL)) if not url]
        if missing:
            sys.exit(f"[SWEEP] queue URL(s) not set in the environment: "
                     f"{', '.join(missing)} (run from the candidate-analysis folder "
                     f"so .env loads).")

    total, videos, transcripts, code = _find_pending_for_sweep(s3)
    print(f"[SWEEP] scanned {total} objects in s3://{BUCKET} "
          f"(grace window {SWEEP_GRACE_MINUTES} min)")
    print(f"[SWEEP] pending: {len(videos)} video->transcript, "
          f"{len(transcripts)} transcript->result, {len(code)} code->result")
    for k in videos:
        print("  [video]     ", k)
    for k in transcripts:
        print("  [transcript]", k)
    for k in code:
        print("  [code]      ", k)

    if LIMIT:
        videos, transcripts, code = videos[:LIMIT], transcripts[:LIMIT], code[:LIMIT]

    if not (videos or transcripts or code):
        print("[SWEEP] nothing pending — pipeline is fully caught up.")
        return
    if not RUN:
        print("\n[SWEEP] DRY RUN — nothing changed. Re-run with --run to re-queue.")
        return

    if videos:
        _requeue_sqs(TRANSCRIPT_QUEUE_URL, "transcript (transcript-jobs)", videos,
                     lambda k: {"bucket": BUCKET, "video_key": k})
    if transcripts:
        _requeue_sqs(LLM_QUEUE_URL, "LLM (llm-jobs)", transcripts,
                     lambda k: {"bucket": BUCKET, "key": k, "kind": "transcript"})
    if code:
        _requeue_sqs(NOTEBOOK_QUEUE_URL, "code (notebook-jobs)", code,
                     lambda k: {"bucket": BUCKET, "key": k,
                                "kind": "notebook" if k.lower().endswith(".ipynb") else "python",
                                "file_extension": "." + k.rsplit(".", 1)[-1].lower()})
    print(f"\n[SWEEP] done — re-queued {len(videos)} video(s), "
          f"{len(transcripts)} transcript(s), {len(code)} code file(s).")


def main():
    s3 = boto3.client("s3", region_name=AWS_REGION, config=_S3_CONFIG)
    if SWEEP:
        _run_sweep(s3)
        return
    if LINKS:
        _run_mode(
            s3,
            finder=find_link_submissions,
            noun="link submission",
            queue_url=LLM_QUEUE_URL,
            queue_label="LLM (llm-jobs)",
            body_fn=lambda k: {"bucket": BUCKET, "key": k, "kind": "text"},
            watch_cmd="sudo journalctl -u llm-service -f",
        )
    else:
        _run_mode(
            s3,
            finder=find_missing_transcripts,
            noun="video missing a transcript",
            queue_url=TRANSCRIPT_QUEUE_URL,
            queue_label="transcript (transcript-jobs)",
            body_fn=lambda k: {"bucket": BUCKET, "video_key": k},
            watch_cmd="sudo journalctl -u transcript-service -f",
        )


if __name__ == "__main__":
    main()
