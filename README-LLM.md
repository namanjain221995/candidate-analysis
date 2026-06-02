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
  → writes <deliverable>_result.json in the folder
  → when a day is complete → DayOVERALL_result.json
  → refreshes CANDIDATE_OVERALL_result.json at the candidate root
```

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
- `<deliverable>_result.json` — per deliverable (score, result, reasoning, positives, negatives)
- `DayOVERALL_result.json`    — per day (overallScore, result, deliverables[], positives, negatives)
- `CANDIDATE_OVERALL_result.json` — per candidate (rolls up all days)
