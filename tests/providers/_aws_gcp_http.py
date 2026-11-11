"""Shared test helpers for the AWS and GCP providers: a loopback HTTP server and a fake clock (no network)."""

from __future__ import annotations

import threading
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


@dataclass
class Req:
    method: str
    path: str
    query: str
    headers: dict
    body: bytes


class LoopbackServer:
    """``handler(Req) -> (status, headers dict, body bytes)`` served on 127.0.0.1; every request is recorded."""

    def __init__(self, handler):
        self.handler, self.requests, outer = handler, [], self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _serve(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                url = urlsplit(self.path)
                req = Req(self.command, url.path, url.query, {k.lower(): v for k, v in self.headers.items()}, body)
                outer.requests.append(req)
                status, headers, payload = outer.handler(req)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            do_GET = do_POST = do_PUT = do_DELETE = _serve

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.url = f"http://127.0.0.1:{self.httpd.server_address[1]}"
        self.thread = threading.Thread(target=self.httpd.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)

    def __enter__(self):
        self.thread.start()
        return self

    def __exit__(self, *exc):
        self.httpd.shutdown()
        self.httpd.server_close()


class FakeClock:
    """``sleep`` advances ``monotonic``, so waiter timeouts are testable without waiting."""

    def __init__(self):
        self.now, self.slept = 1000.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds
