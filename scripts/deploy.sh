#!/bin/bash
set -euo pipefail

# Re-run as root in a non-interactive way if needed.
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
	exec sudo -n "$0" "$@"
fi

echo "==== Starting deploy.sh ===="

cd /opt/ai-agent-boilerplate

echo "Activating virtualenv..."
source venv/bin/activate

cd code

echo "Cleaning old .env..."
rm -f .env

# Keep flask.log across deploys. Every deploy is a VM reboot, so deleting it
# here erased the only record of why the previous run died, right when
# someone was redeploying to recover. Rotate on size instead: one
# generation, so the pair cannot exceed roughly 100M on a 10G disk.
# flask.log* is gitignored -- update_app.sh runs `git clean -fd` before this
# script, which would otherwise delete the rotated copy.
LOG_MAX_BYTES=52428800
if [ -f flask.log ] && [ "$(stat -c %s flask.log)" -gt "$LOG_MAX_BYTES" ]; then
    echo "flask.log over $LOG_MAX_BYTES bytes; rotating to flask.log.1"
    mv -f flask.log flask.log.1
fi

echo "Installing dependencies..."
pip install -r requirements.txt

echo "Generating .env file using Python script..."
python3 get_env.py

echo "Restarting service..."

# Stop only the process from the last deploy, by PID. One PID keeps all other
# services safe, including the second Flask app.
#
# Keep zero.pid in .gitignore. update_app.sh runs `git clean -fd` before this
# script and deletes untracked files.
echo "Stopping only ai-agent service..."
# Use the PID only if it is still this app. Linux uses a PID again after a
# reboot. This script runs as root.
if [ -f zero.pid ] && ps -p "$(cat zero.pid)" -o args= 2>/dev/null | grep -q "port=5000"; then
    OLD_PID=$(cat zero.pid)
    if kill "$OLD_PID" 2>/dev/null; then
        for i in $(seq 1 10); do
            kill -0 "$OLD_PID" 2>/dev/null || break
            sleep 1
        done
        if kill -0 "$OLD_PID" 2>/dev/null; then
            echo "PID $OLD_PID still alive after 10s, sending SIGKILL..."
            kill -9 "$OLD_PID" || true
        fi
    fi
    rm -f zero.pid
else
    if [ -f zero.pid ]; then
        echo "zero.pid is stale (PID $(cat zero.pid) is not this app) - ignoring it"
        rm -f zero.pid
    fi
    # No usable PID file. The first deploy after this change has none, because
    # the previous script did not write one. Stop the old process, or the new
    # one cannot use port 5000.
    #
    # The pattern contains the port. It cannot match this script, which has no
    # --port. It cannot match the other Flask service, which uses another port.
    echo "No usable zero.pid; stopping any instance left by an earlier deploy"
    pkill -f "flask run --host=0.0.0.0 --port=5000" 2>/dev/null || true
    sleep 2
fi

echo "Starting Flask app..."
cd /opt/ai-agent-boilerplate/code
export FLASK_APP=app.py
echo "==== $(date -Is): deploy, starting Flask ====" >> flask.log
nohup flask run --host=0.0.0.0 --port=5000 >> flask.log 2>&1 &
echo $! > zero.pid

# Make sure the app answers before you report success. nohup always succeeds.
# A process that stops at startup leaves the old code in service.
NEW_PID=$(cat zero.pid)
for _ in $(seq 1 30); do
    if ! kill -0 "$NEW_PID" 2>/dev/null; then
        echo "❌ Flask exited during startup. Last 20 lines of flask.log:"
        tail -20 flask.log || true
        exit 1
    fi
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
        http://127.0.0.1:5000/health 2>/dev/null || true)
    # 503 is a good deploy with a bad database. The app starts on purpose.
    if [ "$CODE" = "200" ] || [ "$CODE" = "503" ]; then
        echo "✅ Deployment complete! (PID $NEW_PID, /health returned $CODE)"
        exit 0
    fi
    sleep 1
done

echo "❌ App did not answer on port 5000 within 30s. Last 20 lines of flask.log:"
tail -20 flask.log || true
exit 1
