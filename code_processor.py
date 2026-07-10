"""Code-evaluation processor — turns a candidate submission into model input.

NEW, ADDITIVE MODULE. It does NOT execute candidate code. A submitted Jupyter
notebook already contains its cell outputs, so this module just READS the file:

  - .ipynb : parse the notebook JSON → code cells, markdown, text outputs, and
             embedded chart images (base64). Text outputs are trimmed head+tail so
             a giant dataframe dump can't blow the token budget; chart images are
             collected (capped) and sent to the vision model.
  - .py/.pyw/.ipy : just the code text (capped), no outputs to read.

The parsed text + images are handed to the EXISTING `llm_processor.evaluate(...)`
by the worker, so the OpenAI call, retries, and JSON parsing are reused unchanged.

Public functions
----------------
  load_prompt(settings, prompt_file) -> str
  parse_notebook(raw_bytes, settings) -> (text, [image_data_url, ...])
  read_python(raw_bytes, settings)   -> (text, [])
"""

import json
import re
from pathlib import Path
from typing import List, Tuple

from code_config import CodeSettings

_ANSI_RE = re.compile(r"\x1b\[[0-9;]*m")


def load_prompt(settings: CodeSettings, prompt_file: str) -> str:
    # Day-wise rubrics live in their own folder (settings.code_prompts_dir),
    # separate from the interview prompts, so the existing pipeline is untouched.
    return (Path(settings.code_prompts_dir) / prompt_file).read_text(encoding="utf-8")


def _source_to_str(src) -> str:
    """Notebook `source`/`text` fields are a list of lines OR a single string."""
    if isinstance(src, list):
        return "".join(str(s) for s in src)
    if src is None:
        return ""
    return str(src)


def _strip_ansi(text: str) -> str:
    return _ANSI_RE.sub("", text)


def _trim(text: str, cap: int, note: str = "trimmed") -> str:
    """Keep the head and tail of an over-long block, marking what was dropped."""
    if cap <= 0 or len(text) <= cap:
        return text
    head = int(cap * 0.7)
    tail = cap - head
    omitted = len(text) - cap
    return f"{text[:head]}\n...[{note}: {omitted} chars omitted]...\n{text[-tail:]}"


def _image_data_url(mime: str, b64) -> str:
    if isinstance(b64, list):
        b64 = "".join(b64)
    b64 = (b64 or "").strip().replace("\n", "")
    return f"data:{mime};base64,{b64}"


def parse_notebook(raw_bytes: bytes, settings: CodeSettings) -> Tuple[str, List[str]]:
    """Parse a .ipynb into (readable_text, chart_image_data_urls).

    Raises ValueError if the bytes are not a valid notebook — the worker turns
    that into a deterministic FAIL result rather than retrying forever.
    """
    try:
        nb = json.loads(raw_bytes.decode("utf-8", "replace"))
    except Exception as exc:
        raise ValueError(f"not a valid .ipynb (JSON parse failed): {exc}") from exc

    cells = nb.get("cells")
    if not isinstance(cells, list):
        raise ValueError("notebook has no 'cells' array")

    parts: List[str] = []
    images: List[str] = []

    for i, cell in enumerate(cells, start=1):
        if not isinstance(cell, dict):
            continue
        ctype = cell.get("cell_type")
        src = _source_to_str(cell.get("source", "")).strip()

        if ctype == "markdown":
            if src:
                parts.append(f"### Cell {i} [markdown]\n{src}")
            continue

        if ctype != "code":
            continue

        block = [f"### Cell {i} [code]"]
        if src:
            block.append("```python\n" + src.rstrip() + "\n```")

        out_texts: List[str] = []
        for out in cell.get("outputs", []) or []:
            if not isinstance(out, dict):
                continue
            otype = out.get("output_type")

            if otype == "stream":
                out_texts.append(_source_to_str(out.get("text", "")))

            elif otype in ("execute_result", "display_data"):
                data = out.get("data", {}) or {}
                text_plain = data.get("text/plain")
                if text_plain:
                    out_texts.append(_source_to_str(text_plain))
                # collect charts for the vision model (capped)
                for mime in ("image/png", "image/jpeg"):
                    if mime in data and len(images) < settings.max_images:
                        images.append(_image_data_url(mime, data[mime]))

            elif otype == "error":
                tb = out.get("traceback") or []
                joined = _strip_ansi("\n".join(_source_to_str(t) for t in tb))
                out_texts.append(joined or f"{out.get('ename')}: {out.get('evalue')}")

        cleaned = [t for t in out_texts if t and t.strip()]
        if cleaned:
            joined = _trim("\n".join(cleaned), settings.max_output_chars_per_cell,
                           note="cell output trimmed")
            block.append("--- output ---\n" + joined)

        parts.append("\n".join(block))

    text = "\n\n".join(parts)
    text = _trim(text, settings.max_total_chars, note="notebook trimmed to fit size limit")
    return text, images


def read_python(raw_bytes: bytes, settings: CodeSettings) -> Tuple[str, List[str]]:
    """A plain .py/.pyw/.ipy script: code only, no outputs, capped by size."""
    code = raw_bytes.decode("utf-8", "replace")
    code = _trim(code, settings.max_code_chars, note="script trimmed to fit size limit")
    return f"```python\n{code}\n```", []
