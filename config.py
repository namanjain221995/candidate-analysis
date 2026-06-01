"""Configuration — all values come from environment variables (.env supported).

This is the EC2/S3 transcript service. No Google Drive, no Docker assumptions.
"""

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def _str(name: str, default: str = "") -> str:
    v = os.getenv(name)
    return v.strip() if v is not None and v.strip() != "" else default


def _int(name: str, default: int) -> int:
    return int(_str(name, str(default)))


def _bool(name: str, default: bool = False) -> bool:
    return _str(name, "true" if default else "false").lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class Settings:
    # AWS
    aws_region:            str = _str("AWS_REGION", "us-east-1")
    bucket:                str = _str("BUCKET", "candidate-deliverables")
    transcript_queue_url:  str = _str("TRANSCRIPT_QUEUE_URL")

    # SQS behaviour
    sqs_wait_seconds:        int = _int("SQS_WAIT_SECONDS", 20)        # long-poll
    sqs_visibility_timeout:  int = _int("SQS_VISIBILITY_TIMEOUT", 1800)  # 30 min
    worker_threads:          int = _int("WORKER_THREADS", 4)           # parallel videos

    # OpenAI
    openai_api_key:           str = _str("OPENAI_API_KEY")
    openai_whisper_model:     str = _str("OPENAI_WHISPER_MODEL", "whisper-1")
    openai_transcribe_prompt: str = _str("OPENAI_TRANSCRIBE_PROMPT", "")
    openai_audio_bitrate:     str = _str("OPENAI_AUDIO_BITRATE", "64k")
    language:                 str = _str("LANGUAGE", "en")

    # Transcription tuning
    transcript_suffix:   str  = _str("TRANSCRIPT_SUFFIX", "_transcripts.txt")
    force_retranscribe:  bool = _bool("FORCE_RETRANSCRIBE", False)
    chunk_seconds:       int  = _int("CHUNK_SECONDS", 540)
    overlap_seconds:     int  = _int("OVERLAP_SECONDS", 2)
    ffmpeg_path:         str  = _str("FFMPEG_PATH", "ffmpeg")

    # Retry / pacing
    failure_sleep_seconds: int = _int("FAILURE_SLEEP_SECONDS", 5)

    def validate(self) -> None:
        missing = []
        if not self.transcript_queue_url:
            missing.append("TRANSCRIPT_QUEUE_URL")
        if not self.openai_api_key:
            missing.append("OPENAI_API_KEY")
        if missing:
            raise SystemExit(f"[CONFIG] missing required env vars: {', '.join(missing)}")


SETTINGS = Settings()
