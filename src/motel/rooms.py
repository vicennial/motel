"""Rooms: TTL-bound scratch directories on floors (shared cloud volumes) of this host.

Room life:   active --(checkout time)--> grace --(grace)--> lost & found --(lost_and_found)--> purged
             active|grace --checkout--> lost & found; lost & found --restore--> active
Floor life:  intent -> created -> active -> retiring -> deleted   (a floor is a provider volume)

Everything here runs inside the root-owned desk, under one global lock.
"""

from __future__ import annotations

import json
import os
import secrets
import stat
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal

from motel import billing
from motel.config import Config, Paths, Receipt
from motel.errors import Busy, Guard, InventoryIncomplete, MotelError, NotFound, ProviderError, Refused, SystemFault
from motel.fsutil import safe_rmtree
from motel.guard import check_owned
from motel.state import SCHEMA, Store, atomic_write, safe_write_in_dir
from motel.sysops import GIB, SysOps, format_blank_device, fs_label_for, grow_target_bytes, verify_device, volume_bytes
from motel.units import ROOM_ALPHABET, check_room, check_task, fmt_td, hours, iso, label_stamp, parse_iso, round_up10

ACTIVE, GRACE, LOST, PURGED = "active", "grace", "lost-and-found", "purged"
LIVE = (ACTIVE, GRACE)
COUNTED_FLOOR_STATES = ("intent", "created", "active", "retiring")
INTENT_TIMEOUT = timedelta(hours=1)
REFUSAL_ESCALATION = timedelta(hours=24)  # a refusal this old is an error, not a note
FILL_FACTOR = 0.9


@dataclass(frozen=True)
class Caller:
    uid: int
    gid: int
    name: str
    operator: bool = False  # True only for a verified euid-0 desk process (see desk.real_caller)


@dataclass
class Ctx:
    cfg: Config
    receipt: Receipt
    provider: object
    sysops: SysOps
    store: Store
    paths: Paths
    clock: object


