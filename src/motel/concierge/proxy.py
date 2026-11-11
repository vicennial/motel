"""The guest listener: ``http://127.0.0.1:<port>/u/<binding>/<upstream path>``.

Request flow (every refusal is a JSON error ``{"type": "error", "error": {"type": "motel_<reason>", "message": ...}}``):

1. The request line must be ``/u/<binding>/<path>``. Absolute-form targets, ``%`` escapes and odd characters are refused.
2. The placeholder token comes from ``x-api-key`` or ``Authorization: Bearer`` and is looked up by its SHA-256.
   Unknown, expired or revoked: 401.
3. The binding must be one the token carries and one the operator configured, the path must be in the binding's
   allowed list and the method must be allowed: 403 / 405. Nothing the guest sends (host, query, header, body) selects a
   destination or a credential; the origin, auth style and key come from configuration only.
4. The body is capped (413). A metered request (POST that is not ``count_tokens``) must be JSON with an output cap
   (``max_tokens``; required for the ``anthropic`` format). A worst-case cost is reserved before dispatch (403 when the
   remaining budget cannot cover it, 429 on concurrency).
5. Every inbound auth header, hop-by-hop header, ``host`` and forwarding header is dropped. The real credential is
   injected and the request goes to the binding's fixed origin over verified TLS. Redirects are never followed.
6. The response is streamed back unchanged (chunked), except that any echo of the real key in headers or body is
   replaced by ``[redacted]`` and ``Set-Cookie`` is dropped. Usage is parsed on the way through and the reservation is
   settled to the actual cost.
"""

from __future__ import annotations

import http.client
import io
import json
import logging
import math
import re
import secrets
import socket
import ssl
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from motel.concierge import policy, pricing, redact, usage
from motel.concierge.keys import KeyStore, KeyUnavailable
from motel.concierge.registry import Attempt, Refusal, Registry
from motel.concierge.settings import (
    NODE_BYTES,
    PATH_RE,
    RAW_COPIES,
    TEXT_COPIES,
    BindingConfig,
    TourConfig,
    worst_case_charge,
)

MAX_PARSE_BYTES = 4 * 1024 * 1024  # buffered copy of a non-streamed response kept for usage parsing
MAX_RESPONSE_BYTES = 128 * 1024 * 1024
REQUEST_DEADLINE_S = 1800.0
CONNECT_TIMEOUT_S = 10.0
GUEST_IO_TIMEOUT_S = 60.0  # a single send or receive towards the guest
HEADER_DEADLINE_S = 10.0  # the whole request line plus headers, however slowly they trickle in
BODY_GRACE_S = 10.0  # body deadline = this + length / MIN_BODY_RATE
MIN_BODY_RATE = 64 * 1024  # bytes per second a guest must sustain while sending its body
MAX_TARGET = 8192
PENDING_AUTH = 2  # connections per listener that may be open before they have authenticated
PENDING_WAIT_S = 2.0  # how long a further connection waits for one of those slots before it is dropped
LINGER_MAX_BYTES = 16 << 20  # and the most it reads: past the largest body a request may carry (the time limit usually ends it first)
LINGER_TOTAL_S = 1.0  # after an early reply: the most time spent letting the client's body drain
QUERY_RE = re.compile(r"^[A-Za-z0-9._~!$&'()*+,;=:@/?%-]*$")
LENGTH_RE = re.compile(r"^[0-9]{1,12}$")
HEADER_NAME_RE = re.compile(r"^[!#$%&'*+.^_`|~0-9A-Za-z-]{1,128}$")

HOP_BY_HOP = frozenset({"connection", "keep-alive", "proxy-authenticate", "proxy-authorization", "proxy-connection", "te",
                        "trailer", "transfer-encoding", "upgrade"})
# Dropped from the guest's request in addition to hop-by-hop headers. Auth headers by name (the contract's four plus
# common variants), and anything that could name another destination or organisation.
DROP_REQUEST = frozenset({"authorization", "x-api-key", "proxy-authorization", "cookie", "cookie2", "host", "content-length",
                          "accept-encoding", "expect", "forwarded", "via", "x-real-ip", "openai-organization", "openai-project",
                          "x-http-method-override", "x-original-url", "x-rewrite-url"})
DROP_RESPONSE = frozenset(HOP_BY_HOP | {"content-length", "content-encoding", "set-cookie", "set-cookie2"})
SECRETISH = ("auth", "api-key", "apikey", "secret", "token", "cookie", "password", "credential")


class DuplicateKey(ValueError):
    """A JSON object repeats a key: two parsers could disagree on its value."""


def _unique_pairs(pairs):
    """Every object: an exact repeat (after unescaping, so ``"a"`` and ``"\\u0061"`` count) is refused."""
    out = {}
    for key, value in pairs:
        if key in out:
            raise DuplicateKey(key)
        out[key] = value
    return out


def _no_constant(name):
    raise ValueError(f"{name} is not valid JSON")


def _finite_float(text):
    value = float(text)
    if value != value or value in (float("inf"), float("-inf")):
        raise ValueError("number out of range")
    return value


_TOKEN = re.compile(r'[\s,:\]}]*(?:"(?:[^"\\]++|\\.)*+"|[\[{]|-?(?:NaN|Infinity)|-?[0-9][0-9.eE+-]*|true|false|null)')


