"""Test doubles: a fake clock and fake system operations that need no root and no devices."""

from __future__ import annotations

import os
import secrets
import time
from datetime import UTC, datetime, timedelta

from motel.concierge.errors import AdminRefused, AttemptNotFound, ConciergeError
from motel.concierge.registry import validate_issue
from motel.sysops import GIB


class FakeClock:
    def __init__(self, start: datetime | None = None):
        self.t = start or datetime(2026, 1, 1, 12, 0, tzinfo=UTC)

    def __call__(self) -> datetime:
        return self.t

    def advance(self, **kwargs) -> None:
        self.t += timedelta(**kwargs)


class FakeSysOps:
    """"Mounting" only records the call; mount points are ordinary directories under a temp dir."""

    def __init__(self):
        self.mounted: dict = {}
        self.busy: dict = {}  # path -> ["pid cmd", ...]
        self.calls: list = []
        self.chowns: list = []
        self.free_gb = 1000
        self.total_gb = None  # filesystem size; default is large enough for free_gb
        self.serial_override: dict = {}
        self.fstype_override: dict = {}
        self.nested_mounts: list = []  # mount points to report as existing (e.g. inside a room)
        self.other_device_mounts: dict = {}  # device -> extra mount points (bind mounts, other namespaces)
        self.abandoned = None  # pid of a stuck holder-scan child, if any
        self.signatures: dict = {}  # device -> blkid-style signature dict; absent = blank
        self.sizes: dict = {}  # device -> size in bytes (lsblk)
        self.labels: dict = {}  # device -> ext4 label
        self.stacked: dict = {}  # device -> devices stacked on it (holders)
        self.dirty: set = set()  # devices whose first or last megabytes are not zeros
        self.device_busy: dict = {}  # floor mount point -> holders found by device only (no usable path)

    def resolve_device(self, dev):
        return dev

    def wait_device(self, dev, timeout=90.0):
        pass

    def block_serial(self, dev):
        return self.serial_override.get(dev, os.path.basename(dev))

    def fstype(self, dev):
        return self.fstype_override.get(dev, "ext4")

    def mounted_source(self, mountpoint):
        return self.mounted.get(mountpoint)

    def is_mounted(self, path):
        return path in self.mounted

    def mount(self, dev, mountpoint):
        self.calls.append(("mount", dev, mountpoint))
        self.mounted[mountpoint] = dev

    def umount(self, mountpoint):
        self.calls.append(("umount", mountpoint))
        del self.mounted[mountpoint]

    def prepare_fs(self, dev, label):
        self.calls.append(("prepare_fs", dev, label))
        self.labels[dev] = label[:16]  # tune2fs -L

    def grow_fs(self, dev):
        self.calls.append(("grow_fs", dev))

    def probe(self, dev):
        return dict(self.signatures.get(dev, {}))

    def block_size(self, dev):
        return self.sizes.get(dev, 0)

    def block_holders(self, dev):
        return list(self.stacked.get(dev, []))

    def zero_edges(self, dev):
        if dev in self.dirty:
            from motel.errors import Guard
            raise Guard(f"{dev} holds data near byte 0; refusing to format it")

    def make_fs(self, dev, label):
        self.calls.append(("make_fs", dev, label))
        self.signatures[dev] = {"TYPE": "ext4", "LABEL": label}
        self.labels[dev] = label

    def fs_label(self, dev):
        return self.labels.get(dev, "")

    def rescan(self, dev, target_bytes=None, above=None):
        self.calls.append(("rescan", dev, target_bytes) if above is None else ("rescan", dev, target_bytes, above))

    def chown(self, path, uid, gid):
        self.chowns.append((path, uid, gid))

    def usage(self, path):
        return (self.total_gb or max(100, self.free_gb)) * GIB, self.free_gb * GIB

    def holders(self, path, floor_mp=None):
        prefix = path.rstrip("/") + "/"
        found = [h for p, hs in self.busy.items() if p == path.rstrip("/") or p.startswith(prefix) for h in hs]
        for mp, hs in self.device_busy.items():  # device-level evidence applies to every room on the floor
            if (floor_mp or path).rstrip("/") == mp.rstrip("/"):
                found += hs
        return found

    def mounts_under(self, path):
        base = path.rstrip("/")
        return sorted(m for m in self.nested_mounts if m == base or m.startswith(base + "/"))

    def abandoned_scan(self):
        return self.abandoned

    def device_mounts(self, dev):
        return sorted([mp for mp, d in self.mounted.items() if d == dev] + list(self.other_device_mounts.get(dev, [])))


FAKE_PORT_BASE = 17800  # the fake's attempt ports run FAKE_PORT_BASE + 1, + 2, ... in issue order


