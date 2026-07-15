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

import engines
from config import Settings


OPENAI_AUDIO_URL = "https://api.openai.com/v1/audio/transcriptions"
OPENAI_MAX_FILE_BYTES = 25 * 1024 * 1024

ASSEMBLYAI_BASE_URL = "https://api.assemblyai.com/v2"

_MAX_API_ATTEMPTS = 5
_RETRYABLE_STATUSES = {408, 409, 429, 500, 502, 503, 504}
_NON_RETRYABLE_STATUSES = {400, 401, 403, 404, 413, 415}


# No transcription prompt is sent by default — plain Whisper output, zero
# conditioning. (Whisper's `prompt` is sample-text conditioning, not an
# instruction channel; a previous instruction-style prompt caused repetition
# loops.) If specific vocabulary ever needs seeding, set
# OPENAI_TRANSCRIBE_PROMPT to a SHORT natural sample sentence containing those
# terms — never instructions.


class NonRetryableTranscriptionError(RuntimeError):
    """Raised when the source audio is invalid and retrying cannot fix it."""


_WINDOWS_FORBIDDEN = r'<>:"/\\|?*'


def safe_local_filename(name: str, fallback: str = "file.bin") -> str:
    name = (name or "").strip() or fallback
    name = re.sub(f"[{re.escape(_WINDOWS_FORBIDDEN)}]", "_", name)
    name = re.sub(r"\s+", " ", name).strip().rstrip(" .")
    return name or fallback


def transcript_output_name(video_name: str, transcript_suffix: str, engine_tag: str = "") -> str:
    """Build a transcript filename. The engine tag sits between the stem and the
    suffix; Whisper's tag is empty (original convention), AssemblyAI's is '(A)':
      Whisper:    'video.mp4'  -> 'video_transcripts.txt'
      AssemblyAI: 'video.mp4'  -> 'video(A)_transcripts.txt'."""
    return f"{Path(video_name).stem}{engine_tag}{transcript_suffix}"


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

        # Machine-artifact filters apply in EVERY mode: high no_speech_prob and
        # high compression_ratio flag decoder hallucinations and repetition
        # loops — API-side bugs, not speaker behavior. Verbatim mode protects
        # the speaker's real words (fillers, false starts), never these.
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
    # Default: NO prompt — plain transcription. Env override only, for
    # optional vocabulary seeding with a natural sample sentence.
    custom_prompt = getattr(settings, "openai_transcribe_prompt", None)

    if custom_prompt:
        return str(custom_prompt)

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


def _collapse_repeated_segments(
    segments: List[Tuple[float, str]],
) -> List[Tuple[float, str]]:
    """Collapse runs of consecutive segments with identical normalized text.

    A human re-stating a sentence varies the wording at least slightly; the
    Whisper decoder stuck in a loop emits the EXACT same segment text over and
    over. Keeping only the first occurrence of each consecutive identical run
    removes decoder loops and doubled boundary lines without touching genuine
    filler words or repetitions inside a single segment. Runs in every mode."""
    result: List[Tuple[float, str]] = []
    prev_norm = None
    dropped = 0

    for ts, text in segments:
        norm = " ".join(text.lower().split())
        if prev_norm is not None and norm == prev_norm:
            dropped += 1
            continue
        result.append((ts, text))
        prev_norm = norm

    if dropped:
        print(f"[CLEANUP] collapsed {dropped} repeated segment(s) — decoder loop artifacts")

    return result


def format_timestamp(seconds: float) -> str:
    rounded = int(math.floor(seconds or 0.0))
    minutes, secs = divmod(rounded, 60)
    return f"{minutes}:{secs:02d}"


def prepare_audio(settings: Settings, video_path: Path) -> Optional[Path]:
    """Extract the mono 16 kHz MP3 ONCE so both engines share the exact same
    input (clean paired comparison + saves a second ffmpeg pass).

    Returns the mp3 path, or None when the video has no audio stream at all —
    the caller then writes an empty transcript so the scoring stage gives the
    candidate a deterministic 0/FAIL with re-record guidance.
    """
    if not has_audio_stream(settings, video_path):
        # Candidate uploaded a video with no audio track at all. Instead of
        # failing permanently (which leaves the candidate with NO result and no
        # explanation), the caller returns an empty transcript: the LLM worker
        # detects it and writes a deterministic 0/FAIL result with re-record
        # guidance, delivered through the normal Salesforce flow.
        print(f"[WARN] no audio stream in {video_path.name} — returning empty transcript "
              "so the scoring stage can give the candidate 0/FAIL feedback")
        return None

    mp3_path = video_path.with_name(f"{video_path.stem}__audio.mp3")

    print("[RUN] extracting audio mono 16 kHz MP3")
    extract_audio_mp3(settings, video_path, mp3_path)
    return mp3_path