def decode_body(body: bytes) -> str:
    """The text ``json.loads`` would decode the bytes to (UTF-8, UTF-16 or UTF-32, by their first bytes)."""
    return body.decode(json.detect_encoding(body), "surrogatepass")


_WIDE_UTF8 = re.compile(rb"[\xc4-\xf7]")  # a lead byte for a character above U+00FF
_ASTRAL_UTF8 = re.compile(rb"[\xf0-\xf7]")  # a lead byte for a character above U+FFFF


def text_bytes_bound(body: bytes, encoding: str | None = None) -> int:
    """An upper bound for the memory of the text ``decode_body(body)`` returns, by Python's string kinds: 1 byte per character
    for ASCII and Latin-1, 2 for text within the Basic Multilingual Plane, 4 when any character lies beyond it. The kind is
    read from the raw bytes, so the bound is known before anything is decoded."""
    n = len(body)
    encoding = encoding or json.detect_encoding(body)
    if encoding.startswith("utf-16"):
        return 2 * n  # at most n/2 characters, each at most 4 bytes
    if encoding.startswith("utf-32"):
        return n
    if body.isascii() or not _WIDE_UTF8.search(body):
        return n
    return 4 * n if _ASTRAL_UTF8.search(body) else 2 * n


def count_nodes(text: str, limit: int) -> int:
    """How many keys plus values the JSON text holds, counted without building anything (stops at ``limit + 1``), so a few
    megabytes of ``[],[],[],...`` is refused before the parser allocates an object for each. Every valid JSON text
    tokenises completely, so a document the scan cannot follow is one the parser refuses (and it stops there)."""
    pos, count, match = 0, 0, _TOKEN.match
    while True:
        found = match(text, pos)
        if found is None:
            return count
        count += 1
        if count > limit:
            return count
        pos = found.end()


def too_many_nodes(text: str, limit: int) -> bool:
    return count_nodes(text, limit) > limit


def parse_json(body: bytes | str):
    """Strict parse. Exact duplicate keys are refused at every depth. Keys that differ only in letter case are refused in
    the top-level object only: those are the request fields (``max_tokens``, ``model``, ...) that a case-insensitive
    parser could read differently; nested objects (tool schemas, tool inputs) may legitimately hold ``id`` and ``ID``."""
    obj = json.loads(body if isinstance(body, str) else decode_body(body), object_pairs_hook=_unique_pairs, parse_constant=_no_constant, parse_float=_finite_float)
    if isinstance(obj, dict):
        folded = set()
        for key in obj:
            low = key.casefold()
            if low in folded:
                raise DuplicateKey(key)
            folded.add(low)
    return obj


def canonical_json(obj) -> bytes:
    """The compact UTF-8 form that goes upstream (ASCII-escaped only if a lone surrogate makes UTF-8 impossible)."""
    try:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, allow_nan=False).encode("utf-8")
    except UnicodeEncodeError:
        return json.dumps(obj, separators=(",", ":"), ensure_ascii=True, allow_nan=False).encode("ascii")


def drop_request_header(name: str) -> bool:
    low = name.lower()
    return (low in DROP_REQUEST or low in HOP_BY_HOP or low.startswith("x-forwarded-")
            or any(word in low for word in SECRETISH))


def error_body(kind: str, message: str) -> bytes:
    return json.dumps({"type": "error", "error": {"type": f"motel_{kind}", "message": message}}).encode()


class _Reply(Exception):
    def __init__(self, status: int, kind: str, message: str, headers: dict | None = None):
        super().__init__(message)
        self.status, self.kind, self.message, self.headers = status, kind, message, headers or {}


def settle_amount(*, binding: BindingConfig, req_model: str | None, got: usage.Usage | None, status: int, reserved: float,
                  max_out_total: int, speed_mult: float = 1.0) -> tuple[float, str, bool]:
    """The cost to charge for a finished request: ``(usd, outcome, price_unknown)``.

    Upstream error and redirect responses are not billed. A response whose usage is complete is charged by that usage
    (the dearer of the request model and the response model). One that ended after the input side was reported but
    before the output total is charged for the input plus the worst-case output. Anything unmetered is charged at the
    reserved worst case.
    """
    req_price, req_known = pricing.lookup(req_model, binding.pricing, binding.default_price)
    if status >= 300:
        return 0.0, "redirect" if status < 400 else "upstream_error", not req_known
    if got is not None and (got.final or got.saw_input):
        resp_price, resp_known = pricing.lookup(got.model, binding.pricing, binding.default_price) if got.model else (req_price, req_known)
        candidates = [(req_price, req_known), (resp_price, resp_known)]
        best, known = max(candidates, key=lambda c: c[0].output + c[0].input)
        if got.speed == "fast":
            speed_mult = max(speed_mult, policy.FAST_MULTIPLIER)
        if speed_mult != 1.0:
            best = best.scaled(speed_mult)
        parts = dict(input_tokens=got.input_tokens, cache_read=got.cache_read, cache_write_5m=got.cache_write_5m,
                     cache_write_1h=got.cache_write_1h, extra_usd=got.extra_usd)
        if got.final:
            return pricing.cost_usd(best, output_tokens=got.output_tokens, **parts), "ok", not known
        return pricing.cost_usd(best, output_tokens=max_out_total, **parts), "partial", not known
    return reserved, "unmetered", not req_known


