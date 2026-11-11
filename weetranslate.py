# -*- coding: utf-8 -*-
"""
WeeTranslate — LLM-backed WeeChat translation plugin.

Commands:
    /trad <text>     translate text and open the validation popup
    /trad            translate the current input bar
    /trad help       show help
    /trad config     open the fset configuration menu
    /trad config show  print the effective configuration
    /trad cancel     cancel pending translations

Configuration:
    /set weetranslate.main.provider deepseek  (or openai, custom)
    /set weetranslate.main.api_key sk-...
    /set weetranslate.main.editor auto        ($VISUAL, $EDITOR, otherwise vi)
    /set weetranslate.main.terminal auto      (tmux is an explicit option)
    /set weetranslate.main.terminal_cmd "..."  (when terminal = custom)

WeeChat >= 4.1 is required for hook_url. The plugin never spawns threads and
never blocks WeeChat: HTTP uses hook_url, the popup uses hook_process_hashtable.
"""

from __future__ import annotations

import json
import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import time

import weechat

SCRIPT_NAME = "weetranslate"
SCRIPT_VERSION = "0.5.1"
CONFIG_NAME = "weetranslate"

CONFIG = None
JOBS = {}
JOB_SEQ = 0
HELPER_PATH = None

PROVIDERS = {
    "deepseek": {
        "label": "DeepSeek",
        "api_url": "https://api.deepseek.com/chat/completions",
        "model": "deepseek-chat",
    },
    "openai": {
        "label": "OpenAI",
        "api_url": "https://api.openai.com/v1/chat/completions",
        "model": "gpt-4o-mini",
    },
    "custom": {
        "label": "Custom",
        "api_url": "",
        "model": "",
    },
}

LANGUAGE_ORDER = ("en", "de", "es", "ru")
TRANSLATION_PROMPT = (
    "You are a professional translator. Translate the user's message into "
    "English, German, Spanish and Russian. Return ONLY a valid JSON object with "
    "the keys \"en\", \"de\", \"es\" and \"ru\"; each value is the "
    "translation as a single line. Do not add markdown, comments or explanations."
)

TERMINAL_SPECS = {
    "xterm": ("xterm", ["-T", "{title}", "-e"]),
    "kitty": ("kitty", ["--title", "{title}", "-e"]),
    "gnome-terminal": ("gnome-terminal", ["--title={title}", "--"]),
    "konsole": ("konsole", ["-p", "tabtitle={title}", "-e"]),
    "xfce4-terminal": ("xfce4-terminal", ["--title={title}", "-x"]),
    "x-terminal-emulator": ("x-terminal-emulator", ["-T", "{title}", "-e"]),
    "alacritty": ("alacritty", ["--title", "{title}", "-e"]),
    "urxvt": ("urxvt", ["-title", "{title}", "-e"]),
}
AUTO_TERMINALS = [
    "xterm",
    "x-terminal-emulator",
    "kitty",
    "alacritty",
    "urxvt",
    "gnome-terminal",
    "konsole",
    "xfce4-terminal",
]


# ---------------------------------------------------------------------------
# utilities
# ---------------------------------------------------------------------------

def _color(text, color):
    """Colorize text when colors are available."""
    try:
        return weechat.color(color) + text + weechat.color("reset")
    except Exception:
        return text


def prnt(buffer, message, color=""):
    """Print a prefixed message to a buffer (explicit help/config only)."""
    prefix = _color("[weetranslate]", "yellow")
    if color:
        message = _color(message, color)
    weechat.prnt(buffer, "%s %s" % (prefix, message))


def notify(message, color=""):
    """Notify in the core buffer without polluting the channel."""
    prefix = _color("[weetranslate]", "yellow")
    if color:
        message = _color(message, color)
    weechat.prnt("", "%s %s" % (prefix, message))


def debug(message):
    if opt_bool("debug"):
        message = normalize_single_line(message)
        prefix = _color("[weetranslate debug]", "darkgray")
        weechat.prnt("", "%s %s" % (prefix, message))


def opt_ptr(name):
    return weechat.config_get("%s.main.%s" % (CONFIG_NAME, name))


def opt_str(name, default=""):
    ptr = opt_ptr(name)
    if not ptr:
        return default
    try:
        value = weechat.config_string(ptr)
    except Exception:
        return default
    return value if value else default


def opt_int(name, default=0):
    ptr = opt_ptr(name)
    if not ptr:
        return default
    try:
        return int(weechat.config_integer(ptr))
    except Exception:
        return default


def opt_bool(name, default=False):
    ptr = opt_ptr(name)
    if not ptr:
        return default
    try:
        return bool(weechat.config_boolean(ptr))
    except Exception:
        return default


def expand_config_value(value):
    """Expand WeeChat variables, e.g. ${sec.data.deepseek_key}."""
    if not value:
        return ""
    if "${" not in value:
        return value
    try:
        expanded = weechat.string_eval_expression(value, {}, {}, "")
    except Exception:
        return ""
    if not expanded:
        return ""
    expanded = str(expanded).strip()
    # If the secret does not exist, WeeChat may return the literal expression:
    # do not use it as an API key (it would produce a misleading 401).
    if "${sec.data." in expanded:
        return ""
    return expanded


def normalize_single_line(text):
    """Flatten a multi-line translation into a single IRC line.

    Control characters (including WeeChat color code \x19) are neutralized:
    an API response must not be able to drive the terminal or the buffer.
    """
    if not text:
        return ""
    text = str(text)
    bidi = set("\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u200e\u200f")
    text = "".join(
        char if (ord(char) >= 0x20 and not 0x7f <= ord(char) <= 0x9f
                 and char not in bidi) else " "
        for char in text
    )
    return " ".join(text.split()).strip()


