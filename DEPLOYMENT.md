# Deployment Runbook — AssemblyAI A/B Engine

How to safely roll out the AssemblyAI parallel transcription engine to EC2 via
GitHub Actions.

**What this change does:** every video is now transcribed by **two** engines on
the same audio. Whisper stays the production path (UNTAGGED filenames, pushed to
Salesforce — byte-identical to before). AssemblyAI is an A/B engine tagged `(A)`,
stored in S3 only, never pushed to Salesforce.

**Rollout philosophy — deploy "key-off" first.** AssemblyAI only runs when
`ASSEMBLYAI_API_KEY` is set. So deploy the code first with no key (production runs
exactly as today, zero `(A)` files), confirm it's healthy, then add the key to
turn AssemblyAI on. Whisper/Salesforce can never be affected by the `(A)` path.

---

## A. PRE-DEPLOYMENT (on your machine, before pushing)

### A1. Commit ALL changes — including the new file
`engines.py` is brand new. If it is not committed, the services will crash on
startup with `ModuleNotFoundError: engines`.
```bash
git add engines.py config.py transcriber.py main.py llm_config.py llm_worker.py \
        llm_s3.py requirements.txt .env.example transcript-service.service \
        .github/workflows/ci-cd.yml README.md README-LLM.md DEPLOYMENT.md
git status        # confirm engines.py is listed (as a new file), nothing missing
```

### A2. Confirm the EC2 git tree is clean
The auto-deploy runs `git pull`; a hand-edited **code** file on the server makes
it conflict and fail. (Editing `.env` is fine — it is gitignored.)
```bash
ssh ec2-user@<EC2_HOST> "cd /home/ec2-user/candidate-analysis && git status"
# must say: working tree clean
```

### A3. Confirm where the transcript service actually runs
The deploy pulls code into `/home/ec2-user/candidate-analysis`. The transcript
service MUST run from that same folder, or it will keep running old code.
```bash
ssh ec2-user@<EC2_HOST> "systemctl cat transcript-service | grep -E 'WorkingDirectory|ExecStart'"
# should point at /home/ec2-user/candidate-analysis
```
If it points somewhere else (e.g. `/opt/transcript-service`), fix that first —
either point the live unit at the deploy folder, or also pull code into that folder.

### A4. Decide rollout mode
Start **key-off** (recommended): deploy now, add the AssemblyAI key in step C4.

---

## B. DEPLOY (automatic on push to `main`)

### B1. Push
```bash
git commit -m "Add AssemblyAI as a parallel A/B transcription engine"
git push origin main
```

### B2. Watch the run
Repo → **Actions** tab. The pipeline runs CI (compile + import test) → CD (SSH to
EC2 → `git pull` → `pip install` (installs `assemblyai`) → restart both services).
Wait for the green check.

---

## C. POST-DEPLOYMENT (on the EC2)

### C1. (One-time, only if the `.service` file was changed) reinstall the unit
```bash
cd /home/ec2-user/candidate-analysis
sudo cp transcript-service.service /etc/systemd/system/
sudo systemctl daemon-reload
sudo systemctl restart transcript-service llm-service
```

### C2. Confirm both services are healthy
```bash
sudo systemctl is-active transcript-service llm-service     # both -> active
journalctl -u transcript-service -n 30 --no-pager
# key-off shows: "ASSEMBLYAI_API_KEY not set — running Whisper-only"
```

### C3. (Optional) run one normal candidate
Confirm production is unchanged — Whisper only, no new files. This proves the
refactor is clean before AssemblyAI is switched on.

### C4. Turn AssemblyAI ON — add the key to `.env`, then restart
Secrets are not in git, so this is a manual step on the server.
```bash
nano /home/ec2-user/candidate-analysis/.env
#   add this one line:    ASSEMBLYAI_API_KEY=your-real-key
sudo systemctl restart transcript-service llm-service
journalctl -u transcript-service -n 20 --no-pager
# should now show: "engines=W,A  assemblyai_models=universal-3-pro,universal-2"
```
> The model list (`universal-3-pro,universal-2`) and verbatim mode are already
> defaults — you only add the key. If the transcript service runs from a different
> folder, add the key to that folder's `.env` too.

### C5. Run ONE test candidate end-to-end, then verify in S3
The deliverable folder should contain:
- two transcripts — `…_transcripts.txt` AND `…(A)_transcripts.txt`
- two results — `…_result.json(...)` AND `…_result(A).json(...)`
- two day summaries — `DayOVERALL_result.json` AND `DayOVERALL(A)_result.json`
- **Salesforce → only the Whisper result**; all `(A)` files are S3-only.

### C6. Watch the logs for the two live-only checks
```bash
journalctl -u transcript-service -f       # look for [ASSEMBLYAI] lines
```
- `[ASSEMBLYAI] … HTTP 400` → the model/param combo needs a one-line `.env` tweak
  (affects `(A)` only — see Troubleshooting). Whisper/Salesforce stay safe.
- Check the **transcript-jobs DLQ** is not filling up.

---

## D. ROLLBACK (instant, no code revert)
If anything about `(A)` misbehaves:
```bash
nano /home/ec2-user/candidate-analysis/.env     # set TRANSCRIPTION_ENGINES=W  (or blank the key)
sudo systemctl restart transcript-service llm-service
```
→ back to pure Whisper immediately.

---

## E. Troubleshooting (AssemblyAI / `(A)` path only)

| Symptom in logs | Cause | Fix (in `.env`, then restart) |
|---|---|---|
| `[ASSEMBLYAI] … HTTP 400` mentioning the model | `universal-3-pro` not enabled on your account | `ASSEMBLYAI_SPEECH_MODELS=universal-2` (or blank to use AssemblyAI's default) |
| `[ASSEMBLYAI] … HTTP 400` re language | `language_code` + `speech_models` combo rejected | blank out `LANGUAGE=` to use auto-detect |
| `(A)` transcript has no fillers (um/uh) | disfluencies unsupported on that model | `ASSEMBLYAI_DISFLUENCIES=false`, or pin a model that supports it |
| transcript-jobs DLQ filling up | AssemblyAI failing repeatedly | roll back (section D); investigate key/quota |

None of these affect Whisper or Salesforce — they are all `(A)`-only.

---

## Settings reference (all live in `.env`, all optional except the key)

| Variable | Default | Purpose |
|---|---|---|
| `ASSEMBLYAI_API_KEY` | *(empty)* | Enables AssemblyAI. Empty = Whisper-only. |
| `TRANSCRIPTION_ENGINES` | `W,A` | Which engines run. Set `W` to disable AssemblyAI. Whisper is always forced. |
| `ASSEMBLYAI_SPEECH_MODELS` | `universal-3-pro,universal-2` | Priority list; AssemblyAI auto-falls-back down the list. |
| `ASSEMBLYAI_DISFLUENCIES` | `true` | Keep fillers/false starts (verbatim, matches Whisper). |

---

## The 3 steps people miss
1. **A1** — commit `engines.py` (the new file).
2. **C4** — add `ASSEMBLYAI_API_KEY` to the server's `.env` (the deploy can't carry secrets).
3. **C4** — restart after editing `.env` (settings are read only at startup).
