"""Code-evaluation stage configuration + Day → prompt mapping.

NEW, ADDITIVE MODULE. It lives in the same repo as the transcript/LLM services
and REUSES their helpers (llm_processor, salesforce, llm_s3) without modifying
them. The code worker consumes the `notebook-jobs` queue that the Router Lambda
already feeds.

Design notes
------------
- Candidate notebooks are NOT executed. The submitted .ipynb already contains its
  cell outputs, so the worker reads code + outputs + charts and sends them to
  OpenAI for a Pass/Fail verdict + score + detailed analysis. (No Docker, no
  dataset fetching, no sandbox.)
- This settings object is intentionally shaped so it can be passed straight into
  the EXISTING `llm_processor.evaluate(settings, ...)` and
  `salesforce.notify(settings, ...)` — it exposes the same attribute names those
  functions read (openai_model, openai_reasoning_effort, sf_* ...). Nothing in
  the existing pipeline is changed.
"""

import os
from dataclasses import dataclass

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


def _str(name, default=""):
    v = os.getenv(name)
    return v.strip() if v is not None and v.strip() != "" else default


def _int(name, default):
    return int(_str(name, str(default)))


def _bool(name, default=False):
    return _str(name, "true" if default else "false").lower() in {"1", "true", "yes", "y", "on"}


