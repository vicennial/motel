"""Round 3: volumes whose create succeeded but was not confirmed, duration bounds, non-Linux hosts, scan hygiene."""

import os
import subprocess
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from unittest import mock

from helpers import Fixture
from motel import sysops
from motel.errors import ProviderError
from motel.providers.hetzner import HetznerProvider
from motel.sysops import RealSysOps, ScanTimeout, scan_holders_guarded
from test_hetzner import FakeHetzner


class HetznerUnconfirmedCreateTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeHetzner()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.api.handler())
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.p = HetznerProvider("secret-token", base=f"http://127.0.0.1:{self.server.server_address[1]}",
                                 sleep=lambda _: None, metadata_fetch={"instance-id": "1000001", "availability-zone": "fsn1-dc14"}.__getitem__)

    def test_a_failed_create_action_still_reports_the_volume(self):
        self.api.create_status = "running"
        self.api.action_poll = {"status": "error", "error": {"code": "locked", "message": "busy"}}
        with self.assertRaises(ProviderError) as raised:
            self.p.create_volume("motel-f-1-ab", 10, "fsn1", {"managed-by": "motel"}, server_id="1000001")
        self.assertTrue(raised.exception.indeterminate)
        self.assertEqual(raised.exception.volume_id, "100")
        self.assertIn(100, self.api.volumes)

    def test_an_exhausted_poll_still_reports_the_volume(self):
        self.api.create_status = "running"
        self.api.fail[("GET", "/actions")] = [503] * 5
        with self.assertRaises(ProviderError) as raised:
            self.p.create_volume("motel-f-1-ab", 10, "fsn1", {"managed-by": "motel"}, server_id="1000001")
        self.assertEqual(raised.exception.volume_id, "100")
        self.assertTrue(raised.exception.indeterminate)


class UnconfirmedCreateHousekeepingTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.fx.provider.fail_after_create = True

    def test_the_volume_is_recorded_counted_and_finally_retired(self):
        with self.assertRaises(ProviderError):
            self.fx.checkin(10)
        floors = self.fx.ctx.store.floors()
        self.assertEqual(len(floors), 1)
        self.assertEqual(floors[0]["state"], "created")
        self.assertIn(floors[0]["volume_id"], self.fx.provider.volumes)
        self.assertEqual(self.fx.rooms._capacity()["total_gb"], 50)  # it bills, so it counts against the caps
        report = self.fx.rooms.reap(True)
        self.assertTrue(report["ok"])
        self.assertEqual(report["orphans"], [])
        self.assertIn(floors[0]["volume_id"], self.fx.provider.volumes)  # not retired before it has been idle for a while
        self.fx.later(hours=1)
        self.fx.rooms.reap(True)
        self.fx.later(hours=1)
        report = self.fx.rooms.reap(True)
        self.assertTrue(report["ok"], report)
        self.assertEqual(self.fx.provider.volumes, {})
        self.assertEqual(self.fx.ctx.store.floors()[0]["state"], "deleted")
        self.assertEqual(self.fx.rooms._capacity()["total_gb"], 0)

    def test_a_definite_error_cannot_hide_a_volume_that_exists(self):
        # a provider that raises a plain error after the cloud made the volume (no id on the error)
        self.fx.provider.fail_after_create = False
        original = self.fx.provider.create_volume

        def create_then_fail(*a, **k):
            original(*a, **k)
            raise ProviderError("Hetzner action attach_volume failed: locked")

        self.fx.provider.create_volume = create_then_fail
        with self.assertRaises(ProviderError):
            self.fx.checkin(10)
        self.assertEqual(self.fx.ctx.store.floors()[0]["state"], "created")
        self.assertEqual(self.fx.rooms._capacity()["total_gb"], 50)
        self.fx.provider.create_volume = original
        for _ in range(3):
            self.fx.later(hours=2)
            report = self.fx.rooms.reap(True)
        self.assertEqual(self.fx.provider.volumes, {})
        self.assertTrue(report["ok"])

    def test_a_definitive_failure_without_a_volume_is_still_failed(self):
        self.fx.provider.fail_after_create = False
        self.fx.provider.fail_next["create"] = ProviderError("HTTP 422")
        with self.assertRaises(ProviderError):
            self.fx.checkin(10)
        self.assertEqual(self.fx.ctx.store.floors()[0]["state"], "failed")


class DurationBoundTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def cli(self, *argv):
        import io
        import json

        from motel import cli
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(list(argv) + ["--json"], runner=lambda a: self.fx.desk(*a), out=out, err=err)
        return code, json.loads(out.getvalue() or "{}")

    def test_over_long_requests_reach_the_cap_check(self):
        for ttl in ("8d", "694d", "695d", "1000d", "99999h", "999999d", "9999999m", "10000000m", "9999999d"):
            code, doc = self.cli("checkin", "10G", "--for", ttl)
            self.assertEqual(code, 3, (ttl, doc))
            self.assertIn("limit per call", doc["error"])

    def test_extend_and_restore_take_the_same_bound(self):
        room = self.fx.checkin(10)
        code, _ = self.cli("extend", room["id"], "--for", "999999d")
        self.assertEqual(code, 3)
        self.fx.rooms.checkout(self.fx.caller, room["id"], False)
        code, _ = self.cli("restore", room["id"], "--for", "999999d")
        self.assertEqual(code, 3)

    def test_the_desk_itself_still_rejects_what_it_cannot_parse(self):
        self.assertEqual(self.fx.desk("checkin", "--size", "10G", "--task", "x", "--for", "9999999m", "--json")[0], 3)
        self.assertEqual(self.fx.desk("checkin", "--size", "10G", "--task", "x", "--for", "10000000m", "--json")[0], 2)

    def test_malformed_durations_are_still_usage_errors(self):
        for bad in ("6x", "h", "1.5h", "-1h", "0h", "1234567890123m"):
            code, _ = self.cli("checkin", "10G", "--for", bad)
            self.assertEqual(code, 2, bad)