@dataclass
class Plan:
    """What the guest asked for, as far as cost is concerned."""

    body: bytes
    model: str | None
    reserve_usd: float
    max_out_total: int
    speed_mult: float = 1.0  # fast mode: the reservation and the settlement use this multiple of the standard price


@dataclass
class _Flight:
    """What happened to one admitted request; filled in by the relay, read when settling."""

    started: float
    status: int = 0
    got: usage.Usage | None = None
    sent: int = 0
    usd: float = 0.0
    outcome: str = ""
    price_unknown: bool = False
    interrupted: str = ""
    fixed: bool = False  # usd/outcome were decided by a failure path, not by the response
    settled: bool = False


class AbortHandle:
    """Lets a revocation cut a live upstream connection."""

    def __init__(self):
        self.conn: http.client.HTTPConnection | None = None
        self.sock: socket.socket | None = None  # kept here: http.client forgets conn.sock for close-delimited replies
        self.aborted = False

    def abort(self) -> None:
        self.aborted = True
        sock = self.sock
        if sock is not None:
            try:
                sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass


class ByteBudget:
    """A cap on request-body memory: host-wide, and per attempt so that one attempt cannot take the whole budget."""

    def __init__(self, limit: int, per_owner: int | None = None):
        self.limit = limit
        self.per_owner = limit if per_owner is None else min(limit, per_owner)
        self.used = 0
        self._by_owner: dict = {}
        self._lock = threading.Lock()

    def acquire(self, n: int, owner: str = "") -> bool:
        with self._lock:
            held = self._by_owner.get(owner, 0)
            if self.used + n > self.limit or held + n > self.per_owner:
                return False
            self.used += n
            self._by_owner[owner] = held + n
            return True

    def release(self, n: int, owner: str = "") -> None:
        with self._lock:
            self.used = max(0, self.used - n)
            left = self._by_owner.get(owner, 0) - n
            if left > 0:
                self._by_owner[owner] = left
            else:
                self._by_owner.pop(owner, None)

    def held_by(self, owner: str) -> int:
        with self._lock:
            return self._by_owner.get(owner, 0)


class DeadlineReader(io.RawIOBase):
    """Reads from the guest socket, but never past a deadline: a client that trickles bytes cannot hold a thread."""

    def __init__(self, sock: socket.socket, idle_timeout: float):
        self.sock = sock
        self.idle_timeout = idle_timeout
        self.deadline = time.monotonic() + HEADER_DEADLINE_S

    def set_deadline(self, seconds: float) -> None:
        self.deadline = time.monotonic() + seconds

    def readable(self) -> bool:
        return True

    def readinto(self, buffer) -> int:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise TimeoutError("request deadline")
        self.sock.settimeout(min(remaining, self.idle_timeout))
        try:
            return self.sock.recv_into(buffer)
        finally:
            try:
                self.sock.settimeout(self.idle_timeout)
            except OSError:
                pass


