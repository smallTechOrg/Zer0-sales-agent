#!/bin/bash
# Exercise deploy.sh's stop logic against fake processes.
#
# The logic under test is extracted from deploy.sh itself, so this cannot drift
# from what actually runs. deploy.sh runs as root and kills processes, so the
# cases that matter most are the ones where it must NOT kill something.
#
#   usage: scripts/test_stop_logic.sh scripts/deploy.sh

DEPLOY=${1:?usage: test_stop_logic.sh /path/to/deploy.sh}

# Everything from is_our_app() up to (not including) the start step.
STOP_LOGIC=$(sed -n '/^is_our_app() {/,/^echo "Starting Flask app\.\.\."$/p' "$DEPLOY" | sed '$d')

APP_ROOT=/opt/ai-agent-boilerplate
APP_PORT=5000
PASS=0
FAIL=0

OURS="/opt/ai-agent-boilerplate/venv/bin/python /opt/ai-agent-boilerplate/venv/bin/flask run --host=0.0.0.0 --port=5000"
OTHER_FLASK="/srv/other-service/venv/bin/python /srv/other-service/venv/bin/flask run --host=0.0.0.0 --port=3001"
OTHER_FLASK_SAME_PORT="/srv/other-service/venv/bin/python /srv/other-service/venv/bin/flask run --host=0.0.0.0 --port=5000"
SSHD="/usr/sbin/sshd -D"
DEPLOY_SCRIPT="/bin/bash /opt/ai-agent-boilerplate/scripts/deploy.sh"
# Same install, different port: a second copy of this app must not be killed
# either, which is why the port is part of the identity and not just the path.
OURS_OTHER_PORT="/opt/ai-agent-boilerplate/venv/bin/python /opt/ai-agent-boilerplate/venv/bin/flask run --host=0.0.0.0 --port=5050"

# --- fake process table ----------------------------------------------------
# PROC[pid]=command line   PROC_ROOT[pid]=install dir (stands in for /proc)
declare -A PROC
declare -A PROC_ROOT
LISTENERS=""