def mask_secret(value):
    if not value:
        return "(not configured)"
    return "(configured, %d characters, masked)" % len(value)


def python_executable():
    """Return a real Python interpreter (sys.executable may be weechat)."""
    for candidate in ("python3", "python"):
        found = shutil.which(candidate)
        if found:
            return found
    exe = sys.executable or ""
    if exe and "python" in os.path.basename(exe).lower():
        return exe
    for candidate in ("/usr/local/bin/python3", "/usr/bin/python3"):
        if os.path.exists(candidate):
            return candidate
    return "python3"


def _helper_search_dirs():
    """Directories where the popup helper may live, most specific first."""
    dirs = []

    def add(path):
        if not path:
            return
        try:
            absolute = os.path.abspath(os.path.expanduser(path))
            real = os.path.realpath(absolute)
        except Exception:
            return
        for directory in (absolute, real):
            if directory and directory not in dirs:
                dirs.append(directory)

    try:
        data_dir = weechat.info_get("weechat_data_dir", "") or ""
    except Exception:
        data_dir = ""
    try:
        script_path = weechat.info_get("script_path", SCRIPT_NAME) or ""
    except Exception:
        script_path = ""

    # Prefer the helper next to the loaded script, using only absolute paths.
    # On some systems __file__ is just "weetranslate.py"; that must never make
    # us search the current directory or $HOME. The installed data directory is
    # the fallback when the script path is unavailable.
    if script_path and os.path.isabs(script_path):
        add(os.path.dirname(script_path))
        add(os.path.dirname(os.path.realpath(script_path)))

    raw_file = globals().get("__file__") or ""
    if raw_file and os.path.isabs(raw_file):
        add(os.path.dirname(raw_file))
        add(os.path.dirname(os.path.realpath(raw_file)))

    if data_dir:
        add(os.path.join(data_dir, "python"))
        add(os.path.join(data_dir, "python", "autoload"))

    if script_path and not os.path.isabs(script_path) and data_dir:
        add(os.path.join(data_dir, "python", os.path.dirname(script_path)))

    weechat_home = os.environ.get("WEECHAT_HOME", "")
    if weechat_home:
        add(os.path.join(weechat_home, "python"))

    xdg_data = os.environ.get("XDG_DATA_HOME", "")
    if not xdg_data:
        xdg_data = os.path.join(os.path.expanduser("~"), ".local", "share")
    add(os.path.join(xdg_data, "weechat", "python"))
    add(os.path.join(os.path.expanduser("~"), ".weechat", "python"))
    return dirs


def find_popup_helper():
    """Return the best helper path, or None."""
    for directory in _helper_search_dirs():
        candidate = os.path.join(directory, "weetranslate_popup.py")
        if os.path.isfile(candidate):
            return candidate
    return None


def popup_helper_path():
    if HELPER_PATH and os.path.isfile(HELPER_PATH):
        return HELPER_PATH
    helper = find_popup_helper()
    if helper:
        return helper
    searched = ", ".join(_helper_search_dirs()) or "(no candidate)"
    raise RuntimeError(
        "weetranslate_popup.py not found. It must be installed next to "
        "weetranslate.py. Searched: %s" % searched
    )


def _write_private(path, content):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(content)
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass


def _unhook_job(job):
    for key in ("hook", "timer_hook"):
        hook = job.get(key)
        if not hook:
            continue
        try:
            weechat.unhook(hook)
        except Exception:
            pass
        job[key] = None


def cleanup_job(job_id):
    job = JOBS.pop(job_id, None)
    if not job:
        return
    _unhook_job(job)
    directory = job.get("tmpdir")
    if directory and not opt_bool("keep_temp"):
        shutil.rmtree(directory, ignore_errors=True)


def cancel_jobs(buffer=None):
    """Cancel running jobs (all, or only those for one buffer)."""
    count = 0
    for job_id, job in list(JOBS.items()):
        if buffer is not None and job.get("buffer_name") != buffer:
            continue
        cleanup_job(job_id)
        count += 1
    return count


# ---------------------------------------------------------------------------
# configuration
# ---------------------------------------------------------------------------

def _new_option(section, name, option_type, description, default,
                string_values="", min_value=0, max_value=0, null_allowed=0):
    return weechat.config_new_option(
        CONFIG, section, name, option_type, description, string_values,
        min_value, max_value, default, default, null_allowed,
        "", "", "", "", "", "",
    )


