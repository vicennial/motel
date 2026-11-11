"""RealSysOps: the commands behind the blank-device proof, the formatter and the rescan (runner replaced; no root)."""

import os
import tempfile
import unittest

from motel.errors import Guard, SystemFault
from motel.sysops import CommandFailed, RealSysOps


def failing(code, stderr=""):
    def runner(argv, timeout=0):
        fault = CommandFailed(f"{argv[0]} failed (exit {code}): {stderr}")
        fault.returncode = code
        fault.stderr = stderr
        raise fault
    return runner


class ProbeTest(unittest.TestCase):
    def test_blkid_exit_2_with_nothing_to_say_means_no_signature(self):
        self.assertEqual(RealSysOps(runner=failing(2)).probe("/dev/x"), {})

    def test_blkid_exit_2_with_an_error_message_is_a_read_failure_never_blank(self):
        """blkid exits 2 both for "no signature" and for "could not open or read the device"."""
        for text in ("blkid: error: /dev/x: Input/output error", "blkid: error: /dev/x: No such file or directory"):
            with self.subTest(text), self.assertRaises(SystemFault) as ctx:
                RealSysOps(runner=failing(2, text)).probe("/dev/x")
            self.assertIn("not treating it as blank", str(ctx.exception))

    def test_a_runner_that_only_keeps_the_message_is_read_the_same_way(self):
        def runner(argv, timeout=0):
            fault = CommandFailed("blkid -p -o failed (exit 2): blkid: error: /dev/x: Input/output error")
            fault.returncode = 2
            raise fault
        with self.assertRaises(SystemFault):
            RealSysOps(runner=runner).probe("/dev/x")

    def test_any_other_failure_is_a_fault_never_blank(self):
        with self.assertRaises(SystemFault):
            RealSysOps(runner=failing(4)).probe("/dev/x")

    def test_signatures_are_parsed_from_export_format(self):
        seen = []
        out = "DEVNAME=/dev/x\nTYPE=ext4\nLABEL=motel-f-1234\nUUID=abc\n"
        ops = RealSysOps(runner=lambda argv, timeout=0: seen.append(argv) or out)
        self.assertEqual(ops.probe("/dev/x"), {"TYPE": "ext4", "LABEL": "motel-f-1234", "UUID": "abc"})
        self.assertEqual(seen[0][1:], ["-p", "-o", "export", "/dev/x"])  # low-level probe: no cache
        self.assertEqual(ops.fs_label("/dev/x"), "motel-f-1234")

    def test_size_and_mkfs_commands(self):
        calls = []
        ops = RealSysOps(runner=lambda argv, timeout=0: calls.append(argv) or "53687091200\n")
        self.assertEqual(ops.block_size("/dev/x"), 53687091200)
        ops.make_fs("/dev/x", "motel-f-1234")
        self.assertEqual(calls[0][1:], ["-bdno", "SIZE", "/dev/x"])
        self.assertTrue(calls[1][0].endswith("mkfs.ext4"))
        self.assertEqual(calls[1][1:], ["-q", "-F", "-L", "motel-f-1234", "/dev/x"])

    def test_garbage_size_is_a_fault(self):
        with self.assertRaises(SystemFault):
            RealSysOps(runner=lambda argv, timeout=0: "n/a").block_size("/dev/x")


