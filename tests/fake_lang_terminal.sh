#!/bin/sh
# Fake terminal: choose German (2), then accept (y).
printf '2\ny\n' | "$@"