def config_init():
    global CONFIG

    CONFIG = weechat.config_new(CONFIG_NAME, "", "")
    if not CONFIG:
        prnt("", "Unable to create the configuration.", "red")
        return False

    section = weechat.config_new_section(
        CONFIG, "main", 0, 0,
        "", "", "", "", "", "", "", "", "", "",
    )
    if not section:
        prnt("", "Unable to create the configuration section.", "red")
        weechat.config_free(CONFIG)
        CONFIG = None
        return False

    _new_option(
        section, "provider", "enum",
        "LLM provider (deepseek by default, openai, custom).",
        "deepseek", string_values="deepseek|openai|custom",
    )
    _new_option(
        section, "api_key", "string",
        "Provider API key (or ${sec.data.xxx} for /secure).",
        "", null_allowed=1,
    )
    _new_option(
        section, "api_url", "string",
        "chat/completions URL (empty = provider default).",
        "", null_allowed=1,
    )
    _new_option(
        section, "model", "string",
        "Model (empty = provider default).",
        "", null_allowed=1,
    )
    _new_option(
        section, "default_language", "enum",
        "Default language selected in the popup.",
        "en", string_values="en|de|es|ru",
    )
    _new_option(
        section, "source_language", "string",
        "Source language (auto for automatic detection).",
        "auto",
    )
    _new_option(
        section, "editor", "string",
        "Editor: auto ($VISUAL, $EDITOR, then vi), or a command.",
        "auto",
    )
    _new_option(
        section, "terminal", "enum",
        "Terminal used for the validation window.",
        "auto",
        string_values=(
            "auto|tmux|xterm|x-terminal-emulator|kitty|alacritty|urxvt|"
            "gnome-terminal|konsole|xfce4-terminal|custom"
        ),
    )
    _new_option(
        section, "terminal_cmd", "string",
        "Terminal command when terminal=custom (e.g. xterm -T WeeTranslate -e).",
        "", null_allowed=1,
    )
    _new_option(
        section, "timeout", "integer",
        "API timeout in seconds.",
        "30", min_value=5, max_value=120,
    )
    _new_option(
        section, "max_chars", "integer",
        "Maximum source text length.",
        "2000", min_value=50, max_value=8000,
    )
    _new_option(
        section, "show_original", "boolean",
        "Show the source text in the validation window.",
        "on",
    )
    _new_option(
        section, "debug", "boolean",
        "Show debug messages.",
        "off",
    )
    _new_option(
        section, "keep_temp", "boolean",
        "Keep temporary files (debugging).",
        "off",
    )

    rc = weechat.config_read(CONFIG)
    if rc == weechat.WEECHAT_CONFIG_READ_FILE_NOT_FOUND:
        weechat.config_write(CONFIG)
        debug("Configuration created in the WeeChat config directory.")
    elif rc != weechat.WEECHAT_CONFIG_READ_OK:
        prnt("", "Error reading WeeTranslate configuration.", "red")
        return False

    return True


def config_show(buffer):
    prnt(buffer, "WeeTranslate configuration:")
    for name in (
        "provider", "api_key", "api_url", "model", "source_language",
        "default_language", "editor", "terminal", "terminal_cmd", "timeout",
        "max_chars", "show_original", "debug", "keep_temp",
    ):
        if name in ("timeout", "max_chars"):
            value = str(opt_int(name))
        elif name in ("show_original", "debug", "keep_temp"):
            value = "on" if opt_bool(name) else "off"
        else:
            value = opt_str(name)
        if name == "api_key":
            value = mask_secret(expand_config_value(value))
        elif name == "terminal_cmd" and value:
            value = shlex.quote(value)
        weechat.prnt(buffer, "  %s.main.%s = %s" % (CONFIG_NAME, name, value))
    try:
        _provider, info, url, model = resolve_provider()
        weechat.prnt(buffer, "")
        weechat.prnt(buffer, "  Effective: %s — %s — model %s"
                     % (info["label"], url, model))
    except Exception as exc:
        weechat.prnt(buffer, "  Effective: incomplete configuration (%s)" % exc)
    weechat.prnt(buffer, "")
    weechat.prnt(buffer, "Edit: /set %s.main.<option> <value>" % CONFIG_NAME)
    weechat.prnt(buffer, "Menu: /trad config   (fset)")


# ---------------------------------------------------------------------------
# LLM request
# ---------------------------------------------------------------------------

def _api_error_message(response_code, body, error):
    if error:
        return "Network error: %s" % error
    code = str(response_code or "").strip()
    detail = ""
    try:
        parsed = json.loads(body)
    except Exception:
        parsed = None
    if isinstance(parsed, dict):
        error_obj = parsed.get("error")
        if isinstance(error_obj, dict):
            detail = error_obj.get("message") or ""
    if not detail:
        detail = (body or "").strip()

    detail = normalize_single_line(detail)[:300]

    if code == "401":
        return ("Invalid or rejected API key (HTTP 401). "
                "Configure /set %s.main.api_key sk-..." % CONFIG_NAME)
    if code == "402":
        return "Account or key has insufficient credit (HTTP 402)."
    if code == "429":
        return "Provider rate limit reached (HTTP 429). Try again shortly."
    if code in ("500", "502", "503", "504"):
        return "Provider server unavailable (HTTP %s). Try again later." % code
    if code and code != "200":
        suffix = (": " + detail) if detail else ""
        return "API error (HTTP %s)%s" % (code, suffix)
    if not body:
        return "Empty API response."
    return "Unreadable API response" + ((": " + detail) if detail else ".")


def _clean_model_text(content):
    text = content.strip()
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in ("'", '"'):
        text = text[1:-1].strip()
    return text


def _extract_translations(body):
    """Extract an {en, de, es, ru} object from choices[0].message.content."""
    try:
        data = json.loads(body)
    except Exception as exc:
        raise ValueError("invalid JSON: %s" % exc)

    if not isinstance(data, dict):
        raise ValueError("the JSON response is not an object")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("missing or empty choices field")
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise ValueError("invalid first JSON choice")
    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("missing message field")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty translation content")

    text = _clean_model_text(content)
    try:
        payload = json.loads(text)
    except Exception:
        start = text.find("{")
        end = text.rfind("}")
        if start < 0 or end <= start:
            raise ValueError("the model did not return a JSON object")
        try:
            payload = json.loads(text[start:end + 1])
        except Exception as exc:
            raise ValueError("invalid JSON object: %s" % exc)
    if not isinstance(payload, dict):
        raise ValueError("the translation is not a JSON object")

    aliases = {
        "en": ("en", "english"),
        "de": ("de", "german", "deutsch"),
        "es": ("es", "spanish", "espanol", "español"),
        "ru": ("ru", "russian", "russkiy", "русский"),
    }
    normalized_payload = {str(key).strip().lower(): value
                          for key, value in payload.items()}
    translations = {}
    for code in LANGUAGE_ORDER:
        value = None
        for key in aliases[code]:
            if isinstance(normalized_payload.get(key), str):
                value = normalized_payload[key]
                break
        if value:
            normalized = normalize_single_line(value)
            if normalized:
                translations[code] = normalized
    if not translations:
        raise ValueError("no usable translation in the response")
    return translations


