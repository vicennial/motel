"""Loopback fake upstreams (plain and TLS) and a harness that starts a real concierge against them. No network."""
import http.client
import json
import os
import shutil
import socket
import ssl
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from motel.concierge import fdstore
from motel.concierge.service import Concierge
from motel.concierge.settings import parse_tours_section
from platform_marks import ON_LINUX

REAL = "sk-test-real-0123456789abcdefghij"  # the "real key" in every test; it must never reach a guest or the ledger
MODEL = "claude-sonnet-4-6"
BODY = {"model": MODEL, "max_tokens": 100, "messages": [{"role": "user", "content": "hi"}]}
OK_COST = (100 * 3 + 50 * 15) / 1e6  # the default upstream reply: 100 in, 50 out, on a $3/$15 model


def short_dir(prefix="ms") -> str:
    """A directory with a short path, for unix sockets (the limit is about 100 bytes, and TMPDIR can be long)."""
    for base in ("/tmp", "/var/tmp", tempfile.gettempdir()):
        if os.path.isdir(base) and len(base) < 40:
            return tempfile.mkdtemp(prefix=prefix, dir=base)
    return tempfile.mkdtemp(prefix=prefix)


def default_reply(req, h):
    body = json.dumps({"id": "msg_1", "type": "message", "model": MODEL, "content": [{"type": "text", "text": "ok"}],
                       "usage": {"input_tokens": 100, "output_tokens": 50}}).encode()
    return 200, {"Content-Type": "application/json"}, [body]


class FakeUpstream:
    """Records every request; answers with ``reply(req, handler) -> (status, headers, chunks)``."""

    def __init__(self, reply=default_reply, tls=None):
        self.requests: list = []
        self.reply = reply
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _go(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else b""
                req = {"method": self.command, "path": self.path, "headers": list(self.headers.items()), "body": body}
                owner.requests.append(req)
                status, headers, chunks = owner.reply(req, self)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.end_headers()
                for chunk in chunks:
                    if callable(chunk):
                        chunk = chunk()
                    self.wfile.write(chunk)
                    self.wfile.flush()

            do_GET = do_POST = _go

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.server.daemon_threads = True
        if tls:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            ctx.load_cert_chain(tls["cert"], tls["key"])
            self.server.socket = ctx.wrap_socket(self.server.socket, server_side=True)
        self.scheme = "https" if tls else "http"
        self.port = self.server.server_address[1]
        self.url = f"{self.scheme}://127.0.0.1:{self.port}"
        threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True).start()

    def header(self, index: int, name: str) -> list:
        return [v for k, v in self.requests[index]["headers"] if k.lower() == name.lower()]

    def close(self):
        self.server.shutdown()
        self.server.server_close()


def make_tls(directory: str) -> dict | None:
    """A throwaway CA and a leaf for 127.0.0.1, made with the openssl CLI; None when it is not installed."""
    if not shutil.which("openssl"):
        return None
    d = lambda name: os.path.join(directory, name)  # noqa: E731

    def run(*args, **kw):
        subprocess.run(["openssl", *args], check=True, capture_output=True, cwd=directory, **kw)

    run("req", "-x509", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", "ca.key", "-out", "ca.pem",
        "-days", "2", "-subj", "/CN=motel test ca", "-addext", "basicConstraints=critical,CA:TRUE",
        "-addext", "keyUsage=critical,keyCertSign,cRLSign")
    run("req", "-newkey", "ec", "-pkeyopt", "ec_paramgen_curve:prime256v1", "-nodes", "-keyout", "leaf.key", "-out", "leaf.csr",
        "-subj", "/CN=localhost")
    with open(d("ext.cnf"), "w") as handle:
        handle.write("subjectAltName=IP:127.0.0.1,DNS:localhost\nbasicConstraints=CA:FALSE\nkeyUsage=digitalSignature\n"
                     "extendedKeyUsage=serverAuth\n")
    run("x509", "-req", "-in", "leaf.csr", "-CA", "ca.pem", "-CAkey", "ca.key", "-CAcreateserial", "-out", "leaf.pem", "-days", "2",
        "-extfile", "ext.cnf")
    return {"ca": d("ca.pem"), "cert": d("leaf.pem"), "key": d("leaf.key")}


