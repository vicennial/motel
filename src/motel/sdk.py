"""motel Python SDK: a thin, typed client over the ``motel`` CLI and its ``--json`` contract.

Standard library only. The SDK has no privilege of its own and never imports the front desk: every call
runs ``motel ... --json`` as a subprocess (an argv list, never a shell). The CLI does the real work and
talks to the root-owned front desk through sudo. See ``docs/sdk.md`` and ``docs/cli-json.md``.

    from motel import Motel

    motel = Motel()
    with motel.room(50, ttl="6h", task="video-render") as room:
        print(room.path)                  # scratch directory; checked out when the block ends

Failures raise a subclass of :class:`MotelError` chosen by the CLI exit code: :class:`NoVacancy` (3),
:class:`Busy` (4), :class:`NotFound` (7) and so on. Each carries ``code`` and ``hint``.
"""

from __future__ import annotations

import contextlib
import json
import re
import subprocess
import time
import warnings
from collections.abc import AsyncIterator, Iterator, Mapping, Sequence
from dataclasses import dataclass, field
from decimal import Decimal, InvalidOperation
from typing import Any

__all__ = [
    "AsyncMotel",
    "BadReply",
    "Busy",
    "Cabin",
    "CabinRun",
    "Check",
    "Doctor",
    "GuardRefused",
    "InventoryIncomplete",
    "Motel",
    "MotelError",
    "NoVacancy",
    "NotFound",
    "NotInstalled",
    "NotSetUp",
    "Overview",
    "ProviderError",
    "Room",
    "Timeout",
    "Tour",
    "TourGuest",
    "TourResult",
    "UsageError",
]

DEFAULT_TIMEOUT = 180.0
DEFAULT_CABIN_TIMEOUT = 3600.0

SIZE_RE = re.compile(r"[1-9][0-9]{0,5}[GgTt]?[Bb]?")
DURATION_RE = re.compile(r"[1-9][0-9]{0,3}[mhd]")
ID_RE = re.compile(r"[a-z0-9][a-z0-9-]{2,79}")
TASK_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
TYPE_RE = re.compile(r"[a-z0-9][a-z0-9-]{0,39}")
TOUR_ID_RE = re.compile(r"t-[0-9a-f]{10}")
GUEST_ID_RE = re.compile(r"[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?")
TOUR_DONE = ("done", "failed", "timed_out", "over_budget", "cancelled")

# --- errors --------------------------------------------------------------------------------------------


class MotelError(Exception):
    """Base class for every SDK failure.

    ``code`` is the CLI exit code (5 for client-side failures such as a timeout), ``hint`` the next command
    the CLI suggested, ``payload`` the parsed error JSON when there was one.
    """

    code = 5
    default_hint = "motel doctor"

    def __init__(self, message: str, hint: str | None = None, *, code: int | None = None, payload: Mapping | None = None):
        super().__init__(message)
        self.message = message
        self.hint = hint or self.default_hint
        if code is not None:
            self.code = code
        self.payload = dict(payload or {})

    def __str__(self) -> str:
        return f"{self.message}. Try: {self.hint}"


class UsageError(MotelError):
    """Exit 2: bad arguments. Fix the call."""

    code = 2
    default_hint = "motel --help"


class NoVacancy(MotelError):
    """Exit 3: a cap or policy refused the request. Do not retry in a loop; free a room or ask."""

    code = 3
    default_hint = "motel ls --mine"


class Busy(MotelError):
    """Exit 4: a process still has files open in the room. Leave the directory and retry later."""

    code = 4
    default_hint = "motel ls --mine"


class ProviderError(MotelError):
    """Exit 5: the cloud provider or a system command failed. Run ``doctor``; retry once at most."""

    code = 5


class GuardRefused(MotelError):
    """Exit 6: a safety guard refused to act. Stop and report."""

    code = 6


class NotFound(MotelError):
    """Exit 7: no such room or cabin."""

    code = 7
    default_hint = "motel ls --mine"


class NotSetUp(MotelError):
    """Exit 8: the front desk is not set up. Ask the operator to run ``motel-desk setup``."""

    code = 8
    default_hint = "sudo motel-desk setup hetzner|local"


class InventoryIncomplete(MotelError):
    """Exit 9: provider inventory or cabin cleanup is incomplete. Retry later; report it if it persists."""

    code = 9


class NotInstalled(NotSetUp):
    """The ``motel`` executable was not found or cannot be run (client side; code 8)."""


class Timeout(ProviderError):
    """The CLI did not finish in time (client side; code 5). The operation may or may not have happened."""

    default_hint = "motel ls --mine"


class BadReply(ProviderError):
    """The CLI exited 0 but its stdout was not the expected JSON (client side; code 5)."""


_BY_CODE: dict[int, type[MotelError]] = {
    2: UsageError,
    3: NoVacancy,
    4: Busy,
    5: ProviderError,
    6: GuardRefused,
    7: NotFound,
    8: NotSetUp,
    9: InventoryIncomplete,
}

# --- data ----------------------------------------------------------------------------------------------


