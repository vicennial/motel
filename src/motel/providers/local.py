"""Local provider: "volumes" are sparse loop files under one directory. No cloud account needed.

Good for trying motel, for demos and for CI on a Linux box where you have root. Each volume is a file
``l<N>.img`` plus ``l<N>.json`` (labels, size, attached-to) in the provider directory. The directory must be
root-owned and private; the provider refuses to use it otherwise when it runs on the real filesystem.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
from datetime import UTC, datetime
from decimal import Decimal

from motel.errors import MotelError, NotFound, ProviderError
from motel.providers.base import HostIdentity, Inventory, Volume

GIB = 1024 ** 3
ID_RE = re.compile(r"^l[0-9]{1,9}$")
SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LC_ALL": "C"}


def _tool(name: str) -> str:
    for directory in ("/usr/sbin", "/sbin", "/usr/bin", "/bin"):
        if os.path.exists(f"{directory}/{name}"):
            return f"{directory}/{name}"
    return f"/usr/sbin/{name}"


def _run(argv: list, timeout: float = 300.0) -> str:
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout, stdin=subprocess.DEVNULL,
                              env=SAFE_ENV, shell=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ProviderError(f"{argv[0]}: {exc}") from exc
    if proc.returncode != 0:
        raise ProviderError(f"{' '.join(argv[:2])} failed (exit {proc.returncode}): {proc.stderr.strip()[:200]}")
    return proc.stdout


class LocalProvider:
    name = "local"
    currency = "EUR"
    formats_on_create = True
    gb_bytes = 2 ** 30

    def __init__(self, directory: str, *, runner=_run, server_id: str = "local", location: str = "local",
                 price: str = "0", strict: bool = False):
        self.dir, self.run = directory, runner
        self.server_id, self.location, self.price = server_id, location, Decimal(price)
        os.makedirs(directory, mode=0o700, exist_ok=True)
        if strict:
            st = os.stat(directory)
            if st.st_uid != 0 or st.st_mode & 0o077:
                raise ProviderError(f"{directory} must be root-owned and 0700")

    # -- storage of metadata
    def _meta_path(self, vol_id: str) -> str:
        if not ID_RE.fullmatch(str(vol_id)):
            raise ProviderError(f"refusing odd volume id {vol_id!r}")
        return os.path.join(self.dir, f"{vol_id}.json")

    def _img_path(self, vol_id: str) -> str:
        self._meta_path(vol_id)
        return os.path.join(self.dir, f"{vol_id}.img")

    def _load(self, vol_id: str) -> dict:
        try:
            with open(self._meta_path(vol_id), encoding="utf-8") as handle:
                return json.load(handle)
        except FileNotFoundError:
            raise NotFound(f"no volume {vol_id}") from None
        except (OSError, ValueError) as exc:
            raise ProviderError(f"unreadable volume record {vol_id}: {exc}") from exc

    def _save(self, meta: dict) -> None:
        path = self._meta_path(meta["id"])
        tmp = f"{path}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(meta, handle, sort_keys=True)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)

    @staticmethod
    def _volume(meta: dict) -> Volume:
        return Volume(id=meta["id"], name=meta["name"], size_gb=meta["size_gb"], server_id=meta.get("server_id"),
                      labels=dict(meta.get("labels") or {}), delete_protected=bool(meta.get("protected")),
                      created=datetime.fromisoformat(meta["created"]), location="local")

    def _all(self) -> list:
        out = []
        for name in sorted(os.listdir(self.dir)):
            if name.endswith(".json") and ID_RE.fullmatch(name[:-5]):
                out.append(self._load(name[:-5]))
        return out

    def _allocate_id(self) -> int:
        """Monotonic and persisted, so the id of a deleted volume is never handed out again."""
        counter = os.path.join(self.dir, ".next-id")
        try:
            with open(counter, encoding="utf-8") as handle:
                last = int(handle.read().strip() or 0)
        except (OSError, ValueError):
            last = 0
        last = max([last] + [int(m["id"][1:]) for m in self._all()]) + 1
        tmp = f"{counter}.tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(str(last))
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, counter)
        return last

    # -- interface
    def whoami(self) -> HostIdentity:
        return HostIdentity(self.server_id, self.location, "local")

    def visible(self) -> Inventory:
        return Inventory(volumes=tuple(self._volume(m) for m in self._all()), servers=(self.server_id,))

    def price_gb_month(self, location: str) -> Decimal:
        return self.price

    def get_volume(self, vol_id) -> Volume:
        return self._volume(self._load(str(vol_id)))

    def list_owned(self, labels: dict) -> list:
        return [self._volume(m) for m in self._all() if all((m.get("labels") or {}).get(k) == v for k, v in labels.items())]

    def create_volume(self, name, size_gb, location, labels, server_id=None) -> Volume:
        vol_id = f"l{self._allocate_id()}"
        img = self._img_path(vol_id)
        meta = {"id": vol_id, "name": name, "size_gb": int(size_gb), "labels": dict(labels), "server_id": server_id,
                "created": datetime.now(UTC).replace(microsecond=0).isoformat(), "protected": False, "loop": None}
        self._save(meta)  # record first, so a crash leaves something deletable
        try:
            fd = os.open(img, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
            os.close(fd)
            os.truncate(img, int(size_gb) * GIB)
            self.run([_tool("mkfs.ext4"), "-F", "-q", "-m", "0", img], 600)
        except (OSError, MotelError) as exc:  # the record exists: let housekeeping find and retire it
            raise ProviderError(f"volume {vol_id} was created but could not be formatted: {getattr(exc, 'message', exc)}",
                                indeterminate=True, volume_id=vol_id) from exc
        return self._volume(meta)

    def attach(self, vol_id, server_id) -> None:
        meta = self._load(str(vol_id))
        meta["server_id"] = str(server_id)
        self._save(meta)

    def _loop_of(self, meta: dict) -> str | None:
        out = self.run([_tool("losetup"), "-j", self._img_path(meta["id"])], 30).strip()
        return out.split(":", 1)[0] if out else None

    def detach(self, vol_id) -> None:
        meta = self._load(str(vol_id))
        loop = self._loop_of(meta)
        if loop:
            self.run([_tool("losetup"), "-d", loop], 30)
        meta.update(server_id=None, loop=None)
        self._save(meta)

    def resize(self, vol_id, size_gb) -> None:
        meta = self._load(str(vol_id))
        if int(size_gb) < meta["size_gb"]:
            raise ProviderError("volumes can only grow")
        os.truncate(self._img_path(vol_id), int(size_gb) * GIB)
        loop = self._loop_of(meta)
        if loop:
            self.run([_tool("losetup"), "-c", loop], 30)
        meta["size_gb"] = int(size_gb)
        self._save(meta)

    def delete(self, vol_id) -> None:
        meta = self._load(str(vol_id))
        if meta.get("server_id") is not None:
            raise ProviderError("refusing to delete an attached volume")
        if meta.get("protected"):
            raise ProviderError("delete protection is on")
        os.unlink(self._img_path(vol_id))
        os.unlink(self._meta_path(vol_id))

    def device_path(self, vol_id) -> str:
        meta = self._load(str(vol_id))
        loop = self._loop_of(meta)
        if not loop:
            loop = self.run([_tool("losetup"), "--find", "--show", self._img_path(vol_id)], 30).strip()
        return loop

    def expected_serial(self, vol_id):
        return None
