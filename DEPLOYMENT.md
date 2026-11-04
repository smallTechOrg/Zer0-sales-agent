# Deployment Guide

How the Zer0 sales agent is deployed, monitored and debugged.

## Table of Contents
1. [How Deployment Works](#how-deployment-works)
2. [One-time Setup](#one-time-setup)
3. [Periodic Summary Job](#periodic-summary-job)
4. [Moving Off the VMs](#moving-off-the-vms)
5. [Monitoring](#monitoring)
6. [Rollback](#rollback)
7. [Debugging](#debugging)
8. [Database Connections](#database-connections)

## How Deployment Works

**Stack**: Flask app in a container on **Cloud Run**, served by gunicorn,
talking to the shared PostgreSQL. GitHub Actions builds and deploys it.

| Branch | Cloud Run service | `APP_ENV` | Database | GitHub environment |
|---|---|---|---|---|
| `staging` | `zero-sales-agent-staging` | `staging` | `staging_chat_db` | `deploy-staging` |
| `main` | `zero-sales-agent` | `production` | `prod_chat_db` | `deploy-production` |

**Flow** (`.github/workflows/deploy.yml`), on every push to `staging` or `main`:

1. **test** - runs `pytest` against a throwaway PostgreSQL. A red test stops the deploy.
2. **build** - `docker build` from the root `Dockerfile`, pushed to Artifact
   Registry as `<region>-docker.pkg.dev/<project>/zero-sales-agent/zero-sales-agent:<commit sha>`.
3. **deploy** - `gcloud run deploy` of a new revision **with no traffic**, tagged `candidate`.
4. **smoke test** - `GET /health` on the candidate URL must return 200
   (app up *and* database reachable). If not, the job fails and users stay on
   the previous revision.
5. **promote** - all traffic moves to the new revision.
6. **schedule** - the Cloud Scheduler job for the periodic summary is created
   or updated to point at the service (see below).

The first deploy of a service has nothing to protect, so it goes straight to
100% traffic; steps 3 and 5 apply from the second deploy on.

You can re-run a deploy by hand: Actions → *Deploy to Cloud Run* → *Run workflow*, pick the branch.

**Configuration**

- **Secrets** come from Secret Manager, mounted as environment variables by
  Cloud Run - nothing is written to disk and there is no `.env` in the image:

  | Env var | Secret Manager secret |
  |---|---|
  | `GROQ_API_KEY` | `GROQ_API_KEY` |
  | `DATABASE_URL` | `POSTGRES_URL` |
  | `SLACK_WEBHOOK_URL` | `SLACK_WEBHOOK_URL` |

  A new secret version is picked up by the next deploy (or new instance).
- **Plain settings** (`APP_ENV`, `SCHEDULER_ENABLED=false`, the job auth
  settings, `GROQ_MODEL_NAME`, ...) are written by the *Write service
  environment* step of the workflow. Every setting and its default is
  declared in `code/config.py`; `code/.env.example` lists them.
- **Sizing**: 1 vCPU, 1 GiB, 8 concurrent requests per instance (= gunicorn's
  8 threads; `CLOUD_RUN_CONCURRENCY` to change it), 900s request timeout. Production keeps 1 warm instance (`min-instances=1`) so the
  chat widget never hits a cold start; staging scales to zero.
  `max-instances` is 3 (`CLOUD_RUN_MAX_INSTANCES` to change it): every
  instance opens up to `DB_POOL_MAX_SIZE` (2) connections, so the shared
  database sees at most 6 from each service.

**GitHub settings used by the workflow**

| Where | Name | Notes |
|---|---|---|
| Environment secret (both environments) | `GCP_PROJECT` | e.g. `ai-agent-boilerplate0` |
| Environment secret (both environments) | `GCP_SA_KEY` | JSON key of the deploy service account |
| Repository variable (optional) | `GCP_REGION` | default `us-central1` |
| Repository variable (optional) | `CLOUD_RUN_MAX_INSTANCES` | default `3` |
| Repository variable (optional) | `VPC_NETWORK`, `VPC_SUBNET` | set if the database is only reachable on a private IP |
| Repository variable (optional) | `PERIODIC_SUMMARY_CRON` | default `0 * * * *` (Asia/Kolkata) |
| Repository variable (optional) | `CLOUD_RUN_CONCURRENCY` | default `8`; keep it equal to gunicorn's `--threads` |
| Repository variable | `CLOUD_SCHEDULER_ENABLED` | the summary job runs only when this is `true`; paused otherwise. Set it once the VMs are stopped |
| Repository variable (optional) | `GROQ_MODEL_NAME` | default `openai/gpt-oss-120b` |

## One-time Setup

Run once per GCP project, as a project Owner:

```bash
PROJECT_ID=ai-agent-boilerplate0 REGION=us-central1 \
DEPLOY_SA=<email of the service account in GCP_SA_KEY> \
./scripts/gcp_setup.sh
```

It enables the Cloud Run, Artifact Registry, Cloud Scheduler and Secret
Manager APIs, creates the `zero-sales-agent` image repository, and creates two
service accounts:

- `zero-sales-agent-run@` - what the service runs as. Can read the three secrets, nothing else.
- `zero-scheduler@` - what Cloud Scheduler signs its calls as. Has no roles at all;
  the app trusts it by email.

and gives the deploy account `run.admin`, `artifactregistry.writer`,
`cloudscheduler.admin`, and *act as* on those two accounts. The script is
safe to run again.

Then in GitHub → Settings → Environments, create `deploy-production` (the old
workflow used `deploy-staging` for both) and give it `GCP_PROJECT` and
`GCP_SA_KEY`. Adding *required reviewers* there turns every production deploy
into a one-click approval.

### Database access

Cloud Run does not have the VM's fixed IP. The database must accept
connections from the service, in one of these ways:

- **Private IP (recommended)**: if the database is on a VM or Cloud SQL in the
  same VPC, set the repository variables `VPC_NETWORK` (and `VPC_SUBNET`).
  The workflow then turns on Direct VPC egress for private ranges, and
  `POSTGRES_URL` should use the private IP. Allow the subnet's range on 5432 in
  the firewall / `pg_hba.conf`.
- **Public IP**: the outbound IP of Cloud Run is not fixed. Either route egress
  through Cloud NAT with a reserved IP and allow-list that, or use Cloud SQL
  with the Cloud SQL connector.

If the database cannot be reached, the deploy fails at the smoke test and no
traffic moves.

## Periodic Summary Job

On a VM the summary ran on an in-process APScheduler timer. Cloud Run only
gives an instance CPU while it serves a request and can stop it at any time,
so that timer is not reliable there. Instead:

```
Cloud Scheduler ──(hourly, OIDC token)──▶ POST /jobs/periodic-summary ──▶ periodic_summary()
```

- `SCHEDULER_ENABLED=false` on Cloud Run turns the in-process timer off.
  Locally it is still on by default, so `python app.py` behaves as before.
- The job is `zero-sales-agent[-staging]-periodic-summary`, in the same
  region, cron `0 * * * *` in Asia/Kolkata. The workflow creates or updates it
  on every deploy, so the schedule lives in the repo, not in the console.
- **Paused by default**: the job runs only if the repository variable
  `CLOUD_SCHEDULER_ENABLED` is `true`. Each deploy pauses or resumes it to
  match, and fails if it cannot. Pausing or resuming by hand in the console
  is undone by the next deploy.
- **Auth**: the service is public (the chat widget calls it from browsers), so
  the endpoint checks the token itself (`code/api/jobs.py`): a Google-signed
  OIDC token, audience `JOBS_OIDC_AUDIENCE` (`<service>-jobs`), email in
  `JOBS_ALLOWED_INVOKERS` (`zero-scheduler@`). Anything else gets 401.
  With either setting unset, every call is refused.
- **One run at a time**: the run holds a PostgreSQL advisory lock on a
  dedicated connection, so a manual run or a second instance cannot overlap
  it; an overlapping call answers 409 and does nothing.
- **Responses**: 200 with counts only (no visitor data), 409 already
  running, 500 the run failed, 503 database unreachable. Failed sessions are
  retried on the next run anyway.

Run it now, without waiting for the hour:

```bash
gcloud scheduler jobs run zero-sales-agent-staging-periodic-summary --location us-central1
```

## Moving Off the VMs

The workflow no longer touches the VMs. To switch over with no gap and no
double Slack posts:

1. Run `scripts/gcp_setup.sh`, add the GitHub settings above and sort out
   *Database access*. Leave `CLOUD_SCHEDULER_ENABLED` unset: the VMs still run
   their own hourly summary, and Cloud Scheduler must not run one as well.
2. A day ahead, lower the TTL of the `zero-staging` and `zero` DNS records
   (e.g. 300s), so the switch and a switch back take effect quickly.
3. Merge to `staging`. Check the staging service URL (in the job summary):
   `/health`, the chat widget, `/api/docs`.
4. Point `zero-staging.smalltech.in` at the staging service (Cloud Run domain
   mapping, or a load balancer with a serverless NEG). Google issues the
   TLS certificate only after DNS points at it, so expect some minutes of
   certificate errors; time it for this on production too. Once it answers
   over HTTPS, stop the staging VM.
5. **Merge to `main` and move `zero.smalltech.in` in the same sitting.** From
   the merge on, `main` no longer reaches the production VM: until DNS
   moves, users get the code from before the merge, and both versions write
   to `prod_chat_db`. Merge nothing else to `main` in between. Once the domain
   answers from Cloud Run, stop the production VM.
6. With both VMs stopped, set the repository variable
   `CLOUD_SCHEDULER_ENABLED=true` and re-run the deploy of both branches
   (Actions → *Run workflow*): this resumes both jobs. Trigger one run by
   hand (above) and check Slack.
7. After a week without problems, delete the VMs and the legacy scripts
   (`scripts/deploy.sh`, `scripts/update_app.sh`, `code/get_env.py`).

To go back to a VM during the cutover: start it, point DNS back at it and
unset `CLOUD_SCHEDULER_ENABLED` (then re-run the deploy, which pauses the jobs).

Browsers call the API by its domain, so the chat widget needs no change as
long as the domains are kept.

## Monitoring

**GitHub Actions**: Actions tab → *Deploy to Cloud Run*. Each run's summary
shows the service URL and the image.

**Health check**:
```bash
curl https://<service-url>/health
# 200 when the database is reachable, 503 when it is not:
# {"message":"Hello World","database":"connected","pool":{...}}
```

The probe runs through the same connection pool the chat and prompt endpoints
use, so a green health check means those endpoints can reach the database too.
`database: disconnected` with the app still answering is the expected state
during a database outage - the app stays up and reconnects by itself.

**Logs** (everything the app prints goes to Cloud Logging):
```bash
gcloud run services logs tail zero-sales-agent --region us-central1        # live
gcloud run services logs read zero-sales-agent --region us-central1 --limit 200
```
Or Cloud Console → Cloud Run → service → *Logs*. Metrics (requests, latency,
instances, memory) are on the *Metrics* tab.

**Summary job**: Cloud Console → Cloud Scheduler shows the last run and its
HTTP status; the details are in the service logs under `[JOBS]` and
`[PERIODIC_SUMMARY]`.

## Rollback

Every deploy is a revision, and the previous ones stay available:

```bash
gcloud run revisions list --service zero-sales-agent --region us-central1
gcloud run services update-traffic zero-sales-agent --region us-central1 \
  --to-revisions <revision-name>=100
```

The next push to the branch deploys normally again and moves traffic to it.
Or revert the commit and let the workflow deploy the revert.

## Debugging

**Deploy fails**

| Step | Usual cause |
|---|---|
| test | A real test failure - run `pytest` locally |
| Build and push image | Artifact Registry repo missing (run `gcp_setup.sh`) or `artifactregistry.writer` missing |
| Deploy to Cloud Run | `run.admin` / *act as* on `zero-sales-agent-run@` missing; a secret missing or not readable by the runtime account |
| Smoke test | The revision starts but `/health` is not 200: almost always the database is not reachable from Cloud Run (see *Database access*). The step prints the `/health` body |
| Cloud Scheduler | `cloudscheduler.admin` or *act as* on `zero-scheduler@` missing |

**App not starting** - the revision's logs show the traceback:
```bash
gcloud run services logs read zero-sales-agent-staging --region us-central1 --limit 100
```

**Run the production image locally**
```bash
docker build -t zero .
docker run --rm -p 8080:8080 \
  -e APP_ENV=local -e SCHEDULER_ENABLED=false \
  -e DATABASE_URL=postgresql://postgres:postgres@host.docker.internal:5432/ \
  -e GROQ_API_KEY=... zero
curl localhost:8080/health
```

**Summary job returns 401** - the scheduler job's audience or service account
does not match the service's `JOBS_OIDC_AUDIENCE` / `JOBS_ALLOWED_INVOKERS`.
Re-running the deploy sets both sides again. The log line `[JOBS] periodic
summary refused: ...` says which check failed.

## Database Connections

The app does not need a restart after a PostgreSQL restart. All database access
goes through a connection pool that validates connections before use and
retries on failure, so the API recovers on its own within a few seconds. If it
does not, the database is still unreachable - check the server, not the app.

The pool tests each connection as it hands it out, so the `/health` probe
already replaces what a restart closed - it goes through the pool like any
other request. A background thread also calls the pool's `check()` every
`DB_CHECK_INTERVAL` seconds, as a backstop for when no probe is running. At
300s it costs about 576 empty statements a day.

**The app also starts when the database is down.** Startup waits
`DB_STARTUP_WAIT` seconds for the schema, then serves regardless, so `/health`
answers 503 within seconds rather than the process hanging. A background thread
keeps retrying and creates the schema as soon as the database appears - watch
for `Database schema ready` in the logs.

Measured behaviour during a full outage:

| | Database down | After it returns |
|---|---|---|
| `/health` | 503 in ~5s, `"database":"disconnected"` | green ~10s later |
| Other endpoints | 500 in ~16s (the retry budget) | normal, single-digit ms |
| The process | stays up and answering | no restart needed |

```bash
# What the app itself thinks:
curl -s https://<service-url>/health | python3 -m json.tool
gcloud logging read 'resource.type="cloud_run_revision"
  AND resource.labels.service_name="zero-sales-agent"
  AND textPayload=~"Database unreachable|lost connection|reconnect"' \
  --limit 50 --freshness 1h
```

**Connection pool settings.** Every environment-driven setting in this app is
declared in `code/config.py` with its default; `.env` carries values only. See
`code/.env.example` for the full list. The ones that matter most:

| Variable | Default | Purpose |
|---|---|---|
| `DB_POOL_MIN_SIZE` | 1 | Connections kept open |
| `DB_POOL_MAX_SIZE` | 2 | Ceiling on concurrent connections |
| `DB_POOL_TIMEOUT` | 5 | Seconds one attempt waits for a working connection |
| `DB_RETRY_ATTEMPTS` | 3 | Attempts before a request gives up |
| `DB_RECONNECT_TIMEOUT` | 10 | Caps the pool's background reconnect backoff |
| `DB_POOL_MAX_IDLE` | 300 | Recycle connections idle this long |
| `DB_POOL_MAX_LIFETIME` | 3600 | Recycle connections older than this |
| `DB_HEALTH_TIMEOUT` | 5 | Budget for the `/health` probe |
| `DB_CHECK_INTERVAL` | 300 | Seconds between pool checks; 0 disables them |
| `DB_STARTUP_WAIT` | 5 | Seconds startup waits for the schema before serving anyway |
| `DB_STATEMENT_TIMEOUT_MS` | 10000 | Ceiling on one query, so an overloaded DB cannot park a request |
| `DB_LOCK_TIMEOUT_MS` | 3000 | Fail fast when another process holds a lock |
| `DB_IDLE_TX_TIMEOUT_MS` | 30000 | Backstop against a transaction pinning a connection |
| `DB_TCP_USER_TIMEOUT_MS` | 20000 | Detects a server that went silent (Linux only) |

Worst case a request waits during a full outage is roughly
`DB_RETRY_ATTEMPTS x DB_POOL_TIMEOUT` plus backoff (about 16s by default).

`DB_POOL_MAX_SIZE` is 2 on purpose: the database is a shared cloud instance, so
the app stays a light tenant rather than sizing for its own peak. Two is enough
because no request holds a connection across the LLM call - every borrow is one
query lasting milliseconds. Measured on this app, 50 simultaneous visitors
(350 requests) complete in about 1s against 2 connections, and extra callers
queue rather than fail.

**If another process holds a lock** (a migration, a manual `ALTER TABLE`, or
another app), the app's own queries block on it. With only two pooled
connections, two blocked queries take the whole app out. `DB_LOCK_TIMEOUT_MS`
caps that at 3s rather than letting it consume the full 10s statement budget.

A lock timeout raises `LockNotAvailable` (SQLSTATE 55P03), which is excluded
from retries along with `QueryCanceled` (57014): retrying would wait again on a
lock someone else still holds, costing the full retry budget instead of the
timeout. Measured against an `ACCESS EXCLUSIVE` lock, a blocked query fails
after 3.1s on one attempt, and the pool recovers as soon as the lock is
released.

**If the database is reachable but not answering** (an overloaded shared
instance: connections succeed, queries do not return), `DB_STATEMENT_TIMEOUT_MS`
caps each query at 10s and the request fails rather than parking forever. A
statement that hits that ceiling is deliberately **not** retried - re-running a
query against a database that is already struggling is how a slow database
becomes a down one. Look for `canceling statement due to statement timeout` in
the logs; it means the database needs attention, not the app.

**Before raising or lowering it**, note the constraint that makes 2 safe: no
request may hold two connections at once. Two such requests would take one
connection each and then wait on each other. `test_db_pool.py` enforces this,
and `db_pool` logs a warning with a stack trace if it ever happens at runtime.
Lowering to 1 is not advised - a queued request would then sit behind any slow
query with nothing else to run on.


## Legacy: VM Deploy

Until the VMs are deleted, `scripts/update_app.sh` and `scripts/deploy.sh`
still run on them at boot. Nothing triggers them any more, and they read the
branch from the instance metadata, so a VM keeps serving the last code it
pulled. To look at one:

```bash
gcloud compute ssh <VM_INSTANCE_NAME> --zone=<ZONE>
tail -100 /opt/ai-agent-boilerplate/code/flask.log
```

---
*Last Updated: Nov 2026*
