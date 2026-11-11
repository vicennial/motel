"""A tiny fake cloud API on a loopback socket (no network, no real token), shared by the provider tests."""

from __future__ import annotations

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

TOKEN = "test-token-not-secret-0123456789"


class Clock:
    """Fake time: ``sleep`` advances ``monotonic`` so wait loops and retry back-offs run instantly."""

    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now


class FakeApi:
    """Subclasses implement ``route(method, path, query, body)`` returning ``(status, document_or_None)``."""

    def __init__(self):
        self.requests: list = []  # (method, path, query, body, authorization, all headers)
        self.fail: dict = {}  # (method, path prefix) -> list of status codes to return before the real answer
        self.reply_headers: dict = {}  # extra headers for injected failures

    def route(self, method, path, query, body):  # pragma: no cover - overridden
        raise NotImplementedError

    def posts(self, path):
        return [r for r in self.requests if r[0] == "POST" and r[1] == path]

    def handler(outer):  # noqa: N805
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, doc, headers=None):
                body = b"" if doc is None else (doc.encode() if isinstance(doc, str) else json.dumps(doc).encode())
                self.send_response(code)
                self.send_header("Content-Type", "text/plain" if isinstance(doc, str) else "application/json")
                self.send_header("Content-Length", str(len(body)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                if code != 204:
                    self.wfile.write(body)

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                url = urlparse(self.path)
                query = {k: v[0] for k, v in parse_qs(url.query).items()}
                outer.requests.append((method, url.path, query, body, self.headers.get("Authorization"), dict(self.headers)))
                for (m, prefix), codes in outer.fail.items():
                    if m == method and url.path.startswith(prefix) and codes:
                        return self._send(codes.pop(0), {"error": "injected", "id": "x", "message": "injected",
                                                         "errors": [{"reason": "injected"}]}, outer.reply_headers)
                code, doc = outer.route(method, url.path, query, body)
                return self._send(code, doc)

            do_GET = lambda self: self._handle("GET")  # noqa: E731
            do_POST = lambda self: self._handle("POST")  # noqa: E731
            do_PUT = lambda self: self._handle("PUT")  # noqa: E731
            do_PATCH = lambda self: self._handle("PATCH")  # noqa: E731
            do_DELETE = lambda self: self._handle("DELETE")  # noqa: E731
        return H


def serve(testcase, api: FakeApi) -> str:
    """Start ``api`` on a loopback port for the life of ``testcase``; returns the base URL."""
    server = ThreadingHTTPServer(("127.0.0.1", 0), api.handler())
    threading.Thread(target=server.serve_forever, args=(0.01,), daemon=True).start()
    testcase.addCleanup(server.shutdown)
    testcase.addCleanup(server.server_close)
    return f"http://127.0.0.1:{server.server_address[1]}"
