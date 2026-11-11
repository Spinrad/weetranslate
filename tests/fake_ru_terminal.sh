#!/bin/sh
# Fake terminal: choose Russian (4), then accept (y).
printf '4\ny\n' | "$@"
