"""Root-helper mechanics: exact commands, process scan, and symlink-safe writes."""

import os
import sys
import tempfile
import unittest
from pathlib import Path

from motel.errors import Guard, SystemFault
from motel.providers.fake import FakeProvider
from motel.state import atomic_write, safe_write_in_dir
from motel.sysops import RealSysOps, verify_device


class SysopsCommandTest(unittest.TestCase):
    def setUp(self):
        self.calls = []
        self.ops = RealSysOps(runner=lambda argv, timeout=0: self.calls.append(argv) or "")

    def test_mount_is_nodev_nosuid_ext4_with_absolute_paths_and_no_shell(self):
        self.ops.mount("/dev/disk/by-id/scsi-0HC_Volume_1", "/mnt/motel/f-1234")
        [argv] = self.calls
        self.assertTrue(argv[0].endswith("/mount") and os.path.isabs(argv[0]))
        self.assertEqual(argv[1:], ["-t", "ext4", "-o", "nodev,nosuid,noatime,discard", "/dev/disk/by-id/scsi-0HC_Volume_1", "/mnt/motel/f-1234"])

    def test_umount_is_never_lazy_or_forced(self):
        self.ops.umount("/mnt/motel/f-1234")
        [argv] = self.calls
        self.assertEqual(argv[1:], ["/mnt/motel/f-1234"])

    def test_filesystem_tools(self):
        self.ops.prepare_fs("/dev/x", "motel-f-1234-extra-long")
        self.ops.grow_fs("/dev/x")
        self.assertEqual(self.calls[0][1:5], ["-m", "0", "-L", "motel-f-1234-ext"])
        self.assertTrue(self.calls[1][0].endswith("resize2fs"))

    def test_failing_commands_become_system_faults(self):
        def fail(argv, timeout=0):
            raise SystemFault("boom")
        with self.assertRaises(SystemFault):
            RealSysOps(runner=fail).umount("/x")

    def test_real_runner_uses_a_fixed_environment(self):
        os.environ["LD_PRELOAD"] = "/tmp/evil.so"
        try:
            out = RealSysOps._run([sys.executable, "-c", "import os; print(os.environ.get('LD_PRELOAD'), sorted(os.environ))"])
        finally:
            del os.environ["LD_PRELOAD"]
        self.assertTrue(out.startswith("None"))

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs /proc")
    def test_holders_finds_a_process_with_a_file_open_or_cwd_inside(self):
        with tempfile.TemporaryDirectory() as tmp:
            sub = Path(tmp, "room")
            sub.mkdir()
            with open(sub / "f", "w"):
                found = RealSysOps().holders(str(sub), tmp)
            self.assertTrue(any(h.startswith(f"{os.getpid()} ") for h in found), found)
            old = os.getcwd()
            os.chdir(sub)
            try:
                self.assertTrue(RealSysOps().holders(str(sub), tmp))
            finally:
                os.chdir(old)


class VerifyDeviceTest(unittest.TestCase):
    def test_serial_and_filesystem_must_match(self):
        from motel.testing import FakeSysOps
        ops, provider = FakeSysOps(), FakeProvider()
        self.assertEqual(verify_device(ops, provider, "5", "/dev/fake/5"), "/dev/fake/5")
        ops.serial_override["/dev/fake/5"] = "6"
        with self.assertRaises(Guard):
            verify_device(ops, provider, "5", "/dev/fake/5")
        ops.serial_override.clear()
        ops.fstype_override["/dev/fake/5"] = "ntfs"
        with self.assertRaises(Guard):
            verify_device(ops, provider, "5", "/dev/fake/5")


class SafeWriteTest(unittest.TestCase):
    def test_a_symlink_placed_by_the_caller_is_replaced_not_followed(self):
        with tempfile.TemporaryDirectory() as tmp:
            room, secret = Path(tmp, "room"), Path(tmp, "secret")
            room.mkdir()
            secret.write_text("precious")
            os.symlink(secret, room / ".motel-room.json")
            safe_write_in_dir(str(room), ".motel-room.json", "new")
            self.assertEqual(secret.read_text(), "precious")
            self.assertFalse((room / ".motel-room.json").is_symlink())
            self.assertEqual((room / ".motel-room.json").read_text(), "new")

    def test_a_symlinked_room_directory_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            target, link = Path(tmp, "target"), Path(tmp, "link")
            target.mkdir()
            os.symlink(target, link)
            with self.assertRaises(OSError):
                safe_write_in_dir(str(link), "x", "data")
            self.assertEqual(list(target.iterdir()), [])

    def test_a_preplaced_temp_name_cannot_redirect_the_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp, "target")
            target.write_text("precious")
            atomic_write(str(Path(tmp, "state.json")), "{}")
            self.assertEqual(Path(tmp, "state.json").read_text(), "{}")
            self.assertEqual(target.read_text(), "precious")
            self.assertEqual(sorted(p.name for p in Path(tmp).iterdir()), ["state.json", "target"])

    def test_state_files_are_private(self):
        with tempfile.TemporaryDirectory() as tmp:
            atomic_write(str(Path(tmp, "x.json")), "{}")
            self.assertEqual(os.stat(Path(tmp, "x.json")).st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()