def transcribe_whisper_from_mp3(
    settings: Settings,
    client: requests.Session,
    mp3_path: Path,
) -> Tuple[str, float]:
    """Whisper (engine 'W') transcription from an already-extracted mp3.

    Returns (transcript_text, audio_seconds). Output is byte-identical to the
    original Whisper path — only audio extraction moved out to prepare_audio().
    """
    verbatim_mode = _get_verbatim_mode(settings)

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

    # Always remove decoder-loop artifacts (consecutive identical segments),
    # in verbatim mode too — these are API bugs, not the speaker's words.
    merged = _collapse_repeated_segments(merged)

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


def transcribe_local_video(
    settings: Settings,
    client: requests.Session,
    video_path: Path,
) -> Tuple[str, float]:
    """Back-compat wrapper: extract audio + Whisper-transcribe a local video.

    Behaviour is unchanged from before the engine split (used by any caller that
    wants the single-engine Whisper path end to end).
    """
    mp3_path = prepare_audio(settings, video_path)
    if mp3_path is None:
        return "", 0.0
    return transcribe_whisper_from_mp3(settings, client, mp3_path)


# ── AssemblyAI engine ('A') ────────────────────────────────────────────────────
#
# AssemblyAI reuses the SAME extracted mp3 (no re-extraction, no chunking — the
# v2 API handles long audio via upload). Verbatim is enabled (disfluencies=True)
# so (A) keeps fillers/false starts like Whisper's VERBATIM_MODE. Output is
# formatted into the exact "M:SS: <text>" lines the prompts cite, byte-compatible
# with Whisper's transcript format.


def _aai_speech_models(settings: Settings) -> List[str]:
    """Parse the comma-separated ASSEMBLYAI_SPEECH_MODELS into a priority list,
    e.g. 'universal-3-pro,universal-2' -> ['universal-3-pro', 'universal-2'].
    AssemblyAI routes to the first supported model and falls back down the list."""
    raw = getattr(settings, "assemblyai_speech_models", "") or ""
    return [m.strip() for m in raw.split(",") if m.strip()]


def _aai_request(method, url, headers, *, json=None, data=None):
    """One AssemblyAI REST call with the same retry policy as the Whisper path."""
    last_err: Optional[Exception] = None
    for attempt in range(_MAX_API_ATTEMPTS):
        # For the file-upload call `data` is an open file handle. A previous
        # attempt consumed it to EOF, so rewind before re-sending or a retry would
        # upload 0 bytes.
        if hasattr(data, "seek"):
            try:
                data.seek(0)
            except Exception:
                pass
        try:
            resp = requests.request(
                method, url, headers=headers, json=json, data=data, timeout=600
            )
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_err = exc
            if attempt < _MAX_API_ATTEMPTS - 1:
                sleep_s = _backoff_seconds(attempt)
                print(f"[ASSEMBLYAI] network error ({exc}); retry in {sleep_s:.0f}s")
                time.sleep(sleep_s)
                continue
            raise

        if resp.status_code == 200:
            return resp

        body = (resp.text or "").strip()[:1500]
        if resp.status_code in _NON_RETRYABLE_STATUSES:
            raise NonRetryableTranscriptionError(
                f"AssemblyAI rejected request (HTTP {resp.status_code}): {body}"
            )
        if resp.status_code in _RETRYABLE_STATUSES and attempt < _MAX_API_ATTEMPTS - 1:
            sleep_s = _backoff_seconds(attempt)
            print(f"[ASSEMBLYAI] HTTP {resp.status_code}; retry in {sleep_s:.0f}s")
            time.sleep(sleep_s)
            continue
        raise RuntimeError(f"AssemblyAI failed HTTP {resp.status_code}: {body}")

    if last_err:
        raise last_err
    raise RuntimeError("AssemblyAI: exhausted retries")


def _aai_poll(settings: Settings, headers: dict, transcript_id: str) -> dict:
    """Poll the transcript until it is 'completed' or 'error'."""
    interval = max(1, settings.assemblyai_poll_seconds)
    for _ in range(max(1, settings.assemblyai_poll_max_attempts)):
        data = _aai_request(
            "GET", f"{ASSEMBLYAI_BASE_URL}/transcript/{transcript_id}", headers
        ).json()
        status = data.get("status")
        if status in ("completed", "error"):
            return data
        time.sleep(interval)
    raise RuntimeError("AssemblyAI: polling timed out before completion")


def _aai_lines_from_words(words: List[dict]) -> List[Tuple[float, str]]:
    """Fallback segmentation: group words into sentence-ish lines using
    sentence-ending punctuation, timestamping each line at its first word."""
    segments: List[Tuple[float, str]] = []
    cur: List[str] = []
    start: Optional[float] = None
    for w in words:
        text = (w.get("text") or "").strip()
        if not text:
            continue
        if start is None:
            start = float(w.get("start") or 0.0) / 1000.0
        cur.append(text)
        if text.endswith((".", "?", "!")):
            segments.append((start, " ".join(cur).strip()))
            cur, start = [], None
    if cur:
        segments.append((start or 0.0, " ".join(cur).strip()))
    return segments


