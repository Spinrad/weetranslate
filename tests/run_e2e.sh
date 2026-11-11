#!/usr/bin/env bash
# WeeTranslate integration test: no real API key, no real terminal.
#
# Usage:
#   ./tests/run_e2e.sh y        # accept
#   ./tests/run_e2e.sh n        # edit with the fake editor
#   ./tests/run_e2e.sh q        # cancel
#   ./tests/run_e2e.sh u        # unicode cursor check
#   ./tests/run_e2e.sh close    # /close the popup and accept
#   ./tests/run_e2e.sh detached # terminal that detaches its process
#   ./tests/run_e2e.sh slash    # translation starting with / (safety)
#   ./tests/run_e2e.sh preserve # do not overwrite user input
#   ./tests/run_e2e.sh tmux     # terminal=tmux popup arguments
#   ./tests/run_e2e.sh de       # initial German
#   ./tests/run_e2e.sh lang     # switch to German, then accept
#   ./tests/run_e2e.sh ru       # switch to Russian, then accept
#   ./tests/run_e2e.sh custom   # enter a custom language (Italian)
#
# Starts an isolated WeeChat in tmux, a local DeepSeek mock and a fake
# terminal. It checks the REAL input bar via tests/inspect_input.py.

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
KEY="${1:-y}"
case "$KEY" in
  y|n|q|u|close|detached|slash|preserve|tmux|de|lang|ru|custom) ;;
  *) echo "usage: $0 [y|n|q|u|close|detached|slash|preserve|tmux|de|lang|ru|custom]" >&2; exit 2 ;;
esac

WORK="$ROOT/.work/e2e-$KEY"
SOCK="$ROOT/.work/tmux-e2e-$KEY.sock"
MOCK_LOG="$WORK/mock.log"
PANE_LOG="$WORK/pane.txt"
INPUT_JSON="$WORK/input.json"

rm -rf "$WORK"
mkdir -p "$WORK"

# Dynamically choose a free port to avoid collisions.
PORT="$(python3 - <<'PY'
import socket
s = socket.socket()
s.bind(("127.0.0.1", 0))
print(s.getsockname()[1])
s.close()
PY
)"

cleanup() {
  tmux -S "$SOCK" kill-server 2>/dev/null || true
  if [[ -n "${MOCK_PID:-}" ]]; then
    kill "$MOCK_PID" 2>/dev/null || true
    wait "$MOCK_PID" 2>/dev/null || true
  fi
  rm -f "$SOCK"
}
trap cleanup EXIT

python3 "$ROOT/tests/mock_deepseek.py" --port "$PORT" >"$MOCK_LOG" 2>&1 &
MOCK_PID=$!
sleep 0.5
if ! kill -0 "$MOCK_PID" 2>/dev/null; then
  echo "FAIL: DeepSeek mock did not start (port $PORT)" >&2
  cat "$MOCK_LOG" >&2 || true
  exit 1
fi

TEST_KEY="y"
TERMINAL="$ROOT/tests/fake_terminal.sh"
case "$KEY" in
  n) TEST_KEY="n" ;;
  q) TEST_KEY="q" ;;
  u) TEST_KEY="y" ;;
  close) TEST_KEY="y"; TERMINAL="$ROOT/tests/fake_wait_terminal.sh" ;;
  detached) TEST_KEY="y"; TERMINAL="$ROOT/tests/fake_detached_terminal.sh" ;;
  slash) TEST_KEY="y" ;;
  preserve) TEST_KEY="y"; TERMINAL="$ROOT/tests/fake_keyfile_terminal.sh" ;;
  tmux) TEST_KEY="y"; TERMINAL="" ;;
  de) TEST_KEY="y" ;;
  lang) TEST_KEY="y"; TERMINAL="$ROOT/tests/fake_lang_terminal.sh" ;;
  ru) TEST_KEY="y"; TERMINAL="$ROOT/tests/fake_ru_terminal.sh" ;;
  custom) TEST_KEY="y"; TERMINAL="$ROOT/tests/fake_custom_terminal.sh" ;;
esac

TMUX_ENV=""
if [[ "$KEY" == "tmux" ]]; then
  TMUX_ENV="PATH=$ROOT/tests/fakebin:$PATH WT_FAKE_TMUX_ARGS=$WORK/tmux-args"
fi
tmux -S "$SOCK" new-session -d -s "wt-$KEY" \
  "env WT_TEST_KEY=$TEST_KEY WT_TEST_KEY_FILE=$WORK/key WT_CUSTOM_STATE=$WORK/custom-state WT_INSPECT_FILE=$INPUT_JSON WT_SET_INPUT_FILE=$WORK/set-input $TMUX_ENV weechat --dir $WORK --no-connect -P python"
sleep 2

send() {
  tmux -S "$SOCK" send-keys -t "wt-$KEY" "$1" Enter
  sleep 0.25
}

send "/python load $ROOT/weetranslate.py"
send "/python load $ROOT/tests/inspect_input.py"
send "/set weetranslate.main.api_key test-key"
send "/set weetranslate.main.api_url http://127.0.0.1:$PORT/chat/completions"
if [[ "$KEY" == "tmux" ]]; then
  send "/set weetranslate.main.terminal tmux"
