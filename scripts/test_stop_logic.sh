#!/bin/bash
# Exercise deploy.sh's stop logic against fake processes.
# The logic under test is extracted from the real script, so it cannot drift.

DEPLOY=${1:?usage: test_stop_logic.sh /path/to/deploy.sh}

STOP_LOGIC=$(sed -n '/^is_our_flask() {/,/^fi$/p' "$DEPLOY" | sed '$d')
# Trim to end at the close of the orphan block.
STOP_LOGIC=$(sed -n '/^is_our_flask() {/,/^echo "Starting Flask app\.\.\."$/p' "$DEPLOY" | sed '$d')

PASS=0; FAIL=0

run_case() {
    local name="$1"; shift
    # shellcheck disable=SC2034
    declare -gA PROC=()          # pid -> command line
    KILLED=""; PKILLED=""; OUTPUT=""
    "$@"
}

check() {
    local name="$1" expected="$2" actual="$3"
    if [ "$expected" = "$actual" ]; then
        echo "  PASS  $name"
        PASS=$((PASS+1))
    else
        echo "  FAIL  $name"
        echo "          expected: '$expected'"
        echo "          actual:   '$actual'"
        FAIL=$((FAIL+1))
    fi
}

# --- mocks ----------------------------------------------------------------
ps() {            # ps -p PID -o args=
    local pid="$2"
    [ -n "${PROC[$pid]:-}" ] || return 1
    echo "${PROC[$pid]}"
}
kill() {
    if [ "$1" = "-0" ]; then
        [ -n "${PROC[$2]:-}" ] || return 1
        return 0
    fi
    if [ "$1" = "-9" ]; then
        KILLED="$KILLED kill-9:$2"; unset "PROC[$2]"; return 0
    fi
    [ -n "${PROC[$1]:-}" ] || return 1
    KILLED="$KILLED term:$1"; unset "PROC[$1]"; return 0
}
pgrep() {         # pgrep -f PATTERN
    local pat="$2"
    for p in "${!PROC[@]}"; do
        case "${PROC[$p]}" in *"$pat"*) return 0;; esac
    done
    return 1
}
pkill() {
    local pat sig=""
    if [ "$1" = "-9" ]; then sig="-9"; pat="$3"; else pat="$2"; fi
    local hit=1
    for p in "${!PROC[@]}"; do
        case "${PROC[$p]}" in
            *"$pat"*) PKILLED="$PKILLED pkill$sig:$p"; unset "PROC[$p]"; hit=0;;
        esac
    done
    return $hit
}
sleep() { :; }    # no real waiting
export -f 2>/dev/null || true

FLASK_CMD="flask run --host=0.0.0.0 --port=5000"
PID_FILE="$(mktemp)"
OUTFILE="$(mktemp)"

scenario() {
    local name="$1" pidfile_content="$2"; shift 2
    declare -gA PROC=()
    while [ $# -gt 0 ]; do PROC["$1"]="$2"; shift 2; done
    KILLED=""; PKILLED=""
    if [ -n "$pidfile_content" ]; then
        echo "$pidfile_content" > "$PID_FILE"
    else
        rm -f "$PID_FILE"
    fi
    eval "$STOP_LOGIC" > "$OUTFILE" 2>&1
    OUTPUT=$(cat "$OUTFILE")
    echo "--- $name"
    echo "$OUTPUT" | sed 's/^/        /'
}

echo
echo "=== 1. Normal: PID file points at our Flask ==="
scenario "normal" "4242" \
    4242 "/opt/ai-agent-boilerplate/venv/bin/python /opt/venv/bin/flask run --host=0.0.0.0 --port=5000"
check "our Flask is stopped" " term:4242" "$KILLED"
check "no blind pkill needed" "" "$PKILLED"

echo
echo "=== 2. Stale PID after a reboot: that PID is now sshd ==="
scenario "stale" "4242" \
    4242 "/usr/sbin/sshd -D"
check "sshd is NOT killed" "" "$KILLED"
check "nothing pkilled either" "" "$PKILLED"
check "stale is reported" "yes" "$(echo "$OUTPUT" | grep -q 'stale' && echo yes || echo no)"

echo
echo "=== 3. PID file lost, old Flask still running (orphan) ==="
scenario "orphan" "" \
    7777 "/opt/venv/bin/flask run --host=0.0.0.0 --port=5000"
check "orphan is stopped" " pkill:7777" "$PKILLED"
check "orphan reported" "yes" "$(echo "$OUTPUT" | grep -q 'orphaned' && echo yes || echo no)"

echo
echo "=== 4. Stale PID AND an orphan Flask elsewhere ==="
scenario "both" "4242" \
    4242 "/usr/sbin/sshd -D" \
    7777 "/opt/venv/bin/flask run --host=0.0.0.0 --port=5000"
check "sshd untouched" "" "$KILLED"
check "orphan still cleaned up" " pkill:7777" "$PKILLED"

echo
echo "=== 5. Nothing running at all ==="
scenario "clean" ""
check "no kills" "" "$KILLED"
check "no pkills" "" "$PKILLED"

echo
echo "=== 6. PID file points at a process that no longer exists ==="
scenario "gone" "4242"
check "no kills" "" "$KILLED"
check "reported stale" "yes" "$(echo "$OUTPUT" | grep -q 'stale' && echo yes || echo no)"

rm -f "$PID_FILE" "$OUTFILE"
echo
echo "================================"
echo "  $PASS passed, $FAIL failed"
[ "$FAIL" -eq 0 ]
