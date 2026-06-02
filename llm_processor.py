"""LLM processor — builds the model input and calls OpenAI.

Handles three content kinds in one evaluation:
  - transcript text (video deliverables)
  - reference PDF text (e.g. 31-Questions) and resume text
  - images (diagrams) sent as vision input (base64 data URLs)

Returns a parsed dict: {score, result, reasoning, positives, negatives}.
"""

import base64
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
    transcript_text: Optional[str] = None,
    resume_text: Optional[str] = None,
    reference_pdf_text: Optional[str] = None,
    extra_text: Optional[str] = None,          # e.g. JD text
    image_data_urls: Optional[List[str]] = None,
) -> dict:
    """Run one evaluation. Any combination of inputs may be provided."""
    blocks = []  # text segments for the user message

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
        "temperature": 0,
        "response_format": {"type": "json_object"},
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": content},
        ],
    }

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
        if resp.status_code in _RETRYABLE and attempt < _MAX_ATTEMPTS - 1:
            time.sleep(min(60, 2 ** (attempt + 1)))
            continue
        raise RuntimeError(f"OpenAI chat failed HTTP {resp.status_code}: {body}")

    if last_err:
        raise last_err
    raise RuntimeError("OpenAI chat: exhausted retries")
