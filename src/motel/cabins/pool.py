"""Opt-in, per-user warm cabin pooling (client-side only).

Reuses the caller's own active cabins within their already-paid hour without touching ``motel-desk``, sudoers,
``cabins/controller.py`` or ``tours``. Pool state and per-cabin ``fcntl.flock`` lockfiles live strictly under the
calling user's state directory (``~/.local/state/motel/pools/<name>/``, dirs ``0700``, files ``0600``).

Default ``motel cabin run`` and ``motel tour`` never use pools and always create a fresh server per job.
"""

from __future__ import annotations

import errno
import fcntl
import json
import os
import re
import secrets
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta
from types import SimpleNamespace

from motel.cabins.config import CONFIG_FILE, load_config
from motel.cabins.errors import (
    EXIT_ERROR,
    EXIT_INCOMPLETE,
    EXIT_NOT_FOUND,
    EXIT_OK,
    ConfigError,
    Prerequisite,
    Usage,
)
from motel.cabins.util import LEASE_ID_RE, SCHEMA, iso, parse_duration, parse_iso, parse_size_gb, utcnow

POOL_SCHEMA = "motel-pool/v1"
POOL_NAME_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
DEFAULT_POOL_TTL = "1h"
MIN_REMAINING = timedelta(minutes=8)


def validate_pool_name(name: str | None) -> str:
    """Validate a pool name against ``^[a-z0-9][a-z0-9-]{0,31}$``."""
    if not isinstance(name, str) or not POOL_NAME_RE.fullmatch(name):
        raise Usage(f"bad pool name {name!r}: use 1-32 lowercase letters, digits or hyphens",
                    "motel cabin pool ls")
    return name


def pool_task_name(name: str) -> str:
    """Derive a valid cabin ``--task`` name (``pool-<name>``, capped to 32 chars and ending in alnum)."""
    validate_pool_name(name)
    return f"pool-{name}"[:32].rstrip("-")


def pools_root(cabins_root: str | None = None) -> str:
    """Per-user pool state directory: ``~/.local/state/motel/pools`` (or sibling of an explicit ``cabins_root``)."""
    if cabins_root is not None:
        return os.path.join(os.path.dirname(os.path.normpath(cabins_root)), "pools")
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "motel", "pools")


def _ensure_dir(path: str) -> None:
    os.makedirs(path, mode=0o700, exist_ok=True)
    os.chmod(path, 0o700)


def _open_lockfile(path: str) -> int:
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        os.fchmod(fd, 0o600)
    except OSError:
        os.close(fd)
        raise
    return fd


def _parse_aware_iso(value) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = parse_iso(value)
    except ValueError:
        return None
    return dt if dt is not None and dt.tzinfo is not None else None


@dataclass(frozen=True)
class PoolSpec:
    """Normalized cabin shape requested by a pooled run."""

    server_type: str | None     # resolved type (explicit or from config), or None when default & config unreadable
    explicit_type: bool
    image: str | None           # requested baked image name or numeric id, or None for stock system image
    stock_image: str | None     # default system image from config (e.g. "ubuntu-24.04"), or None if unreadable
    disk_gb: int | None


def _read_config_defaults(config_path: str | None) -> tuple[str | None, str | None]:
    """Read default ``(server_type, stock_image)`` from ``[cabins]`` in ``motel.toml`` if readable."""
    if not config_path:
        return None, None
    try:
        cfg = load_config(config_path, require_enabled=False)
    except (Prerequisite, ConfigError, OSError):
        return None, None
    default_type = cfg.types[0] if cfg.types else None
    return default_type, cfg.image


def resolve_spec(args, *, config_path: str | None = CONFIG_FILE) -> PoolSpec:
    """Validate pool-run launch options and resolve the exact ``(server_type, image, disk)`` tuple."""
    disk_gb = parse_size_gb(args.disk) if args.disk else None
    if args.ttl:
        parse_duration(args.ttl, what="--for")
    if args.image is not None:
        from motel.tours.bake import parse_image_spec
        parse_image_spec(args.image)
    cfg_type, cfg_image = _read_config_defaults(config_path)
    server_type = args.server_type if args.server_type is not None else cfg_type
    return PoolSpec(
        server_type=server_type,
        explicit_type=args.server_type is not None,
        image=args.image,
        stock_image=cfg_image,
        disk_gb=disk_gb,
    )


