"""LLM processor — builds the model input and calls OpenAI.

Handles three content kinds in one evaluation:
  - transcript text (video deliverables)
  - reference PDF text (e.g. 31-Questions) and resume text
  - images (diagrams) sent as vision input (base64 data URLs)

Returns a parsed dict: {score, result, reasoning, positives, negatives}.
"""

import base64
import hashlib
import json
import re
import time
from pathlib import Path
from typing import List, Optional

import requests

from llm_config import LLMSettings

OPENAI_CHAT_URL = "https://api.openai.com/v1/chat/completions"
_MAX_ATTEMPTS = 5
_RETRYABLE = {408, 409, 429, 500, 502, 503, 504}


# ── Deterministic-reproducibility cache ──────────────────────────────────────
# Same model input (prompt + code/transcript + images + model + seed) → same
# verdict, GUARANTEED, by storing the parsed result under a content hash in S3 and
# returning it verbatim on a re-score. A fixed `seed` (added in evaluate) nudges
# the model toward the same output even on a cache MISS, but the cache is what
# actually guarantees "same input → same reasoning + score". Cache objects are
# `<prefix><sha256>.json`; the `.json` suffix (not `_result.json`, not a media
# extension) matches no trigger-Lambda route, so they never re-enter the pipeline.
_S3_CLIENT = None


def _s3_for(settings):
    global _S3_CLIENT
    if _S3_CLIENT is None:
        import llm_s3
        _S3_CLIENT = llm_s3.build_s3(settings)
    return _S3_CLIENT


def _cache_key(payload: dict) -> str:
    canon = json.dumps(payload, sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canon.encode("utf-8")).hexdigest()


def _cache_object_key(settings, key: str) -> str:
    prefix = getattr(settings, "llm_cache_prefix", "_llm_cache/")
    return f"{prefix}{key}.json"


def _cache_get(settings, key: str):
    try:
        s3 = _s3_for(settings)
        obj = s3.get_object(Bucket=settings.bucket, Key=_cache_object_key(settings, key))
        return json.loads(obj["Body"].read().decode("utf-8"))
    except Exception:
        return None  # miss (including NoSuchKey) — score fresh


def _cache_put(settings, key: str, result: dict) -> None:
    try:
        s3 = _s3_for(settings)
        s3.put_object(
            Bucket=settings.bucket, Key=_cache_object_key(settings, key),
            Body=json.dumps(result, ensure_ascii=False).encode("utf-8"),
            ContentType="application/json",
        )
    except Exception as exc:
        print(f"[LLM-CACHE] store failed (non-fatal): {exc}")


def _post_with_retries(client: requests.Session, payload: dict) -> dict:
    """POST to OpenAI with bounded backoff. If a model rejects `seed` (some
    reasoning models do), drop it and retry immediately rather than fail the job —
    the content-hash cache still gives exact reproducibility on resubmission."""
    last_err = None
    for attempt in range(_MAX_ATTEMPTS):
        try:
            resp = client.post(OPENAI_CHAT_URL, json=payload, timeout=300)
        except (requests.Timeout, requests.ConnectionError) as exc:
            last_err = exc
            if attempt < _MAX_ATTEMPTS - 1:
                time.sleep(min(60, 2 ** (attempt + 1)))
                continue
            raise

        if resp.status_code == 200:
            return _clean_json(resp.json()["choices"][0]["message"]["content"])

        body = (resp.text or "")[:1000]
        # A model that rejects `seed` (some reasoning models do) returns 400. Rather
        # than risk a hard fail → DLQ, drop `seed` on ANY 400 while it is still set
        # and retry once; if the 400 was for another reason it simply recurs (seed
        # now gone) and then raises below. Costs at most one extra attempt.
        if resp.status_code == 400 and "seed" in payload:
            payload.pop("seed", None)
            print("[LLM] 400 with seed set — retrying once without seed")
            continue
        if resp.status_code in _RETRYABLE and attempt < _MAX_ATTEMPTS - 1:
            time.sleep(min(60, 2 ** (attempt + 1)))
            continue
        raise RuntimeError(f"OpenAI chat failed HTTP {resp.status_code}: {body}")

    if last_err:
        raise last_err
    raise RuntimeError("OpenAI chat: exhausted retries")


def load_prompt(settings: LLMSettings, prompt_file: str) -> str:
    return (Path(settings.prompts_dir) / prompt_file).read_text(encoding="utf-8")