@dataclass(frozen=True)
class CodeSettings:
    # ── AWS ──────────────────────────────────────────────────────────────────
    aws_region:        str = _str("AWS_REGION", "us-east-1")
    bucket:            str = _str("BUCKET", "candidate-deliverables")
    # The queue the Router Lambda routes .ipynb/.py/.pyw/.ipy coding submissions to.
    notebook_queue_url: str = _str("NOTEBOOK_QUEUE_URL")

    # ── SQS behaviour ────────────────────────────────────────────────────────
    sqs_wait_seconds:       int = _int("SQS_WAIT_SECONDS", 20)          # long-poll
    # 30 min, matching the recommended notebook-jobs visibility timeout so a slow
    # OpenAI call never lets the message reappear mid-evaluation.
    sqs_visibility_timeout: int = _int("CODE_SQS_VISIBILITY_TIMEOUT", 1800)
    worker_threads:         int = _int("CODE_WORKER_THREADS", 4)

    # ── OpenAI ───────────────────────────────────────────────────────────────
    openai_api_key: str = _str("OPENAI_API_KEY")
    # Own model knob (falls back to the LLM stage's model, then a sensible default)
    # so code grading can use a different/vision-capable model without touching the
    # transcript/LLM services.
    openai_model:   str = _str("OPENAI_CODE_MODEL", _str("OPENAI_LLM_MODEL", "gpt-5.5"))
    # none | low | medium | high | xhigh. Code review benefits from a little more
    # reasoning than the rubric-math LLM stage, so default 'medium'.
    openai_reasoning_effort: str = _str("OPENAI_CODE_REASONING_EFFORT", "medium")

    # ── Deterministic scoring (same code → same verdict) ──────────────────────
    # Read straight by llm_processor.evaluate (shared with the LLM stage): a fixed
    # seed for best-effort determinism, plus a content-hash S3 cache that GUARANTEES
    # an identical verdict when the same .ipynb/.py is re-scored. Own env knobs
    # (fall back to the LLM-stage vars) so code grading can tune them independently.
    openai_seed:       int  = _int("OPENAI_CODE_SEED", _int("OPENAI_SEED", 7))
    llm_cache_enabled: bool = _bool("CODE_LLM_CACHE_ENABLED", _bool("LLM_CACHE_ENABLED", True))
    llm_cache_prefix:  str  = _str("LLM_CACHE_PREFIX", "_llm_cache/")

    # ── Prompts / result naming (mirrors the LLM stage conventions) ───────────
    prompts_dir:   str = _str("PROMPTS_DIR", "prompts")
    # Day-wise coding-assignment rubric prompts live in their OWN folder, separate
    # from the interview prompts in prompts/, so the existing pipeline is untouched.
    code_prompts_dir: str = _str("CODE_PROMPTS_DIR", "advance training prompt")
    result_suffix: str = _str("LLM_RESULT_SUFFIX", "_result.json")
    pass_marker:   str = _str("LLM_PASS_MARKER", "(Pass)")
    fail_marker:   str = _str("LLM_FAIL_MARKER", "(Fail)")

    # ── Size / cost caps (notebooks with big outputs or many charts) ──────────
    # Text outputs (big dataframes, long logs) are trimmed head+tail per cell.
    max_output_chars_per_cell: int = _int("CODE_MAX_OUTPUT_CHARS_PER_CELL", 2000)
    # Whole-submission text budget before a final trim (code is always kept; the
    # least-important text is dropped first).
    max_total_chars:           int = _int("CODE_MAX_TOTAL_CHARS", 120000)
    # Chart/plot images (base64) sent to the vision model — capped to control cost.
    max_images:                int = _int("CODE_MAX_IMAGES", 10)
    # Plain .py/.pyw/.ipy scripts: cap the code text.
    max_code_chars:            int = _int("CODE_MAX_CODE_CHARS", 100000)

    # ── Duplicate-event protection ───────────────────────────────────────────
    # A re-DELIVERED S3/SQS event for the SAME upload must not create a new
    # attempt. We drop a tiny marker object per processed upload (keyed by
    # version_id/etag/sequencer) under <deliverable>/_processed/. No new AWS
    # component required; swap for DynamoDB later if stronger guarantees are needed.
    dedup_enabled: bool = _bool("CODE_DEDUP_ENABLED", True)

    # ── Retry pacing ─────────────────────────────────────────────────────────
    failure_sleep_seconds: int = _int("FAILURE_SLEEP_SECONDS", 5)

    # ── Salesforce (reuses salesforce.notify — same env vars as the LLM stage) ─
    # Code results push to Salesforce. ON by default (the team wants code results in
    # Salesforce). Resolution: CODE_SF_ENABLED overrides; if unset it inherits
    # SF_ENABLED; if neither is set it defaults to True. Set CODE_SF_ENABLED=false to
    # turn code-Salesforce off without touching the video pipeline (e.g. to validate
    # results in S3 first). Requires SF_CLIENT_ID + SF_CLIENT_SECRET in the .env.
    sf_enabled:       bool = _bool("CODE_SF_ENABLED", _bool("SF_ENABLED", True))
    sf_login_url:     str  = _str("SF_LOGIN_URL", "https://techsara--dev9.sandbox.my.salesforce.com")
    sf_apex_path:     str  = _str("SF_APEX_PATH", "/services/apexrest/v1/deliverable-result/")
    sf_client_id:     str  = _str("SF_CLIENT_ID")
    sf_client_secret: str  = _str("SF_CLIENT_SECRET")
    sf_timeout:       int  = _int("SF_TIMEOUT", 30)

    def validate(self):
        missing = [k for k, v in {
            "NOTEBOOK_QUEUE_URL": self.notebook_queue_url,
            "OPENAI_API_KEY": self.openai_api_key,
        }.items() if not v]
        if self.sf_enabled and not self.sf_client_id:
            missing.append("SF_CLIENT_ID")
        if self.sf_enabled and not self.sf_client_secret:
            missing.append("SF_CLIENT_SECRET")
        if missing:
            raise SystemExit(f"[CONFIG] missing required env vars: {', '.join(missing)}")


CODE_SETTINGS = CodeSettings()


