#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Unit tests for the WeeTranslate terminal helper (weetranslate_popup.py)."""

from __future__ import annotations

import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
HELPER = REPO_ROOT / "weetranslate_popup.py"


class PopupTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tempdir = tempfile.mkdtemp(prefix="weetranslate-popup-")
        self.addCleanup(shutil.rmtree, self.tempdir, ignore_errors=True)
        self.tmp = Path(self.tempdir)
        self.translation = self.tmp / "translation.txt"
        self.original = self.tmp / "original.txt"
        self.result = self.tmp / "result.txt"
        self.translation.write_text("Hello world", encoding="utf-8")
        self.original.write_text("Good morning world", encoding="utf-8")

    # -- helpers -----------------------------------------------------------

    def make_args(self, *extra: str, no_original: bool = False) -> list:
        args = [
            sys.executable,
            str(HELPER),
            "--translation",
            str(self.translation),
            "--result",
            str(self.result),
            "--editor",
            "vi",
            "--default-lang",
            "en",
        ]
        if no_original:
            args.append("--no-original")
        else:
            args.extend(["--original", str(self.original)])
        args.extend(extra)
        return args

    def run_helper(self, input_bytes: bytes, *extra: str, no_original: bool = False,
                   env=None, timeout: float = 10.0):
        run_env = dict(os.environ)
        run_env.pop("COLUMNS", None)
        run_env.pop("LINES", None)
        if env:
            run_env.update(env)
        return subprocess.run(
            self.make_args(*extra, no_original=no_original),
            input=input_bytes,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=timeout,
            env=run_env,
        )

    def assert_no_tmp_leftovers(self) -> None:
        leftovers = list(self.tmp.glob(self.result.name + ".tmp.*"))
        self.assertEqual(leftovers, [], "leftover temporary files: %r" % leftovers)

    def wait_for_prompt(self, proc: subprocess.Popen, timeout: float = 10.0) -> bytes:
        """Read stdout until the prompt is displayed."""
        deadline = time.monotonic() + timeout
        data = b""
        while b"Choice> " not in data:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise AssertionError("prompt not displayed before timeout: %r" % data)
            ready, _, _ = select.select([proc.stdout], [], [], remaining)
            if not ready:
                raise AssertionError("prompt not displayed before timeout: %r" % data)
            chunk = os.read(proc.stdout.fileno(), 4096)
            if not chunk:
                raise AssertionError(
                    "process exited before the prompt (code=%r): %r"
                    % (proc.poll(), data)
                )
            data += chunk
        return data

    # -- core tests --------------------------------------------------------

    def test_accept_with_y_writes_result_and_exits_0(self) -> None:
        proc = self.run_helper(b"y\n")

        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertTrue(self.result.exists(), "the result file must exist")
        self.assertEqual(self.result.read_text(encoding="utf-8"), "Hello world")
        self.assertIn(b"WeeTranslate", proc.stdout)
        self.assertIn(b"[y] accept", proc.stdout)
        self.assertIn(b"[q] cancel", proc.stdout)
        self.assertIn("Good morning world".encode("utf-8"), proc.stdout)
        self.assert_no_tmp_leftovers()

    def test_cancel_with_q_writes_nothing_and_exits_2(self) -> None:
        proc = self.run_helper(b"q\n")

        self.assertEqual(proc.returncode, 2, proc.stderr.decode("utf-8", "replace"))
        self.assertFalse(self.result.exists(), "no result must be written")
        self.assert_no_tmp_leftovers()

    def test_edit_with_n_runs_editor_and_accepts_modified_text(self) -> None:
        editor = self.tmp / "fake_editor.py"
        editor.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "path = sys.argv[-1]\n"
            "with open(path, 'a', encoding='utf-8') as handle:\n"
            "    handle.write(' EDITED')\n",
            encoding="utf-8",
        )
        editor.chmod(0o755)
        editor_cmd = "%s %s" % (shlex.quote(sys.executable), shlex.quote(str(editor)))

        proc = self.run_helper(b"n\n", "--editor", editor_cmd)

        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(self.result.read_text(encoding="utf-8"), "Hello world EDITED")
        self.assertEqual(
            " ".join(self.translation.read_text(encoding="utf-8").split()),
            "Hello world EDITED",
        )
        self.assert_no_tmp_leftovers()

    def test_sigterm_accepts_current_translation(self) -> None:
        run_env = dict(os.environ)
        run_env.pop("COLUMNS", None)
        proc = subprocess.Popen(
            self.make_args(),
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=run_env,
        )
        try:
            self.wait_for_prompt(proc)
            proc.send_signal(signal.SIGTERM)
            stdout, stderr = proc.communicate(timeout=10)
        finally:
            if proc.poll() is None:
                proc.kill()
                proc.communicate()

        self.assertEqual(proc.returncode, 0, stderr.decode("utf-8", "replace"))
        self.assertTrue(self.result.exists(), "SIGTERM must accept the translation")
        self.assertEqual(self.result.read_text(encoding="utf-8"), "Hello world")
        self.assert_no_tmp_leftovers()

    def test_no_original_does_not_crash(self) -> None:
        proc = self.run_helper(b"y\n", no_original=True)

        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(self.result.read_text(encoding="utf-8"), "Hello world")
        self.assertNotIn(b"Original:", proc.stdout)

    def test_atomic_write_leaves_no_tmp_file(self) -> None:
        proc = self.run_helper(b"y\n")

        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(
            list(self.tmp.glob(self.result.name + ".tmp.*")),
            [],
            "os.replace must remove the temporary file",
        )

    # -- additional coverage ----------------------------------------------

    def test_enter_accepts(self) -> None:
        proc = self.run_helper(b"\n")

        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(self.result.read_text(encoding="utf-8"), "Hello world")

    def test_result_is_normalized_to_one_line(self) -> None:
        self.translation.write_text("Hello\n  world\tagain\n", encoding="utf-8")

        proc = self.run_helper(b"y\n")

        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(self.result.read_text(encoding="utf-8"), "Hello world again")

    def test_unknown_key_reprompts_before_accepting(self) -> None:
        proc = self.run_helper(b"x\ny\n")

        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(self.result.read_text(encoding="utf-8"), "Hello world")
        self.assertIn("unknown key", proc.stderr.decode("utf-8", "replace"))

    def test_missing_editor_reprompts_without_crashing(self) -> None:
        missing = str(self.tmp / "does-not-exist-editor")
        proc = self.run_helper(b"n\nq\n", "--editor", missing)

        self.assertEqual(proc.returncode, 2, proc.stderr.decode("utf-8", "replace"))
        self.assertFalse(self.result.exists())
        self.assertIn("not found", proc.stderr.decode("utf-8", "replace"))

    def test_editor_leaving_empty_file_reprompts_then_cancels(self) -> None:
        editor = self.tmp / "empty_editor.py"
        editor.write_text(
            "#!/usr/bin/env python3\n"
            "import sys\n"
            "open(sys.argv[-1], 'w', encoding='utf-8').close()\n",
            encoding="utf-8",
        )
        editor.chmod(0o755)
        editor_cmd = "%s %s" % (shlex.quote(sys.executable), shlex.quote(str(editor)))

        proc = self.run_helper(b"n\nq\n", "--editor", editor_cmd)

        self.assertEqual(proc.returncode, 2, proc.stderr.decode("utf-8", "replace"))
        self.assertFalse(self.result.exists())
        self.assertIn("empty", proc.stderr.decode("utf-8", "replace"))

    def test_language_selector_accepts_german(self) -> None:
        translations = self.tmp / "translations.json"
        translations.write_text(
            '{"en": "Hello world", "de": "Hallo Welt", "es": "Hola mundo"}',
            encoding="utf-8",
        )
        proc = self.run_helper(b"2\ny\n", "--translations", str(translations))

        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(self.result.read_text(encoding="utf-8"), "Hallo Welt")
        self.assertIn("2 German*".encode("utf-8"), proc.stdout)

    def test_language_selector_accepts_russian(self) -> None:
        translations = self.tmp / "translations.json"
        translations.write_text(
            '{"en": "Hello world", "de": "Hallo Welt", '
            '"es": "Hola mundo", "ru": "Привет, мир"}',
            encoding="utf-8",
        )
        proc = self.run_helper(b"4\ny\n", "--translations", str(translations))

        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(self.result.read_text(encoding="utf-8"), "Привет, мир")
        self.assertIn("4 Russian*".encode("utf-8"), proc.stdout)

    def test_custom_language_request(self) -> None:
        translations = self.tmp / "translations.json"
        translations.write_text(
            '{"en": "Hello world", "de": "Hallo Welt", '
            '"es": "Hola mundo", "ru": "Привет, мир"}',
            encoding="utf-8",
        )
        done = self.tmp / "done.txt"
        proc = self.run_helper(
            b"5\nItalian\n",
            "--translations", str(translations),
            "--done", str(done),
        )

        self.assertEqual(proc.returncode, 3, proc.stderr.decode("utf-8", "replace"))
        self.assertEqual(done.read_text(encoding="utf-8").strip(), "retranslate")
        self.assertEqual(
            (self.tmp / "custom-language.txt").read_text(encoding="utf-8").strip(),
            "Italian",
        )

    def test_bidi_controls_are_removed(self) -> None:
        self.translation.write_text(
            "Hello \u202e world \u2066!", encoding="utf-8"
        )
        proc = self.run_helper(b"y\n")

        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        result = self.result.read_text(encoding="utf-8")
        self.assertNotIn("\u202e", result)
        self.assertNotIn("\u2066", result)
        self.assertEqual(result, "Hello world !")

    def test_dumb_term_has_no_ansi_escape(self) -> None:
        proc = self.run_helper(b"y\n", env={"TERM": "dumb"})

        self.assertEqual(proc.returncode, 0, proc.stderr.decode("utf-8", "replace"))
        self.assertNotIn(b"\x1b", proc.stdout)

    def test_escape_cancels(self) -> None:
        proc = self.run_helper(b"\x1b\n")

        self.assertEqual(proc.returncode, 2, proc.stderr.decode("utf-8", "replace"))
        self.assertFalse(self.result.exists())


if __name__ == "__main__":
    unittest.main(verbosity=2)
