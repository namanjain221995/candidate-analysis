"""Transcription core — ffmpeg audio extraction, chunking, OpenAI Whisper calls,
timestamp merge, and optional cleanup.

VERBATIM MODE:
- Preserves filler words like um, uh, hmm.
- Preserves repetitions and false starts.
- Does not remove short spoken segments.
- Uses a prompt asking the model not to correct grammar or technical terms.
- Best for interview evaluation where candidate mistakes must remain visible.
"""

import json
import math
import random
import re
import subprocess
import time
from pathlib import Path
from typing import List, Optional, Tuple

import requests

from config import Settings


OPENAI_AUDIO_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_MAX_FILE_BYTES = 25 * 1024 * 1024

_MAX_API_ATTEMPTS = 5
_RETRYABLE_STATUSES = {408, 409, 429, 500, 502, 503, 504}
_NON_RETRYABLE_STATUSES = {400, 401, 403, 404, 413, 415}


VERBATIM_TRANSCRIBE_PROMPT = (
    "Transcribe exactly what is spoken. "
    "Do not correct grammar. "
    "Do not fix technical terms. "
    "Do not rewrite sentences. "
    "Preserve filler words such as um, uh, hmm, ah, eh. "
    "Preserve repetitions, false starts, pauses, and mispronunciations. "
    "If the speaker says a wrong word, keep the wrong word. "
    "Output verbatim speech only."
)


class NonRetryableTranscriptionError(RuntimeError):
    """Raised when the source audio is invalid and retrying cannot fix it."""


_WINDOWS_FORBIDDEN = r'<>:"/\\|?*'


