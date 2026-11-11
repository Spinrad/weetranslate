#!/bin/sh
# Fake terminal: first popup selects Other and types Italian; the reopened
# popup accepts with y.
if [ ! -f "${WT_CUSTOM_STATE:?}" ]; then
  : > "$WT_CUSTOM_STATE"
  printf '5\nItalian\n' | "$@"
else
  printf 'y\n' | "$@"
fi
