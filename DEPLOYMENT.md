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
4. `deploy.sh`: Activates venv at `/home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate`, cleans `.env`/`flask.log`, installs dependencies, decodes the `GROQ_API_KEY`/`DATABASE_URL`/`GROQ_MODEL_NAME` values injected by the workflow over SSH into `.env`, kills old Flask processes, starts Flask on `0.0.0.0:5000` (background)

**Key Paths**:
- Scripts: `/scripts/deploy.sh`, `/scripts/update_app.sh`
- App: `/home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code/`
- Logs: `/var/log/update_app.log`, `/home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code/flask.log`

**Environment**: `GROQ_API_KEY`/`DATABASE_URL` live in GitHub **environment variables** (separate values for staging/production, plaintext/unmasked by design — see `CI_CD.md`); `GROQ_MODEL_NAME` is a shared repo-level GitHub variable. All other deploy credentials (service account key, VM name/zone, project ID) remain GitHub Actions **secrets**.


## Monitoring Deployments

**GitHub Actions**: Navigate to Actions tab → select workflow run → view stages and logs. Check for green checkmarks and typical 1-3 min duration.

**Health Check**:
```bash
curl http://<VM_EXTERNAL_IP>:5000/health  # Expected: {"message": "Hello World"}
```

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
python3 -c "from db import get_connection; get_connection()"  # Test DB
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
```bash
sudo systemctl status postgresql
sudo systemctl start postgresql
psql -U <username> -d <database_name>
sudo tail -f /var/log/postgresql/postgresql-*.log
cat /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate/code/.env | grep DB
```

**6. Port Already in Use**
```bash
sudo lsof -i :5000                    # Find PID
sudo kill -9 <PID>                    # Kill process
pkill -f flask                        # Kill all Flask
cd /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate && ./scripts/deploy.sh
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

# Restart
pkill -f flask && cd /home/vivek/Ai-agent-boilerplate/ai-agent-boilerplate && ./scripts/deploy.sh

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