def _extract_single_translation(body):
    """Extract a plain translation from choices[0].message.content."""
    try:
        data = json.loads(body)
    except Exception as exc:
        raise ValueError("invalid JSON: %s" % exc)
    if not isinstance(data, dict):
        raise ValueError("the JSON response is not an object")
    choices = data.get("choices")
    if not isinstance(choices, list) or not choices:
        raise ValueError("missing or empty choices field")
    first_choice = choices[0]
    if not isinstance(first_choice, dict):
        raise ValueError("invalid first JSON choice")
    message = first_choice.get("message")
    if not isinstance(message, dict):
        raise ValueError("missing message field")
    content = message.get("content")
    if not isinstance(content, str) or not content.strip():
        raise ValueError("empty translation content")
    text = normalize_single_line(_clean_model_text(content))
    if not text:
        raise ValueError("empty translation after cleanup")
    return text


def resolve_provider():
    """Resolve the effective provider, API URL and model."""
    provider = (opt_str("provider", "deepseek") or "deepseek").strip().lower()
    info = PROVIDERS.get(provider)
    if not info:
        raise RuntimeError("unknown provider: %s" % provider)
    url = opt_str("api_url", "").strip() or info["api_url"]
    model = opt_str("model", "").strip() or info["model"]
    if not url:
        raise RuntimeError("api_url is empty for the custom provider")
    if not model:
        raise RuntimeError("model is empty for the custom provider")
    return provider, info, url, model


def _request_translation(job, custom_language=None):
    text = job.get("source") or ""
    try:
        _provider, provider_info, api_url, model = resolve_provider()
    except Exception as exc:
        _fail(job, "Invalid provider configuration: %s" % exc)
        return
    if not api_url.startswith(("http://", "https://")):
        _fail(job, "api_url must start with http:// or https://.")
        return

    api_key = expand_config_value(opt_str("api_key")).strip()
    if not api_key:
        notify("%s API key is missing." % provider_info["label"], "red")
        weechat.prnt("", "  /set %s.main.api_key sk-..." % CONFIG_NAME)
        weechat.prnt("", "  or, safer: /secure set llm_key sk-...")
        weechat.prnt("", "  then: /set %s.main.api_key ${sec.data.llm_key}"
                     % CONFIG_NAME)
        cleanup_job(job["id"])
        return
    if "\n" in api_key or "\r" in api_key:
        _fail(job, "API key contains a newline: configuration rejected.")
        return

    source = opt_str("source_language", "auto").strip() or "auto"
    if custom_language:
        system_prompt = (
            "You are a professional translator. Translate the user's message "
            "into %s. Return ONLY the translation as a single line, without "
            "quotes, comments or explanations." % custom_language
        )
        job["mode"] = "custom"
        job["custom_language"] = custom_language
    else:
        system_prompt = TRANSLATION_PROMPT
        job["mode"] = "multi"
    if source.lower() != "auto":
        system_prompt += " The source language is %s." % source

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": text},
        ],
        "temperature": 0.1,
        "stream": False,
    }
    body = json.dumps(payload, ensure_ascii=False)
    headers = "\n".join([
        "Content-Type: application/json",
        "Authorization: Bearer " + api_key,
        "Accept: application/json",
    ])
    options = {
        "httpheader": headers,
        "post": "1",
        "postfields": body,
        "useragent": "weetranslate/%s" % SCRIPT_VERSION,
    }

    timeout_ms = max(5, opt_int("timeout", 30)) * 1000
    job["stage"] = "api"
    hook = weechat.hook_url(api_url, options, timeout_ms, "api_cb", job["id"])
    if not hook:
        _fail(job, "Unable to start the API request.")
        return
    job["hook"] = hook


def translate(buffer, text):
    global JOB_SEQ

    text = (text or "").strip()
    if not text:
        notify("No text to translate. Use /trad <text>.", "yellow")
        return

    max_chars = opt_int("max_chars", 2000)
    if len(text) > max_chars:
        notify("Text too long (%d characters, maximum %d)."
               % (len(text), max_chars), "red")
        return

    JOB_SEQ += 1
    job_id = "job-%d-%d" % (int(time.time()), JOB_SEQ)
    buffer_name = weechat.buffer_get_string(buffer, "full_name") or "core.weechat"
    try:
        previous_input = weechat.buffer_get_string(buffer, "input") or ""
    except Exception:
        previous_input = ""
    job = {
        "id": job_id,
        "buffer_name": buffer_name,
        "source": text,
        "previous_input": previous_input,
        "stage": "api",
        "created": time.time(),
        "tmpdir": None,
        "hook": None,
        "mode": "multi",
        "translations": {},
        "custom_language": None,
    }
    JOBS[job_id] = job
    _request_translation(job)


