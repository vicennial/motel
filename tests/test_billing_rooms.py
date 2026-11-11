"""Rooms and hourly billing: an empty shared floor waits for the release window of its paid hour."""

import unittest
from dataclasses import replace
from datetime import timedelta

from helpers import Fixture, toml_with
from motel import billing
from motel.errors import ConfigError

HOURLY = billing.Rule(billing.STARTED_HOUR, "https://example.invalid/pricing", "test rule")
PER_SECOND = billing.Rule(billing.PER_SECOND, "https://example.invalid/pricing", "test rule", minimum_s=60)


def emptied(fx, size=10, suite=False):
    """A floor created now whose only room was checked out and purged at once (`checkout --now`)."""
    room = fx.checkin(size, suite=suite)
    fx.rooms.checkout(fx.caller, room["id"], True)
    return room


class HeldFloorTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.fx.provider.volume_billing = HOURLY

    def reap(self):
        return self.fx.rooms.reap(True)

    def test_an_empty_floor_is_kept_past_retire_after_until_its_release_window(self):
        fx = self.fx
        emptied(fx)
        fx.later(minutes=16)                         # retire_after (15m) is over; the paid hour is not
        report = self.reap()
        self.assertEqual(1, len(fx.provider.volumes))
        self.assertEqual(1, len(report["held"]))
        held = report["held"][0]
        self.assertEqual(("empty", "provider"), (held["reason"], held["billing_source"]))
        self.assertTrue(held["floor"] and held["release_from"] < held["release_by"] <= held["paid_until"])
        self.assertNotIn("detach", [c[0] for c in fx.provider.calls])
        fx.later(minutes=20)                         # minute 36: still in the first paid hour
        self.assertEqual(1, len(self.reap()["held"]))
        self.assertEqual(1, len(fx.provider.volumes))

    def test_it_is_retired_when_the_window_opens(self):
        fx = self.fx
        emptied(fx)
        fx.later(minutes=44, seconds=1)              # 60 - 6 (margin) - 10 (one pass) = minute 44
        report = self.reap()
        self.assertEqual({}, fx.provider.volumes)
        self.assertEqual([], report["held"])
        self.assertEqual(["detach", "delete"], [c[0] for c in fx.provider.calls if c[0] in ("detach", "delete")])

    def test_a_pass_that_would_be_followed_by_a_pass_too_late_retires_now(self):
        fx = self.fx
        emptied(fx)
        fx.later(minutes=43)                         # the next pass (10 min + 1 min slack) would land at 54:00, still in time
        self.assertEqual(1, len(self.reap()["held"]))
        fx.later(seconds=30)                         # minute 43:30: the next pass would come after the window closed
        self.assertEqual([], self.reap()["held"])
        self.assertEqual({}, fx.provider.volumes)

    def test_a_new_checkin_packs_onto_the_held_floor_and_the_floor_is_kept(self):
        fx = self.fx
        first = emptied(fx)
        fx.later(minutes=20)
        self.assertEqual(1, len(self.reap()["held"]))
        again = fx.checkin(10)                       # packs onto the held floor: no second volume
        self.assertEqual(first["floor"], again["floor"])
        self.assertEqual(1, len(fx.provider.ops("create")))
        fx.later(minutes=30)                         # minute 50: inside the window, but the floor is in use
        report = self.reap()
        self.assertEqual(1, len(fx.provider.volumes))
        self.assertEqual([], report["held"])
        self.assertFalse(fx.ctx.store.floor(first["floor"]).get("idle_since"))

    def test_once_the_room_that_packed_on_is_gone_the_floor_waits_again_for_its_window(self):
        fx = self.fx
        first = emptied(fx)
        fx.later(minutes=20)
        again = fx.checkin(10)
        self.reap()
        fx.later(minutes=5)
        fx.rooms.checkout(fx.caller, again["id"], True)   # empty again at minute 25
        fx.later(minutes=16)                              # minute 41: retire_after over, window not open
        self.assertEqual(1, len(self.reap()["held"]))
        fx.later(minutes=4)                               # minute 45
        self.reap()
        self.assertEqual({}, fx.provider.volumes)
        self.assertTrue(first["floor"])

    def test_a_floor_left_unreaped_across_the_boundary_waits_for_the_next_hours_window(self):
        fx = self.fx
        emptied(fx)
        fx.later(minutes=70)                          # the reaper did not run; hour 2 is paid for already
        report = self.reap()
        self.assertEqual(1, len(fx.provider.volumes))
        self.assertEqual(1, len(report["held"]))
        fx.later(minutes=35)                          # minute 105: the window of hour 2 (opens at 104)
        self.reap()
        self.assertEqual({}, fx.provider.volumes)

    def test_the_provider_creation_time_is_the_clock_not_the_floors_own_record(self):
        fx = self.fx
        emptied(fx)
        vid = next(iter(fx.provider.volumes))
        # the cloud says the volume is 4 minutes older than motel's own record of asking for it
        fx.provider.volumes[vid] = replace(fx.provider.volumes[vid], created=fx.clock() - timedelta(minutes=4))
        fx.later(minutes=40)                          # minute 44 of the provider's hour: the window is open
        self.reap()
        self.assertEqual({}, fx.provider.volumes)

    def test_dry_run_reports_the_hold_and_changes_nothing(self):
        fx = self.fx
        emptied(fx)
        fx.later(minutes=16)
        report = fx.rooms.reap(False)
        self.assertEqual(1, len(report["held"]))
        self.assertEqual([], [a for a in report["actions"] if "unmount, detach, delete" in a])
        self.assertEqual(1, len(fx.provider.volumes))


    def test_the_window_is_judged_by_the_clock_now_not_by_the_start_of_the_pass(self):
        fx = self.fx
        emptied(fx)
        fx.later(minutes=40)                          # the pass starts at minute 40: before the window
        real_get = fx.provider.get_volume

        def slow_get(vol_id):                         # a slow provider call: the pass is a few minutes old by the time it decides
            fx.later(minutes=5)
            return real_get(vol_id)
        fx.provider.get_volume = slow_get
        report = self.reap()
        self.assertEqual({}, fx.provider.volumes)     # minute 45 by the clock: the window is open
        self.assertEqual([], report["held"])

    def test_a_volume_time_without_a_zone_is_ignored_and_the_arrival_time_is_used(self):
        fx = self.fx
        emptied(fx)
        vid = next(iter(fx.provider.volumes))
        fx.provider.volumes[vid] = replace(fx.provider.volumes[vid], created=fx.clock().replace(tzinfo=None) - timedelta(minutes=3))
        fx.later(minutes=20)
        report = self.reap()
        self.assertEqual("received", report["held"][0]["billing_source"])
        self.assertEqual(1, len(fx.provider.volumes))

    def test_the_human_reap_report_prints_the_held_floors(self):
        from motel import render
        fx = self.fx
        emptied(fx)
        fx.later(minutes=20)
        text = render.human_reap(self.reap())
        self.assertIn("held: floor ", text)
        self.assertIn("kept for packing until 2026-01-01T12:44:00Z", text)
        self.assertIn("deleted by 2026-01-01T12:54:00Z", text)
        self.assertNotEqual("nothing to do", text)


