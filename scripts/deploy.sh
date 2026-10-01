#!/bin/bash
set -euo pipefail

# Re-run as root in a non-interactive way if needed.
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
	exec sudo -n "$0" "$@"
fi

APP_DIR=/opt/ai-agent-boilerplate/code
APP_PORT=5000
PID_FILE=zero.pid
# The full command line of the app, used both to confirm a PID really is ours
# and as the fallback pattern. It is specific enough that it cannot match this
# script -- which is what went wrong with `pkill -f "/opt/ai-agent-boilerplate"`,
# since that matched deploy.sh's own path and killed the deploy mid-run.
FLASK_CMD="flask run --host=0.0.0.0 --port=$APP_PORT"

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

# Is this PID actually our app? PIDs are reused, so after a reboot the number
# in zero.pid can belong to an unrelated process -- and this script runs as
# root, which can kill anything.
is_our_flask() {
    local pid="${1:-}"
    [ -n "$pid" ] || return 1
    ps -p "$pid" -o args= 2>/dev/null | grep -qF "$FLASK_CMD"
}

# SIGTERM, wait up to 10s, then SIGKILL. SIGTERM matters: the app closes its
# database connection pool on it, so the shared instance gets its connections
# back instead of waiting to reap them.
stop_pid() {
    local pid="$1"
    echo "  stopping PID $pid"
    kill "$pid" 2>/dev/null || return 0
    for _ in $(seq 1 10); do
        kill -0 "$pid" 2>/dev/null || return 0
        sleep 1
    done
    echo "  PID $pid still alive after 10s, sending SIGKILL..."
    kill -9 "$pid" 2>/dev/null || true
}

echo "Stopping only ai-agent service..."

# 1. The PID we recorded last time -- but only if it is still our Flask.
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if is_our_flask "$OLD_PID"; then
        stop_pid "$OLD_PID"
    else
        echo "  ${PID_FILE} is stale (PID ${OLD_PID:-none} is not our app) - ignoring it"
    fi
    rm -f "$PID_FILE"
fi

# 2. Anything of ours still running without a PID file to point at it: a deploy
#    that crashed before writing one, or a file that was deleted. Skipping this
#    would leave the old process holding port 5000, the new one would fail to
#    bind, and the deploy would report success while serving the old code.
if pgrep -f "$FLASK_CMD" >/dev/null 2>&1; then
    echo "  found an orphaned Flask with no PID file - stopping it"
    pkill -f "$FLASK_CMD" 2>/dev/null || true
    for _ in $(seq 1 10); do
        pgrep -f "$FLASK_CMD" >/dev/null 2>&1 || break
        sleep 1
    done
    pkill -9 -f "$FLASK_CMD" 2>/dev/null || true
fi

echo "Starting Flask app..."
cd "$APP_DIR"
export FLASK_APP=app.py
nohup flask run --host=0.0.0.0 --port="$APP_PORT" > flask.log 2>&1 &
NEW_PID=$!
echo "$NEW_PID" > "$PID_FILE"

# Confirm it is actually serving before calling the deploy a success. Without
# this a failed bind ("Address already in use") is invisible: the process dies,
# the old one keeps serving, and the script still prints a tick.
echo "Waiting for the app to answer on port $APP_PORT..."
for _ in $(seq 1 30); do
    if ! kill -0 "$NEW_PID" 2>/dev/null; then
        echo "❌ Flask exited during startup. Last 20 lines of flask.log:"
        tail -20 flask.log || true
        exit 1
    fi
    CODE=$(curl -s -o /dev/null -w '%{http_code}' --max-time 3 \
        "http://127.0.0.1:$APP_PORT/health" 2>/dev/null || true)
    case "$CODE" in
        200)
            echo "✅ App is up (PID $NEW_PID), database connected"
            exit 0
            ;;
        503)
            # The app deliberately starts even when the database is down, so
            # this is a successful deploy with an unhealthy database.
            echo "⚠️  App is up (PID $NEW_PID), but /health reports the database as down"
            exit 0
            ;;
    esac
    sleep 1
done

echo "❌ App did not answer on port $APP_PORT within 30s. Last 20 lines of flask.log:"
tail -20 flask.log || true
exit 1