def api_cb(data, url, _options, output):
    job = JOBS.get(data)
    if not job or job.get("stage") != "api":
        return weechat.WEECHAT_RC_OK

    job["hook"] = None
    if not isinstance(output, dict):
        output = {}
    response_code = output.get("response_code", "") or ""
    body = output.get("output", "") or ""
    error = output.get("error", "") or ""
    error_code = output.get("error_code", "") or ""

    debug(
        "API %s response=%s error=%s mode=%s"
        % (url, response_code, error or error_code, job.get("mode"))
    )

    if error or not response_code:
        message = _api_error_message(response_code, body, error)
        if error_code == "6":
            message = "API request timed out."
        _fail(job, message)
        return weechat.WEECHAT_RC_OK

    if str(response_code) != "200":
        _fail(job, _api_error_message(response_code, body, ""))
        return weechat.WEECHAT_RC_OK

    if job.get("mode") == "custom":
        try:
            translation = _extract_single_translation(body)
        except Exception as exc:
            _fail(job, "Unable to extract translation: %s" % exc)
            return weechat.WEECHAT_RC_OK
        translations = job.get("translations") or {}
        translations["custom"] = translation
        job["translations"] = translations
        job["translation"] = translation
        job["stage"] = "popup"
        try:
            _open_popup(job, translations, job.get("custom_language"))
        except Exception as exc:
            debug("Popup exception: %r" % exc)
            _fail(job, "Unable to open the validation window: %s" % exc)
        return weechat.WEECHAT_RC_OK

    try:
        translations = _extract_translations(body)
    except Exception as exc:
        _fail(job, "Unable to extract translations: %s" % exc)
        return weechat.WEECHAT_RC_OK

    job["translations"] = translations
    job["translation"] = translations.get("en") or next(iter(translations.values()))
    job["stage"] = "popup"

    try:
        _open_popup(job, translations)
    except Exception as exc:
        debug("Popup exception: %r" % exc)
        _fail(job, "Unable to open the validation window: %s" % exc)
    return weechat.WEECHAT_RC_OK


def _fail(job, message):
    message = normalize_single_line(message)[:1000] or "Unknown error."
    notify(message, "red")
    cleanup_job(job["id"])


def _shell_join(args):
    """Quote arguments for a shell command (tmux display-popup)."""
    return " ".join(shlex.quote(str(arg)) for arg in args)


def _choose_terminal():
    terminal = opt_str("terminal", "auto").strip() or "auto"
    if terminal != "auto":
        if terminal == "tmux" and not os.environ.get("TMUX"):
            raise RuntimeError(
                "terminal=tmux requires WeeChat to run inside tmux "
                "($TMUX is not set)."
            )
        return terminal

    for candidate in AUTO_TERMINALS:
        if shutil.which(candidate):
            return candidate

    raise RuntimeError(
        "No X11 terminal found. In SSH/tmux without X11, explicitly set "
        "/set %s.main.terminal tmux; otherwise install xterm or configure "
        "terminal_cmd." % CONFIG_NAME
    )


def _build_terminal_argv(helper_args):
    """Build the complete terminal + helper command."""
    terminal = _choose_terminal()
    title = "WeeTranslate"

    if terminal == "tmux":
        tmux = shutil.which("tmux")
        if not tmux:
            raise RuntimeError("tmux not found in PATH.")
        args = [
            "display-popup", "-E",
            "-w", "80%", "-h", "60%",
            "-T", title,
        ]
        pane = os.environ.get("TMUX_PANE")
        if pane:
            args.extend(["-t", pane])
        args.append(_shell_join(helper_args))
        return [tmux] + args

    if terminal == "custom":
        command_line = opt_str("terminal_cmd", "").strip()
        if not command_line:
            raise RuntimeError(
                "terminal=custom but terminal_cmd is empty. "
                "Example: /set %s.main.terminal_cmd \"xterm -T WeeTranslate -e\""
                % CONFIG_NAME
            )
        parts = shlex.split(command_line)
        if not parts:
            raise RuntimeError("terminal_cmd is empty after parsing.")
        command = _resolve_executable(parts[0])
        return [command] + [arg.replace("{title}", title) for arg in parts[1:]] + helper_args

    if terminal not in TERMINAL_SPECS:
        raise RuntimeError("Unknown terminal: %s" % terminal)

    command, args = TERMINAL_SPECS[terminal]
    resolved = shutil.which(command)
    if not resolved:
        raise RuntimeError("Terminal not found: %s" % command)
    args = [arg.replace("{title}", title) for arg in args]
    return [resolved] + args + helper_args


def _resolve_executable(command):
    if os.path.isabs(command) and os.path.exists(command):
        return command
    resolved = shutil.which(command)
    if resolved:
        return resolved
    if os.path.exists(command):
        return os.path.abspath(command)
    raise RuntimeError("Command not found: %s" % command)


def resolve_editor():
    """Resolve editor=auto: $VISUAL, $EDITOR, then vi/vim/nano."""
    editor = opt_str("editor", "auto").strip() or "auto"
    if editor.lower() not in ("auto", ""):
        return editor
    for variable in ("VISUAL", "EDITOR"):
        value = os.environ.get(variable, "").strip()
        if value:
            return value
    for candidate in ("vi", "vim", "nano"):
        if shutil.which(candidate):
            return candidate
    return "vi"