class Rooms:
    def __init__(self, ctx: Ctx):
        self.c = ctx
        self.cfg, self.store, self.provider, self.sys = ctx.cfg, ctx.store, ctx.provider, ctx.sysops
        self.sid = ctx.receipt.server_id
        self.never = ctx.receipt.never_touch | ctx.cfg.never_touch
        self.mount_root = ctx.paths.real(ctx.cfg.mount_root)

    # ------------------------------------------------------------------ helpers
    def now(self):
        return self.c.clock()

    def mountpoint(self, floor: dict) -> str:
        return os.path.join(self.mount_root, floor["id"])

    def room_path(self, floor: dict, room_id: str) -> str:
        return os.path.join(self.mountpoint(floor), "rooms", check_room(room_id))

    def trash_path(self, floor: dict, room_id: str) -> str:
        return os.path.join(self.mountpoint(floor), ".lost-and-found", check_room(room_id))

    def _require_host(self) -> None:
        actual = self.provider.whoami().server_id
        if str(actual) != self.sid:
            raise Guard(f"this server is {actual}, but setup was done for server {self.sid}", "motel doctor")

    def _price(self) -> Decimal:
        if self.cfg.price_gb_month_eur is not None:
            return Decimal(str(self.cfg.price_gb_month_eur))
        try:
            return Decimal(self.provider.price_gb_month(self.c.receipt.location))
        except MotelError as exc:
            raise ProviderError(f"cannot determine the price per GB ({exc.message})",
                                "set rooms.price_gb_month_eur in /etc/motel/motel.toml, then run: motel doctor") from exc

    def _guard(self, vol_id: str, action: str):
        """Re-read the volume at the provider and require the full allowlist. Returns the fresh volume."""
        try:
            vol = self.provider.get_volume(vol_id)
        except NotFound as exc:
            raise SystemFault(f"volume {vol_id} no longer exists at the provider") from exc
        check_owned(vol, self.store.floor_by_volume(vol_id), action=action, server_id=self.sid, never_touch=self.never)
        return vol

    def _owned(self, caller: Caller, room: dict) -> None:
        if caller.uid not in (0, room["owner_uid"]):
            raise Refused(f"room {room['id']} belongs to {room['owner_name']}", "motel ls --mine")

    def _room(self, room_id: str) -> dict:
        room = self.store.room(check_room(room_id))
        if room is None:
            raise NotFound(f"no room {room_id}")
        return room

    def _floor_of(self, room: dict) -> dict:
        floor = self.store.floor(room["floor"])
        if floor is None:
            raise SystemFault(f"room {room['id']} points at unknown floor {room['floor']}")
        return floor

    def _rooms_on(self, floor_id: str, rooms: list, states=None) -> list:
        return [r for r in rooms if r["floor"] == floor_id and (r["state"] in states if states else r["state"] != PURGED)]

    def _reserved_gb(self, floor_id: str, rooms: list) -> int:
        return sum(int(r["size_gb"]) for r in self._rooms_on(floor_id, rooms, LIVE))

    def _touch_floor(self, floor: dict, rooms: list) -> None:
        """Track when a floor became empty so it can be retired after ``retire_after``."""
        used = bool(self._rooms_on(floor["id"], rooms))
        want = None if used else (floor.get("idle_since") or iso(self.now()))
        if floor.get("idle_since") != want:
            floor["idle_since"] = want
            self.store.save_floor(floor)

    def _require_mounted(self, floor: dict, expected_dev: str | None = None) -> None:
        """The floor's mount point must be a mount of the floor's own, verified device (never anything else)."""
        mp = self.mountpoint(floor)
        if not self.sys.is_mounted(mp):
            raise Guard(f"{mp} is not mounted; refusing to touch the underlying directory", "motel doctor")
        expected = expected_dev or self.sys.resolve_device(self.provider.device_path(floor["volume_id"]))
        actual = self.sys.mounted_source(mp)
        if actual != expected:
            raise Guard(f"{mp} is mounted from {actual}, not from the floor's device {expected}; refusing to touch it",
                        "motel doctor")

    # ------------------------------------------------------------------ caps
    def _capacity(self, floors: list | None = None) -> dict:
        floors = self.store.floors() if floors is None else floors
        counted = [f for f in floors if f["state"] in COUNTED_FLOOR_STATES]
        total = sum(int(f["size_gb"]) for f in counted)
        return {"floors": len(counted), "max_floors": self.cfg.max_floors, "total_gb": total,
                "max_total_gb": self.cfg.max_total_gb, "max_floor_gb": self.cfg.max_floor_gb,
                "monthly_cap_eur": self.cfg.monthly_eur}

    def _billed_gb(self, floors: list, new_floor_gb: int, grow_gb: int, grow_from: int) -> int:
        """GB the monthly cost is figured on: the provider's billing tiers when it has them (``billable_gb``), else the
        requested sizes."""
        tier = getattr(self.provider, "billable_gb", None)
        if not callable(tier):
            return self._capacity(floors)["total_gb"] + new_floor_gb + grow_gb
        billed = sum(int(tier(int(f["size_gb"]))) for f in floors if f["state"] in COUNTED_FLOOR_STATES)
        if new_floor_gb:
            billed += int(tier(new_floor_gb))
        if grow_gb:
            billed += int(tier(grow_from + grow_gb)) - int(tier(grow_from))
        return billed

    def _check_caps(self, floors: list, *, new_floor_gb: int = 0, grow_gb: int = 0, grow_from: int = 0) -> None:
        cap = self._capacity(floors)
        total = cap["total_gb"] + new_floor_gb + grow_gb
        if new_floor_gb and cap["floors"] + 1 > self.cfg.max_floors:
            raise Refused(f"no vacancy: {cap['floors']} of {self.cfg.max_floors} floors already exist", "motel ls --mine")
        if new_floor_gb > self.cfg.max_floor_gb:
            raise Refused(f"no vacancy: {new_floor_gb} GB exceeds the {self.cfg.max_floor_gb} GB floor cap", "motel ls --mine")
        if total > self.cfg.max_total_gb:
            raise Refused(f"no vacancy: {cap['total_gb']}/{self.cfg.max_total_gb} GB in use; {total} GB would exceed the cap",
                          "motel ls --mine")
        billed = self._billed_gb(floors, new_floor_gb, grow_gb, grow_from)
        cost = Decimal(billed) * self._price()
        if cost > Decimal(str(self.cfg.monthly_eur)):
            raise Refused(f"no vacancy: {billed} GB would cost about EUR {cost:.2f}/month; the monthly cap is "
                          f"EUR {self.cfg.monthly_eur:g}", "motel ls --mine")

    # ------------------------------------------------------------------ reconcile
    def reconcile(self) -> tuple:
        """Match intents to provider volumes by their unique label. Returns (owned volumes, orphans).

        Unrecorded volumes are only reported, never adopted.
        """
        owned = self.provider.list_owned({"managed-by": "motel", "role": "floor", "host": self.sid})
        by_label = {v.labels.get("floor"): v for v in owned}
        floors = self.store.floors()
        now = self.now()
        for floor in floors:
            if floor["state"] in ("intent", "abandoned") and not floor.get("volume_id"):
                vol = by_label.get(floor["id"])
                if vol is not None and vol.name == floor["name"] and vol.name.startswith("motel-"):
                    floor.update(volume_id=vol.id, state="created", size_gb=vol.size_gb)
                    self.store.save_floor(floor)
                    self.store.ledger("floor-reconciled", floor=floor["id"], volume_id=vol.id)
                elif floor["state"] == "intent" and now - parse_iso(floor["created"]) > INTENT_TIMEOUT:
                    floor["state"] = "abandoned"
                    self.store.save_floor(floor)
                    self.store.ledger("floor-abandoned", floor=floor["id"])
        known = self.store.live_volume_ids()
        orphans = [v for v in owned if v.id not in known]
        return owned, orphans

    # ------------------------------------------------------------------ floors
    def _new_floor_id(self) -> str:
        taken = {f["id"] for f in self.store.floors()}
        while True:
            fid = "f-" + secrets.token_hex(2)
            if fid not in taken:
                return fid

    def _create_floor(self, size_gb: int, suite: bool, task: str, floors: list) -> dict:
        self._check_caps(floors, new_floor_gb=size_gb)  # reserved below by writing the intent, under the lock
        now = self.now()
        fid = self._new_floor_id()
        name = f"motel-{fid}-{secrets.token_hex(2)}"
        labels = {"managed-by": "motel", "role": "floor", "host": self.sid, "floor": fid,
                  "suite": "1" if suite else "0", "created": label_stamp(now)}
        floor = {"schema": SCHEMA, "id": fid, "name": name, "labels": labels, "volume_id": None, "size_gb": size_gb,
                 "suite": suite, "state": "intent", "created": iso(now), "idle_since": iso(now), "first_task": task.lower()}
        self.store.save_floor(floor)  # intent BEFORE the provider call
        self.store.ledger("floor-intent", floor=fid, size_gb=size_gb, suite=suite)
        try:
            vol = self.provider.create_volume(name, size_gb, self.c.receipt.location, labels, server_id=self.sid)
        except ProviderError as exc:
            made = exc.volume_id
            if not made and not exc.indeterminate:
                # a definite failure must not hide a volume that exists: look for our label before giving up
                try:
                    made = next((v.id for v in self.provider.list_owned({"floor": fid, "managed-by": "motel"})
                                 if v.name == name), None)
                except MotelError:
                    made = None
            if made:
                # the cloud made the volume (it bills): record it NOW so caps count it and housekeeping retires it
                floor.update(volume_id=str(made), state="created")
                self.store.save_floor(floor)
                self.store.ledger("floor-created-unconfirmed", floor=fid, volume_id=str(made))
                raise ProviderError(f"{exc.message}; floor {fid} is recorded and housekeeping will retire it if it stays unused",
                                    "motel doctor", indeterminate=True, volume_id=str(made)) from exc
            if not exc.indeterminate:
                floor["state"] = "failed"
                self.store.save_floor(floor)
                raise
            vol = next((v for v in self.provider.list_owned({"floor": fid, "managed-by": "motel"}) if v.name == name), None)
            if vol is None:
                raise ProviderError(f"the create call for floor {fid} may or may not have worked ({exc.message}); "
                                    "its capacity stays reserved until housekeeping resolves it", "motel doctor",
                                    indeterminate=True) from exc
        floor.update(volume_id=vol.id, state="created", size_gb=vol.size_gb, volume_received=iso(self.now()))
        self.store.save_floor(floor)
        self.store.ledger("floor-created", floor=fid, volume_id=vol.id, size_gb=vol.size_gb)
        self.ensure_mounted(floor)
        return floor

    def _grow_floor(self, floor: dict, new_size: int, floors: list) -> None:
        old = int(floor["size_gb"])
        if new_size <= old:
            raise SystemFault(f"refusing a resize of floor {floor['id']} that does not grow it ({old} -> {new_size} GB)")
        self._check_caps(floors, grow_gb=new_size - old, grow_from=old)
        self._guard(floor["volume_id"], "resize")
        floor["size_gb"] = new_size  # reserve before the provider call
        floor["resize_pending"] = iso(self.now())
        before = self._device_bytes(floor)  # what the guest sees now: the grow waits for more than this
        if before:
            floor["fs_grow_from"] = before
        self.store.save_floor(floor)
        try:
            self.provider.resize(floor["volume_id"], new_size)
        except ProviderError as exc:
            if exc.indeterminate:
                raise  # it may have happened: keep the reservation; housekeeping reconciles with the provider's size
            try:
                actual = self.provider.get_volume(floor["volume_id"]).size_gb
            except MotelError:
                actual = old
            floor["size_gb"] = max(old, actual)
            floor.pop("resize_pending", None)
            floor.pop("fs_grow_from", None)
            self.store.save_floor(floor)
            raise
        floor.pop("resize_pending", None)
        floor["fs_grow_pending"] = True  # the volume is bigger; housekeeping finishes the job if growing the fs fails
        self.store.save_floor(floor)
        self._grow_fs(floor)
        self.store.ledger("floor-resized", floor=floor["id"], size_gb=new_size)

    def _device_bytes(self, floor: dict) -> int | None:
        """The size the guest shows for the floor's device now, or None when it cannot be read."""
        try:
            return self.sys.block_size(self.sys.resolve_device(self.provider.device_path(floor["volume_id"]))) or None
        except MotelError:
            return None

    def _note_grow_from(self, floor: dict, old_gb: int, new_gb: int) -> None:
        """A grow found by housekeeping: remember the size the guest shows now, if it is still the old one.

        The grow later waits for more than this. It is recorded only when the guest's size is exactly an old-size reading
        and not a new-size reading: a guest that already shows the new size (virtio updates by itself) must not be made
        to wait for a size above it, and a size that matches neither is not a safe baseline.
        """
        if old_gb >= new_gb:
            return
        seen = self._device_bytes(floor)
        if seen and seen in volume_bytes(self.provider, old_gb) and seen not in volume_bytes(self.provider, new_gb):
            floor["fs_grow_from"] = seen

    def _grow_fs(self, floor: dict, real: str | None = None) -> None:
        """Grow the filesystem to the volume (online, on the verified device) and clear the marker."""
        if real is None:
            real = verify_device(self.sys, self.provider, floor["volume_id"], self.provider.device_path(floor["volume_id"]),
                                 fs_label_for(floor["id"]))
        # the guest may not see an online resize until the device is rescanned (Azure, Scaleway)
        # Wait for both: at least the smaller reading of the new size, and more than the size seen before the resize
        # (on a cloud whose unit is unknown the old size can already pass the first test).
        self.sys.rescan(real, grow_target_bytes(self.provider, int(floor["size_gb"])), floor.get("fs_grow_from") or None)
        self.sys.grow_fs(real)
        floor.pop("fs_grow_pending", None)
        floor.pop("fs_grow_from", None)
        self.store.save_floor(floor)

    def _check_mount_root(self) -> None:
        os.makedirs(self.mount_root, mode=0o755, exist_ok=True)
        st = os.lstat(self.mount_root)
        if not stat.S_ISDIR(st.st_mode):
            raise Guard(f"{self.mount_root} is not a directory", "motel doctor")
        if self.c.paths.strict and (st.st_uid != 0 or st.st_mode & 0o022):
            raise Guard(f"{self.mount_root} must be root-owned and not group/world writable", "motel doctor")

    def ensure_mounted(self, floor: dict) -> str:
        """Attach (if needed), verify, format-label and mount the floor; returns its mount point."""
        vid = floor["volume_id"]
        mp = self.mountpoint(floor)
        if floor["state"] == "active" and self.sys.is_mounted(mp):
            try:  # the device path of a detached volume may be unknown (Vultr, Azure): then it is not mounted either
                if self.sys.mounted_source(mp) == self.sys.resolve_device(self.provider.device_path(vid)):
                    return mp
            except ProviderError:
                pass
        vol = self.provider.get_volume(vid)
        if vol.server_id is None:
            check_owned(vol, floor, action="attach", server_id=self.sid, never_touch=self.never)
            self.provider.attach(vid, self.sid)
        dev = self.provider.device_path(vid)  # after the attach: some clouds learn the path only then
        self._guard(vid, "mount")
        self.sys.wait_device(dev)
        formats = getattr(self.provider, "formats_on_create", True)
        if floor["state"] == "created" and not formats:
            format_blank_device(self.sys, self.provider, vid, dev, int(vol.size_gb), floor["id"], own_mountpoint=mp)
        # a filesystem we labelled ourselves is an identity check for clouds with no device serial
        label = fs_label_for(floor["id"]) if floor["state"] != "created" or not formats else None
        real = verify_device(self.sys, self.provider, vid, dev, label)
        if floor["state"] == "created":
            self.sys.prepare_fs(real, f"motel-{floor['id']}")
        self._check_mount_root()
        if os.path.lexists(mp):
            st = os.lstat(mp)
            if not stat.S_ISDIR(st.st_mode) or (self.c.paths.strict and st.st_uid != 0):
                raise Guard(f"{mp} exists and is not a root-owned directory", "motel doctor")
            if not self.sys.is_mounted(mp) and os.listdir(mp):
                raise Guard(f"{mp} exists and is not empty", "motel doctor")
        else:
            os.mkdir(mp, 0o755)
        if self.sys.is_mounted(mp):
            self._require_mounted(floor, real)  # never mount on top of, or trust, a foreign mount
        else:
            self.sys.mount(real, mp)
            self._require_mounted(floor, real)
        if floor.get("fs_grow_pending"):
            self._grow_fs(floor, real)
        for sub, mode in (("rooms", 0o755), (".lost-and-found", 0o700), (".motel", 0o755)):
            path = os.path.join(mp, sub)
            if not os.path.isdir(path):
                os.mkdir(path, mode)
            self.sys.chown(path, 0, 0)
            os.chmod(path, mode)
        if floor["state"] == "created":
            atomic_write(os.path.join(mp, ".motel", "manifest.json"), json.dumps(
                {"schema": SCHEMA, "floor": floor["id"], "volume_id": vid, "name": floor["name"], "host": self.sid,
                 "created": floor["created"], "disposable": True}, indent=1), 0o644)
            floor["state"] = "active"
            self.store.save_floor(floor)
        return mp

    def _retire_floor(self, floor: dict) -> None:
        vid, mp = floor["volume_id"], self.mountpoint(floor)
        floor["state"] = "retiring"
        self.store.save_floor(floor)
        self.store.ledger("floor-retiring", floor=floor["id"], volume_id=vid)
        vol = self.provider.get_volume(vid)
        if vol.server_id == self.sid:
            self._guard(vid, "detach")
            if self.sys.is_mounted(mp):
                self._require_mounted(floor)
                nested = [m for m in self.sys.mounts_under(mp) if m != mp.rstrip("/")]
                if nested:
                    raise Busy(f"floor {floor['id']} has a mount inside it at {', '.join(nested[:3])}", "motel ls --all")
                busy = self.sys.holders(mp)
                if busy:
                    raise Busy(f"floor {floor['id']} is in use by {', '.join(busy[:5])}", "motel ls --all")
                self.sys.umount(mp)
            # the device must not be mounted anywhere else (bind mounts, other namespaces) before it is detached
            elsewhere = [m for m in self.sys.device_mounts(self.sys.resolve_device(self.provider.device_path(vid)))]
            if elsewhere:
                raise Busy(f"floor {floor['id']}'s device is still mounted at {', '.join(elsewhere[:3])}", "motel ls --all")
            if os.path.isdir(mp) and not self.sys.is_mounted(mp):
                try:
                    os.rmdir(mp)
                except OSError:
                    pass
            self.provider.detach(vid)
            self.store.ledger("floor-detached", floor=floor["id"], volume_id=vid)
        self._guard(vid, "delete")
        self.provider.delete(vid)
        floor.update(state="deleted", deleted=iso(self.now()))
        self.store.save_floor(floor)
        self.store.ledger("floor-deleted", floor=floor["id"], volume_id=vid, size_gb=floor["size_gb"])

    # ------------------------------------------------------------------ placement
    def _place(self, size_gb: int, suite: bool, task: str, rooms: list) -> dict:
        floors = self.store.floors()
        if not suite:
            shared = [f for f in floors if f["state"] == "active" and not f["suite"]]
            for floor in sorted(shared, key=lambda f: int(f["size_gb"])):
                if int(floor["size_gb"]) - self._reserved_gb(floor["id"], rooms) < size_gb:
                    continue
                mp = self.ensure_mounted(floor)
                if self.sys.usage(mp)[1] < size_gb * GIB * FILL_FACTOR:
                    continue
                return floor
        refusal = None
        counted = [f for f in floors if f["state"] in COUNTED_FLOOR_STATES]
        if suite or len(counted) < self.cfg.max_floors:
            want = round_up10(size_gb if suite else max(size_gb, self.cfg.min_floor_gb))
            want = min(want, max(self.cfg.max_floor_gb, size_gb))
            try:
                return self._create_floor(want, suite, task, floors)
            except Refused as exc:
                refusal = exc
        if not suite:
            growable = [f for f in floors if f["state"] == "active" and not f["suite"]]
            for floor in sorted(growable, key=lambda f: int(f["size_gb"])):
                mp = self.ensure_mounted(floor)
                total, free = self.sys.usage(mp)
                used_gb = -(-(total - free) // GIB)  # what is really on the disk (rooms over their size, lost & found)
                new_size = round_up10(max(self._reserved_gb(floor["id"], rooms), used_gb) + size_gb)
                if new_size <= int(floor["size_gb"]):
                    continue  # growing by nothing would not make room (and the provider would refuse)
                if new_size <= self.cfg.max_floor_gb:
                    try:
                        self._grow_floor(floor, new_size, floors)
                    except Refused as exc:
                        refusal = refusal or exc
                        continue
                    return floor
        raise refusal or Refused(f"no vacancy: {len(counted)} floor(s) at the cap and none can grow by {size_gb} GB",
                                 "motel ls --mine")

    # ------------------------------------------------------------------ verbs
    def checkin(self, caller: Caller, size_gb: int, ttl: timedelta, task: str, suite: bool) -> dict:
        check_task(task)
        if not 1 <= size_gb <= self.cfg.max_floor_gb:
            raise Refused(f"no vacancy: a room may hold 1 to {self.cfg.max_floor_gb} GB", f"motel checkin {self.cfg.max_floor_gb}G")
        if ttl > self.cfg.max_ttl_call:
            raise Refused(f"checkout time {fmt_td(ttl)} exceeds the {fmt_td(self.cfg.max_ttl_call)} limit per call; extend later instead",
                          "motel checkin 200G --for 6h")
        with self.store.locked():
            self._require_host()
            self.reconcile()
            rooms = self.store.rooms()
            floor = self._place(size_gb, suite, task, rooms)
            self.ensure_mounted(floor)
            self._require_mounted(floor)
            now = self.now()
            room_id = self._new_room_id(task, rooms)
            room = {"schema": SCHEMA, "id": room_id, "task": task, "owner_uid": caller.uid, "owner_name": caller.name,
                    "size_gb": size_gb, "floor": floor["id"], "suite": suite, "created": iso(now),
                    "expires": iso(now + ttl), "max_expires": iso(now + self.cfg.max_lifetime), "state": ACTIVE,
                    "warned": False, "host": self.sid}
            self.store.save_room(room)  # record first; a crash leaves a record without a directory, never the reverse
            path = self.room_path(floor, room_id)
            try:
                os.mkdir(path, 0o700)
                self._write_marker(room, floor)
                self.sys.chown(path, caller.uid, caller.gid)
            except OSError as exc:
                room.update(state=PURGED, purged_at=iso(now), note=f"creation failed: {exc}")
                self.store.save_room(room)
                raise SystemFault(f"could not create the room directory: {exc}") from exc
            floor["idle_since"] = None
            self.store.save_floor(floor)
            self.store.ledger("room-checked-in", room=room_id, floor=floor["id"], size_gb=size_gb, owner=caller.name,
                              uid=caller.uid, expires=room["expires"], task=task)
            return self.describe(room)

    def _new_room_id(self, task: str, rooms: list) -> str:
        taken = {r["id"] for r in rooms}
        while True:
            room_id = f"{task}-" + "".join(secrets.choice(ROOM_ALPHABET) for _ in range(4))
            if room_id not in taken:
                return room_id

    def _write_marker(self, room: dict, floor: dict) -> None:
        path = self.room_path(floor, room["id"])
        if not os.path.isdir(path):
            return
        doc = {"schema": SCHEMA, "id": room["id"], "owner": room["owner_name"], "size_gb": room["size_gb"],
               "created": room["created"], "expires": room["expires"], "max_expires": room["max_expires"]}
        safe_write_in_dir(path, ".motel-room.json", json.dumps(doc, indent=1, sort_keys=True) + "\n", 0o644)
        text = (f"DISPOSABLE motel room {room['id']}.\n"
                f"Checkout time {room['expires']}. After a {fmt_td(self.cfg.grace)} grace the room is moved to lost & found\n"
                f"and purged {fmt_td(self.cfg.lost_and_found)} later. Nothing here is backed up.\n"
                f"Check: motel status {room['id']}    Extend: motel extend {room['id']} --for 6h\n"
                f"Copy anything worth keeping elsewhere, then: motel checkout {room['id']}\n")
        safe_write_in_dir(path, "MOTEL-ROOM.txt", text, 0o444)

    def describe(self, room: dict) -> dict:
        now = self.now()
        expires = parse_iso(room["expires"])
        floor = self.store.floor(room["floor"])
        state = room["state"]
        if state == ACTIVE and now >= expires:
            state = GRACE
        out = {k: room.get(k) for k in ("id", "task", "size_gb", "floor", "suite", "created", "expires", "max_expires")}
        out.update(owner=room["owner_name"], uid=room["owner_uid"], state=state, time_left_h=hours(expires - now),
                   path=None, warn=False)
        if floor is not None:
            if state in LIVE:
                out["path"] = self.room_path(floor, room["id"])
            elif state == LOST:
                out["path"] = self.trash_path(floor, room["id"])
        if state in LIVE and now >= expires:
            out["grace_ends"] = iso(expires + self.cfg.grace)
        if state == ACTIVE and expires - now <= self.cfg.warn_before:
            out["warn"] = True
        if state == LOST and room.get("trashed_at"):
            out["purge_at"] = iso(parse_iso(room["trashed_at"]) + self.cfg.lost_and_found)
            out["lost_and_found_reason"] = room.get("trash_reason")
        out["floor_free_gb"] = out["floor_used_pct"] = None
        if floor is not None and floor.get("volume_id"):
            mp = self.mountpoint(floor)
            try:
                if self.sys.is_mounted(mp):
                    total, free = self.sys.usage(mp)
                    out["floor_free_gb"] = round(free / GIB, 1)
                    out["floor_used_pct"] = round(100 * (1 - free / total), 1) if total else None
            except OSError:
                pass
        return out

    def extend(self, caller: Caller, room_id: str, ttl: timedelta) -> dict:
        with self.store.locked():
            room = self._room(room_id)
            self._owned(caller, room)
            if room["state"] not in LIVE:
                raise Refused(f"room {room_id} is {room['state']}; use `motel restore` while it is in lost & found",
                              f"motel restore {room_id}")
            if ttl > self.cfg.max_ttl_call:
                raise Refused(f"{fmt_td(ttl)} exceeds the {fmt_td(self.cfg.max_ttl_call)} limit per extension", f"motel extend {room_id} --for 7d")
            now = self.now()
            wanted = max(parse_iso(room["expires"]), now) + ttl
            cap = parse_iso(room["max_expires"])
            if wanted > cap:
                if now >= cap:
                    raise Refused(f"room {room_id} reached its {fmt_td(self.cfg.max_lifetime)} lifetime limit",
                                  f"motel checkin 200G --task {room['task']}")
                wanted = cap
            room.update(expires=iso(wanted), state=ACTIVE, warned=False)
            self.store.save_room(room)
            self.store.ledger("room-extended", room=room_id, expires=room["expires"], uid=caller.uid)
            try:
                self._write_marker(room, self._floor_of(room))
            except OSError:
                pass  # the record is authoritative; the marker is a convenience
            return self.describe(room)

    def _refuse_if_busy(self, room: dict, path: str, mp: str) -> None:
        """Busy (exit 4) if a mount sits at or under the room, or any process holds the floor's filesystem there."""
        nested = self.sys.mounts_under(path)
        if nested:
            raise Busy(f"room {room['id']} has a mount at {', '.join(nested[:3])}; unmount it first",
                       f"motel status {room['id']}")
        busy = self.sys.holders(path, mp)
        if busy:
            raise Busy(f"room {room['id']} is in use by: {', '.join(busy[:5])}; leave the directory and retry",
                       f"motel checkout {room['id']}")

    def _trash(self, room: dict, reason: str) -> None:
        floor = self._floor_of(room)
        mp = self.ensure_mounted(floor)
        self._require_mounted(floor)
        src, dst = self.room_path(floor, room["id"]), self.trash_path(floor, room["id"])
        if os.path.lexists(src):
            self._refuse_if_busy(room, src, mp)
            if os.path.lexists(dst):
                raise Guard(f"{dst} already exists", "motel doctor")
            os.rename(src, dst)
        room.update(state=LOST, trashed_at=iso(self.now()), trash_reason=reason)
        self.store.save_room(room)
        self.store.ledger("room-lost-and-found", room=room["id"], reason=reason)
        if self.cfg.lost_and_found == timedelta(0):
            self._purge(room)

    def _purge(self, room: dict) -> None:
        floor = self._floor_of(room)
        mp = self.ensure_mounted(floor)
        self._require_mounted(floor)
        path = self.trash_path(floor, room["id"])
        if os.path.lexists(path):
            if os.path.islink(path) or not os.path.isdir(path):
                raise Guard(f"{path} is not a plain directory", "motel doctor")
            self._refuse_if_busy(room, path, mp)
            # descriptor-based, iterative, confined to the floor's filesystem (see motel.fsutil)
            safe_rmtree(path, expect_dev=os.stat(mp).st_dev)
        room.update(state=PURGED, purged_at=iso(self.now()))
        self.store.save_room(room)
        self.store.ledger("room-purged", room=room["id"])
        self._touch_floor(floor, self.store.rooms())

    def checkout(self, caller: Caller, room_id: str, now_flag: bool) -> dict:
        with self.store.locked():
            room = self._room(room_id)
            self._owned(caller, room)
            if room["state"] == PURGED:
                raise Refused(f"room {room_id} is already purged", "motel ls --mine")
            if room["state"] in LIVE:
                self._trash(room, "checkout")
            if now_flag and room["state"] == LOST:
                self._purge(room)
            self.store.ledger("room-checked-out", room=room_id, purged=room["state"] == PURGED, uid=caller.uid)
            return self.describe(room)

    def restore(self, caller: Caller, room_id: str, ttl: timedelta | None) -> dict:
        ttl = ttl or self.cfg.default_ttl
        if ttl > self.cfg.max_ttl_call:
            raise Refused(f"{fmt_td(ttl)} exceeds the {fmt_td(self.cfg.max_ttl_call)} limit per call", f"motel restore {room_id} --for 6h")
        with self.store.locked():
            room = self._room(room_id)
            self._owned(caller, room)
            floor = self._floor_of(room)
            if room["state"] != LOST:
                raise Refused(f"room {room_id} is {room['state']}; only lost & found rooms can be restored", "motel ls --mine")
            now = self.now()
            cap = parse_iso(room["max_expires"])
            if now >= cap:
                raise Refused(f"room {room_id} is past its {fmt_td(self.cfg.max_lifetime)} lifetime limit", "motel ls --mine")
            self.ensure_mounted(floor)
            self._require_mounted(floor)
            src, dst = self.trash_path(floor, room_id), self.room_path(floor, room_id)
            if not os.path.isdir(src) or os.path.lexists(dst):
                raise Refused(f"room {room_id} cannot be restored: its data is gone", "motel ls --mine")
            # Record first, data second: a crash in between leaves an active record whose data is still in
            # lost & found, which housekeeping moves back (_heal_room). The data is never lost.
            before = dict(room)
            room.update(state=ACTIVE, expires=iso(min(now + ttl, cap)), warned=False, trashed_at=None, trash_reason=None)
            self.store.save_room(room)
            try:
                os.rename(src, dst)
            except OSError:
                self.store.save_room(before)
                raise
            floor["idle_since"] = None
            self.store.save_floor(floor)
            self.store.ledger("room-restored", room=room_id, expires=room["expires"], uid=caller.uid)
            try:
                self._write_marker(room, floor)
            except OSError:
                pass  # the record is authoritative; the marker is a convenience
            return self.describe(room)

    def status(self, caller: Caller, room_id: str) -> dict:
        room = self._room(room_id)
        self._owned(caller, room)
        return self.describe(room)

    def ls(self, caller: Caller, everyone: bool) -> dict:
        rooms = [r for r in self.store.rooms() if r["state"] != PURGED and (everyone or r["owner_uid"] == caller.uid)]
        floors = self.store.floors()
        price = None
        try:
            price = self._price()
        except MotelError:
            pass
        info = []
        for floor in floors:
            if floor["state"] not in COUNTED_FLOOR_STATES:
                continue
            info.append({"id": floor["id"], "size_gb": floor["size_gb"], "suite": floor["suite"], "state": floor["state"],
                         "rooms": len(self._rooms_on(floor["id"], self.store.rooms())),
                         "eur_per_day": None if price is None else round(float(Decimal(floor["size_gb"]) * price / Decimal("30.4")), 3)})
        capacity = self._capacity(floors)
        if price is not None:
            capacity["est_monthly_eur"] = round(float(Decimal(capacity["total_gb"]) * price), 2)
        return {"rooms": [self.describe(r) for r in rooms], "floors": info, "capacity": capacity}

    # ------------------------------------------------------------------ housekeeping
    def reap(self, apply: bool) -> dict:
        report = {"schema": SCHEMA, "host": self.sid, "run_at": iso(self.now()), "apply": apply, "actions": [],
                  "warnings": [], "refused": [], "orphans": [], "errors": [], "floors": [], "rooms": [],
                  "held": []}

        def note(exc: Exception, what: str, rec: dict | None = None, save=None, now=None) -> None:
            if not isinstance(exc, MotelError):  # one room's trouble must never stop housekeeping for the others
                exc = SystemFault(f"{exc.__class__.__name__}: {exc}")
            bucket = "refused" if isinstance(exc, (Busy, Guard, Refused)) else "errors"
            text = f"{what}: {exc.message}"
            if rec is not None and isinstance(exc, (Busy, Guard)):
                since = rec.get("refused_since")
                if not since:
                    since = iso(now)
                    if apply:
                        rec["refused_since"] = since
                        save(rec)
                if now - parse_iso(since) > REFUSAL_ESCALATION:
                    bucket, text = "errors", f"{text} (refused since {since}, for more than 24 h)"
            report[bucket].append(text)

        def cleared(rec: dict, save) -> None:
            if apply and rec.get("refused_since"):
                rec.pop("refused_since", None)
                save(rec)

        with self.store.locked():
            now = self.now()
            try:
                self._require_host()
            except MotelError as exc:  # wrong server: touch nothing, and make the run fail loudly
                report["errors"].append(f"host identity mismatch: {exc.message}")
                report["ok"] = False
                return report
            inventory_ok = True
            try:
                _, orphans = self.reconcile() if apply else (None, self._orphans_only())
                report["orphans"] += [f"volume {v.id} ({v.name}) carries motel labels for this host but has no motel "
                                      "record; it is left alone" for v in orphans]
            except InventoryIncomplete as exc:
                inventory_ok = False
                report["errors"].append(exc.message)
            except MotelError as exc:
                inventory_ok = False
                note(exc, "provider")
            for room in self.store.rooms():
                try:
                    self._reap_room(room, now, apply, report)
                    cleared(room, self.store.save_room)
                except (MotelError, OSError, RecursionError) as exc:
                    note(exc, f"room {room['id']}", room, self.store.save_room, now)
            if inventory_ok:
                rooms = self.store.rooms()
                for floor in self.store.floors():
                    try:
                        self._reap_floor(floor, rooms, now, apply, report)
                        cleared(floor, self.store.save_floor)
                    except (MotelError, OSError, RecursionError) as exc:
                        note(exc, f"floor {floor['id']}", floor, self.store.save_floor, now)
            report["rooms"] = [self.describe(r) for r in self.store.rooms() if r["state"] != PURGED]
        report["ok"] = not report["errors"]
        return report

    def _orphans_only(self) -> list:
        owned = self.provider.list_owned({"managed-by": "motel", "role": "floor", "host": self.sid})
        known = self.store.live_volume_ids()
        return [v for v in owned if v.id not in known]

    def _heal_room(self, room: dict, apply: bool, report: dict) -> None:
        """An active record whose data sits in lost & found (a crash during restore): move it back."""
        floor = self.store.floor(room["floor"])
        if floor is None or floor.get("state") != "active" or not self.sys.is_mounted(self.mountpoint(floor)):
            return
        self._require_mounted(floor)  # a foreign mount on the floor's path is never touched
        src, trash = self.room_path(floor, room["id"]), self.trash_path(floor, room["id"])
        if not os.path.lexists(src) and os.path.isdir(trash) and not os.path.islink(trash):
            report["actions"].append(f"room {room['id']}: data was left in lost & found by an interrupted restore, moved back")
            if apply:
                os.rename(trash, src)
                self.store.ledger("room-healed", room=room["id"])

    def _reap_room(self, room: dict, now, apply: bool, report: dict) -> None:
        state, expires = room["state"], parse_iso(room["expires"])
        grace = self.cfg.grace
        if state in LIVE:
            self._heal_room(room, apply, report)
        if state in LIVE and now >= expires:
            if state == ACTIVE:
                report["actions"].append(f"room {room['id']} passed its checkout time (grace {fmt_td(grace)})")
                if apply:
                    room["state"] = GRACE
                    self.store.save_room(room)
                    self.store.ledger("room-grace", room=room["id"])
            if now >= expires + grace:
                report["actions"].append(f"room {room['id']} grace over: moved to lost & found")
                if apply:
                    self._trash(room, "expired")
        elif state == ACTIVE and not room.get("warned") and expires - now <= self.cfg.warn_before:
            report["warnings"].append(f"room {room['id']} ({room['owner_name']}, {room['task']}) checks out at {room['expires']}")
            if apply:
                room["warned"] = True
                self.store.save_room(room)
        elif state == LOST and now >= parse_iso(room["trashed_at"]) + self.cfg.lost_and_found:
            report["actions"].append(f"room {room['id']} purged from lost & found")
            if apply:
                self._purge(room)

    def _hold_until_window(self, floor: dict, vol) -> dict | None:
        """An empty SHARED floor that has waited ``retire_after`` is kept until its release window, when the provider
        bills every started hour from creation: deleting it early saves nothing, and a new checkin can still pack
        onto it. A report entry (shaped like the cabin reaper's ``held`` entries) when it is held, None when it is
        retired now. Never held: a suite floor (nothing can pack onto it), ``retire_after = "0"`` (the operator asked
        for immediate retirement), a provider whose billing is not hourly or not known, and anything with no usable
        creation time. The window is judged by the clock now, not by the time the pass started: a pass can run for
        minutes."""
        if floor.get("suite") or self.cfg.retire_after <= timedelta(0) or not billing.volume_rule(self.provider).aligned:
            return None
        now = self.now()
        try:
            record = parse_iso(floor.get("created"))
            received = parse_iso(floor.get("volume_received"))
        except (TypeError, ValueError):
            record = received = None
        start, source = billing.anchor([vol.created], [received], record, now)
        if start is None:
            return None
        window = billing.release_window(start, now, margin=self.cfg.release_margin)
        if window.release:
            return None
        return {"floor": floor["id"], "reason": "empty", "paid_until": iso(window.paid_until),
                "release_from": iso(window.opens), "release_by": iso(window.closes), "billing_source": source}

    def _reap_floor(self, floor: dict, rooms: list, now, apply: bool, report: dict) -> None:
        if floor["state"] not in ("created", "active", "retiring"):
            return
        vid = floor["volume_id"]
        users = self._rooms_on(floor["id"], rooms)
        try:
            vol = self.provider.get_volume(vid)
        except NotFound:
            if users:
                report["errors"].append(f"floor {floor['id']}: volume {vid} is gone at the provider but rooms still point at it")
            elif apply:
                floor.update(state="deleted", note="volume vanished at the provider")
                self.store.save_floor(floor)
                self.store.ledger("floor-vanished", floor=floor["id"], volume_id=vid)
            return
        if apply and (int(vol.size_gb) > int(floor["size_gb"]) or floor.get("resize_pending")):
            pending = floor.get("resize_pending")
            if int(vol.size_gb) >= int(floor["size_gb"]):
                recorded = int(floor["size_gb"])
                floor["size_gb"] = int(vol.size_gb)  # the resize happened (or happened out of band)
                floor.pop("resize_pending", None)
                floor["fs_grow_pending"] = True  # a bigger volume is useless until the filesystem follows
                if not floor.get("fs_grow_from"):
                    self._note_grow_from(floor, recorded, int(vol.size_gb))
            elif pending and now - parse_iso(pending) > INTENT_TIMEOUT:
                floor["size_gb"] = int(vol.size_gb)  # the resize never happened; release the reservation
                floor.pop("resize_pending", None)
                floor.pop("fs_grow_from", None)  # no grow is coming: a stale "before" size would block the next one
            self.store.save_floor(floor)
        # an empty floor is retired, not grown: a grow that fails (a guest that never sees the new size) must not keep
        # the floor, and its bill, alive
        if apply and users and floor.get("fs_grow_pending") and vol.server_id == self.sid and floor["state"] == "active" \
                and self.sys.is_mounted(self.mountpoint(floor)):
            self._require_mounted(floor)
            self._grow_fs(floor)
            self.store.ledger("floor-grown", floor=floor["id"], size_gb=int(floor["size_gb"]))
        price = None
        try:
            price = self._price()
        except MotelError:
            pass
        info = {"id": floor["id"], "volume_id": vid, "size_gb": int(vol.size_gb), "state": floor["state"], "rooms": len(users),
                "eur_per_day": None if price is None else round(float(Decimal(vol.size_gb) * price / Decimal("30.4")), 3)}
        report["floors"].append(info)
        if vol.server_id not in (None, self.sid):
            report["orphans"].append(f"floor {floor['id']} (volume {vid}) is attached to server {vol.server_id}, not this one")
            return
        if floor["state"] == "retiring":
            report["actions"].append(f"floor {floor['id']} finish retiring: unmount, detach, delete")
            if apply:
                self._retire_floor(floor)
            return
        if users:
            if apply and floor.get("idle_since"):
                floor["idle_since"] = None
                self.store.save_floor(floor)
            if apply:
                try:
                    mp = self.ensure_mounted(floor)
                except MotelError as exc:
                    report["errors"].append(f"floor {floor['id']} remount: {exc.message}")
                    return
                total, free = self.sys.usage(mp)
                info["used_pct"] = round(100 * (1 - free / total), 1) if total else None
                if info["used_pct"] and info["used_pct"] >= 90:
                    report["warnings"].append(f"floor {floor['id']} is {info['used_pct']}% full")
            return
        if not floor.get("idle_since"):
            if apply:
                floor["idle_since"] = iso(now)
                self.store.save_floor(floor)
            return
        if now - parse_iso(floor["idle_since"]) < self.cfg.retire_after:
            return
        held = self._hold_until_window(floor, vol)
        if held is not None:
            report["held"].append(held)
            return
        # fail early, in the report, if the allowlist would refuse the retirement
        check_owned(vol, floor, action="mount" if vol.server_id == self.sid else "attach", server_id=self.sid,
                    never_touch=self.never)
        report["actions"].append(f"floor {floor['id']} has no rooms: unmount, detach, delete")
        if apply:
            self._retire_floor(floor)