class FakeSystemd:
    """A model of systemd's file-descriptor store behind a notify socket, in this process.

    Like the real one: a stored socket stays bound while the service is down; entries are de-duplicated by open file (the
    inode), names are NOT unique, FDSTOREREMOVE drops every entry of the name, FileDescriptorStoreMax= is a limit (a socket beyond
    it is dropped), and entries are listed newest first (``newest_first``) or oldest first. A thread drains the socket, so the
    tests never block on a full queue; ``sync()`` waits until everything sent so far has been processed."""

    def __init__(self, limit: int = 512, newest_first: bool = True):
        self.dir = short_dir("fs")
        self.path = os.path.join(self.dir, "n.sock")
        self.sock = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        self.sock.bind(self.path)
        self.entries: list[tuple[str, int]] = []  # (name, fd), in the order the next start would receive them
        self.log: list[str] = []
        self.limit, self.newest_first = limit, newest_first
        self._lock = threading.Lock()
        self._pings: dict[str, threading.Event] = {}
        self._closed = False
        self._thread = threading.Thread(target=self._run, daemon=True, name="fake-systemd")
        self._thread.start()

    def _run(self):
        while True:
            try:
                msg, fds, _, _ = socket.recv_fds(self.sock, 4096, 8)
            except OSError:
                return
            if self._closed:
                return
            fields = dict(line.split("=", 1) for line in msg.decode().split("\n") if line)
            with self._lock:
                self._handle(fields, fds)

    def _handle(self, fields, fds):
        if "PING" in fields:
            self._pings[fields["PING"]].set()
        elif fields.get("FDSTORE") == "1":
            fd, name = fds[0], fields["FDNAME"]
            for extra in fds[1:]:
                os.close(extra)
            ino = os.fstat(fd).st_ino
            if any(os.fstat(entry).st_ino == ino for _, entry in self.entries):
                os.close(fd)
                self.log.append("dup:" + name)
            elif len(self.entries) >= self.limit:
                os.close(fd)
                self.log.append("full:" + name)
            else:
                self.entries.insert(0, (name, fd)) if self.newest_first else self.entries.append((name, fd))
                self.log.append("add:" + name)
        elif fields.get("FDSTOREREMOVE") == "1":
            keep = []
            for name, fd in self.entries:
                if name == fields["FDNAME"]:
                    os.close(fd)
                else:
                    keep.append((name, fd))
            self.entries = keep
            self.log.append("remove:" + fields["FDNAME"])

    def sync(self, timeout: float = 10.0) -> None:
        ping = f"p{time.monotonic_ns()}"
        event = self._pings[ping] = threading.Event()
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            sender.sendto(f"PING={ping}\n".encode(), self.path)
        assert event.wait(timeout), "the fake systemd did not process the notify queue"

    def env(self) -> dict:
        return {"NOTIFY_SOCKET": self.path, "LISTEN_PID": str(os.getpid())}

    def store(self, limit: int | None = None) -> fdstore.FdStore:
        return fdstore.FdStore(self.env(), limit=limit or self.limit)

    def inherited(self):
        """What the next start would be handed: duplicates of the stored fds, split by the real ``inherited_listeners``."""
        self.sync()
        with self._lock:
            pairs = [(os.dup(fd), name) for name, fd in self.entries]
        real = fdstore.listen_fd_names
        fdstore.listen_fd_names = lambda environ=None, pid=None: pairs
        try:
            return fdstore.inherited_listeners()
        finally:
            fdstore.listen_fd_names = real

    def names(self) -> list:
        """``[(name, port)]`` of the stored sockets."""
        self.sync()
        with self._lock:
            out = []
            for name, fd in self.entries:
                dup = socket.socket(fileno=os.dup(fd))
                out.append((name, dup.getsockname()[1]))
                dup.close()
            return out

    def put(self, name: str, sock: socket.socket) -> None:
        """Put a socket in the store directly, as if an earlier run had stored it (names may repeat)."""
        with self._lock:
            entry = (name, os.dup(sock.fileno()))
            self.entries.insert(0, entry) if self.newest_first else self.entries.append(entry)

    def close(self) -> None:
        self._closed = True
        with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM) as sender:
            try:
                sender.sendto(b"PING=x\n", self.path)
            except OSError:
                pass
        self.sock.close()
        with self._lock:
            for _, fd in self.entries:
                os.close(fd)
            self.entries = []
        shutil.rmtree(self.dir, ignore_errors=True)


