"""The admin socket (server side) and :class:`ConciergeClient` (what the engine calls).

Protocol: one JSON object per line, one request per connection. The server reads at most 64 KiB, answers with one JSON
line and closes. Requests::

    {"op": "issue", "tour": ..., "guest": ..., "attempt": ..., "bindings": [...], "expires_at": ..., "budget_usd": ...,
     "max_concurrent": 2}                               ->  {"ok": true, "token": "mt_..."}
    {"op": "revoke", "attempt": "a-1"} / {"op": "revoke", "tour": "t-1"}   ->  {"ok": true, "revoked": 1}
    {"op": "usage", "attempt": "a-1"}                   ->  {"ok": true, "usage": {...}}
    {"op": "health"}                                    ->  {"ok": true, "health": {...}}

Failures are ``{"ok": false, "kind": "invalid|exists|not_found|forbidden|bad_request", "error": "..."}``.

Access control is twofold: the socket file is created ``0600`` by systemd as root (see ``install.py``), and the server
reads ``SO_PEERCRED`` on every connection and serves only uid 0 (:func:`check_peer`). The second check holds even if the
file mode is ever wrong.
"""

from __future__ import annotations

import json
import os
import socket
import socketserver
import struct
import threading

from motel.concierge.errors import AdminRefused, AttemptNotFound, ConciergeError
from motel.concierge.registry import ID_RE, Registry

DEFAULT_SOCKET = "/run/motel/concierge.sock"
MAX_REQUEST = 64 * 1024
MAX_RESPONSE = 1024 * 1024
IO_TIMEOUT_S = 5.0
_CRED = struct.Struct("3i")  # struct ucred: pid, uid, gid


def peer_credentials(sock: socket.socket) -> tuple[int, int, int]:
    """(pid, uid, gid) of the process on the other end of a unix socket."""
    return _CRED.unpack(sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, _CRED.size))


def check_peer(creds: tuple[int, int, int] | None, allowed_uids=frozenset({0})) -> bool:
    """True only for a peer whose uid is allowed (root by default). Missing credentials are never allowed."""
    if not creds or len(creds) != 3:
        return False
    return creds[1] in allowed_uids


def listen_fds(environ=None, pid: int | None = None) -> list[int]:
    """File descriptors passed by systemd socket activation (``LISTEN_FDS``), or an empty list."""
    env = os.environ if environ is None else environ
    try:
        if int(env.get("LISTEN_PID", "")) != (os.getpid() if pid is None else pid):
            return []
        count = int(env.get("LISTEN_FDS", "0"))
    except ValueError:
        return []
    return list(range(3, 3 + count)) if count > 0 else []


def dispatch(registry: Registry, request: dict, health_extra=None, endpoints=None) -> dict:
    """Run one admin request against the registry; never raises."""
    op = request.get("op")
    try:
        if op == "issue":
            token = registry.issue(tour=request.get("tour"), guest=request.get("guest"), attempt=request.get("attempt"),
                                   bindings=request.get("bindings"), expires_at=request.get("expires_at"),
                                   budget_usd=request.get("budget_usd"), max_concurrent=request.get("max_concurrent", 2))
            return {"ok": True, "token": token}
        if op == "revoke":
            return {"ok": True, "revoked": registry.revoke(attempt=request.get("attempt"), tour=request.get("tour"))}
        if op == "endpoint":
            attempt = request.get("attempt")
            if not isinstance(attempt, str) or not ID_RE.fullmatch(attempt):
                raise AdminRefused(f"bad attempt id {attempt!r}", "invalid")
            registry.get(attempt)  # AttemptNotFound when unknown
            port = endpoints.port_of(attempt) if endpoints is not None else None
            if port is None:
                raise AdminRefused(f"attempt {attempt} has no open listener (it is revoked or expired, or no port was free)", "gone")
            return {"ok": True, "port": port}
        if op == "usage":
            return {"ok": True, "usage": registry.usage(request.get("attempt"))}
        if op == "health":
            health = registry.health()
            if health_extra:
                health.update(health_extra())
            return {"ok": True, "health": health}
    except AdminRefused as exc:
        return {"ok": False, "kind": exc.kind, "error": exc.message}
    except (TypeError, ValueError) as exc:
        return {"ok": False, "kind": "bad_request", "error": f"malformed request ({type(exc).__name__})"}
    return {"ok": False, "kind": "bad_request", "error": f"unknown op {op!r}"}


class _Handler(socketserver.StreamRequestHandler):
    timeout = IO_TIMEOUT_S

    def handle(self):
        try:
            line = self.rfile.readline(MAX_REQUEST + 1)
            if len(line) > MAX_REQUEST:
                reply = {"ok": False, "kind": "bad_request", "error": "request too large"}
            else:
                try:
                    request = json.loads(line)
                except ValueError:
                    request = None
                if isinstance(request, dict):
                    reply = dispatch(self.server.registry, request, self.server.health_extra, self.server.endpoints)
                else:
                    reply = {"ok": False, "kind": "bad_request", "error": "send one JSON object per line"}
            self.wfile.write(json.dumps(reply).encode() + b"\n")
        except OSError:
            pass


