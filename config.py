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

    # OpenAI (Whisper engine = "W")
    openai_api_key:           str = _str("OPENAI_API_KEY")
    openai_whisper_model:     str = _str("OPENAI_WHISPER_MODEL", "whisper-1")
    openai_transcribe_prompt: str = _str("OPENAI_TRANSCRIBE_PROMPT", "")
    openai_audio_bitrate:     str = _str("OPENAI_AUDIO_BITRATE", "64k")
    language:                 str = _str("LANGUAGE", "en")

    # AssemblyAI (engine = "A") — parallel A/B engine, S3-only (never Salesforce).
    # The model and verbatim settings are PINNED here so a provider-side default
    # change can never silently skew the Whisper-vs-AssemblyAI comparison.
    assemblyai_api_key:       str  = _str("ASSEMBLYAI_API_KEY")
    # Speech models in PRIORITY order (comma-separated). AssemblyAI routes to the
    # first supported model and AUTOMATICALLY FALLS BACK to the next, so
    # "universal-3-pro,universal-2" uses Universal-3 Pro (newest/best, English-
    # capable) and falls back to Universal-2 otherwise — this is AssemblyAI's own
    # default. Sent as the `speech_models` array (the singular `speech_model` is
    # deprecated). To try a newer model later (e.g. universal-3-5-pro) just change
    # this env var — no code change. Leave blank to let AssemblyAI pick its default.
    assemblyai_speech_models: str  = _str("ASSEMBLYAI_SPEECH_MODELS", "universal-3-pro,universal-2")
    # Verbatim: keep fillers / false starts / disfluencies so (A) matches
    # Whisper's VERBATIM_MODE and the comparison is fair.
    assemblyai_disfluencies:  bool = _bool("ASSEMBLYAI_DISFLUENCIES", True)
    assemblyai_poll_seconds:      int = _int("ASSEMBLYAI_POLL_SECONDS", 3)
    assemblyai_poll_max_attempts: int = _int("ASSEMBLYAI_POLL_MAX_ATTEMPTS", 600)

    # Which engines transcribe every video (comma-separated; default both).
    # Whisper always runs first regardless of order. Set to "W" to fall back to
    # the original Whisper-only behaviour. See engines.configured_engines().
    transcription_engines: str = _str("TRANSCRIPTION_ENGINES", "W,A")

    # Transcription tuning
    transcript_suffix:   str  = _str("TRANSCRIPT_SUFFIX", "_transcripts.txt")
    force_retranscribe:  bool = _bool("FORCE_RETRANSCRIBE", False)
    # 180s (3-min) chunks: shorter chunks limit the damage if Whisper ever
    # loops mid-chunk, improve accuracy, and parallelize better. (Was 540.)
    chunk_seconds:       int  = _int("CHUNK_SECONDS", 180)
    overlap_seconds:     int  = _int("OVERLAP_SECONDS", 2)
    ffmpeg_path:         str  = _str("FFMPEG_PATH", "ffmpeg")

    # Retry / pacing
    failure_sleep_seconds: int = _int("FAILURE_SLEEP_SECONDS", 5)
    verbatim_mode: bool = _bool("VERBATIM_MODE", default=True)

    def validate(self) -> None:
        # AssemblyAI is the sole transcription engine, so the transcript service
        # requires ASSEMBLYAI_API_KEY. It NO LONGER requires OPENAI_API_KEY —
        # Whisper is retired and OpenAI is never called for transcription — so
        # stopping the OpenAI/Whisper billing (or dropping that key from this
        # service's env) can't crash the worker. (The LLM scoring service keeps
        # its own OPENAI_API_KEY requirement in llm_config.py.)
        missing = []
        if not self.transcript_queue_url:
            missing.append("TRANSCRIPT_QUEUE_URL")
        if not self.assemblyai_api_key:
            missing.append("ASSEMBLYAI_API_KEY")
        if missing:
            raise SystemExit(f"[CONFIG] missing required env vars: {', '.join(missing)}")


SETTINGS = Settings()