def _open_popup(job, translations, custom_language=None):
    helper = popup_helper_path()

    source = job.get("source") or ""
    editor = resolve_editor()
    if custom_language and "custom" in translations:
        default_lang = "custom"
    else:
        default_lang = opt_str("default_language", "en").strip() or "en"
        if default_lang not in translations:
            default_lang = next(code for code in LANGUAGE_ORDER if code in translations)

    tmpdir = tempfile.mkdtemp(prefix="weetranslate-")
    job["tmpdir"] = tmpdir
    translation_file = os.path.join(tmpdir, "translation.txt")
    translations_file = os.path.join(tmpdir, "translations.json")
    original_file = os.path.join(tmpdir, "original.txt")
    result_file = os.path.join(tmpdir, "result.txt")
    done_file = os.path.join(tmpdir, "done.txt")
    pid_file = os.path.join(tmpdir, "helper.pid")
    started_file = os.path.join(tmpdir, "started.txt")
    _write_private(
        translations_file,
        json.dumps(translations, ensure_ascii=False, sort_keys=True) + "\n",
    )
    _write_private(translation_file, translations[default_lang] + "\n")
    _write_private(original_file, source + "\n")
    job["result_file"] = result_file
    job["done_file"] = done_file
    job["pid_file"] = pid_file
    job["started_file"] = started_file

    helper_args = [
        python_executable(), helper,
        "--translations", translations_file,
        "--translation", translation_file,
        "--original", original_file,
        "--result", result_file,
        "--done", done_file,
        "--pid-file", pid_file,
        "--started", started_file,
        "--default-lang", default_lang,
        "--editor", editor,
    ]
    if custom_language:
        helper_args.extend(["--custom-language", custom_language])
    if not opt_bool("show_original", True):
        helper_args.append("--no-original")

    argv = _build_terminal_argv(helper_args)

    options = {"arg%d" % index: arg for index, arg in enumerate(argv[1:], 1)}
    debug("Popup: %s" % _shell_join(argv))
    job["popup_started"] = time.time()
    job["process_exit"] = None
    hook = weechat.hook_process_hashtable(
        argv[0], options, 0, "popup_cb", job["id"]
    )
    if not hook:
        raise RuntimeError("hook_process_hashtable failed")
    job["hook"] = hook
    # Some terminals (gnome-terminal, ...) detach their process and return
    # immediately. Watch the helper's "done" file instead of relying only on
    # the terminal exit code.
    timer = weechat.hook_timer(300, 0, 0, "popup_timer_cb", job["id"])
    if not timer:
        raise RuntimeError("hook_timer failed (popup monitoring)")
    job["timer_hook"] = timer


def _read_small_file(path):
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return handle.read()
    except OSError as exc:
        debug("Unable to read %s: %s" % (path, exc))
        return ""


def _finish_popup(job, status, result="", detail=""):
    """Finish an already completed popup (once)."""
    if job.get("finished"):
        return

    if status == "retranslate":
        _unhook_job(job)
        tmpdir = job.get("tmpdir") or ""
        language = normalize_single_line(
            _read_small_file(os.path.join(tmpdir, "custom-language.txt"))
        )
        if not language:
            notify("No custom language name provided.", "red")
            cleanup_job(job["id"])
            return
        if tmpdir:
            shutil.rmtree(tmpdir, ignore_errors=True)
        job["tmpdir"] = None
        job["translations"] = job.get("translations") or {}
        job["custom_language"] = language
        job["mode"] = "custom"
        job["stage"] = "api"
        job["finished"] = False
        _request_translation(job, language)
        return

    job["finished"] = True
    job["stage"] = "done"
    _unhook_job(job)

    if status == "accepted":
        result = normalize_single_line(result)
        if result:
            _inject_result(job, result)
        else:
            notify("The window accepted an empty translation: nothing was "
                   "inserted.", "red")
    elif status == "canceled":
        debug("Translation canceled by the user.")
    else:
        notify("The translation window reported an error.", "red")
        if detail:
            debug("Popup output: %s" % detail[:500])

    cleanup_job(job["id"])


def _check_popup_done(job):
    """Read done.txt and finish the job if the helper is done. True when done."""
    done_file = job.get("done_file") or ""
    if not done_file or not os.path.isfile(done_file):
        return False

    status = normalize_single_line(_read_small_file(done_file)) or "error"
    if status not in ("accepted", "canceled", "error", "retranslate"):
        debug("Unexpected popup status: %r" % status)
        status = "error"

    result = ""
    result_file = job.get("result_file") or ""
    if result_file and os.path.isfile(result_file):
        result = _read_small_file(result_file)

    _finish_popup(job, status, result=result)
    return True


def _helper_pid(job):
    pid_file = job.get("pid_file") or ""
    if not pid_file or not os.path.isfile(pid_file):
        return 0
    try:
        with open(pid_file, "r", encoding="ascii", errors="ignore") as handle:
            return int(handle.read().strip() or "0")
    except (OSError, ValueError):
        return 0


def _helper_alive(job):
    pid = _helper_pid(job)
    if pid <= 1:
        return False
    return _pid_looks_like_helper(pid)


def popup_timer_cb(data, _remaining_calls):
    job = JOBS.get(data)
    if not job or job.get("stage") != "popup":
        return weechat.WEECHAT_RC_OK

    if _check_popup_done(job):
        return weechat.WEECHAT_RC_OK

    now = time.time()
    started_at = job.get("popup_started", now)
    if now - started_at > 1800:
        _fail(job, "The translation window stayed open for more than 30 min: "
                   "giving up. Use /trad cancel if needed.")
        return weechat.WEECHAT_RC_OK

    started_file = job.get("started_file") or ""
    started = bool(started_file) and os.path.isfile(started_file)
    process_exit = job.get("process_exit")
    process_exited_at = job.get("process_exited_at", now)

    if started:
        # The helper started then disappeared without writing done.txt:
        # SIGKILL, crash, etc. Do not keep the timer alive for 30 minutes.
        if not _helper_alive(job):
            if now - process_exited_at > 2 or now - started_at > 10:
                if job.get("close_requested"):
                    _finish_popup(job, "accepted",
                                  result=job.get("translation", ""))
                    return weechat.WEECHAT_RC_OK
                detail = normalize_single_line(job.get("process_err") or "")[:300]
                _fail(job, "The window closed without validating (helper "
                           "interrupted)%s" % ((": " + detail) if detail else ""))
        return weechat.WEECHAT_RC_OK

    if process_exit is not None:
        if process_exit != 0 and now - process_exited_at > 2:
            detail = normalize_single_line(job.get("process_err") or "")[:300]
            _fail(job, "The window could not start (code %s)%s"
                  % (process_exit, (": " + detail) if detail else ""))
        elif now - started_at > 10:
            _fail(job, "The window did not start (no helper detected).")
    elif now - started_at > 30:
        _fail(job, "The popup helper did not start.")
    return weechat.WEECHAT_RC_OK


