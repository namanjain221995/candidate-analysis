"""Transcript service — multi-threaded SQS worker for EC2.

Run on EC2:  python main.py

Flow:
  S3 upload → trigger Lambda → SQS (transcript-jobs)
  This process runs N threads; each thread long-polls SQS, and for every
  message { "bucket", "video_key" } it:
    1. skips if a transcript already exists in S3
    2. downloads the video to a temp file
    3. transcribes it (Whisper) via the transcriber module
    4. uploads <video>_transcripts.txt beside the video in S3
    5. deletes the SQS message on success

  On a transient error the message is NOT deleted, so SQS redelivers it after
  the visibility timeout; after maxReceiveCount it lands in the DLQ.
  On a permanent error (bad/silent video) the message is deleted so it does
  not retry forever.
"""

import json
import signal
import tempfile
import threading
import time
from pathlib import Path

import boto3

import engines
from config import SETTINGS
import s3_store
from transcriber import (
    NonRetryableTranscriptionError,
    create_openai_client,
    ensure_ffmpeg,
    prepare_audio,
    safe_local_filename,
    transcribe_engine,
    transcript_output_name,
)


_stop = threading.Event()


def _pending_engines(s3, bucket: str, video_key: str, video_name: str):
    """The engines that still need a transcript for this video.

    Per-engine skip: an engine whose transcript already exists is dropped, so
    re-runs only redo the missing engine(s). AssemblyAI is skipped entirely when
    its key isn't configured, so production (Whisper) is never blocked by it.

    Returns [(engine, transcript_key), ...].
    """
    pending = []
    for engine in engines.configured_engines():
        if engine == engines.ASSEMBLYAI and not SETTINGS.assemblyai_api_key:
            print(f"[SKIP] ({engine}) ASSEMBLYAI_API_KEY not configured — Whisper-only")
            continue
        transcript_name = transcript_output_name(
            video_name, SETTINGS.transcript_suffix, engines.engine_tag(engine)
        )
        transcript_key = s3_store.sibling_key(video_key, transcript_name)
        if not SETTINGS.force_retranscribe and s3_store.object_exists(s3, bucket, transcript_key):
            print(f"[SKIP] ({engine}) transcript exists: {transcript_key}")
            continue
        pending.append((engine, transcript_key))
    return pending


def _handle_message(sqs, s3, client, body: dict) -> None:
    bucket    = body["bucket"]
    video_key = body["video_key"]
    video_name = video_key.rsplit("/", 1)[-1]

    # Both engines transcribe the SAME source audio. Whisper is always first, so
    # the production transcript is written (and durable) before AssemblyAI runs.
    pending = _pending_engines(s3, bucket, video_key, video_name)
    if not pending:
        print(f"[SKIP] all transcripts exist: {video_key}")
        return

    if s3_store.object_size(s3, bucket, video_key) == 0:
        raise NonRetryableTranscriptionError(f"S3 video is empty: {video_key}")

    with tempfile.TemporaryDirectory() as temp_dir:
        local_video = Path(temp_dir) / safe_local_filename(video_name, "video.mp4")
        print(f"[DL] s3://{bucket}/{video_key}")
        s3_store.download_file(s3, bucket, video_key, local_video)
        if local_video.stat().st_size == 0:
            raise RuntimeError(f"Downloaded video empty; will retry: {video_key}")

        # Extract audio ONCE; feed the same mp3 to every engine. None => the
        # video has no audio track: each engine gets an empty transcript so the
        # scoring stage gives a deterministic 0/FAIL.
        mp3_path = prepare_audio(SETTINGS, local_video)

        for engine, transcript_key in pending:
            started = time.monotonic()
            if mp3_path is None:
                content, audio_seconds = "", 0.0
            else:
                content, audio_seconds = transcribe_engine(SETTINGS, client, engine, mp3_path)

            print(f"[UP] ({engine}) s3://{bucket}/{transcript_key}")
            s3_store.upload_text(s3, bucket, transcript_key, content)
            # Per-engine cost/latency signal for the A/B comparison.
            print(f"[DONE] ({engine}) {video_key} "
                  f"({audio_seconds / 60:.1f} min audio, {time.monotonic() - started:.1f}s)")


def _worker_loop(worker_no: int) -> None:
    sqs = boto3.client("sqs", region_name=SETTINGS.aws_region)
    s3  = s3_store.build_s3_client(SETTINGS)
    client = create_openai_client(SETTINGS)
    print(f"[WORKER {worker_no}] started")

    while not _stop.is_set():
        try:
            resp = sqs.receive_message(
                QueueUrl=SETTINGS.transcript_queue_url,
                MaxNumberOfMessages=1,
                WaitTimeSeconds=SETTINGS.sqs_wait_seconds,
                VisibilityTimeout=SETTINGS.sqs_visibility_timeout,
            )
        except Exception as exc:
            print(f"[WORKER {worker_no}][SQS-ERROR] {exc}")
            time.sleep(SETTINGS.failure_sleep_seconds)
            continue

        messages = resp.get("Messages", [])
        if not messages:
            continue

        msg = messages[0]
        receipt = msg["ReceiptHandle"]
        try:
            body = json.loads(msg["Body"])
            _handle_message(sqs, s3, client, body)
            sqs.delete_message(QueueUrl=SETTINGS.transcript_queue_url, ReceiptHandle=receipt)

        except NonRetryableTranscriptionError as exc:
            print(f"[WORKER {worker_no}][PERMANENT] {exc}")
            sqs.delete_message(QueueUrl=SETTINGS.transcript_queue_url, ReceiptHandle=receipt)

        except Exception as exc:
            print(f"[WORKER {worker_no}][ERROR] {exc}  (will retry via SQS)")
            try:
                client.close()
            except Exception:
                pass
            client = create_openai_client(SETTINGS)
            time.sleep(SETTINGS.failure_sleep_seconds)

    print(f"[WORKER {worker_no}] stopped")


def _install_signal_handlers() -> None:
    def _graceful(signum, frame):
        print(f"[MAIN] signal {signum} received, shutting down…")
        _stop.set()
    signal.signal(signal.SIGINT, _graceful)
    signal.signal(signal.SIGTERM, _graceful)


def main() -> None:
    SETTINGS.validate()
    ensure_ffmpeg(SETTINGS)
    _install_signal_handlers()

    active = engines.configured_engines()
    if engines.ASSEMBLYAI in active and not SETTINGS.assemblyai_api_key:
        active = [e for e in active if e != engines.ASSEMBLYAI]
        print("[MAIN] ASSEMBLYAI_API_KEY not set — running Whisper-only")

    print(f"[MAIN] bucket={SETTINGS.bucket}")
    print(f"[MAIN] queue={SETTINGS.transcript_queue_url}")
    print(f"[MAIN] threads={SETTINGS.worker_threads}  model={SETTINGS.openai_whisper_model}")
    print(f"[MAIN] engines={','.join(active)}"
          + (f"  assemblyai_models={SETTINGS.assemblyai_speech_models}"
             if engines.ASSEMBLYAI in active else ""))

    threads = []
    for i in range(SETTINGS.worker_threads):
        t = threading.Thread(target=_worker_loop, args=(i + 1,), daemon=True)
        t.start()
        threads.append(t)

    while not _stop.is_set():
        time.sleep(1)
    for t in threads:
        t.join(timeout=SETTINGS.sqs_visibility_timeout)


if __name__ == "__main__":
    main()
