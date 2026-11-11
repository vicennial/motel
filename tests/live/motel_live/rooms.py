"""The rooms scenario: M0 install-to-purge, M1 live checks (reboot, interrupted acquire, extend in grace, restore, purge,
busy refusal, unrelated volumes untouched). Runs against the throwaway test server through ``ctx.remote``."""

from __future__ import annotations

from datetime import datetime

from motel_live import cloud
from motel_live.core import Abort, parse_json, release_bounds, wait_until
from motel_live.remote import HOLD_SCRIPT, KILL_SCRIPT

USER = "alice"
OUTSIDER = "bob"
MOUNT_ROOT = "/mnt/motel"
HOLD_PID = "/tmp/motel-live-hold.pid"


ROOMS_POLL_S = 30


def parse_epoch(text):
    """An ISO time (as hcloud and the fake print it) as seconds since the epoch, or None."""
    from datetime import UTC, datetime
    try:
        parsed = datetime.fromisoformat(str(text).replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed.replace(tzinfo=parsed.tzinfo or UTC).timestamp()


def rooms_config(*, users=("alice",), min_floor_gb=20, max_floors=4, max_floor_gb=100, max_total_gb=200,
                 grace="2m", lost_and_found="3m", retire_after="2m", warn_before="1m") -> str:
    listed = ", ".join(f'"{u}"' for u in users)
    return (f'# written by the motel live harness\n[host]\nprovider = "hetzner"\nserver_id = "auto"\nlocation = "auto"\n'
            f'allow_users = [{listed}]\n\n[rooms]\nmount_root = "{MOUNT_ROOT}"\nfilesystem = "ext4"\ndefault_ttl = "30m"\n'
            f'max_ttl_call = "7d"\nmax_lifetime = "14d"\ngrace = "{grace}"\nlost_and_found = "{lost_and_found}"\n'
            f'min_floor_gb = {min_floor_gb}\nwarn_before = "{warn_before}"\nretire_after = "{retire_after}"\n\n'
            f'[rooms.caps]\nmax_floors = {max_floors}\nmax_floor_gb = {max_floor_gb}\nmax_total_gb = {max_total_gb}\nmonthly_eur = 30\n')


def when(text):
    return datetime.fromisoformat(str(text).replace("Z", "+00:00"))


class RoomsScenario:
    def __init__(self, ctx):
        self.ctx, self.rep, self.rem, self.hc, self.clock = ctx, ctx.report, ctx.remote, ctx.hc, ctx.clock
        self.dry = ctx.dry
        self.floor_gb, self.room_gb, self.suite_gb = ctx.args.min_floor_gb, ctx.args.room_gb, ctx.args.suite_gb

    # ----------------------------------------------------------------- helpers
    def motel(self, *args, user=USER):
        res = self.rem.motel(user, *args, "--json")
        return res, parse_json(res)

    def desk(self, *args):
        res = self.rem.desk(*args, "--json")
        return res, parse_json(res)

    def room(self, doc) -> dict:
        return doc["room"] if self.dry else (doc.get("room") or {})

    def motel_volumes(self) -> list:
        return self.hc.list("volume", "managed-by=motel")

    def set_config(self, text: str) -> None:
        res = self.rem.put_text("/etc/motel/motel.toml", text)
        if res.rc != 0:
            raise AssertionError(f"could not write the config: {res.err.strip()[:200]}")

    def put_file(self, c, room, name="marker.txt", content="motel-live-data"):
        path = f"{room['path']}/{name}"
        res = self.rem.write_file(path, content, as_user=USER)
        c.require(res.rc == 0, f"could not write {path} as {USER}: {res.err.strip()[:200]}")
        return path

    def reap_apply(self):
        res, doc = self.desk("reap", "--apply")
        return res, doc

    # ----------------------------------------------------------------- scenario
    def run(self) -> None:
        state = {}
        for step in (self.doctor_and_permissions, self.first_checkin, self.pack_and_suite, self.extend_room, self.grow_floor, self.caps_refusal,
                     self.busy_refusal, self.lifecycle_walk, self.interrupted_acquire, self.reboot, self.setup_rerun,
                     self.checkout_all_and_retire, self.bystander_untouched):
            step(state)

    def doctor_and_permissions(self, st) -> None:
        with self.rep.check("rooms: motel doctor reports ok", self.clock) as c:
            res, doc = self.motel("doctor")
            c.require(res.rc == 0 and doc.get("ok") is True, f"doctor rc={res.rc}: non-ok checks: " + "; ".join(
                f"{k.get('level')} {k.get('check')}: {k.get('detail', '')}"[:300] for k in (doc.get("checks") or [])
                if k.get("level") != "ok"))
            c.evidence("non-ok checks: " + "; ".join(f"{k['level']} {k['check']}" for k in (doc.get("checks") or [])
                                                      if k.get("level") != "ok") if not self.dry else "dry")
        with self.rep.check("rooms: token file is root-only; the user cannot read it or run operator verbs", self.clock) as c:
            res = self.rem.run(["stat", "-c", "%U:%G %a", "/etc/motel/credentials/hetzner.token"])
            c.evidence(f"token file: {res.out.strip()}")
            c.require(res.out.strip() == "root:root 600", f"unexpected token file ownership/mode: {res.out.strip()!r}")
            res = self.rem.run(["cat", "/etc/motel/credentials/hetzner.token"], as_user=USER)
            c.require(res.rc != 0, "the unprivileged user could read the token file")
            res = self.rem.run(["sudo", "-n", "/usr/local/sbin/motel-desk", "reap", "--json"], as_user=USER)
            c.evidence(f"user running `motel-desk reap`: rc={res.rc}")
            c.require(res.rc != 0, "the unprivileged user could run an operator verb through sudo")
            res = self.rem.run(["sudo", "-n", "/usr/local/sbin/motel-desk", "setup", "hetzner", "--json"], as_user=USER)
            c.require(res.rc != 0, "the unprivileged user could run setup through sudo")
        with self.rep.check("rooms: a user outside allow_users is refused", self.clock) as c:
            res, doc = self.motel("ls", user=OUTSIDER)
            c.evidence(f"{OUTSIDER}: rc={res.rc}")
            c.require(res.rc != 0, "a user outside allow_users could run motel")

    def first_checkin(self, st) -> None:
        with self.rep.check(f"rooms: checkin {self.room_gb}G creates a new floor (volume attached, formatted, mounted)", self.clock) as c:
            before = {str(v["id"]) for v in self.motel_volumes()}
            res, doc = self.motel("checkin", f"{self.room_gb}G", "--for", "2h", "--task", "alpha")
            room = self.room(doc)
            c.require(res.rc == 0 and doc.get("ok"), f"checkin rc={res.rc}: {res.out[:300]} {res.err[:200]}")
            st["A"] = room
            path = room["path"]
            c.require(str(path).startswith(MOUNT_ROOT + "/"), f"room path {path!r} is not under {MOUNT_ROOT}")
            c.require(room["state"] == "active", f"state {room['state']}")
            vols = [v for v in self.motel_volumes() if str(v["id"]) not in before]
            c.require(len(vols) == 1, f"expected one new motel volume, found {len(vols)}")
            vol = vols[0]
            labels = vol.get("labels") or {}
            c.evidence(f"volume {vol['id']} {vol.get('size')} GB labels {labels}")
            c.require(vol.get("size") == self.floor_gb, f"floor volume is {vol.get('size')} GB, expected {self.floor_gb}")
            c.require(labels.get("managed-by") == "motel" and labels.get("role") == "floor", f"labels {labels}")
            c.require(labels.get("host") == str(self.ctx.state["server_id"]), "volume host label is not the test server")
            c.require(cloud._server_ref(vol.get("server")) == str(self.ctx.state["server_id"]), "volume is not attached to the test server")
            st["floor_A"] = room["floor"]
            st["floor_vol"] = str(vol["id"])
        if self.rep.last_failed():
            raise Abort("first checkin failed: nothing else in the rooms scenario can run")
        room = st["A"]
        with self.rep.check("rooms: room is private to the user, writable, and the floor is mounted nodev,nosuid,noatime", self.clock) as c:
            res = self.rem.run(["stat", "-c", "%U %a", room["path"]])
            c.require(res.out.strip() == f"{USER} 700", f"room dir owner/mode {res.out.strip()!r}")
            self.put_file(c, room)
            res = self.rem.read_file(f"{room['path']}/marker.txt", as_user=USER)
            c.require(res.out == "motel-live-data", "file written in the room did not read back")
            res = self.rem.read_file(f"{room['path']}/marker.txt", as_user=OUTSIDER)
            c.require(res.rc != 0, "another user could read a file in the room")
            res = self.rem.run(["findmnt", "-no", "OPTIONS", f"{MOUNT_ROOT}/{room['floor']}"])
            c.evidence(f"mount options: {res.out.strip()}")
            for opt in ("nodev", "nosuid", "noatime"):
                c.require(opt in res.out, f"mount option {opt} missing")
            res = self.rem.run(["grep", "-c", MOUNT_ROOT, "/etc/fstab"])
            c.require(res.out.strip() in ("0", ""), "motel wrote to /etc/fstab")
        with self.rep.check("rooms: ls and status JSON are sane", self.clock) as c:
            res, doc = self.motel("ls")
            c.require(res.rc == 0, f"ls rc={res.rc}")
            ids = [r["id"] for r in (doc.get("rooms") or [])] if not self.dry else []
            c.require(room["id"] in ids or self.dry, f"{room['id']} not in ls")
            cap = doc.get("capacity") or {}
            c.require(cap.get("floors") == 1 and cap.get("total_gb") == self.floor_gb, f"capacity {cap}")
            res, doc2 = self.motel("status", room["id"])
            r2 = self.room(doc2)
            c.require(res.rc == 0 and r2.get("id") == room["id"] and r2.get("state") == "active", f"status {res.out[:200]}")
            res, _ = self.motel("status", "nosuch-ab12")
            c.require(res.rc == 7, f"status of a missing room exited {res.rc}, expected 7")

    def pack_and_suite(self, st) -> None:
        if "A" not in st:
            return
        with self.rep.check("rooms: a second checkin packs onto the same floor (no new volume)", self.clock) as c:
            res, doc = self.motel("checkin", f"{self.room_gb}G", "--for", "2h", "--task", "bravo")
            room = self.room(doc)
            c.require(res.rc == 0, f"checkin rc={res.rc}: {res.out[:300]}")
            st["B"] = room
            c.require(room["floor"] == st["floor_A"], f"room went to floor {room['floor']}, expected {st['floor_A']}")
            c.require(len(self.motel_volumes()) == 1, "a second volume was created")
        with self.rep.check("rooms: --suite gets a floor of its own with an exact size", self.clock) as c:
            res, doc = self.motel("checkin", f"{self.suite_gb}G", "--suite", "--for", "2h", "--task", "suite")
            room = self.room(doc)
            c.require(res.rc == 0, f"checkin rc={res.rc}: {res.out[:300]}")
            st["C"] = room
            c.require(room["floor"] != st["floor_A"] and room.get("suite") is True, "suite shares a floor")
            vols = self.motel_volumes()
            sizes = sorted(v.get("size") for v in vols)
            c.evidence(f"volume sizes: {sizes}")
            c.require(len(vols) == 2 and self.suite_gb in sizes, f"expected 2 volumes incl. {self.suite_gb} GB, got {sizes}")

    def extend_room(self, st) -> None:
        if "A" not in st:
            return
        with self.rep.check("rooms: extend pushes the checkout time out", self.clock) as c:
            before = when(st["A"]["expires"]) if not self.dry else None
            res, doc = self.motel("extend", st["A"]["id"], "--for", "10m")
            room = self.room(doc)
            c.require(res.rc == 0, f"extend rc={res.rc}: {res.out[:300]}")
            c.require(self.dry or when(room["expires"]) > before, "expiry did not move")
            st["A"] = {**st["A"], "expires": room["expires"]}
        with self.rep.check("rooms: over-long checkin and extend are refused with exit 3", self.clock) as c:
            res, _ = self.motel("checkin", "1G", "--for", "8d", "--task", "toolong")
            c.require(res.rc == 3, f"8d checkin exited {res.rc}, expected 3")
            res, _ = self.motel("extend", st["A"]["id"], "--for", "8d")
            c.require(res.rc == 3, f"8d extend exited {res.rc}, expected 3")

    def grow_floor(self, st) -> None:
        """No floor slot left and the shared floor is full: the floor volume grows and the mounted ext4 follows online."""
        if "B" not in st or "C" not in st:
            return
        with self.rep.check("rooms: a full shared floor at the floor cap grows online (volume resized, mounted fs follows)", self.clock) as c:
            self.set_config(rooms_config(min_floor_gb=self.floor_gb, max_floors=2))
            try:
                vol_id = st["floor_vol"]
                before = self.hc.describe("volume", vol_id).get("size") if not self.dry else self.floor_gb
                res, doc = self.motel("checkin", f"{self.room_gb}G", "--for", "2h", "--task", "grow")
                room = self.room(doc)
                c.require(res.rc == 0, f"checkin rc={res.rc}: {res.out[:300]}")
                c.require(self.dry or room["floor"] == st["floor_A"], f"room went to floor {room.get('floor')}, expected {st['floor_A']}")
                c.require(self.dry or len(self.motel_volumes()) == 2, "a new volume was created instead of growing the floor")
                after = self.hc.describe("volume", vol_id).get("size") if not self.dry else before
                res = self.rem.run(["df", "-B1G", "--output=size", f"{MOUNT_ROOT}/{st['floor_A']}"])
                fs_gb = int((res.out.split() or ["0", "0"])[-1]) if not self.dry else after
                c.evidence(f"floor volume {before} GB -> {after} GB; mounted fs {fs_gb} GB")
                c.require(self.dry or after > before, f"floor volume did not grow ({before} -> {after} GB)")
                c.require(self.dry or fs_gb >= after - 1, f"filesystem is {fs_gb} GB on a {after} GB volume: it did not grow")
                if not self.dry:
                    self.put_file(c, room)
                    res, doc = self.motel("doctor")
                    c.require(res.rc == 0, f"doctor after the grow exited {res.rc}: {res.out[:300]}")
                st["G"] = room
            finally:
                self.set_config(rooms_config(min_floor_gb=self.floor_gb))

    def caps_refusal(self, st) -> None:
        if "C" not in st:
            return
        with self.rep.check("rooms: caps refusal exits 3 and makes no provider write", self.clock) as c:
            f, s = self.floor_gb, self.suite_gb
            tiny = rooms_config(min_floor_gb=f, max_floors=4, max_floor_gb=max(f, s + 5), max_total_gb=f + 2 * s)
            self.set_config(tiny)
            try:
                before = sorted((str(v["id"]), v.get("size")) for v in self.hc.list("volume"))
                res, doc = self.motel("checkin", f"{s + 5}G", "--suite", "--task", "capped")
                after = sorted((str(v["id"]), v.get("size")) for v in self.hc.list("volume"))
                c.evidence(f"exit {res.rc}: {doc.get('error') if not self.dry else ''}")
                c.require(res.rc == 3, f"cap-exceeding checkin exited {res.rc}, expected 3: {res.out[:300]}")
                c.require(before == after, f"provider volumes changed: {before} -> {after}")
            finally:
                self.set_config(rooms_config(min_floor_gb=f))

    def busy_refusal(self, st) -> None:
        if "B" not in st:
            return
        room = st["B"]
        with self.rep.check("rooms: checkout --now of a room with an open file is refused (exit 4) and nothing is lost", self.clock) as c:
            path = self.put_file(c, room, "held.txt")
            holder = self.rem.popen(["sh", "-c", HOLD_SCRIPT, "sh", path, HOLD_PID], as_user=USER)
            try:
                wait_until(self.clock, lambda: self.rem.read_file(HOLD_PID, as_user=USER).out.strip(), timeout=90, interval=2,
                           what="the holder process", dry=self.dry)
                res, doc = self.motel("checkout", room["id"], "--now")
                c.require(res.rc == 4, f"checkout --now exited {res.rc}, expected 4: {res.out[:300]}")
                res, doc = self.motel("status", room["id"])
                c.require(self.room(doc).get("state") == "active", "the busy room is no longer active")
                res = self.rem.read_file(path, as_user=USER)
                c.require(res.out == "motel-live-data", "data in the busy room changed")
            finally:
                self.rem.run(["sh", "-c", KILL_SCRIPT, "sh", HOLD_PID], as_user=USER)
                if holder is not None:
                    try:
                        holder.wait(timeout=20)
                    except Exception:  # noqa: BLE001
                        holder.kill()
        with self.rep.check("rooms: once the file is closed, checkout --now purges the room", self.clock) as c:
            res, doc = self.motel("checkout", room["id"], "--now")
            c.require(res.rc == 0, f"checkout --now rc={res.rc}: {res.out[:300]}")
            c.require(self.room(doc).get("state") == "purged", f"state {self.room(doc).get('state')}")
            res = self.rem.run(["test", "-e", room["path"]])
            c.require(res.rc != 0, "the room directory still exists after a purge")
        st.pop("B")

    def lifecycle_walk(self, st) -> None:
        if "A" not in st:
            return
        def mk(task):
            res, doc = self.motel("checkin", "3G", "--for", "2m", "--task", task)
            if res.rc != 0:
                raise AssertionError(f"checkin {task} rc={res.rc}: {res.out[:300]}")
            return self.room(doc)
        with self.rep.check("rooms: walk 1 - dry-run reap changes nothing; active -> grace; data stays; extend in grace revives", self.clock) as c:
            w1, w2 = mk("walka"), mk("walkb")
            st["W1"], st["W2"] = w1, w2
            for room in (w1, w2):
                self.put_file(c, room)
            wait_until(self.clock, lambda: self.room(self.motel("status", w1["id"])[1]).get("state") == "grace",
                       timeout=300, interval=15, what=f"{w1['id']} to enter grace", dry=self.dry)
            res, doc = self.desk("reap")
            c.require(res.rc == 0 and doc.get("apply") is False, f"dry-run reap rc={res.rc}")
            c.evidence("dry-run actions: " + "; ".join((doc.get("actions") or [])[:3]) if not self.dry else "dry")
            res, doc = self.reap_apply()
            c.require(res.rc == 0, f"reap --apply rc={res.rc}: {res.out[:300]}")
            res = self.rem.read_file(f"{w1['path']}/marker.txt", as_user=USER)
            c.require(res.out == "motel-live-data", "a room in grace lost its data")
            res, doc = self.motel("extend", w1["id"], "--for", "30m")
            c.require(res.rc == 0 and self.room(doc).get("state") == "active", f"extend in grace: {res.out[:300]}")
            res = self.rem.read_file(f"{w1['path']}/marker.txt", as_user=USER)
            c.require(res.out == "motel-live-data", "extend in grace lost data")
        with self.rep.check("rooms: walk 2 - grace -> lost & found (data moved out of reach) -> restore brings it back", self.clock) as c:
            w2 = st["W2"]

            def lost():
                self.reap_apply()
                return self.room(self.motel("status", w2["id"])[1]).get("state") == "lost-and-found"
            wait_until(self.clock, lost, timeout=420, interval=30, what=f"{w2['id']} in lost & found", dry=self.dry)
            res = self.rem.run(["test", "-e", f"{w2['path']}/marker.txt"])
            c.evidence(f"holding path: {self.room(self.motel('status', w2['id'])[1]).get('path')}")
            res = self.rem.run(["test", "-e", f"{MOUNT_ROOT}/{w2['floor']}/rooms/{w2['id']}"])
            c.require(res.rc != 0, "the room is still in its original place after moving to lost & found")
            res = self.rem.run(["ls", "-A", f"{MOUNT_ROOT}/{w2['floor']}/.lost-and-found/{w2['id']}"], as_user=USER)
            c.require(res.rc != 0, "the user can list lost & found")
            res, doc = self.motel("restore", w2["id"], "--for", "30m")
            c.require(res.rc == 0 and self.room(doc).get("state") == "active", f"restore: {res.out[:300]}")
            res = self.rem.read_file(f"{w2['path']}/marker.txt", as_user=USER)
            c.require(res.out == "motel-live-data", "restore did not bring the data back")
        with self.rep.check("rooms: walk 3 - checkout -> lost & found -> purged by reap, directory gone", self.clock) as c:
            w2 = st["W2"]
            res, doc = self.motel("checkout", w2["id"])
            c.require(res.rc == 0 and self.room(doc).get("state") == "lost-and-found", f"checkout: {res.out[:300]}")

            def purged():
                self.reap_apply()
                res, doc = self.motel("status", w2["id"])
                return res.rc == 7 or self.room(doc).get("state") == "purged"
            wait_until(self.clock, purged, timeout=420, interval=30, what=f"{w2['id']} to be purged", dry=self.dry)
            res = self.rem.run(["test", "-e", f"{MOUNT_ROOT}/{w2['floor']}/.lost-and-found/{w2['id']}"])
            c.require(res.rc != 0, "purged data is still on disk")
            st.pop("W2")

    def interrupted_acquire(self, st) -> None:
        with self.rep.check("rooms: desk killed mid-checkin (volume created) - reap reconciles with no orphan", self.clock) as c:
            before = {str(v["id"]) for v in self.motel_volumes()}
            window = None
            for _attempt in range(2):
                proc = self.rem.popen([cloud_cli(), "checkin", f"{self.suite_gb}G", "--suite", "--task", "intr", "--json"], as_user=USER)
                try:
                    new = wait_until(self.clock, lambda: [v for v in self.motel_volumes() if str(v["id"]) not in before],
                                     timeout=240, interval=1, what="a new motel volume", dry=self.dry)
                except AssertionError:
                    new = []
                self.rem.run(["pkill", "-KILL", "-f", "[m]otel-desk checkin"])
                if proc is not None:
                    try:
                        proc.wait(timeout=60)
                    except Exception:  # noqa: BLE001
                        proc.kill()
                if new:
                    window = f"killed after the provider create (volume {new[0]['id']})" if not self.dry else "dry"
                    break
            if window is None:
                c.skip("checkin finished before it could be killed; the window was missed twice")
            c.evidence(window)
            leftover = [v for v in self.motel_volumes() if str(v["id"]) not in before]
            res, doc = self.reap_apply()
            c.require(res.rc == 0, f"reap --apply rc={res.rc}: {res.out[:300]}")
            c.evidence("reap actions: " + "; ".join((doc.get("actions") or [])[:4]) if not self.dry else "dry")
            c.require(not doc.get("orphans"), f"orphans after reap: {doc.get('orphans')}")
            res, doc = self.motel("doctor")
            orphan = [k for k in (doc.get("checks") or []) if "unrecorded" in k.get("check", "") and k.get("level") != "ok"]
            c.require(res.rc == 0 and not orphan, f"doctor after reconcile: {orphan or res.out[:200]}")
            # the reconciled floor holds no room, so it is retired after retire_after

            def gone():
                self.reap_apply()
                return not [v for v in self.motel_volumes() if str(v["id"]) not in before]
            wait_until(self.clock, gone, timeout=420, interval=30, what="the interrupted floor to be retired", dry=self.dry)
            c.evidence(f"{len(leftover)} leftover volume(s) at the kill, all retired")

    def reboot(self, st) -> None:
        if "A" not in st:
            return
        room = st["A"]
        with self.rep.check("rooms: after a reboot, reap remounts the floors and data and state are intact", self.clock) as c:
            self.rem.run(["systemctl", "reboot"], timeout=60)
            c.require(self.rem.wait_ssh(gone_first=True), "the server did not come back over ssh")
            self.rem.run(["cloud-init", "status", "--wait"], timeout=300)
            res = self.rem.run(["findmnt", "-n", f"{MOUNT_ROOT}/{room['floor']}"])
            c.evidence(f"mounted right after boot: {'yes' if res.rc == 0 else 'no (expected: nothing is in fstab)'}")
            res, doc = self.motel("status", room["id"])
            c.require(res.rc == 0 and self.room(doc).get("state") == "active", f"status after reboot: {res.out[:300]}")
            res, doc = self.reap_apply()
            c.require(res.rc == 0 and doc.get("ok") is not False, f"reap after reboot rc={res.rc}: {res.out[:300]}")
            res = self.rem.run(["findmnt", "-n", f"{MOUNT_ROOT}/{room['floor']}"])
            c.require(res.rc == 0, "the floor was not remounted by reap")
            res = self.rem.read_file(f"{room['path']}/marker.txt", as_user=USER)
            c.require(res.out == "motel-live-data", "data did not survive the reboot")
            res, doc = self.motel("doctor")
            c.require(res.rc == 0 and doc.get("ok") is True, f"doctor after reboot: {res.out[:300]}")
        with self.rep.check("rooms: reap and checkin keep working after the reboot", self.clock) as c:
            res, doc = self.motel("checkin", "2G", "--for", "2h", "--task", "afterboot")
            c.require(res.rc == 0, f"checkin after reboot rc={res.rc}: {res.out[:300]}")
            st["D"] = self.room(doc)

    def setup_rerun(self, st) -> None:
        with self.rep.check("rooms: re-running setup keeps never-touch to the bystander and does not freeze motel's own volumes", self.clock) as c:
            res = self.ctx.run_setup()
            c.require(res.rc == 0, f"setup rc={res.rc}: {res.err.strip()[:300]}")
            doc = parse_json(self.rem.read_file("/etc/motel/setup-receipt.json"))
            ids = sorted(str(v.get("id")) for v in (doc.get("never_touch") or [])) if not self.dry else []
            want = [str(self.ctx.state["bystander"]["id"])] if not self.dry else []
            c.evidence(f"never_touch ids: {ids}")
            c.require(ids == want, f"never_touch is {ids}, expected {want}")

    def checkout_all_and_retire(self, st) -> None:
        """Every room checked out with --now, then reap retires every floor. Hetzner bills a volume per started hour, so an
        empty SHARED floor is kept until its release window (see ``core.release_bounds``) and only then deleted; a suite floor
        has nothing to pack onto it and is retired as soon as ``retire_after`` is over. Each volume is held to its own bounds."""
        with self.rep.check("rooms: checkout --now of every room, then reap retires every floor (a shared floor in its release "
                            "window, a suite floor after retire_after) and deletes every motel volume", self.clock) as c:
            t_start = self.clock.time()
            res, doc = self.motel("ls")
            listed = (doc.get("rooms") or []) if not self.dry else [{"id": "<room>"}]
            # a floor that holds a listed room is emptied by the checkouts below, so it is empty no earlier than t_start;
            # any other floor may have been empty for a long time, and when it became empty is not shown by any command
            occupied = {r.get("floor") for r in listed}
            for room in listed:
                res = self.rem.motel(USER, "checkout", room["id"], "--now", "--json")
                c.require(res.rc == 0, f"checkout {room['id']} rc={res.rc}: {res.out[:200]}")
            t_end = self.clock.time()
            vols = {} if self.dry else {str(v["id"]): v for v in self.motel_volumes()}
            gone = {}
            last_poll = [self.clock.time()]        # when the volumes were last listed: they were all still there then

            def retired():
                res, doc = self.reap_apply()
                c.require(not doc.get("errors"), f"reap errors: {doc.get('errors')}")
                t = self.clock.time()
                left = {str(v["id"]) for v in self.motel_volumes()}
                for vid in vols:
                    if vid not in left and vid not in gone:
                        gone[vid] = (last_poll[0], t)          # deleted after the previous listing, no later than this one
                last_poll[0] = t
                return not left
            wait_until(self.clock, retired, timeout=self.retire_timeout(vols, t_end), interval=ROOMS_POLL_S,
                       what="every floor to be retired", dry=self.dry)
            for vid, vol in vols.items():
                created = parse_epoch(vol.get("created"))
                labels = vol.get("labels") or {}
                shared = labels.get("suite") != "1"
                lo_after, hi_after = t_start + self.retire_after_s, t_end + self.retire_after_s
                if created is None or not shared:
                    # nothing can pack onto a suite floor: it goes as soon as retire_after is over (a few polls of slack)
                    c.require(gone[vid][0] <= hi_after + 5 * ROOMS_POLL_S,
                              f"suite floor {vid} was still there {int(gone[vid][0] - hi_after)}s after retire_after")
                    continue
                earliest, _ = release_bounds(lo_after - created)
                _, latest = release_bounds(hi_after - created)
                lo, hi = gone[vid]
                c.evidence(f"shared floor {vid}: deleted between {int(lo - created)}s and {int(hi - created)}s after creation; "
                           f"expected {int(earliest)}s to {int(latest)}s")
                if labels.get("floor") not in occupied:
                    # it may have been empty long before this step, so its retire_after may have ended long before t_start:
                    # nothing observable gives the earliest allowed time, and the check for an early delete is skipped
                    c.evidence(f"shared floor {vid} held no listed room before the checkouts: when it became empty is not observable, "
                               f"so the early-delete (lower bound) check is skipped for it")
                else:
                    c.require(hi - created >= earliest, f"shared floor {vid} was deleted at most {int(hi - created)}s after creation, before "
                                                        f"its release window ({int(earliest)}s): an early delete inside a paid hour saves nothing")
                c.require(lo - created <= latest, f"shared floor {vid} was deleted at least {int(lo - created)}s after creation, past the "
                                                  f"end of its paid hour ({int(latest)}s)")
            res, doc = self.reap_apply()
            c.require(not doc.get("orphans") and not doc.get("errors"), f"final reap: orphans={doc.get('orphans')} errors={doc.get('errors')}")
            res = self.rem.run(["sh", "-c", 'ls -A "$1" | wc -l', "sh", MOUNT_ROOT])
            c.require(res.out.strip() in ("0", ""), f"{MOUNT_ROOT} still has entries: {res.out.strip()}")
            res, doc = self.motel("ls")
            c.require(not doc.get("rooms"), f"rooms remain: {doc.get('rooms')}")

    retire_after_s = 120      # rooms_config()'s retire_after

    def retire_timeout(self, vols: dict, t_end: float) -> int:
        """How long to wait: until the latest end of any floor's paid hour, plus the usual 15 minutes."""
        latest = 0.0
        for vol in vols.values():
            created = parse_epoch(vol.get("created"))
            if created is not None:
                latest = max(latest, created + release_bounds(t_end + self.retire_after_s - created)[1] - t_end)
        return int(max(900.0, latest + 900))

    def bystander_untouched(self, st) -> None:
        with self.rep.check("rooms: the pre-existing bystander volume is untouched (id, size, labels, attachment, protection)", self.clock) as c:
            want = self.ctx.state["bystander"]
            now = cloud.snapshot_volume(self.hc.describe("volume", want["id"]))
            c.evidence(f"before {want}\nnow    {now}")
            c.require(self.dry or now == want, "the bystander volume changed")


def cloud_cli() -> str:
    from motel_live.remote import CLI
    return CLI