@dataclass
class Room:
    """One room, as printed by ``motel checkin|status|extend|checkout|restore --json``.

    ``path`` is the directory to use while ``state`` is ``active`` or ``grace``. ``raw`` holds the whole JSON
    object, so keys added by newer versions of motel stay reachable.
    """

    id: str
    task: str | None = None
    owner: str | None = None
    uid: int | None = None
    size_gb: int | None = None
    suite: bool = False
    floor: str | None = None
    state: str = "active"
    created: str | None = None
    expires: str | None = None
    max_expires: str | None = None
    time_left_h: float | None = None
    warn: bool = False
    path: str | None = None
    grace_ends: str | None = None
    purge_at: str | None = None
    lost_and_found_reason: str | None = None
    floor_free_gb: float | None = None
    floor_used_pct: float | None = None
    raw: dict = field(default_factory=dict, repr=False, compare=False)
    _client: Motel | None = field(default=None, repr=False, compare=False)

    @classmethod
    def from_json(cls, data: Mapping, client: Motel | None = None) -> Room:
        known = {f: data.get(f) for f in _ROOM_FIELDS if f in data}
        known["id"] = str(data.get("id", ""))
        known["suite"] = bool(data.get("suite", False))
        known["warn"] = bool(data.get("warn", False))
        return cls(**known, raw=dict(data), _client=client)

    # Convenience calls on the client that produced this room. ``extend`` and ``refresh`` update it in place.
    def extend(self, ttl: str) -> Room:
        return self._update(self._need().extend(self.id, ttl))

    def refresh(self) -> Room:
        return self._update(self._need().status(self.id))

    def checkout(self, now: bool = False) -> Room:
        return self._update(self._need().checkout(self.id, now=now))

    def _need(self) -> Motel:
        if self._client is None:
            raise UsageError("this Room is not attached to a Motel client", "Motel().status(room_id)")
        return self._client

    def _update(self, other: Room) -> Room:
        for name in _ROOM_FIELDS + ("raw",):
            setattr(self, name, getattr(other, name))
        return self


_ROOM_FIELDS = (
    "id", "task", "owner", "uid", "size_gb", "suite", "floor", "state", "created", "expires", "max_expires",
    "time_left_h", "warn", "path", "grace_ends", "purge_at", "lost_and_found_reason", "floor_free_gb", "floor_used_pct",
)


@dataclass
class Overview:
    """``motel ls --json``: rooms plus the host's floors and capacity (headroom before asking)."""

    rooms: list[Room]
    floors: list[dict]
    capacity: dict


@dataclass
class Check:
    check: str
    level: str
    ok: bool
    detail: str = ""


@dataclass
class Doctor:
    """``motel doctor --json``. ``ok`` is false when any check has level ``fail`` (the CLI exits 5)."""

    ok: bool
    checks: list[Check]

    @property
    def failures(self) -> list[Check]:
        return [c for c in self.checks if c.level == "fail"]


@dataclass
class Cabin:
    """One cabin (disposable server). ``id`` is the lease id. Deletion at checkout is final.

    ``cost_estimate_eur`` and ``hourly_eur`` are net estimates; the provider invoice is authoritative.
    """

    id: str
    name: str | None = None
    state: str = "active"
    task: str | None = None
    owner: str | None = None
    created: str | None = None
    expires: str | None = None
    time_left_s: int | None = None
    address: str | None = None
    server_type: str | None = None
    location: str | None = None
    hourly_eur: Decimal | None = None
    cost_estimate_eur: Decimal | None = None
    disk_gb: int | None = None
    warnings: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False, compare=False)
    _client: Motel | None = field(default=None, repr=False, compare=False)

    @property
    def released(self) -> bool:
        return self.state == "released"

    @classmethod
    def from_json(cls, data: Mapping, client: Motel | None = None) -> Cabin:
        offer = data.get("offer") or {}
        disk = data.get("disk") or {}
        return cls(
            id=str(data.get("lease_id", "")), name=data.get("name"), state=data.get("state") or "active",
            task=data.get("task"), owner=data.get("owner"), created=data.get("created_at"), expires=data.get("expires_at"),
            time_left_s=data.get("time_left_s"), address=data.get("address"),
            server_type=offer.get("server_type"), location=offer.get("location"),
            hourly_eur=_decimal(offer.get("total_hourly_net_eur")), cost_estimate_eur=_decimal(data.get("cost_estimate_net_eur")),
            disk_gb=disk.get("size_gb"), warnings=list(data.get("warnings") or []), raw=dict(data), _client=client)

    def run(self, command: Sequence[str] | str, *, check: bool = False, timeout: float | None = None) -> CabinRun:
        """Run a command in this cabin over SSH (``motel cabin ssh ID -- CMD``). No files are synced."""
        return self._need().cabin_ssh(self.id, command, check=check, timeout=timeout)

    def extend(self, ttl: str) -> Cabin:
        return self._update(self._need().cabin_extend(self.id, ttl))

    def refresh(self) -> Cabin:
        return self._update(self._need().cabin_status(self.id))

    def checkout(self) -> Cabin:
        return self._update(self._need().cabin_checkout(self.id))

    def _need(self) -> Motel:
        if self._client is None:
            raise UsageError("this Cabin is not attached to a Motel client", "Motel().cabin_status(cabin_id)")
        return self._client

    def _update(self, other: Cabin) -> Cabin:
        for name in ("id", "name", "state", "task", "owner", "created", "expires", "time_left_s", "address", "server_type",
                     "location", "hourly_eur", "cost_estimate_eur", "disk_gb", "warnings", "raw"):
            setattr(self, name, getattr(other, name))
        return self


