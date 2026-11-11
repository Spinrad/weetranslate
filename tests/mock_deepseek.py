#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Small DeepSeek mock server for local WeeTranslate tests.

Usage:
    python3 tests/mock_deepseek.py [--port 8765] [--mode ok|401|500|timeout]

Returns a POST /chat/completions response compatible with OpenAI/DeepSeek.
Never use in production.
"""

import argparse
import json
import os
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

MODE = "ok"
LOG_PATH = os.environ.get("MOCK_LOG", "")


def log(message):
    if not LOG_PATH:
        return
    with open(LOG_PATH, "a", encoding="utf-8") as handle:
        handle.write(message + "\n")


def translate(text):
    normalized = " ".join(text.split())
    low = normalized.lower()
    if "hello" in low:
        return {
            "en": "Hello, how are you?",
            "de": "Hallo, wie geht es dir?",
            "es": "Hola, ¿cómo estás?",
            "ru": "Привет, как дела?",
        }
    if "morning" in low:
        return {
            "en": "How are you doing?",
            "de": "Wie geht es dir?",
            "es": "¿Cómo estás?",
            "ru": "Как дела?",
        }
    if "test" in low:
        return {
            "en": "This is a test translation.",
            "de": "Dies ist eine Testübersetzung.",
            "es": "Esta es una traducción de prueba.",
            "ru": "Это тестовый перевод.",
        }
    if "thanks" in low:
        return {
            "en": "Thank you very much.",
            "de": "Vielen Dank.",
            "es": "Muchas gracias.",
            "ru": "Большое спасибо.",
        }
    if "slash" in low:
        return {"en": "/quit", "de": "/quit", "es": "/quit", "ru": "/quit"}
    return {
        "en": "EN: " + normalized,
        "de": "DE: " + normalized,
        "es": "ES: " + normalized,
        "ru": "RU: " + normalized,
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "MockDeepSeek/0.1"

    def _send_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b"{}"
        auth = self.headers.get("Authorization", "")
        log("POST %s auth_present=%s body=%s"
            % (self.path, bool(auth), raw.decode("utf-8", "replace")))

        if self.path not in ("/chat/completions", "/v1/chat/completions"):
            self._send_json(404, {"error": {"message": "not found"}})
            return

        if MODE == "401":
            self._send_json(401, {"error": {"message": "invalid api key"}})
            return
        if MODE == "500":
            self._send_json(500, {"error": {"message": "internal error"}})
            return
        if MODE == "timeout":
            time.sleep(10)
            self._send_json(500, {"error": {"message": "late"}})
            return
        if MODE == "malformed":
            self._send_json(200, [])
            return

        try:
            request = json.loads(raw.decode("utf-8"))
            messages = request.get("messages") or []
            user_text = ""
            for message in messages:
                if message.get("role") == "user":
                    user_text = message.get("content") or ""
            model = request.get("model") or "deepseek-chat"
            system_text = ""
            for message in messages:
                if message.get("role") == "system":
                    system_text = message.get("content") or ""
            custom_language = None
            if ("Translate the user's message into " in system_text
                    and "valid JSON object" not in system_text):
                custom_language = system_text.split(
                    "Translate the user's message into ", 1
                )[1].split(".", 1)[0].strip()
        except Exception as exc:
            self._send_json(400, {"error": {"message": "bad json: %s" % exc}})
            return

        if custom_language:
            if custom_language.lower() == "italian":
                body_text = "Ciao, come stai?"
            else:
                body_text = "%s: %s" % (custom_language, user_text)
            log("CUSTOM TRANSLATION %r -> %r" % (user_text, body_text))
            content = body_text
        else:
            translations = translate(user_text)
            log("TRANSLATION %r -> %r" % (user_text, translations))
            content = json.dumps(translations, ensure_ascii=False)
        self._send_json(200, {
            "id": "mock-chatcmpl-1",
            "object": "chat.completion",
            "created": int(time.time()),
            "model": model,
            "choices": [{
                "index": 0,
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }],
            "usage": {"prompt_tokens": 10, "completion_tokens": 5, "total_tokens": 15},
        })

    def do_GET(self):
        self._send_json(200, {"status": "ok", "mode": MODE})

    def log_message(self, fmt, *args):
        sys.stderr.write("[mock-deepseek] " + (fmt % args) + "\n")


def main():
    global MODE
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--mode", default=os.environ.get("MOCK_MODE", "ok"))
    args = parser.parse_args()
    MODE = args.mode
    server = ThreadingHTTPServer(("127.0.0.1", args.port), Handler)
    sys.stderr.write("[mock-deepseek] http://127.0.0.1:%d mode=%s\n" % (args.port, MODE))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
