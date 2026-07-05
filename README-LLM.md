# LLM Scoring Stage

Runs alongside the transcript service in the same repo. Scores each deliverable
and writes JSON results, plus per-day and per-candidate overalls.

## Flow
```
transcript .txt / image .png / JD .txt written to S3
  → llm_trigger_lambda → SQS (llm-jobs)
  → llm_worker.py (on EC2)
     - video deliverable  → score from transcript (+resume, +reference PDF)
     - image deliverable  → score the PNG via vision
     - combined (System Design, Team Structure, JD) → video pulls in sibling image/text
  → writes <deliverable>_result(<engine>).json in the folder
  → when a day is complete FOR THAT ENGINE → DayOVERALL(<engine>)_result.json
  → candidate overall stays DISABLED
```

## Two transcription engines (A/B)
Every video is transcribed by **both** engines on the same source audio (see
`engines.py`):

| Engine | Tag | Scored | Salesforce | S3 |
|--------|-----|--------|------------|----|
| Whisper    | *(none)* | yes | **yes** (production) | yes |
| AssemblyAI | `(A)` | yes | no  | yes |

**Only AssemblyAI is tagged.** Whisper keeps the ORIGINAL filenames untouched, so
the production path (and its Salesforce payload) is byte-identical to before
AssemblyAI existed — "no tag = Whisper, `(A)` = AssemblyAI". The tag lives in the
**filename only**; folders, prefixes, and `metadata.json` are untouched:

| | Whisper (untagged, production) | AssemblyAI (S3-only) |
|--|--|--|
| transcript | `…_transcripts.txt` | `…(A)_transcripts.txt` |
| result | `…_result.json(Pass)(Attempt-1)` | `…_result(A).json(Pass)(Attempt-1)` |
| day overall | `DayOVERALL_result.json` | `DayOVERALL(A)_result.json` |
| Salesforce log | `…_sf_log(Attempt-N).json` | `…_sf_log(A)(Attempt-N).json` |

A **video** → two transcripts → two genuine scorings (different transcript →
potentially different verdict per engine). An **image/text** deliverable is
scored **once** and saved as two copies of the identical verdict (one LLM cost).
Attempt numbers, folder tagging, and day rollups are engine-scoped and never mix
the two engines. Only Whisper is pushed to Salesforce.

The trigger Lambda needs **no change**: `…_transcripts.txt` /
`…(A)_transcripts.txt` both still end with `*_transcripts.txt` (→ two llm jobs),
and `DayOVERALL(A)_result.json` still contains `overall` (→ ignored).

## Files
- `llm_worker.py`     — the worker you run (multi-threaded SQS consumer)
- `llm_processor.py`  — builds input (text/image/pdf), calls OpenAI, parses JSON
- `llm_s3.py`         — S3 read/list/sibling/resume helpers
- `llm_overall.py`    — day + candidate overall (math combine), metadata reading
- `llm_config.py`     — settings + deliverable→prompt mapping + combined rules
- `llm_trigger_lambda.py` — S3 event → SQS (decides transcript/image/text)
- `prompts/`          — all 10 evaluation prompts (JSON output)
- `pdf/`              — reference PDFs (31-Questions, Niche-Questions)
- `requirements-llm.txt`

## AWS setup
1. SQS queue `llm-jobs` (+ DLQ `llm-jobs-dlq`, maxReceiveCount 3)
2. Lambda `llm-enqueue-trigger` from `llm_trigger_lambda.py`, env `LLM_QUEUE_URL`,
   with `sqs:SendMessage` on the queue
3. A SECOND S3 event notification on the bucket → this Lambda.
   (You already have one for the transcript Lambda. This one has no suffix filter
   since it must catch .txt transcripts, .png images, and .txt JD files — the
   Lambda code decides what to enqueue and ignores the rest.)
4. EC2 role: add `sqs:ReceiveMessage`, `sqs:DeleteMessage` on `llm-jobs`
   (S3 read/write already granted for the transcript worker)

## Run on EC2
```
pip install -r requirements-llm.txt
# add to .env:
#   LLM_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/985100584614/llm-jobs
#   OPENAI_LLM_MODEL=gpt-4o
python llm_worker.py
```

## Result files
- Whisper:    `<deliverable>_result.json(<Pass|Fail>)(Attempt-N)` and `DayOVERALL_result.json` — original names, unchanged
- AssemblyAI: `<deliverable>_result(A).json(<Pass|Fail>)(Attempt-N)` and `DayOVERALL(A)_result.json` — the only new files
- `CANDIDATE_OVERALL_result.json` — DISABLED (per-candidate rollup is not written)
