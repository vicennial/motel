"""Growth, resize bookkeeping, setup under load, and the installer's trust checks."""

import os
import tempfile
import threading
import unittest
from pathlib import Path

from helpers import SERVER, Fixture, toml_with
from motel import installer
from motel.errors import ProviderError, Usage
from motel.providers.fake import FakeProvider
from platform_marks import linux_only


class GrowthTest(unittest.TestCase):
    def test_a_resize_that_would_not_grow_is_never_sent(self):
        fx = Fixture(toml_with(max_floors=1))
        self.addCleanup(fx.close)
        fx.checkin(10)  # one 50 GB floor, 10 GB reserved
        fx.sysops.free_gb = 5  # but the disk is really full
        room = fx.checkin(10)  # growth is sized from the real usage, so it is a real growth
        sizes = [c[2] for c in fx.provider.ops("resize")]
        self.assertTrue(sizes and all(s > 50 for s in sizes), sizes)
        self.assertTrue(room["id"])

    def test_floors_that_cannot_grow_are_skipped(self):
        fx = Fixture(toml_with(max_floors=1))
        self.addCleanup(fx.close)
        fx.checkin(10)
        fx.sysops.total_gb, fx.sysops.free_gb = 30, 5  # 25 GB used + 10 GB fits the 50 GB floor, but only 5 GB is free: no growth helps
        from motel.errors import Refused
        with self.assertRaises(Refused):
            fx.checkin(10)
        self.assertEqual(fx.provider.ops("resize"), [])

    def test_the_fake_provider_rejects_non_growth_like_hetzner(self):
        provider = FakeProvider()
        vol = provider.create_volume("motel-x", 50, "fsn1", {}, server_id=None)
        for size in (50, 40):
            with self.assertRaises(ProviderError):
                provider.resize(vol.id, size)


class IndeterminateResizeTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(toml_with(max_floors=1))
        self.addCleanup(self.fx.close)
        self.fx.checkin(40)
        self.fx.provider.fail_next["resize"] = ProviderError("timeout", indeterminate=True)

    def floor(self):
        return self.fx.ctx.store.floors()[0]

    def test_the_reservation_is_kept_when_the_outcome_is_unknown(self):
        with self.assertRaises(ProviderError):
            self.fx.checkin(30)
        self.assertEqual(self.floor()["size_gb"], 70)
        self.assertIn("resize_pending", self.floor())

    def test_housekeeping_adopts_the_providers_size_if_the_resize_happened(self):
        with self.assertRaises(ProviderError):
            self.fx.checkin(30)
        vid = self.floor()["volume_id"]
        from dataclasses import replace
        self.fx.provider.volumes[vid] = replace(self.fx.provider.volumes[vid], size_gb=70)
        self.fx.rooms.reap(True)
        self.assertEqual(self.floor()["size_gb"], 70)
        self.assertNotIn("resize_pending", self.floor())

    def test_the_reservation_is_released_only_after_an_hour_without_a_resize(self):
        with self.assertRaises(ProviderError):
            self.fx.checkin(30)
        self.fx.later(minutes=30)
        self.fx.rooms.reap(True)
        self.assertEqual(self.floor()["size_gb"], 70)
        self.fx.later(minutes=40)
        self.fx.rooms.reap(True)
        self.assertEqual(self.floor()["size_gb"], 50)

    def test_a_definitive_failure_restores_the_real_size(self):
        self.fx.provider.fail_next["resize"] = ProviderError("HTTP 422")
        with self.assertRaises(ProviderError):
            self.fx.checkin(30)
        self.assertEqual(self.floor()["size_gb"], 50)
        self.assertNotIn("resize_pending", self.floor())


