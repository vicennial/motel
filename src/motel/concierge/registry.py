"""Placeholder tokens, budgets and concurrency: the concierge's in-memory state, rebuilt from the ledger.

Restart semantics (chosen, not accidental): a restart does **not** revoke anything. The ledger is replayed, so every
issued token keeps its expiry, revocation and spend. A request that was in flight when the process died is settled at its
full reserved (worst-case) cost, because nobody can know what the upstream billed. The only thing a restart cannot
restore is the token itself: only its SHA-256 is stored, so a token is shown once, at ``issue``.

Budget semantics: before dispatch a worst-case cost is *reserved* (``begin``); when the response ends it is *settled* to
the cost computed from the reported usage (``finish``). Admission requires ``spent + reserved + new reservation <=
budget``. Reservations make parallel requests safe; they do not make the budget a hard cap (see ``docs/concierge.md``).
"""

from __future__ import annotations

import hashlib
import hmac
import math
import re
import secrets
import threading
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from motel.concierge import ledger as ledger_mod
from motel.concierge.errors import AdminRefused, AttemptNotFound

ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
TOKEN_PREFIX = "mt_"
MAX_TOKEN_TTL_S = 7 * 24 * 3600
MAX_ATTEMPT_CONCURRENCY = 16
CONNECTION_MARGIN = 2  # open connections an attempt may hold beyond its request concurrency (bodies still arriving)
RETAIN_S = 24 * 3600  # how long an expired or revoked attempt stays queryable
_DUMMY = hashlib.sha256(b"motel-concierge-miss").hexdigest()


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8", "replace")).hexdigest()


def parse_expiry(text) -> float:
    if not isinstance(text, str) or not text:
        raise AdminRefused("expires_at must be an ISO-8601 timestamp such as 2026-01-01T12:00:00Z", "invalid")
    try:
        when = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        raise AdminRefused(f"bad expires_at {text!r}: use an ISO-8601 timestamp such as 2026-01-01T12:00:00Z", "invalid") from None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return when.timestamp()


def validate_issue(*, tour, guest, attempt, bindings, expires_at, budget_usd, max_concurrent, known_bindings, max_budget_usd,
                   now: float) -> float:
    """Check the arguments of ``issue`` (shared with the in-memory fake); return the expiry as an epoch."""
    for label, value in (("tour", tour), ("guest", guest), ("attempt", attempt)):
        if not isinstance(value, str) or not ID_RE.fullmatch(value):
            raise AdminRefused(f"bad {label} id {value!r}: letters, digits, '.', '_' and '-', at most 64 characters", "invalid")
    if not isinstance(bindings, list | tuple) or not bindings or len(set(bindings)) != len(bindings):
        raise AdminRefused("bindings must be a non-empty list of distinct binding names", "invalid")
    for name in bindings:
        if name not in known_bindings:
            raise AdminRefused(f"unknown binding {name!r}; configured: {', '.join(sorted(known_bindings)) or 'none'}", "invalid")
    ok = isinstance(budget_usd, int | float) and not isinstance(budget_usd, bool) and math.isfinite(budget_usd) and budget_usd > 0
    if not ok:
        raise AdminRefused("budget_usd must be a positive number", "invalid")
    if max_budget_usd is not None and budget_usd > max_budget_usd + 1e-9:
        raise AdminRefused(f"budget_usd {budget_usd} is above the operator ceiling max_usd_model_per_guest {max_budget_usd}", "invalid")
    if (not isinstance(max_concurrent, int) or isinstance(max_concurrent, bool)
            or not 1 <= max_concurrent <= MAX_ATTEMPT_CONCURRENCY):
        raise AdminRefused(f"max_concurrent must be an integer from 1 to {MAX_ATTEMPT_CONCURRENCY}", "invalid")
    expires = parse_expiry(expires_at)
    if expires <= now:
        raise AdminRefused("expires_at is in the past", "invalid")
    if expires > now + MAX_TOKEN_TTL_S:
        raise AdminRefused(f"expires_at is more than {MAX_TOKEN_TTL_S // 3600} hours away", "invalid")
    return expires


@dataclass
class Attempt:
    attempt: str
    tour: str
    guest: str
    token_sha256: str
    bindings: tuple
    issued_at: float
    expires_at: float
    budget_usd: float
    max_concurrent: int
    spent_usd: float = 0.0
    reserved_usd: float = 0.0
    inflight: int = 0
    conns: int = 0  # open guest connections after authentication (admitted or still reading their body)
    requests: int = 0
    refused: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    revoked_at: float | None = None
    handles: set = field(default_factory=set)  # live upstream connections; closed when the attempt is revoked
    port: int = 0  # the loopback port of this attempt's own listener (0: none)

    def snapshot(self) -> dict:
        return {"attempt": self.attempt, "tour": self.tour, "guest": self.guest, "token_sha256": self.token_sha256,
                "bindings": list(self.bindings), "issued_at": self.issued_at, "expires_at": self.expires_at,
                "budget_usd": self.budget_usd, "max_concurrent": self.max_concurrent, "spent_usd": self.spent_usd,
                "requests": self.requests, "refused": self.refused, "input_tokens": self.input_tokens,
                "output_tokens": self.output_tokens, "revoked_at": self.revoked_at, "port": self.port}