class AbandonedScanTest(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.marker = os.path.join(self.tmp.name, ".abandoned-scan")
        self.started = os.path.join(self.tmp.name, "started")
        self.argv = [sys.executable, "-c", f"open({self.started!r}, 'w').close(); print('[]')"]

    def sleeper(self):
        child = subprocess.Popen(["sleep", "60"])
        self.addCleanup(child.wait)
        self.addCleanup(child.kill)
        return child

    @unittest.skipUnless(sys.platform.startswith("linux"), "needs /proc")
    def test_no_new_scan_while_an_abandoned_one_is_alive(self):
        stuck = self.sleeper()
        with open(self.marker, "w") as handle:
            handle.write(f"{stuck.pid} {sysops._scan_identity(stuck.pid)} {sysops._boot_id()}\n")
        with self.assertRaises(ScanTimeout) as raised:
            scan_holders_guarded("/proc", "/x", None, argv=self.argv, marker=self.marker)
        self.assertIn(str(stuck.pid), raised.exception.message)
        self.assertFalse(os.path.exists(self.started))  # no new child was started
        self.assertEqual(RealSysOps(scan_marker=self.marker).abandoned_scan(), stuck.pid)
        stuck.kill()
        stuck.wait()
        self.assertEqual(scan_holders_guarded("/proc", "/x", None, argv=self.argv, marker=self.marker), [])
        self.assertTrue(os.path.exists(self.started))
        self.assertFalse(os.path.exists(self.marker))  # a marker whose process is gone is cleaned up

    def test_a_marker_for_a_reused_pid_is_not_trusted(self):
        with open(self.marker, "w") as handle:
            handle.write(f"{os.getpid()} 1 {sysops._boot_id()}\n")  # right pid, wrong start time
        self.assertIsNone(sysops.abandoned_scan(self.marker))

    def test_a_marker_from_another_boot_is_stale_even_if_the_pid_matches(self):
        with open(self.marker, "w") as handle:
            handle.write(f"{os.getpid()} {sysops._scan_identity(os.getpid())} not-this-boot\n")
        self.assertIsNone(sysops.abandoned_scan(self.marker))

    def test_only_the_desk_removes_a_stale_marker_never_doctor(self):
        with open(self.marker, "w") as handle:
            handle.write("1 1 old-boot\n")
        ops = RealSysOps(scan_marker=self.marker)
        self.assertIsNone(ops.abandoned_scan())  # what doctor calls
        self.assertTrue(os.path.exists(self.marker))
        scan_holders_guarded("/proc", "/x", None, argv=self.argv, marker=self.marker)  # what the desk does
        self.assertFalse(os.path.exists(self.marker))

    def test_a_child_that_survives_its_kill_leaves_a_marker(self):
        with mock.patch.object(subprocess.Popen, "wait", side_effect=subprocess.TimeoutExpired("x", 1)), \
                mock.patch.object(sysops, "_scan_identity", return_value="4242"):
            with self.assertRaises(ScanTimeout):
                scan_holders_guarded("/proc", "/x", None, timeout=0.3, marker=self.marker,
                                     argv=[sys.executable, "-c", "import time; time.sleep(30)"])
            with open(self.marker) as handle:
                self.assertIn(" 4242 ", handle.read())
        while sysops._ABANDONED:  # the real child was killed; reap it
            sysops._ABANDONED.pop().wait()

    def test_doctor_warns_about_a_stuck_scan(self):
        import json
        fx = Fixture()
        self.addCleanup(fx.close)
        code, out, _ = fx.desk("doctor", "--json")
        check = next(c for c in json.loads(out)["checks"] if c["check"] == "no stuck holder scan")
        self.assertEqual(check["level"], "ok")
        fx.sysops.abandoned = 31337
        code, out, _ = fx.desk("doctor", "--json")
        check = next(c for c in json.loads(out)["checks"] if c["check"] == "no stuck holder scan")
        self.assertEqual(check["level"], "warn")
        self.assertIn("31337", check["detail"])


class LocalProviderUnconfirmedCreateTest(unittest.TestCase):
    def test_a_failed_format_reports_the_volume_id(self):
        import tempfile

        from motel.errors import SystemFault
        from motel.providers.local import LocalProvider
        with tempfile.TemporaryDirectory() as tmp:
            def runner(argv, timeout=60):
                if "mkfs.ext4" in argv[0]:
                    raise SystemFault("mkfs failed")
                return ""
            provider = LocalProvider(os.path.join(tmp, "local"), runner=runner)
            with self.assertRaises(ProviderError) as raised:
                provider.create_volume("motel-f-1-ab", 1, "local", {"managed-by": "motel"}, server_id="1000001")
            self.assertTrue(raised.exception.indeterminate)
            self.assertIn(raised.exception.volume_id, [v.id for v in provider.list_owned({"managed-by": "motel"})])


if __name__ == "__main__":
    unittest.main()
