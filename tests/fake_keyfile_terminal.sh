#!/bin/sh
# Fake terminal: wait until WT_TEST_KEY_FILE contains a key, then inject it
# into the helper. Used to simulate user input while the popup is open.
while [ ! -f "${WT_TEST_KEY_FILE:?}" ]; do
  sleep 0.1
done
cat "$WT_TEST_KEY_FILE" | "$@"