def entry_from_view(view: dict, *, default_type: bool, clock=utcnow) -> dict:
    """Extract pool index metadata from a cabin view returned by ``checkin`` or ``status``."""
    offer = view.get("offer") or {}
    disk = view.get("disk") or {}
    return {
        "lease_id": view["lease_id"],
        "server_type": offer.get("server_type"),
        "default_type": bool(default_type),
        "image_type": offer.get("image_type"),
        "image_name": offer.get("image_name"),
        "image_id": offer.get("image_id"),
        "disk_gb": disk.get("size_gb") if isinstance(disk, dict) else None,
        "added_at": iso(clock()),
    }


def _image_matches(spec: PoolSpec, image_type, image_name, image_id) -> bool:
    if spec.image is None:
        if image_type == "snapshot":
            return False
        if spec.stock_image is not None and image_name is not None:
            return image_name == spec.stock_image
        return True
    if image_type != "snapshot":
        return False
    if spec.image.isdigit():
        return image_id is not None and str(image_id) == spec.image
    return image_name == spec.image


def matches_entry(spec: PoolSpec, entry: dict) -> bool:
    """Check whether a pool index entry matches ``spec``."""
    if not isinstance(entry, dict):
        return False
    if entry.get("disk_gb") != spec.disk_gb:
        return False
    if spec.server_type is not None:
        if entry.get("server_type") != spec.server_type:
            return False
    elif not entry.get("default_type"):
        return False
    return _image_matches(spec, entry.get("image_type"), entry.get("image_name"), entry.get("image_id"))


def matches_view(spec: PoolSpec, view: dict, *, entry: dict | None = None) -> bool:
    """Re-verify exact ``(server_type, image, disk)`` against a live cabin view from ``motel-desk``."""
    if not isinstance(view, dict):
        return False
    disk = view.get("disk")
    view_disk = disk.get("size_gb") if isinstance(disk, dict) else None
    if view_disk != spec.disk_gb:
        return False
    offer = view.get("offer") or {}
    view_type = offer.get("server_type")
    if spec.server_type is not None:
        if view_type != spec.server_type:
            return False
    else:
        if not (entry and entry.get("default_type") and view_type and view_type == entry.get("server_type")):
            return False
    return _image_matches(spec, offer.get("image_type"), offer.get("image_name"), offer.get("image_id"))


def evaluate_live(view: dict, now: datetime) -> tuple[bool, str, bool]:
    """Return ``(usable, reason, should_prune)`` for a live cabin status view."""
    state = view.get("state")
    if state in ("released", "cleanup_pending"):
        return False, f"state={state}", True
    if state != "active":
        return False, f"state={state}", False
    if view.get("sealed") or (view.get("admission") or {}).get("sealed_at"):
        return False, "sealed", True
    if view.get("server_present") is False:
        return False, "server_absent", True
    expires = _parse_aware_iso(view.get("expires_at"))
    if expires is None:
        return False, "missing_expiry", False
    rw = (view.get("billing") or {}).get("release_window") or {}
    rw_opens = _parse_aware_iso(rw.get("opens"))
    cutoff = min(expires, rw_opens) if rw_opens is not None else expires
    if cutoff - now < MIN_REMAINING:
        return False, "expiring", True
    return True, "ok", False


class CabinLock:
    """Non-blocking per-cabin ``fcntl.flock`` lockfile handle."""

    def __init__(self, fd: int, path: str):
        self._fd: int | None = fd
        self.path = path

    def release(self, *, unlink: bool = False) -> None:
        fd = self._fd
        if fd is None:
            return
        self._fd = None
        try:
            if unlink:
                try:
                    os.unlink(self.path)
                except FileNotFoundError:
                    pass
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError:
                pass
        finally:
            os.close(fd)