class EdgesTest(unittest.TestCase):
    def device(self, content: bytes) -> str:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        path = os.path.join(tmp.name, "dev")
        with open(path, "wb") as handle:
            handle.write(content)
        return path

    def test_zeros_pass(self):
        RealSysOps().zero_edges(self.device(bytes(3 * 1024 * 1024)), nbytes=1024 * 1024)

    def test_data_near_the_start_or_the_end_is_refused(self):
        size, edge = 3 * 1024 * 1024, 1024 * 1024
        for where in (0, 100, size - 1, size - edge):
            data = bytearray(size)
            data[where] = 1
            with self.subTest(where), self.assertRaises(Guard) as ctx:
                RealSysOps().zero_edges(self.device(bytes(data)), nbytes=edge)
            self.assertIn("zeroed", ctx.exception.hint)

    def test_the_middle_is_not_read(self):
        size, edge = 5 * 1024 * 1024, 1024 * 1024
        data = bytearray(size)
        data[size // 2] = 1
        RealSysOps().zero_edges(self.device(bytes(data)), nbytes=edge)

    def test_a_small_device_is_read_whole(self):
        with self.assertRaises(Guard):
            RealSysOps().zero_edges(self.device(b"\0" * 10 + b"x"), nbytes=1024)

    def test_a_missing_or_empty_device_is_a_fault(self):
        with self.assertRaises(SystemFault):
            RealSysOps().zero_edges("/nonexistent/dev")
        with self.assertRaises(SystemFault):
            RealSysOps().zero_edges(self.device(b""))

    def test_a_read_error_is_a_fault_not_a_blank(self):
        path = self.device(bytes(4096))
        import motel.sysops as sysops
        real = sysops.os.pread

        def failing_pread(fd, n, offset):
            raise OSError(5, "Input/output error")
        sysops.os.pread = failing_pread
        self.addCleanup(setattr, sysops.os, "pread", real)
        with self.assertRaises(SystemFault):
            RealSysOps().zero_edges(path, nbytes=1024)


class HoldersTest(unittest.TestCase):
    def test_holders_are_listed_and_an_unreadable_sysfs_is_a_fault(self):
        with tempfile.TemporaryDirectory() as tmp:
            os.makedirs(os.path.join(tmp, "sdz", "holders", "dm-0"))
            ops = RealSysOps()
            ops.block_sysfs = tmp
            self.assertEqual(ops.block_holders("/dev/sdz"), ["dm-0"])
            os.rmdir(os.path.join(tmp, "sdz", "holders", "dm-0"))
            self.assertEqual(ops.block_holders("/dev/sdz"), [])
            with self.assertRaises(SystemFault):
                ops.block_holders("/dev/other")


class RescanTest(unittest.TestCase):
    def test_waits_until_the_device_reaches_the_target(self):
        sizes = iter([100, 100, 200])
        ops = RealSysOps(runner=lambda argv, timeout=0: f"{next(sizes)}\n")
        ops.rescan_timeout = 30.0
        import motel.sysops as sysops
        slept = []
        original = sysops.time.sleep
        sysops.time.sleep = slept.append
        self.addCleanup(setattr, sysops.time, "sleep", original)
        with tempfile.TemporaryDirectory() as tmp:
            dev = os.path.join(tmp, "sdz")
            open(dev, "w").close()
            ops.rescan(dev, 200)
        self.assertEqual(len(slept), 2)

    def rescan_reads(self, sizes, target, above, timeout=30.0):
        """Run rescan against a device that shows ``sizes`` in turn; returns how many size reads it took."""
        reads = iter(sizes)
        taken = []

        def runner(argv, timeout=0):
            taken.append(1)
            return f"{next(reads)}\n"

        ops = RealSysOps(runner=runner)
        ops.rescan_timeout = timeout
        import motel.sysops as sysops
        original = sysops.time.sleep
        sysops.time.sleep = lambda seconds: None
        self.addCleanup(setattr, sysops.time, "sleep", original)
        with tempfile.TemporaryDirectory() as tmp:
            dev = os.path.join(tmp, "sdz")
            open(dev, "w").close()
            ops.rescan(dev, target, above)
        return len(taken)

    def test_a_device_that_already_passes_the_smaller_target_still_has_to_grow(self):
        """Hetzner-sized step, unit unknown: 200 -> 210 GB. The old 200 GiB device (214.7e9 bytes) is already above the
        decimal 210e9 target, so the size alone proves nothing; the wait also needs a size above the old one."""
        old_gib, new_gib, target = 200 * 2 ** 30, 210 * 2 ** 30, 210 * 10 ** 9
        self.assertGreater(old_gib, target)  # the trap
        self.assertEqual(self.rescan_reads([old_gib, old_gib, new_gib], target, old_gib), 3)

    def test_without_the_floor_the_old_size_would_have_passed(self):
        old_gib, target = 200 * 2 ** 30, 210 * 10 ** 9
        self.assertEqual(self.rescan_reads([old_gib], target, None), 1)

    def test_a_decimal_device_reaches_the_decimal_target(self):
        self.assertEqual(self.rescan_reads([200 * 10 ** 9, 210 * 10 ** 9], 210 * 10 ** 9, 200 * 10 ** 9), 2)

    def test_larger_than_before_is_not_enough_below_the_target(self):
        self.assertEqual(self.rescan_reads([100, 150, 200], 200, 100), 3)

    def test_a_guest_that_never_sees_the_new_size_times_out(self):
        old_gib = 200 * 2 ** 30
        with self.assertRaises(SystemFault) as ctx:
            self.rescan_reads(iter(lambda: old_gib, None), 210 * 10 ** 9, old_gib, timeout=-1.0)
        self.assertIn("more than", str(ctx.exception))

    def test_gives_up_with_a_fault(self):
        ops = RealSysOps(runner=lambda argv, timeout=0: "100\n")
        ops.rescan_timeout = -1.0
        with tempfile.TemporaryDirectory() as tmp:
            dev = os.path.join(tmp, "sdz")
            open(dev, "w").close()
            with self.assertRaises(SystemFault):
                ops.rescan(dev, 200)


if __name__ == "__main__":
    unittest.main()
