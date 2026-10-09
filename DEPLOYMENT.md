# Deployment Guide

Guide for deploying, monitoring, and debugging the AI Agent Boilerplate application.

## Table of Contents
1. [How Deployment Works](#how-deployment-works)
2. [Monitoring Deployments](#monitoring-deployments)
3. [Debugging Issues](#debugging-issues)

## How Deployment Works

**Stack**: Flask Python app on GCP Compute Engine with PostgreSQL, automated via GitHub Actions CI/CD.

**Flow**: `Push to GitHub → GitHub Actions → SSH to GCP VM → Pull Code → Run deploy.sh → App Running`

**Process**:
1. Push to configured branch triggers GitHub Actions
2. GitHub Actions authenticates with GCP service account, SSHs to VM, executes scripts
3. `update_app.sh`: Cleans logs, fetches branch from GCP metadata, pulls latest code (hard reset), runs `deploy.sh`
4. `deploy.sh`: Activates venv, cleans `.env`/`flask.log`, installs dependencies, generates `.env` via `get_env.py`, stops the previous app via the PID in `code/zero.pid` (SIGTERM, then SIGKILL after 10s), starts Flask on `0.0.0.0:5000` in the background, records its PID, and waits for `/health` to answer before reporting success

**Key Paths**:
- Scripts: `/scripts/deploy.sh`, `/scripts/update_app.sh`
- App: `/home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code/`
- Logs: `/var/log/update_app.log`, `/home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code/flask.log`

**Environment**: Secrets stored in GitHub (service account key, VM IP, project ID, DB credentials, API keys)


## Monitoring Deployments

**GitHub Actions**: Navigate to Actions tab → select workflow run → view stages and logs. Check for green checkmarks and typical 1-3 min duration.

**Health Check**:
```bash
curl http://<VM_EXTERNAL_IP>:5000/health
# 200 when the database is reachable, 503 when it is not:
# {"message":"Hello World","database":"connected",
#  "pool":{"min_size":1,"max_size":2,"size":2,"available":2,"waiting":0,"connections_lost":0}}
```

The probe runs through the same connection pool the chat and prompt endpoints
use, so a green health check means those endpoints can reach the database too.
`database: disconnected` with the app still answering is the expected state
during a database outage - the app stays up and reconnects by itself.

**On VM**:
```bash
ps aux | grep flask                                                        # Process running
tail -f /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code/flask.log  # Live logs
top                                                                        # CPU/memory
df -h                                                                      # Disk space
netstat -tulpn | grep 5000                                                # Network
psql -U <username> -d <database_name> -c "SELECT 1;"                      # DB connection
```



## Debugging Issues

**SSH Access** (macOS):
```bash
brew install --cask google-cloud-sdk  # Install gcloud
gcloud init                            # Initialize
gcloud config set project ai-agent-boilerplate0
gcloud compute ssh ai-agent-staging --zone=us-central1-c
```

### Common Issues

**1. Deployment Fails (GitHub Actions)**
- Check Actions tab → failed workflow → expand step
- Causes: Auth failure (verify service account key in secrets), SSH timeout (check VM/firewall), permissions (verify roles)

**2. Application Not Starting**
```bash
gcloud compute ssh ai-agent-staging --zone=us-central1-c
ps aux | grep flask
cat /var/log/update_app.log
sudo su # to access /home/vivek
cat /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code/flask.log
# Manual start:
cd /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code
source ../venv/bin/activate
export FLASK_APP=app.py
flask run --host=0.0.0.0 --port=5000
```

**3. Application Crashes**
```bash
tail -f /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code/flask.log
cd /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code
source ../venv/bin/activate
python3 -c "import app"  # Test import
cat .env                  # Verify variables
python3 -c "from db import ping; ping(); print('DB OK')"  # Test DB via the pool
```

**4. Missing Dependencies**
```bash
cd /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate
source venv/bin/activate
pip list
cd code && pip install -r requirements.txt
../scripts/deploy.sh
```

**5. Database Connection Errors**

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
for `Database schema ready` in `flask.log`.

Measured behaviour during a full outage:

| | Database down | After it returns |
|---|---|---|
| `/health` | 503 in ~5s, `"database":"disconnected"` | green ~10s later |
| Other endpoints | 500 in ~16s (the retry budget) | normal, single-digit ms |
| The process | stays up and answering | no restart needed |

```bash
sudo systemctl status postgresql
sudo systemctl start postgresql
psql -U <username> -d <database_name>
sudo tail -f /var/log/postgresql/postgresql-*.log
cat /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code/.env | grep DB

# What the app itself thinks:
curl -s http://localhost:5000/health | python3 -m json.tool
grep -E "Database unreachable|lost connection|reconnect"   /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code/flask.log
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
`flask.log`; it means the database needs attention, not the app.

**Before raising or lowering it**, note the constraint that makes 2 safe: no
request may hold two connections at once. Two such requests would take one
connection each and then wait on each other. `test_db_pool.py` enforces this,
and `db_pool` logs a warning with a stack trace if it ever happens at runtime.
Lowering to 1 is not advised - a queued request would then sit behind any slow
query with nothing else to run on.

**6. Port Already in Use**

`deploy.sh` stops the previous app using the PID in `code/zero.pid`. If that
file is missing or stale the old process survives and the new one cannot bind.

```bash
cat /opt/ai-agent-boilerplate/code/zero.pid   # what deploy.sh thinks is running
sudo lsof -i :5000                            # what actually holds the port
sudo kill <PID>                               # SIGTERM: the app closes its DB pool
sudo kill -9 <PID>                            # only if it ignores SIGTERM
cd /opt/ai-agent-boilerplate && ./scripts/deploy.sh
```

`zero.pid` is in `.gitignore` on purpose, not for tidiness: `update_app.sh`
runs `git clean -fd` before `deploy.sh`, which deletes untracked files. If the
PID file went with them, nothing would be stopped, the new process could not
bind port 5000, and the old code would keep serving while the deploy reported
success.

A stale `zero.pid` is the one case to watch. After a reboot the recorded PID
can belong to an unrelated process, and `deploy.sh` runs as root, so there is
no permission check to stop the kill. If the app has just restarted and
something else on the box died at the same time, check this first:

```bash
cat /opt/ai-agent-boilerplate/code/zero.pid
ps -p "$(cat /opt/ai-agent-boilerplate/code/zero.pid)" -o pid=,args=
```

### Complete Debugging Checklist

### Complete Debugging Checklist

```bash
gcloud compute ssh <VM_INSTANCE_NAME> --zone=<ZONE>
uptime && df -h && free -m                    # System status
ps aux | grep flask                           # Process
tail -100 /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code/flask.log
tail -100 /var/log/update_app.log
cd /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate && git status && git log -1 && ls -la code/
cat code/.env | grep -v "PASSWORD\|SECRET\|KEY"  # Environment (safe)
source venv/bin/activate && cd code && export FLASK_APP=app.py && python3 -c "import app; print('Import successful')"
curl http://localhost:5000/health
sudo iptables -L -n                           # Firewall
```

**Log Locations**:
- Update: `/var/log/update_app.log`
- Flask: `/home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code/flask.log`
- PostgreSQL: `/var/log/postgresql/postgresql-*.log`
- System: `/var/log/syslog`

**Collect Diagnostics**:
```bash
# On VM:
{ echo "=== System ===" && uname -a && echo "=== Disk ===" && df -h && \
  echo "=== Memory ===" && free -m && echo "=== Processes ===" && ps aux | grep -E 'flask|python' && \
  echo "=== Flask Log ===" && tail -50 /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code/flask.log
} > ~/debug-info.txt
# Local: gcloud compute scp <VM_INSTANCE_NAME>:~/debug-info.txt . --zone=<ZONE>
```

## Best Practices

**Before Deploy**: Test locally, update `requirements.txt`, commit all files, descriptive messages  
**After Deploy**: Monitor Actions, check health endpoint, review logs, test APIs  
**Maintenance**: Rotate logs weekly, update dependencies monthly, monitor resources

## Quick Reference

```bash
# Deploy manually
cd /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate && ./scripts/deploy.sh

# Live logs
tail -f /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code/flask.log

# Restart (deploy.sh stops the old process by PID and starts a new one)
cd /opt/ai-agent-boilerplate && ./scripts/deploy.sh

# Health check
curl http://localhost:5000/health

# SSH
gcloud compute ssh <VM_INSTANCE_NAME> --zone=<ZONE>
```

## Storage cleaning

```bash
# Clean cache
sudo apt clean

# Clear logs
sudo journalctl --vacuum-size=200M
```
---
*Last Updated: Feb 2026*