def _aai_segments(settings: Settings, headers: dict, transcript_id: str, transcript: dict
                  ) -> List[Tuple[float, str]]:
    """Sentence-level (start_seconds, text) segments for the M:SS line format.

    Prefer AssemblyAI's /sentences resource (clean sentence boundaries, like
    Whisper segments); fall back to grouping words; finally the whole text."""
    try:
        sentences = _aai_request(
            "GET", f"{ASSEMBLYAI_BASE_URL}/transcript/{transcript_id}/sentences", headers
        ).json().get("sentences") or []
    except Exception as exc:
        print(f"[ASSEMBLYAI] sentences endpoint unavailable ({exc}); using words")
        sentences = []

    segments: List[Tuple[float, str]] = []
    for s in sentences:
        text = (s.get("text") or "").strip()
        if not text:
            continue
        segments.append((float(s.get("start") or 0.0) / 1000.0, text))
    if segments:
        return segments

    words = transcript.get("words") or []
    if words:
        return _aai_lines_from_words(words)

    full = (transcript.get("text") or "").strip()
    return [(0.0, full)] if full else []


def transcribe_assemblyai_from_mp3(settings: Settings, mp3_path: Path) -> Tuple[str, float]:
    """AssemblyAI (engine 'A') transcription from the shared extracted mp3.

    Returns (transcript_text, audio_seconds) in the same format Whisper emits.
    """
    if not settings.assemblyai_api_key:
        raise NonRetryableTranscriptionError("ASSEMBLYAI_API_KEY is not configured.")

    headers = {"authorization": settings.assemblyai_api_key}
    speech_models = _aai_speech_models(settings)
    duration_seconds = audio_duration_seconds(settings, mp3_path)
    audio_mb = mp3_path.stat().st_size / (1024 * 1024)
    print(
        f"[ASSEMBLYAI] uploading {audio_mb:.1f} MB "
        f"({duration_seconds / 60:.1f} min) -> {','.join(speech_models) or 'default'}"
    )

    # 1) upload the SAME mp3 the Whisper path used (raw bytes, streamed)
    with open(mp3_path, "rb") as fh:
        upload_url = _aai_request(
            "POST", f"{ASSEMBLYAI_BASE_URL}/upload", headers, data=fh
        ).json()["upload_url"]

    # 2) create the transcript with PINNED verbatim config
    config = {
        "audio_url": upload_url,
        # Verbatim: keep fillers / false starts / stumbles so (A) is comparable
        # to Whisper's VERBATIM_MODE.
        "disfluencies": bool(settings.assemblyai_disfluencies),
        "punctuate": True,
        "format_text": True,
    }
    # Priority-ordered model list — AssemblyAI uses the first supported model and
    # automatically falls back to the next (e.g. universal-3-pro -> universal-2).
    if speech_models:
        config["speech_models"] = speech_models
    if settings.language:
        config["language_code"] = settings.language

    transcript_id = _aai_request(
        "POST", f"{ASSEMBLYAI_BASE_URL}/transcript", headers, json=config
    ).json()["id"]

    # 3) poll to completion
    transcript = _aai_poll(settings, headers, transcript_id)
    if transcript.get("status") == "error":
        raise NonRetryableTranscriptionError(
            f"AssemblyAI transcription failed: {transcript.get('error')}"
        )

    # 4) format into the exact M:SS line format the prompts cite
    segments = _aai_segments(settings, headers, transcript_id, transcript)
    lines = [f"{format_timestamp(ts)}: {text}" for ts, text in segments if text]
    content = ("\n".join(lines).strip() + "\n") if lines else ""

    print(f"[ASSEMBLYAI] transcript: {len(lines)} segments")
    if not lines:
        print("[WARN] AssemblyAI transcript empty — audio may be silent or too noisy")

    return content, float(duration_seconds)


def transcribe_engine(
    settings: Settings,
    engine: str,
    mp3_path: Path,
    whisper_client: Optional[requests.Session] = None,
) -> Tuple[str, float]:
    """Dispatch a single engine over the SHARED extracted mp3.

    AssemblyAI is the production engine and needs NO client (each REST call is
    stateless). Whisper is retired: it only runs if a caller explicitly passes a
    whisper_client (the dormant rollback path); without one it raises rather than
    silently calling OpenAI.
    """
    if engine == engines.ASSEMBLYAI:
        return transcribe_assemblyai_from_mp3(settings, mp3_path)
    if engine == engines.WHISPER:
        if whisper_client is None:
            raise NonRetryableTranscriptionError(
                "Whisper is retired: no OpenAI client is configured for transcription."
            )
        return transcribe_whisper_from_mp3(settings, whisper_client, mp3_path)
    raise NonRetryableTranscriptionError(f"unknown transcription engine: {engine!r}")