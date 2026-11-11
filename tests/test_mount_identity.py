"""A floor's mount point must be a mount of the floor's own verified device; restore and reap edge cases."""

import json
import os
import unittest
from datetime import timedelta
from pathlib import Path

from helpers import Fixture
from motel.errors import Guard, Refused
from motel.rooms import Caller

ROOT_CALLER = Caller(0, 0, "root")


class MountIdentityTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.room = self.fx.checkin(10, task="a")
        self.floor = self.fx.ctx.store.floor(self.room["floor"])
        self.mp = self.fx.rooms.mountpoint(self.floor)
        self.fx.sysops.mounted[self.mp] = "/dev/sdz-some-other-filesystem"
        self.fx.sysops.calls.clear()

    def test_checkout_and_purge_refuse_a_foreign_mount_and_delete_nothing(self):
        (Path(self.room["path"]) / "f").write_text("keep")
        with self.assertRaises(Guard):
            self.fx.rooms.checkout(self.fx.caller, self.room["id"], True)
        self.assertEqual((Path(self.room["path"]) / "f").read_text(), "keep")

    def test_checkin_never_mounts_on_top_of_a_foreign_mount(self):
        with self.assertRaises(Guard):
            self.fx.checkin(10, task="b")
        self.assertFalse([c for c in self.fx.sysops.calls if c[0] == "mount"])

    def test_housekeeping_reports_it_and_touches_nothing(self):
        self.fx.later(hours=40)
        report = self.fx.rooms.reap(True)
        self.assertTrue(any("not from the floor's device" in r for r in report["refused"]), report)
        self.assertTrue(os.path.exists(self.room["path"]))

    def test_restoring_the_right_source_works_again(self):
        self.fx.sysops.mounted[self.mp] = self.fx.provider.device_path(self.floor["volume_id"])
        self.assertEqual(self.fx.rooms.checkout(self.fx.caller, self.room["id"], True)["state"], "purged")

    def test_the_mount_is_verified_right_after_mounting(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        real_mount = fx.sysops.mount
        fx.sysops.mount = lambda dev, mp: (real_mount(dev, mp), fx.sysops.mounted.__setitem__(mp, "/dev/wrong"))
        with self.assertRaises(Guard):
            fx.checkin(10)


class RestoreTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def test_restore_obeys_max_ttl_call(self):
        room = self.fx.checkin(10)
        self.fx.rooms.checkout(self.fx.caller, room["id"], False)
        with self.assertRaises(Refused):
            self.fx.rooms.restore(self.fx.caller, room["id"], timedelta(days=8))
        self.assertEqual(self.fx.rooms.restore(self.fx.caller, room["id"], timedelta(days=7))["state"], "active")

    def test_crash_after_the_record_is_saved_but_before_the_data_moves_loses_nothing(self):
        fx = self.fx
        room = fx.checkin(10)
        (Path(room["path"]) / "f").write_text("keep")
        info = fx.rooms.checkout(fx.caller, room["id"], False)
        record = fx.ctx.store.room(room["id"])
        record.update(state="active", trashed_at=None, trash_reason=None)  # the record was saved; the rename never ran
        fx.ctx.store.save_room(record)
        self.assertTrue(os.path.isdir(info["path"]))
        report = fx.rooms.reap(True)
        self.assertTrue(any("moved back" in a for a in report["actions"]), report)
        self.assertEqual((Path(room["path"]) / "f").read_text(), "keep")

    def test_a_failed_move_puts_the_record_back(self):
        from unittest import mock
        fx = self.fx
        room = fx.checkin(10)
        fx.rooms.checkout(fx.caller, room["id"], False)
        with mock.patch("motel.rooms.os.rename", side_effect=OSError(5, "boom")), self.assertRaises(OSError):
            fx.rooms.restore(fx.caller, room["id"], None)
        self.assertEqual(fx.ctx.store.room(room["id"])["state"], "lost-and-found")

    def test_marker_write_errors_do_not_fail_the_restore(self):
        from unittest import mock
        fx = self.fx
        room = fx.checkin(10)
        fx.rooms.checkout(fx.caller, room["id"], False)
        with mock.patch.object(type(fx.rooms), "_write_marker", side_effect=OSError(30, "read-only")):
            self.assertEqual(fx.rooms.restore(fx.caller, room["id"], None)["state"], "active")


class HostIdentityReapTest(unittest.TestCase):
    def test_a_wrong_server_is_an_error_and_the_room_phase_is_skipped(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        room = fx.checkin(10, hours=1)
        fx.later(days=3)
        fx.provider.server_id = "424242"
        code, out, _ = fx.desk("reap", "--apply", "--json", caller=ROOT_CALLER)
        doc = json.loads(out)
        self.assertEqual(code, 5)
        self.assertFalse(doc["ok"])
        self.assertTrue(any("host identity" in e for e in doc["errors"]))
        self.assertEqual(fx.ctx.store.room(room["id"])["state"], "active")  # nothing was touched
        self.assertFalse(fx.provider.ops("detach") or fx.provider.ops("delete"))


if __name__ == "__main__":
    unittest.main()
