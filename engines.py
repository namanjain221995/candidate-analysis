"""Transcription engine identifiers — shared by the transcript stage (main.py /
transcriber.py) and the LLM stage (llm_worker.py / llm_overall.py).

Two engines run in parallel on the SAME source audio for every video deliverable:

  Whisper      — the production path: scored, pushed to Salesforce, stored in S3.
                 Its files carry NO engine tag, so they keep the ORIGINAL naming
                 convention exactly (byte-identical to before AssemblyAI existed).
  (A) AssemblyAI — the A/B comparison path: scored and stored in S3 ONLY, never
                 pushed to Salesforce. Its files carry the "(A)" tag.

Only AssemblyAI is tagged — so "no tag = Whisper, (A) = AssemblyAI". The tag
lives in the FILENAME only; folders, prefixes, and metadata.json are untouched:

  transcript:  <stem>_transcripts.txt          <stem>(A)_transcripts.txt
  result:      <name>_<id>_result.json(...)    <name>_<id>_result(A).json(...)
  day overall: DayOVERALL_result.json          DayOVERALL(A)_result.json
  sf log:      <name>_<id>_sf_log(...).json    <name>_<id>_sf_log(A)(...).json

Leaving Whisper untagged guarantees the production path (filenames + Salesforce
payload) is byte-identical to the original pipeline. This module is the single
source of truth for these identifiers so the two stages can never drift apart.
"""

import os

WHISPER = "W"
ASSEMBLYAI = "A"

# The production engine — the ONLY engine whose per-deliverable results are
# pushed to Salesforce. Everything else is stored in S3 only.
SF_ENGINE = WHISPER

# Engine assumed for a legacy/untagged transcript (a *_transcripts.txt written
# before engine tagging existed). Keeps old artifacts flowing as Whisper.
DEFAULT_ENGINE = WHISPER

_ALL = (WHISPER, ASSEMBLYAI)


def configured_engines():
    """Ordered list of engines to run, from TRANSCRIPTION_ENGINES (default 'W,A').

    Whisper is always present and always first, so the production path is never
    silently dropped and always runs (and becomes durable) before AssemblyAI.
    """
    raw = os.getenv("TRANSCRIPTION_ENGINES") or "W,A"
    out = []
    for tok in raw.replace(";", ",").split(","):
        t = tok.strip().upper()
        if t and t in _ALL and t not in out:
            out.append(t)
    # Whisper (production) is ALWAYS present and ALWAYS first — never silently
    # dropped, even if TRANSCRIPTION_ENGINES omits it or is mis-set (e.g. "A").
    return [WHISPER] + [e for e in out if e != WHISPER]


def engine_tag(engine: str) -> str:
    """The literal label inserted into filenames.

    Whisper is UNTAGGED (empty string) so production keeps the original naming;
    only AssemblyAI is tagged:  engine_tag('W') -> '',  engine_tag('A') -> '(A)'.
    """
    return "" if engine == WHISPER else f"({engine})"


def split_engine_tag(stem: str):
    """('...(A)') -> ('A', '...');  anything else -> (None, '...').

    Only AssemblyAI carries a tag, so a stem ending in '(A)' is AssemblyAI and
    everything else is Whisper (the caller maps None -> DEFAULT_ENGINE). A
    Salesforce id is 15-18 chars and ends like '...vMAC)', never '(A)', so an id
    in parentheses (e.g. '...(a1UO1000002Rd0vMAC)') is left intact.
    """
    suffix = f"({ASSEMBLYAI})"   # "(A)"
    if stem.endswith(suffix):
        return ASSEMBLYAI, stem[: -len(suffix)]
    return None, stem
