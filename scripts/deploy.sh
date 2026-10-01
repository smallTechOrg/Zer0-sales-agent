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

echo "Cleaning old .env and Flask logs..."
rm -f .env
rm -f flask.log

echo "Installing dependencies..."
pip install -r requirements.txt

echo "Generating .env file using Python script..."
python3 get_env.py

echo "Restarting service..."

# Stop only the process we started last time, by PID. Targeting one PID keeps
# every other service on this machine out of it -- including the second Flask
# app, which an earlier `pkill -f "/opt/ai-agent-boilerplate"` would have been
# at risk of matching, along with this script itself.
#
# NOTE: zero.pid must stay in .gitignore. update_app.sh runs `git clean -fd`
# before this script, which deletes untracked files -- the PID file would go
# with them, nothing would be stopped, and the new process would fail to bind
# port 5000 while the old one kept serving.
echo "Stopping only ai-agent service..."
# The PID is only trusted if it is still this app. PIDs are reused, so after a
# reboot the recorded number can belong to an unrelated process -- and this
# script runs as root, so there is no permission check to stop the kill.
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
elif [ -f zero.pid ]; then
    echo "zero.pid is stale (PID $(cat zero.pid) is not this app) - ignoring it"
    rm -f zero.pid
fi

echo "Starting Flask app..."
cd /opt/ai-agent-boilerplate/code
export FLASK_APP=app.py
nohup flask run --host=0.0.0.0 --port=5000 > flask.log 2>&1 &
echo $! > zero.pid

echo "✅ Deployment complete!"
