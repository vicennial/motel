"""The append-only JSONL ledger: the concierge's only persistent state.

One JSON object per line, written with a single ``write`` call to a file opened ``O_APPEND``. Events:

``start``    the process started
``issue``    a token was issued: SHA-256 of the token (never the token), tour, guest, attempt, bindings, expiry, budget
``revoke``   a scope (attempt and/or tour) was revoked
``reserve``  a request was admitted and its worst-case cost reserved (written, and synced, before the upstream call)
``settle``   the request finished: actual cost and token counts; an unsettled ``reserve`` found at start-up is settled
             at its full reserved amount
``refuse``   a request from a known token was refused, with the reason
``snapshot`` compaction: the state of one attempt, carried into a fresh file

Replaying the file rebuilds the in-memory state, so a restart loses nothing (see ``registry.py``). Real keys and
placeholder tokens never reach this module: callers pass only hashes and counters.
"""

from __future__ import annotations

import json
import os
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime

LEDGER_NAME = "ledger.jsonl"
COMPACT_BYTES = 32 * 1024 * 1024


def stamp(epoch: float) -> str:
    return datetime.fromtimestamp(epoch, UTC).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


class Ledger:
    def __init__(self, path: str, *, fsync: bool = True, clock=time.time):
        self.path = path
        self.fsync = fsync
        self.clock = clock
        self._lock = threading.Lock()
        os.makedirs(os.path.dirname(path) or ".", mode=0o700, exist_ok=True)
        self._fd = self._open()

    def _open(self) -> int:
        fd = os.open(self.path, os.O_RDWR | os.O_APPEND | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_CLOEXEC", 0), 0o600)
        size = os.fstat(fd).st_size
        if size and os.pread(fd, 1, size - 1) != b"\n":  # a torn last write: keep the next event on its own line
            os.write(fd, b"\n")
        return fd

    def append(self, ev: str, *, durable: bool = False, **fields) -> None:
        record = {"v": 1, "t": stamp(self.clock()), "ev": ev}
        record.update(fields)
        data = (json.dumps(record, separators=(",", ":"), sort_keys=False) + "\n").encode()
        with self._lock:
            view = memoryview(data)
            while view:
                view = view[os.write(self._fd, view):]
            if durable and self.fsync:
                os.fsync(self._fd)

    def size(self) -> int:
        try:
            return os.fstat(self._fd).st_size
        except OSError:
            return 0

    def rotate(self, snapshots: list[dict]) -> str:
        """Keep the old file under a dated name and start a new one that begins with the given snapshots."""
        with self._lock:
            old = f"{self.path}.{int(self.clock())}"
            os.close(self._fd)
            os.rename(self.path, old)
            self._fd = self._open()
            for snap in snapshots:
                record = {"v": 1, "t": stamp(self.clock()), "ev": "snapshot"}
                record.update(snap)
                os.write(self._fd, (json.dumps(record, separators=(",", ":")) + "\n").encode())
            if self.fsync:
                os.fsync(self._fd)
            return old

    def close(self) -> None:
        with self._lock:
            if self._fd >= 0:
                os.close(self._fd)
                self._fd = -1


def read_events(path: str) -> Iterator[dict]:
    """Yield the events of a ledger file; a damaged or truncated line (for example a torn last write) is skipped."""
    try:
        handle = open(path, "rb")  # noqa: SIM115
    except FileNotFoundError:
        return
    with handle:
        for raw in handle:
            try:
                event = json.loads(raw)
            except ValueError:
                continue
            if isinstance(event, dict) and isinstance(event.get("ev"), str):
                yield event
