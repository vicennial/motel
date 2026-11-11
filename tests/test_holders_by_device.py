"""Busy detection by device, against a fake /proc (no root, no real processes)."""

import os
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from motel.sysops import resolve_ancestry, scan_holders

FLOOR_DEV, OTHER_DEV = 111, 222


class FakeProc(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        base = Path(self.tmp.name).resolve()
        self.proc, self.floor, self.other = base / "proc", base / "floor", base / "other"
        self.r1, self.r2 = self.floor / "rooms" / "r1", self.floor / "rooms" / "r2"
        for d in (self.proc, self.r1, self.r2, self.other):
            d.mkdir(parents=True)
        (self.r1 / "f1").write_text("x")
        (self.r2 / "f2").write_text("x")
        self.blind_pids = set()
        self.ghost_links = set()

    def stat_fn(self, path):
        """Real stat, but with fake devices: everything under `floor` is device 111, the rest 222.
        Links of processes in `blind_pids` (and `ghost_links`) have a floor device but no path that leads to them."""
        path = str(path)
        rel = os.path.relpath(path, self.proc).split(os.sep)
        if not rel[0].startswith("..") and rel[0].isdigit() and int(rel[0]) in self.blind_pids:
            return SimpleNamespace(st_dev=FLOOR_DEV, st_ino=987654321, st_mode=0o40755)
        if path in self.ghost_links:
            return SimpleNamespace(st_dev=FLOOR_DEV, st_ino=555, st_mode=0o100644)
        st = os.stat(path)
        real = os.path.realpath(path)
        dev = FLOOR_DEV if real == str(self.floor) or real.startswith(str(self.floor) + os.sep) else OTHER_DEV
        return SimpleNamespace(st_dev=dev, st_ino=st.st_ino, st_mode=st.st_mode)

    def process(self, pid, *, cwd=None, fds=(), maps="", comm="proc"):
        d = self.proc / str(pid)
        (d / "fd").mkdir(parents=True)
        os.symlink(cwd or self.other, d / "cwd")
        os.symlink(self.other, d / "root")
        os.symlink(self.other, d / "exe")
        for n, target in enumerate(fds, start=3):
            os.symlink(target, d / "fd" / str(n))
        (d / "maps").write_text(maps)
        (d / "comm").write_text(comm + "\n")

    def scan(self, path, floor_mp="default", **kw):
        mp = str(self.floor) if floor_mp == "default" else floor_mp
        return scan_holders(str(self.proc), str(path), mp, stat_fn=self.stat_fn, **kw)


class HoldersByDeviceTest(FakeProc):
    def test_cwd_inside_the_room_holds_that_room_only(self):
        self.process(1, cwd=self.r1, comm="shell")
        self.assertEqual(self.scan(self.r1), ["1 shell"])
        self.assertEqual(self.scan(self.r2), [])

    def test_open_file_inside_the_room(self):
        self.process(2, fds=[self.r1 / "f1"], comm="writer")
        self.assertEqual(self.scan(self.r1), ["2 writer"])
        self.assertEqual(self.scan(self.r2), [])

    def test_a_process_on_another_device_is_ignored(self):
        self.process(3, cwd=self.other, fds=[self.other])
        self.assertEqual(self.scan(self.r1), [])
        self.assertEqual(self.scan(self.floor, floor_mp=None), [])

    def test_a_mapping_on_the_floor_device(self):
        key = f"{os.major(FLOOR_DEV):02x}:{os.minor(FLOOR_DEV):02x}"
        inode = os.stat(self.r2 / "f2").st_ino
        self.process(4, maps=f"7f00-7f01 r--p 00000000 {key} {inode} {self.r2 / 'f2'}\n7f02-7f03 r--p 0 00:00 0 [heap]\n", comm="mapper")
        self.assertEqual(self.scan(self.r2), ["4 mapper"])
        self.assertEqual(self.scan(self.r1), [])

    def test_evidence_without_a_usable_path_blocks_every_room_on_the_floor(self):
        self.blind_pids = {5}
        self.process(5, comm="container")  # another mount namespace: the device matches, the path means nothing here
        self.assertEqual(self.scan(self.r1), ["5 container"])
        self.assertEqual(self.scan(self.r2), ["5 container"])

    def _ghost_fd(self, pid, name, target):
        self.process(pid, comm=name)
        link = self.proc / str(pid) / "fd" / "3"
        os.symlink(target, link)
        self.ghost_links = self.ghost_links | {str(link)}

    def test_a_deleted_file_holds_the_floor_but_no_room(self):
        # r6: one unlinked file held in room A must not block checkout of room B (or A itself)
        self._ghost_fd(6, "orphan-fd", f"{self.r1}/gone (deleted)")  # what the kernel shows for an unlinked file
        self.assertEqual(self.scan(self.r1), [])
        self.assertEqual(self.scan(self.r2), [])
        self.assertEqual(self.scan(self.floor, floor_mp=None), ["6 orphan-fd"])

    def test_a_real_file_named_deleted_is_not_mistaken_for_an_unlinked_one(self):
        (self.r1 / "x (deleted)").write_text("x")
        self.process(11, fds=[self.r1 / "x (deleted)"], comm="odd-name")
        self.assertEqual(self.scan(self.r1), ["11 odd-name"])
        self.assertEqual(self.scan(self.r2), [])

    def test_a_reference_that_cannot_be_resolved_still_blocks_every_room(self):
        self._ghost_fd(12, "unknown", f"{self.r1}/not-there")  # device matches, name leads nowhere
        self.assertEqual(self.scan(self.r1), ["12 unknown"])
        self.assertEqual(self.scan(self.r2), ["12 unknown"])

    def test_another_namespace_is_placed_by_walking_below_its_root(self):
        # the process sees the floor at /data; the walk below /proc/PID/root tells which room the file is in
        r1_id = (FLOOR_DEV, os.stat(self.r1).st_ino)
        walks = []

        def resolve(root, target):
            walks.append((root, target))
            return {"/data/rooms/r1/f1": [(7, 1), (FLOOR_DEV, 11), r1_id, (FLOOR_DEV, 99)],
                    "/data/elsewhere/f": [(7, 1), (FLOOR_DEV, 11), (FLOOR_DEV, 12)]}.get(target)

        self._ghost_fd(13, "in-r1", "/data/rooms/r1/f1")
        self.assertEqual(self.scan(self.r1, resolve_fn=resolve), ["13 in-r1"])
        self.assertEqual(self.scan(self.r2, resolve_fn=resolve), [])
        self.assertTrue(all(root == str(self.proc / "13" / "root") for root, _ in walks))
        os.remove(self.proc / "13" / "fd" / "3")
        os.symlink("/data/elsewhere/f", self.proc / "13" / "fd" / "3")
        self.assertEqual(self.scan(self.r1, resolve_fn=resolve), [])

    def test_floor_level_counts_any_reference_to_the_device(self):
        self.process(7, cwd=self.r1, comm="a")
        self.process(8, fds=[self.r2 / "f2"], comm="b")
        self.process(9, cwd=self.other, comm="c")
        self.assertEqual(sorted(self.scan(self.floor, floor_mp=None)), ["7 a", "8 b"])

    def mounts(self, pid, **ids):
        """mountinfo and fdinfo for a fake process: ids maps 'fd3' / 'cwd' to a mount id; 30 = other device, 31 = floor."""
        d = self.proc / str(pid)
        (d / "mountinfo").write_text(f"29 1 0:5 / / rw - rootfs rootfs rw\n30 1 0:{OTHER_DEV} / {self.other} rw - nfs4 server:/x rw\n"
                                     f"31 1 0:{FLOOR_DEV} / {self.floor} rw - ext4 /dev/x rw\n")
        (d / "fdinfo").mkdir()
        for name, mnt in ids.items():
            if name.startswith("fd"):
                (d / "fdinfo" / name[2:]).write_text(f"pos:\t0\nflags:\t02\nmnt_id:\t{mnt}\n")

    def test_references_on_other_mounts_are_never_stat_ed(self):
        # a hung NFS/FUSE mount elsewhere must not stall the scan: its descriptors are skipped by mount id
        self.process(20, fds=[self.other / "x", self.r1 / "f1"], comm="mixed")
        self.mounts(20, fd3=30, fd4=31)
        seen = []

        def spy(path):
            seen.append(str(path))
            return self.stat_fn(path)

        found = scan_holders(str(self.proc), str(self.r1), str(self.floor), stat_fn=spy)
        self.assertEqual(found, ["20 mixed"])
        names = [os.path.relpath(p, self.proc) for p in seen if p.startswith(str(self.proc))]
        self.assertNotIn("20/fd/3", names)  # on the other mount
        self.assertNotIn("20/cwd", names)  # readlink puts it under the other mount
        self.assertNotIn("20/root", names)
        self.assertNotIn("20/exe", names)
        self.assertIn("20/fd/4", names)

    def test_an_unknown_mount_id_is_stat_ed_like_before(self):
        self.process(21, fds=[self.r1 / "f1"], comm="unknown-mount")
        self.mounts(21, fd3=99)  # not in the process's mountinfo
        self.assertEqual(self.scan(self.r1), ["21 unknown-mount"])

    def test_without_mountinfo_everything_is_stat_ed(self):
        self.process(22, fds=[self.r1 / "f1"], comm="no-mountinfo")
        self.assertEqual(self.scan(self.r1), ["22 no-mountinfo"])

    def test_vanished_processes_are_skipped(self):
        (self.proc / "10").mkdir()  # no cwd/fd/maps at all
        self.assertEqual(self.scan(self.r1), [])


@unittest.skipUnless(sys.platform.startswith("linux"), "O_PATH walking is Linux-only")
class ResolveAncestryTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()
        (self.base / "ns" / "data" / "room").mkdir(parents=True)
        (self.base / "ns" / "data" / "room" / "f").write_text("x")

    def ident(self, path):
        st = os.stat(path)
        return (st.st_dev, st.st_ino)

    def test_every_component_is_listed(self):
        chain = resolve_ancestry(str(self.base / "ns"), "/data/room/f")
        self.assertEqual(chain, [self.ident(self.base / "ns"), self.ident(self.base / "ns/data"),
                                 self.ident(self.base / "ns/data/room"), self.ident(self.base / "ns/data/room/f")])

    def test_the_root_may_be_a_link_like_proc_pid_root(self):
        os.symlink(self.base / "ns", self.base / "root")
        self.assertIn(self.ident(self.base / "ns/data/room"), resolve_ancestry(str(self.base / "root"), "/data/room/f"))

    def test_symlinks_dotdot_missing_and_relative_names_are_not_resolved(self):
        os.symlink(self.base / "ns/data/room", self.base / "ns" / "link")
        self.assertIsNone(resolve_ancestry(str(self.base / "ns"), "/link/f"))
        self.assertIsNone(resolve_ancestry(str(self.base / "ns"), "/data/../data/room"))
        self.assertIsNone(resolve_ancestry(str(self.base / "ns"), "/data/nope"))
        self.assertIsNone(resolve_ancestry(str(self.base / "ns"), "data/room"))
        self.assertIsNone(resolve_ancestry(str(self.base / "missing"), "/data"))


class MountTableTest(unittest.TestCase):
    TABLE = [(1, "0:5", "/"), (2, "8:1", "/mnt/motel/f-1"), (3, "9:9", "/mnt/motel/f-1/rooms/a b"), (4, "7:7", "/mnt/motel/f-1")]

    def test_the_longest_mount_point_wins_and_a_later_over_mount_beats_an_earlier_one(self):
        from motel.sysops import device_of_path
        self.assertEqual(device_of_path(self.TABLE, "/srv/x"), "0:5")
        self.assertEqual(device_of_path(self.TABLE, "/mnt/motel/f-1/rooms/r1/f"), "7:7")  # entry 4 over-mounts entry 2
        self.assertEqual(device_of_path(self.TABLE, "/mnt/motel/f-1/rooms/a b/x"), "9:9")
        self.assertEqual(device_of_path(self.TABLE, "/mnt/motel/f-10/x"), "0:5")  # f-10 is not under f-1
        self.assertEqual(device_of_path(self.TABLE, "/mnt/motel/f-1 (deleted)"), "7:7")

    def test_paths_it_cannot_place_are_unknown(self):
        from motel.sysops import device_of_path
        self.assertIsNone(device_of_path(self.TABLE, "(unreachable)/etc"))
        self.assertIsNone(device_of_path([], "/x"))

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs /proc")
    def test_the_real_table_places_a_real_directory_on_its_device(self):
        from motel.sysops import device_of_path, fdinfo_mnt_id, mount_table
        table = mount_table("/proc/self/mountinfo")
        with tempfile.TemporaryDirectory() as tmp:
            st = os.stat(tmp)
            self.assertEqual(device_of_path(table, os.path.realpath(tmp)), f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}")
            with open(Path(tmp, "f"), "w") as handle:
                mnt = fdinfo_mnt_id(f"/proc/self/fdinfo/{handle.fileno()}")
            self.assertEqual({mid: mm for mid, mm, _ in table}[mnt], f"{os.major(st.st_dev)}:{os.minor(st.st_dev)}")
        self.assertIsNone(mount_table("/nonexistent-motel"))
        self.assertIsNone(fdinfo_mnt_id("/nonexistent-motel"))


class NoOPathTest(unittest.TestCase):
    def test_without_o_path_nothing_can_be_placed_and_the_answer_is_conservative(self):
        from unittest import mock

        from motel import sysops
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sysops, "_O_PATH", None):
                self.assertIsNone(resolve_ancestry(tmp, "/"))


class RealProcTest(unittest.TestCase):
    @unittest.skipUnless(os.path.isdir("/proc/self"), "needs /proc")
    def test_this_process_is_found_by_device_when_it_has_a_file_open_in_the_room(self):
        with tempfile.TemporaryDirectory() as tmp:
            room = Path(tmp, "room")
            room.mkdir()
            with open(room / "f", "w"):
                found = scan_holders("/proc", str(room), tmp)
            self.assertTrue(any(h.startswith(f"{os.getpid()} ") for h in found), found)


if __name__ == "__main__":
    unittest.main()