else
  send "/set weetranslate.main.terminal custom"
  send "/set weetranslate.main.terminal_cmd $TERMINAL"
fi
send "/set weetranslate.main.editor $ROOT/tests/fake_editor.sh"
if [[ "$KEY" == "de" ]]; then
  send "/set weetranslate.main.default_language de"
fi

TEXT="Hello, how are you?"
EXPECTED="Hello, how are you?"
if [[ "$KEY" == "u" ]]; then
  TEXT="Coffee ☕"
  EXPECTED="EN: Coffee ☕"
elif [[ "$KEY" == "slash" ]]; then
  TEXT="Slash phrase"
  EXPECTED=" /quit"
elif [[ "$KEY" == "preserve" ]]; then
  EXPECTED="Draft"
elif [[ "$KEY" == "de" ]] || [[ "$KEY" == "lang" ]]; then
  EXPECTED="Hallo, wie geht es dir?"
elif [[ "$KEY" == "ru" ]]; then
  EXPECTED="Привет, как дела?"
elif [[ "$KEY" == "custom" ]]; then
  EXPECTED="Ciao, come stai?"
fi

send "/trad $TEXT"
if [[ "$KEY" == "preserve" ]]; then
  sleep 2
  # Simulate real user input via the test inspector timer.
  printf 'Draft' > "$WORK/set-input"
  printf 'y\n' > "$WORK/key"
elif [[ "$KEY" == "close" ]]; then
  # Let the popup open, then close it from WeeChat.
  sleep 2
  send "/close"
fi

FOUND=0
_attempt=0
while [ "$_attempt" -lt 100 ]; do
  _attempt=$((_attempt + 1))
  tmux -S "$SOCK" capture-pane -p -t "wt-$KEY" -S -80 > "$PANE_LOG" 2>/dev/null || true
  if [[ "$KEY" == "q" ]]; then
    # Cancellation must not write to the channel and must leave the input
    # empty. Allow the popup time to close.
    if [[ "$_attempt" -gt 20 ]] && \
       python3 - "$INPUT_JSON" "$EXPECTED" "q" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
sys.exit(0 if data.get("input", "") == "" else 1)
PY
    then
      FOUND=1
      break
    fi
  else
    if python3 - "$INPUT_JSON" "$EXPECTED" "$KEY" <<'PY'
import json, sys
try:
    data = json.load(open(sys.argv[1], encoding="utf-8"))
except Exception:
    sys.exit(1)
value = data.get("input", "")
if sys.argv[3] == "n":
    ok = value.endswith("EDITED")
else:
    ok = value == sys.argv[2]
if not ok:
    sys.exit(1)
if sys.argv[3] == "preserve":
    sys.exit(0)
sys.exit(0 if data.get("input_pos") == len(value) else 1)
PY
    then
      if [[ "$KEY" == "preserve" ]] && [[ "$_attempt" -lt 20 ]]; then
        sleep 0.15
        continue
      fi
      FOUND=1
      break
    fi
  fi
  sleep 0.15
done

echo "===== pane ($KEY) ====="
tail -n 12 "$PANE_LOG"
echo "===== input.json ====="
cat "$INPUT_JSON" 2>/dev/null || true
echo
echo "========================"

if [[ "$FOUND" != "1" ]]; then
  echo "FAIL: input bar does not match the expected result ($KEY)" >&2
  exit 1
fi

if grep -qE 'Translation (in progress|received|inserted)|Translation canceled' "$PANE_LOG"; then
  echo "FAIL: translation messages leaked into the channel" >&2
  exit 1
fi

if [[ "$KEY" == "tmux" ]]; then
  for needle in display-popup WeeTranslate weetranslate_popup.py; do
    if ! grep -q "$needle" "$WORK/tmux-args"; then
      echo "FAIL: missing tmux argument: $needle" >&2
      cat "$WORK/tmux-args" >&2
      exit 1
    fi
  done
fi

if [[ "$KEY" == "q" ]]; then
  echo "PASS ($KEY): canceled, input bar empty"
elif [[ "$KEY" == "preserve" ]]; then
  echo "PASS ($KEY): user input 'Draft' preserved, translation not inserted"
elif [[ "$KEY" == "tmux" ]]; then
  echo "PASS ($KEY): terminal=tmux, display-popup args correct, injection OK"
else
  echo "PASS ($KEY): input bar correct (cursor at end of line)"
fi

tmux -S "$SOCK" send-keys -t "wt-$KEY" C-u
sleep 0.2
tmux -S "$SOCK" send-keys -t "wt-$KEY" "/python unload wt_inspect" Enter 2>/dev/null || true
sleep 0.3
tmux -S "$SOCK" send-keys -t "wt-$KEY" C-u
tmux -S "$SOCK" send-keys -t "wt-$KEY" '/quit -yes' Enter 2>/dev/null || true
for _ in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do
  tmux -S "$SOCK" has-session -t "wt-$KEY" 2>/dev/null || break
  sleep 0.1
done
sleep 1
