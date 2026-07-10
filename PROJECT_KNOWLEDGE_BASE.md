# Candidate Deliverable Analysis — Complete Project Knowledge Base

> **Purpose of this document:** a single, self-contained reference that explains
> the ENTIRE project in depth — what it does, the architecture, every file, the
> end-to-end workflow, the data model, naming conventions, configuration, and
> deployment. Hand this file to any LLM (ChatGPT, Claude, etc.) and it will
> understand the whole system without needing the source code.

---

## 1. What This Project Is (One Paragraph)

This is an **automated candidate-evaluation pipeline** for **Techsara**, a company
that runs a multi-day training/assessment program for job candidates. Candidates
submit "deliverables" — recorded **videos** (mock interviews, explanations),
hand-drawn **diagrams/images** (system designs, team structures), and **text
documents** (job-description alignment). The system automatically:

1. **Transcribes** every submitted video into timestamped text (speech-to-text).
2. **Scores** each deliverable with an LLM (GPT) acting as a senior interviewer,
   producing a Pass/Fail verdict, a numeric score, reasoning, positives, and
   negatives — all as structured JSON.
3. **Rolls up** the scores into per-day overall results.
4. **Pushes** the production results into **Salesforce** (the company CRM) so
   recruiters see them.

It runs **24/7 on AWS**, is **fully event-driven** (work starts the instant a file
is uploaded), retries failures automatically, and is engineered so an ongoing
**A/B experiment** (comparing two speech-to-text engines) can never disturb the
real production results.

---

## 2. Business Context / Domain Model

- **Candidate**: a person going through Techsara's assessment. Each candidate has
  a top-level folder in S3, e.g. `Jay Thakkar(001...)/`.
- **Training / Training Steps / Day**: the program is organized into days
  (Day 1 … Day 6), each with a set of expected deliverables. This structure is
  described in a `metadata.json` file at the candidate's root folder.
- **Deliverable**: one unit of work the candidate submits and that gets scored.
  Types:
  - **Video deliverable** — scored from its transcript (e.g. HR mock interview).
  - **Image deliverable** — a diagram scored via LLM vision (e.g. system-design
    architecture, team-structure diagram).
  - **Text deliverable** — a document scored as text (e.g. job-description text).
  - **Combined deliverable** — a video that ALSO pulls in a sibling image/text
    (e.g. a spoken system-design that is judged against the drawn diagram).
- **Result**: the JSON verdict for one deliverable
  (`{score, result, reasoning, positives, negatives, ...}`).
- **Day Overall**: the averaged/combined result for all deliverables in a day.
- **Salesforce record id**: an ID (15–18 alphanumeric chars) embedded in
  parentheses inside the uploaded file's name, e.g.
  `HR Questions Recording-(a1UO1000002Rd0vMAC).mp4`. This id links a result back
  to the correct Salesforce record.

### The 6 days and their deliverables (from `llm_config.py` routing rules)

| Day | Deliverable (folder contains…) | Prompt used | Extra inputs |
|-----|--------------------------------|-------------|--------------|
| 1 | `hr questions` | `mock-prompt.txt` | `31-Questions.pdf` + resume |
| 1 | `niche fundamentals` | `niche-prompt.txt` | `Niche-Questions.pdf` + resume |
| 2 | `project scenario` | `project-scenario.txt` | resume |
| 3 | `introduction and career flow` | `intro-prompt.txt` | resume |
| 3 | `tools and system explanation` | `Tools-Technology-prompt.txt` | resume |
| 3 | `team structure diagram` | `Tools-Technology-prompt.txt` | resume + own image |
| 3 | `team structure video` | `Tools-Technology-prompt.txt` | resume + sibling image |
| 3 | `resume-based mock interview` | `CV-prompt.txt` | resume |
| 4 | `recruiter persona` | `persona.txt` | resume |
| 4 | `hiring manager persona` | `persona.txt` | resume |
| 4 | `architect persona` | `persona.txt` | resume |
| 5 | `job description alignment 1 text` | `JD-prompt.txt` | resume + own text |
| 5 | `job description alignment 2 text` | `JD-prompt.txt` | resume + own text |
| 5 | `job description alignment 1 image` | `JD-prompt.txt` | resume + own image |
| 5 | `job description alignment 2 image` | `JD-prompt.txt` | resume + own image |
| 5 | `job description alignment` (video) | `JD-prompt.txt` | resume + sibling text + sibling image |
| 5 | `small talk` | `smalltalk.txt` | resume |
| 6 | `system design problem 2` (diagram) | `System-design.txt` | resume + own image |
| 6 | `system design problem 1` (video) | `System-design.txt` | resume + day image (sibling diagram) |
| 6 | `system design` (fallback) | `System-design.txt` | resume + own image + sibling image |

**Rule matching** (`match_rule`): the deliverable folder name is lowercased,
underscores→spaces, repeated spaces collapsed; then the FIRST rule whose "needle"
is a substring of the name wins. Order is most-specific-first (e.g. the standalone
`team structure diagram` rule must come before `team structure video`).

---

## 3. High-Level Architecture

