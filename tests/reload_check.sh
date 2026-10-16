#!/usr/bin/env bash
# Exercise the real reload function with fake screen/process/configuration APIs.
set -euo pipefail
TEST_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
test_dir="$(mktemp -d)"
trap 'rm -f "$test_dir/events"; rmdir "$test_dir"' EXIT
test_python() {
    case "$*" in
        *'import ast;'*) printf 'precheck\n' >> "$test_dir/events"; return "$CHECK_CODE" ;;
        *hashlib*) echo test-instance ;;
        *p.hostname*) echo '127.0.0.1 3001 1' ;;
        *) echo 'ws://127.0.0.1:3001' ;;
    esac
}
PYTHON_CMD=test_python
source "$TEST_ROOT/start.sh" help >/dev/null

require_screen() { :; }
require_python() { :; }
session_state() { echo "$STATE"; }
bot_running() { [ "$BOT_ALIVE" = 1 ]; }
sleep() { :; }
screen() {
    if [[ "$*" != *"$BOT_SESSION"* || "$*" != *'-X stuff'* ]]; then
        echo 'unexpected screen operation' >&2
        return 1
    fi
    printf 'interrupt\n' >> "$test_dir/events"
    [ "$NOTIFY_CODE" = 0 ] || return 1
    if [ "$STOP_OK" = 1 ]; then BOT_ALIVE=0; fi
}
start_bot() {
    printf 'start\n' >> "$test_dir/events"
    [ "$START_CODE" = 0 ] || return 1
    BOT_ALIVE=1
}
do_start() { echo 'reload must not start NapCat' >&2; return 99; }
do_stop() { echo 'reload must not stop NapCat' >&2; return 99; }
reset_case() {
    STATE=running
    BOT_ALIVE=1
    CHECK_CODE=0
    STOP_OK=1
    NOTIFY_CODE=0
    START_CODE=0
    : > "$test_dir/events"
}
assert_events() {
    [ "$(cat "$test_dir/events")" = "$1" ] || {
        cat "$test_dir/events" >&2
        exit 1
    }
}

reset_case
do_reload >/dev/null
assert_events $'precheck\ninterrupt\nstart'
[ "$BOT_ALIVE" = 1 ]
echo 'PASS: managed bot reloads without NapCat/terminal operations'

reset_case
CHECK_CODE=1
if do_reload >/dev/null 2>&1; then exit 1; fi
assert_events precheck
[ "$BOT_ALIVE" = 1 ]
echo 'PASS: invalid source/configuration leaves the current bot running'

reset_case
STATE=none
if do_reload >/dev/null 2>&1; then exit 1; fi
assert_events precheck
echo 'PASS: unmanaged process is not interrupted or duplicated'

reset_case
STOP_OK=0
if do_reload >/dev/null 2>&1; then exit 1; fi
assert_events $'precheck\ninterrupt'
[ "$BOT_ALIVE" = 1 ]
echo 'PASS: slow shutdown never forces a kill or starts a duplicate'

reset_case
NOTIFY_CODE=1
if do_reload >/dev/null 2>&1; then exit 1; fi
assert_events $'precheck\ninterrupt'
echo 'PASS: failed exit notification stops reload'

reset_case
STATE=none
BOT_ALIVE=0
do_reload >/dev/null
assert_events $'precheck\nstart'
echo 'PASS: stopped bot can be started without touching NapCat'

reset_case
START_CODE=1
if do_reload >/dev/null 2>&1; then exit 1; fi
assert_events $'precheck\ninterrupt\nstart'
echo 'PASS: startup failure is reported'
