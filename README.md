# Candidate Transcript Service

Event-driven transcription pipeline. When a candidate video lands in the S3
bucket, a Lambda enqueues a job to SQS, and this worker (running on EC2) pulls
the job, transcribes the video with the OpenAI Whisper API, and writes a
`<videoname>_transcripts.txt` file back into the same S3 folder.

```
S3 (video uploaded) ──▶ trigger Lambda ──▶ SQS (transcript-jobs) ──▶ EC2 worker ──▶ transcript .txt back in S3
```

## Files

| File | Where it runs | Purpose |
|------|---------------|---------|
| `main.py` | EC2 | Multi-threaded SQS worker — the thing you run |
| `transcriber.py` | EC2 | Audio extraction, chunking, Whisper + AssemblyAI calls, cleanup |
| `engines.py` | EC2 | Shared engine identifiers (W=Whisper, A=AssemblyAI) + filename tagging |
| `s3_store.py` | EC2 | S3 download / upload helpers |
| `config.py` | EC2 | Reads settings from environment / `.env` |
| `requirements.txt` | EC2 | Python dependencies |
| `setup_ec2.sh` | EC2 | One-shot installer (ffmpeg + venv + deps) |
| `transcript-service.service` | EC2 | systemd unit for always-on running |
| `trigger_lambda.py` | AWS Lambda | Deployed separately — S3 event → SQS |

> `trigger_lambda.py` is included for reference; it is deployed as a Lambda
> function, NOT run on the EC2.

## Setup on EC2 (Amazon Linux 2023)

```bash
# 1. clone your repo
git clone https://github.com/<your-org>/transcript-service.git
cd transcript-service

# 2. one-shot install (ffmpeg, venv, deps)
bash setup_ec2.sh

# 3. configure
cp .env.example .env
nano .env          # fill in TRANSCRIPT_QUEUE_URL and OPENAI_API_KEY

# 4. test run (foreground)
source venv/bin/activate
python main.py
```

A clean start prints:
```
[MAIN] bucket=candidate-deliverables
[MAIN] queue=https://sqs.us-east-1.amazonaws.com/.../transcript-jobs
[MAIN] threads=4  model=whisper-1
[WORKER 1] started
...
```

## Required AWS setup (one time)

1. **SQS queue** `transcript-jobs` (+ a dead-letter queue `transcript-jobs-dlq`,
   maxReceiveCount 3).
2. **Trigger Lambda** `transcript-enqueue-trigger` from `trigger_lambda.py`,
   with env var `TRANSCRIPT_QUEUE_URL`, and `sqs:SendMessage` permission on the queue.
3. **S3 event notification** on the bucket: All object create events → the Lambda.
4. **EC2 instance role** with: `sqs:ReceiveMessage`, `sqs:DeleteMessage`,
   `sqs:GetQueueAttributes`, `s3:GetObject`, `s3:PutObject`.

## Environment variables (`.env`)

| Var | Required | Default | Notes |
|-----|----------|---------|-------|
| `TRANSCRIPT_QUEUE_URL` | yes | — | SQS queue URL |
| `OPENAI_API_KEY` | yes | — | OpenAI key for Whisper |
| `AWS_REGION` | no | us-east-1 | |
| `BUCKET` | no | candidate-deliverables | |
| `WORKER_THREADS` | no | 4 | parallel videos |
| `CHUNK_SECONDS` | no | 540 | audio chunk length |
| `OPENAI_WHISPER_MODEL` | no | whisper-1 | |
| `LANGUAGE` | no | en | |

The EC2 uses its **instance IAM role** for AWS credentials — do not put AWS keys
in `.env`.

## Run as a service (always-on)

```bash
sudo cp transcript-service.service /etc/systemd/system/
# edit WorkingDirectory / paths in the unit if your clone isn't /opt/transcript-service
sudo systemctl daemon-reload
sudo systemctl enable --now transcript-service
sudo systemctl status transcript-service
journalctl -u transcript-service -f      # live logs
```

## How transcripts are named

Each video is transcribed by **two engines in parallel** on the same extracted
audio (see `engines.py`). **Only AssemblyAI is tagged** — Whisper keeps the
original filename untouched, so production is byte-identical to before. For a
video `Day1_HR_Jay.mp4`, two transcripts are written to the **same folder**:

```
Day1_HR_Jay_transcripts.txt      ← Whisper   (untagged; production: scored + Salesforce + S3)
Day1_HR_Jay(A)_transcripts.txt   ← AssemblyAI (A/B: scored + S3 only)
```

The skip-if-exists check is **per engine**, so re-running only redoes the
missing engine(s) (unless `FORCE_RETRANSCRIBE=true`). Set
`ASSEMBLYAI_API_KEY` to enable `(A)`; leave it blank to run Whisper only.
`TRANSCRIPTION_ENGINES` (default `W,A`) controls which engines run — Whisper is
always first so the production path is durable before AssemblyAI runs.
