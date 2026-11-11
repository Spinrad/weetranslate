#!/bin/sh
# Fake terminal that leaves the helper waiting for a key (pipe with no data).
# Used to test /close: the plugin sends SIGTERM to the helper.
sleep 30 | "$@"