class PoolStore:
    """Per-user pool directory, JSON index and per-cabin ``fcntl.flock`` lockfiles."""

    def __init__(self, root: str):
        self.root = root

    def pool_dir(self, name: str) -> str:
        validate_pool_name(name)
        return os.path.join(self.root, name)

    def _ensure_pool_dir(self, name: str) -> str:
        _ensure_dir(self.root)
        pdir = self.pool_dir(name)
        _ensure_dir(pdir)
        return pdir

    def list_pools(self) -> list[str]:
        if not os.path.isdir(self.root):
            return []
        out = []
        try:
            with os.scandir(self.root) as entries:
                for entry in entries:
                    if entry.is_dir(follow_symlinks=False) and POOL_NAME_RE.fullmatch(entry.name):
                        out.append(entry.name)
        except OSError:
            return []
        return sorted(out)

    @contextmanager
    def _index_lock(self, name: str):
        pdir = self._ensure_pool_dir(name)
        fd = _open_lockfile(os.path.join(pdir, "index.lock"))
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield pdir
        finally:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)

    @staticmethod
    def _read_index_unlocked(pdir: str, name: str) -> dict:
        path = os.path.join(pdir, "index.json")
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
        except (OSError, ValueError):
            return {"schema": POOL_SCHEMA, "name": name, "cabins": {}}
        raw = doc.get("cabins") if isinstance(doc, dict) else None
        if not isinstance(raw, dict):
            return {"schema": POOL_SCHEMA, "name": name, "cabins": {}}
        cabins = {k: v for k, v in raw.items() if LEASE_ID_RE.fullmatch(str(k)) and isinstance(v, dict)}
        return {"schema": POOL_SCHEMA, "name": name, "cabins": cabins}

    @staticmethod
    def _write_index_unlocked(pdir: str, doc: dict) -> None:
        index_path = os.path.join(pdir, "index.json")
        tmp_path = os.path.join(pdir, f".index-{secrets.token_hex(6)}.tmp")
        data = (json.dumps(doc, indent=1, sort_keys=True) + "\n").encode("utf-8")
        fd = os.open(tmp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.fchmod(fd, 0o600)
            os.write(fd, data)
            os.fsync(fd)
        except BaseException:
            os.close(fd)
            try:
                os.unlink(tmp_path)
            except OSError:
                pass
            raise
        os.close(fd)
        os.replace(tmp_path, index_path)

    def load_entries(self, name: str) -> list[dict]:
        """Return pool entries in deterministic order (oldest added first, then lease id)."""
        pdir = self.pool_dir(name)
        if not os.path.isdir(pdir):
            return []
        with self._index_lock(name) as locked_dir:
            doc = self._read_index_unlocked(locked_dir, name)
            entries = list(doc["cabins"].values())
        entries.sort(key=lambda e: (str(e.get("added_at") or ""), str(e.get("lease_id") or "")))
        return entries

    def add_entry(self, name: str, entry: dict) -> None:
        lease_id = entry["lease_id"]
        if not LEASE_ID_RE.fullmatch(lease_id):
            raise Usage(f"bad cabin id {lease_id!r}", "motel cabin ls")
        with self._index_lock(name) as pdir:
            doc = self._read_index_unlocked(pdir, name)
            doc["cabins"][lease_id] = entry
            self._write_index_unlocked(pdir, doc)

    def remove_entry(self, name: str, lease_id: str) -> bool:
        pdir = self.pool_dir(name)
        if not os.path.isdir(pdir):
            return False
        with self._index_lock(name) as locked_dir:
            doc = self._read_index_unlocked(locked_dir, name)
            removed = doc["cabins"].pop(lease_id, None) is not None
            if removed:
                self._write_index_unlocked(locked_dir, doc)
            return removed

    def lock_path(self, name: str, lease_id: str) -> str:
        if not LEASE_ID_RE.fullmatch(lease_id or ""):
            raise Usage(f"bad cabin id {lease_id!r}", "motel cabin ls")
        return os.path.join(self.pool_dir(name), f"{lease_id}.lock")

    def try_lock_cabin(self, name: str, lease_id: str) -> CabinLock | None:
        """Acquire a non-blocking exclusive ``flock`` on ``<pool>/<lease_id>.lock``, or return None if busy."""
        self._ensure_pool_dir(name)
        path = self.lock_path(name, lease_id)
        fd = _open_lockfile(path)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            os.close(fd)
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                return None
            raise
        try:
            st_fd = os.fstat(fd)
            st_path = os.stat(path)
        except FileNotFoundError:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            return None
        if st_fd.st_ino != st_path.st_ino or st_fd.st_dev != st_path.st_dev:
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            finally:
                os.close(fd)
            return None
        return CabinLock(fd, path)

    def is_cabin_locked(self, name: str, lease_id: str) -> bool:
        """Non-blocking probe: True when another process currently holds the cabin's lockfile."""
        path = self.lock_path(name, lease_id)
        try:
            fd = os.open(path, os.O_RDWR | os.O_NOFOLLOW)
        except FileNotFoundError:
            return False
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EACCES):
                return True
            raise
        else:
            fcntl.flock(fd, fcntl.LOCK_UN)
            return False
        finally:
            os.close(fd)