class NeverHeldTest(unittest.TestCase):
    def fixture(self, **toml):
        fx = Fixture(toml_with(**toml) if toml else toml_with())
        self.addCleanup(fx.close)
        return fx

    def test_unknown_billing_is_retired_exactly_as_before(self):
        fx = self.fixture()                           # the in-memory provider is not in the billing table as hourly
        self.assertFalse(billing.volume_rule(fx.provider).aligned)
        emptied(fx)
        fx.later(minutes=16)
        report = fx.rooms.reap(True)
        self.assertEqual({}, fx.provider.volumes)
        self.assertEqual([], report["held"])

    def test_a_provider_billed_per_second_is_not_aligned_either(self):
        fx = self.fixture()
        fx.provider.volume_billing = PER_SECOND
        emptied(fx)
        fx.later(minutes=16)
        fx.rooms.reap(True)
        self.assertEqual({}, fx.provider.volumes)

    def test_a_suite_floor_is_never_held(self):
        fx = self.fixture()
        fx.provider.volume_billing = HOURLY
        emptied(fx, 120, suite=True)                  # a suite checked out with --now: nothing can pack onto it
        fx.later(minutes=16)
        report = fx.rooms.reap(True)
        self.assertEqual({}, fx.provider.volumes)
        self.assertEqual([], report["held"])

    def test_retire_after_zero_is_an_explicit_request_for_immediate_retirement(self):
        fx = self.fixture(retire_after='"0m"')
        fx.provider.volume_billing = HOURLY
        emptied(fx)
        fx.rooms.reap(True)
        self.assertEqual({}, fx.provider.volumes)

    def test_the_margin_is_configurable_and_validated(self):
        fx = self.fixture(release_margin='"20m"')
        fx.provider.volume_billing = HOURLY
        emptied(fx)
        fx.later(minutes=31)                          # window opens at 60 - 20 - 10 = minute 30
        fx.rooms.reap(True)
        self.assertEqual({}, fx.provider.volumes)
        with self.assertRaises(ConfigError):
            Fixture(toml_with(release_margin='"1m"'))


if __name__ == "__main__":
    unittest.main()