def safe_local_filename(name: str, fallback: str = "file.bin") -> str:
    name = (name or "").strip() or fallback
    name = re.sub(f"[{re.escape(_WINDOWS_FORBIDDEN)}]", "_", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(" .")
    return name or fallback


def transcript_output_name(video_name: str, transcript_suffix: str) -> str:
    return f"{Path(video_name).stem}{transcript_suffix}"


def _ffprobe_path(settings: Settings) -> str:
    ffmpeg = Path(settings.ffmpeg_path)
    if ffmpeg.name.lower().startswith("ffmpeg"):
        return str(ffmpeg.with_name("ffprobe"))
    return "ffprobe"


def ensure_ffmpeg(settings: Settings) -> None:
    subprocess.run(
        [settings.ffmpeg_path, "-version"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        check=True,
    )


def extract_audio_mp3(settings: Settings, video_path: Path, mp3_path: Path) -> None:
    """Extract mono 16 kHz MP3 from any video container."""
    base_args = [
        settings.ffmpeg_path,
        "-y",
        "-loglevel",
        "error",
        "-i",
        str(video_path),
        "-vn",
        "-ac",
        "1",
        "-ar",
        "16000",
        "-codec:a",
        "libmp3lame",
        "-b:a",
        settings.openai_audio_bitrate,
    ]

    proc = subprocess.run(
        base_args + ["-af", "highpass=f=80,afftdn=nf=-25", str(mp3_path)],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if proc.returncode != 0:
        proc2 = subprocess.run(
            base_args + [str(mp3_path)],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        if proc2.returncode != 0:
            raise RuntimeError(
                f"ffmpeg audio extraction failed: {proc2.stderr.strip()}"
            )


def audio_duration_seconds(settings: Settings, audio_path: Path) -> float:
    proc = subprocess.run(
        [
            _ffprobe_path(settings),
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            str(audio_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if proc.returncode != 0:
        raise RuntimeError(f"ffprobe duration failed: {proc.stderr.strip()}")

    try:
        data = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        return 0.0

    return float(data.get("format", {}).get("duration") or 0.0)


def has_audio_stream(settings: Settings, video_path: Path) -> bool:
    proc = subprocess.run(
        [
            _ffprobe_path(settings),
            "-v",
            "error",
            "-show_entries",
            "stream=codec_type",
            "-of",
            "json",
            str(video_path),
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )

    if proc.returncode != 0:
        raise NonRetryableTranscriptionError(
            f"Video not readable by ffprobe: {(proc.stderr or '').strip()[:500]}"
        )

    try:
        probe = json.loads(proc.stdout or "{}")
    except json.JSONDecodeError:
        probe = {}

    return any(
        s.get("codec_type") == "audio"
        for s in probe.get("streams", [])
        if isinstance(s, dict)
    )


def split_audio(settings: Settings, audio_path: Path) -> List[Tuple[Path, float, float]]:
    """Re-encode MP3 into chunks with overlap.

    Returns:
        [(chunk_path, actual_start, logical_start), ...]
    """
    total_seconds = audio_duration_seconds(settings, audio_path)
    chunks: List[Tuple[Path, float, float]] = []

    start = 0.0
    idx = 0

    while start < total_seconds:
        actual_start = max(
            0.0,
            start - (settings.overlap_seconds if idx > 0 else 0.0),
        )
        duration = settings.chunk_seconds + (
            settings.overlap_seconds if idx > 0 else 0.0
        )

        out_path = audio_path.with_name(f"{audio_path.stem}_part{idx}.mp3")

        proc = subprocess.run(
            [
                settings.ffmpeg_path,
                "-y",
                "-loglevel",
                "error",
                "-i",
                str(audio_path),
                "-ss",
                str(actual_start),
                "-t",
                str(duration),
                "-ac",
                "1",
                "-ar",
                "16000",
                "-codec:a",
                "libmp3lame",
                "-b:a",
                settings.openai_audio_bitrate,
                str(out_path),
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )

        if proc.returncode != 0:
            raise RuntimeError(f"ffmpeg split failed: {proc.stderr.strip()}")

        chunks.append((out_path, actual_start, start))

        start += settings.chunk_seconds
        idx += 1

    return chunks


def create_openai_client(settings: Settings) -> requests.Session:
    if not settings.openai_api_key:
        raise RuntimeError("OPENAI_API_KEY is not configured.")

    session = requests.Session()
    session.headers.update({"Authorization": f"Bearer {settings.openai_api_key}"})
    return session


def _backoff_seconds(attempt: int, base: float = 2.0, cap: float = 60.0) -> float:
    return min(cap, base * (2 ** attempt)) + random.uniform(0, 1.5)


def _parse_verbose_json(
    payload: dict,
    *,
    verbatim_mode: bool = True,
) -> List[Tuple[float, str]]:
    items: List[Tuple[float, str]] = []

    for seg in payload.get("segments") or []:
        if not isinstance(seg, dict):
            continue

        text = (seg.get("text") or "").strip()

        if not text:
            continue

        # In verbatim mode, do NOT remove possible no-speech / repeated / odd words.
        # For clean mode, keep previous filters.
        if not verbatim_mode:
            if seg.get("no_speech_prob") is not None:
                if float(seg["no_speech_prob"]) > 0.8:
                    continue

            if seg.get("compression_ratio") is not None:
                if float(seg["compression_ratio"]) > 2.4:
                    continue

        items.append((float(seg.get("start") or 0.0), text))

    if not items:
        full = (payload.get("text") or "").strip()
        if full:
            items.append((0.0, full))

    return items


def _get_verbatim_mode(settings: Settings) -> bool:
    return bool(getattr(settings, "verbatim_mode", True))


def _get_transcribe_prompt(settings: Settings) -> str:
    custom_prompt = getattr(settings, "openai_transcribe_prompt", None)

    if custom_prompt:
        return str(custom_prompt)

    if _get_verbatim_mode(settings):
        return VERBATIM_TRANSCRIBE_PROMPT

    return ""


def transcribe_chunk(
    settings: Settings,
    client: requests.Session,
    audio_path: Path,
) -> List[Tuple[float, str]]:
    chunk_size = audio_path.stat().st_size

    if chunk_size > OPENAI_MAX_FILE_BYTES:
        raise NonRetryableTranscriptionError(
            f"Audio chunk {audio_path.name} is {chunk_size} bytes, exceeding the "
            f"OpenAI 25 MB limit. Reduce CHUNK_SECONDS or OPENAI_AUDIO_BITRATE."
        )

    verbatim_mode = _get_verbatim_mode(settings)

    data = {
        "model": settings.openai_whisper_model,
        "response_format": "verbose_json",
        "temperature": "0",
    }

    if getattr(settings, "language", None):
        data["language"] = settings.language

    prompt = _get_transcribe_prompt(settings)
    if prompt:
        data["prompt"] = prompt

    last_err: Optional[Exception] = None

    for attempt in range(_MAX_API_ATTEMPTS):
        try:
            with open(audio_path, "rb") as fh:
                files = {"file": (audio_path.name, fh, "audio/mpeg")}
                response = client.post(
                    OPENAI_AUDIO_URL,
                    data=data,
                    files=files,
                    timeout=600,
                )

        except (requests.Timeout, requests.ConnectionError) as exc:
            last_err = exc

            if attempt < _MAX_API_ATTEMPTS - 1:
                sleep_s = _backoff_seconds(attempt)
                print(f"[WHISPER] network error ({exc}); retry in {sleep_s:.0f}s")
                time.sleep(sleep_s)
                continue

            raise

        if response.status_code == 200:
            try:
                return _parse_verbose_json(
                    response.json(),
                    verbatim_mode=verbatim_mode,
                )
            except ValueError as exc:
                raise RuntimeError(
                    f"OpenAI Whisper returned non-JSON: {exc}"
                ) from exc

        body = (response.text or "").strip()[:1500]

        if response.status_code in _NON_RETRYABLE_STATUSES:
            raise NonRetryableTranscriptionError(
                f"OpenAI Whisper rejected request "
                f"(HTTP {response.status_code}): {body}"
            )

        if (
            response.status_code in _RETRYABLE_STATUSES
            and attempt < _MAX_API_ATTEMPTS - 1
        ):
            retry_after = response.headers.get("retry-after")

            try:
                ra_s = float(retry_after) if retry_after else 0.0
            except ValueError:
                ra_s = 0.0

            sleep_s = max(_backoff_seconds(attempt), ra_s)
            print(f"[WHISPER] HTTP {response.status_code}; retry in {sleep_s:.0f}s")
            time.sleep(sleep_s)
            continue

        raise RuntimeError(
            f"OpenAI Whisper failed HTTP {response.status_code}: {body}"
        )

    if last_err:
        raise last_err

    raise RuntimeError("OpenAI Whisper: exhausted retries")


_HALLUCINATION_PHRASES = {
    "thank you",
    "thanks for watching",
    "thank you for watching",
    "please subscribe",
    "like and subscribe",
    "see you next time",
    "bye",
    "bye bye",
    ".",
}

_HALLUCINATION_RE = re.compile(r"^[\s.,!?-]*$")


def _is_hallucination(text: str, *, verbatim_mode: bool = True) -> bool:
    if verbatim_mode:
        return False

    t = text.strip().lower()

    if t in _HALLUCINATION_PHRASES:
        return True

    if _HALLUCINATION_RE.match(text):
        return True

    if len(t) <= 2:
        return True

    return False


def _deduplicate_segments(
    segments: List[Tuple[float, str]],
    *,
    window: int = 6,
    verbatim_mode: bool = True,
) -> List[Tuple[float, str]]:
    if verbatim_mode:
        return segments

    if not segments:
        return segments

    result: List[Tuple[float, str]] = []
    recent: List[str] = []

    for ts, text in segments:
        if _is_hallucination(text, verbatim_mode=verbatim_mode):
            continue

        t = text.strip().lower()

        if len(t) <= 40 and recent[-window:].count(t) >= 2:
            continue

        result.append((ts, text))
        recent.append(t)

    return result


def format_timestamp(seconds: float) -> str:
    rounded = int(math.floor(seconds or 0.0))
    minutes, secs = divmod(rounded, 60)
    return f"{minutes}:{secs:02d}"


def transcribe_local_video(
    settings: Settings,
    client: requests.Session,
    video_path: Path,
) -> Tuple[str, float]:
    """Transcribe local video file.

    Returns:
        (transcript_text, audio_seconds)
    """
    verbatim_mode = _get_verbatim_mode(settings)

    if not has_audio_stream(settings, video_path):
        raise NonRetryableTranscriptionError(
            f"Video has no audio stream to transcribe: {video_path.name}"
        )

    mp3_path = video_path.with_name(f"{video_path.stem}__audio.mp3")

    print("[RUN] extracting audio mono 16 kHz MP3")
    extract_audio_mp3(settings, video_path, mp3_path)

    duration_seconds = audio_duration_seconds(settings, mp3_path)
    audio_mb = mp3_path.stat().st_size / (1024 * 1024)

    print(
        f"[INFO] audio duration: {duration_seconds / 60:.1f} min "
        f"({audio_mb:.1f} MB)"
    )

    print(f"[INFO] verbatim mode: {verbatim_mode}")

    if duration_seconds <= settings.chunk_seconds + 5:
        parts = [(mp3_path, 0.0, 0.0)]
    else:
        parts = split_audio(settings, mp3_path)

    merged: List[Tuple[float, str]] = []

    for index, (chunk_path, actual_start, logical_start) in enumerate(parts):
        chunk_mb = chunk_path.stat().st_size / (1024 * 1024)

        print(
            f"[API] chunk {index + 1}/{len(parts)} "
            f"({chunk_mb:.1f} MB, offset={format_timestamp(actual_start)}) "
            f"-> {settings.openai_whisper_model}"
        )

        for relative_start, text in transcribe_chunk(settings, client, chunk_path):
            absolute_start = relative_start + actual_start

            # Keep overlap trimming only. This prevents duplicate chunk-overlap text.
            # It does not remove candidate filler words or repetitions inside valid audio.
            if index > 0 and absolute_start < logical_start:
                continue

            merged.append((absolute_start, text))

        if chunk_path != mp3_path:
            chunk_path.unlink(missing_ok=True)

    merged.sort(key=lambda item: item[0])

    merged = _deduplicate_segments(
        merged,
        verbatim_mode=verbatim_mode,
    )

    lines = [f"{format_timestamp(ts)}: {text}" for ts, text in merged]

    content = ("\n".join(lines).strip() + "\n") if lines else ""

    print(f"[INFO] transcript: {len(lines)} segments")
    if not lines:
        print("[WARN] transcript empty — audio may be silent or too noisy")

    return content, float(duration_seconds)