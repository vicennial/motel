"""Room lifecycle and placement, ported from the original lease tests and extended."""

import json
import os
import unittest
from datetime import timedelta
from pathlib import Path

from helpers import SERVER, Fixture, toml_with
from motel.errors import Busy, Guard, NotFound, ProviderError, Refused, Usage


class CheckinTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def test_first_room_creates_a_shared_floor_and_a_private_dir(self):
        room = self.fx.checkin(20, task="video-render")
        [create] = self.fx.provider.ops("create")
        self.assertEqual(create[2], 50)  # shared floors start at min_floor_gb
        labels = create[3]
        self.assertEqual((labels["managed-by"], labels["role"], labels["host"]), ("motel", "floor", SERVER))
        self.assertTrue(create[1].startswith("motel-"))
        self.assertEqual(create[4], SERVER)
        self.assertTrue(room["id"].startswith("video-render-") and len(room["id"].split("-")[-1]) == 4)
        self.assertEqual(os.stat(room["path"]).st_mode & 0o777, 0o700)
        marker = json.loads((Path(room["path"]) / ".motel-room.json").read_text())
        self.assertEqual((marker["id"], marker["owner"], marker["size_gb"]), (room["id"], "alice", 20))
        self.assertEqual(marker["expires"], room["expires"])
        self.assertIn("DISPOSABLE", (Path(room["path"]) / "MOTEL-ROOM.txt").read_text())
        self.assertEqual(room["expires"], "2026-01-01T18:00:00Z")
        self.assertEqual(room["time_left_h"], 6.0)
        mp = self.fx.rooms.mountpoint(self.fx.ctx.store.floor(room["floor"]))
        manifest = json.loads((Path(mp) / ".motel" / "manifest.json").read_text())
        self.assertTrue(manifest["disposable"])
        self.assertEqual(self.fx.sysops.chowns[-1][1:], (self.fx.uid, self.fx.uid))

    def test_mount_options_come_from_sysops_and_floor_is_prepared_once(self):
        self.fx.checkin(10)
        self.fx.checkin(10)
        self.assertEqual(len([c for c in self.fx.sysops.calls if c[0] == "mount"]), 1)
        self.assertEqual(len([c for c in self.fx.sysops.calls if c[0] == "prepare_fs"]), 1)

    def test_second_room_shares_the_floor_when_it_fits(self):
        a, b = self.fx.checkin(20), self.fx.checkin(30)
        self.assertEqual(a["floor"], b["floor"])
        self.assertEqual(len(self.fx.provider.ops("create")), 1)

    def test_room_that_does_not_fit_gets_a_new_floor(self):
        a, b = self.fx.checkin(40), self.fx.checkin(20)
        self.assertNotEqual(a["floor"], b["floor"])

    def test_suite_gets_its_own_exact_size_floor_that_is_never_shared(self):
        a = self.fx.checkin(120, suite=True)
        self.assertEqual(self.fx.provider.volumes[self.fx.ctx.store.floor(a["floor"])["volume_id"]].size_gb, 120)
        b = self.fx.checkin(10)
        self.assertNotEqual(a["floor"], b["floor"])

    def test_at_the_floor_cap_an_existing_floor_grows(self):
        fx = Fixture(toml_with(max_floors=1))
        self.addCleanup(fx.close)
        a, b = fx.checkin(40), fx.checkin(30)
        self.assertEqual(a["floor"], b["floor"])
        vid = fx.ctx.store.floor(a["floor"])["volume_id"]
        self.assertIn(("resize", vid, 70), fx.provider.calls)
        self.assertTrue(any(c[0] == "grow_fs" for c in fx.sysops.calls))

    def test_fixed_names_and_sizes_are_validated(self):
        for task in ("", "../x", "a b", "x" * 33):
            with self.assertRaises(Usage):
                self.fx.checkin(10, task=task)

    def test_room_ids_are_unique_and_readable(self):
        ids = {self.fx.checkin(1, task="t")["id"] for _ in range(20)}
        self.assertEqual(len(ids), 20)

    def test_wrong_server_refuses_everything(self):
        self.fx.provider.server_id = "42"
        with self.assertRaises(Guard):
            self.fx.checkin(10)
        self.assertFalse(self.fx.provider.ops("create"))

    def test_placement_skips_a_floor_that_is_really_full(self):
        a = self.fx.checkin(10)
        self.fx.sysops.free_gb = 5  # less than 90% of the request is physically free
        b = self.fx.checkin(10)
        self.assertNotEqual(a["floor"], b["floor"])


