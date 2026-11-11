# -*- coding: utf-8 -*-
"""Tests for the --done marker used by the plugin to detect popup completion."""

import signal
import subprocess
import sys
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HELPER = ROOT / "weetranslate_popup.py"


def make_files(tmp_path):
    translation = tmp_path / "translation.txt"
    original = tmp_path / "original.txt"
    result = tmp_path / "result.txt"
    done = tmp_path / "done.txt"
    translation.write_text("Hello, how are you?\n", encoding="utf-8")
    original.write_text("Good morning\n", encoding="utf-8")
    return translation, original, result, done


def run_helper(tmp_path, key, extra_args=None, wait=10):
    translation, original, result, done = make_files(tmp_path)
    cmd = [
        sys.executable, str(HELPER),
        "--translation", str(translation),
        "--original", str(original),
        "--result", str(result),
        "--done", str(done),
        "--editor", "vi",
    ]
    if extra_args:
        cmd.extend(extra_args)
    proc = subprocess.run(
        cmd,
        input=(key + "\n").encode(),
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=wait,
    )
    return proc, result, done


def test_done_accepted(tmp_path):
    proc, result, done = run_helper(tmp_path, "y")
    assert proc.returncode == 0, proc.stderr.decode()
    assert done.read_text(encoding="utf-8").strip() == "accepted"
    assert result.read_text(encoding="utf-8").strip() == "Hello, how are you?"


def test_done_canceled(tmp_path):
    proc, result, done = run_helper(tmp_path, "q")
    assert proc.returncode == 2, proc.stderr.decode()
    assert done.read_text(encoding="utf-8").strip() == "canceled"
    assert not result.exists()


def test_done_after_edit(tmp_path):
    editor = tmp_path / "editor.sh"
    editor.write_text("#!/bin/sh\nprintf ' EDITED' >> \"$1\"\n", encoding="utf-8")
    editor.chmod(0o755)
    proc, result, done = run_helper(
        tmp_path, "n", extra_args=["--editor", str(editor)]
    )
    assert proc.returncode == 0, proc.stderr.decode()
    assert done.read_text(encoding="utf-8").strip() == "accepted"
    assert result.read_text(encoding="utf-8").strip().endswith("EDITED")


def test_done_on_sigterm(tmp_path):
    translation, original, result, done = make_files(tmp_path)
    started = tmp_path / "started.txt"
    cmd = [
        sys.executable, str(HELPER),
        "--translation", str(translation),
        "--original", str(original),
        "--result", str(result),
        "--done", str(done),
        "--started", str(started),
    ]
    proc = subprocess.Popen(
        cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )
    try:
        # started.txt is written AFTER the signal handlers are installed.
        deadline = time.time() + 5
        while time.time() < deadline and not started.exists():
            time.sleep(0.05)
        assert started.exists(), "the helper did not start"
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=5)
    finally:
        if proc.poll() is None:
            proc.kill()
    assert proc.returncode == 0
    assert done.read_text(encoding="utf-8").strip() == "accepted"
    assert result.read_text(encoding="utf-8").strip() == "Hello, how are you?"
