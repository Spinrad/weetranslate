#!/bin/sh
# Fake terminal for tests: run the popup command with a key injected on
# stdin (WT_TEST_KEY=y by default).
printf '%s\n' "${WT_TEST_KEY:-y}" | "$@"
