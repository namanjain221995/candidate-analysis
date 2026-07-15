"""Transcription engine identifiers — shared by the transcript stage (main.py /
transcriber.py) and the LLM stage (llm_worker.py / llm_overall.py).

PRODUCTION ENGINE: AssemblyAI. Every new video is transcribed by AssemblyAI ONLY;
its results are scored, pushed to Salesforce, and stored in S3. AssemblyAI is now
UNTAGGED, so it keeps the clean original naming:

  transcript:  <stem>_transcripts.txt
  result:      <name>_<id>_result.json(...)
  day overall: DayOVERALL_result.json
  sf log:      <name>_<id>_sf_log(...).json

Whisper is RETIRED. Its code remains (dormant, for easy rollback) but it never
runs — configured_engines() returns AssemblyAI only — so the OpenAI/Whisper
transcription endpoint is never called and its billing drops to zero. (The OpenAI
key is still used by the LLM SCORING stage; that is a separate concern.)

This module is the single source of truth for these identifiers so the two stages
can never drift apart.
"""

import os

WHISPER = "W"
ASSEMBLYAI = "A"

# The production engine — the ONLY engine whose per-deliverable results are
# pushed to Salesforce. Everything else is stored in S3 only. AssemblyAI now
# holds this role (was Whisper).
SF_ENGINE = ASSEMBLYAI

# Engine assumed for an untagged transcript. AssemblyAI is now untagged, so a
# plain *_transcripts.txt maps to AssemblyAI (as does any legacy untagged file).
DEFAULT_ENGINE = ASSEMBLYAI

_ALL = (WHISPER, ASSEMBLYAI)


def configured_engines():
    """The engines to run. AssemblyAI is now the SOLE production engine; Whisper is
    retired and never runs, so the OpenAI/Whisper transcription endpoint is never
    hit. TRANSCRIPTION_ENGINES is deliberately NOT consulted — a stale 'W,A' left
    in the environment must never silently re-enable Whisper. To bring Whisper back
    (rollback), return it here explicitly.
    """
    return [ASSEMBLYAI]


def engine_tag(engine: str) -> str:
    """The literal label inserted into filenames.

    AssemblyAI (production) is now UNTAGGED (empty string) so it keeps the clean
    original naming; a retired-Whisper file — if one were ever produced — would be
    tagged '(W)':  engine_tag('A') -> '',  engine_tag('W') -> '(W)'.
    """
    return "" if engine == ASSEMBLYAI else f"({engine})"


def split_engine_tag(stem: str):
    """('...(W)') -> ('W', '...');  anything else -> (None, '...').

    AssemblyAI (production) is untagged, so an untagged stem is AssemblyAI (the
    caller maps None -> DEFAULT_ENGINE = AssemblyAI). Only a retired-Whisper file
    would carry a '(W)' tag. A Salesforce id is 15-18 chars and ends like
    '...vMAC)', never '(W)', so an id in parentheses is left intact.
    """
    suffix = f"({WHISPER})"   # "(W)"
    if stem.endswith(suffix):
        return WHISPER, stem[: -len(suffix)]
    return None, stem