def _has_access_files(cli, lease_id: str) -> bool:
    cdir = cli.cabin_dir(lease_id)
    return os.path.isfile(os.path.join(cdir, "ssh_config")) and os.path.isfile(os.path.join(cdir, "id_ed25519"))


def _prune_locked(store: PoolStore, pool_name: str, lease_id: str, lock: CabinLock) -> None:
    store.remove_entry(pool_name, lease_id)
    lock.release(unlink=True)


def run_pooled(cli, args) -> int:
    """Execute ``motel cabin run --pool NAME ... -- CMD...``."""
    pool_name = validate_pool_name(args.pool)
    if args.cabin or args.keep:
        raise Usage("--pool cannot be combined with --cabin or --keep", "motel cabin run --pool NAME -- CMD")
    if args.task:
        raise Usage("--pool sets the cabin task name automatically; omit --task", "motel cabin run --pool NAME -- CMD")
    spec = resolve_spec(args, config_path=cli.config_path)
    store = PoolStore(cli.pools_root)
    want_json = bool(getattr(args, "json", False))
    started = cli.monotonic()

    selected_view = None
    lock: CabinLock | None = None
    pool_hit = False

    for entry in store.load_entries(pool_name):
        lease_id = entry.get("lease_id")
        if not lease_id or not LEASE_ID_RE.fullmatch(lease_id):
            continue
        if not matches_entry(spec, entry):
            continue
        candidate_lock = store.try_lock_cabin(pool_name, lease_id)
        if candidate_lock is None:
            continue
        if not _has_access_files(cli, lease_id):
            _prune_locked(store, pool_name, lease_id, candidate_lock)
            continue
        rc, doc = cli.desk_call(["status", lease_id])
        if rc == EXIT_NOT_FOUND or (isinstance(doc, dict) and doc.get("code") == EXIT_NOT_FOUND):
            _prune_locked(store, pool_name, lease_id, candidate_lock)
            continue
        view = doc.get("cabin") if (rc == EXIT_OK and isinstance(doc, dict)) else None
        if not isinstance(view, dict):
            candidate_lock.release()
            continue
        usable, _reason, should_prune = evaluate_live(view, cli.clock())
        if not usable:
            if should_prune:
                _prune_locked(store, pool_name, lease_id, candidate_lock)
            else:
                candidate_lock.release()
            continue
        if not matches_view(spec, view, entry=entry):
            candidate_lock.release()
            continue
        selected_view = view
        lock = candidate_lock
        pool_hit = True
        break

    try:
        if selected_view is None:
            checkin_args = SimpleNamespace(
                server_type=args.server_type,
                ttl=args.ttl or DEFAULT_POOL_TTL,
                task=pool_task_name(pool_name),
                disk=args.disk,
                image=args.image,
            )
            rc, selected_view = cli.checkin(checkin_args, want_json)
            if selected_view is None:
                return rc
            cabin = selected_view["lease_id"]
            lock = store.try_lock_cabin(pool_name, cabin)
            if lock is None:
                # Fresh lease_id could only be locked by a bug; fail closed without registering.
                raise RuntimeError(f"could not lock freshly created cabin {cabin}")
            store.add_entry(pool_name, entry_from_view(selected_view, default_type=not spec.explicit_type, clock=cli.clock))
            offer = selected_view.get("offer") or {}
            img_label = offer.get("image_name") if offer.get("image_type") == "snapshot" else None
            image_str = f" · image {img_label}" if img_label else ""
            cli.say(f"pool {pool_name} cold miss -> cabin {cabin} · EUR {offer.get('total_hourly_net_eur')}/h net"
                    f"{image_str} · ready in {int(cli.monotonic() - started)}s")
        else:
            cabin = selected_view["lease_id"]
            offer = selected_view.get("offer") or {}
            img_label = offer.get("image_name") if offer.get("image_type") == "snapshot" else None
            image_str = f" · image {img_label}" if img_label else ""
            cli.say(f"pool {pool_name} warm hit {cabin} · {offer.get('server_type')}{image_str}")

        sync_rc = cli.sync(cabin, selected_view, delete=True)
        if sync_rc != 0:
            cli.say(f"syncing the worktree failed (rsync exit {sync_rc}). Try: motel cabin ssh {cabin}")
            result = sync_rc
        else:
            result = cli.remote_run(cabin, selected_view, args.command)
    finally:
        if lock is not None:
            lock.release()

    cli.say(f"pool {pool_name} kept {cabin} warm (motel cabin pool drain {pool_name} to release)")
    cli.result_line(args, cabin, result, kept=True, offer=selected_view.get("offer"),
                    pool=pool_name, pool_hit=pool_hit)
    return result


