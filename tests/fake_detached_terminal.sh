#!/bin/sh
# Simulates a terminal that detaches its process (like gnome-terminal):
# returns immediately, helper runs in the background.
( printf '%s\n' "${WT_TEST_KEY:-y}" | "$@" ) >/dev/null 2>&1 &
exit 0
