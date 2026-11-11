"""Housekeeping must survive hostile or accidental room content: deep trees, nested mounts, one bad room."""

import contextlib
import os
import resource
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from helpers import Fixture
from motel import fsutil
from motel.errors import Busy, Guard
from motel.rooms import Caller
from motel.sysops import mounts_at_or_under, parse_mountinfo

ROOT_CALLER = Caller(0, 0, "root")


def deep_chain(top: str, depth: int) -> None:
    """Build `depth` nested directories with one descriptor at a time, like any user can."""
    fd = os.open(top, os.O_RDONLY)
    try:
        for _ in range(depth):
            os.mkdir("d", dir_fd=fd)
            nxt = os.open("d", os.O_RDONLY, dir_fd=fd)
            os.close(fd)
            fd = nxt
        with open("leaf", "w", opener=lambda p, f: os.open(p, f, 0o600, dir_fd=fd)) as handle:
            handle.write("x")
    finally:
        os.close(fd)


@contextlib.contextmanager
def low_fd_limit(limit=128):
    """``limit`` descriptors of headroom above the highest one already open (pytest and earlier tests may hold some)."""
    soft, hard = resource.getrlimit(resource.RLIMIT_NOFILE)
    fd_dir = "/proc/self/fd" if os.path.isdir("/proc/self/fd") else "/dev/fd"
    highest = max((int(n) for n in os.listdir(fd_dir) if n.isdigit()), default=2)
    resource.setrlimit(resource.RLIMIT_NOFILE, (highest + 1 + limit, hard))
    try:
        yield
    finally:
        resource.setrlimit(resource.RLIMIT_NOFILE, (soft, hard))


class SafeRmtreeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def test_removes_files_and_nested_directories(self):
        top = self.base / "t"
        (top / "a" / "b").mkdir(parents=True)
        (top / "a" / "b" / "f").write_text("x")
        (top / "g").write_text("y")
        (top / "empty").mkdir()
        self.assertGreaterEqual(fsutil.safe_rmtree(str(top)), 5)
        self.assertFalse(top.exists())

    def test_a_3000_deep_tree_with_a_small_descriptor_limit(self):
        top = self.base / "deep"
        top.mkdir()
        deep_chain(str(top), 3000)
        with low_fd_limit(64):
            fsutil.safe_rmtree(str(top))
        self.assertFalse(top.exists())

    def test_wide_trees_are_not_quadratic(self):
        top = self.base / "wide"
        top.mkdir()
        for i in range(3000):
            (top / f"d{i}").mkdir()
        fsutil.safe_rmtree(str(top))
        self.assertFalse(top.exists())

    def test_symlinks_are_removed_not_followed(self):
        outside = self.base / "outside"
        outside.mkdir()
        (outside / "precious").write_text("keep")
        top = self.base / "t"
        top.mkdir()
        os.symlink(outside, top / "link_to_dir")
        os.symlink(outside / "precious", top / "link_to_file")
        fsutil.safe_rmtree(str(top))
        self.assertEqual((outside / "precious").read_text(), "keep")
        self.assertFalse(top.exists())

    def test_a_symlinked_top_is_refused(self):
        real = self.base / "real"
        real.mkdir()
        (real / "f").write_text("keep")
        os.symlink(real, self.base / "top")
        with self.assertRaises(OSError):
            fsutil.safe_rmtree(str(self.base / "top"))
        self.assertTrue((real / "f").exists())

    def test_it_never_leaves_the_filesystem(self):
        top = self.base / "t"
        (top / "mounted_here").mkdir(parents=True)
        (top / "mounted_here" / "other_fs_file").write_text("keep")
        (top / "plain").write_text("x")
        inode = os.stat(top / "mounted_here").st_ino
        other_device = lambda st: 999 if st.st_ino == inode else st.st_dev  # noqa: E731
        with self.assertRaises(Guard):
            fsutil.safe_rmtree(str(top), dev_of=other_device)
        self.assertTrue((top / "mounted_here" / "other_fs_file").exists())

    def test_the_top_must_be_on_the_expected_device(self):
        top = self.base / "t"
        top.mkdir()
        (top / "f").write_text("x")
        with self.assertRaises(Guard):
            fsutil.safe_rmtree(str(top), expect_dev=os.stat(top).st_dev + 1)
        self.assertTrue((top / "f").exists())


