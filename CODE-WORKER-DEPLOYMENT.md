# Code-Evaluation Worker — Deployment Runbook

**Audience:** the engineer/DevOps deploying this to the EC2.
**What's new:** a third always-on worker (`code-service`) that grades Advanced
AI-ML Training **coding assignments** (Jupyter `.ipynb` / Python `.py`). It runs
next to the existing `transcript-service` and `llm-service` and **changes nothing**
about them.

---

## 1. What this service does (in one paragraph)

When a candidate uploads a coding-assignment notebook to the
`candidate-deliverables` S3 bucket, the **Router Lambda** (already live) routes it
to the **`notebook-jobs`** SQS queue. This new worker (`code_worker.py`) pulls the
job, reads the notebook (code + the candidate's **saved outputs** + any charts),
sends it to **OpenAI** with the matching **day-wise rubric**, and gets back a
**Pass/Fail verdict + score + detailed analysis**. It then writes a result JSON
into the same S3 folder, tags the source file, and (optionally) pushes the result
to **Salesforce** — using the **same result schema and Apex endpoint** as the
existing video/image pipeline. **Candidate code is NOT executed** (the notebook
already contains its outputs), so there is no sandbox, no Docker, and no dataset
download.

---

## 2. Where it fits

```
candidate-deliverables (S3)
        │  one ObjectCreated notification
        ▼
   Router Lambda ──► transcript-jobs ──► transcript-service   (existing, unchanged)
        │        └─► llm-jobs        ──► llm-service          (existing, unchanged)
        └──────────► notebook-jobs   ──► code-service          ◄── THIS NEW SERVICE
                        (+ notebook-jobs-dlq)                       (code_worker.py)
                                                                         │
                                              result JSON → S3  +  (optional) Salesforce
```

A code file is routed to `notebook-jobs` **only** when its S3 path is under a
`trainingSteps/` folder AND a parent folder contains `Coding Assignment` AND the
extension is `.ipynb/.py/.pyw/.ipy`. (This logic lives in the Router Lambda,
already deployed.)

---

## 3. Already done (by dev — no action needed)

- ✅ `notebook-jobs` queue + `notebook-jobs-dlq` (maxReceiveCount 5) created.
- ✅ Router Lambda routes code submissions to `notebook-jobs` (single S3 notification).
- ✅ Router Lambda has `sqs:SendMessage` on `notebook-jobs`.
- ✅ All service code committed: `code_worker.py`, `code_processor.py`,
  `code_config.py`, `code-service.service`, and the day rubrics in
  `advance training prompt/`.
- ✅ No new Python dependencies — the worker uses `boto3`/`requests`/`python-dotenv`,
  which `requirements.txt` and `requirements-llm.txt` already install.

---

## 4. What YOU need to do to deploy (checklist)

### 4.1 — IAM: let the EC2 role read the new queue **(REQUIRED)**

The EC2 instance role currently has SQS receive/delete on `transcript-jobs` and
`llm-jobs` only. Add the same for `notebook-jobs`. Attach this statement to the
**EC2 instance role** (region us-east-1, account 985100584614):

```json
{
  "Sid": "CodeWorkerNotebookJobs",
  "Effect": "Allow",
  "Action": [
    "sqs:ReceiveMessage",
    "sqs:DeleteMessage",
    "sqs:GetQueueAttributes"
  ],
  "Resource": "arn:aws:sqs:us-east-1:985100584614:notebook-jobs"
}
```

> S3 permissions are already sufficient — the worker reuses the existing
> `s3:GetObject` / `s3:PutObject` / `s3:DeleteObject` grants (results, tagging,
> dedup markers). No S3 change needed.

### 4.2 — `.env` on the EC2: add the queue URL **(REQUIRED)**

Edit `/home/ec2-user/candidate-analysis/.env` and add:

```bash
NOTEBOOK_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/985100584614/notebook-jobs

# --- optional; sensible defaults are used if omitted ---
# CODE_WORKER_THREADS=4
# CODE_SQS_VISIBILITY_TIMEOUT=1800          # 30 min; must exceed a grade's runtime
# OPENAI_CODE_MODEL=gpt-4o                   # falls back to OPENAI_LLM_MODEL, then gpt-5.5
# OPENAI_CODE_REASONING_EFFORT=medium        # none|low|medium|high|xhigh (reasoning models)
# CODE_MAX_TOTAL_CHARS=120000                # size caps for big notebooks
# CODE_MAX_IMAGES=10                         # max charts sent to the vision model
# CODE_DEDUP_ENABLED=true
# CODE_SF_ENABLED=true                        # Salesforce for code results (see §5)
```

> **Model note:** the grading model must be **vision-capable** (charts are sent as
> images). If `OPENAI_CODE_MODEL` is unset it inherits `OPENAI_LLM_MODEL` — the same
> model your image deliverables already use — so this is normally fine as-is.
>
> **Salesforce:** code results POST to the **same Apex endpoint and schema** as the
> video/image results, reusing the existing `SF_CLIENT_ID` / `SF_CLIENT_SECRET` /
> `SF_LOGIN_URL` / `SF_APEX_PATH`. Sending is **ON by default** (`CODE_SF_ENABLED`
> defaults true). **`SF_CLIENT_ID` + `SF_CLIENT_SECRET` MUST be present in the `.env`**
> or the service exits at startup. Set `CODE_SF_ENABLED=false` to disable. See §5.

### 4.3 — Passwordless sudo for the new service (so CI/CD can restart it)

The CI/CD deploy restarts services with `sudo`. Add the new service to the sudoers
drop-in (mirrors the existing entries). Append these lines to
`/etc/sudoers.d/candidate-analysis-deploy`:

```
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl restart code-service
ec2-user ALL=(ALL) NOPASSWD: /usr/bin/systemctl is-active code-service
```

(Then `sudo visudo -c` to validate the file.)

### 4.4 — Install and start the service **(REQUIRED, one-time)**

```bash
cd /home/ec2-user/candidate-analysis
git pull                       # get the new files (or let CI/CD deploy first)
sudo cp code-service.service /etc/systemd/system/code-service.service
sudo systemctl daemon-reload
sudo systemctl enable --now code-service
sudo systemctl status code-service        # should be: active (running)
```

### 4.5 — CI/CD: make future deploys manage the new service

In `.github/workflows/ci-cd.yml`, in the **deploy** job's `script:`, add the new
service alongside the existing two (no new `pip install` line is needed):

```bash
# in the "Installing systemd unit files" step:
sudo cp code-service.service /etc/systemd/system/code-service.service
# in the "Restarting services" step:
sudo systemctl restart code-service
# in the "Status" step:
sudo systemctl is-active code-service
```

*(Optional but recommended: add the new modules to the CI import smoke test:
`python -c "import code_config, code_processor, code_worker"`.)*

### 4.6 — Verify

```bash
journalctl -u code-service -f
```
On a clean start you should see:
```
[CODE-MAIN] bucket=candidate-deliverables queue=.../notebook-jobs
[CODE-MAIN] threads=4 model=<model> sf_enabled=False
[CODE-WORKER 1] started
...
```
Then upload one real coding assignment and watch for:
`[DL] …` → `[LLM] grading …` → `[DONE] …_result.json(Pass|Fail)(Attempt-1)`.
Confirm the result JSON appears in the S3 deliverable folder, and that the
`notebook-jobs` queue drains and the **DLQ stays empty**.

---

## 5. Salesforce is ON by default — verify the record id once

Code-Salesforce is enabled by default (`CODE_SF_ENABLED` defaults true), so results
POST to Salesforce as soon as the service runs with valid `SF_CLIENT_ID` /
`SF_CLIENT_SECRET`. **Strongly recommended:** confirm the record id once before
trusting it in production (a wrong `deliverableResultId` attaches to the wrong record):

1. *(Optional, safest)* temporarily set `CODE_SF_ENABLED=false`, run one real
   candidate, open the result JSON in S3, and confirm the verdict/score look right
   **and that `deliverableResultId` is the correct Salesforce record id** (see §7).
2. Set `CODE_SF_ENABLED=true` (or just leave it — it is the default) and restart.
   Code results POST to the same Apex endpoint as the video/image results.

---

## 6. Rollback (instant, no code revert)

```bash
sudo systemctl stop code-service        # stop grading; messages wait safely in notebook-jobs
```
Nothing else is affected — the transcript and LLM services keep running normally.
To also stop Salesforce writes without stopping grading, set `SF_ENABLED=false`
and restart.

---

## 7. ⚠️ One item to confirm before enabling Salesforce

The result is attached to a Salesforce record via **`deliverableResultId`**, which
the worker reads from the `(id)` in the S3 name — in this order: the **submission
file name** → the **Coding Assignment folder** → the **candidate root folder**.

Your video pipeline puts the DeliverableResult id (`a1U…`) in the **file name**. If
the coding-assignment `.ipynb` files carry that same `(a1U…)` id, this is
byte-identical to the proven flow. If they do **not**, the worker falls back to a
folder id, which may be a different Salesforce object.

**Action:** share one real S3 key of a coding submission so dev can confirm (and
pin, if needed) where the id sits. Keep Salesforce OFF until this is confirmed.

---

## 8. Files added / changed (all additive)

| File | Type | Note |
|------|------|------|
| `code_worker.py` | new | the service (SQS consumer → grade → S3 → Salesforce) |
| `code_processor.py` | new | reads notebook/script into model input |
| `code_config.py` | new | settings + Day→rubric mapping |
| `advance training prompt/day-1…day-6 + code-eval.txt` | new | the day-wise grading rubrics |
| `code-service.service` | new | systemd unit |
| `.env.example` | edited (additive) | documents `NOTEBOOK_QUEUE_URL` + code settings |

**Not touched:** `main.py`, `transcriber.py`, `llm_worker.py`, `llm_processor.py`,
`salesforce.py`, `llm_s3.py`, `engines.py`, the existing `prompts/`, and both
existing service units. The new worker only **imports and reuses** the existing
modules — it does not modify them.

---

## 9. Quick reference — result naming (mirrors the existing pipeline)

```
<deliverable>_<id>_result.json(Pass)(Attempt-1)     # the grade
<deliverable>_<id>_sf_log(Attempt-1).json           # Salesforce call log
<submission>.ipynb(Pass)(Attempt-1)                 # source, tagged after grading
_processed/<upload-id>                              # duplicate-event guard marker
```
All of these are ignored by the Router, so results never re-trigger the pipeline.
Attempts are `max(existing Attempt-N) + 1`; a genuine re-upload becomes Attempt-2,
-3, … even after a Pass. A re-delivered duplicate event does **not** create a new
attempt.
```
