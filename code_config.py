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


# ── Deliverable/Day → prompt mapping ──────────────────────────────────────────
#
# Matching mirrors the LLM stage (llm_config.match_rule): the match context is
# lowercased, underscores→spaces, spaces collapsed; FIRST substring match wins.
# The worker passes "<day folder> <deliverable folder>" as the context, so the Day
# folder name ("Day 2 - Regression(...)") reliably selects the rubric even if the
# assignment folder omits the topic. Day-number rules come first; topic rules are a
# fallback for folders that omit the number. Advanced AI-ML Training curriculum:
#   Day 1 ML Fundamentals · Day 2 Regression · Day 3 Classification ·
#   Day 4 Model Optimization · Day 5 Unsupervised Learning · Day 6 NLP
CODE_DELIVERABLE_RULES = [
    # day-number (most reliable — present on the Day/training-step folder)
    ("day 1", "day-1-ml-fundamentals.txt"),
    ("day 2", "day-2-regression.txt"),
    ("day 3", "day-3-classification.txt"),
    ("day 4", "day-4-model-optimization.txt"),
    ("day 5", "day-5-unsupervised.txt"),
    ("day 6", "day-6-nlp.txt"),
    # topic fallbacks (if a folder omits the day number)
    ("machine learning fundamentals", "day-1-ml-fundamentals.txt"),
    ("regression",                    "day-2-regression.txt"),
    ("classification",                "day-3-classification.txt"),
    ("model optimization",            "day-4-model-optimization.txt"),
    ("unsupervised",                  "day-5-unsupervised.txt"),
    ("natural language",              "day-6-nlp.txt"),
    ("nlp",                           "day-6-nlp.txt"),
]

# Generic fallback prompt (a real, working evaluator) used if no Day rule matches.
DEFAULT_CODE_PROMPT = _str("CODE_DEFAULT_PROMPT", "code-eval.txt")


def match_code_prompt(match_context: str) -> str:
    """Pick the rubric prompt file for a coding assignment. `match_context` is the
    Day folder name + deliverable folder name (see code_worker)."""
    name = " ".join((match_context or "").lower().replace("_", " ").split())
    for needle, prompt_file in CODE_DELIVERABLE_RULES:
        if needle in name:
            return prompt_file
    return DEFAULT_CODE_PROMPT