def popup_cb(data, _command, return_code, out, err):
    job = JOBS.get(data)
    if not job or job.get("stage") != "popup":
        return weechat.WEECHAT_RC_OK

    running = getattr(weechat, "WEECHAT_HOOK_PROCESS_RUNNING", -1)
    if return_code == running:
        return weechat.WEECHAT_RC_OK

    job["hook"] = None
    job["process_exit"] = return_code
    job["process_exited_at"] = time.time()
    job["process_err"] = err or out or ""

    error_code = getattr(weechat, "WEECHAT_HOOK_PROCESS_ERROR", -2)
    if return_code == error_code:
        detail = normalize_single_line(job["process_err"])[:300]
        _fail(job, "Error launching the window: %s"
              % (detail or "unknown"))
        return weechat.WEECHAT_RC_OK

    # If done.txt already exists, finish immediately. Otherwise the timer
    # keeps polling (for terminals that detach their process).
    _check_popup_done(job)
    return weechat.WEECHAT_RC_OK


def _latest_popup_for_buffer(buffer):
    try:
        name = weechat.buffer_get_string(buffer, "full_name")
    except Exception:
        return None
    candidates = [
        job for job in JOBS.values()
        if job.get("stage") == "popup" and job.get("buffer_name") == name
    ]
    if not candidates:
        return None
    return max(candidates, key=lambda job: job.get("created", 0))


def _pid_looks_like_helper(pid):
    """Check that the PID is really our helper, without assuming /proc.

    Linux: /proc/<pid>/cmdline. FreeBSD/macOS: ps -p <pid> -o command=.
    Avoids killing a recycled PID after the helper exits.
    """
    proc_cmdline = "/proc/%d/cmdline" % pid
    if os.path.isdir("/proc"):
        try:
            with open(proc_cmdline, "rb") as handle:
                cmdline = handle.read().decode("utf-8", "replace")
            return "weetranslate_popup.py" in cmdline
        except OSError:
            pass  # no procfs, or the process is already gone: try ps

    ps = shutil.which("ps")
    if not ps:
        return False
    try:
        output = subprocess.check_output(
            [ps, "-p", str(pid), "-o", "command="],
            stderr=subprocess.DEVNULL, timeout=1, text=True,
        )
    except (OSError, subprocess.SubprocessError):
        return False
    return "weetranslate_popup.py" in output


def close_popup_cb(data, buffer, _command):
    """/close closes the active popup for the buffer and accepts its content.

    The command is intercepted only when a popup belongs to this buffer, so
    /close still closes a normal buffer.
    """
    if buffer is None:
        return weechat.WEECHAT_RC_OK
    job = _latest_popup_for_buffer(buffer)
    if not job:
        return weechat.WEECHAT_RC_OK

    pid = _helper_pid(job)
    job["close_requested"] = True
    if pid > 1 and _pid_looks_like_helper(pid):
        try:
            os.kill(pid, signal.SIGTERM)
            debug("Close requested for helper %s." % pid)
        except OSError as exc:
            notify("Unable to close the window (%s); validating the "
                   "translation directly." % exc, "yellow")
            _finish_popup(job, "accepted", result=job.get("translation", ""))
    else:
        # No PID yet (window still starting): validate the translation we
        # already have and clean up.
        debug("No PID yet; validating the translation directly.")
        _finish_popup(job, "accepted", result=job.get("translation", ""))

    eat = getattr(weechat, "WEECHAT_RC_OK_EAT", 1)
    return eat


def _is_command_char(char):
    try:
        return bool(weechat.string_is_command_char(char))
    except Exception:
        return char == "/"


def _inject_result(job, text):
    buffer_name = job.get("buffer_name") or ""
    try:
        buffer = weechat.buffer_search("==", buffer_name) if buffer_name else None
    except Exception:
        buffer = None
    if not buffer:
        # Do not inject into an arbitrary buffer: the user could send the
        # translation to the wrong place.
        notify("Translation ready, but the source buffer (%s) no longer "
               "exists. Text: %s" % (buffer_name or "unknown", text), "red")
        return

    text = normalize_single_line(text)
    if not text:
        notify("Empty translation: nothing was inserted.", "red")
        return

    # If the user typed something while the window was open, do not
    # silently overwrite it.
    try:
        current_input = weechat.buffer_get_string(buffer, "input") or ""
    except Exception:
        current_input = ""
    previous_input = job.get("previous_input") or ""
    if current_input.strip() and current_input != previous_input:
        notify("Your input changed during translation; it was not "
               "overwritten. Translation available: %s" % text, "yellow")
        return

    # A translation starting with WeeChat's command character (/) would be
    # executed on Enter. Add a leading space to force a message.
    if text and _is_command_char(text[0]):
        text = " " + text
        notify("The translation started with a command character: a leading "
               "space was added to prevent execution.", "yellow")

    try:
        weechat.buffer_set(buffer, "input", text)
        # Move the cursor to the end of the line. input_pos is a byte offset
        # on newer WeeChat versions and clamped to a character offset on 4.1;
        # using the UTF-8 byte length works for both.
        try:
            weechat.buffer_set(
                buffer, "input_pos", str(len(text.encode("utf-8")))
            )
        except Exception:
            pass
    except Exception as exc:
        notify("Unable to insert the translation: %s" % exc, "red")


