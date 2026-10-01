#!/bin/bash
set -euo pipefail

# Re-run as root in a non-interactive way if needed.
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
	exec sudo -n "$0" "$@"
fi

APP_ROOT=/opt/ai-agent-boilerplate
APP_DIR="$APP_ROOT/code"
APP_PORT=5000
PID_FILE=zero.pid

echo "==== Starting deploy.sh ===="

cd "$APP_ROOT"

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

# ---------------------------------------------------------------------------
# Identifying our own process
#
# Three things must all hold, because each previous approach broke one of them:
#   - never kill an unrelated process (a PID from zero.pid can be recycled
#     after a reboot, and this script runs as root)
#   - never kill another Flask service on this machine (so "is it flask?" is
#     not a sufficient test -- it has to be *this install*)
#   - never leave our own old process running (it would hold port 5000 and
#     database connections, and the deploy would silently serve stale code)
#
# So the test is the installation directory, not the command name. Another
# service's processes live elsewhere and are skipped even when they are Flask
# on the same port.
# ---------------------------------------------------------------------------
is_our_app() {
    local pid="${1:-}" target
    [ -n "$pid" ] || return 1
    kill -0 "$pid" 2>/dev/null || return 1

    # /proc is authoritative: it cannot be fooled by how the command line is
    # spelled, and the venv binary and working directory both live under
    # APP_ROOT for our process only.
    for link in exe cwd; do
        target=$(readlink -f "/proc/$pid/$link" 2>/dev/null || true)
        case "$target" in
            "$APP_ROOT"|"$APP_ROOT"/*) return 0 ;;
        esac
    done

    # Fallback where /proc is unavailable. Both conditions are required: the
    # install path alone also matches this very script, which is how
    # `pkill -f "/opt/ai-agent-boilerplate"` used to kill the deploy mid-run.
    local args
    args=$(ps -p "$pid" -o args= 2>/dev/null || true)
    case "$args" in
        *"$APP_ROOT"*) case "$args" in *"flask run"*) return 0 ;; esac ;;
    esac
    return 1
}

# PIDs listening on our port, using whichever tool the image has.
listeners_on_port() {
    if command -v ss >/dev/null 2>&1; then
        ss -lptn "sport = :$APP_PORT" 2>/dev/null |
            grep -oE 'pid=[0-9]+' | cut -d= -f2 | sort -u
    elif command -v lsof >/dev/null 2>&1; then
        lsof -t -i ":$APP_PORT" -sTCP:LISTEN 2>/dev/null | sort -u
    elif command -v fuser >/dev/null 2>&1; then
        fuser "$APP_PORT/tcp" 2>/dev/null | tr -s ' ' '\n' | grep -E '^[0-9]+$' || true
    fi
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

# 1. The PID we recorded last time, if it is still this app.
if [ -f "$PID_FILE" ]; then
    OLD_PID=$(cat "$PID_FILE" 2>/dev/null || true)
    if is_our_app "$OLD_PID"; then
        stop_pid "$OLD_PID"
    else
        echo "  $PID_FILE is stale (PID ${OLD_PID:-none} is not this app) - ignoring it"
    fi
    rm -f "$PID_FILE"
fi

# 2. Any other process of ours still running, from a deploy that lost its PID
#    file. Every candidate is checked against this installation, so a second
#    Flask service on the machine is listed here and then skipped.
for pid in $(pgrep -f "flask run" 2>/dev/null || true); do
    if is_our_app "$pid"; then
        echo "  found another instance of this app (PID $pid) with no PID file"
        stop_pid "$pid"
    fi
done

# 3. If something that is not ours holds the port, report it instead of killing
#    it. Starting would fail to bind anyway, and killing someone else's service
#    to take their port is never the right call.
for pid in $(listeners_on_port); do
    # It may have exited since we listed it -- including one we just stopped.
    # A dead PID is not another service.
    kill -0 "$pid" 2>/dev/null || continue
    if ! is_our_app "$pid"; then
        echo "❌ Port $APP_PORT is held by PID $pid, which is not this app:"
        ps -p "$pid" -o pid=,user=,args= 2>/dev/null || true
        echo "   Refusing to kill another service. Free the port, or point this"
        echo "   app at a different APP_PORT."
        exit 1
    fi
done

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