# ── Deliverable → prompt mapping (TOPIC-based) ────────────────────────────────
#
# Matching mirrors the LLM stage: the context is lowercased, underscores→spaces,
# spaces collapsed; FIRST substring match wins. code_worker matches on the
# DELIVERABLE folder name first (e.g. "Natural Language Processing Coding
# Assignment"), falling back to "<day folder> <deliverable>" only if that yields no
# topic. We key on the TOPIC, not the day number: the 15-day calendar renumbered the
# topics AND puts BOTH Day-8 ANN assignments (Fundamentals and Advanced Concepts)
# under one day folder, so the day number is ambiguous but the topic is not. The
# rubric FILE names match the new day numbers (both Day-8 ANN assignments share the
# "day-8-" prefix, distinguished by the "-advanced" suffix).
# 15-day curriculum: D1 ML Fundamentals · D2 Regression · D3 Classification ·
# D4 Model Optimization · D6 Unsupervised · D7 NLP · D8 ANN Fundamentals + ANN
# Advanced · D9 CNN · D11 RNN · D12 LSTM/GRU · D13 Transformers
# (D5/D10/D14 revision, D15 combined mock — no coding deliverable).
CODE_DELIVERABLE_RULES = [
    # MOST SPECIFIC FIRST: deep-learning topics precede generic words like
    # "classification"/"regression" that also appear in problem statements.
    ("advanced concept",              "day-8-ann-advanced.txt"),   # D8 ANN Advanced
    ("ann advance",                   "day-8-ann-advanced.txt"),
    ("artificial neural network",     "day-8-ann.txt"),            # D8 ANN Fundamentals
    ("transformer",                   "day-13-transformers.txt"),  # D13 Transformers
    # LSTM/GRU before "recurrent": the D12 title "LSTM & Gated Recurrent Unit (GRU)"
    # contains "recurrent", which would otherwise steal it to the RNN rubric.
    ("lstm",                          "day-12-lstm.txt"),          # D12 LSTM/GRU
    ("gru",                           "day-12-lstm.txt"),
    ("convolutional",                 "day-9-cnn.txt"),            # D9 CNN
    ("recurrent",                     "day-11-rnn.txt"),           # D11 RNN
    ("unsupervised",                  "day-6-unsupervised.txt"),   # D6 Unsupervised
    ("natural language",              "day-7-nlp.txt"),            # D7 NLP
    ("nlp",                           "day-7-nlp.txt"),
    ("model optimization",            "day-4-model-optimization.txt"),
    ("classification",                "day-3-classification.txt"),
    ("regression",                    "day-2-regression.txt"),
    ("machine learning fundamental",  "day-1-ml-fundamentals.txt"),
    # ── DAY-NUMBER FALLBACK ────────────────────────────────────────────────────
    # Last resort when a deliverable/day folder carries NO topic word (just "Day N").
    # Topic rules above always win first. Two-digit days precede "day 1" because
    # "day 1" is a substring of "day 11"/"day 12"/"day 13". NO "day 8" rule: Day 8
    # holds BOTH ANN assignments (Fundamentals + Advanced), so the number cannot
    # disambiguate them — a topic-less Day-8 name falls through to the default prompt.
    # Days 5/10/14 (revision) and 15 (mock) have no coding deliverable, so no rule.
    ("day 13", "day-13-transformers.txt"),
    ("day 12", "day-12-lstm.txt"),
    ("day 11", "day-11-rnn.txt"),
    ("day 9",  "day-9-cnn.txt"),
    ("day 7",  "day-7-nlp.txt"),
    ("day 6",  "day-6-unsupervised.txt"),
    ("day 4",  "day-4-model-optimization.txt"),
    ("day 3",  "day-3-classification.txt"),
    ("day 2",  "day-2-regression.txt"),
    ("day 1",  "day-1-ml-fundamentals.txt"),
]

# Generic fallback prompt (a real, working evaluator) used if no Day rule matches.
DEFAULT_CODE_PROMPT = _str("CODE_DEFAULT_PROMPT", "code-eval.txt")


def match_code_prompt(match_context: str) -> str:
    """Pick the rubric prompt file for a coding assignment. `match_context` is the
    deliverable folder name (code_worker tries that first, then falls back to
    "<day folder> <deliverable folder>"). Returns DEFAULT_CODE_PROMPT if nothing
    matches."""
    name = " ".join((match_context or "").lower().replace("_", " ").split())
    for needle, prompt_file in CODE_DELIVERABLE_RULES:
        if needle in name:
            return prompt_file
    return DEFAULT_CODE_PROMPT