def _render_pool_ls(rows: list[dict], pool_name: str | None) -> str:
    if not rows:
        return f"no pooled cabins in pool {pool_name}" if pool_name else "no pooled cabins"
    lines = []
    for row in rows:
        state = f"{row['state']} (sealed)" if row.get("sealed") else str(row.get("state") or "unknown")
        img = row.get("image") or "-"
        disk = f" disk {row['disk_gb']}G" if row.get("disk_gb") else ""
        rw = row.get("release_window") or {}
        if row.get("paid_until"):
            timing = f"paid_until {row['paid_until']}"
            if rw.get("opens") and rw.get("closes"):
                timing += f" release_window {rw['opens']}..{rw['closes']}"
        elif row.get("expires_at"):
            timing = f"expires {row['expires_at']}"
        else:
            timing = ""
        lines.append(
            f"{row['pool']:<16} {row['lease_id']:<44} {state:<18} {row['lock']:<5} "
            f"{str(row.get('server_type') or '-'):<10} {img:<16}{disk}"
            + (f"  {timing}" if timing else "")
        )
    return "\n".join(lines)


def pool_ls(cli, pool_name: str | None, want_json: bool) -> int:
    """List pooled cabins with live status and lock state, pruning released entries."""
    if pool_name is not None:
        validate_pool_name(pool_name)
    store = PoolStore(cli.pools_root)
    names = [pool_name] if pool_name is not None else store.list_pools()

    rows: list[dict] = []
    pruned: list[str] = []
    for name in names:
        for entry in store.load_entries(name):
            lease_id = entry.get("lease_id")
            if not lease_id or not LEASE_ID_RE.fullmatch(lease_id):
                continue
            busy = store.is_cabin_locked(name, lease_id)
            rc, doc = cli.desk_call(["status", lease_id])
            if rc == EXIT_NOT_FOUND or (isinstance(doc, dict) and doc.get("code") == EXIT_NOT_FOUND):
                if not busy:
                    lock = store.try_lock_cabin(name, lease_id)
                    if lock is not None:
                        _prune_locked(store, name, lease_id, lock)
                        pruned.append(lease_id)
                continue
            if rc != EXIT_OK or not isinstance(doc, dict) or doc.get("ok") is False or not isinstance(doc.get("cabin"), dict):
                return cli.fail(rc, doc if isinstance(doc, dict) else {"ok": False, "error": "status failed"}, want_json)
            view = doc["cabin"]
            if view.get("state") == "released":
                if not busy:
                    lock = store.try_lock_cabin(name, lease_id)
                    if lock is not None:
                        _prune_locked(store, name, lease_id, lock)
                        pruned.append(lease_id)
                continue
            offer = view.get("offer") or {}
            billing_info = view.get("billing") or {}
            disk = view.get("disk") or {}
            rows.append({
                "pool": name,
                "lease_id": lease_id,
                "state": view.get("state"),
                "sealed": bool(view.get("sealed")),
                "lock": "busy" if busy else "idle",
                "server_type": offer.get("server_type") or entry.get("server_type"),
                "image": offer.get("image_name") or entry.get("image_name"),
                "image_type": offer.get("image_type") or entry.get("image_type"),
                "disk_gb": disk.get("size_gb") if isinstance(disk, dict) else entry.get("disk_gb"),
                "expires_at": view.get("expires_at"),
                "paid_until": billing_info.get("paid_until"),
                "release_window": billing_info.get("release_window"),
            })

    out_doc = {"schema": SCHEMA, "ok": True, "pool": pool_name, "cabins": rows, "pruned": pruned}
    if want_json:
        print(json.dumps(out_doc, indent=1, sort_keys=True), file=cli.out)
    else:
        print(_render_pool_ls(rows, pool_name), file=cli.out)
    return EXIT_OK


