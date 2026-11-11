"""Round 2: holder scan timeout, filesystem growth after a provider resize, long refusals, healing, entry files."""

import os
import shutil
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path

from helpers import Fixture, toml_with
from motel import fsutil, installer
from motel.errors import Busy, ProviderError
from motel.sysops import RealSysOps, ScanTimeout, scan_holders_guarded


class ScanTimeoutTest(unittest.TestCase):
    def test_a_hung_scan_is_busy_not_idle(self):
        ops = RealSysOps()
        ops.scan_argv = [sys.executable, "-c", "import time; time.sleep(60)"]
        ops.scan_timeout = 0.4
        started = time.monotonic()
        with self.assertRaises(ScanTimeout) as raised:
            ops.holders("/mnt/x/rooms/r1", "/mnt/x")
        self.assertLess(time.monotonic() - started, 10)
        self.assertIsInstance(raised.exception, Busy)
        self.assertEqual(raised.exception.code, 4)
        self.assertIn("hung", raised.exception.message)

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs /proc")
    def test_the_child_runs_without_the_parents_descriptors(self):
        with tempfile.TemporaryFile() as lock:
            code = ("import os,sys,json; json.dump(['%d fd' % int(os.path.exists('/proc/self/fd/%d' % int(sys.argv[1])))], sys.stdout)")
            out = scan_holders_guarded("/proc", "/x", None, argv=[sys.executable, "-c", code, str(lock.fileno())])
        self.assertEqual(out, ["0 fd"])

    def test_a_failing_or_garbled_scan_is_an_error_never_an_empty_answer(self):
        from motel.errors import SystemFault
        for code in ("import sys; sys.exit(3)", "print('not json')", "print('{\"a\": 1}')"):
            with self.assertRaises(SystemFault):
                scan_holders_guarded("/proc", "/x", None, argv=[sys.executable, "-c", code])

    @unittest.skipUnless(os.path.isdir("/proc/self"), "needs /proc")
    def test_the_real_child_scan_finds_this_process(self):
        with tempfile.TemporaryDirectory() as tmp:
            room = Path(tmp, "room")
            room.mkdir()
            with open(room / "f", "w"):
                found = RealSysOps().holders(str(room), tmp)
            self.assertTrue(any(h.startswith(f"{os.getpid()} ") for h in found), found)

    def test_a_timed_out_scan_stops_checkout_and_changes_nothing(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        room = fx.checkin(10)

        def hang(path, floor_mp=None):
            raise ScanTimeout("could not tell whether it is in use", "motel doctor")

        fx.sysops.holders = hang
        with self.assertRaises(Busy):
            fx.rooms.checkout(fx.caller, room["id"], False)
        self.assertTrue(os.path.isdir(room["path"]))
        self.assertEqual(fx.rooms.status(fx.caller, room["id"])["state"], "active")


class DeviceMountsTest(unittest.TestCase):
    def test_a_missing_device_link_is_not_mounted_anywhere(self):
        self.assertEqual(RealSysOps().device_mounts("/dev/disk/by-id/does-not-exist-motel"), [])


class GrowFilesystemTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(toml_with(max_floors=1))
        self.addCleanup(self.fx.close)
        self.fx.checkin(40)

    def floor(self):
        return self.fx.ctx.store.floors()[0]

    def grows(self):
        return [c for c in self.fx.sysops.calls if c[0] == "grow_fs"]

    def test_an_indeterminate_resize_that_happened_still_grows_the_filesystem(self):
        # r7: the response was lost, the resize happened; reap records the size AND grows the filesystem
        self.fx.provider.fail_next["resize"] = ProviderError("timeout", indeterminate=True)
        with self.assertRaises(ProviderError):
            self.fx.checkin(30)
        vid = self.floor()["volume_id"]
        self.fx.provider.volumes[vid] = replace(self.fx.provider.volumes[vid], size_gb=70)
        self.assertEqual(self.grows(), [])
        self.fx.rooms.reap(True)
        self.assertEqual(self.floor()["size_gb"], 70)
        self.assertEqual([c[1] for c in self.grows()], [self.fx.provider.device_path(vid)])
        self.assertNotIn("fs_grow_pending", self.floor())
        self.assertNotIn("resize_pending", self.floor())
        self.fx.rooms.reap(True)
        self.assertEqual(len(self.grows()), 1)  # done once

    def test_an_out_of_band_size_increase_grows_it_too(self):
        vid = self.floor()["volume_id"]
        self.fx.provider.volumes[vid] = replace(self.fx.provider.volumes[vid], size_gb=80)
        self.fx.rooms.reap(True)
        self.assertEqual(self.floor()["size_gb"], 80)
        self.assertEqual(len(self.grows()), 1)

    def test_a_failed_growth_is_retried_by_the_next_run(self):
        vid = self.floor()["volume_id"]
        self.fx.provider.volumes[vid] = replace(self.fx.provider.volumes[vid], size_gb=80)
        real = self.fx.sysops.grow_fs

        def broken(dev):
            raise ProviderError("resize2fs failed")

        self.fx.sysops.grow_fs = broken
        report = self.fx.rooms.reap(True)
        self.assertFalse(report["ok"])
        self.assertTrue(self.floor()["fs_grow_pending"])
        self.fx.sysops.grow_fs = real
        self.fx.rooms.reap(True)
        self.assertNotIn("fs_grow_pending", self.floor())
        self.assertEqual(len(self.grows()), 1)

    def test_growth_waits_for_the_mount_when_the_floor_is_not_mounted(self):
        vid = self.floor()["volume_id"]
        mp = self.fx.rooms.mountpoint(self.floor())
        del self.fx.sysops.mounted[mp]
        shutil.rmtree(mp)  # the fake "mount" is an ordinary directory: empty it, as an unmounted mount point is
        os.mkdir(mp)
        self.fx.provider.volumes[vid] = replace(self.fx.provider.volumes[vid], size_gb=80)
        floor = self.floor()
        floor["fs_grow_pending"] = True
        self.fx.ctx.store.save_floor(floor)
        self.fx.rooms.ensure_mounted(self.floor())
        self.assertEqual(len(self.grows()), 1)
        self.assertNotIn("fs_grow_pending", self.floor())


class LongRefusalTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.room = self.fx.checkin(10, hours=1)
        self.fx.sysops.busy = {self.room["path"]: ["4242 editor"]}
        self.fx.later(hours=1 + 6 + 1)  # past the grace period: reap wants to move it to lost & found

    def record(self):
        return self.fx.ctx.store.room(self.room["id"])

    def test_a_refusal_is_a_note_at_first_and_an_error_after_24_hours(self):
        report = self.fx.rooms.reap(True)
        self.assertTrue(report["ok"])
        self.assertTrue(any("in use" in r for r in report["refused"]))
        since = self.record()["refused_since"]
        self.fx.later(hours=23)
        report = self.fx.rooms.reap(True)
        self.assertTrue(report["ok"])
        self.assertEqual(self.record()["refused_since"], since)  # the first refusal is remembered, not renewed
        self.fx.later(hours=2)
        report = self.fx.rooms.reap(True)
        self.assertFalse(report["ok"])
        self.assertTrue(any("more than 24 h" in e and self.room["id"] in e for e in report["errors"]))

    def test_a_cleared_refusal_starts_over(self):
        self.fx.rooms.reap(True)
        self.assertIn("refused_since", self.record())
        self.fx.sysops.busy = {}
        self.fx.rooms.reap(True)
        self.assertNotIn("refused_since", self.record())

    def test_a_dry_run_records_nothing(self):
        self.fx.rooms.reap(False)
        self.assertNotIn("refused_since", self.record())

    def test_the_exit_code_of_the_desk_is_non_zero_and_doctor_warns(self):
        self.fx.rooms.reap(True)
        self.fx.later(hours=25)
        code, out, _ = self.fx.desk("reap", "--apply", "--json", caller=type(self.fx.caller)(0, 0, "root"))
        self.assertNotEqual(code, 0)
        code, out, _ = self.fx.desk("doctor", "--json")
        import json
        check = next(c for c in json.loads(out)["checks"] if c["check"] == "no housekeeping refusal older than 24 h")
        self.assertEqual(check["level"], "warn")
        self.assertIn(self.room["id"], check["detail"])

    def test_a_guard_refusal_counts_too(self):
        self.fx.sysops.busy = {}
        floor = self.fx.ctx.store.floors()[0]
        self.fx.sysops.serial_override[self.fx.provider.device_path(floor["volume_id"])] = "someone-else"
        self.fx.sysops.mounted.pop(self.fx.rooms.mountpoint(floor), None)  # forces a re-verification at the next mount
        self.fx.rooms.reap(True)
        self.fx.later(hours=25)
        report = self.fx.rooms.reap(True)
        self.assertFalse(report["ok"])


class HealRoomTest(unittest.TestCase):
    def test_a_foreign_mount_on_the_floor_path_is_never_touched(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        room = fx.checkin(10)
        floor = fx.ctx.store.floors()[0]
        mp = fx.rooms.mountpoint(floor)
        trash = fx.rooms.trash_path(floor, room["id"])
        os.makedirs(trash)
        shutil.rmtree(room["path"])  # a restore crashed after moving the data to lost & found
        fx.sysops.mounted[mp] = "/dev/someone-elses-disk"
        report = fx.rooms.reap(True)
        self.assertTrue(os.path.isdir(trash))
        self.assertFalse(os.path.lexists(room["path"]))
        self.assertTrue(any("not from the floor's device" in r for r in report["refused"]), report)


class ReconcileTest(unittest.TestCase):
    def test_a_failed_record_is_not_adopted(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        vol = fx.provider.create_volume("motel-f-aaaa-bbbb", 50, "fsn1",
                                        {"managed-by": "motel", "role": "floor", "host": fx.provider.server_id, "floor": "f-aaaa"},
                                        server_id=None)
        floor = {"schema": "motel/v1", "id": "f-aaaa", "name": vol.name, "labels": {}, "volume_id": None, "size_gb": 50,
                 "suite": False, "state": "failed", "created": "2026-01-01T00:00:00Z", "idle_since": None, "first_task": "x"}
        fx.ctx.store.save_floor(floor)
        fx.rooms.reconcile()
        self.assertIsNone(fx.ctx.store.floor("f-aaaa")["volume_id"])
        _, orphans = fx.rooms.reconcile()
        self.assertEqual([v.id for v in orphans], [vol.id])


class EntryFileAuditTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "motel-desk")
        with open(self.path, "w") as handle:
            handle.write(installer.render_entry("motel.desk"))
        os.chmod(self.path, 0o755)
        self.uid = os.getuid()

    def audit(self, module="motel.desk"):
        return installer.audit_entry(self.path, module, owner_uid=self.uid)

    def test_the_installed_entry_is_clean(self):
        self.assertEqual(self.audit(), [])

    def test_modified_content_is_reported(self):
        with open(self.path, "a") as handle:
            handle.write("import os; os.system('id')\n")
        self.assertTrue(any("content differs" in p for p in self.audit()))

    def test_wrong_module_is_reported(self):
        self.assertTrue(any("content differs" in p for p in self.audit("motel.cli")))

    def test_writable_by_group_or_others_is_reported(self):
        os.chmod(self.path, 0o775)
        self.assertTrue(any("writable" in p for p in self.audit()))

    def test_wrong_owner_is_reported(self):
        self.assertTrue(any("owned by uid" in p for p in installer.audit_entry(self.path, "motel.desk", owner_uid=self.uid + 1)))

    def test_a_symlink_or_missing_file_is_reported(self):
        link = os.path.join(self.tmp.name, "link")
        os.symlink(self.path, link)
        self.assertEqual(installer.audit_entry(link, "motel.desk", owner_uid=self.uid), [f"{link}: not a regular file"])
        self.assertTrue(installer.audit_entry(os.path.join(self.tmp.name, "nope"), "motel.desk"))


class RmtreeEdgeCasesTest(unittest.TestCase):
    def test_symlink_loop_fifo_and_newline_names(self):
        # r7: a tree with a symlink to outside, a symlink loop, a fifo and a newline in a name
        with tempfile.TemporaryDirectory() as tmp:
            base = Path(tmp)
            outside = base / "outside"
            outside.mkdir()
            (outside / "keep").write_text("x")
            top = base / "trash" / "r-abcd"
            (top / "a" / "b" / "c").mkdir(parents=True)
            os.symlink(outside, top / "a" / "link-to-outside")
            os.symlink(top / "a", top / "a" / "b" / "loop")
            os.mkfifo(top / "a" / "fifo")
            (top / "a" / "b" / "c" / "weird \n name").write_text("x")
            count = fsutil.safe_rmtree(str(top), expect_dev=os.stat(base).st_dev)
            self.assertGreaterEqual(count, 8)
            self.assertFalse(os.path.lexists(top))
            self.assertEqual(os.listdir(outside), ["keep"])


if __name__ == "__main__":
    unittest.main()
