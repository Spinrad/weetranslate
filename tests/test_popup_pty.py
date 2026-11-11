# -*- coding: utf-8 -*-
"""Real TTY tests for the popup (termios, Escape vs arrows, SIGTERM)."""

import signal
import sys
from pathlib import Path

import pytest

pexpect = pytest.importorskip("pexpect")

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


def spawn(tmp_path, editor="vi"):
    translation, original, result, done = make_files(tmp_path)
    args = [
        str(HELPER),
        "--translation", str(translation),
        "--original", str(original),
        "--result", str(result),
        "--done", str(done),
        "--editor", editor,
    ]
    child = pexpect.spawn(
        sys.executable, args,
        encoding="utf-8",
        timeout=8,
        env={"TERM": "xterm", "PATH": "/usr/bin:/bin"},
    )
    child.expect("Choice>")
    return child, result, done


def finish(child):
    child.expect(pexpect.EOF)
    child.close()
    return child.exitstatus


def test_pty_accept_y(tmp_path):
    child, result, done = spawn(tmp_path)
    child.send("y")
    assert finish(child) == 0
    assert result.read_text(encoding="utf-8").strip() == "Hello, how are you?"
    assert done.read_text(encoding="utf-8").strip() == "accepted"


def test_pty_cancel_q(tmp_path):
    child, result, done = spawn(tmp_path)
    child.send("q")
    assert finish(child) == 2
    assert done.read_text(encoding="utf-8").strip() == "canceled"
    assert not result.exists()


def test_pty_arrow_does_not_cancel(tmp_path):
    child, result, _ = spawn(tmp_path)
    child.send("\x1b[A")  # up arrow
    child.expect("Choice>")  # prompt again: it did not cancel
    child.send("y")
    assert finish(child) == 0
    assert result.exists()


def test_pty_escape_cancels(tmp_path):
    child, _, done = spawn(tmp_path)
    child.send("\x1b")
    assert finish(child) == 2
    assert done.read_text(encoding="utf-8").strip() == "canceled"


def test_pty_sigterm_accepts(tmp_path):
    child, result, done = spawn(tmp_path)
    child.kill(signal.SIGTERM)
    assert finish(child) == 0
    assert result.read_text(encoding="utf-8").strip() == "Hello, how are you?"
    assert done.read_text(encoding="utf-8").strip() == "accepted"
