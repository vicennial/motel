"""Root-owned lease records: atomic writes, a global lock, per-lease locks and an append-only guest book."""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import secrets
import shutil
import stat
import tempfile
import time

from motel.cabins.errors import Busy, Guarded, NotFound, Usage
from motel.cabins.util import LEASE_ID_RE, iso, parse_iso, utcnow


def atomic_write(path: str, text: str, mode: int = 0o600) -> None:
    """Write via a fresh O_EXCL temp file and rename; never follows a pre-placed symlink."""
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
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    dir_fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(dir_fd)
    finally:
        os.close(dir_fd)


KEY_DIR_MODE = 0o750      # root:<system group>; the group bit lets the unprivileged ssh client traverse
KEY_FILE_MODE = 0o640     # root:<system group>, read-only for the group
DIR_FLAGS = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | os.O_CLOEXEC


class Store:
    def __init__(self, root, clock=utcnow, host_id=None, keys_dir=None):
        """Everything here is owned by root; nothing is ever chowned.

        ``keys_dir`` may live outside ``root`` (the installed layout puts it at ``/var/lib/motel-cabin/keys``,
        root:motel-cabin, mode 2750). The unprivileged system user can read the controller keys through the
        group but can write nothing there, so it cannot redirect root's writes. New files and directories
        inherit the group from the setgid ``keys`` directory.
        """
        self.root, self.clock, self.host_id = root, clock, host_id
        self.leases = os.path.join(root, "leases")
        self.keys = keys_dir or os.path.join(root, "keys")
        self.cache = os.path.join(root, "cache")
        self.tmp = os.path.join(root, "tmp")
        for directory in (root, self.leases, self.cache, self.tmp):
            os.makedirs(directory, mode=0o700, exist_ok=True)
            os.chmod(directory, 0o700)
        # The key tree is verified lazily (key_dir_fd, the guest channel, doctor), never here: a bad layout must
        # not stop reap or checkout from deleting servers that keep billing.

    @staticmethod
    def _dir_fd(name, dir_fd=None, create=True):
        """Open a directory by name (never following a symlink), creating it if absent, and verify with fstat
        on the descriptor that it is owned by this process and writable by nobody else."""
        try:
            try:
                fd = os.open(name, DIR_FLAGS, dir_fd=dir_fd)
            except FileNotFoundError:
                if not create:
                    raise
                made = True
                try:
                    os.mkdir(name, KEY_DIR_MODE, dir_fd=dir_fd)
                except FileExistsError:
                    made = False
                fd = os.open(name, DIR_FLAGS, dir_fd=dir_fd)
                if made:
                    # Exact mode whatever the umask, keeping a setgid bit inherited from the parent: files
                    # created below take their group from it, which is how the system group gets read access.
                    os.fchmod(fd, KEY_DIR_MODE | (os.fstat(fd).st_mode & stat.S_ISGID))
        except OSError:
            raise Guarded("a cabin key directory is missing or is a symlink; refusing to touch it") from None
        st = os.fstat(fd)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o022:
            os.close(fd)
            raise Guarded("a cabin key directory is not private to root; refusing to touch it")
        return fd

    @contextlib.contextmanager
    def _flock(self, path, blocking):
        fd = os.open(path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
            except BlockingIOError:
                raise Busy("this cabin is busy with another motel operation", "motel cabin status") from None
            yield
        finally:
            os.close(fd)

    def global_lock(self):
        return self._flock(os.path.join(self.root, ".lock"), True)

    def lease_lock(self, lease_id, blocking=False):
        if not LEASE_ID_RE.fullmatch(lease_id):
            raise Usage("bad cabin id", "motel cabin ls")
        return self._flock(os.path.join(self.leases, lease_id + ".lock"), blocking)

    def save(self, rec):
        rec["updated_at"] = iso(self.clock())
        atomic_write(os.path.join(self.leases, rec["lease_id"] + ".json"),
                     json.dumps(rec, sort_keys=True, indent=1))

    def load(self, lease_id):
        if not LEASE_ID_RE.fullmatch(lease_id or ""):
            raise Usage(f"bad cabin id {lease_id!r}", "motel cabin ls")
        try:
            with open(os.path.join(self.leases, lease_id + ".json"), encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            raise NotFound(f"no cabin {lease_id}", "motel cabin ls") from None

    def all(self):
        out = []
        for name in sorted(os.listdir(self.leases)):
            if name.endswith(".json") and not name.startswith("."):
                try:
                    with open(os.path.join(self.leases, name), encoding="utf-8") as handle:
                        out.append(json.load(handle))
                except (OSError, ValueError):
                    out.append({"lease_id": name[:-5], "state": "unreadable"})
        return out

    def exists(self, lease_id):
        return os.path.exists(os.path.join(self.leases, lease_id + ".json"))

    def ledger(self, event, **fields):
        """Append-only guest book. Callers pass only IDs, states and sanitized codes."""
        line = json.dumps({"ts": iso(self.clock()), "event": event, "host": self.host_id, **fields}, sort_keys=True)
        fd = os.open(os.path.join(self.root, "guestbook.jsonl"),
                     os.O_WRONLY | os.O_APPEND | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            os.write(fd, (line + "\n").encode())
        finally:
            os.close(fd)

    def key_path(self, lease_id):
        return os.path.join(self.keys, lease_id)

    @contextlib.contextmanager
    def key_dir_fd(self, lease_id):
        """A verified descriptor for the lease's key directory (created root-owned if absent). All key files
        are created relative to it, so swapping the path later cannot redirect a write."""
        if not LEASE_ID_RE.fullmatch(lease_id or ""):
            raise Usage("bad cabin id", "motel cabin ls")
        os.makedirs(os.path.dirname(self.keys), mode=0o750, exist_ok=True)
        top = self._dir_fd(self.keys)
        try:
            fd = self._dir_fd(lease_id, top)
        finally:
            os.close(top)
        try:
            yield fd
        finally:
            os.close(fd)

    @staticmethod
    def write_key(dir_fd, name, text, mode=KEY_FILE_MODE):
        """Create a new key file relative to ``dir_fd``: exclusive, no symlink following, exact mode."""
        if "/" in name or name in (".", ".."):
            raise Usage("bad key file name")
        try:
            fd = os.open(name, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode,
                         dir_fd=dir_fd)
        except FileExistsError:
            raise Guarded(f"key file {name} already exists; refusing to overwrite or follow it") from None
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                os.fchmod(handle.fileno(), mode)
                handle.write(text)
                handle.flush()
                os.fsync(handle.fileno())
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(name, dir_fd=dir_fd)
            raise

    def remove_keys(self, lease_id):
        """Delete a lease's key directory relative to a verified descriptor for ``keys``. A key tree with a bad
        layout is left alone (the keys are only hygiene; the server and firewall deletions never wait for it)."""
        if not LEASE_ID_RE.fullmatch(lease_id or ""):
            return
        try:
            top = self._dir_fd(self.keys, create=False)
        except (Guarded, OSError):
            return
        try:
            shutil.rmtree(lease_id, ignore_errors=True, dir_fd=top)
        finally:
            os.close(top)

    def clean_scratch(self, max_age_s=3600):
        """Remove key-generation scratch directories left behind by a crash (older than ``max_age_s``)."""
        now = time.time()
        try:
            names = os.listdir(self.tmp)
        except OSError:
            return
        for name in names:
            path = os.path.join(self.tmp, name)
            try:
                st = os.lstat(path)
                if name.startswith("keygen-") and stat.S_ISDIR(st.st_mode) and now - st.st_mtime > max_age_s:
                    shutil.rmtree(path, ignore_errors=True)
            except OSError:
                continue

    def scratch(self):
        """A fresh root-only directory for key generation (ssh-keygen output never touches a shared place)."""
        self.clean_scratch()
        path = tempfile.mkdtemp(dir=self.tmp, prefix="keygen-")
        os.close(self._dir_fd(path, create=False))          # fstat-verified: ours, not writable by others
        return path

    def key_access_problems(self, gid=None):
        """Lease key directories whose layout would stop the unprivileged ssh client or let it write.

        ``gid`` is the system user's primary group (the group the tree must belong to); without it the group of
        ``keys`` itself is the reference. Keys written before the system user and the setgid layout existed are
        root-only and show up here; the guest then reads as unknown (TTL only) until the cabin is checked out.
        Entries that vanish while we look (a concurrent checkout) are skipped."""
        problems = []
        try:
            want = os.stat(self.keys).st_gid if gid is None else gid
            names = sorted(os.listdir(self.keys))
        except OSError:
            return ["keys directory unreadable"]
        for name in names:
            path = os.path.join(self.keys, name)
            try:
                st = os.lstat(path)
                if not stat.S_ISDIR(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o027 \
                        or st.st_gid != want:
                    problems.append(name)
                    continue
                entries = os.listdir(path)
            except FileNotFoundError:
                continue
            except OSError:
                problems.append(name)
                continue
            for entry in entries:
                try:
                    fst = os.lstat(os.path.join(path, entry))
                except FileNotFoundError:
                    continue
                except OSError:
                    problems.append(f"{name}/{entry}")
                    continue
                if (not stat.S_ISREG(fst.st_mode) or fst.st_uid != os.geteuid() or fst.st_mode & 0o037
                        or not fst.st_mode & 0o040 or fst.st_gid != want):
                    problems.append(f"{name}/{entry}")
        return problems

    # -- short-lived provider read cache, so unprivileged callers cannot exhaust the reaper's API budget
    def cache_get(self, name, ttl_s):
        try:
            with open(os.path.join(self.cache, name + ".json"), encoding="utf-8") as handle:
                doc = json.load(handle)
            at = parse_iso(doc["at"])
            if at is not None and 0 <= (self.clock() - at).total_seconds() < ttl_s:
                return doc["value"]
        except (OSError, ValueError, KeyError, TypeError):
            pass
        return None

    def cache_put(self, name, value):
        atomic_write(os.path.join(self.cache, name + ".json"), json.dumps({"at": iso(self.clock()), "value": value}))

    def cache_drop(self):
        for name in os.listdir(self.cache):
            if name.endswith(".json"):
                with contextlib.suppress(OSError):
                    os.unlink(os.path.join(self.cache, name))


def check_key_layout(keys, gid=None):
    """None when the key tree is usable, else a short reason. With ``gid`` (the system user's primary group)
    the installed layout is required: ``keys`` root:<gid> 2750 and its parent (the user's home) root-owned,
    group <gid>, traversable by the group. ``setpriv --clear-groups --regid <gid>`` means the tree's group has
    to be the primary group. Never raises and never changes anything."""
    try:
        st = os.lstat(keys)
    except FileNotFoundError:
        return "the keys directory is missing"
    except OSError:
        return "the keys directory is unreadable"
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISDIR(st.st_mode):
        return "the keys directory is a symlink or not a directory"
    if st.st_uid != os.geteuid():
        return "the keys directory is not owned by root"
    if st.st_mode & 0o022:
        return "the keys directory is writable by its group or others"
    if gid is None:
        return None
    if st.st_gid != gid or stat.S_IMODE(st.st_mode) != 0o2750:
        return f"the keys directory must be root:{gid} mode 2750 (the system user's primary group)"
    try:
        home = os.lstat(os.path.dirname(keys))
    except OSError:
        return "the system user's home is unreadable"
    if (stat.S_ISLNK(home.st_mode) or not stat.S_ISDIR(home.st_mode) or home.st_uid != os.geteuid()
            or home.st_gid != gid or home.st_mode & 0o050 != 0o050 or home.st_mode & 0o022):
        return f"the system user's home must be root:{gid}, group-traversable and not writable (mode 0750)"
    return None
