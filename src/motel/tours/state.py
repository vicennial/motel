"""Durable tour state, root-owned, under ``/var/lib/motel/tours/<id>/``.

Every file is written through a fresh ``O_EXCL`` temp file and renamed, and never follows a planted symlink.
The caller's inputs are copied in through an already opened descriptor, with a size cap and a digest. Intent
is written before every create, so a crash leaves enough to reconcile (``tour reap``).

Layout of one tour::

    tour.json          the tour: owner, resolved spec, limits, budget, supervisor, per-guest status
    attempts/<a>.json  one attempt: phase, lease, flags, cost, usage (written only by the worker of that guest)
    inputs/<key>       staged git bundles and overlay patches
    results/<guest>/   result.json, result.bundle, artifacts/, stdout.log, stderr.log, run.json
    events.jsonl       append-only log of what the engine did (ids and codes only)
    cancel             exists once a cancel was requested
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import secrets
import shutil
import stat
import time
from datetime import timedelta

from motel.cabins.store import atomic_write
from motel.cabins.util import iso, parse_iso, utcnow
from motel.tours.errors import Broken, NotFound, Refused, Usage

SCHEMA = "motel/tour/v1"
TOUR_ID_RE = re.compile(r"^t-[0-9a-f]{10}$")
ATTEMPT_ID_RE = re.compile(r"^a-[0-9a-f]{10}$")

EXEC_STATES = ("queued", "booking", "running", "collecting", "done", "failed", "timed_out", "over_budget", "cancelled")
TERMINAL = ("done", "failed", "timed_out", "over_budget", "cancelled")
CLEANUP_STATES = ("live", "deleting", "deleted", "deletion_pending")
RETENTION = timedelta(days=14)        # finished tours are removed by `tour reap --apply` after this long
READ_CHUNK = 1 << 20


def new_tour_id() -> str:
    return "t-" + secrets.token_hex(5)


def new_attempt_id() -> str:
    return "a-" + secrets.token_hex(5)


def worst_cleanup(states) -> str:
    """The cleanup status of a set of attempts: a pending deletion outranks live, live outranks deleting."""
    states = list(states)
    for state in ("deletion_pending", "live", "deleting"):
        if state in states:
            return state
    return "deleted"


def sha256_file(path: str) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(READ_CHUNK), b""):
            digest.update(chunk)
    return digest.hexdigest()


def open_user_file(path, uid: int, max_bytes: int):
    """Open a file the caller named, as root would, without being fooled: no symlink at the end, a regular file
    owned by the caller (root may name any), and not larger than ``max_bytes``. Returns (fd, size)."""
    if not isinstance(path, str) or not path.startswith("/") or "\x00" in path or len(path) > 4096:
        raise Usage("an input must be named by an absolute path")
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except OSError as exc:
        raise Usage(f"cannot open an input file ({exc.strerror or type(exc).__name__}); it must be a regular file, not a link") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Usage("an input must be a regular file")
        if uid != 0 and st.st_uid != uid:
            raise Usage("an input file must belong to you")
        if st.st_size > max_bytes:
            raise Refused(f"an input file is larger than the limit of {max_bytes // (1024 * 1024)} MiB")
    except BaseException:
        os.close(fd)
        raise
    return fd, st.st_size


class TourStore:
    def __init__(self, root: str, clock=utcnow):
        self.root, self.clock = root, clock
        os.makedirs(root, mode=0o700, exist_ok=True)
        os.chmod(root, 0o700)

    # -- paths and ids
    def path(self, tour_id: str, *parts: str) -> str:
        if not TOUR_ID_RE.fullmatch(tour_id or ""):
            raise Usage(f"bad tour id {tour_id!r}", "motel tour ls")
        return os.path.join(self.root, tour_id, *parts)

    def exists(self, tour_id: str) -> bool:
        return os.path.isfile(self.path(tour_id, "tour.json"))

    def ids(self) -> list:
        try:
            names = sorted(os.listdir(self.root))
        except OSError:
            return []
        return [n for n in names if TOUR_ID_RE.fullmatch(n)]

    # -- the tour record
    def create(self, tour_id: str, doc: dict) -> None:
        base = self.path(tour_id)
        try:
            os.mkdir(base, 0o700)
        except FileExistsError:
            raise Broken(f"tour {tour_id} already exists") from None
        for sub in ("attempts", "inputs", "results", "tmp"):
            os.mkdir(os.path.join(base, sub), 0o700)
        self.save(doc)
        self.event(tour_id, "created", owner=doc["owner"]["uid"], guests=sorted(doc["guests"]))

    def load(self, tour_id: str) -> dict:
        try:
            with open(self.path(tour_id, "tour.json"), encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            raise NotFound(f"no tour {tour_id}") from None
        except (OSError, ValueError) as exc:
            raise Broken(f"tour {tour_id} is unreadable: {type(exc).__name__}") from None

    def save(self, doc: dict) -> None:
        doc["updated_at"] = iso(self.clock())
        atomic_write(self.path(doc["id"], "tour.json"), json.dumps(doc, sort_keys=True, indent=1))

    @contextlib.contextmanager
    def lock(self, tour_id: str):
        fd = os.open(self.path(tour_id, ".lock"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    @contextlib.contextmanager
    def admission(self, name: str, timeout: float | None = None):
        """An exclusive lock between processes, for the checks and writes of one `tour start`. ``name`` is ``user-<uid>``
        (the ceilings on one user's tours) or ``host`` (the free-space check and staging); take the user lock first."""
        if not re.fullmatch(r"(host|user-[0-9]{1,10})", name):
            raise ValueError("bad admission lock name")
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        fd = os.open(os.path.join(self.root, f".admit-{name}"), os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            if timeout is None:
                fcntl.flock(fd, fcntl.LOCK_EX)
            else:
                until = time.monotonic() + timeout
                while True:
                    try:
                        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                        break
                    except BlockingIOError:
                        if time.monotonic() >= until:
                            raise Refused("the host is busy starting other tours right now", "wait a moment and run it again") from None
                        time.sleep(0.05)
            yield
        finally:
            os.close(fd)

    def update(self, tour_id: str, fn) -> dict:
        """Load, change and save the tour record under its lock. ``fn`` edits the document in place."""
        with self.lock(tour_id):
            doc = self.load(tour_id)
            fn(doc)
            self.save(doc)
            return doc

    # -- attempts
    def save_attempt(self, tour_id: str, rec: dict) -> None:
        rec["updated_at"] = iso(self.clock())
        atomic_write(self.path(tour_id, "attempts", rec["id"] + ".json"), json.dumps(rec, sort_keys=True, indent=1))

    def load_attempt(self, tour_id: str, attempt_id: str) -> dict:
        if not ATTEMPT_ID_RE.fullmatch(attempt_id or ""):
            raise Usage(f"bad attempt id {attempt_id!r}")
        try:
            with open(self.path(tour_id, "attempts", attempt_id + ".json"), encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            raise NotFound(f"no attempt {attempt_id}") from None
        except (OSError, ValueError):
            raise Broken(f"attempt {attempt_id} is unreadable") from None

    def attempts(self, tour_id: str) -> list:
        out = []
        folder = self.path(tour_id, "attempts")
        for name in sorted(os.listdir(folder)) if os.path.isdir(folder) else []:
            if name.endswith(".json") and not name.startswith("."):
                with contextlib.suppress(Exception):
                    out.append(self.load_attempt(tour_id, name[:-5]))
        return sorted(out, key=lambda r: (r.get("guest", ""), r.get("n", 0)))

    # -- events, cancel
    def event(self, tour_id: str, name: str, **fields) -> None:
        line = json.dumps({"ts": iso(self.clock()), "event": name, **fields}, sort_keys=True)
        fd = os.open(self.path(tour_id, "events.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)

    def request_cancel(self, tour_id: str) -> None:
        atomic_write(self.path(tour_id, "cancel"), iso(self.clock()))

    def cancel_requested(self, tour_id: str) -> bool:
        return os.path.exists(self.path(tour_id, "cancel"))

    # -- inputs
    def stage_input(self, tour_id: str, key: str, src_fd: int, max_bytes: int) -> tuple:
        """Copy an opened file into the tour as ``inputs/<key>``. Returns (sha256, bytes)."""
        digest, total = hashlib.sha256(), 0
        dest = self.path(tour_id, "inputs", key)
        out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
        try:
            with os.fdopen(out, "wb") as handle, os.fdopen(os.dup(src_fd), "rb") as source:
                for chunk in iter(lambda: source.read(READ_CHUNK), b""):
                    total += len(chunk)
                    if total > max_bytes:
                        raise Refused(f"an input file is larger than the limit of {max_bytes // (1024 * 1024)} MiB")
                    digest.update(chunk)
                    handle.write(chunk)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(dest)
            raise
        return digest.hexdigest(), total

    def input_path(self, tour_id: str, key: str) -> str:
        return self.path(tour_id, "inputs", key)

    def staged_bytes(self, tour_id: str) -> int:
        """Bytes of the caller's files still staged for this tour (0 once the tour is over and they were dropped)."""
        total = 0
        try:
            for name in os.listdir(self.path(tour_id, "inputs")):
                with contextlib.suppress(OSError):
                    total += os.lstat(os.path.join(self.path(tour_id, "inputs"), name)).st_size
        except OSError:
            pass
        return total

    def drop_inputs(self, tour_id: str) -> None:
        """Delete the staged copies of the caller's files. Idempotent; called whenever a tour reaches a terminal state."""
        folder = self.path(tour_id, "inputs")
        try:
            names = os.listdir(folder)
        except OSError:
            return
        for name in names:
            with contextlib.suppress(OSError):
                os.unlink(os.path.join(folder, name))

    # -- results
    def results_dir(self, tour_id: str, guest: str, create: bool = False) -> str:
        path = self.path(tour_id, "results", guest)
        if create:
            os.makedirs(path, mode=0o700, exist_ok=True)
        return path

    def remove(self, tour_id: str) -> None:
        shutil.rmtree(self.path(tour_id), ignore_errors=True)

    def age(self, doc: dict):
        stamp = parse_iso(doc.get("finished_at") or doc.get("created_at"))
        return None if stamp is None else self.clock() - stamp


def write_new_file(directory_fd: int, name: str, data_chunks, mode: int = 0o600) -> tuple:
    """Create ``name`` below an open directory: exclusive, no symlink. Returns (sha256, bytes)."""
    if "/" in name or name in ("", ".", ".."):
        raise Usage("bad file name")
    fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode, dir_fd=directory_fd)
    digest, total = hashlib.sha256(), 0
    try:
        with os.fdopen(fd, "wb") as handle:
            for chunk in data_chunks:
                digest.update(chunk)
                total += len(chunk)
                handle.write(chunk)
            handle.flush()
            os.fsync(handle.fileno())
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(name, dir_fd=directory_fd)
        raise
    return digest.hexdigest(), total


def open_dir_path(root_fd: int, parts) -> int:
    """Open (creating each level, 0700) a nested directory below ``root_fd`` without following any link."""
    fd = os.dup(root_fd)
    try:
        for part in parts:
            if part in ("", ".", ".."):
                raise Usage("bad directory name")
            try:
                os.mkdir(part, 0o700, dir_fd=fd)
            except FileExistsError:
                pass
            nxt = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC, dir_fd=fd)
            os.close(fd)
            fd = nxt
        return fd
    except BaseException:
        os.close(fd)
        raise
