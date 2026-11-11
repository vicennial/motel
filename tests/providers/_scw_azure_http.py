"""A tiny loopback HTTP harness shared by the Scaleway and Azure provider tests (no network, no credentials)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse


class Reply:
    def __init__(self, status=200, doc=None, headers=None, drop=False):
        self.status, self.doc, self.headers, self.drop = status, doc, dict(headers or {}), drop


class Loopback:
    """Runs ``handler(req) -> Reply`` on 127.0.0.1. ``req`` has method, path, query, headers, body."""

    def __init__(self, handler):
        outer = self
        self.handler, self.requests = handler, []

        class Req:
            pass

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _do(self):
                length = int(self.headers.get("Content-Length") or 0)
                raw = self.rfile.read(length) if length else b""
                url = urlparse(self.path)
                req = Req()
                req.method, req.path, req.query = self.command, url.path, {k: v[0] for k, v in parse_qs(url.query).items()}
                req.raw_query, req.headers = url.query, {k.lower(): v for k, v in self.headers.items()}
                req.body = json.loads(raw) if raw else None
                outer.requests.append(req)
                reply = outer.handler(req)
                if reply.drop:
                    self.connection.close()
                    return
                body = b"" if reply.doc is None else json.dumps(reply.doc).encode()
                self.send_response(reply.status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                for key, value in reply.headers.items():
                    self.send_header(key, str(value))
                self.end_headers()
                self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = _do

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), H)
        self.server.daemon_threads = True
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.02}, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def seen(self, method=None, prefix=""):
        return [r for r in self.requests if (method is None or r.method == method) and r.path.startswith(prefix)]


class Clock:
    """Fake time: ``sleep`` advances ``monotonic``, so waits finish instantly and timeouts are testable."""

    def __init__(self, start=1000.0):
        self.now = start
        self.slept = []

    def sleep(self, seconds):
        self.slept.append(seconds)
        self.now += seconds

    def monotonic(self):
        return self.now