@dataclass
class CabinRun:
    """The result of a command that ran in a cabin. ``returncode`` is the command's own exit status, except when the
    checkout failed outright (busy past the wait, error, guard): then it is the checkout's nonzero status and the cabin
    may still be active and billing (``released`` is ``False``).

    A nonzero status is not an exception unless ``check=True``: it can be the command's failure, not motel's.
    ``cabin_id``, ``cost_estimate_eur``, ``hourly_eur``, ``released`` and ``kept`` come from the summary line that
    ``motel cabin run --json`` prints last on stdout (``{schema, ok, cabin, exit_code, kept, released, hourly_net_eur,
    cost_estimate_net_eur}``); the SDK removes that line from ``stdout``. With an older motel that prints no summary,
    they fall back to the messages on stderr (best effort) and may be ``None``.

    ``released`` is ``True`` when the cabin was checked out and deleted, ``False`` when the checkout did not finish
    (the command's own ``returncode`` is still returned, but the cabin keeps billing until the reaper finishes it, or
    until you run ``motel cabin checkout``), and ``None`` when it is unknown (``keep=True``, or ``cabin_ssh``).
    ``check=True`` does not raise for a pending cleanup that follows a command that exited 0: check ``released``.
    """

    returncode: int
    stdout: str = ""
    stderr: str = ""
    cabin_id: str | None = None
    cost_estimate_eur: Decimal | None = None
    hourly_eur: Decimal | None = None
    released: bool | None = None
    kept: bool | None = None


# --- helpers -------------------------------------------------------------------------------------------


def _decimal(value: Any) -> Decimal | None:
    try:
        return None if value is None else Decimal(str(value))
    except InvalidOperation:
        return None


def _size(value: int | str) -> str:
    text = str(value)
    if isinstance(value, bool) or not SIZE_RE.fullmatch(text):
        raise UsageError(f"invalid size {value!r}: use a number of GB, or 200G or 1T", "motel checkin 50G --for 6h")
    return text if text[-1] in "GgTtBb" else text + "G"


def _duration(value: str, what: str = "ttl") -> str:
    if not isinstance(value, str) or not DURATION_RE.fullmatch(value):
        raise UsageError(f"invalid {what} {value!r}: use minutes, hours or days, such as 90m, 6h or 3d", "motel checkin 50G --for 6h")
    return value


def _ident(value: str, what: str) -> str:
    if not isinstance(value, str) or not ID_RE.fullmatch(value):
        raise UsageError(f"invalid {what} id {value!r}: 3-80 characters of a-z, 0-9 and '-'", "motel ls --mine")
    return value


def _pattern(value: str, regex: re.Pattern, what: str, example: str) -> str:
    if not isinstance(value, str) or not regex.fullmatch(value):
        raise UsageError(f"invalid {what} {value!r}: use lowercase a-z, 0-9 and '-' (for example {example})", "motel --help")
    return value


def _command(command: Sequence[str] | str) -> list[str]:
    """A command as an argv list. A plain string runs under ``sh -c`` on the cabin."""
    if isinstance(command, str):
        argv = ["sh", "-c", command]
    elif isinstance(command, Sequence) and not isinstance(command, bytes):
        argv = list(command)
    else:
        argv = []
    if not argv or not all(isinstance(a, str) and a and "\x00" not in a for a in argv):
        raise UsageError("invalid command: give a non-empty argv list of non-empty strings, or one shell string",
                         "motel cabin run -- make test")
    return argv


def _doc(stdout: str) -> dict | None:
    try:
        doc = json.loads(stdout)
    except ValueError:
        return None
    return doc if isinstance(doc, dict) else None


def _stderr_hint(stderr: str) -> tuple[str, str | None]:
    """``motel: message. Try: next command`` -> (message, hint)."""
    line = next((ln for ln in reversed(stderr.strip().splitlines()) if ln.strip()), "")
    line = line.removeprefix("motel:").strip()
    message, _, hint = line.partition(". Try: ")
    return (message.strip() or "the motel command failed"), (hint.strip() or None)


def _error(code: int, doc: Mapping | None, stderr: str) -> MotelError:
    if doc and doc.get("error"):
        message, hint = str(doc["error"]).rstrip(". "), doc.get("hint")
    else:
        message, hint = _stderr_hint(stderr)
    cls = _BY_CODE.get(code, MotelError)
    return cls(message, hint, code=code, payload=doc)


_CABIN_STARTED = re.compile(r"cabin (\S+) ·")
_CABIN_RELEASED = re.compile(r"released (\S+) · est\. cost EUR (\S+) net")
_CABIN_NOT_RELEASED = re.compile(r"checkout of (\S+) (?:is NOT complete|FAILED)")