class SetupUnderLoadTest(unittest.TestCase):
    def test_setup_waits_for_the_lock_and_never_freezes_motels_own_volumes(self):
        import io

        from motel import desk
        from motel.rooms import Caller
        fx = Fixture(receipt=False)
        self.addCleanup(fx.close)
        cloud = FakeProvider(server_id=SERVER)
        cloud.add(11, 100, name="data-volume", server_id=SERVER)
        # motel is mid-create: a floor intent exists whose volume the cloud has already made (lost response)
        from motel.state import Store
        store = Store(fx.paths.state_dir, fx.clock, SERVER)
        store.save_floor({"id": "f-aaaa", "name": "motel-f-aaaa-ab", "labels": {}, "volume_id": None, "size_gb": 50, "suite": False,
                          "state": "intent", "created": "2026-01-01T12:00:00Z"})
        store.save_floor({"id": "f-bbbb", "name": "motel-f-bbbb-ab", "labels": {}, "volume_id": "21", "size_gb": 50, "suite": False,
                          "state": "abandoned", "created": "2026-01-01T12:00:00Z"})
        cloud.add(20, 50, labels={"floor": "f-aaaa"}, name="motel-f-aaaa-ab")
        cloud.add(21, 50, labels={"floor": "f-bbbb"}, name="motel-f-bbbb-ab")
        cloud.add(22, 50, labels={"floor": "f-aaaa"}, name="motel-impostor")  # same label, wrong name: not ours
        done = []

        def run():
            out = io.StringIO()
            done.append(desk.run(["setup", "hetzner", "--json"], paths=fx.paths, caller=Caller(0, 0, "root"), sysops=fx.sysops,
                                 clock=fx.clock, out=out, err=io.StringIO(), hetzner_factory=lambda token: cloud,
                                 token_reader=lambda: "t" * 64))

        with store.locked():  # a checkin holds the global lock
            thread = threading.Thread(target=run)
            thread.start()
            thread.join(0.3)
            self.assertTrue(thread.is_alive(), "setup must wait for the lock")
        thread.join(5)
        self.assertEqual(done, [0])
        import json
        ids = sorted(v["id"] for v in json.loads(Path(fx.paths.receipt_file).read_text())["never_touch"])
        self.assertEqual(ids, ["11", "22"])


class InstallerTrustTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name).resolve()

    def test_audit_chain_flags_writable_and_foreign_directories(self):
        lib = self.base / "a" / "lib"
        lib.mkdir(parents=True)
        os.chmod(self.base / "a", 0o755)
        os.chmod(lib, 0o755)
        # as if we were root: the directories are ours, so the audit for owner 0 reports every one of them
        problems = installer.audit_chain(str(lib), owner_uid=0) if os.getuid() != 0 else []
        if os.getuid() != 0:
            self.assertTrue(any("not root" in p for p in problems))
        self.assertEqual([p for p in installer.audit_chain(str(lib), owner_uid=os.getuid()) if "writable" in p and str(self.base) in p], [])
        os.chmod(self.base / "a", 0o775)
        self.assertTrue(any("writable by group" in p and p.startswith(str(self.base / "a")) for p in installer.audit_chain(str(lib), owner_uid=os.getuid())))
        os.chmod(self.base / "a", 0o757)
        self.assertTrue(any("writable by group or others" in p and p.startswith(str(self.base / "a")) for p in installer.audit_chain(str(lib), owner_uid=os.getuid())))

    def test_audit_tree_flags_loose_files(self):
        (self.base / "t").mkdir()
        f = self.base / "t" / "x.py"
        f.write_text("x")
        os.chmod(f, 0o644)
        self.assertEqual(installer.audit_tree(str(self.base / "t"), owner_uid=os.getuid()), [])
        os.chmod(f, 0o666)
        self.assertEqual(len(installer.audit_tree(str(self.base / "t"), owner_uid=os.getuid())), 1)

    @linux_only
    def test_install_refuses_when_a_parent_is_writable_by_others_and_copies_nothing(self):
        prefix = self.base / "root"
        (prefix / "usr/local").mkdir(parents=True)
        os.chmod(prefix / "usr/local", 0o777)
        with self.assertRaises(Usage) as raised:
            installer.install(str(prefix), str(Path(installer.__file__).parent.parent), users=["alice"], provider="hetzner",
                              systemd=False, cron=False, dry_run=False, visudo=False, log=lambda *_: None, audit=True,
                              owner_uid=os.getuid())
        self.assertIn("writable by group or others", raised.exception.message)
        self.assertFalse((prefix / "usr/local/lib/motel/motel").exists())

    def test_install_creates_lib_with_mode_0755_regardless_of_umask(self):
        prefix = self.base / "root"
        prefix.mkdir()
        old = os.umask(0o077)
        try:
            installer.install(str(prefix), str(Path(installer.__file__).parent.parent), users=["alice"], provider="hetzner",
                              systemd=False, cron=False, dry_run=False, visudo=False, log=lambda *_: None)
        finally:
            os.umask(old)
        self.assertEqual(oct((prefix / "usr/local/lib/motel").stat().st_mode & 0o777), "0o755")

    def test_doctor_reports_the_installed_code_check_only_on_a_real_system(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        code, out, _ = fx.desk("doctor")
        self.assertNotIn("installed code", out)  # the temp-prefix fixture is not the real filesystem


if __name__ == "__main__":
    unittest.main()