def _render_pool_drain(doc: dict, pool_name: str | None) -> str:
    if not any((doc["drained"], doc["skipped"], doc["pruned"], doc["errors"])):
        return f"no pooled cabins to drain in pool {pool_name}" if pool_name else "no pooled cabins to drain"
    lines = []
    for item in doc["drained"]:
        lines.append(f"drained {item['pool']} {item['lease_id']} ({item['state']})")
    for item in doc["pruned"]:
        lines.append(f"pruned {item['pool']} {item['lease_id']} ({item['reason']})")
    for item in doc["skipped"]:
        lines.append(f"skipped {item['pool']} {item['lease_id']} ({item['reason']}; still running)")
    for item in doc["errors"]:
        lines.append(f"error {item['pool']} {item['lease_id']}: {item['error']}")
    return "\n".join(lines)


def pool_drain(cli, pool_name: str | None, want_json: bool) -> int:
    """Check out every idle (unlocked) cabin in the named pool (or all pools), skipping busy cabins."""
    if pool_name is not None:
        validate_pool_name(pool_name)
    store = PoolStore(cli.pools_root)
    names = [pool_name] if pool_name is not None else store.list_pools()

    drained: list[dict] = []
    skipped: list[dict] = []
    pruned: list[dict] = []
    errors: list[dict] = []
    exit_code = EXIT_OK

    for name in names:
        for entry in store.load_entries(name):
            lease_id = entry.get("lease_id")
            if not lease_id or not LEASE_ID_RE.fullmatch(lease_id):
                continue
            lock = store.try_lock_cabin(name, lease_id)
            if lock is None:
                skipped.append({"pool": name, "lease_id": lease_id, "reason": "busy"})
                cli.say(f"pool {name} skipping busy cabin {lease_id}")
                continue
            try:
                rc, doc = cli.checkout_with_retry(lease_id)
                done = (doc.get("cabin") or {}) if isinstance(doc, dict) else {}
                if rc == EXIT_OK and done.get("state") == "released":
                    cli.cleanup_keys(lease_id)
                    cli.show_receipt(lease_id, done)
                    _prune_locked(store, name, lease_id, lock)
                    lock = None
                    drained.append({
                        "pool": name,
                        "lease_id": lease_id,
                        "state": "released",
                        "receipt": cli.receipt_of(done),
                    })
                elif rc == EXIT_NOT_FOUND or (isinstance(doc, dict) and doc.get("code") == EXIT_NOT_FOUND):
                    _prune_locked(store, name, lease_id, lock)
                    lock = None
                    pruned.append({"pool": name, "lease_id": lease_id, "reason": "not_found"})
                elif rc == EXIT_INCOMPLETE and done.get("state") == "cleanup_pending":
                    cli.say(f"WARNING: checkout of {lease_id} is NOT complete (cleanup pending). "
                            f"Try: motel cabin status {lease_id}")
                    cli.show_receipt(lease_id, done)
                    _prune_locked(store, name, lease_id, lock)
                    lock = None
                    drained.append({
                        "pool": name,
                        "lease_id": lease_id,
                        "state": "cleanup_pending",
                        "receipt": cli.receipt_of(done),
                    })
                    if exit_code == EXIT_OK:
                        exit_code = EXIT_INCOMPLETE
                else:
                    msg = (doc or {}).get("error", "failed") if isinstance(doc, dict) else "failed"
                    cli.say(f"pool {name} checkout of {lease_id} FAILED ({msg}; exit {rc}). "
                            f"Try: motel cabin checkout {lease_id}")
                    errors.append({"pool": name, "lease_id": lease_id, "code": rc or EXIT_ERROR, "error": msg})
                    if exit_code in (EXIT_OK, EXIT_INCOMPLETE):
                        exit_code = rc or EXIT_ERROR
            finally:
                if lock is not None:
                    lock.release()

    out_doc = {
        "schema": SCHEMA,
        "ok": exit_code == EXIT_OK,
        "pool": pool_name,
        "drained": drained,
        "skipped": skipped,
        "pruned": pruned,
        "errors": errors,
    }
    if want_json:
        print(json.dumps(out_doc, indent=1, sort_keys=True), file=cli.out)
    else:
        print(_render_pool_drain(out_doc, pool_name), file=cli.out)
    return exit_code


__all__ = [
    "DEFAULT_POOL_TTL",
    "MIN_REMAINING",
    "POOL_NAME_RE",
    "POOL_SCHEMA",
    "CabinLock",
    "PoolSpec",
    "PoolStore",
    "entry_from_view",
    "evaluate_live",
    "matches_entry",
    "matches_view",
    "pool_drain",
    "pool_ls",
    "pool_task_name",
    "pools_root",
    "resolve_spec",
    "run_pooled",
    "validate_pool_name",
]