def extract_pdf_text(pdf_path: Path) -> str:
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(str(pdf_path)) as pdf:
            for page in pdf.pages:
                parts.append(page.extract_text() or "")
        return "\n".join(parts).strip()
    except Exception:
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(pdf_path))
            return "\n".join((p.extract_text() or "") for p in reader.pages).strip()
        except Exception as exc:
            raise RuntimeError(f"Could not extract PDF text from {pdf_path}: {exc}")


def image_to_data_url(image_path: Path) -> str:
    ext = image_path.suffix.lower().lstrip(".")
    mime = {"jpg": "jpeg", "jpeg": "jpeg", "png": "png", "webp": "webp", "gif": "gif"}.get(ext, "png")
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return f"data:image/{mime};base64,{b64}"


def _clean_json(text: str) -> dict:
    t = text.strip()
    t = re.sub(r"^```(?:json)?", "", t).strip()
    t = re.sub(r"```$", "", t).strip()
    if not t.startswith("{"):
        a, b = t.find("{"), t.rfind("}")
        if a != -1 and b != -1:
            t = t[a:b + 1]
    data = json.loads(t)
    score = data.get("score")
    try:
        score = int(round(float(score)))
    except (TypeError, ValueError):
        score = None
    return {
        "score": score,
        "result": (data.get("result") or "").upper(),
        "reasoning": data.get("reasoning", ""),
        "positives": data.get("positives", []) or [],
        "negatives": data.get("negatives", []) or [],
    }


def evaluate(
    settings: LLMSettings,
    client: requests.Session,
    *,
    system_prompt: str,
    deliverable_name: Optional[str] = None,
    transcript_text: Optional[str] = None,
    resume_text: Optional[str] = None,
    reference_pdf_text: Optional[str] = None,
    extra_text: Optional[str] = None,          # e.g. JD text
    image_data_urls: Optional[List[str]] = None,
) -> dict:
    """Run one evaluation. Any combination of inputs may be provided."""
    blocks = []  # text segments for the user message

    if deliverable_name:
        blocks.append("DELIVERABLE NAME: " + deliverable_name)
    if reference_pdf_text:
        blocks.append("REFERENCE BASELINE (internal use only):\n" + reference_pdf_text)
    if resume_text:
        blocks.append("CANDIDATE RESUME:\n" + resume_text)
    if extra_text:
        blocks.append("SUPPORTING TEXT (e.g. job description):\n" + extra_text)
    if transcript_text:
        blocks.append("TRANSCRIPT:\n" + transcript_text)
    if image_data_urls and not transcript_text and not extra_text:
        blocks.append("Evaluate the attached image(s) per the instructions.")

    user_text = "\n\n".join(blocks) if blocks else "Evaluate the attached input."

    # build the user message: text + any images
    content = [{"type": "text", "text": user_text}]
    for url in (image_data_urls or []):
        content.append({"type": "image_url", "image_url": {"url": url}})

    payload = {
        "model": settings.openai_model,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
    }

    # GPT-5.x / o-series reasoning models reject `temperature` (HTTP 400:
    # only the default 1 is supported) and take `reasoning_effort` instead.
    # The "-chat" variants (e.g. gpt-5.2-chat-latest) are non-reasoning and
    # keep accepting temperature, as do gpt-4o and older models.
    model_l = settings.openai_model.lower()
    is_reasoning = model_l.startswith(("gpt-5", "o1", "o3", "o4")) and "chat" not in model_l
    if is_reasoning:
        effort = (settings.openai_reasoning_effort or "").strip().lower()
        if effort:
            payload["reasoning_effort"] = effort
    else:
        payload["temperature"] = 0

    # Reproducibility: pin a seed (best-effort determinism on the API side; dropped
    # automatically by _post_with_retries if the model rejects it) so identical
    # input tends to the same output even on a cache miss.
    seed = getattr(settings, "openai_seed", None)
    if seed is not None:
        payload["seed"] = seed

    # Content-hash cache: identical input → identical verdict, GUARANTEED. The key
    # covers the full payload (prompt + code/transcript + images + model + params +
    # seed), so any change to the submission or the rubric misses and re-scores.
    cache_key = _cache_key(payload) if getattr(settings, "llm_cache_enabled", False) else None
    if cache_key:
        hit = _cache_get(settings, cache_key)
        if hit is not None:
            print(f"[LLM-CACHE] hit {cache_key[:12]} → reusing identical result")
            return hit

    result = _post_with_retries(client, payload)

    if cache_key:
        _cache_put(settings, cache_key, result)
    return result