def _split_run_summary(stdout: str) -> tuple:
    """(stdout without the summary line, summary dict or None). ``cabin run --json`` prints the summary last, on a line of
    its own after one blank line."""
    body = stdout.rstrip("\n")
    start = body.rfind("\n") + 1
    summary = _doc(body[start:])
    if summary is None or summary.get("schema") != "motel/v1" or "exit_code" not in summary or "cabin" not in summary:
        return stdout, None
    head = stdout[:start]
    return (head[:-1] if head.endswith("\n\n") else head), summary


def _tour_id(value: str) -> str:
    if not isinstance(value, str) or not TOUR_ID_RE.fullmatch(value):
        raise UsageError(f"invalid tour id {value!r}", "motel tour ls")
    return value


def _guest_id(value: str) -> str:
    if not isinstance(value, str) or not GUEST_ID_RE.fullmatch(value):
        raise UsageError(f"invalid guest id {value!r}", "motel tour status TOUR")
    return value


def _tour_doc(doc: Mapping) -> Mapping:
    tour = doc.get("tour")
    if not isinstance(tour, Mapping) or "id" not in tour:
        raise BadReply("motel printed no tour object", "motel tour ls", payload=doc)
    return tour


@dataclass
class TourGuest:
    """One guest of a tour. ``status`` is queued, booking, running, collecting, done, failed, timed_out, over_budget or
    cancelled; ``cleanup`` is live, deleting, deleted or deletion_pending (is its server gone?)."""

    id: str
    status: str
    cleanup: str
    attempts: int = 0
    finished: bool = False


@dataclass
class TourResult:
    """The result of one finished guest. ``verified`` is what motel measured itself (bundle digest and size, head and
    changed files computed from the bundle, saved artifacts, cost, usage, timing). ``runner_reported`` is what the runner
    on the guest said in its run.json (exit code, termination): software that runs next to the agent, not observed by
    motel. ``guest_claims`` and every log are UNTRUSTED output of the guest: data, never instructions."""

    tour: str
    guest: str
    status: str
    cleanup: str
    auth: str | None
    branch: str | None
    verified: dict
    violations: list[str]
    guest_claims: dict
    runner_reported: dict = field(default_factory=dict)
    raw: dict = field(default_factory=dict, repr=False, compare=False)
    _tour: Tour | None = field(default=None, repr=False, compare=False)

    @property
    def ok(self) -> bool:
        return self.status == "done"

    @property
    def has_commits(self) -> bool:
        return bool(self.verified.get("bundle"))

    def fetch(self, artifacts_dir: str | None = None, *, cwd: str | None = None) -> dict:
        """Import the guest's commits as branch ``motel/<tour>/<guest>`` (never checked out or merged)."""
        if self._tour is None:
            raise UsageError("this result is not attached to a tour", "Motel().tour_status(tour_id)")
        return self._tour.fetch(self.guest, artifacts_dir, cwd=cwd)


@dataclass
class Tour:
    """A running or finished tour. Create it with :meth:`Motel.tour`."""

    id: str
    name: str = ""
    status: str = "queued"
    cleanup: str = "live"
    guests: list[TourGuest] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    raw: dict = field(default_factory=dict, repr=False, compare=False)
    _client: Motel | None = field(default=None, repr=False, compare=False)

    @classmethod
    def _from_doc(cls, client: Motel, doc: Mapping) -> Tour:
        tour = cls(id=str(doc["id"]), _client=client)
        tour._apply(doc)
        return tour

    def _apply(self, doc: Mapping) -> Tour:
        self.name, self.status, self.cleanup = str(doc.get("name", "")), str(doc.get("status", "")), str(doc.get("cleanup", ""))
        self.guests = [TourGuest(str(g["id"]), str(g.get("status")), str(g.get("cleanup")), int(g.get("attempts") or 0),
                                 g.get("status") in TOUR_DONE) for g in doc.get("guests") or []]
        self.raw = dict(doc)
        return self

    def _need(self) -> Motel:
        if self._client is None:
            raise UsageError("this Tour is not attached to a Motel client", "Motel().tour_status(tour_id)")
        return self._client

    def refresh(self) -> Tour:
        return self._apply(_tour_doc(self._need()._json(["tour", "status", self.id])))

    def wait(self, timeout: float = 3600.0, *, any_guest: bool = False) -> bool:
        """Block until all guests (or any, with ``any_guest``) have finished. Returns False if ``timeout`` ran out first."""
        client = self._need()
        doc = client._json(["tour", "wait", self.id, "--any" if any_guest else "--all", "--timeout", str(int(timeout))],
                           timeout + 60, tolerate=(9,))
        self._apply(_tour_doc(doc))
        return bool(doc.get("satisfied", True))

    def result(self, guest: str) -> TourResult:
        """The result of one finished guest."""
        doc = self._need()._json(["tour", "result", self.id, _guest_id(guest)])
        r = doc.get("result")
        if not isinstance(r, Mapping):
            raise BadReply("motel printed no result", "motel tour status " + self.id, payload=doc)
        claims = r.get("guest_claims") or {}
        return TourResult(tour=str(r.get("tour")), guest=str(r.get("guest")), status=str(r.get("status")),
                          cleanup=str(r.get("cleanup")), auth=r.get("auth"), branch=r.get("branch"),
                          verified=dict(r.get("verified") or {}), runner_reported=dict(r.get("runner_reported") or {}),
                          violations=list(r.get("violations") or []),
                          guest_claims={"untrusted": True, "result": claims.get("result")}, raw=dict(r), _tour=self)

    def as_completed(self, *, poll_s: float = 5.0, timeout: float | None = None, sleep=time.sleep,
                     monotonic=time.monotonic) -> Iterator[TourResult]:
        """Yield each guest's :class:`TourResult` as it finishes, in the order they finish."""
        seen: set = set()
        deadline = None if timeout is None else monotonic() + timeout
        while True:
            self.refresh()
            for guest in self.guests:
                if guest.finished and guest.id not in seen:
                    seen.add(guest.id)
                    yield self.result(guest.id)
            if len(seen) == len(self.guests):
                return
            if deadline is not None and monotonic() >= deadline:
                raise Timeout(f"tour {self.id} did not finish within {timeout:g}s; it keeps running", f"motel tour wait {self.id}")
            sleep(poll_s)

    def results(self, *, timeout: float | None = None, poll_s: float = 5.0) -> list[TourResult]:
        """Wait for every guest and return all results in guest order."""
        found = {r.guest: r for r in self.as_completed(poll_s=poll_s, timeout=timeout)}
        return [found[g.id] for g in self.guests]

    def fetch(self, guest: str, artifacts_dir: str | None = None, *, cwd: str | None = None) -> dict:
        """``motel tour fetch``: import the guest's commits into the repository at ``cwd`` (default: the current one)."""
        args = ["tour", "fetch", self.id, _guest_id(guest)] + (["--artifacts", artifacts_dir] if artifacts_dir else [])
        proc = self._need()._spawn([*args, "--json"], self._need().cabin_timeout, cwd=cwd)
        doc = _doc(proc.stdout)
        if proc.returncode != 0 or doc is None:
            raise _error(proc.returncode, doc, proc.stderr)
        return doc

    def cancel(self) -> Tour:
        """Stop the tour: placeholders revoked, guests stopped, output collected, servers deleted."""
        return self._apply(_tour_doc(self._need()._json(["tour", "cancel", self.id], self._need().cabin_timeout)))