class CapsTest(unittest.TestCase):
    def test_caps_refuse_before_any_provider_call(self):
        for toml, size, kw in ((toml_with(max_floor_gb=100), 101, {}), (toml_with(max_total_gb=100, max_floor_gb=100), 60, {"twice": True}),
                               (toml_with(monthly_eur=2), 60, {}), (toml_with(max_floors=1), 40, {"twice_suite": True})):
            fx = Fixture(toml)
            self.addCleanup(fx.close)
            if kw.get("twice"):
                fx.checkin(60, suite=True)
                before = len(fx.provider.ops("create"))
                with self.assertRaises(Refused):
                    fx.checkin(60, suite=True)
            elif kw.get("twice_suite"):
                fx.checkin(40, suite=True)
                before = len(fx.provider.ops("create"))
                with self.assertRaises(Refused):
                    fx.checkin(40, suite=True)
            else:
                before = 0
                with self.assertRaises(Refused):
                    fx.checkin(size)
            self.assertEqual(len(fx.provider.ops("create")), before)

    def test_monthly_cost_is_projected_from_size_times_price(self):
        fx = Fixture(toml_with(monthly_eur=10))  # 0.05 EUR per GB-month: 200 GB would cost 10, 210 would not
        self.addCleanup(fx.close)
        fx.checkin(200, suite=True)
        with self.assertRaises(Refused) as ctx:
            fx.checkin(10, suite=True)
        self.assertIn("monthly cap", str(ctx.exception))
        self.assertTrue(ctx.exception.line().endswith("motel ls --mine"))

    def test_error_for_no_vacancy_names_the_cap_and_the_next_command(self):
        fx = Fixture(toml_with(max_total_gb=100, max_floor_gb=100))
        self.addCleanup(fx.close)
        fx.checkin(90, suite=True)
        with self.assertRaises(Refused) as ctx:
            fx.checkin(50, suite=True)
        self.assertIn("no vacancy", ctx.exception.line())

    def test_intents_reserve_capacity(self):
        fx = Fixture(toml_with(max_total_gb=100, max_floor_gb=100))
        self.addCleanup(fx.close)
        fx.provider.drop_next_create = True
        with self.assertRaises(ProviderError):
            fx.checkin(80, suite=True)
        with self.assertRaises(Refused):  # the lost create still counts
            fx.checkin(80, suite=True)

    def test_ttl_limits(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        with self.assertRaises(Refused):
            fx.checkin(10, hours=24 * 8)
        room = fx.checkin(10)
        with self.assertRaises(Refused):
            fx.rooms.extend(fx.caller, room["id"], timedelta(days=8))


class LifecycleTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def test_active_grace_lost_and_found_purged_then_floor_retired(self):
        fx = self.fx
        room = fx.checkin(10)
        (Path(room["path"]) / "data.bin").write_text("x")
        fx.later(hours=4, minutes=30)
        self.assertTrue(any("checks out" in w for w in fx.rooms.reap(True)["warnings"]))
        self.assertFalse(fx.rooms.reap(True)["warnings"])  # warned once
        fx.later(hours=2)
        fx.rooms.reap(True)
        self.assertEqual(fx.rooms.status(fx.caller, room["id"])["state"], "grace")
        self.assertTrue(os.path.exists(room["path"]))  # still usable in grace
        fx.later(hours=6)
        fx.rooms.reap(True)
        info = fx.rooms.status(fx.caller, room["id"])
        self.assertEqual((info["state"], info["lost_and_found_reason"]), ("lost-and-found", "expired"))
        self.assertFalse(os.path.exists(room["path"]))
        self.assertEqual((Path(info["path"]) / "data.bin").read_text(), "x")
        self.assertEqual(len(fx.provider.volumes), 1)  # the floor is kept while lost & found holds data
        fx.later(hours=24)
        fx.rooms.reap(True)
        self.assertEqual(fx.rooms.status(fx.caller, room["id"])["state"], "purged")
        fx.rooms.reap(True)  # starts the idle clock
        self.assertEqual(len(fx.provider.volumes), 1)
        fx.later(minutes=16)
        fx.rooms.reap(True)
        self.assertFalse(fx.provider.volumes)
        self.assertEqual([c[0] for c in fx.provider.calls if c[0] in ("detach", "delete")], ["detach", "delete"])
        self.assertTrue(any(c[0] == "umount" for c in fx.sysops.calls))

    def test_extend_in_grace_revives_the_room(self):
        fx = self.fx
        room = fx.checkin(10, hours=1)
        fx.later(hours=2)
        fx.rooms.reap(True)
        out = fx.rooms.extend(fx.caller, room["id"], timedelta(hours=6))
        self.assertEqual(out["state"], "active")
        self.assertAlmostEqual(out["time_left_h"], 6.0)
        marker = json.loads((Path(room["path"]) / ".motel-room.json").read_text())
        self.assertEqual(marker["expires"], out["expires"])

    def test_extend_is_capped_by_lifetime(self):
        fx = Fixture(toml_with(max_lifetime='"30h"', max_ttl_call='"20h"', default_ttl='"6h"'))
        self.addCleanup(fx.close)
        room = fx.checkin(10)
        self.assertEqual(fx.rooms.extend(fx.caller, room["id"], timedelta(hours=12))["expires"], "2026-01-02T06:00:00Z")
        self.assertEqual(fx.rooms.extend(fx.caller, room["id"], timedelta(hours=20))["expires"], "2026-01-02T18:00:00Z")

    def test_checkout_restore_and_checkout_now(self):
        fx = self.fx
        room = fx.checkin(10)
        (Path(room["path"]) / "f").write_text("keep")
        out = fx.rooms.checkout(fx.caller, room["id"], False)
        self.assertEqual((out["state"], out["lost_and_found_reason"]), ("lost-and-found", "checkout"))
        self.assertFalse(os.path.exists(room["path"]))
        back = fx.rooms.restore(fx.caller, room["id"], timedelta(hours=2))
        self.assertEqual((Path(back["path"]) / "f").read_text(), "keep")
        gone = fx.rooms.checkout(fx.caller, room["id"], True)
        self.assertEqual(gone["state"], "purged")
        self.assertFalse(os.path.exists(Path(fx.rooms.mountpoint(fx.ctx.store.floor(room["floor"]))) / ".lost-and-found" / room["id"]))
        with self.assertRaises(Refused):
            fx.rooms.restore(fx.caller, room["id"], None)

    def test_lost_and_found_zero_purges_at_checkout(self):
        fx = Fixture(toml_with(lost_and_found='"0h"'))
        self.addCleanup(fx.close)
        room = fx.checkin(10)
        self.assertEqual(fx.rooms.checkout(fx.caller, room["id"], False)["state"], "purged")

    def test_busy_room_is_never_moved_and_nothing_is_killed(self):
        fx = self.fx
        room = fx.checkin(10, hours=1)
        fx.sysops.busy[room["path"] + "/x"] = ["4242 python"]
        fx.later(hours=8)
        report = fx.rooms.reap(True)
        self.assertTrue(any("4242" in r for r in report["refused"]))
        self.assertTrue(os.path.exists(room["path"]))
        with self.assertRaises(Busy):
            fx.rooms.checkout(fx.caller, room["id"], False)
        fx.sysops.busy.clear()
        fx.rooms.reap(True)
        self.assertEqual(fx.rooms.status(fx.caller, room["id"])["state"], "lost-and-found")

    def test_dry_run_changes_nothing(self):
        fx = self.fx
        room = fx.checkin(10, hours=1)
        fx.later(days=5)
        report = fx.rooms.reap(False)
        self.assertTrue(report["actions"])
        self.assertEqual(fx.ctx.store.room(room["id"])["state"], "active")
        self.assertEqual(len(fx.provider.volumes), 1)

    def test_reboot_remounts_floors_with_live_rooms(self):
        fx = self.fx
        room = fx.checkin(10)
        mp = fx.rooms.mountpoint(fx.ctx.store.floor(room["floor"]))
        fx.sysops.mounted.clear()
        for child in Path(mp).iterdir():  # an unmounted mount point is empty
            os.rename(child, fx.root / f"saved-{child.name}")
        fx.rooms.reap(True)
        self.assertIn(mp, fx.sysops.mounted)

    def test_nothing_is_touched_when_a_floor_is_not_mounted(self):
        fx = self.fx
        room = fx.checkin(10)
        floor = fx.ctx.store.floor(room["floor"])
        fx.sysops.mounted.clear()
        with self.assertRaises(Guard):  # an unmounted mount point must never be written to or deleted from
            fx.rooms._require_mounted(floor)

    def test_other_users_cannot_touch_my_room(self):
        from motel.rooms import Caller
        fx = self.fx
        room = fx.checkin(10)
        eve = Caller(uid=fx.uid + 1, gid=fx.uid + 1, name="eve")
        for call in (lambda: fx.rooms.extend(eve, room["id"], timedelta(hours=1)), lambda: fx.rooms.checkout(eve, room["id"], True),
                     lambda: fx.rooms.restore(eve, room["id"], None), lambda: fx.rooms.status(eve, room["id"])):
            with self.assertRaises(Refused):
                call()
        root = Caller(uid=0, gid=0, name="root")
        self.assertEqual(fx.rooms.status(root, room["id"])["id"], room["id"])
        self.assertEqual(fx.rooms.ls(eve, False)["rooms"], [])
        self.assertEqual(len(fx.rooms.ls(eve, True)["rooms"]), 1)

    def test_unknown_room(self):
        with self.assertRaises(NotFound):
            self.fx.rooms.status(self.fx.caller, "nope-ab12")

    def test_guest_book_records_the_story(self):
        room = self.fx.checkin(10)
        self.fx.rooms.checkout(self.fx.caller, room["id"], True)
        events = [json.loads(line)["event"] for line in (Path(self.fx.paths.state_dir) / "guestbook.jsonl").read_text().splitlines()]
        for event in ("floor-intent", "floor-created", "room-checked-in", "room-lost-and-found", "room-purged"):
            self.assertIn(event, events)


if __name__ == "__main__":
    unittest.main()