The system is **two independent event-driven stages**, both following the same
pattern: an S3 upload fires a Lambda (a "doorbell"), the Lambda enqueues a job to
SQS (a "to-do list"), and a multi-threaded worker on EC2 (the "factory") pulls the
job and does the heavy work, writing results back to S3.

```
                          ┌─────────────────────────── AWS ───────────────────────────┐
Candidate uploads         │                                                            │
video / image / text ────▶│  S3 bucket: candidate-deliverables                         │
                          │        │                    │                              │
                          │  (event)│              (event)│                             │
                          │        ▼                    ▼                              │
                          │  Lambda:                Lambda:                            │
                          │  transcript-            llm-enqueue-trigger                │
                          │  enqueue-trigger        (llm_trigger_lambda.py)            │
                          │  (trigger_lambda.py)         │                             │
                          │        │                     │                             │
                          │        ▼                     ▼                             │
                          │  SQS: transcript-jobs   SQS: llm-jobs                      │
                          │   (+ DLQ ...-dlq)        (+ DLQ ...-dlq)                    │
                          │        │                     │                             │
                          │        ▼                     ▼                             │
                          │  EC2 service:           EC2 service:                       │
                          │  transcript-service     llm-service                        │
                          │  (main.py +             (llm_worker.py + llm_processor,    │
                          │   transcriber.py)        llm_s3, llm_overall, salesforce)  │
                          │        │                     │                             │
                          │        ▼                     ▼                             │
                          │  writes *_transcripts   writes *_result.json,             │
                          │  .txt back to S3 ───────▶ DayOVERALL_result.json to S3     │
                          └──────────────────────────────┼─────────────────────────────┘
                                                          │ (Whisper results only)
                                                          ▼
                                                  Salesforce (Apex REST endpoint)
```

External (non-AWS) services: **OpenAI** (Whisper transcription + GPT scoring),
**AssemblyAI** (second transcription engine, A/B), **Salesforce** (CRM sink),
**GitHub Actions** (CI/CD deployer).

---

## 4. AWS Components (exact names + roles)

Region: **`us-east-1`**. Account: **`985100584614`**.

| Component | Exact name | What it does |
|-----------|-----------|--------------|
| **S3 bucket** | `candidate-deliverables` | Single storage for everything: videos, transcripts, images, docs, resumes, `metadata.json`, and all result/overall/sf-log JSON files. Has TWO S3 event notifications (one per Lambda). |
| **SQS queue** | `transcript-jobs` | Holds "transcribe this video" jobs. Body: `{bucket, video_key}`. |
| **SQS DLQ** | `transcript-jobs-dlq` | Dead-letter queue; catches transcript jobs that fail 3× (`maxReceiveCount 3`). |
| **SQS queue** | `llm-jobs` | Holds "score this deliverable" jobs. Body: `{bucket, key, kind}`. |
| **SQS DLQ** | `llm-jobs-dlq` | Dead-letter queue for scoring jobs that fail 3×. |
| **Lambda** | `transcript-enqueue-trigger` | Source `trigger_lambda.py`. Fires on any S3 object-create; enqueues only real video files. Env: `TRANSCRIPT_QUEUE_URL`. Perm: `sqs:SendMessage`. |
| **Lambda** | `llm-enqueue-trigger` | Source `llm_trigger_lambda.py`. Fires on object-create; enqueues transcript/image/text jobs, ignores videos and results. Env: `LLM_QUEUE_URL`. Perm: `sqs:SendMessage`. |
| **EC2 instance** | (one, Amazon Linux 2023) | Runs BOTH workers 24/7 from `/home/ec2-user/candidate-analysis`. |
| **systemd unit** | `transcript-service` | Runs `main.py` (always-on, auto-restart). |
| **systemd unit** | `llm-service` | Runs `llm_worker.py` (always-on, auto-restart). |
| **IAM (EC2 role)** | instance profile | Needs `sqs:ReceiveMessage`, `sqs:DeleteMessage`, `sqs:GetQueueAttributes` (both queues); `s3:GetObject`, `s3:PutObject`, **`s3:DeleteObject`** (rename = copy+delete for tagging). |
| **IAM (Lambda roles)** | execution roles | `sqs:SendMessage` on their queue + basic logging. |
| **Security Group** | on the EC2 | Inbound SSH (port 22) so GitHub Actions can deploy. |

Queue URLs:
- `https://sqs.us-east-1.amazonaws.com/985100584614/transcript-jobs`
- `https://sqs.us-east-1.amazonaws.com/985100584614/llm-jobs`

**Why a queue at all?** Transcription and scoring are slow and can fail. SQS makes
work durable: on a transient error the worker does NOT delete the message, so SQS
redelivers it after the visibility timeout; after 3 receives it goes to the DLQ.
On a permanent error (bad/silent video) the worker DELETES the message so it never
retries forever.

---

## 5. The A/B Transcription-Engine Design (critical concept)

Every video is transcribed by **two engines in parallel on the exact same
extracted audio**:

