#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Standalone terminal UI for the WeeTranslate plugin.

The plugin launches this helper in an external window (xterm, kitty, ...).
It displays translations and offers:

    y / Enter : accept the displayed translation
    n / e     : edit the current language with the configured editor
    1 / 2 / 3 : switch language
    q / Esc   : cancel

The accepted result is written atomically to ``--result``; the plugin reads it
and inserts it into the input bar.

No external dependencies. Python 3.8+.
"""

from __future__ import annotations

import argparse
import json
import os
import select
import shlex
import shutil
import signal
import subprocess
import sys
import textwrap
from typing import List, Optional, Sequence

EXIT_ACCEPT = 0
EXIT_ERROR = 1
EXIT_CANCEL = 2
EXIT_RETRANSLATE = 3

ACCEPT_KEYS = frozenset(("y", "Y", "\n", "\r"))
EDIT_KEYS = frozenset(("n", "N", "e", "E"))
CANCEL_KEYS = frozenset(("q", "Q", "\x1b", "\x03"))
LANGUAGE_KEYS = {"1": "en", "2": "de", "3": "es", "4": "ru", "5": "custom"}
LANGUAGES = {"en": "English", "de": "German", "es": "Spanish", "ru": "Russian"}
LANGUAGE_ORDER = ("en", "de", "es", "ru")

_LABEL_COLOR = "\x1b[36m"
_TITLE_COLOR = "\x1b[1;36m"
_HELP_COLOR = "\x1b[2m"
_RESET = "\x1b[0m"


# ---------------------------------------------------------------------------
# Utility functions (testable without a terminal)
# ---------------------------------------------------------------------------

def normalize_for_result(text: str) -> str:
    """Normalize accepted text for a single-line IRC input.

    - neutralize control characters (C0, C1, DEL) and bidi controls;
    - strip leading and trailing whitespace;
    - collapse whitespace runs (including newlines) to one space.
    """
    bidi = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f")
    text = "".join(
        char if (char >= " " and not ("\x7f" <= char <= "\x9f")
                 and char not in bidi) else " "
        for char in text
    )
    return " ".join(text.split())


def sanitize_for_display(text: str) -> str:
    """Neutralize control characters for terminal display.

    Newlines are kept; other control characters (ANSI sequences, C0/C1, \r,
    \t, bidi controls, ...) are replaced with a space so a translation cannot
    drive the terminal.
    """
    text = text.expandtabs(4)
    bidi = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f")
    cleaned = []
    for char in text:
        if char == "\n":
            cleaned.append(char)
        elif char in bidi:
            cleaned.append(" ")
        elif ord(char) < 0x20 or 0x7f <= ord(char) <= 0x9f:
            cleaned.append(" ")
        else:
            cleaned.append(char)
    return "".join(cleaned)


def wrap_text(text: str, width: int, max_lines: Optional[int] = None) -> List[str]:
    """Wrap ``text`` to ``width`` columns, optionally truncating to ``max_lines``."""
    width = max(1, width)
    lines: List[str] = []
    for paragraph in text.split("\n"):
        paragraph = paragraph.rstrip()
        if not paragraph:
            lines.append("")
            continue
        wrapped = textwrap.wrap(
            paragraph,
            width=width,
            break_long_words=True,
            break_on_hyphens=False,
        )
        lines.extend(wrapped or [""])

    while lines and lines[0] == "":
        lines.pop(0)
    while lines and lines[-1] == "":
        lines.pop()

    if max_lines is not None and len(lines) > max_lines:
        lines = lines[:max_lines]
        last = lines[-1]
        if len(last) > width - 3:
            last = last[: max(0, width - 3)].rstrip()
        lines[-1] = last + "..."

    return lines or [""]


def terminal_width(fallback: int = 80, maximum: int = 100) -> int:
    """Return a readable frame width adapted to the terminal (fallback 80)."""
    try:
        columns = shutil.get_terminal_size(fallback=(fallback, 24)).columns
    except (OSError, ValueError):
        columns = fallback
    if not columns or columns <= 0:
        columns = fallback
    return max(40, min(columns, maximum))


def supports_color(stream=None) -> bool:
    """True only on a real terminal that is not ``TERM=dumb``."""
    stream = sys.stdout if stream is None else stream
    try:
        is_tty = bool(stream.isatty())
    except (AttributeError, OSError):
        is_tty = False
    return is_tty and os.environ.get("TERM", "") != "dumb"


def _truncate(text: str, width: int) -> str:
    if width <= 0:
        return ""
    if len(text) <= width:
        return text
    if width <= 3:
        return text[:width]
    return text[: width - 3] + "..."


def _row(text: str, inner: int, left: str = "|", right: str = "|",
         color: str = "", use_color: bool = False) -> str:
    text = text[:inner]
    body = text + " " * max(0, inner - len(text))
    if use_color and color:
        body = color + body + _RESET
    return left + " " + body + " " + right


def _label_rows(label: str, text: str, inner: int, max_lines: Optional[int] = None) -> List[str]:
    """Wrap ``text`` under ``label`` and indent continuation lines."""
    prefix = label
    available = max(5, inner - len(prefix))
    wrapped = wrap_text(text, available, max_lines=max_lines)
    rows = []
    for index, chunk in enumerate(wrapped):
        lead = prefix if index == 0 else " " * len(prefix)
        rows.append(lead + chunk)
    return rows


def language_label(code: str, custom_language: Optional[str] = None) -> str:
    if code == "custom":
        label = normalize_for_result(custom_language or "Other")
        return label if len(label) <= 32 else label[:29] + "..."
    return LANGUAGES.get(code, code)


def build_frame(
    translations: dict,
    current_lang: str,
    original: Optional[str] = None,
    custom_language: Optional[str] = None,
    editor: str = "vi",
    width: int = 80,
    use_color: bool = False,
) -> List[str]:
    """Build the popup frame with a language selector."""
    width = max(40, width)
    inner = width - 4
    border = "+" + "-" * (width - 2) + "+"

    lines: List[str] = [border]
    lines.append(_row("WeeTranslate", inner, color=_TITLE_COLOR,
                      use_color=use_color))
    lines.append(border)

    if original is not None:
        original_text = sanitize_for_display(original).strip() or "(empty)"
        for row in _label_rows("Original: ", original_text, inner, max_lines=6):
            lines.append(_row(row, inner, color=_LABEL_COLOR, use_color=use_color))
        lines.append(_row("", inner))

    lang_label = language_label(current_lang, custom_language)
    translation_text = sanitize_for_display(
        translations.get(current_lang, "")
    ).strip() or "(empty)"
    for row in _label_rows("Translation (%s): " % lang_label, translation_text, inner):
        lines.append(_row(row, inner, color=_LABEL_COLOR, use_color=use_color))

    lines.append(border)

    choices = []
    for index, code in enumerate(LANGUAGE_ORDER):
        if code in translations:
            choices.append("%d %s%s" % (
                index + 1, LANGUAGES[code],
                "*" if code == current_lang else "",
            ))
    custom_label = _truncate(normalize_for_result(custom_language or "Other..."), 32)
    choices.append("5 %s%s" % (
        custom_label, "*" if current_lang == "custom" else "",
    ))
    selector = "Language:  " + "   ".join(choices)
    for row in wrap_text(selector, inner):
        lines.append(_row(row, inner, color=_HELP_COLOR, use_color=use_color))

    help_line = "[y] accept   [n] edit (%s)   [q] cancel" % editor
    for row in wrap_text(help_line, inner):
        lines.append(_row(row, inner, color=_HELP_COLOR, use_color=use_color))
    lines.append(border)
    return lines


def write_result_atomic(result_path: str, content: str) -> None:
    """Atomically write ``content`` to ``result_path`` (tmp file + os.replace)."""
    tmp_path = "%s.tmp.%d" % (result_path, os.getpid())
    try:
        with open(tmp_path, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(content)
            handle.flush()
            try:
                os.fsync(handle.fileno())
            except OSError:
                pass
        os.replace(tmp_path, result_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.unlink(tmp_path)
        except OSError:
            pass


def write_done(done_path: Optional[str], status: str) -> None:
    """Write the completion marker (accepted / canceled / error), if requested.

    The plugin watches this file instead of assuming the terminal returns when
    it closes (gnome-terminal, for example, detaches immediately).
    """
    if not done_path:
        return
    try:
        write_result_atomic(done_path, status + "\n")
    except OSError as exc:
        print(
            "WeeTranslate: unable to write state %s: %s" % (done_path, exc),
            file=sys.stderr,
        )
        sys.stderr.flush()


def write_started_file(started_path: Optional[str]) -> None:
    """Marker indicating that the helper started."""
    if not started_path:
        return
    try:
        write_result_atomic(started_path, "started\n")
    except OSError as exc:
        print("WeeTranslate: unable to write started: %s" % exc,
              file=sys.stderr)
        sys.stderr.flush()


def write_pid_file(pid_path: Optional[str]) -> None:
    """Write the helper PID so WeeChat can close it on /close."""
    if not pid_path:
        return
    try:
        write_result_atomic(pid_path, str(os.getpid()) + "\n")
        os.chmod(pid_path, 0o600)
    except OSError as exc:
        print("WeeTranslate: unable to write PID: %s" % exc, file=sys.stderr)
        sys.stderr.flush()


def read_text_file(path: str) -> str:
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        return handle.read()


# ---------------------------------------------------------------------------
# Keyboard input
# ---------------------------------------------------------------------------

def read_key() -> Optional[str]:
    """Read a single key, or a line's first non-blank char when not a TTY.

    Returns ``None`` on EOF.  When stdin is a TTY, termios cbreak mode is always
    restored, including when a signal interrupts the read.
    """
    if sys.stdin.isatty():
        return _read_key_tty()
    return _read_key_pipe()


def _read_key_tty() -> Optional[str]:
    import termios  # POSIX only, imported lazily

    fd = sys.stdin.fileno()
    old_settings = termios.tcgetattr(fd)
    data = b""
    try:
        # cbreak without ECHO/ICANON, and without ISIG: Ctrl-C is delivered as
        # a literal \x03 byte so that it is handled deterministically instead of
        # racing with Python's KeyboardInterrupt handling.
        mode = termios.tcgetattr(fd)
        mode[3] &= ~(termios.ECHO | termios.ICANON | termios.ISIG)
        mode[6][termios.VMIN] = 1
        mode[6][termios.VTIME] = 0
        termios.tcsetattr(fd, termios.TCSANOW, mode)
        try:
            data = os.read(fd, 1)
            if data == b"\x1b":
                # Distinguish a lone Escape (cancel) from an arrow/function
                # key sequence (ESC [ A, ESC O P, ...).
                ready, _, _ = select.select([fd], [], [], 0.05)
                if ready:
                    data += os.read(fd, 16)
        except (InterruptedError, OSError):
            data = b""
    finally:
        try:
            termios.tcsetattr(fd, termios.TCSADRAIN, old_settings)
        except (OSError, termios.error):
            # The terminal may already be gone (window closed): never mask the
            # signal handler's SystemExit with a restore error.
            pass

    if not data:
        return None
    return data.decode("utf-8", "ignore")


def _read_key_pipe() -> Optional[str]:
    line = sys.stdin.readline()
    if line == "":
        return None
    stripped = line.strip()
    if not stripped:
        # Empty line: equivalent to pressing Enter.
        return "\n"
    return stripped[0]


# ---------------------------------------------------------------------------
# Display
# ---------------------------------------------------------------------------

def _clear_screen(use_color: bool) -> None:
    if use_color:
        sys.stdout.write("\x1b[2J\x1b[H")
    else:
        # TERM=dumb or piped stdout: no escape sequence.
        sys.stdout.write("\n")


def show_frame(
    translations: dict,
    current_lang: str,
    original: Optional[str],
    custom_language: Optional[str],
    editor: str,
    width: int,
) -> None:
    use_color = supports_color()
    _clear_screen(use_color)
    for line in build_frame(
        translations,
        current_lang,
        original,
        custom_language=custom_language,
        editor=editor,
        width=width,
        use_color=use_color,
    ):
        sys.stdout.write(line + "\n")
    sys.stdout.write("Choice> ")
    sys.stdout.flush()


def launch_editor(editor: str, translation_path: str) -> bool:
    """Launch the editor on the translation file. Return True on success."""
    try:
        argv = shlex.split(editor)
    except ValueError as exc:
        print("WeeTranslate: invalid editor command: %s" % exc, file=sys.stderr)
        sys.stderr.flush()
        return False

    if not argv:
        print("WeeTranslate: no editor configured.", file=sys.stderr)
        sys.stderr.flush()
        return False

    try:
        subprocess.run(argv + [translation_path], check=False)
    except FileNotFoundError:
        print(
            "WeeTranslate: editor not found: %s" % argv[0],
            file=sys.stderr,
        )
        sys.stderr.flush()
        return False
    except OSError as exc:
        print(
            "WeeTranslate: unable to launch editor %s: %s" % (argv[0], exc),
            file=sys.stderr,
        )
        sys.stderr.flush()
        return False
    return True


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def install_signal_handlers(translation_path: str, result_path: str,
                            done_path: Optional[str] = None) -> None:
    """Closing the window (SIGHUP/SIGTERM) accepts the displayed translation."""

    def handler(signum, frame):  # noqa: ARG001 - required signature
        try:
            current = read_text_file(translation_path)
        except OSError:
            current = ""
        normalized = normalize_for_result(current)
        if normalized:
            try:
                write_result_atomic(result_path, normalized)
                write_done(done_path, "accepted")
            except OSError as exc:
                print(
                    "WeeTranslate: unable to write result: %s" % exc,
                    file=sys.stderr,
                )
                sys.stderr.flush()
                write_done(done_path, "error")
                sys.exit(EXIT_ERROR)
        else:
            write_done(done_path, "canceled")
        sys.exit(EXIT_ACCEPT)

    for name in ("SIGHUP", "SIGTERM"):
        sig = getattr(signal, name, None)
        if sig is None:
            continue
        try:
            signal.signal(sig, handler)
        except (ValueError, OSError, RuntimeError):
            # Not in main thread (should not happen) or unsupported.
            continue


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def _accept(translation: str, result_path: str) -> int:
    normalized = normalize_for_result(translation)
    try:
        write_result_atomic(result_path, normalized)
    except OSError as exc:
        print(
            "WeeTranslate: unable to write result: %s" % exc,
            file=sys.stderr,
        )
        sys.stderr.flush()
        return EXIT_ERROR
    return EXIT_ACCEPT


def _configure_streams() -> None:
    """Avoid a crash if the terminal encoding cannot represent a character."""
    for stream in (sys.stdout, sys.stderr):
        try:
            stream.reconfigure(errors="replace")
        except (AttributeError, OSError, ValueError):
            pass


def load_translations(args: argparse.Namespace):
    """Load multilingual translations (JSON) or a single translation."""
    translations = {}
    if args.translations:
        data = json.loads(read_text_file(args.translations))
        if not isinstance(data, dict):
            raise ValueError("the translations file is not a JSON object")
        for code in LANGUAGE_ORDER + ("custom",):
            value = data.get(code)
            if isinstance(value, str):
                normalized = normalize_for_result(value)
                if normalized:
                    translations[code] = normalized
    elif args.translation:
        normalized = normalize_for_result(read_text_file(args.translation))
        if normalized:
            code = args.default_lang if args.default_lang in LANGUAGES else "en"
            translations[code] = normalized

    if not translations:
        raise ValueError("no translation available")
    if args.default_lang in translations:
        current = args.default_lang
    else:
        current = next(code for code in LANGUAGE_ORDER if code in translations)
    return translations, current


def write_current_translation(path: str, text: str) -> None:
    write_result_atomic(path, normalize_for_result(text) + "\n")


def prompt_custom_language() -> str:
    """Read a free-form target language name from the user."""
    sys.stdout.write("Language name (empty to cancel): ")
    sys.stdout.flush()
    try:
        line = sys.stdin.readline()
    except (KeyboardInterrupt, EOFError):
        return ""
    if line == "":
        return ""
    return normalize_for_result(line)[:80]


def run(args: argparse.Namespace) -> int:
    try:
        translations, current_lang = load_translations(args)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        print("WeeTranslate: unreadable translations: %s" % exc, file=sys.stderr)
        return EXIT_ERROR

    edit_path = args.translation
    if not edit_path:
        base = os.path.dirname(args.translations) if args.translations else os.getcwd()
        edit_path = os.path.join(base, "translation.txt")
    write_current_translation(edit_path, translations[current_lang])
    install_signal_handlers(edit_path, args.result, args.done)

    original: Optional[str] = None
    if not args.no_original:
        try:
            original = read_text_file(args.original)
        except OSError as exc:
            print(
                "WeeTranslate: unable to read original %s: %s"
                % (args.original, exc),
                file=sys.stderr,
            )
            print("WeeTranslate: original text will not be displayed.", file=sys.stderr)

    write_pid_file(args.pid_file)
    write_started_file(args.started)

    width = terminal_width()
    while True:
        show_frame(
            translations=translations,
            current_lang=current_lang,
            original=original,
            custom_language=args.custom_language,
            editor=args.editor,
            width=width,
        )

        try:
            key = read_key()
        except KeyboardInterrupt:
            # Ctrl-C arriving before ISIG is disabled (or SIGINT sent by another
            # process): still a cancellation.
            sys.stdout.write("\n")
            sys.stdout.flush()
            return EXIT_CANCEL

        sys.stdout.write("\n")
        sys.stdout.flush()

        if key is None:
            print("WeeTranslate: input closed, canceling.", file=sys.stderr)
            return EXIT_CANCEL

        if key in ACCEPT_KEYS:
            return _accept(translations[current_lang], args.result)

        if key in CANCEL_KEYS:
            return EXIT_CANCEL

        if key in LANGUAGE_KEYS:
            target = LANGUAGE_KEYS[key]
            if target == "custom" and "custom" not in translations:
                language = prompt_custom_language()
                if not language:
                    continue
                language_file = os.path.join(
                    os.path.dirname(edit_path), "custom-language.txt"
                )
                write_result_atomic(language_file, language + "\n")
                return EXIT_RETRANSLATE
            if target in translations:
                current_lang = target
                write_current_translation(edit_path, translations[current_lang])
            else:
                print("WeeTranslate: language unavailable (%s)."
                      % language_label(target, args.custom_language),
                      file=sys.stderr)
                sys.stderr.flush()
            continue

        if key in EDIT_KEYS:
            if not launch_editor(args.editor, edit_path):
                continue
            try:
                edited = normalize_for_result(read_text_file(edit_path))
            except OSError as exc:
                print(
                    "WeeTranslate: unable to read translation back: %s" % exc,
                    file=sys.stderr,
                )
                continue
            if not edited:
                print(
                    "WeeTranslate: translation is empty after editing "
                    "and cannot be accepted.",
                    file=sys.stderr,
                )
                sys.stderr.flush()
                continue
            translations[current_lang] = edited
            return _accept(edited, args.result)

        print(
            "WeeTranslate: unknown key (%r)."
            % (key if key not in ("\x1b", "\x03") else "Escape"),
            file=sys.stderr,
        )
        sys.stderr.flush()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="weetranslate_popup.py",
        description="Display a translation and let the user accept or edit it.",
    )
    parser.add_argument("--translation", default=None,
                        help="edit file for the current translation")
    parser.add_argument("--translations", default=None,
                        help="JSON file {en,de,es} with translations")
    parser.add_argument("--original", default=None, help="UTF-8 source text file")
    parser.add_argument("--result", required=True, help="file where the accepted text is written")
    parser.add_argument("--editor", default="vi", help="editor command (default: vi)")
    parser.add_argument("--default-lang",
                        choices=("en", "de", "es", "ru", "custom"), default="en",
                        help="initially selected language")
    parser.add_argument("--custom-language", default=None,
                        help="display name for the custom language")
    parser.add_argument(
        "--no-original",
        action="store_true",
        help="do not display the source text",
    )
    parser.add_argument(
        "--done",
        default=None,
        help="completion marker written at the end (accepted/canceled/error)",
    )
    parser.add_argument(
        "--pid-file",
        default=None,
        help="file for the helper PID (used by /close)",
    )
    parser.add_argument(
        "--started",
        default=None,
        help="marker written when the helper starts",
    )
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    _configure_streams()
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.translation and not args.translations:
        parser.error("--translation or --translations is required")
    if not args.no_original and not args.original:
        parser.error("--original is required unless --no-original is used")
    try:
        rc = run(args)
    except Exception:
        write_done(args.done, "error")
        raise
    status = {
        EXIT_ACCEPT: "accepted",
        EXIT_CANCEL: "canceled",
        EXIT_RETRANSLATE: "retranslate",
    }.get(rc, "error")
    write_done(args.done, status)
    return rc


if __name__ == "__main__":
    sys.exit(main())
