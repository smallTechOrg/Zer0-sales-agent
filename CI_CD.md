# CI/CD Pipeline

## Overview

Automated test → deploy pipeline via GitHub Actions. Pushes to `staging` deploy to the shared staging VM. Pushes to `main` deploy to the dedicated production VM.

## Architecture

```
Push to staging ──▶ test ──▶ SSH deploy to staging-instance (port 5000)
Push to main   ──▶ test ──▶ SSH deploy to ai-agent-prod   (port 5000)
```

## Pipeline Stages

### 1. Test (`test` job)

Runs on every push to `main` or `staging`.

- **Service container**: PostgreSQL 16 on `localhost:5432` (DB: `test_chatdb`)
- **Python**: 3.12
- **Lint**: `ruff check .` (inside `code/`)
- **Tests**: `pytest test/ -v` (inside `code/`)

### 2. Deploy Staging (`deploy-staging` job)

Triggers only on pushes to `staging`. Requires the `test` job to pass.

1. Authenticates to GCP using the service account key
2. Sets `BRANCH_NAME=staging` in VM instance metadata (used by `config.py` → `get_db_name()` to select `staging_chat_db`)
3. Base64-encodes `GROQ_API_KEY`, `DATABASE_URL`, and `GROQ_MODEL_NAME` (see **GitHub Variables** below) and SSHs into the staging VM to run:
   - `git fetch --all && git reset --hard origin/staging`
   - `scripts/deploy.sh` (activates venv, installs deps, decodes the injected values into `.env`, restarts Flask on port 5000)
4. Verifies `GET /health` returns 200

### 3. Deploy Production (`deploy-production` job)

Triggers only on pushes to `main`. Requires the `test` job to pass.

Same as staging but targets `ai-agent-prod` VM and sets `BRANCH_NAME=main` (selects `prod_chat_db`).

## GitHub Variables Required

By decision, **all** pipeline config — including the GCP service account key — is stored as **GitHub Variables**, not Secrets. Configure these in **repo Settings → Secrets and variables → Actions → Variables**:

| Variable | Scope | Value |
|----------|-------------|-------|
| `GCP_SA_KEY` | repo-level | Service account JSON key with `compute.instanceAdmin.v1` role |
| `GCP_PROJECT` | repo-level | `ai-agent-boilerplate0` |
| `GCP_VM_INSTANCE_NAME_STAGING` | staging environment | `staging-instance` |
| `GCP_ZONE_STAGING` | staging environment | `us-central1-f` |
| `GCP_VM_INSTANCE_NAME_PROD` | production environment | `ai-agent-prod` |
| `GCP_ZONE_PROD` | production environment | VM zone for production |
| `GROQ_MODEL_NAME` | repo-level (shared) | e.g. `meta-llama/llama-4-scout-17b-16e-instruct` — same for both environments unless overridden |
| `GROQ_API_KEY` | staging **and** production environments, set separately | Groq API key |
| `DATABASE_URL` | staging **and** production environments, set separately | Base Postgres URL, e.g. `postgresql://user:pass@host:5432/` — `config.py` appends the branch-specific DB name at runtime |

GitHub Actions resolves `vars.NAME` using **environment variables first, repo-level variables as fallback** — so a repo-level `GROQ_MODEL_NAME` is picked up by both jobs automatically, but if you ever add an environment-level `GROQ_MODEL_NAME` under `staging` or `production`, that overrides the shared value for just that job. Use this to keep `GROQ_MODEL_NAME` shared while keeping per-environment values (`GROQ_API_KEY`, `DATABASE_URL`, VM name/zone) distinct per environment.

**Important — these are Variables, not Secrets**: GitHub does **not** encrypt them and does **not** mask them in Actions run logs. `GROQ_API_KEY`, `DATABASE_URL` (which embeds the DB password), and now **`GCP_SA_KEY` (the GCP service account private key)** will appear in plaintext wherever they're referenced/echoed, and anyone with read access to the repo can view them under Settings. Unlike the app config, `GCP_SA_KEY` is a live cloud credential — treat this as a real widening of blast radius, not just a logging inconvenience. This was an explicit, acknowledged choice — flagging the concrete consequence here so it's not a surprise later.

## GitHub Environments

Create two environments in **repo Settings → Environments**:

- **staging** — no protection rules needed. Add its own `GROQ_API_KEY` / `DATABASE_URL` environment variables.
- **production** — recommended: add required reviewers for manual approval gate before prod deploys. Add its own `GROQ_API_KEY` / `DATABASE_URL` environment variables (separate from staging).

## Secrets & Variables Architecture

App config used to live in GCP Secret Manager, fetched on the VM at deploy time. It now lives entirely in GitHub Variables instead:

- The `Deploy to <env> VM` step base64-encodes `GROQ_API_KEY` / `DATABASE_URL` / `GROQ_MODEL_NAME` and passes them as env vars on the `gcloud compute ssh --command=...` line (base64 avoids shell-quoting/injection issues if a value contains special characters — this is for safe transport, not for secrecy).
- `scripts/deploy.sh` decodes them and writes `code/.env` directly — no external secret store, no `get_env.py`, one less GCP IAM permission (`secretmanager.secretAccessor`) to manage.
- Trade-off (acknowledged, see callout above): these values are plaintext in GitHub (Settings UI and Actions logs), not in a dedicated secrets manager with rotation/audit history.

## VM Prerequisites

Each VM must have:

1. Repo cloned at `/home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate`
2. Python 3.12 virtualenv at `venv/`
3. SSH key added to GitHub for `git fetch` to work
4. `curl` installed (for health checks)

## Deploy Script

The existing `scripts/deploy.sh` handles the on-VM deployment:

1. Activates virtualenv
2. `pip install -r requirements.txt`
3. Decodes the `GROQ_API_KEY_B64` / `DATABASE_URL_B64` / `GROQ_MODEL_NAME_B64` env vars injected over SSH and writes `.env`
4. Kills any existing Flask process
5. Starts Flask on `0.0.0.0:5000` via `nohup`

## Rollback

To rollback, push the previous good commit to the target branch:

```bash
git revert HEAD && git push origin main
```

Or manually SSH into the VM and reset:

```bash
gcloud compute ssh VM_NAME --zone=ZONE --command="
  cd /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate &&
  sudo git reset --hard GOOD_COMMIT_SHA &&
  sudo ./scripts/deploy.sh
"
```