| Engine | Code id | Filename tag | Scored? | Salesforce? | S3? |
|--------|---------|--------------|---------|-------------|-----|
| **Whisper** (OpenAI) | `W` | *(none — untagged)* | yes | **yes (production)** | yes |
| **AssemblyAI** | `A` | `(A)` | yes | **no** | yes |

Key design guarantees (see `engines.py`):

- **Only AssemblyAI is tagged.** Whisper keeps the ORIGINAL filenames untouched,
  so the production path (files + Salesforce payload) is **byte-identical** to how
  it worked before AssemblyAI existed. Mental model: **"no tag = Whisper,
  `(A)` = AssemblyAI."**
- The tag lives in the **filename only** — folders, prefixes, and `metadata.json`
  are never changed.
- **Whisper always runs first**, so the production transcript is durably written
  before the experimental engine even starts.
- If `ASSEMBLYAI_API_KEY` is not set, the `(A)` path is skipped entirely and the
  system runs Whisper-only (zero new files) — instant, safe rollback.
- A **video** → two transcripts → two GENUINE, independent scorings (different
  text can yield a different verdict per engine).
- An **image/text** deliverable → scored ONCE, then saved as two identical
  engine-tagged copies (no second LLM cost).
- **Attempt numbers, folder tagging, and day rollups are engine-scoped** and never
  mix. `(W)` rolls up only `(W)`; `(A)` only `(A)`.

Filename comparison:

| Artifact | Whisper (untagged, production) | AssemblyAI (S3-only) |
|----------|-------------------------------|----------------------|
| transcript | `…_transcripts.txt` | `…(A)_transcripts.txt` |
| result | `…_result.json(Pass)(Attempt-1)` | `…_result(A).json(Pass)(Attempt-1)` |
| day overall | `DayOVERALL_result.json` | `DayOVERALL(A)_result.json` |
| sf log | `…_sf_log(Attempt-N).json` | `…_sf_log(A)(Attempt-N).json` |

---

## 6. End-to-End Workflow (step by step)

### Stage 1 — Transcription

1. Candidate uploads `Day1_HR_Jay.mp4` into the deliverable folder in S3.
2. S3 event → **`transcript-enqueue-trigger`** Lambda (`trigger_lambda.py`).
   - It checks the extension is a video (`.mp4/.mov/.avi/.mkv/.webm/.m4v/.mpeg/.mpg`).
   - If yes, it sends `{"bucket", "video_key"}` to **`transcript-jobs`**.
3. **`transcript-service`** (`main.py`) — 4 worker threads long-poll the queue.
   For each message (`_handle_message`):
   a. Compute which engines still need a transcript (`_pending_engines`) — skips an
      engine whose transcript already exists (per-engine skip), skips AssemblyAI if
      no key. If none pending → skip.
   b. If the S3 object is empty → raise `NonRetryableTranscriptionError` (deleted,
      no retry).
   c. Download the video to a temp dir.
   d. `prepare_audio` extracts a **mono 16 kHz MP3 once** via ffmpeg. If the video
      has NO audio stream → returns `None` → each engine gets an empty transcript
      (→ later a deterministic 0/FAIL).
   e. For each pending engine, `transcribe_engine` runs Whisper (chunked) or
      AssemblyAI (upload+poll) on the shared MP3.
   f. Upload `<stem><tag>_transcripts.txt` next to the video in S3.
   g. On success, delete the SQS message.
4. The new transcript object lands in S3 → fires the SECOND Lambda.

### Stage 2 — Scoring

5. S3 event (on the `.txt` transcript, or on a `.png`/`.txt` upload) →
   **`llm-enqueue-trigger`** Lambda (`llm_trigger_lambda.py`).
   - Decides `kind`: `_transcripts.txt`→`transcript`, image→`image`, other
     `.txt`→`text`. Ignores videos and result files.
   - Sends `{"bucket", "key", "kind"}` to **`llm-jobs`**.
6. **`llm-service`** (`llm_worker.py`) — 4 threads long-poll. For each message
   (`_handle`):
   a. Resolve the deliverable folder + name; skip combined-input-only folders;
      skip already-tagged files (earlier attempts).
   b. For `image`/`text` kinds, only score standalone if the rule asks for
      `own_image`/`own_text`; otherwise it's a combined INPUT pulled by a sibling
      and is skipped.
   c. `transcript` → `_handle_transcript`: detect engine from the filename tag,
      compute the attempt number, gather inputs + score, finalize.
   d. `image`/`text` → `_handle_image_text`: score once, then write identical
      copies for each configured engine (shared attempt number).
   e. `_gather_and_score` builds the LLM input: transcript + resume PDF text +
      reference PDF text + sibling/own image (vision) + sibling/own text; loads the
      right prompt; calls OpenAI; returns the parsed result dict. Empty/short
      transcript (<20 chars) → deterministic 0/FAIL "no usable speech" result
      (no LLM call).
   f. `_finalize_engine_result` writes `<name>_<id>_result<tag>.json(Pass|Fail)(Attempt-N)`,
      tags the other files in the folder with the same marker (engine-scoped),
      pushes to Salesforce (Whisper only), writes an `_sf_log…json`, and refreshes
      that engine's day overall under a per-(day,engine) lock.
