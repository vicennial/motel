"""Clouds that hand out blank devices: the desk formats only a device it has proved blank, and reads the device
path after the attach. Also the serial-less identity check, the rescan before a grow and tiered billing."""

import shutil
import unittest

from helpers import Fixture, toml_with
from motel.errors import Guard, ProviderError, Refused, SystemFault
from motel.providers.fake import FakeProvider
from motel.sysops import GIB


class BlankProvider(FakeProvider):
    formats_on_create = False
    gb_bytes = None  # undeclared: the desk accepts binary or decimal gigabytes


class LateDeviceProvider(BlankProvider):
    """Vultr: the device path is known only while the volume is attached."""

    def device_path(self, vol_id):
        if self.volumes[str(vol_id)].server_id is None:
            raise ProviderError(f"block {vol_id} has no usable mount_id (it is attached only after an attach call)")
        return super().device_path(vol_id)


class BlankDeviceTest(unittest.TestCase):
    provider_class = BlankProvider

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.fx.provider = self.fx.ctx.provider = self.provider_class(server_id=self.fx.provider.server_id, clock=self.fx.clock)
        self.fx.rooms.provider = self.fx.provider
        self.fx.sysops.sizes["/dev/fake/900001"] = 50 * GIB
        self.dev = "/dev/fake/900001"

    def calls(self, name):
        return [c for c in self.fx.sysops.calls if c[0] == name]

    def test_a_blank_device_of_the_right_size_is_formatted_then_mounted(self):
        room = self.fx.checkin(10)
        [mkfs] = self.calls("make_fs")
        self.assertEqual(mkfs[1], self.dev)
        self.assertEqual(mkfs[2], "motel-" + room["floor"])
        self.assertEqual(self.fx.ctx.store.floor(room["floor"])["state"], "active")
        self.assertEqual(len(self.calls("mount")), 1)

    def test_a_device_with_any_signature_is_never_formatted(self):
        for n, signature in enumerate(({"TYPE": "ext4", "LABEL": "precious"}, {"PTTYPE": "gpt"}, {"TYPE": "LVM2_member"})):
            with self.subTest(signature):
                dev = f"/dev/fake/{900001 + n}"  # every attempt creates a new volume
                self.fx.sysops.sizes[dev] = 50 * GIB
                self.fx.sysops.signatures[dev] = signature
                self.fx.sysops.calls.clear()
                with self.assertRaises(Guard) as ctx:
                    self.fx.rooms._create_floor(50, False, "job", [])
                self.assertIn("signature", ctx.exception.message)
                self.assertEqual(self.calls("make_fs"), [])
                self.assertEqual(self.calls("mount"), [])

    def test_a_device_mounted_somewhere_is_never_formatted(self):
        self.fx.sysops.other_device_mounts[self.dev] = ["/srv/elsewhere"]
        with self.assertRaises(Guard) as ctx:
            self.fx.checkin(10)
        self.assertIn("mounted", ctx.exception.message)
        self.assertEqual(self.calls("make_fs"), [])

    def test_a_device_of_the_wrong_size_is_never_formatted(self):
        self.fx.sysops.sizes[self.dev] = 20 * GIB
        with self.assertRaises(Guard) as ctx:
            self.fx.checkin(10)
        self.assertIn("bytes", ctx.exception.message)
        self.assertEqual(self.calls("make_fs"), [])

    def test_decimal_gigabytes_are_accepted_too(self):
        self.fx.sysops.sizes[self.dev] = 50 * 10 ** 9
        self.fx.checkin(10)
        self.assertEqual(len(self.calls("make_fs")), 1)

    def test_the_serial_is_checked_before_anything_else(self):
        self.fx.sysops.serial_override[self.dev] = "someone-elses-disk"
        with self.assertRaises(Guard):
            self.fx.checkin(10)
        self.assertEqual(self.calls("make_fs"), [])

    def test_a_filesystem_this_desk_already_made_is_not_made_again(self):
        """mkfs ran, then the desk died before the state changed: the next run must not refuse or wipe."""
        provider = self.fx.provider
        original = provider.create_volume

        def create(name, size_gb, location, labels, server_id=None):
            vol = original(name, size_gb, location, labels, server_id)
            self.fx.sysops.signatures[self.dev] = {"TYPE": "ext4", "LABEL": "motel-" + labels["floor"]}
            self.fx.sysops.labels[self.dev] = "motel-" + labels["floor"]
            return vol
        provider.create_volume = create
        self.fx.checkin(10)
        self.assertEqual(self.calls("make_fs"), [])
        self.assertEqual(len(self.calls("mount")), 1)

    def test_a_provider_that_formats_is_never_asked_to_be_formatted(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        fx.checkin(10)
        self.assertEqual([c for c in fx.sysops.calls if c[0] == "make_fs"], [])


class LateDeviceTest(BlankDeviceTest):
    provider_class = LateDeviceProvider

    def test_the_device_path_is_read_after_the_attach(self):
        room = self.fx.checkin(10)
        order = [c[0] for c in self.fx.provider.calls if c[0] in ("attach", "create")]
        self.assertEqual(order[-1], "create")  # created attached: no separate attach needed here
        self.assertEqual(len(self.calls("mount")), 1)
        floor = self.fx.ctx.store.floor(room["floor"])
        self.assertEqual(floor["state"], "active")

    def test_a_detached_volume_is_attached_before_its_path_is_asked_for(self):
        room = self.fx.checkin(10)
        floor = self.fx.ctx.store.floor(room["floor"])
        mp = self.fx.rooms.mountpoint(floor)
        self.fx.rooms.sys.umount(mp)
        shutil.rmtree(mp)  # a real umount leaves an empty directory
        self.fx.provider.detach(floor["volume_id"])
        self.fx.rooms.ensure_mounted(floor)  # an active floor, detached: path unknown until the attach
        self.assertEqual(self.fx.provider.get_volume(floor["volume_id"]).server_id, self.fx.provider.server_id)


class EvidenceTest(unittest.TestCase):
    """Positive proof of blankness: unreadable, dirty or stacked devices are never formatted."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        provider = BlankProvider(server_id=self.fx.provider.server_id, clock=self.fx.clock)
        self.fx.provider = self.fx.ctx.provider = self.fx.rooms.provider = provider
        self.dev = "/dev/fake/900001"
        self.fx.sysops.sizes[self.dev] = 50 * GIB

    def refused(self, exc_type=Guard):
        with self.assertRaises(exc_type):
            self.fx.checkin(10)
        self.assertEqual([c for c in self.fx.sysops.calls if c[0] == "make_fs"], [])

    def test_nonzero_head_or_tail_is_never_formatted(self):
        self.fx.sysops.dirty.add(self.dev)
        self.refused()

    def test_a_device_with_another_device_stacked_on_it_is_never_formatted(self):
        self.fx.sysops.stacked[self.dev] = ["dm-3"]
        self.refused()

    def test_a_probe_that_fails_is_never_blank(self):
        from motel.errors import SystemFault

        def broken(dev):
            raise SystemFault("blkid could not probe")
        self.fx.sysops.probe = broken
        self.refused(SystemFault)

    def test_the_serial_and_size_are_checked_again_right_before_mkfs(self):
        sizes = iter([50 * GIB, 49 * GIB])
        self.fx.sysops.block_size = lambda dev: next(sizes)
        self.refused()


class SerialLessTest(unittest.TestCase):
    """Azure: no block-device serial, so the ext4 label motel-<floor> is the identity check."""

    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        provider = BlankProvider(server_id=self.fx.provider.server_id, clock=self.fx.clock)
        provider.expected_serial = lambda vol_id: None
        self.fx.provider = self.fx.ctx.provider = self.fx.rooms.provider = provider
        self.fx.sysops.sizes["/dev/fake/900001"] = 50 * GIB

    def test_a_freshly_made_filesystem_carries_the_label(self):
        room = self.fx.checkin(10)
        self.assertEqual(self.fx.sysops.labels["/dev/fake/900001"], "motel-" + room["floor"])

    def test_a_device_with_the_wrong_label_is_refused_on_remount(self):
        room = self.fx.checkin(10)
        floor = self.fx.ctx.store.floor(room["floor"])
        mp = self.fx.rooms.mountpoint(floor)
        self.fx.rooms.sys.umount(mp)
        shutil.rmtree(mp)
        self.fx.sysops.labels["/dev/fake/900001"] = "somebody-else"
        with self.assertRaises(Guard) as ctx:
            self.fx.rooms.ensure_mounted(floor)
        self.assertIn("label", ctx.exception.message)


class GrowTest(unittest.TestCase):
    def test_the_guest_is_rescanned_to_the_new_size_before_the_filesystem_grows(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        room = fx.checkin(40)
        floor = fx.ctx.store.floor(room["floor"])
        fx.sysops.calls.clear()
        fx.rooms._grow_floor(floor, 80, fx.ctx.store.floors())
        names = [c[0] for c in fx.sysops.calls]
        self.assertEqual(names[names.index("rescan") + 1], "grow_fs")
        self.assertEqual([c for c in fx.sysops.calls if c[0] == "rescan"][0][2], 80 * GIB)

    def test_a_provider_without_a_declared_unit_waits_for_the_smaller_interpretation(self):
        """Formatting accepts GiB or GB; the grow must not wait for a size a decimal device never reaches."""
        fx = Fixture()
        self.addCleanup(fx.close)
        fx.provider.gb_bytes = None
        room = fx.checkin(40)
        floor = fx.ctx.store.floor(room["floor"])
        fx.sysops.calls.clear()
        fx.rooms._grow_floor(floor, 80, fx.ctx.store.floors())
        self.assertEqual([c for c in fx.sysops.calls if c[0] == "rescan"][0][2], 80 * 10 ** 9)

    def test_the_rescan_also_waits_for_more_than_the_size_seen_before_the_resize(self):
        """A 200 GB floor grown to 210 GB with the unit unknown: the old device must not satisfy the wait."""
        fx = Fixture()
        self.addCleanup(fx.close)
        fx.provider.gb_bytes = None
        room = fx.checkin(40)
        floor = fx.ctx.store.floor(room["floor"])
        dev = fx.provider.device_path(floor["volume_id"])
        fx.sysops.sizes[dev] = 40 * GIB
        fx.sysops.calls.clear()
        fx.rooms._grow_floor(floor, 80, fx.ctx.store.floors())
        call = [c for c in fx.sysops.calls if c[0] == "rescan"][0]
        self.assertEqual(call[2:], (80 * 10 ** 9, 40 * GIB))
        self.assertNotIn("fs_grow_from", fx.ctx.store.floor(room["floor"]), "cleared once the filesystem followed")

    def test_the_size_before_the_resize_survives_a_crash_between_the_resize_and_the_grow(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        fx.provider.gb_bytes = None
        room = fx.checkin(40)
        floor = fx.ctx.store.floor(room["floor"])
        dev = fx.provider.device_path(floor["volume_id"])
        fx.sysops.sizes[dev] = 40 * GIB
        original = fx.sysops.grow_fs

        def crash(real):
            raise SystemFault("simulated: the process died here")

        fx.sysops.grow_fs = crash
        with self.assertRaises(SystemFault):
            fx.rooms._grow_floor(floor, 80, fx.ctx.store.floors())
        fx.sysops.grow_fs = original
        saved = fx.ctx.store.floor(room["floor"])
        self.assertTrue(saved["fs_grow_pending"])
        self.assertEqual(saved["fs_grow_from"], 40 * GIB)
        fx.sysops.calls.clear()
        fx.rooms._grow_fs(saved)
        self.assertEqual([c for c in fx.sysops.calls if c[0] == "rescan"][0][3], 40 * GIB)

    def test_a_provider_with_decimal_gigabytes_names_its_unit(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        fx.provider.gb_bytes = 10 ** 9
        room = fx.checkin(40)
        floor = fx.ctx.store.floor(room["floor"])
        fx.sysops.calls.clear()
        fx.rooms._grow_floor(floor, 80, fx.ctx.store.floors())
        self.assertEqual([c for c in fx.sysops.calls if c[0] == "rescan"][0][2], 80 * 10 ** 9)


class HousekeepingGrowBaselineTest(unittest.TestCase):
    """A grow that housekeeping finds (out of band, or after a crash) still waits for more than the old guest size."""

    def setUp(self):
        self.fx = Fixture(toml_with(max_floors=1))  # one floor: more space means growing it
        self.addCleanup(self.fx.close)
        room = self.fx.checkin(40)
        self.floor_id = room["floor"]
        self.vid = self.floor()["volume_id"]
        self.dev = self.fx.provider.device_path(self.vid)

    def floor(self):
        return self.fx.ctx.store.floor(self.floor_id)

    def grow_out_of_band(self, new_gb):
        from dataclasses import replace
        self.fx.provider.volumes[self.vid] = replace(self.fx.provider.volumes[self.vid], size_gb=new_gb)
        self.fx.sysops.calls.clear()
        self.fx.rooms.reap(True)
        return [c for c in self.fx.sysops.calls if c[0] == "rescan"]

    def test_the_old_guest_size_is_recorded_when_the_mismatch_is_first_seen(self):
        old_gb = int(self.floor()["size_gb"])
        self.fx.sysops.sizes[self.dev] = old_gb * GIB
        calls = self.grow_out_of_band(old_gb + 40)
        self.assertEqual(len(calls), 1)
        self.assertEqual(calls[0][2:], ((old_gb + 40) * GIB, old_gb * GIB))
        self.assertNotIn("fs_grow_from", self.floor())

    def test_a_guest_that_already_shows_the_new_size_is_not_made_to_wait_for_more(self):
        old_gb = int(self.floor()["size_gb"])
        self.fx.sysops.sizes[self.dev] = (old_gb + 40) * GIB      # virtio updated by itself
        calls = self.grow_out_of_band(old_gb + 40)
        self.assertEqual(len(calls[0]), 3, calls)

    def test_a_size_that_matches_neither_reading_is_not_a_baseline(self):
        old_gb = int(self.floor()["size_gb"])
        self.fx.sysops.sizes[self.dev] = old_gb * GIB + 12345
        self.assertEqual(len(self.grow_out_of_band(old_gb + 40)[0]), 3)

    def test_an_unreadable_guest_size_records_nothing(self):
        old_gb = int(self.floor()["size_gb"])
        self.assertEqual(len(self.grow_out_of_band(old_gb + 40)[0]), 3)

    def test_with_an_undeclared_unit_a_decimal_old_size_is_recognised(self):
        self.fx.provider.gb_bytes = None
        old_gb = int(self.floor()["size_gb"])
        self.fx.sysops.sizes[self.dev] = old_gb * 10 ** 9
        calls = self.grow_out_of_band(old_gb + 40)
        self.assertEqual(calls[0][3], old_gb * 10 ** 9)

    def test_a_reservation_that_times_out_forgets_the_old_size(self):
        from motel.errors import ProviderError
        old_gb = int(self.floor()["size_gb"])
        self.fx.sysops.sizes[self.dev] = old_gb * GIB
        self.fx.provider.fail_next["resize"] = ProviderError("timeout", indeterminate=True)
        with self.assertRaises(ProviderError):
            self.fx.checkin(30, task="more")
        self.assertEqual(self.floor()["fs_grow_from"], old_gb * GIB)
        self.fx.later(minutes=70)
        self.fx.rooms.reap(True)
        self.assertNotIn("fs_grow_from", self.floor())
        self.assertNotIn("resize_pending", self.floor())


class TieredBillingTest(unittest.TestCase):
    def test_the_monthly_cap_uses_the_billed_tier_when_the_provider_has_one(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        # 50 GB at EUR 0.05 = 2.50: fine. A provider that bills 50 GB as 1024 GB costs 51.20: over the EUR 30 cap.
        fx.provider.billable_gb = lambda gb: 1024
        with self.assertRaises(Refused) as ctx:
            fx.checkin(10)
        self.assertIn("1024 GB would cost", ctx.exception.message)

    def test_without_tiers_nothing_changes(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        fx.checkin(10, hours=1)
        self.assertEqual(fx.rooms._capacity()["total_gb"], 50)


if __name__ == "__main__":
    unittest.main()


class ResumeAndRetireTest(unittest.TestCase):
    def blank_fixture(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        provider = BlankProvider(server_id=fx.provider.server_id, clock=fx.clock)
        fx.provider = fx.ctx.provider = fx.rooms.provider = provider
        fx.sysops.sizes["/dev/fake/900001"] = 50 * GIB
        return fx

    def test_a_crash_between_mount_and_active_resumes_instead_of_wedging(self):
        import motel.rooms as rooms_mod
        fx = self.blank_fixture()
        real_write = rooms_mod.atomic_write

        def dies(*args, **kwargs):
            raise OSError("simulated crash before the manifest and state=active")
        rooms_mod.atomic_write = dies
        try:
            with self.assertRaises(OSError):
                fx.checkin(10)
        finally:
            rooms_mod.atomic_write = real_write
        [floor] = fx.ctx.store.floors()
        self.assertEqual(floor["state"], "created")
        self.assertTrue(fx.sysops.is_mounted(fx.rooms.mountpoint(floor)))
        fx.sysops.calls.clear()
        fx.rooms.ensure_mounted(floor)  # used to raise "mounted at ...; refusing to format it"
        self.assertEqual(fx.ctx.store.floor(floor["id"])["state"], "active")
        self.assertEqual([c for c in fx.sysops.calls if c[0] == "make_fs"], [])
        self.assertTrue(fx.checkin(10))

    def test_a_device_mounted_anywhere_else_is_still_refused(self):
        fx = self.blank_fixture()
        fx.sysops.other_device_mounts["/dev/fake/900001"] = ["/srv/elsewhere"]
        with self.assertRaises(Guard):
            fx.checkin(10)

    def test_a_grow_that_cannot_finish_never_keeps_an_empty_floor_alive(self):
        from motel.errors import SystemFault
        fx = Fixture()
        self.addCleanup(fx.close)
        room = fx.checkin(10)
        floor = fx.ctx.store.floor(room["floor"])
        fx.rooms.checkout(fx.caller, room["id"], True)
        floor["fs_grow_pending"] = True
        fx.ctx.store.save_floor(floor)

        def never_sees_the_new_size(dev):
            raise SystemFault("the guest never saw the new size")
        fx.sysops.grow_fs = never_sees_the_new_size
        fx.later(minutes=1)
        fx.rooms.reap(True)  # marks the floor idle
        fx.later(minutes=30)
        report = fx.rooms.reap(True)
        self.assertEqual(report["errors"], [])
        self.assertEqual(fx.ctx.store.floor(floor["id"])["state"], "deleted")
        self.assertEqual(fx.provider.volumes, {})
