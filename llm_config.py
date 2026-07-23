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
    # AssemblyAI key is read here too (the LLM stage doesn't transcribe) ONLY to
    # gate the (A) score-once/save-twice copies for image/text deliverables: when
    # no key is configured the transcript stage produces no (A) transcripts, so
    # the worker must likewise emit no (A) image/text copies — keeping "no key ⇒
    # zero new files" true and the two stages consistent.
    assemblyai_api_key: str = _str("ASSEMBLYAI_API_KEY")
    # gpt-5.x are reasoning models: the worker automatically sends
    # reasoning_effort instead of temperature for them (see llm_processor).
    openai_model:   str = _str("OPENAI_LLM_MODEL", "gpt-5.5")
    # none | low | medium | high | xhigh. "none" = fastest (Instant-style),
    # "low" adds light reasoning for the rubric math. Default: low.
    openai_reasoning_effort: str = _str("OPENAI_REASONING_EFFORT", "low")

    # ── Deterministic scoring (same input → same verdict) ─────────────────────
    # openai_seed: a fixed request seed (best-effort determinism; auto-dropped if a
    #   reasoning model rejects it — see llm_processor).
    # llm_cache_enabled: content-hash result cache in S3 that GUARANTEES an
    #   identical verdict when the same submission is re-scored. Cache objects live
    #   under llm_cache_prefix (a '.json' suffix that matches no trigger route).
    openai_seed:       int  = _int("OPENAI_SEED", 7)
    llm_cache_enabled: bool = _bool("LLM_CACHE_ENABLED", True)
    llm_cache_prefix:  str  = _str("LLM_CACHE_PREFIX", "_llm_cache/")

    transcript_suffix: str = _str("TRANSCRIPT_SUFFIX", "_transcripts.txt")
    result_suffix:     str = _str("LLM_RESULT_SUFFIX", "_result.json")

    # Pass/Fail tags appended to every file in a deliverable folder after analysis.
    # Placed AFTER the extension (e.g. video.mp4(Fail)) so the router/transcript
    # worker never re-process a tagged file.
    pass_marker: str = _str("LLM_PASS_MARKER", "(Pass)")
    fail_marker: str = _str("LLM_FAIL_MARKER", "(Fail)")

    # ── Link deliverables (Github/Kaggle .txt link submissions) ───────────────
    # Link submissions are NOT graded. When a candidate uploads a link, the worker
    # ACKNOWLEDGES it to Salesforce so the record leaves "Pending". The Apex only
    # accepts Pass/Fail, so links are reported as PASS (change LINK_RESULT if the
    # endpoint later gains a real "Submitted" status). A deliverable counts as a
    # "link" when its folder name contains any LINK_KEYWORDS substring (comma-sep).
    link_submission_enabled: bool = _bool("LINK_SUBMISSION_ENABLED", True)
    link_result:             str  = _str("LINK_RESULT", "PASS")
    link_keywords:           str  = _str("LINK_KEYWORDS", "github link,kaggle link")

    prompts_dir: str = _str("PROMPTS_DIR", "prompts")
    pdf_dir:     str = _str("PDF_DIR", "pdf")

    failure_sleep_seconds: int = _int("FAILURE_SLEEP_SECONDS", 5)

    # Salesforce callout — OAuth client_credentials → Bearer POST to an Apex REST
    # endpoint. Credentials come from env/.env, never committed.
    sf_enabled:       bool = _bool("SF_ENABLED", False)
    sf_login_url:     str  = _str("SF_LOGIN_URL", "https://techsara--dev9.sandbox.my.salesforce.com")
    sf_apex_path:     str  = _str("SF_APEX_PATH", "/services/apexrest/v1/deliverable-result/")
    sf_client_id:     str  = _str("SF_CLIENT_ID")       # connected-app consumer key
    sf_client_secret: str  = _str("SF_CLIENT_SECRET")   # connected-app secret
    sf_timeout:       int  = _int("SF_TIMEOUT", 30)

    def validate(self):
        missing = [k for k, v in {
            "LLM_QUEUE_URL": self.llm_queue_url,
            "OPENAI_API_KEY": self.openai_api_key,
        }.items() if not v]
        if self.sf_enabled and not self.sf_client_id:
            missing.append("SF_CLIENT_ID")
        if self.sf_enabled and not self.sf_client_secret:
            missing.append("SF_CLIENT_SECRET")
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
#   "own_image"       → score the image(s) in THIS deliverable's own folder
#   "own_text"        → score the text in THIS deliverable's own folder
#
# Rule ordering note: substring match, first match wins, so the standalone
# image/text rules (most specific, e.g. "team structure diagram") MUST come
# before the broader video rules (e.g. "team structure video" / "system design").
DELIVERABLE_RULES = [
    # Day 1
    ("hr questions",                  "mock-prompt.txt",             ["pdf:31-Questions.pdf", "resume"]),
    ("niche fundamentals",            "niche-prompt.txt",            ["pdf:Niche-Questions.pdf", "resume"]),

    # Day 2
    ("project scenario",              "project-scenario.txt",        ["resume"]),

    # Day 3
    ("introduction and career flow",  "intro-prompt.txt",            ["resume"]),
    ("tools and system explanation",  "Tools-Technology-prompt.txt", ["resume"]),
    # Team structure: the diagram is now scored standalone (own image) AND the
    # video still references the sibling diagram. Two separate results.
    ("team structure diagram",        "Tools-Technology-prompt.txt", ["resume", "own_image"]),
    ("team structure video",          "Tools-Technology-prompt.txt", ["resume", "sibling_image"]),
    ("resume-based mock interview",   "CV-prompt.txt",               ["resume"]),

    # Day 4
    ("recruiter persona",             "persona.txt",                 ["resume"]),
    ("hiring manager persona",        "persona.txt",                 ["resume"]),
    ("architect persona",             "persona.txt",                 ["resume"]),

    # Day 5  JD: the text and image folders are now scored standalone, AND the
    # JD video still references its sibling text + image. Separate results.
    # (match_rule normalizes '_1' → ' 1' and collapses spaces, so one spelling
    # per rule is enough and covers 'Alignment _1', 'Alignment  1', 'Alignment 1'.)
    ("job description alignment 1 text",   "JD-prompt.txt",          ["resume", "own_text"]),
    ("job description alignment 2 text",   "JD-prompt.txt",          ["resume", "own_text"]),
    ("job description alignment 1 image",  "JD-prompt.txt",          ["resume", "own_image"]),
    ("job description alignment 2 image",  "JD-prompt.txt",          ["resume", "own_image"]),
    ("job description alignment",     "JD-prompt.txt",               ["resume", "sibling_text", "sibling_image"]),
    ("small talk",                    "smalltalk.txt",               ["resume"]),

    # Day 6  System design. Per candidate there are two folders:
    #   'System Design Problem 2' holds the architecture DIAGRAM (image). It is a
    #       standalone image deliverable: scored on its OWN diagram (own_image),
    #       producing its own _result.json + Salesforce record.
    #   'System Design Problem 1' holds the VIDEO. It is scored from its transcript
    #       AND enriched with the diagram living in the sibling problem folder
    #       (Problem 2) via day_image, so the spoken design is judged against the
    #       drawn architecture. The image therefore feeds BOTH results.
    # These two MUST come before the generic 'system design' fallback (first
    # substring match wins). The fallback covers any other/legacy layout.
    ("system design problem 2",       "System-design.txt",           ["resume", "own_image"]),
    ("system design problem 1",       "System-design.txt",           ["resume", "day_image"]),
    ("system design",                 "System-design.txt",           ["resume", "own_image", "sibling_image"]),
]

# Folders that are INPUTS to a combined deliverable and are NOT scored on their own.
#
# Product decision: image-only and JD text-only deliverables are now scored
# STANDALONE (each gets its own _result.json), so they are no longer treated as
# combined-input-only. The companion video deliverables still pull the sibling
# image/text for their own result, so a video and its image/text folder produce
# TWO separate results — each counted once in the day overall (the overall groups
# by deliverable subfolder, so distinct folders never double-count).
COMBINED_INPUT_MARKERS = []


def match_rule(deliverable_folder_name: str):
    # Normalize: lowercase, underscores → spaces, collapse repeated spaces, so
    # 'Job Description Alignment _1 text' matches 'job description alignment 1 text'.
    name = " ".join(deliverable_folder_name.lower().replace("_", " ").split())
    for needle, prompt_file, extras in DELIVERABLE_RULES:
        if needle in name:
            return prompt_file, extras
    return None


def is_combined_input_only(deliverable_folder_name: str) -> bool:
    """True if this folder's content is pulled into a sibling's result and should
    NOT produce its own result."""
    name = deliverable_folder_name.lower()
    return any(m in name for m in COMBINED_INPUT_MARKERS)