7. `_maybe_write_overall`: gather the latest same-engine result per deliverable in
   the day; read `metadata.json` to learn how many are expected; if complete, write
   `DayOVERALL<tag>_result.json` = average score, PASS only if all passed.
8. Salesforce (`salesforce.py`) receives the full Whisper result JSON via an Apex
   REST endpoint (OAuth client-credentials → Bearer POST).

---

## 7. File-by-File Reference

### 7.1 Shared / cross-stage

**`engines.py`** — Single source of truth for engine identifiers, shared by BOTH
stages so they can never drift.
- Constants: `WHISPER="W"`, `ASSEMBLYAI="A"`, `SF_ENGINE=W` (the only engine pushed
  to Salesforce), `DEFAULT_ENGINE=W` (assumed for legacy untagged transcripts).
- `configured_engines()` — reads `TRANSCRIPTION_ENGINES` (default `"W,A"`); always
  forces Whisper first, even if the env var omits or misconfigures it.
- `engine_tag(engine)` — `"W"→""`, `"A"→"(A)"` (Whisper untagged).
- `split_engine_tag(stem)` — a stem ending in `(A)` → `("A", stem_without_tag)`;
  anything else → `(None, stem)`. Careful not to confuse the `(A)` tag with a
  Salesforce id in parentheses (ids are 15–18 chars ending like `…vMAC)`).

### 7.2 Stage 1 — Transcription service (runs on EC2)

**`main.py`** — The transcript worker you run (`transcript-service`).
- Multi-threaded SQS consumer (`WORKER_THREADS`, default 4). Long-polls
  `transcript-jobs`.
- `_pending_engines` — per-engine skip logic (don't redo an engine whose transcript
  exists; skip AssemblyAI with no key).
- `_handle_message` — orchestrates: skip-if-done, empty-object guard, download,
  `prepare_audio` (extract MP3 once), then per-engine transcribe + upload.
- `_worker_loop` — receive → handle → delete on success; keep message (retry) on
  transient error; delete on `NonRetryableTranscriptionError`. Recreates the OpenAI
  client on error.
- Graceful shutdown via SIGINT/SIGTERM (`_stop` event).
- Uses: `boto3` (SQS + S3), `config.SETTINGS`, `s3_store`, `transcriber`, `engines`.

**`transcriber.py`** — Transcription core (ffmpeg + Whisper + AssemblyAI).
- `ensure_ffmpeg` / `_ffprobe_path` — verify ffmpeg/ffprobe present.
- `extract_audio_mp3` — ffmpeg to mono 16 kHz MP3, with a denoise filter
  (`highpass=f=80,afftdn=nf=-25`) and a fallback without the filter.
- `has_audio_stream` — ffprobe check; unreadable video → `NonRetryableTranscriptionError`.
- `prepare_audio` — extract MP3 ONCE (shared by both engines); returns `None` if no
  audio track (→ empty transcript → 0/FAIL downstream).
- `split_audio` — re-encode into overlapping chunks (`CHUNK_SECONDS`=180,
  `OVERLAP_SECONDS`=2). Small chunks limit damage from decoder loops.
- Whisper path: `transcribe_chunk` (POST to `…/audio/transcriptions`,
  `verbose_json`, temperature 0, retries with backoff, 25 MB file guard),
  `_parse_verbose_json` (drops decoder-hallucination segments via `no_speech_prob`
  > 0.8 and `compression_ratio` > 2.4), `transcribe_whisper_from_mp3` (merge chunks,
  trim overlap, `_collapse_repeated_segments` to kill decoder loops,
  `_deduplicate_segments`), output as `M:SS: text` lines.
- **VERBATIM MODE** (`VERBATIM_MODE=true`): keeps fillers/false starts/repetitions
  so candidate mistakes stay visible; only machine artifacts are removed.
- AssemblyAI path: `transcribe_assemblyai_from_mp3` — upload the SAME MP3
  (`/v2/upload`), create transcript with pinned config (`disfluencies`,
  `speech_models` priority list `universal-3-pro,universal-2`, `language_code`),
  `_aai_poll` until completed/error, `_aai_segments` (prefer `/sentences`, fall
  back to word grouping), format to the same `M:SS: text` lines.
- `transcribe_engine` — dispatch `W`→Whisper, `A`→AssemblyAI over the shared MP3.
- Retry policy: `_RETRYABLE_STATUSES={408,409,429,500,502,503,504}`,
  `_NON_RETRYABLE_STATUSES={400,401,403,404,413,415}`, 5 attempts, jittered backoff.

**`s3_store.py`** — S3 helpers for the transcript stage.
- `build_s3_client` (s3v4, 8 retries), `object_size`, `object_exists`
  (head_object; 404→False), `download_file`, `upload_text`, `prefix_of`,
  `sibling_key` (a filename in the same prefix as the video).