class Refusal(Exception):
    """A request the registry will not admit: ``kind`` becomes ``motel_<kind>`` in the JSON error."""

    def __init__(self, kind: str, message: str, status: int):
        super().__init__(message)
        self.kind, self.message, self.status = kind, message, status


class Registry:
    def __init__(self, ledger: ledger_mod.Ledger, *, known_bindings, max_budget_usd: float | None, clock=time.time):
        self.ledger = ledger
        self.known_bindings = frozenset(known_bindings)
        self.max_budget_usd = max_budget_usd
        self.clock = clock
        self._lock = threading.Lock()
        self._by_attempt: dict[str, Attempt] = {}
        self._by_hash: dict[str, Attempt] = {}
        self._pending: set[str] = set()  # attempts whose issue is in progress (a socket reserved, maybe stored)
        self._host_inflight: dict[str, int] = {}
        self.unknown_token_refusals = 0
        self.endpoints = None  # per-attempt listeners (motel.concierge.endpoints.Endpoints), wired by the service
        self.started = clock()
        self._replay()

    # -- restart -----------------------------------------------------------------------------------------------

    def _replay(self) -> None:
        pending: dict[tuple, float] = {}
        for ev in ledger_mod.read_events(self.ledger.path):
            kind = ev.get("ev")
            att = self._by_attempt.get(ev.get("attempt"))
            try:
                if kind in ("issue", "snapshot"):
                    new = Attempt(ev["attempt"], ev["tour"], ev["guest"], ev["token_sha256"], tuple(ev["bindings"]),
                                  float(ev.get("issued_at", ev.get("expires_at", 0))), float(ev["expires_at"]), float(ev["budget_usd"]),
                                  int(ev["max_concurrent"]), port=int(ev.get("port", 0)))
                    if kind == "snapshot":
                        for name in ("spent_usd", "requests", "refused", "input_tokens", "output_tokens", "revoked_at"):
                            setattr(new, name, ev.get(name, getattr(new, name)))
                    self._by_attempt[new.attempt] = new
                    self._by_hash[new.token_sha256] = new
                elif kind == "port" and att is not None:
                    att.port = int(ev["port"])
                elif kind == "revoke":
                    self._apply_revoke(ev.get("attempt"), ev.get("tour"), float(ev.get("at", 0)))
                elif kind == "reserve" and att is not None:
                    pending[(att.attempt, ev["req"])] = float(ev["usd"])
                    att.requests += 1
                elif kind == "settle" and att is not None:
                    pending.pop((att.attempt, ev.get("req")), None)
                    att.spent_usd += float(ev.get("usd", 0))
                    att.input_tokens += int(ev.get("input_tokens", 0))
                    att.output_tokens += int(ev.get("output_tokens", 0))
                elif kind == "refuse" and att is not None:
                    att.refused += 1
            except (KeyError, TypeError, ValueError):
                continue  # a malformed event is skipped, never fatal
        for (attempt, req), usd in pending.items():  # died mid-request: charge the worst case
            att = self._by_attempt.get(attempt)
            if att is not None:
                att.spent_usd += usd
                self.ledger.append("settle", attempt=attempt, req=req, usd=usd, reserved=usd, input_tokens=0, output_tokens=0,
                                   outcome="restart", price_unknown=False)
        now = self.clock()
        for att in list(self._by_attempt.values()):
            if self._finished_before(att, now - RETAIN_S):
                del self._by_attempt[att.attempt]
                self._by_hash.pop(att.token_sha256, None)
        if self.ledger.size() > ledger_mod.COMPACT_BYTES:
            self.ledger.rotate([a.snapshot() for a in self._by_attempt.values()])
        self.ledger.append("start", durable=True, attempts=len(self._by_attempt))

    @staticmethod
    def _finished_before(att: Attempt, cutoff: float) -> bool:
        ended = min(att.expires_at, att.revoked_at) if att.revoked_at is not None else att.expires_at
        return ended < cutoff and att.inflight == 0

    # -- admin -------------------------------------------------------------------------------------------------

    def issue(self, *, tour, guest, attempt, bindings, expires_at, budget_usd, max_concurrent=2) -> str:
        now = self.clock()
        expires = validate_issue(tour=tour, guest=guest, attempt=attempt, bindings=bindings, expires_at=expires_at,
                                 budget_usd=budget_usd, max_concurrent=max_concurrent, known_bindings=self.known_bindings,
                                 max_budget_usd=self.max_budget_usd, now=now)
        token = TOKEN_PREFIX + secrets.token_urlsafe(32)
        digest = hash_token(token)
        with self._lock:  # a duplicate is 'exists' even when every port is taken, so look before reserving a port
            if attempt in self._by_attempt or attempt in self._pending:
                raise AdminRefused(f"attempt {attempt} already has a token (or is being issued); an attempt is issued once", "exists")
            self._pending.add(attempt)  # claimed before anything is reserved or stored: a second caller never touches our store entry
        try:
            sock = self.endpoints.reserve() if self.endpoints is not None else None  # may raise AdminRefused: no free port
            port = sock.getsockname()[1] if sock is not None else 0
            if sock is not None:
                self.endpoints.preserve(attempt, sock)  # closes the socket and raises AdminRefused(busy) if it cannot be kept safe
            try:
                with self._lock:
                    att = Attempt(attempt, tour, guest, digest, tuple(bindings), now, expires, float(budget_usd), max_concurrent, port=port)
                    self.ledger.append("issue", durable=True, token_sha256=digest, tour=tour, guest=guest, attempt=attempt,
                                       bindings=list(bindings), issued_at=now, expires_at=expires, budget_usd=float(budget_usd),
                                       max_concurrent=max_concurrent, port=port)
                    self._by_attempt[attempt] = att
                    self._by_hash[digest] = att
            except BaseException:
                if sock is not None:
                    sock.close()
                    self.endpoints.unstore(attempt)
                raise
            if sock is not None:
                self.endpoints.start(att, sock)  # the listener is up before the caller sees the token
        finally:
            with self._lock:
                self._pending.discard(attempt)
        self._prune(now)
        return token

    def get(self, attempt: str) -> Attempt:
        with self._lock:
            att = self._by_attempt.get(attempt)
        if att is None:
            raise AttemptNotFound(f"no attempt {attempt!r} (unknown, or older than {RETAIN_S // 3600} hours)")
        return att

    def live(self, att: Attempt) -> bool:
        return att.revoked_at is None and self.clock() < att.expires_at

    def live_attempts(self) -> list:
        with self._lock:
            return [a for a in self._by_attempt.values() if self.live(a)]

    def set_port(self, attempt: str, port: int) -> None:
        """Record that an attempt's listener moved (it could not be re-opened on its old port after a restart)."""
        with self._lock:
            att = self._by_attempt.get(attempt)
            if att is not None and att.port != port:
                att.port = port
                self.ledger.append("port", durable=True, attempt=attempt, port=port)

    def revoke(self, *, attempt: str | None = None, tour: str | None = None) -> int:
        if not attempt and not tour:
            raise AdminRefused("revoke needs an attempt or a tour", "invalid")
        for label, value in (("attempt", attempt), ("tour", tour)):
            if value is not None and not (isinstance(value, str) and ID_RE.fullmatch(value)):
                raise AdminRefused(f"bad {label} id {value!r}", "invalid")
        now = self.clock()
        with self._lock:
            self.ledger.append("revoke", durable=True, attempt=attempt, tour=tour, at=now)
            hit = self._apply_revoke(attempt, tour, now)
            handles = [h for att in hit for h in att.handles]
        for handle in handles:  # stop spending now: cut the live upstream connections
            try:
                handle.abort()
            except Exception:  # noqa: BLE001
                pass
        if self.endpoints is not None:
            for att in hit:  # and stop listening for the attempt
                self.endpoints.close(att.attempt)
        return len(hit)

    def _apply_revoke(self, attempt, tour, at: float) -> list:
        hit = []
        for att in self._by_attempt.values():
            if attempt and att.attempt != attempt or tour and att.tour != tour:
                continue
            if att.revoked_at is None:
                att.revoked_at = at
                hit.append(att)
        return hit

    def usage(self, attempt: str) -> dict:
        with self._lock:
            att = self._by_attempt.get(attempt)
            if att is None:
                raise AttemptNotFound(f"no attempt {attempt!r} (unknown, or older than {RETAIN_S // 3600} hours)")
            return {"usd": round(att.spent_usd + att.reserved_usd, 6), "input_tokens": att.input_tokens,
                    "output_tokens": att.output_tokens, "requests": att.requests, "refused": att.refused}

    def health(self) -> dict:
        now = self.clock()
        with self._lock:
            live = [a for a in self._by_attempt.values() if a.revoked_at is None and a.expires_at > now]
            return {"status": "ok", "uptime_s": int(now - self.started), "attempts": len(self._by_attempt), "live_tokens": len(live),
                    "inflight": sum(a.inflight for a in self._by_attempt.values()),
                    "unknown_token_refusals": self.unknown_token_refusals}

    def _prune(self, now: float) -> None:
        with self._lock:
            for att in list(self._by_attempt.values()):
                if self._finished_before(att, now - RETAIN_S):
                    del self._by_attempt[att.attempt]
                    self._by_hash.pop(att.token_sha256, None)

    # -- guest path ----------------------------------------------------------------------------------------------

    def lookup(self, token: str) -> Attempt | None:
        """Find the attempt for a presented token. The lookup key is the token's SHA-256, so the time taken depends only
        on a digest the caller cannot steer towards a stored one; the final comparison is constant-time anyway."""
        digest = hash_token(token)
        att = self._by_hash.get(digest)
        if att is not None and hmac.compare_digest(att.token_sha256, digest):
            return att
        hmac.compare_digest(_DUMMY, digest)
        self.unknown_token_refusals += 1
        return None

    def state_refusal(self, att: Attempt) -> Refusal | None:
        if att.revoked_at is not None:
            return Refusal("token_revoked", "this token was revoked", 401)
        if self.clock() >= att.expires_at:
            return Refusal("token_expired", "this token has expired", 401)
        return None

    def begin(self, att: Attempt, *, req: str, binding: str, host_key: str, host_limit: int, reserve_usd: float, path: str,
              model: str | None) -> None:
        """Admit a request or raise :class:`Refusal`. On success the reservation is written to the ledger and held."""
        with self._lock:
            problem = self.state_refusal(att)
            if problem is None and att.inflight >= att.max_concurrent:
                problem = Refusal("concurrency", f"this attempt already has {att.inflight} requests in flight "
                                  f"(limit {att.max_concurrent})", 429)
            if problem is None and self._host_inflight.get(host_key, 0) >= host_limit:
                problem = Refusal("host_busy", f"{host_key} already has {host_limit} requests in flight", 429)
            if problem is None:
                remaining = att.budget_usd - att.spent_usd - att.reserved_usd
                if reserve_usd > remaining + 1e-9:
                    problem = Refusal("budget_exhausted",
                                      f"worst case for this request is ${reserve_usd:.4f} but ${max(remaining, 0):.4f} of the "
                                      f"${att.budget_usd:.2f} budget is left; lower max_tokens or shorten the prompt", 403)
            if problem is not None:
                att.refused += 1
                self.ledger.append("refuse", attempt=att.attempt, binding=binding, path=path, reason=problem.kind, status=problem.status)
                raise problem
            self.ledger.append("reserve", durable=True, attempt=att.attempt, req=req, binding=binding, path=path, model=model,
                               usd=round(reserve_usd, 8))
            att.reserved_usd += reserve_usd
            att.inflight += 1
            att.requests += 1
            self._host_inflight[host_key] = self._host_inflight.get(host_key, 0) + 1

    def open_connection(self, att: Attempt, margin: int = CONNECTION_MARGIN) -> bool:
        """Count an authenticated connection; False when the attempt already holds ``max_concurrent + margin`` of them."""
        with self._lock:
            if att.conns >= att.max_concurrent + margin:
                return False
            att.conns += 1
            return True

    def close_connection(self, att: Attempt) -> None:
        with self._lock:
            att.conns = max(0, att.conns - 1)

    def refuse(self, att: Attempt, *, kind: str, status: int, binding: str | None = None, path: str | None = None) -> None:
        with self._lock:
            att.refused += 1
            self.ledger.append("refuse", attempt=att.attempt, binding=binding, path=path, reason=kind, status=status)

    def finish(self, att: Attempt, *, req: str, host_key: str, reserved_usd: float, actual_usd: float, **detail) -> None:
        with self._lock:
            att.reserved_usd = max(0.0, att.reserved_usd - reserved_usd)
            att.spent_usd += actual_usd
            att.inflight = max(0, att.inflight - 1)
            att.input_tokens += int(detail.get("input_tokens", 0))
            att.output_tokens += int(detail.get("output_tokens", 0))
            self._host_inflight[host_key] = max(0, self._host_inflight.get(host_key, 1) - 1)
            self.ledger.append("settle", attempt=att.attempt, req=req, usd=round(actual_usd, 8), reserved=round(reserved_usd, 8), **detail)

    def add_handle(self, att: Attempt, handle) -> bool:
        """Track a live upstream connection; False when the attempt was revoked in the meantime (do not proceed)."""
        with self._lock:
            if att.revoked_at is not None:
                return False
            att.handles.add(handle)
            return True

    def drop_handle(self, att: Attempt, handle) -> None:
        with self._lock:
            att.handles.discard(handle)