@dataclass
class Core:
    cfg: TourConfig
    registry: Registry
    keys: KeyStore
    tls_context: ssl.SSLContext | None = None
    log: logging.Logger = logging.getLogger("motel.concierge")
    body_budget: ByteBudget | None = None

    def __post_init__(self):
        if self.tls_context is None:
            self.tls_context = ssl.create_default_context()
        if self.body_budget is None:
            # one attempt may take the larger of one worst-case request (nodes included) and a quarter of the host budget
            share = max(worst_case_charge(self.cfg.max_request_bytes, self.cfg.concierge_max_json_nodes), self.cfg.max_buffered_bytes // 4)
            self.body_budget = ByteBudget(self.cfg.max_buffered_bytes, share)


class _Pending:
    """A claim on one of a listener's few "has not authenticated yet" slots; released once, whichever path gets there first."""

    def __init__(self, sem: threading.BoundedSemaphore):
        self._sem = sem
        self._lock = threading.Lock()
        self._held = True

    def release(self) -> None:
        with self._lock:
            held, self._held = self._held, False
        if held:
            self._sem.release()


class GuestServer(ThreadingHTTPServer):
    """The listener of one attempt: it serves that attempt's token and nobody else's.

    Pool: ``max_concurrent + 2`` authenticated connections (counted by the registry, 429 beyond that) plus ``PENDING_AUTH``
    that have not authenticated yet. A connection beyond the second pending one waits ``PENDING_WAIT_S`` for a slot and is
    then dropped. So a hostile guest can fill only its own listener, never anyone else's."""

    daemon_threads = True
    request_queue_size = 64

    def __init__(self, sock: socket.socket, core: Core, attempt: str, pending: int = PENDING_AUTH):
        self.core = core
        self.attempt = attempt
        self._pending = threading.BoundedSemaphore(pending)
        self._claims: dict[int, _Pending] = {}
        super().__init__(sock.getsockname(), GuestHandler, bind_and_activate=False)
        self.socket.close()
        self.socket = sock
        self.server_address = sock.getsockname()

    def process_request(self, request, client_address):
        if not self._pending.acquire(timeout=PENDING_WAIT_S):  # the pre-auth slots are taken: drop, do not queue
            self.shutdown_request(request)
            return
        claim = _Pending(self._pending)
        thread = threading.Thread(target=self._serve_connection, args=(request, client_address, claim), daemon=True)
        thread.start()

    def _serve_connection(self, request, client_address, claim: _Pending) -> None:
        self._claims[threading.get_ident()] = claim
        try:
            self.finish_request(request, client_address)
        except Exception:  # noqa: BLE001
            self.handle_error(request, client_address)
        finally:
            self._claims.pop(threading.get_ident(), None)
            claim.release()
            self.shutdown_request(request)

    def authenticated(self) -> None:
        """The calling connection has proven which attempt it is: it no longer counts against the pre-auth slots."""
        claim = self._claims.get(threading.get_ident())
        if claim is not None:
            claim.release()


class GuestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "motel-concierge"
    sys_version = ""
    timeout = GUEST_IO_TIMEOUT_S

    def setup(self):
        super().setup()
        self.rfile.close()
        self._reader = DeadlineReader(self.connection, GUEST_IO_TIMEOUT_S)
        self.rfile = io.BufferedReader(self._reader)

    def handle_expect_100(self):
        """Do not invite the body before the request is authenticated and admitted (``_read_body`` sends the 100)."""
        self._expects_continue = True
        return True

    def log_message(self, format, *args):  # noqa: A002 - the stdlib's name; access lines are logged by us, without queries
        pass

    def send_error(self, code, message=None, explain=None):  # parse-level errors from the stdlib, in our shape
        kind = {400: "bad_request", 414: "bad_request", 431: "bad_request"}.get(code, "bad_request")
        self._reply_error(code if code in (400, 414, 431, 501) else 400, kind, "malformed request")

    # -- verbs -------------------------------------------------------------------------------------------------

    def do_POST(self):
        self._handle("POST")

    def do_GET(self):
        self._handle("GET")

    def _other(self):
        self._body_unread = True
        self._reply_error(405, "method_not_allowed", "method not allowed", {"Allow": "GET, POST"})

    do_PUT = do_DELETE = do_PATCH = do_HEAD = do_OPTIONS = _other

    # -- plumbing ----------------------------------------------------------------------------------------------

    _responded = False
    _body_unread = False
    _expects_continue = False
    _authed = False
    _held_conn: Attempt | None = None
    _held_bytes = 0
    _held_owner = ""

    def _reply_error(self, status: int, kind: str, message: str, headers: dict | None = None) -> None:
        if self._responded:
            self.close_connection = True
            return
        body = error_body(kind, message)
        self._responded = True
        self.close_connection = True
        try:
            self.send_response_only(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            for name, value in (headers or {}).items():
                self.send_header(name, value)
            self.end_headers()
            self.wfile.write(body)
        except (OSError, ssl.SSLError):  # the guest hung up before it read the refusal: nothing to tell, nothing to log
            pass

    def _handle(self, method: str) -> None:
        self.close_connection = True
        started = time.monotonic()
        core: Core = self.server.core
        self._attempt_id = None
        status_note = "?"
        try:
            self._serve(core, method)
            status_note = "done"
        except _Reply as reply:
            status_note = f"refused:{reply.kind}"
            self._release_body(core)  # before the reply, so the client never sees an answer while the charge is still held
            self._reply_error(reply.status, reply.kind, reply.message, reply.headers)
        except (BrokenPipeError, ConnectionResetError, TimeoutError, ssl.SSLError):
            status_note = "connection_lost"
            self.close_connection = True
        except Exception:  # noqa: BLE001 - a bug must not become a hung connection; log the type only
            core.log.exception("concierge handler error")
            status_note = "internal_error"
            try:
                self._reply_error(502, "internal_error", "the concierge failed to handle this request")
            except OSError:
                pass
        finally:
            if self._held_conn is not None:
                core.registry.close_connection(self._held_conn)
            self._release_body(core)
            core.log.info("guest %s attempt=%s %s %.0fms", method, self._attempt_id or "-", status_note,
                          (time.monotonic() - started) * 1000)
            self._linger()

    def _release_body(self, core: Core, plan: Plan | None = None) -> None:
        """Give the memory charge back (idempotent). With ``plan``, also drop the forwarded copy: it has been sent."""
        if plan is not None:
            plan.body = b""
        held, self._held_bytes = self._held_bytes, 0
        if held:
            core.body_budget.release(held, self._held_owner)

    def _linger(self) -> None:
        """After an early reply, let the client's unread request body drain so a reset does not eat our response.

        At most ``LINGER_TOTAL_S`` in total, however the client paces its bytes, authenticated or not. Before authentication
        the connection still holds one of its own listener's 2 pending slots, so a hostile client spends only its own attempt's
        capacity (listeners are per attempt) and never waits longer than that second."""
        if not self._body_unread:
            return
        try:
            self.connection.shutdown(socket.SHUT_WR)
            deadline = time.monotonic() + LINGER_TOTAL_S
            left = LINGER_MAX_BYTES
            while left > 0:
                remaining = deadline - time.monotonic()
                self.connection.settimeout(max(remaining, 0.0) if remaining > 0 else 0.0)
                try:
                    chunk = self.connection.recv(65536)
                except (BlockingIOError, TimeoutError):
                    break
                if not chunk:
                    break
                left -= len(chunk)
        except OSError:
            pass

    # -- the request -------------------------------------------------------------------------------------------

    def _serve(self, core: Core, method: str) -> None:
        cfg, registry = core.cfg, core.registry
        target = self.path
        if not target.startswith("/u/") or len(target) > MAX_TARGET:
            self._body_unread = True
            raise _Reply(400, "bad_request", "expected /u/<binding>/<upstream path>")
        raw_path, _, query = target.partition("?")
        binding_name, slash, rest = raw_path[3:].partition("/")
        upath = "/" + rest
        if (not slash or "%" in raw_path or "#" in target or not QUERY_RE.fullmatch(query) or not PATH_RE.fullmatch(upath)
                or ".." in upath.split("/") or "//" in upath):
            self._body_unread = True
            raise _Reply(400, "bad_request", "malformed path or query")

        for name, value in self.headers.items():  # nothing that could split or smuggle a header upstream
            if not HEADER_NAME_RE.fullmatch(name) or "\r" in value or "\n" in value or "\0" in value:
                self._body_unread = True
                raise _Reply(400, "bad_request", "malformed header")

        self._body_unread = True  # an authentication refusal is sent before any body is read, too
        att = self._authenticate(registry)
        self._authed = True
        self.server.authenticated()
        self._attempt_id = att.attempt
        if not registry.open_connection(att):  # one attempt cannot hold the whole connection pool
            registry.refuse(att, kind="concurrency", status=429, binding=binding_name, path=upath)
            raise _Reply(429, "concurrency", "this attempt already holds as many connections as it may", {"Retry-After": "1"})
        self._held_conn = att
        self._body_unread = True
        problem = registry.state_refusal(att)
        if problem is not None:
            registry.refuse(att, kind=problem.kind, status=problem.status, binding=binding_name, path=upath)
            raise _Reply(problem.status, problem.kind, problem.message)
        binding = cfg.bindings.get(binding_name)
        if binding is None or binding_name not in att.bindings:
            registry.refuse(att, kind="binding_denied", status=403, binding=binding_name, path=upath)
            raise _Reply(403, "binding_denied", "this token cannot use that binding")
        if not binding.path_allowed(upath):
            registry.refuse(att, kind="path_denied", status=403, binding=binding_name, path=upath)
            raise _Reply(403, "path_denied", "that path is not allowed for this binding")
        if method not in binding.methods:
            registry.refuse(att, kind="method_not_allowed", status=405, binding=binding_name, path=upath)
            raise _Reply(405, "method_not_allowed", "that method is not allowed for this binding", {"Allow": ", ".join(binding.methods)})

        betas = policy.beta_values((self.headers.get_all("anthropic-beta") or []) + (self.headers.get_all("openai-beta") or []))
        bad = policy.denied_betas(binding.betas, betas)
        if bad:
            registry.refuse(att, kind="feature_denied", status=403, binding=binding_name, path=upath)
            raise _Reply(403, "feature_denied", f"beta value not allowed on this binding: {', '.join(bad[:5])}")

        body = self._read_body(method, cfg.max_request_bytes, registry, att, binding_name, upath)
        self._body_unread = False
        try:
            plan = self._plan(binding, method, upath, body, lambda n: self._charge(core, att, binding_name, upath, n))
            body = None  # only the forwarded copy in the plan stays
        except _Reply as refusal:
            registry.refuse(att, kind=refusal.kind, status=refusal.status, binding=binding_name, path=upath)
            raise
        try:
            key = core.keys.get(binding_name)
        except KeyUnavailable as exc:
            core.log.error("binding %s: %s", binding_name, exc)
            registry.refuse(att, kind="key_unavailable", status=502, binding=binding_name, path=upath)
            raise _Reply(502, "key_unavailable", "the concierge cannot read the credential for this binding") from None

        req_id = secrets.token_hex(6)
        try:
            registry.begin(att, req=req_id, binding=binding_name, host_key=binding.host_key, host_limit=binding.max_concurrent,
                           reserve_usd=plan.reserve_usd, path=upath, model=plan.model)
        except Refusal as refusal:
            headers = {"Retry-After": "1"} if refusal.status == 429 else None
            raise _Reply(refusal.status, refusal.kind, refusal.message, headers) from None
        self._dispatch(core, att, binding, key, method, upath, query, plan, req_id)

    def _authenticate(self, registry: Registry) -> Attempt:
        candidates = []
        api_key = self.headers.get("x-api-key")
        if api_key:
            candidates.append(api_key.strip())
        auth = self.headers.get("authorization") or ""
        if auth[:7].lower() == "bearer ":
            candidates.append(auth[7:].strip())
        if not candidates:
            raise _Reply(401, "missing_token", "send the placeholder token in x-api-key or Authorization: Bearer")
        for candidate in candidates:
            if 0 < len(candidate) <= 256:
                att = registry.lookup(candidate)
                if att is not None and att.attempt == self.server.attempt:  # a token works on its own attempt's listener only
                    return att
        raise _Reply(401, "invalid_token", "unknown token")

    def _read_body(self, method, limit, registry, att, binding_name, upath) -> bytes:
        def refuse(status, kind, message):
            registry.refuse(att, kind=kind, status=status, binding=binding_name, path=upath)
            return _Reply(status, kind, message)

        if self.headers.get("transfer-encoding"):
            raise refuse(411, "length_required", "send a Content-Length; chunked request bodies are not supported")
        lengths = self.headers.get_all("content-length") or []
        if len(lengths) > 1:
            raise refuse(400, "bad_request", "duplicate Content-Length")
        if lengths and not LENGTH_RE.fullmatch(lengths[0].strip()):
            raise refuse(400, "bad_request", "bad Content-Length")
        if not lengths:
            if method == "POST":
                raise refuse(411, "length_required", "send a Content-Length")
            return b""
        length = int(lengths[0])
        if length > limit:
            raise refuse(413, "request_too_large", f"request body is limited to {limit} bytes")
        if method == "GET" and length:
            raise refuse(400, "bad_request", "a GET must not carry a body")
        held = length  # the raw bytes; _plan charges the rest once it knows how much the decoded text needs
        self._charge(self.server.core, att, binding_name, upath, held)
        if self._expects_continue:
            self.wfile.write(b"HTTP/1.1 100 Continue\r\n\r\n")
            self.wfile.flush()
        self._reader.set_deadline(BODY_GRACE_S + length / MIN_BODY_RATE)  # a slow sender is cut, however it paces itself
        body = self.rfile.read(length) if length else b""
        if len(body) != length:
            raise refuse(400, "bad_request", "the request body ended early")
        return body

    def _charge(self, core: Core, att: Attempt, binding_name: str, upath: str, n: int) -> None:
        """Charge ``n`` more bytes of body memory to the attempt. A request whose whole charge can never fit the attempt's share
        or the host total is refused with 413 (waiting would not help); otherwise a full budget is 429 busy (retry)."""
        if n <= 0:
            return
        owner = self._held_owner or att.attempt
        budget = core.body_budget
        if self._held_bytes + n > min(budget.per_owner, budget.limit):
            core.registry.refuse(att, kind="request_too_large", status=413, binding=binding_name, path=upath)
            raise _Reply(413, "request_too_large", "this request would need more memory than one request may hold "
                         f"({min(budget.per_owner, budget.limit)} bytes), so it can never be served; send less")
        if not budget.acquire(n, owner):
            core.registry.refuse(att, kind="busy", status=429, binding=binding_name, path=upath)
            raise _Reply(429, "busy", "too many request bodies are in memory right now; retry", {"Retry-After": "1"})
        self._held_bytes += n
        self._held_owner = owner

    def _plan(self, binding: BindingConfig, method: str, upath: str, body: bytes, charge=lambda n: None) -> Plan:
        metered = method == "POST" and not upath.endswith("/count_tokens")
        if not body:
            if metered:
                raise _Reply(400, "bad_request", "a metered request body must be a JSON object")
            return Plan(body, None, 0.0, 0)
        # On every path: a body is forwarded only as the strict JSON object that was checked, never as the guest sent it.
        try:
            # The raw bytes are charged already. Before decoding: the re-encoded copy and the three text copies (decoded text,
            # the strings of the parsed tree, the re-serialised text), at the string kind this body needs.
            charge((RAW_COPIES - 1) * len(body) + TEXT_COPIES * text_bytes_bound(body))
            text = decode_body(body)
            limit = self.server.core.cfg.concierge_max_json_nodes
            nodes = count_nodes(text, limit)
            charge(min(nodes, limit) * NODE_BYTES)  # the dicts, lists and string objects of the tree
            if nodes > limit:
                raise _Reply(413, "request_too_large", "the JSON body holds more keys and values than this binding accepts "
                             f"({self.server.core.cfg.concierge_max_json_nodes})")
            obj = parse_json(text)
            del text
        except DuplicateKey as exc:
            raise _Reply(400, "bad_request", f"the JSON body repeats the key {exc.args[0]!r}; refusing a body two parsers could read differently") from None
        except (ValueError, RecursionError):
            raise _Reply(400, "bad_request", "the request body must be a strict JSON object") from None
        if not isinstance(obj, dict):
            raise _Reply(400, "bad_request", "the request body must be a JSON object")
        # What is forwarded is what was parsed: the concierge and the upstream read the same values.
        try:
            body = canonical_json(obj)
        except (ValueError, RecursionError):
            raise _Reply(400, "bad_request", "the JSON body cannot be re-encoded safely") from None
        model = obj.get("model") if isinstance(obj.get("model"), str) else None
        is_responses = binding.format == "openai" and policy.is_responses_path(upath)
        found, images = policy.detect(obj, responses=is_responses)
        refused = policy.refused(found, binding.allow)
        if refused:
            message = f"this binding does not allow: {', '.join(refused)} (see the binding's allow list)"
            if is_responses and "server_state" in refused and obj.get("store") is not False:
                message += (': a Responses request, compaction (/responses/compact) included, must say "store": false, or the upstream keeps '
                            "it. This is permanent for this request: sending it again gets the same 403")
            raise _Reply(403, "feature_denied", message)
        if not metered:
            return Plan(body, model, 0.0, 0)
        caps = [obj[k] for k in ("max_tokens", "max_completion_tokens", "max_output_tokens") if k in obj]
        if any(isinstance(c, bool) or not isinstance(c, int) or not 1 <= c <= 10_000_000 for c in caps):
            raise _Reply(400, "bad_max_tokens", "max_tokens must be a positive integer")
        if caps:
            cap = max(caps)
        elif binding.format == "anthropic":
            raise _Reply(400, "max_tokens_required", "max_tokens is required so the worst-case cost can be reserved")
        else:
            cap = binding.default_max_output_tokens
        n = obj.get("n", 1)
        n = n if isinstance(n, int) and not isinstance(n, bool) and 1 <= n <= 128 else 1
        speed_mult = policy.FAST_MULTIPLIER if "fast_mode" in found else 1.0
        price, _ = pricing.lookup(model, binding.pricing, binding.default_price)
        # An upper-ish bound: real text runs 3-4 bytes per token. An image is billed by its pixels, not its bytes.
        pages = policy.pdf_pages(obj) if "pdf" in found else 0
        input_estimate = math.ceil(len(body) / 3) + images * policy.IMAGE_ALLOWANCE_TOKENS + pages * policy.PDF_TOKENS_PER_PAGE
        price = price.for_input(input_estimate).scaled(speed_mult)
        reserve = (input_estimate * price.input + cap * n * price.output) / pricing.MILLION
        if binding.format == "openai" and upath.endswith("/chat/completions") and obj.get("stream") is True:
            opts = obj.get("stream_options")
            opts = dict(opts) if isinstance(opts, dict) else {}
            if opts.get("include_usage") is not True:  # without it a streamed chat reply carries no usage to meter
                opts["include_usage"] = True
                obj["stream_options"] = opts
                body = canonical_json(obj)
        return Plan(body, model, reserve, cap * n, speed_mult)

    # -- upstream ----------------------------------------------------------------------------------------------

    def _dispatch(self, core: Core, att: Attempt, binding: BindingConfig, key: str, method: str, upath: str, query: str,
                  plan: Plan, req_id: str) -> None:
        flight = _Flight(started=time.monotonic())
        handle = AbortHandle()
        redactor = redact.StreamRedactor(key)

        def finalize() -> None:
            """Settle exactly once. Called before the last bytes go out, so a guest that has seen the whole reply can
            already see its cost in the budget."""
            if flight.settled:
                return
            flight.settled = True
            if not flight.fixed:
                usd, outcome, flight.price_unknown = settle_amount(
                    binding=binding, req_model=plan.model, got=flight.got, status=flight.status, reserved=plan.reserve_usd,
                    max_out_total=plan.max_out_total, speed_mult=plan.speed_mult)
                flight.usd = usd
                flight.outcome = flight.interrupted or outcome  # a stream that was cut is never recorded as ok
            self._settle(core, att, binding, req_id, plan, flight, redactor)

        try:
            self._relay(core, att, binding, key, method, upath, query, plan, handle, redactor, flight, finalize)
            finalize()
        except _Reply:  # refused before the guest got any bytes; the failure path already fixed the cost
            finalize()
            raise
        except BaseException:
            flight.usd, flight.outcome, flight.fixed = plan.reserve_usd, "error_charged", True
            finalize()
            raise
        finally:
            core.registry.drop_handle(att, handle)
            if handle.conn is not None:
                handle.conn.close()

    def _relay(self, core: Core, att: Attempt, binding: BindingConfig, key: str, method: str, upath: str, query: str, plan: Plan,
               handle: AbortHandle, redactor: redact.StreamRedactor, flight: _Flight, finalize) -> None:
        def fail(status: int, kind: str, message: str, usd: float, outcome: str):
            flight.usd, flight.outcome, flight.fixed = usd, outcome, True
            return _Reply(status, kind, message)

        if not core.registry.add_handle(att, handle):
            raise fail(401, "token_revoked", "this token was revoked", 0.0, "revoked")
        try:
            conn = self._connect(core, binding, handle)
        except _Reply as reply:  # nothing was sent upstream: free
            flight.usd, flight.outcome, flight.fixed = 0.0, "upstream_unreachable", True
            raise reply
        if handle.aborted:  # revoked while connecting: abort() found no socket to cut yet. Nothing has been sent.
            raise fail(401, "token_revoked", "this token was revoked", 0.0, "revoked")
        try:
            resp = self._request_upstream(core, conn, binding, key, method, upath, query, plan)
        except (OSError, http.client.HTTPException, ValueError) as exc:
            # The request may have reached the upstream and been billed: charge the reserved worst case.
            kind = "upstream_tls" if isinstance(exc, ssl.SSLError) else "upstream_error"
            raise fail(502, kind, f"the upstream request failed ({type(exc).__name__})", plan.reserve_usd,
                       "revoked" if handle.aborted else "upstream_lost") from None
        flight.status = resp.status
        encoding = (resp.getheader("content-encoding") or "identity").strip().lower()
        if encoding not in ("", "identity"):
            raise fail(502, "upstream_encoding", "the upstream sent an encoded response; refusing to relay what cannot be checked",
                       plan.reserve_usd, "upstream_encoding")
        sse = (resp.getheader("content-type") or "").lower().startswith("text/event-stream")
        parser = usage.SseUsage(binding.format) if sse else None
        buffered, truncated = bytearray(), False
        chunked = self.request_version != "HTTP/1.0" and flight.status >= 200 and flight.status not in (204, 205, 304)
        self._start_response(resp, flight.status, redactor, chunked)
        deadline = flight.started + REQUEST_DEADLINE_S
        ok = True
        while True:
            if handle.aborted:  # revoked: stop at once, whatever the socket still has buffered
                ok = False
                break
            try:
                chunk = resp.read1(65536)
            except (OSError, http.client.HTTPException):
                ok = False
                break
            if not chunk:
                break
            if parser is not None:
                parser.feed(chunk)
            elif not truncated:
                buffered += chunk
                if len(buffered) > MAX_PARSE_BYTES:
                    truncated = True
                    buffered.clear()
            flight.sent += len(chunk)
            if flight.sent > MAX_RESPONSE_BYTES or time.monotonic() > deadline or not self._write(redactor.feed(chunk), chunked):
                ok = False
                break
        if resp.length:  # a Content-Length body that ended early reads as a clean EOF to read1()
            ok = False
        if handle.aborted:
            ok = False  # a revocation shuts the socket, which reads as a clean end of stream
        if ok:
            ok = self._write(redactor.finish(), chunked)
        if parser is not None:
            flight.got = parser.usage if (parser.usage.final or parser.usage.saw_input) else None
        elif not truncated and buffered and flight.status < 300:
            flight.got = usage.from_json_body(binding.format, bytes(buffered))
        if not ok:
            flight.interrupted = "revoked" if handle.aborted else "cut"
            self.close_connection = True
        finalize()
        if ok and chunked:
            self._write_raw(b"0\r\n\r\n")

    def _settle(self, core, att, binding, req_id, plan, flight, redactor) -> None:
        got = flight.got
        core.registry.finish(
            att, req=req_id, host_key=binding.host_key, reserved_usd=plan.reserve_usd, actual_usd=flight.usd,
            input_tokens=got.total_input if got else 0, output_tokens=got.output_tokens if got else 0,
            cache_read=got.cache_read if got else 0, cache_write=(got.cache_write_5m + got.cache_write_1h) if got else 0,
            model=(got.model if got and got.model else plan.model), binding=binding.name, status=flight.status,
            outcome=flight.outcome, price_unknown=bool(flight.price_unknown), over_reservation=flight.usd > plan.reserve_usd + 1e-9,
            bytes=flight.sent, redactions=redactor.hits, ms=int((time.monotonic() - flight.started) * 1000))

    def _connect(self, core: Core, binding: BindingConfig, handle: AbortHandle) -> http.client.HTTPConnection:
        if binding.scheme == "https":
            conn = http.client.HTTPSConnection(binding.host, binding.port, timeout=CONNECT_TIMEOUT_S, context=core.tls_context)
        else:
            conn = http.client.HTTPConnection(binding.host, binding.port, timeout=CONNECT_TIMEOUT_S)
        handle.conn = conn
        try:
            conn.connect()
        except (OSError, http.client.HTTPException) as exc:
            conn.close()
            kind = "upstream_tls" if isinstance(exc, ssl.SSLError) else "upstream_error"
            raise _Reply(502, kind, f"cannot connect to the upstream ({type(exc).__name__})") from None
        conn.sock.settimeout(core.cfg.concierge_upstream_timeout_s)
        handle.sock = conn.sock
        return conn

    def _request_upstream(self, core: Core, conn, binding: BindingConfig, key: str, method: str, upath: str, query: str, plan: Plan):
        target = upath + ("?" + query if query else "")
        conn.putrequest(method, target, skip_accept_encoding=True)  # Host comes from the configured origin, never the guest
        connection_tokens = {t.strip().lower() for t in (self.headers.get("connection") or "").split(",") if t.strip()}
        for name, value in self.headers.items():
            low = name.lower()
            if drop_request_header(name) or low in connection_tokens:
                continue
            conn.putheader(name, value)
        conn.putheader("Accept-Encoding", "identity")
        if binding.auth == "x-api-key":
            conn.putheader("x-api-key", key)
        else:
            conn.putheader("Authorization", f"Bearer {key}")
        if method == "POST":
            conn.putheader("Content-Length", str(len(plan.body)))
        conn.endheaders(plan.body if method == "POST" else None)
        self._release_body(core, plan)  # the body has gone upstream: it no longer needs memory while the reply is awaited
        return conn.getresponse()  # never follows redirects: http.client has no such feature

    def _start_response(self, resp, status: int, redactor: redact.StreamRedactor, chunked: bool) -> None:
        self._responded = True
        self.send_response_only(status, resp.reason or None)
        connection_tokens = {t.strip().lower() for t in (resp.getheader("connection") or "").split(",") if t.strip()}
        for name, value in resp.getheaders():
            low = name.lower()
            if low in DROP_RESPONSE or low in connection_tokens:
                continue
            self.send_header(name, redactor.scrub_text(value).replace("\r", " ").replace("\n", " "))
        if chunked:
            self.send_header("Transfer-Encoding", "chunked")
        self.send_header("Connection", "close")
        self.end_headers()
        self.wfile.flush()

    def _write(self, data: bytes, chunked: bool) -> bool:
        if not data:
            return True
        return self._write_raw((b"%x\r\n" % len(data)) + data + b"\r\n" if chunked else data)

    def _write_raw(self, data: bytes) -> bool:
        try:
            self.wfile.write(data)
            self.wfile.flush()
        except (OSError, ssl.SSLError):
            return False
        return True