ps() {                      # ps -p PID -o args=   /   ps -p PID -o pid=,user=,args=
    local pid="$2"
    [ -n "${PROC[$pid]:-}" ] || return 1
    echo "${PROC[$pid]}"
}
readlink() {                # readlink -f /proc/PID/exe|cwd
    local path="$2" pid
    case "$path" in
        /proc/*) pid=$(echo "$path" | cut -d/ -f3) ;;
        *) return 1 ;;
    esac
    [ -n "${PROC_ROOT[$pid]:-}" ] || return 1
    echo "${PROC_ROOT[$pid]}/venv/bin/python"
}
kill() {
    if [ "$1" = "-0" ]; then [ -n "${PROC[$2]:-}" ]; return $?; fi
    if [ "$1" = "-9" ]; then echo -n " kill9:$2" >> "$KILLFILE"; unset "PROC[$2]"; return 0; fi
    [ -n "${PROC[$1]:-}" ] || return 1
    echo -n " term:$1" >> "$KILLFILE"; unset "PROC[$1]"; return 0
}
pgrep() {                   # pgrep -f PATTERN
    local pat="$2" found=1
    for p in "${!PROC[@]}"; do
        case "${PROC[$p]}" in *"$pat"*) echo "$p"; found=0 ;; esac
    done
    return $found
}
# Mock `ss` rather than listeners_on_port: that function is part of the logic
# under test, so stubbing it would skip the parsing this is meant to cover.
command() { [ "$2" = "ss" ]; }
ss() {
    local p
    for p in $LISTENERS; do
        echo "LISTEN 0 128 0.0.0.0:$APP_PORT 0.0.0.0:* users:((\"flask\",pid=$p,fd=3))"
    done
}
sleep() { :; }

check() {
    local name="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "  PASS  $name"; PASS=$((PASS+1))
    else
        echo "  FAIL  $name"
        echo "          expected: '$expected'"
        echo "          actual:   '$actual'"
        FAIL=$((FAIL+1))
    fi
}

PID_FILE_PATH="$(mktemp)"
OUTFILE="$(mktemp)"
KILLFILE="$(mktemp)"
SURVIVORS="$(mktemp)"

# scenario <pidfile contents> <listeners> [pid cmdline root]...
scenario() {
    local pidfile_content="$1" listeners="$2"; shift 2
    PROC=(); PROC_ROOT=()
    while [ $# -gt 0 ]; do
        PROC["$1"]="$2"; PROC_ROOT["$1"]="$3"; shift 3
    done
    LISTENERS="$listeners"
    : > "$KILLFILE"
    PID_FILE="$PID_FILE_PATH"
    if [ -n "$pidfile_content" ]; then
        echo "$pidfile_content" > "$PID_FILE_PATH"
    else
        rm -f "$PID_FILE_PATH"
    fi
    # Subshell: the logic may exit(1) on a genuine port conflict, and that must
    # not end the test run. Survivors are reported back through a file.
    (
        eval "$STOP_LOGIC"
        printf '%s
' "${!PROC[@]}" > "$SURVIVORS"
    ) > "$OUTFILE" 2>&1
    EXIT_CODE=$?
    [ -s "$SURVIVORS" ] || : > "$SURVIVORS"
    KILLED=$(cat "$KILLFILE")
    OUTPUT=$(cat "$OUTFILE")
    sed 's/^/        /' "$OUTFILE"
}

echo
echo "=== 1. Normal: PID file points at our app ==="
scenario "4242" "4242" 4242 "$OURS" "$APP_ROOT"
check "our app is stopped" " term:4242" "$KILLED"

echo
echo "=== 2. Stale PID after a reboot: that PID is now sshd ==="
scenario "4242" "" 4242 "$SSHD" "/usr"
check "sshd is NOT killed" "" "$KILLED"
check "stale is reported" "yes" "$(echo "$OUTPUT" | grep -q 'stale' && echo yes || echo no)"

echo
echo "=== 3. Orphan of ours, no PID file ==="
scenario "" "7777" 7777 "$OURS" "$APP_ROOT"
check "orphan is stopped" " term:7777" "$KILLED"

echo
echo "=== 4. A SECOND Flask service on this machine (different install) ==="
scenario "" "" 8888 "$OTHER_FLASK" "/srv/other-service"
check "other service is NOT killed" "" "$KILLED"

echo
echo "=== 5. Ours AND another Flask service running together ==="
scenario "4242" "4242" \
    4242 "$OURS" "$APP_ROOT" \
    8888 "$OTHER_FLASK" "/srv/other-service"
check "only ours is stopped" " term:4242" "$KILLED"
check "other service survives" "yes" "$(grep -qx 8888 "$SURVIVORS" && echo yes || echo no)"

echo
echo "=== 6. Another service is holding OUR port ==="
scenario "" "8888" 8888 "$OTHER_FLASK_SAME_PORT" "/srv/other-service"
check "it is NOT killed" "" "$KILLED"
check "deploy refuses to continue" "1" "$EXIT_CODE"
check "the conflict is reported" "yes" "$(echo "$OUTPUT" | grep -q 'not this app' && echo yes || echo no)"

echo
echo "=== 7. deploy.sh's own process must never match ==="
scenario "" "" \
    9999 "$DEPLOY_SCRIPT" "$APP_ROOT" \
    4242 "$OURS" "$APP_ROOT"
check "the deploy script is not killed" "yes" "$(grep -qx 9999 "$SURVIVORS" && echo yes || echo no)"
check "the app still is" " term:4242" "$KILLED"

echo
echo "=== 7b. A second copy of THIS app on a different port ==="
scenario "" "" 6060 "$OURS_OTHER_PORT" "$APP_ROOT"
check "the other port's instance is NOT killed" "" "$KILLED"

echo
echo "=== 7c. That instance alongside ours: only ours goes ==="
scenario "4242" "4242"     4242 "$OURS" "$APP_ROOT"     6060 "$OURS_OTHER_PORT" "$APP_ROOT"
check "only our port's instance is stopped" " term:4242" "$KILLED"
check "the other port's instance survives" "yes" "$(grep -qx 6060 "$SURVIVORS" && echo yes || echo no)"

echo
echo "=== 8. Nothing running ==="
scenario "" ""
check "no kills" "" "$KILLED"

echo
echo "=== 9. PID file points at a dead PID ==="
scenario "4242" ""
check "no kills" "" "$KILLED"
check "reported stale" "yes" "$(echo "$OUTPUT" | grep -q 'stale' && echo yes || echo no)"

rm -f "$PID_FILE_PATH" "$OUTFILE" "$KILLFILE" "$SURVIVORS"
echo
echo "========================================"
echo "  $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