# --- the client ----------------------------------------------------------------------------------------


class Motel:
    """A client for one ``motel`` executable.

    ``binary`` is the CLI to run (a name on PATH or a path). ``timeout`` bounds room calls and quick cabin
    calls (seconds); ``cabin_timeout`` bounds the slow ones that create a server or run a command. ``env``
    replaces the child environment when given.
    """

    def __init__(self, binary: str = "motel", timeout: float | None = DEFAULT_TIMEOUT, *,
                 cabin_timeout: float | None = DEFAULT_CABIN_TIMEOUT, env: Mapping[str, str] | None = None):
        self.binary = binary
        self.timeout = timeout
        self.cabin_timeout = cabin_timeout
        self.env = dict(env) if env is not None else None

    # -- plumbing
    def _spawn(self, args: Sequence[str], timeout: float | None, cwd: str | None = None,
               stdin_text: str | None = None) -> subprocess.CompletedProcess:
        feed = {"stdin": subprocess.DEVNULL} if stdin_text is None else {"input": stdin_text}
        try:
            return subprocess.run([self.binary, *args], capture_output=True, text=True,
                                  timeout=timeout, check=False, shell=False, env=self.env, cwd=cwd, **feed)
        except FileNotFoundError:
            raise NotInstalled(f"cannot find the motel executable {self.binary!r}", "pipx install motel") from None
        except PermissionError:
            raise NotInstalled(f"cannot run the motel executable {self.binary!r}", "motel --version") from None
        except subprocess.TimeoutExpired:
            raise Timeout(f"motel {args[0] if args else ''} did not finish within {timeout:g}s; it may still have happened",
                          "motel ls --mine") from None

    def _json(self, args: Sequence[str], timeout: float | None = None, *, tolerate: Sequence[int] = (),
              stdin_text: str | None = None) -> dict:
        """Run ``motel ARGS --json`` and return the parsed object; map failures to exceptions.

        ``tolerate`` lists nonzero exit codes that still carry a success-shaped document (for example 9 on
        ``cabin ls`` when the provider inventory is incomplete).
        """
        proc = self._spawn([*args, "--json"], self.timeout if timeout is None else timeout, stdin_text=stdin_text)
        doc = _doc(proc.stdout)
        if proc.returncode == 0:
            if doc is None or doc.get("ok") is False:
                raise BadReply("motel printed unreadable output (expected one JSON object)", "motel doctor", payload=doc)
            return doc
        if proc.returncode in tolerate and doc is not None and "error" not in doc:
            return doc
        raise _error(proc.returncode, doc, proc.stderr)

    def _room(self, doc: Mapping) -> Room:
        data = doc.get("room")
        if not isinstance(data, Mapping) or "id" not in data:
            raise BadReply("motel printed no room object", "motel ls --mine", payload=doc)
        return Room.from_json(data, self)

    def _cabin(self, doc: Mapping) -> Cabin:
        # `cabin checkin --json` prints the cabin object itself; the other verbs wrap it as {"cabin": {...}}.
        data = doc.get("cabin") if isinstance(doc.get("cabin"), Mapping) else doc
        if not data.get("lease_id"):
            raise BadReply("motel printed no cabin object", "motel cabin ls", payload=doc)
        return Cabin.from_json(data, self)

    # -- rooms
    def checkin(self, size_gb: int | str, ttl: str = "6h", task: str | None = None, suite: bool = False) -> Room:
        """Take a room of ``size_gb`` GB (or ``"200G"``, ``"1T"``) until ``ttl`` from now."""
        args = ["checkin", _size(size_gb), "--for", _duration(ttl)]
        if task:
            args += ["--task", _pattern(task, TASK_RE, "task", "video-render")]
        if suite:
            args.append("--suite")
        return self._room(self._json(args))

    @contextlib.contextmanager
    def room(self, size_gb: int | str, ttl: str = "6h", task: str | None = None, suite: bool = False, *,
             keep: bool = False, now: bool = False) -> Iterator[Room]:
        """Check in a room, yield it, and check it out when the block ends (unless ``keep=True``).

        ``now=True`` deletes the data at once instead of moving it to lost & found. A checkout failure after a
        failed block is reported as a warning so it cannot hide the original exception.
        """
        room = self.checkin(size_gb, ttl, task, suite)
        try:
            yield room
        except BaseException:
            if not keep:
                self._quiet_checkout(room, now)
            raise
        else:
            if not keep:
                room.checkout(now=now)

    def _quiet_checkout(self, room: Room, now: bool) -> None:
        try:
            room.checkout(now=now)
        except MotelError as exc:
            warnings.warn(f"motel: could not check out room {room.id}: {exc}", RuntimeWarning, stacklevel=3)

    def overview(self, scope: str = "mine") -> Overview:
        """Rooms plus the host's floors and capacity. ``scope`` is ``"mine"`` or ``"all"``."""
        if scope not in ("mine", "all"):
            raise UsageError(f"invalid scope {scope!r}: use 'mine' or 'all'", "motel ls --mine")
        doc = self._json(["ls", f"--{scope}"])
        return Overview([Room.from_json(r, self) for r in doc.get("rooms") or []], list(doc.get("floors") or []),
                        dict(doc.get("capacity") or {}))

    def rooms(self, scope: str = "mine") -> list[Room]:
        return self.overview(scope).rooms

    def status(self, room_id: str) -> Room:
        return self._room(self._json(["status", _ident(room_id, "room")]))

    def extend(self, room_id: str, ttl: str) -> Room:
        """Late checkout: push the checkout time out by ``ttl``. Also revives a room in its grace period."""
        return self._room(self._json(["extend", _ident(room_id, "room"), "--for", _duration(ttl)]))

    def checkout(self, room_id: str, now: bool = False) -> Room:
        """Check out a room. It moves to lost & found for a day; ``now=True`` deletes it at once (irreversible)."""
        args = ["checkout", _ident(room_id, "room")]
        if now:
            args.append("--now")
        return self._room(self._json(args))

    def restore(self, room_id: str, ttl: str | None = None) -> Room:
        args = ["restore", _ident(room_id, "room")]
        if ttl:
            args += ["--for", _duration(ttl)]
        return self._room(self._json(args))

    def doctor(self) -> Doctor:
        """Run the health check. A failing check is data (``ok`` false), not an exception."""
        doc = self._json(["doctor"], tolerate=(5,))
        checks = [Check(str(c.get("check", "")), str(c.get("level", "")), bool(c.get("ok")), str(c.get("detail") or ""))
                  for c in doc.get("checks") or []]
        return Doctor(bool(doc.get("ok")) and not any(c.level == "fail" for c in checks), checks)

    # -- cabins (experimental)
    def cabin_checkin(self, server_type: str | None = None, ttl: str | None = None, task: str | None = None,
                      disk_gb: int | str | None = None) -> Cabin:
        args = ["cabin", "checkin"]
        if server_type:
            args += ["--type", _pattern(server_type, TYPE_RE, "server type", "cpx52")]
        if ttl:
            args += ["--for", _duration(ttl)]
        if task:
            args += ["--task", _pattern(task, TASK_RE, "task", "test-suite")]
        if disk_gb:
            args += ["--disk", _size(disk_gb)]
        return self._cabin(self._json(args, self.cabin_timeout))

    @contextlib.contextmanager
    def cabin(self, server_type: str | None = None, ttl: str | None = None, task: str | None = None,
              disk_gb: int | str | None = None, *, keep: bool = False) -> Iterator[Cabin]:
        """Check in a cabin, yield it, and delete it when the block ends (unless ``keep=True``). Deletion is final.

        A cabin left in ``cleanup_pending`` keeps billing: the checkout then raises :class:`InventoryIncomplete`
        (or warns, when the block itself failed).
        """
        cabin = self.cabin_checkin(server_type, ttl, task, disk_gb)
        try:
            yield cabin
        except BaseException:
            if not keep:
                try:
                    cabin.checkout()
                except MotelError as exc:
                    warnings.warn(f"motel: could not check out cabin {cabin.id}: {exc}", RuntimeWarning, stacklevel=2)
            raise
        else:
            if not keep:
                cabin.checkout()

    def cabins(self, *, strict: bool = False) -> list[Cabin]:
        """Your cabins. Exit 9 (provider inventory incomplete) still returns the local records unless ``strict``."""
        doc = self._json(["cabin", "ls"], tolerate=() if strict else (9,))
        return [Cabin.from_json(c, self) for c in doc.get("cabins") or []]

    def cabin_status(self, cabin_id: str) -> Cabin:
        return self._cabin(self._json(["cabin", "status", _ident(cabin_id, "cabin")]))

    def cabin_extend(self, cabin_id: str, ttl: str) -> Cabin:
        return self._cabin(self._json(["cabin", "extend", _ident(cabin_id, "cabin"), "--for", _duration(ttl)]))

    def cabin_checkout(self, cabin_id: str, *, strict: bool = True) -> Cabin:
        """Delete a cabin. Final. Exit 9 means cleanup is pending (it still bills): raised unless ``strict=False``."""
        doc = self._json(["cabin", "checkout", _ident(cabin_id, "cabin")], self.cabin_timeout, tolerate=(9,))
        cabin = self._cabin(doc)
        if strict and not cabin.released:
            raise InventoryIncomplete(f"cabin {cabin.id} is {cabin.state}, not released; it may still bill",
                                      f"motel cabin status {cabin.id}", payload=doc)
        return cabin

    def cabin_ssh(self, cabin_id: str, command: Sequence[str] | str, *, check: bool = False,
                  timeout: float | None = None) -> CabinRun:
        """Run a command in an existing cabin (``motel cabin ssh ID -- CMD``). No files are synced."""
        argv = _command(command)
        proc = self._spawn(["cabin", "ssh", _ident(cabin_id, "cabin"), "--", *argv],
                           self.cabin_timeout if timeout is None else timeout)
        return self._finish_run(proc, check, cabin_id)

    def cabin_run(self, command: Sequence[str] | str, *, sync: str | None = None, server_type: str | None = None,
                  ttl: str | None = None, task: str | None = None, disk_gb: int | str | None = None, keep: bool = False,
                  check: bool = False, timeout: float | None = None) -> CabinRun:
        """One shot: check in a cabin, sync a git checkout to it, run ``command`` there, check out.

        ``sync`` is the git worktree to copy (default: the current directory). The CLI copies tracked files plus
        untracked files that are not ignored, to ``~/work`` on the cabin, and runs the command there. The cabin is
        deleted afterwards unless ``keep=True``. Because the command's exit status is returned as ``returncode``,
        a nonzero value is not mapped to an exception (it cannot be told apart from the command's own failure);
        pass ``check=True`` to raise :class:`MotelError` for any nonzero status.
        """
        args = ["cabin", "run", "--json"]
        if server_type:
            args += ["--type", _pattern(server_type, TYPE_RE, "server type", "cpx52")]
        if ttl:
            args += ["--for", _duration(ttl)]
        if task:
            args += ["--task", _pattern(task, TASK_RE, "task", "test-suite")]
        if disk_gb:
            args += ["--disk", _size(disk_gb)]
        if keep:
            args.append("--keep")
        proc = self._spawn([*args, "--", *_command(command)], self.cabin_timeout if timeout is None else timeout, cwd=sync)
        return self._finish_run(proc, check, None)

    def _finish_run(self, proc: subprocess.CompletedProcess, check: bool, cabin_id: str | None) -> CabinRun:
        stdout, summary = _split_run_summary(proc.stdout)
        started = _CABIN_STARTED.search(proc.stderr)
        released = _CABIN_RELEASED.search(proc.stderr)
        if summary is not None:
            run = CabinRun(proc.returncode, stdout, proc.stderr, cabin_id or summary.get("cabin"),
                           _decimal(summary.get("cost_estimate_net_eur")), _decimal(summary.get("hourly_net_eur")),
                           summary.get("released"), summary.get("kept"))
        else:
            pending = _CABIN_NOT_RELEASED.search(proc.stderr)
            run = CabinRun(proc.returncode, stdout, proc.stderr, cabin_id or (started.group(1) if started else None),
                           _decimal(released.group(2)) if released else None,
                           released=False if pending else (True if released else None))
        if check and proc.returncode != 0:
            failure = _doc(stdout.strip().splitlines()[-1]) if stdout.strip() and summary is None else None
            raise _error(proc.returncode, failure if failure and failure.get("error") else None, proc.stderr)
        return run

    # -- tours
    def tour(self, spec: Mapping | str, *, timeout: float | None = None) -> Tour:
        """Start a tour: a swarm of bounded, non-interactive agent jobs, each on its own disposable server.

        ``spec`` is a mapping with the fields of the TOML spec in ``docs/tours.md`` (``tour``, ``defaults``,
        ``guests``), or a path to a TOML or JSON spec file. The current directory's git repository is bundled for
        each guest, so run it inside the repository. Returns at once with the tour id; use ``as_completed()`` or
        ``results()``. A tour can only ask for less than the operator's ceilings: a refusal raises :class:`NoVacancy`
        (exit 3) whose message names the ceiling.
        """
        if isinstance(spec, str):
            doc = self._json(["tour", "start", spec], timeout or self.cabin_timeout)
        elif isinstance(spec, Mapping):
            try:
                text = json.dumps(spec)
            except (TypeError, ValueError):
                raise UsageError("the tour spec must be plain JSON data", "docs/tours.md") from None
            doc = self._json(["tour", "start", "-"], timeout or self.cabin_timeout, stdin_text=text)
        else:
            raise UsageError("give the tour spec as a mapping or a file path", "docs/tours.md")
        return Tour._from_doc(self, _tour_doc(doc))

    def tour_status(self, tour_id: str) -> Tour:
        return Tour._from_doc(self, _tour_doc(self._json(["tour", "status", _tour_id(tour_id)])))

    def tours(self) -> list[Tour]:
        doc = self._json(["tour", "ls"])
        return [Tour._from_doc(self, t) for t in doc.get("tours") or []]