**`config.py`** — Settings for the transcript stage (frozen dataclass `SETTINGS`,
read from env / `.env`). Key fields: `aws_region`, `bucket`,
`transcript_queue_url`, SQS tuning (`sqs_wait_seconds`=20,
`sqs_visibility_timeout`=1800, `worker_threads`=4), OpenAI/Whisper
(`openai_api_key`, `openai_whisper_model`=whisper-1, `language`=en,
`openai_audio_bitrate`=64k), AssemblyAI (`assemblyai_api_key`,
`assemblyai_speech_models`, `assemblyai_disfluencies`=True, poll settings),
`transcription_engines`="W,A", chunking (`chunk_seconds`=180, `overlap_seconds`=2),
`verbatim_mode`=True. `validate()` requires `TRANSCRIPT_QUEUE_URL` + `OPENAI_API_KEY`.

**`trigger_lambda.py`** — The transcript Lambda (`transcript-enqueue-trigger`).
Deployed separately as an AWS Lambda (Python 3.12), NOT run on EC2. On S3
object-create, enqueues a `{bucket, video_key}` job to `transcript-jobs` for real
video files only. Env: `TRANSCRIPT_QUEUE_URL`.

### 7.3 Stage 2 — LLM scoring service (runs on EC2)

**`llm_worker.py`** — The scoring worker (`llm-service`). Multi-threaded SQS
consumer of `llm-jobs`.
- `_engine_result_suffix(engine)` — `_result.json` (W) / `_result(A).json` (A).
- `_gather_and_score` — matches the rule, reads the transcript (0/FAIL if empty),
  and for each `extra` gathers: `resume` (find + extract resume PDF),
  `pdf:NAME` (reference PDF from local `pdf/`), `sibling_image`/`sibling_text`
  (companion folder), `day_image` (newest image elsewhere in the day, excluding own
  folder — used by System Design Problem 1 video → Problem 2 diagram),
  `own_image`/`own_text` (this folder's content). Calls `llm_processor.evaluate`.
- `_find_sibling` — pairs a video with its companion image/text folder by matching
  base words AND exact deliverable number (so "Problem 1" never pulls "Problem 2").
- `_attempt_number` — engine-scoped count of existing result files + 1.
- `_finalize_engine_result` — writes the labeled result file, tags the other files
  in the folder (engine-scoped via `only_tag='(A)'` for AssemblyAI or
  `exclude_tag='(A)'` for Whisper), pushes to Salesforce (Whisper only) and writes
  an sf_log, then refreshes the day overall under a per-(day,engine) lock.
- `_maybe_write_overall` — collect latest same-engine result per deliverable; read
  `metadata.json` for expected count; if complete, write `DayOVERALL<tag>_result.json`.
- `_refresh_candidate_overall` — candidate-level rollup EXISTS but is DISABLED
  (commented out).
- `_handle_transcript` / `_handle_image_text` / `_handle` — message routing
  (see workflow above).
- `_sf_id_from_name` — extract the Salesforce id `(a1U…)` from a filename.

**`llm_processor.py`** — Builds the LLM request and calls OpenAI.
- `load_prompt`, `extract_pdf_text` (pdfplumber, fallback pypdf),
  `image_to_data_url` (base64 data URL for vision), `_clean_json` (strips code
  fences, extracts the JSON object, normalizes `score`/`result`/`reasoning`/
  `positives`/`negatives`).
- `evaluate` — assembles the user message (deliverable name, reference baseline,
  resume, supporting text, transcript, images), sends `response_format=json_object`
  to `…/chat/completions`. For reasoning models (`gpt-5*`, `o1/o3/o4`, non-`chat`)
  it sends `reasoning_effort` instead of `temperature`; otherwise `temperature=0`.
  Retries on `{408,409,429,500,502,503,504}`, 5 attempts.

**`llm_s3.py`** — S3 helpers for the scoring stage.
- `build_s3`, `read_text`, `list_prefix`, `list_objects` (with LastModified for
  newest-first), `is_tagged`, `tag_folder_files` (append a `(Pass)…`/`(Fail)…`
  marker to untagged files via copy+delete — engine-scoped with `only_tag`/
  `exclude_tag`), `download`, `prefix_of`, `parent_prefix`,
  `deliverable_name_from_prefix`, `training_steps_prefix`, `find_resume_pdf`
  (under `trainingSteps/resume pdf/`), `find_first_image` / `find_first_text`
  (newest, prefer untagged, fall back to tagged; `exclude_prefix` for sibling
  pulls).

**`llm_overall.py`** — Day/candidate roll-up math.
- `read_metadata` (candidate root `metadata.json`),
  `expected_deliverables_for_day` (which deliverables a day should have),
  `combine` (overallScore = rounded average; result = PASS only if ALL passed;
  dedup positives/negatives, capped).

**`llm_config.py`** — Scoring-stage settings + the deliverable→prompt routing table.
- `LLM_SETTINGS` (frozen dataclass): `llm_queue_url`, `openai_model`
  (default `gpt-5.5`), `openai_reasoning_effort` (default `low`),
  `result_suffix`=_result.json, `pass_marker`=(Pass), `fail_marker`=(Fail),
  `prompts_dir`=prompts, `pdf_dir`=pdf, and Salesforce config (`sf_enabled`,
  `sf_login_url`, `sf_apex_path`, `sf_client_id`, `sf_client_secret`, `sf_timeout`).
  `validate()` requires `LLM_QUEUE_URL` + `OPENAI_API_KEY` (+ SF creds if enabled).
- `DELIVERABLE_RULES` — the routing table (see §2). `match_rule` (normalized
  substring, first match wins). `COMBINED_INPUT_MARKERS`=[] (image/JD-text are now
  scored standalone). `is_combined_input_only`.

**`salesforce.py`** — Salesforce callout (Whisper results only).
- `_get_token` — OAuth `client_credentials` POST → cached `access_token` +
  `instance_url` (invalidated on 401).
- `notify` — POSTs the FULL result JSON to the Apex REST endpoint with a Bearer
  token; retries on 401 (refresh token) / 429 / 5xx; NEVER raises (Salesforce being
  down must not kill analysis); returns a log dict saved next to the result in S3.

**`llm_trigger_lambda.py`** — The scoring Lambda (`llm-enqueue-trigger`). On S3
object-create, decides `kind` (transcript/image/text) and enqueues to `llm-jobs`.
Ignores videos, result files, and anything containing `overall`. Env: `LLM_QUEUE_URL`
(optional `TRANSCRIPT_SUFFIX`, `RESULT_SUFFIX`).

### 7.4 Prompts and reference PDFs

**`prompts/`** — 10 detailed evaluation prompts (each 11k–23k chars). Each defines a
role (e.g. "Senior Hiring Manager"), the inputs it will receive, the rubric, and
strict rules (be evidence-based, cite `M:SS` timestamps as proof, do NOT penalize
accents/mispronunciation/transcription artifacts, respect confidential info like
salary). Every prompt returns strict JSON: `{score, result, reasoning, positives,
negatives}`. Files: `mock-prompt.txt`, `niche-prompt.txt`, `project-scenario.txt`,
`intro-prompt.txt`, `Tools-Technology-prompt.txt`, `CV-prompt.txt`, `persona.txt`,
`JD-prompt.txt`, `smalltalk.txt`, `System-design.txt`.

**`pdf/`** — Reference "ideal answer" baselines fed to the model:
`31-Questions.pdf` (HR mock), `Niche-Questions.pdf` (niche fundamentals).

### 7.5 Infrastructure / deployment

**`setup_ec2.sh`** — One-shot EC2 bootstrap (Amazon Linux 2023): installs Python +
a static ffmpeg build, creates a `venv`, installs `requirements.txt`.

**`cicd_ec2_setup.sh`** — Run ONCE on EC2: writes a sudoers file so `ec2-user` can
`systemctl restart`/`is-active` the two services without a password (so the
GitHub Actions deploy doesn't hang), and marks the repo a git safe.directory.

**`transcript-service.service` / `llm-service.service`** — systemd units. Both run
from `/home/ec2-user/candidate-analysis`, load env from that folder's `.env`,
`Restart=always`, `RestartSec=5`. `ExecStart` runs `venv/bin/python main.py` and
`… llm_worker.py` respectively.

**`.github/workflows/ci-cd.yml`** — CI/CD pipeline.
- **CI** (on push AND PR): Python 3.12, `pip install` both requirements +
  `pyflakes`, `compileall` (syntax), pyflakes lint (non-blocking), and an import
  smoke test with dummy env vars.
- **CD** (only on push to `main`, only if CI passed): SSH to EC2
  (`appleboy/ssh-action`), `git fetch origin main` + `git reset --hard origin/main`
  (never aborts on server edits; leaves untracked `.env` alone), reinstall deps,
  copy the `.service` unit files into `/etc/systemd/system/`, `daemon-reload`,
  restart both services, verify `is-active`.
- Requires GitHub secrets: `EC2_HOST`, `EC2_USER` (`ec2-user`), `EC2_SSH_KEY`
  (private deploy key).

**`requirements.txt`** (transcript stage): `boto3`, `requests`, `python-dotenv`,
`assemblyai`.
**`requirements-llm.txt`** (scoring stage): `boto3`, `requests`, `python-dotenv`,
`pdfplumber`, `pypdf`.

**`.gitignore`** — ignores `.env` (secrets), `__pycache__`, `venv/`, ffmpeg
downloads, `/image`.

**`.env.example`** — template for all env vars (see §8). `.env` itself is never
committed; AWS creds come from the EC2 instance role, not the file.

### 7.6 Docs (human-facing, in the repo)

- `README.md` — transcript service overview + AWS setup.
- `README-LLM.md` — scoring stage overview + A/B naming.
- `DEPLOYMENT.md` — safe AssemblyAI rollout runbook ("deploy key-off first").
- `CICD-SETUP.md` — GitHub Actions → EC2 setup.
- `how-to-see.txt` — the two live-log commands
  (`journalctl -u transcript-service -f`, `journalctl -u llm-service -f`).
- **`PROJECT_KNOWLEDGE_BASE.md`** — this file.

---

## 8. Configuration (Environment Variables)

All config comes from environment variables (a `.env` file on the EC2 is loaded by
`python-dotenv`). Secrets are never committed.

### Transcript stage (`config.py`)
| Var | Default | Purpose |
|-----|---------|---------|
| `AWS_REGION` | `us-east-1` | AWS region |
| `BUCKET` | `candidate-deliverables` | S3 bucket |
| `TRANSCRIPT_QUEUE_URL` | — (required) | SQS transcript queue |
| `OPENAI_API_KEY` | — (required) | OpenAI key (Whisper) |
| `OPENAI_WHISPER_MODEL` | `whisper-1` | Whisper model |
| `LANGUAGE` | `en` | Transcription language |
| `OPENAI_AUDIO_BITRATE` | `64k` | MP3 bitrate |
| `WORKER_THREADS` | `4` | Parallel videos |
| `SQS_WAIT_SECONDS` | `20` | Long-poll |
| `SQS_VISIBILITY_TIMEOUT` | `1800` | 30-min visibility |
| `CHUNK_SECONDS` | `180` | Audio chunk length |
| `OVERLAP_SECONDS` | `2` | Chunk overlap |
| `TRANSCRIPT_SUFFIX` | `_transcripts.txt` | Transcript filename suffix |
| `FORCE_RETRANSCRIBE` | `false` | Ignore skip-if-exists |
| `VERBATIM_MODE` | `true` | Keep fillers/false starts |
| `TRANSCRIPTION_ENGINES` | `W,A` | Which engines run (Whisper always forced) |
| `ASSEMBLYAI_API_KEY` | *(empty)* | Enables AssemblyAI; empty = Whisper-only |
| `ASSEMBLYAI_SPEECH_MODELS` | `universal-3-pro,universal-2` | Priority model list |
| `ASSEMBLYAI_DISFLUENCIES` | `true` | Keep fillers (verbatim) |
| `ASSEMBLYAI_POLL_SECONDS` | `3` | Poll interval |
| `ASSEMBLYAI_POLL_MAX_ATTEMPTS` | `600` | Poll cap |

### Scoring stage (`llm_config.py`)
| Var | Default | Purpose |
|-----|---------|---------|
| `LLM_QUEUE_URL` | — (required) | SQS llm queue |
| `OPENAI_API_KEY` | — (required) | OpenAI key (shared) |
| `OPENAI_LLM_MODEL` | `gpt-5.5` | Scoring model |
| `OPENAI_REASONING_EFFORT` | `low` | none/low/medium/high/xhigh (reasoning models) |
| `LLM_WORKER_THREADS` | `4` | Parallel scorings |
| `LLM_SQS_VISIBILITY_TIMEOUT` | `600` | 10-min visibility |
| `LLM_RESULT_SUFFIX` | `_result.json` | Result filename suffix |
| `LLM_PASS_MARKER` / `LLM_FAIL_MARKER` | `(Pass)` / `(Fail)` | Verdict tags |
| `PROMPTS_DIR` / `PDF_DIR` | `prompts` / `pdf` | Local asset folders |
| `SF_ENABLED` | `false` | Enable Salesforce push |
| `SF_LOGIN_URL` | `https://techsara--dev9.sandbox.my.salesforce.com` | SF login |
| `SF_APEX_PATH` | `/services/apexrest/v1/deliverable-result/` | Apex REST path |
| `SF_CLIENT_ID` / `SF_CLIENT_SECRET` | — | Connected-app creds (required if SF on) |
| `SF_TIMEOUT` | `30` | HTTP timeout |

---

## 9. S3 Layout & Naming Conventions (the data model on disk)

```
candidate-deliverables/                              (bucket)
└── Jay Thakkar(001...)/                              (candidate root)
    ├── metadata.json                                 (defines days + expected deliverables)
    └── .../trainingSteps/
        ├── resume pdf/<resume>.pdf                    (candidate resume)
        └── Day 1 - Foundations(a0z...)/               (a "day" folder)
            ├── HR Questions(a1U...)/                  (a deliverable folder)
            │   ├── HR...(a1UO...vMAC).mp4             (uploaded video)
            │   ├── HR..._transcripts.txt              (Whisper transcript)
            │   ├── HR...(A)_transcripts.txt           (AssemblyAI transcript)
            │   ├── HR..._<id>_result.json(Pass)(Attempt-1)      (Whisper result)
            │   ├── HR..._<id>_result(A).json(Pass)(Attempt-1)   (AssemblyAI result)
            │   ├── HR..._<id>_sf_log(Attempt-1).json            (Whisper SF log)
            │   └── HR..._<id>_sf_log(A)(Attempt-1).json         (AssemblyAI SF log)
            ├── DayOVERALL_result.json                 (Whisper day rollup)
            └── DayOVERALL(A)_result.json              (AssemblyAI day rollup)
```

Naming rules that make the pipeline safe:
- **Engine tag** goes BEFORE the extension: `_result.json` (W) vs `_result(A).json` (A).
- **Verdict + attempt tag** goes AFTER the extension:
  `_result.json(Pass)(Attempt-1)`. Because a tagged file no longer ends in a
  router-recognized suffix, the trigger Lambdas never re-process it (no infinite
  loops). The tag is also how the worker detects earlier attempts and skips them.
- **Attempt numbering** is per-engine: a re-submission becomes `(Attempt-2)`, etc.;
  the day rollup keeps the max-attempt result per deliverable folder.

---

## 10. Reliability, Idempotency & Error Handling

- **Idempotency / no double work:** per-engine skip-if-transcript-exists; scoring
  skips already-tagged files; the LLM trigger ignores results and `overall` files;
  tagged files match no router route.
- **Retries:** SQS redelivers on transient failure; API calls (Whisper/AssemblyAI/
  OpenAI chat/Salesforce) use bounded exponential backoff with jitter on
  retryable HTTP codes.
- **Poison messages:** permanent failures (empty/silent/unreadable video) raise
  `NonRetryableTranscriptionError` → the message is deleted so it doesn't loop; DLQs
  catch anything that fails 3×.
- **Muted video → graceful 0/FAIL:** no audio track or <20-char transcript yields a
  deterministic 0/FAIL with "check your microphone / re-record" guidance, delivered
  through the normal result→Salesforce flow (no wasted LLM call).
- **Concurrency safety:** per-(day, engine) locks so two same-engine results
  finishing at once don't both clobber the same `DayOVERALL` file; `(W)` and `(A)`
  rollups never block each other.
- **Salesforce never fatal:** `notify()` never raises; failures are logged to S3
  and the pipeline continues.
- **Production isolation:** the entire A/B mechanism is filename-only; Whisper's
  files and Salesforce payload are byte-identical to the pre-AssemblyAI pipeline;
  blanking the AssemblyAI key or setting `TRANSCRIPTION_ENGINES=W` reverts instantly.

---

## 11. External Integrations

- **OpenAI Whisper** — `POST https://api.openai.com/v1/audio/transcriptions`
  (`verbose_json`, temperature 0, per-chunk, 25 MB/chunk limit).
- **OpenAI Chat (GPT scoring)** — `POST https://api.openai.com/v1/chat/completions`
  (`response_format=json_object`; `reasoning_effort` for reasoning models, else
  `temperature=0`).
- **AssemblyAI** — `https://api.assemblyai.com/v2` (`/upload` → `/transcript`
  → poll `/transcript/{id}` → `/transcript/{id}/sentences`). Verbatim via
  `disfluencies`; model priority via `speech_models`.
- **Salesforce** — OAuth `client_credentials` at
  `{SF_LOGIN_URL}/services/oauth2/token`, then Bearer `POST {instance_url}{SF_APEX_PATH}`
  with the full result JSON to an Apex `@RestResource` (reads `deliverableResultId`
  and `result`).
- **GitHub Actions** — CI on push/PR; CD SSHes into EC2 on push to `main`.

---

## 12. Result JSON Shapes

**Per-deliverable result** (written by `_finalize_engine_result`):
```json
{
  "score": 78,
  "result": "PASS",
  "reasoning": "…",
  "positives": ["…"],
  "negatives": ["… | Proof: … | Suggestion: …"],
  "deliverable": "HR Questions",
  "deliverableResultId": "a1UO1000002Rd0vMAC",
  "attempt": 1,
  "video": "HR Questions Recording-(a1UO1000002Rd0vMAC)"
}
```

**Day overall** (written by `_maybe_write_overall` via `llm_overall.combine`):
```json
{
  "candidate": "Jay Thakkar(001...)",
  "day": "Day 1 - Foundations(a0z...)",
  "engine": "W",
  "overallScore": 74,
  "result": "PASS",
  "deliverables": [{"deliverable": "HR Questions", "score": 78, "result": "PASS"}],
  "reasoning": "Average score 74 across N deliverable(s); all passed.",
  "positives": ["…"],
  "negatives": ["…"]
}
```
Candidate-level overall is implemented (`_refresh_candidate_overall`) but currently
**disabled**.

---

## 13. Glossary

- **S3** — AWS object storage (the shared file store).
- **SQS** — AWS message queue (durable to-do list); **DLQ** = dead-letter queue.
- **Lambda** — small AWS function triggered by an S3 event (the "doorbell").
- **EC2** — AWS virtual server that runs the always-on workers.
- **systemd** — Linux service manager that keeps the workers running/restarting.
- **Whisper / AssemblyAI** — the two speech-to-text engines (A/B).
- **LLM / GPT** — the model that scores deliverables.
- **Deliverable** — one scored candidate submission.
- **Verbatim mode** — transcription that keeps fillers/mistakes for fair evaluation.
- **Engine tag** — `(A)` in a filename = AssemblyAI; no tag = Whisper.
- **Attempt** — a numbered re-submission of a deliverable, per engine.
- **Day Overall** — the combined score for a day's deliverables, per engine.
- **Salesforce record id** — the `(a1U…)` id in a filename linking a result to CRM.

---

*End of knowledge base. This document reflects the code in the repository at the
time of writing; if the code changes (rules in `llm_config.py`, defaults in
`config.py`/`llm_config.py`, or the CI/CD flow), update this file to match.*