class FakeConcierge:
    """In-memory stand-in for :class:`motel.concierge.admin.ConciergeClient`: same methods, same validation, no server.

    ``issue`` returns a placeholder token and remembers the attempt; nothing is proxied. Tests drive spend with
    :meth:`spend` and can make the concierge "unreachable" with ``unavailable = True``. ``calls`` records every call as
    ``(method, kwargs)``. ``clock`` may return epoch seconds or a datetime (for example :class:`FakeClock`).
    """

    def __init__(self, *, bindings=("anthropic", "openai"), max_budget_usd: float | None = None, clock=time.time):
        self.known_bindings = frozenset(bindings)
        self.max_budget_usd = max_budget_usd
        self.clock = clock
        self.attempts: dict = {}
        self.calls: list = []
        self.unavailable = False
        self.lost_listeners: dict = {}
        self._serial = 0

    def _now(self) -> float:
        now = self.clock()
        return now.timestamp() if isinstance(now, datetime) else float(now)

    def _enter(self, name: str, **kwargs) -> None:
        self.calls.append((name, kwargs))
        if self.unavailable:
            raise ConciergeError("the concierge is not running (fake: unavailable = True)")

    def issue(self, *, tour: str, guest: str, attempt: str, bindings: list, expires_at: str, budget_usd: float,
              max_concurrent: int = 2) -> str:
        self._enter("issue", tour=tour, guest=guest, attempt=attempt, bindings=list(bindings), expires_at=expires_at,
                    budget_usd=budget_usd, max_concurrent=max_concurrent)
        expires = validate_issue(tour=tour, guest=guest, attempt=attempt, bindings=bindings, expires_at=expires_at,
                                 budget_usd=budget_usd, max_concurrent=max_concurrent, known_bindings=self.known_bindings,
                                 max_budget_usd=self.max_budget_usd, now=self._now())
        if attempt in self.attempts:
            raise AdminRefused(f"attempt {attempt} already has a token; an attempt is issued once", "exists")
        self._serial += 1
        token = f"mt_fake{self._serial:04d}_{secrets.token_hex(8)}"
        self.attempts[attempt] = {"token": token, "tour": tour, "guest": guest, "bindings": list(bindings), "expires": expires,
                                  "port": FAKE_PORT_BASE + self._serial,
                                  "budget_usd": float(budget_usd), "max_concurrent": max_concurrent, "revoked": False,
                                  "usd": 0.0, "input_tokens": 0, "output_tokens": 0, "requests": 0, "refused": 0}
        return token

    def revoke(self, *, attempt: str | None = None, tour: str | None = None) -> int:
        self._enter("revoke", attempt=attempt, tour=tour)
        if not attempt and not tour:
            raise AdminRefused("revoke needs an attempt or a tour", "invalid")
        count = 0
        for name, rec in self.attempts.items():
            if (attempt and name != attempt) or (tour and rec["tour"] != tour) or rec["revoked"]:
                continue
            rec["revoked"] = True
            count += 1
        return count

    def usage(self, *, attempt: str) -> dict:
        self._enter("usage", attempt=attempt)
        rec = self.attempts.get(attempt)
        if rec is None:
            raise AttemptNotFound(f"no attempt {attempt!r}")
        return {key: rec[key] for key in ("usd", "input_tokens", "output_tokens", "requests", "refused")}

    def endpoint(self, *, attempt: str) -> dict:
        self._enter("endpoint", attempt=attempt)
        rec = self.attempts.get(attempt)
        if rec is None:
            raise AttemptNotFound(f"no attempt {attempt!r}")
        if not self._live(rec):
            raise AdminRefused(f"attempt {attempt} has no open listener (it is revoked or expired, or no port was free)", "gone")
        return {"port": rec["port"]}

    def health(self) -> dict:
        self._enter("health")
        return {"status": "ok", "fake": True, "attempts": len(self.attempts),
                "live_tokens": sum(1 for r in self.attempts.values() if self._live(r)),
                "lost_listeners": dict(self.lost_listeners)}

    # -- test helpers (not part of the client interface) -------------------------------------------------------------

    def lose_listener(self, attempt: str, reason: str = "not_preserved") -> None:
        """What the real concierge does when a restart cannot re-open an attempt's port: revoke it and report it in health()."""
        self.attempts[attempt]["revoked"] = True
        self.lost_listeners[attempt] = reason

    def _live(self, rec: dict) -> bool:
        return not rec["revoked"] and self._now() < rec["expires"]

    def spend(self, attempt: str, usd: float, *, input_tokens: int = 0, output_tokens: int = 0, requests: int = 1,
              refused: int = 0) -> None:
        rec = self.attempts[attempt]
        rec["usd"] = round(rec["usd"] + usd, 6)
        rec["input_tokens"] += input_tokens
        rec["output_tokens"] += output_tokens
        rec["requests"] += requests
        rec["refused"] += refused

    def token_for(self, attempt: str) -> str:
        return self.attempts[attempt]["token"]

    def is_valid(self, token: str) -> bool:
        """Would the real concierge still accept this token (not revoked, not expired)?"""
        return any(rec["token"] == token and self._live(rec) for rec in self.attempts.values())