class Harness:
    """A started concierge with one binding per upstream, a temp state dir and a temp key file."""

    def __init__(self, upstream_url: str, *, tls_ca: str | None = None, binding: dict | None = None, tour: dict | None = None,
                 clock=time.time, admin: bool = False, allowed_uids=frozenset({0}), extra_bindings: dict | None = None,
                 root: str | None = None, sweep_interval: float = 1.0, systemd: FakeSystemd | None = None):
        if not ON_LINUX:  # the concierge is a Linux service: SO_PEERCRED on its admin socket, systemd's fd store
            pytest.skip("the concierge service only runs on Linux (SO_PEERCRED, SOCK_CLOEXEC, systemd fd store)")
        self.root = root or tempfile.mkdtemp(prefix="mc")
        self.key_file = os.path.join(self.root, "anthropic.key")
        with open(self.key_file, "w") as handle:
            handle.write(REAL + "\n")
        os.makedirs(os.path.join(self.root, "state"), exist_ok=True)
        table = {"enabled": True, "max_usd_model_per_guest": 5.0,
                 "bindings": {"anthropic": {"upstream": upstream_url, "auth": "x-api-key", "key_file": self.key_file,
                                            "paths": ["/v1/messages", "/v1/messages/count_tokens"], **(binding or {})}}}
        table["bindings"].update(extra_bindings or {})
        table.update(tour or {})
        self.cfg = parse_tours_section(table)
        ctx = ssl.create_default_context(cafile=tls_ca) if tls_ca else None
        self.sock_dir = short_dir() if admin else None
        self.admin_path = os.path.join(self.sock_dir, "a.sock") if admin else None
        self.clock = clock
        self.systemd = systemd or FakeSystemd()  # every attempt's socket is kept in it, as under systemd
        self._kw = dict(tls_context=ctx, clock=clock, admin_path=self.admin_path, allowed_uids=allowed_uids, fsync=False,
                        ephemeral_ports=True, sweep_interval=sweep_interval)
        self.concierge = Concierge(self.cfg, os.path.join(self.root, "state"), store=self.systemd.store(), **self._kw).start()
        self.registry = self.concierge.registry
        self.ledger_path = self.concierge.ledger.path
        self._ports: dict = {}  # token -> the port of its attempt's own listener
        self._probe_port = None

    @property
    def port(self) -> int:
        """A listener to aim raw requests at: the first attempt's. With none issued yet, a throwaway attempt is issued."""
        if self._ports:
            return next(iter(self._ports.values()))
        if self._probe_port is None:
            self.issue("probe-0", tour="probe", guest="probe")
            return next(iter(self._ports.values()))
        return self._probe_port

    def port_of(self, token_or_attempt: str) -> int:
        if token_or_attempt in self._ports:
            return self._ports[token_or_attempt]
        return self.registry.get(token_or_attempt).port

    def issue(self, attempt="a-1", *, tour="t-1", guest="g-1", bindings=("anthropic",), budget=1.0, ttl=3600, max_concurrent=2) -> str:
        from datetime import UTC, datetime
        expires = datetime.fromtimestamp(self.clock() + ttl, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")
        token = self.registry.issue(tour=tour, guest=guest, attempt=attempt, bindings=list(bindings), expires_at=expires,
                                    budget_usd=budget, max_concurrent=max_concurrent)
        self._ports[token] = self.registry.get(attempt).port
        return token

    def post(self, path, token=None, body=BODY, headers=None, auth="x-api-key", method="POST", raw_body=None, timeout=20):
        hdrs = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
        if token:
            hdrs.update({"x-api-key": token} if auth == "x-api-key" else {"Authorization": f"Bearer {token}"})
        hdrs.update(headers or {})
        payload = raw_body if raw_body is not None else (json.dumps(body).encode() if body is not None else None)
        return request(self._ports.get(token) or self.port, method, path, hdrs, payload, timeout)

    def ledger_text(self) -> str:
        with open(self.ledger_path) as handle:
            return handle.read()

    def events(self, kind=None) -> list:
        out = [json.loads(line) for line in self.ledger_text().splitlines() if line]
        return [e for e in out if kind is None or e["ev"] == kind]

    def restart(self, **overrides):
        """Stop the concierge and start another on the same state directory; the sockets come back from the fake systemd."""
        self.concierge.stop()
        stored, _ = self.systemd.inherited()
        kw = {**self._kw, **overrides}
        self.concierge = Concierge(self.cfg, os.path.join(self.root, "state"), store=self.systemd.store(), inherited=stored, **kw).start()
        self.registry = self.concierge.registry
        return self.concierge

    def settle(self) -> None:
        """Wait until the fake systemd has processed what the concierge sent it (a revoke closes the stored socket)."""
        self.systemd.sync()

    def close(self):
        self.concierge.stop()
        self.systemd.close()
        shutil.rmtree(self.root, ignore_errors=True)
        if self.sock_dir:
            shutil.rmtree(self.sock_dir, ignore_errors=True)


def request(port, method, path, headers=None, body=None, timeout=20):
    """-> (status, headers as a list of pairs, body). Uses http.client, so chunked replies are decoded."""
    conn = http.client.HTTPConnection("127.0.0.1", port, timeout=timeout)
    try:
        conn.request(method, path, body=body, headers=headers or {})
        resp = conn.getresponse()
        return resp.status, resp.getheaders(), resp.read()
    finally:
        conn.close()


def raw_exchange(port, data: bytes, timeout=5) -> bytes:
    """Send raw bytes, return everything the server answers before closing."""
    with socket.create_connection(("127.0.0.1", port), timeout=timeout) as sock:
        out = b""
        try:
            sock.sendall(data)
            sock.shutdown(socket.SHUT_WR)
            while True:
                chunk = sock.recv(65536)
                if not chunk:
                    break
                out += chunk
        except (TimeoutError, OSError):
            pass
        return out


def error_kind(body: bytes) -> str:
    return json.loads(body)["error"]["type"]


def sse(*events) -> list:
    """Server-sent events as byte chunks: each ``(name, data)`` pair becomes one chunk."""
    return [f"event: {name}\ndata: {json.dumps(data)}\n\n".encode() for name, data in events]
