#!/usr/bin/env bash
# WeeTranslate API error tests (local mock, no real key).
#
# Covers: missing key, missing ${sec.data.*} secret, malformed JSON, 401,
# 500, timeout. Each case starts an isolated WeeChat and checks the message.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
SOCK="$ROOT/.work/tmux-api-errors.sock"

cleanup() {
  tmux -S "$SOCK" kill-server 2>/dev/null || true
  if [[ -n "${MOCK_PID:-}" ]]; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
  fi
  rm -f "$SOCK"
}
trap cleanup EXIT

run_case() {
  local name="$1" mode="$2" key="$3" expected="$4" timeout_s="$5"
  local work="$ROOT/.work/api-error-$name"
  local port=""
  local mock_pid=""

  rm -rf "$work"
  mkdir -p "$work"

  if [[ "$mode" != "none" ]]; then
    port="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"
    python3 "$ROOT/tests/mock_deepseek.py" --port "$port" --mode "$mode" \
      >"$work/mock.log" 2>&1 &
    mock_pid=$!
    MOCK_PID=$mock_pid
    sleep 0.4
  fi

  tmux -S "$SOCK" kill-server 2>/dev/null || true
  tmux -S "$SOCK" new-session -d -s "$name" \
    "weechat --dir $work --no-connect -P python"
  sleep 1.5

  send() {
    tmux -S "$SOCK" send-keys -t "$name" "$1" Enter
    sleep 0.25
  }

  send "/python load $ROOT/weetranslate.py"
  send "/set weetranslate.main.api_key $key"
  if [[ -n "$port" ]]; then
    send "/set weetranslate.main.api_url http://127.0.0.1:$port/chat/completions"
  fi
  send "/set weetranslate.main.timeout $timeout_s"
  send "/trad Hello"

  local found=0
  local attempt=0
  while [ "$attempt" -lt 80 ]; do
    attempt=$((attempt + 1))
    tmux -S "$SOCK" capture-pane -p -t "$name" -S -60 >"$work/pane.txt" 2>/dev/null || true
    if grep -qF "$expected" "$work/pane.txt"; then
      found=1
      break
    fi
    sleep 0.2
  done

  echo "----- $name -----"
  tail -n 6 "$work/pane.txt" || true
  if [[ "$found" != "1" ]]; then
    echo "FAIL ($name): expected message not found: $expected" >&2
    exit 1
  fi
  echo "PASS ($name): $expected"

  tmux -S "$SOCK" send-keys -t "$name" "/python unload wt_inspect" Enter 2>/dev/null || true
  sleep 0.3
  tmux -S "$SOCK" send-keys -t "$name" C-u
  tmux -S "$SOCK" send-keys -t "$name" '/quit -yes' Enter 2>/dev/null || true
  for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
    tmux -S "$SOCK" has-session -t "$name" 2>/dev/null || break
    sleep 0.1
  done
  tmux -S "$SOCK" kill-server 2>/dev/null || true
  if [[ -n "$mock_pid" ]]; then
    kill "$mock_pid" 2>/dev/null || true
    wait "$mock_pid" 2>/dev/null || true
    MOCK_PID=""
  fi
}

run_case "missing-key" "none" "" "DeepSeek API key is missing" 30
run_case "missing-secret" "ok" '${sec.data.absent}' "DeepSeek API key is missing" 30
run_case "malformed" "malformed" "test-key" "Unable to extract translations" 30
run_case "unauthorized" "401" "bad-key" "HTTP 401" 30
run_case "server-error" "500" "test-key" "HTTP 500" 30
run_case "timeout" "timeout" "test-key" "API request timed out" 5

echo "All API error tests passed."
