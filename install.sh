#!/bin/sh
# Install WeeTranslate into WeeChat's Python directory.
#
# POSIX sh: works with /bin/sh on FreeBSD, no bash required.
#
# Usage:
#   ./install.sh
#   WEECHAT_HOME=/path ./install.sh
#
# WeeChat 4.x uses ~/.local/share/weechat (or $XDG_DATA_HOME/weechat),
# unless WEECHAT_HOME is set.

set -eu

ROOT=$(cd "$(dirname "$0")" && pwd)
DATA_HOME=${XDG_DATA_HOME:-$HOME/.local/share}

if [ -n "${WEECHAT_HOME:-}" ]; then
  PYTHON_DIR="$WEECHAT_HOME/python"
else
  PYTHON_DIR="$DATA_HOME/weechat/python"
fi

if command -v weechat >/dev/null 2>&1; then
  version=$(weechat --version 2>/dev/null | awk 'NR==1 {print $2}')
  if [ -n "$version" ]; then
    if ! printf '%s\n' "$version" | awk -F. '
      { if ($1 < 4 || ($1 == 4 && $2 < 1)) exit 1; exit 0 }
    '; then
      echo "Warning: WeeChat $version detected; WeeTranslate requires WeeChat >= 4.1 (hook_url)." >&2
    fi
  fi
fi

if [ ! -f "$ROOT/weetranslate.py" ] || [ ! -f "$ROOT/weetranslate_popup.py" ]; then
  echo "Error: plugin files not found in $ROOT" >&2
  exit 1
fi

mkdir -p "$PYTHON_DIR/autoload"

install -m 0644 "$ROOT/weetranslate.py" "$PYTHON_DIR/weetranslate.py"
install -m 0755 "$ROOT/weetranslate_popup.py" "$PYTHON_DIR/weetranslate_popup.py"
ln -sfn ../weetranslate.py "$PYTHON_DIR/autoload/weetranslate.py"

echo "WeeTranslate installed in: $PYTHON_DIR"
echo
echo "Restart WeeChat, or load it at runtime:"
echo "  /python load $PYTHON_DIR/weetranslate.py"
echo
echo "Then configure the API key:"
echo "  /set weetranslate.main.api_key sk-..."
echo "  /trad help"