class AdminServer(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
    daemon_threads = True
    request_queue_size = 16

    def __init__(self, registry: Registry, *, path: str | None = None, fd: int | None = None, allowed_uids=frozenset({0}),
                 health_extra=None, endpoints=None):
        """Serve on ``path`` (created here, mode 0600) or on ``fd`` (a socket systemd already created and secured)."""
        self.registry = registry
        self.allowed_uids = frozenset(allowed_uids)
        self.health_extra = health_extra
        self.endpoints = endpoints
        self.path = path
        super().__init__(path or "", _Handler, bind_and_activate=False)
        if fd is not None:
            self.socket.close()
            self.socket = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM, fileno=fd)
        elif path:
            self._bind_private(path)
        else:
            raise ValueError("AdminServer needs a path or an inherited fd")

    def _bind_private(self, path: str) -> None:
        directory = os.path.dirname(path)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        try:
            if os.path.exists(path) or os.path.islink(path):
                os.unlink(path)  # a stale socket from a previous run
        except OSError:
            pass
        old = os.umask(0o177)
        try:
            self.socket.bind(path)
        finally:
            os.umask(old)
        os.chmod(path, 0o600)
        self.socket.listen(self.request_queue_size)

    def verify_request(self, request, client_address):
        try:
            return check_peer(peer_credentials(request), self.allowed_uids)
        except OSError:
            return False

    def serve_in_thread(self) -> threading.Thread:
        thread = threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True, name="concierge-admin")
        thread.start()
        return thread


class ConciergeClient:
    """Talks to the admin socket. Needs root: the socket refuses everyone else."""

    def __init__(self, path: str = DEFAULT_SOCKET, timeout: float = 10.0):
        self.path = path
        self.timeout = timeout

    def _call(self, request: dict) -> dict:
        data = json.dumps(request).encode() + b"\n"
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
                sock.settimeout(self.timeout)
                sock.connect(self.path)
                sock.sendall(data)
                sock.shutdown(socket.SHUT_WR)
                chunks, size = [], 0
                while True:
                    chunk = sock.recv(65536)
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_RESPONSE:
                        raise ConciergeError("the concierge sent an oversized reply")
        except FileNotFoundError:
            raise ConciergeError(f"the concierge is not running (no socket at {self.path})") from None
        except PermissionError:
            raise ConciergeError(f"permission denied on {self.path}; only root may use the concierge", "sudo -v") from None
        except (ConnectionResetError, BrokenPipeError):
            raise ConciergeError(f"the concierge closed the connection at {self.path} (only root is served)", "sudo -v") from None
        except (ConnectionError, TimeoutError, OSError) as exc:
            raise ConciergeError(f"cannot talk to the concierge at {self.path} ({type(exc).__name__})") from None
        try:
            reply = json.loads(b"".join(chunks))
        except ValueError:
            raise ConciergeError(f"the concierge closed the connection at {self.path} without an answer (only root is served)", "sudo -v") from None
        if not isinstance(reply, dict):
            raise ConciergeError("the concierge sent an unexpected reply")
        if not reply.get("ok"):
            kind, message = reply.get("kind", "refused"), str(reply.get("error", "refused"))
            raise (AttemptNotFound(message) if kind == "not_found" else AdminRefused(message, kind))
        return reply

    def issue(self, *, tour: str, guest: str, attempt: str, bindings: list[str], expires_at: str, budget_usd: float,
              max_concurrent: int = 2) -> str:
        reply = self._call({"op": "issue", "tour": tour, "guest": guest, "attempt": attempt, "bindings": list(bindings),
                            "expires_at": expires_at, "budget_usd": budget_usd, "max_concurrent": max_concurrent})
        return reply["token"]

    def revoke(self, *, attempt: str | None = None, tour: str | None = None) -> int:
        request = {"op": "revoke"}
        if attempt is not None:
            request["attempt"] = attempt
        if tour is not None:
            request["tour"] = tour
        return int(self._call(request)["revoked"])

    def usage(self, *, attempt: str) -> dict:
        return self._call({"op": "usage", "attempt": attempt})["usage"]

    def endpoint(self, *, attempt: str) -> dict:
        """Where the attempt's own listener is: ``{"port": int}`` on 127.0.0.1 of the controller. The engine forwards the
        guest's fixed ``concierge_port`` to it. Raises :class:`AdminRefused` (kind ``gone``) once the attempt is revoked or expired."""
        return {"port": int(self._call({"op": "endpoint", "attempt": attempt})["port"])}

    def health(self) -> dict:
        return self._call({"op": "health"})["health"]