# ---------------------------------------------------------------------------
# /trad command
# ---------------------------------------------------------------------------

def command_trad_cb(_data, buffer, args):
    args = (args or "").strip()
    if buffer is None:
        buffer = weechat.current_buffer()

    # Allow translating text that looks like a subcommand.
    if args.startswith("-- "):
        translate(buffer, args[3:].strip())
        return weechat.WEECHAT_RC_OK

    if args in ("help", "-h", "--help", "?"):
        _show_help(buffer)
        return weechat.WEECHAT_RC_OK

    if args == "config":
        # Native WeeChat menu: fset filtered on plugin options.
        weechat.command(buffer, "/fset %s.*" % CONFIG_NAME)
        return weechat.WEECHAT_RC_OK
    if args == "config show":
        config_show(buffer)
        return weechat.WEECHAT_RC_OK

    if args == "cancel":
        count = cancel_jobs()
        if count:
            notify("%d translation(s) canceled." % count, "yellow")
        else:
            notify("No translation in progress.", "yellow")
        return weechat.WEECHAT_RC_OK

    if not args:
        current_input = ""
        try:
            current_input = weechat.buffer_get_string(buffer, "input")
        except Exception:
            pass
        current_input = (current_input or "").strip()
        if current_input:
            # Do not clear the input here: on failure (missing key, timeout,
            # cancellation), the user's input remains intact.
            translate(buffer, current_input)
        else:
            _show_help(buffer)
        return weechat.WEECHAT_RC_OK

    translate(buffer, args)
    return weechat.WEECHAT_RC_OK


def _show_help(buffer):
    prnt(buffer, "Translation via an LLM (DeepSeek/OpenAI).")
    weechat.prnt(buffer, "")
    weechat.prnt(buffer, "  /trad <text>      translate and open the validation window")
    weechat.prnt(buffer, "  /trad             translate the current input bar")
    weechat.prnt(buffer, "  /trad config      open the plugin fset menu")
    weechat.prnt(buffer, "  /trad config show print the configuration as text")
    weechat.prnt(buffer, "  /trad cancel      cancel pending translations")
    weechat.prnt(buffer, "  /trad -- <text>   force translation of text that "
                 "looks like a subcommand")
    weechat.prnt(buffer, "")
    weechat.prnt(buffer, "In the window: 1-4 = language, 5 = other, "
                 "y = accept, n = edit, q = cancel.")
    weechat.prnt(buffer, "Closing the window also accepts the displayed translation.")
    weechat.prnt(buffer, "While a window is open, /close closes it and accepts "
                 "the translation (without closing the buffer).")
    weechat.prnt(buffer, "")
    weechat.prnt(buffer, "Configuration:")
    weechat.prnt(buffer, "  /trad config      plugin fset menu")
    weechat.prnt(buffer, "  /set %s.main.provider deepseek   (or openai, custom)" % CONFIG_NAME)
    weechat.prnt(buffer, "  /set %s.main.api_key sk-..." % CONFIG_NAME)
    weechat.prnt(buffer, "  /set %s.main.editor auto   "
                 "($VISUAL/$EDITOR, otherwise vi)" % CONFIG_NAME)
    weechat.prnt(buffer, "  /set %s.main.terminal auto   "
                 "(explicit tmux for SSH without X11)" % CONFIG_NAME)


# ---------------------------------------------------------------------------
# lifecycle
# ---------------------------------------------------------------------------

def shutdown_cb():
    cancel_jobs()
    if CONFIG:
        try:
            weechat.config_write(CONFIG)
        except Exception:
            pass
        try:
            weechat.config_free(CONFIG)
        except Exception:
            pass
    return weechat.WEECHAT_RC_OK


def main():
    global CONFIG, HELPER_PATH

    weechat.register(
        SCRIPT_NAME,
        "DeepSeek, whipped by Vincent Bonnevialle (@lateralux)",
        SCRIPT_VERSION,
        "BSD-3-Clause",
        "LLM translation (DeepSeek/OpenAI) with external validation/editing",
        "shutdown_cb",
        "",
    )

    if not config_init():
        return weechat.WEECHAT_RC_ERROR

    # Cache the helper path at load time: WeeChat may expose __file__ as None
    # after another Python script is loaded, which used to fall back to $HOME.
    HELPER_PATH = find_popup_helper()

    weechat.hook_command(
        "trad",
        "Translate text via an LLM, open a validation/editing window, then "
        "insert the result into the input bar.",
        "[<text> | config | cancel | help]",
        "text: text to translate (otherwise use the input bar)\n"
        "config: open the configuration menu\n"
        "cancel: cancel pending translations\n"
        "help: show this help",
        "",
        "command_trad_cb",
        "",
    )

    # /close closes the active popup for the current buffer (and validates
    # it) instead of closing the buffer. Without a popup, /close is unchanged.
    weechat.hook_command_run("/close", "close_popup_cb", "")

    debug("Plugin loaded (helper=%s)" % (HELPER_PATH or "(not found)"))
    return weechat.WEECHAT_RC_OK


if __name__ == "__main__":
    main()
