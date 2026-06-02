"""LLM stage configuration + deliverable rules.

Sits in the same repo as the transcript service (config.py, main.py, etc.).
The LLM worker uses its own env vars (LLM_QUEUE_URL, OPENAI_LLM_MODEL) but shares
the bucket and OpenAI key.
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
class LLMSettings:
    aws_region:    str = _str("AWS_REGION", "us-east-1")
    bucket:        str = _str("BUCKET", "candidate-deliverables")
    llm_queue_url: str = _str("LLM_QUEUE_URL")

    sqs_wait_seconds:       int = _int("SQS_WAIT_SECONDS", 20)
    sqs_visibility_timeout: int = _int("LLM_SQS_VISIBILITY_TIMEOUT", 600)
    worker_threads:         int = _int("LLM_WORKER_THREADS", 4)

    openai_api_key: str = _str("OPENAI_API_KEY")
    openai_model:   str = _str("OPENAI_LLM_MODEL", "gpt-4o")

    transcript_suffix: str = _str("TRANSCRIPT_SUFFIX", "_transcripts.txt")
    result_suffix:     str = _str("LLM_RESULT_SUFFIX", "_result.json")

    # Pass/Fail tags appended to every file in a deliverable folder after analysis.
    # Placed AFTER the extension (e.g. video.mp4(Fail)) so the router/transcript
    # worker never re-process a tagged file.
    pass_marker: str = _str("LLM_PASS_MARKER", "(Pass)")
    fail_marker: str = _str("LLM_FAIL_MARKER", "(Fail)")

    prompts_dir: str = _str("PROMPTS_DIR", "prompts")
    pdf_dir:     str = _str("PDF_DIR", "pdf")

    failure_sleep_seconds: int = _int("FAILURE_SLEEP_SECONDS", 5)

    # Salesforce callout (JWT bearer; credentials from AWS Secrets Manager).
    sf_enabled:     bool = _bool("SF_ENABLED", False)
    sf_secret_name: str  = _str("SF_SECRET_NAME", "sf/jwt/credentials")
    sf_apex_path:   str  = _str("SF_APEX_PATH", "")  # e.g. /services/apexrest/deliverableResult
    sf_audience:    str  = _str("SF_AUDIENCE", "https://login.salesforce.com")
    sf_timeout:     int  = _int("SF_TIMEOUT", 30)

    def validate(self):
        missing = [k for k, v in {
            "LLM_QUEUE_URL": self.llm_queue_url,
            "OPENAI_API_KEY": self.openai_api_key,
        }.items() if not v]
        if self.sf_enabled and not self.sf_apex_path:
            missing.append("SF_APEX_PATH")
        if missing:
            raise SystemExit(f"[CONFIG] missing required env vars: {', '.join(missing)}")


LLM_SETTINGS = LLMSettings()


# ── Deliverable → prompt + extra inputs ──────────────────────────────────────
#
# Matching: deliverable folder name CONTAINS the key (case-insensitive),
# first match wins (order most-specific first).
#
# extras can include:
#   "resume"          → attach candidate resume text (trainingSteps/resume pdf/)
#   "pdf:NAME"        → attach a reference PDF shipped in the local pdf/ folder
#   "sibling_image"   → pull in the diagram image from the matching sibling folder
#   "sibling_text"    → pull in the text from the matching sibling folder
#
DELIVERABLE_RULES = [
    # Day 1
    ("hr questions",                  "mock-prompt.txt",             ["pdf:31-Questions.pdf", "resume"]),
    ("niche fundamentals",            "niche-prompt.txt",            ["pdf:Niche-Questions.pdf", "resume"]),

    # Day 2
    ("project scenario",              "project-scenario.txt",        ["resume"]),

    # Day 3
    ("introduction and career flow",  "intro-prompt.txt",            ["resume"]),
    ("tools and system explanation",  "Tools-Technology-prompt.txt", ["resume"]),
    ("team structure video",          "Tools-Technology-prompt.txt", ["resume", "sibling_image"]),
    ("resume-based mock interview",   "CV-prompt.txt",               ["resume"]),

    # Day 4
    ("recruiter persona",             "persona.txt",                 ["resume"]),
    ("hiring manager persona",        "persona.txt",                 ["resume"]),
    ("architect persona",             "persona.txt",                 ["resume"]),

    # Day 5  (JD video pulls in its sibling text + image → one result)
    ("job description alignment",     "JD-prompt.txt",               ["resume", "sibling_text", "sibling_image"]),
    ("small talk",                    "smalltalk.txt",               ["resume"]),

    # Day 6  (system design video pulls in its matching diagram image → one result)
    ("system design",                 "System-design.txt",           ["resume", "sibling_image"]),
]

# Folders that are INPUTS to a combined deliverable, not scored on their own.
# If a folder name contains any of these, the worker skips scoring it directly
# (its content is pulled in by the matching video deliverable instead).
COMBINED_INPUT_MARKERS = [
    "team structure diagram",
    "job description alignment  1 text",
    "job description alignment 1 text",
    "job description alignment  1 image",
    "job description alignment 1 image",
    "system design problem 1 image",
    "system design problem 2 image",
]


def match_rule(deliverable_folder_name: str):
    name = deliverable_folder_name.lower()
    for needle, prompt_file, extras in DELIVERABLE_RULES:
        if needle in name:
            return prompt_file, extras
    return None


def is_combined_input_only(deliverable_folder_name: str) -> bool:
    """True if this folder's content is pulled into a sibling's result and should
    NOT produce its own result."""
    name = deliverable_folder_name.lower()
    return any(m in name for m in COMBINED_INPUT_MARKERS)
