# -*- coding: utf-8 -*-
"""WeeChat test script: publish the current input bar as JSON.

Loaded only by integration tests. WT_INSPECT_FILE is the output path. A timer
writes the state whenever it changes, so tests can check the real input bar
without relying on tmux pane rendering. WT_SET_INPUT_FILE can be used to inject
text into the input bar and simulate user typing.
"""

import json
import os

import weechat

OUTPUT = os.environ.get("WT_INSPECT_FILE", "")
SET_INPUT = os.environ.get("WT_SET_INPUT_FILE", "")
_last = None
_last_set = None


def tick(_data, _remaining_calls):
    global _last, _last_set
    if not OUTPUT:
        return weechat.WEECHAT_RC_OK
    buffer = weechat.current_buffer()
    if SET_INPUT and os.path.isfile(SET_INPUT):
        try:
            with open(SET_INPUT, "r", encoding="utf-8") as handle:
                injected = handle.read()
        except OSError:
            injected = None
        if injected is not None and injected != _last_set:
            _last_set = injected
            weechat.buffer_set(buffer, "input", injected)
            try:
                weechat.buffer_set(
                    buffer, "input_pos", str(len(injected.encode("utf-8")))
                )
            except Exception:
                pass
    try:
        payload = {
            "buffer": weechat.buffer_get_string(buffer, "full_name"),
            "input": weechat.buffer_get_string(buffer, "input") or "",
            "input_pos": weechat.buffer_get_integer(buffer, "input_pos"),
        }
    except Exception:
        return weechat.WEECHAT_RC_OK
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True)
    if encoded == _last:
        return weechat.WEECHAT_RC_OK
    _last = encoded
    tmp = OUTPUT + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(encoded + "\n")
        os.replace(tmp, OUTPUT)
    except OSError:
        pass
    return weechat.WEECHAT_RC_OK


weechat.register("wt_inspect", "tests", "0.1", "BSD-3-Clause",
                 "Inspect the input bar for tests", "", "")
if OUTPUT:
    weechat.hook_timer(100, 0, 0, "tick", "")
