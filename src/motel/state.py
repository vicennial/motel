"""Root-owned state: one global lock, JSON records written atomically, and the append-only guest book."""

from __future__ import annotations

import fcntl
import json
import os
import secrets
from contextlib import contextmanager

from motel.errors import Guard, SystemFault
from motel.units import FLOOR_RE, ROOM_RE, iso

SCHEMA = "motel/v1"


def atomic_write(path: str, text: str, mode: int = 0o600) -> None:
    """Write ``path`` via a fresh O_EXCL temp file and rename; never follows a pre-placed symlink."""
    directory = os.path.dirname(path)
    tmp = os.path.join(directory, f".{os.path.basename(path)}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


def safe_write_in_dir(directory: str, name: str, text: str, mode: int = 0o644) -> None:
    """Write a root-owned file inside a directory the caller owns, without following symlinks.

    The directory is opened with O_NOFOLLOW and every step is relative to that descriptor, so a caller
    who replaces the name with a symlink only replaces the link itself.
    """
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    try:
        tmp = f".{name}.{secrets.token_hex(4)}.tmp"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, mode, dir_fd=dir_fd)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                handle.write(text)
            os.rename(tmp, name, src_dir_fd=dir_fd, dst_dir_fd=dir_fd)
        except BaseException:
            try:
                os.unlink(tmp, dir_fd=dir_fd)
            except OSError:
                pass
            raise
    finally:
        os.close(dir_fd)


class Store:
    def __init__(self, state_dir: str, clock, host_id: str):
        self.dir, self.clock, self.host = state_dir, clock, host_id
        self.rooms_dir = os.path.join(state_dir, "rooms")
        self.floors_dir = os.path.join(state_dir, "floors")
        for directory in (state_dir, self.rooms_dir, self.floors_dir):
            os.makedirs(directory, mode=0o700, exist_ok=True)

    @contextmanager
    def locked(self):
        fd = os.open(os.path.join(self.dir, ".lock"), os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def ledger(self, event: str, **fields) -> None:
        record = {"ts": iso(self.clock()), "event": event, "host": self.host, **fields}
        line = json.dumps(record, sort_keys=True) + "\n"
        fd = os.open(os.path.join(self.dir, "guestbook.jsonl"), os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, line.encode())
            os.fsync(fd)
        finally:
            os.close(fd)

    # -- records
    @staticmethod
    def _read(path: str) -> dict:
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except FileNotFoundError:
            raise
        except (OSError, ValueError) as exc:
            raise SystemFault(f"state file {path} is unreadable: {exc}") from exc
        if not isinstance(doc, dict):
            raise SystemFault(f"state file {path} is not a record")
        return doc

    def _list(self, directory: str) -> list:
        out = []
        for name in sorted(os.listdir(directory)):
            if name.endswith(".json") and not name.startswith("."):
                try:
                    out.append(self._read(os.path.join(directory, name)))
                except FileNotFoundError:
                    continue
        return out

    def rooms(self) -> list:
        return self._list(self.rooms_dir)

    def room(self, room_id: str):
        if not ROOM_RE.fullmatch(room_id or ""):
            return None
        try:
            return self._read(os.path.join(self.rooms_dir, room_id + ".json"))
        except FileNotFoundError:
            return None

    def save_room(self, room: dict) -> None:
        if not ROOM_RE.fullmatch(room["id"]):
            raise SystemFault(f"refusing to save a room with id {room['id']!r}")
        atomic_write(os.path.join(self.rooms_dir, room["id"] + ".json"), json.dumps(room, indent=1, sort_keys=True))

    def floors(self) -> list:
        return self._list(self.floors_dir)

    def floor(self, floor_id: str):
        if not FLOOR_RE.fullmatch(floor_id or ""):
            return None
        try:
            return self._read(os.path.join(self.floors_dir, floor_id + ".json"))
        except FileNotFoundError:
            return None

    TERMINAL_FLOOR_STATES = ("deleted", "failed")

    def floor_by_volume(self, volume_id: str):
        """The live floor record for a provider volume id. Finished records never match, and two live
        records for one id are a bookkeeping error that must stop the caller."""
        live = [f for f in self.floors() if f.get("volume_id") == str(volume_id) and f.get("state") not in self.TERMINAL_FLOOR_STATES]
        if len(live) > 1:
            raise Guard(f"volume {volume_id} has {len(live)} live floor records ({', '.join(f['id'] for f in live)})", "motel doctor")
        return live[0] if live else None

    def live_volume_ids(self) -> set:
        return {f["volume_id"] for f in self.floors() if f.get("volume_id") and f.get("state") not in self.TERMINAL_FLOOR_STATES}

    def save_floor(self, floor: dict) -> None:
        if not FLOOR_RE.fullmatch(floor["id"]):
            raise SystemFault(f"refusing to save a floor with id {floor['id']!r}")
        atomic_write(os.path.join(self.floors_dir, floor["id"] + ".json"), json.dumps(floor, indent=1, sort_keys=True))