class MountinfoTest(unittest.TestCase):
    TEXT = ("36 35 98:0 /mnt1 /mnt/motel/f-1 rw,noatime master:1 - ext4 /dev/sdb rw\n"
            "37 36 98:1 / /mnt/motel/f-1/rooms/a-abcd/sub\\040dir rw - tmpfs tmpfs rw\n"
            "38 35 8:1 / /mnt/motel/f-10 rw - ext4 /dev/sdc rw\n")

    def test_parse_and_filter(self):
        entries = parse_mountinfo(self.TEXT)
        self.assertEqual(entries[1], ("98:1", "/mnt/motel/f-1/rooms/a-abcd/sub dir"))
        self.assertEqual(mounts_at_or_under(entries, "/mnt/motel/f-1/rooms/a-abcd"), ["/mnt/motel/f-1/rooms/a-abcd/sub dir"])
        self.assertEqual(mounts_at_or_under(entries, "/mnt/motel/f-1"), ["/mnt/motel/f-1", "/mnt/motel/f-1/rooms/a-abcd/sub dir"])
        self.assertEqual(mounts_at_or_under(entries, "/mnt/motel/f-1/rooms/other"), [])  # f-10 is not under f-1


class HousekeepingSurvivalTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def past_everything(self):
        self.fx.later(hours=6 + 6 + 24 + 1)

    def test_a_mount_under_a_room_blocks_checkout_and_housekeeping(self):
        room = self.fx.checkin(10, hours=1)
        self.fx.sysops.nested_mounts = [room["path"] + "/inner"]
        with self.assertRaises(Busy) as raised:
            self.fx.rooms.checkout(self.fx.caller, room["id"], False)
        self.assertIn("mount", raised.exception.message)
        self.fx.later(hours=9)
        report = self.fx.rooms.reap(True)
        self.assertTrue(any("mount" in r for r in report["refused"]))
        self.assertTrue(os.path.exists(room["path"]))
        self.fx.sysops.nested_mounts = []
        self.fx.rooms.reap(True)
        self.assertEqual(self.fx.rooms.status(self.fx.caller, room["id"])["state"], "lost-and-found")

    def test_a_mount_under_a_room_in_lost_and_found_blocks_the_purge(self):
        room = self.fx.checkin(10)
        info = self.fx.rooms.checkout(self.fx.caller, room["id"], False)
        self.fx.sysops.nested_mounts = [info["path"] + "/inner"]
        with self.assertRaises(Busy):
            self.fx.rooms.checkout(self.fx.caller, room["id"], True)
        self.assertTrue(os.path.isdir(info["path"]))

    def test_one_bad_room_does_not_stop_the_others_or_the_floors(self):
        fx = self.fx
        bad = fx.checkin(10, task="bad", suite=True)
        good = fx.checkin(10, task="good", suite=True)
        for room in (bad, good):
            fx.rooms.checkout(fx.caller, room["id"], False)
        fx.later(hours=25)
        real = fsutil.safe_rmtree

        def explode(path, **kw):
            if bad["id"] in path:
                raise PermissionError(13, "Permission denied", path)
            return real(path, **kw)

        with mock.patch("motel.rooms.safe_rmtree", explode):
            report = fx.rooms.reap(True)
            self.assertTrue(any(bad["id"] in e for e in report["errors"]), report)
            self.assertEqual(fx.ctx.store.room(good["id"])["state"], "purged")
            self.assertEqual(fx.ctx.store.room(bad["id"])["state"], "lost-and-found")
            self.assertFalse(report["ok"])
            fx.later(minutes=1)
            fx.rooms.reap(True)  # starts the idle clock of the good floor
            fx.later(minutes=16)
            fx.rooms.reap(True)
        self.assertEqual(fx.ctx.store.floor(good["floor"])["state"], "deleted")
        self.assertEqual(fx.ctx.store.floor(bad["floor"])["state"], "active")

    def test_housekeeping_survives_a_deep_tree_under_a_small_fd_limit(self):
        fx = self.fx
        a = fx.checkin(10, task="aaa")
        b = fx.checkin(10, task="zzz")
        deep_chain(a["path"], 3000)
        fx.later(hours=6 + 6 + 24 + 1)
        with low_fd_limit(128):
            code, out, _ = fx.desk("reap", "--apply", "--json", caller=ROOT_CALLER)
            self.assertEqual(code, 0, out)
            fx.later(hours=25)
            code, out, _ = fx.desk("reap", "--apply", "--json", caller=ROOT_CALLER)
            self.assertEqual(code, 0, out)
        self.assertEqual({r["task"]: r["state"] for r in fx.ctx.store.rooms()}, {"aaa": "purged", "zzz": "purged"})
        self.assertFalse(os.path.exists(fx.rooms.trash_path(fx.ctx.store.floor(b["floor"]), a["id"])))

    def test_a_plain_oserror_in_one_room_is_reported_not_raised(self):
        fx = self.fx
        room = fx.checkin(10, hours=1)
        fx.later(hours=9)
        with mock.patch("motel.rooms.os.rename", side_effect=OSError(5, "Input/output error")):
            report = fx.rooms.reap(True)
        self.assertTrue(any(room["id"] in e and "Input/output error" in e for e in report["errors"]))
        self.assertFalse(report["ok"])

    def test_retiring_a_floor_refuses_while_the_device_is_mounted_elsewhere(self):
        fx = self.fx
        room = fx.checkin(10)
        fx.rooms.checkout(fx.caller, room["id"], True)
        floor = fx.ctx.store.floor(room["floor"])
        dev = fx.provider.device_path(floor["volume_id"])
        fx.sysops.other_device_mounts[dev] = ["/srv/elsewhere"]
        fx.later(minutes=1)
        fx.rooms.reap(True)
        fx.later(minutes=20)
        report = fx.rooms.reap(True)
        self.assertTrue(any("/srv/elsewhere" in r for r in report["refused"]), report)
        self.assertFalse(fx.provider.ops("detach"))
        fx.sysops.other_device_mounts.clear()
        fx.rooms.reap(True)
        self.assertTrue(fx.provider.ops("delete"))

    def test_retiring_a_floor_refuses_while_a_mount_is_nested_inside_it(self):
        fx = self.fx
        room = fx.checkin(10)
        fx.rooms.checkout(fx.caller, room["id"], True)
        floor = fx.ctx.store.floor(room["floor"])
        fx.sysops.nested_mounts = [fx.rooms.mountpoint(floor) + "/rooms/x/inner"]
        fx.later(minutes=1)
        fx.rooms.reap(True)
        fx.later(minutes=20)
        report = fx.rooms.reap(True)
        self.assertTrue(any("mount inside" in r for r in report["refused"]), report)
        self.assertFalse(fx.provider.ops("detach"))

    def test_device_level_evidence_blocks_every_room_on_the_floor(self):
        fx = self.fx
        a, b = fx.checkin(10, hours=1), fx.checkin(10, hours=1)
        mp = fx.rooms.mountpoint(fx.ctx.store.floor(a["floor"]))
        fx.sysops.device_busy[mp] = ["777 container-proc"]
        for room in (a, b):
            with self.assertRaises(Busy):
                fx.rooms.checkout(fx.caller, room["id"], False)


if __name__ == "__main__":
    unittest.main()