# --- asyncio -------------------------------------------------------------------------------------------


class AsyncMotel:
    """The same client for asyncio: each call runs the blocking :class:`Motel` call in a worker thread.

    Cancelling an ``await`` stops waiting but does not stop the CLI; the subprocess finishes (or times out) on its
    own. Prefer ``async with amotel.room(...)`` so the checkout still runs. ``Room`` and ``Cabin`` objects it
    returns have blocking helper methods: call ``await amotel.extend(room.id, ttl)`` instead.
    """

    def __init__(self, binary: str = "motel", timeout: float | None = DEFAULT_TIMEOUT, *,
                 cabin_timeout: float | None = DEFAULT_CABIN_TIMEOUT, env: Mapping[str, str] | None = None):
        self.sync = Motel(binary, timeout, cabin_timeout=cabin_timeout, env=env)

    @staticmethod
    async def _to(fn, /, *args, **kwargs):
        import asyncio
        return await asyncio.to_thread(fn, *args, **kwargs)

    @contextlib.asynccontextmanager
    async def room(self, size_gb: int | str, ttl: str = "6h", task: str | None = None, suite: bool = False, *,
                   keep: bool = False, now: bool = False) -> AsyncIterator[Room]:
        room = await self.checkin(size_gb, ttl, task, suite)
        try:
            yield room
        except BaseException:
            if not keep:
                try:
                    await self._to(self.sync.checkout, room.id, now)
                except MotelError as exc:
                    warnings.warn(f"motel: could not check out room {room.id}: {exc}", RuntimeWarning, stacklevel=2)
            raise
        else:
            if not keep:
                await self._to(self.sync.checkout, room.id, now)

    @contextlib.asynccontextmanager
    async def cabin(self, server_type: str | None = None, ttl: str | None = None, task: str | None = None,
                    disk_gb: int | str | None = None, *, keep: bool = False) -> AsyncIterator[Cabin]:
        cabin = await self.cabin_checkin(server_type, ttl, task, disk_gb)
        try:
            yield cabin
        except BaseException:
            if not keep:
                try:
                    await self._to(self.sync.cabin_checkout, cabin.id)
                except MotelError as exc:
                    warnings.warn(f"motel: could not check out cabin {cabin.id}: {exc}", RuntimeWarning, stacklevel=2)
            raise
        else:
            if not keep:
                await self._to(self.sync.cabin_checkout, cabin.id)

    async def checkin(self, *a, **k) -> Room:
        return await self._to(self.sync.checkin, *a, **k)

    async def overview(self, *a, **k) -> Overview:
        return await self._to(self.sync.overview, *a, **k)

    async def rooms(self, *a, **k) -> list[Room]:
        return await self._to(self.sync.rooms, *a, **k)

    async def status(self, *a, **k) -> Room:
        return await self._to(self.sync.status, *a, **k)

    async def extend(self, *a, **k) -> Room:
        return await self._to(self.sync.extend, *a, **k)

    async def checkout(self, *a, **k) -> Room:
        return await self._to(self.sync.checkout, *a, **k)

    async def restore(self, *a, **k) -> Room:
        return await self._to(self.sync.restore, *a, **k)

    async def doctor(self) -> Doctor:
        return await self._to(self.sync.doctor)

    async def cabin_checkin(self, *a, **k) -> Cabin:
        return await self._to(self.sync.cabin_checkin, *a, **k)

    async def cabins(self, *a, **k) -> list[Cabin]:
        return await self._to(self.sync.cabins, *a, **k)

    async def cabin_status(self, *a, **k) -> Cabin:
        return await self._to(self.sync.cabin_status, *a, **k)

    async def cabin_extend(self, *a, **k) -> Cabin:
        return await self._to(self.sync.cabin_extend, *a, **k)

    async def cabin_checkout(self, *a, **k) -> Cabin:
        return await self._to(self.sync.cabin_checkout, *a, **k)

    async def cabin_ssh(self, *a, **k) -> CabinRun:
        return await self._to(self.sync.cabin_ssh, *a, **k)

    async def cabin_run(self, *a, **k) -> CabinRun:
        return await self._to(self.sync.cabin_run, *a, **k)

