"""The billing table and the window arithmetic (no I/O, a fixed clock)."""

import re
import unittest
from datetime import UTC, datetime, timedelta

from motel import billing
from motel.providers import registry

T0 = datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)
M = timedelta(minutes=1)


class TableTest(unittest.TestCase):
    def test_every_registered_provider_has_an_entry(self):
        for name in registry.SPECS:
            self.assertIn(name, billing.BILLING, name)

    def test_hetzner_bills_servers_and_volumes_per_started_hour_from_creation(self):
        entry = billing.provider_billing("hetzner")
        self.assertTrue(entry.servers.aligned and entry.volumes.aligned)
        self.assertEqual("created", billing.ANCHOR)

    def test_a_confirmed_rule_cites_an_official_https_source_and_an_unconfirmed_one_says_why(self):
        for name, entry in billing.BILLING.items():
            if name in ("local", "fake"):
                continue
            for kind, rule in (("servers", entry.servers), ("volumes", entry.volumes)):
                self.assertRegex(rule.source, r"^https://", (name, kind))
                self.assertTrue(rule.note, (name, kind))
                if rule.granularity == billing.PER_SECOND and kind == "servers":
                    self.assertGreater(rule.minimum_s, 0, (name, kind))

    def test_only_hetzner_linode_and_vultr_servers_are_aligned(self):
        aligned = sorted((n, k) for n, e in billing.BILLING.items() for k, r in (("servers", e.servers), ("volumes", e.volumes))
                         if r.aligned)
        self.assertEqual([("hetzner", "servers"), ("hetzner", "volumes"), ("linode", "servers"), ("linode", "volumes"),
                          ("vultr", "servers")], aligned)

    def test_an_unlisted_or_unknown_provider_is_never_aligned(self):
        self.assertFalse(billing.provider_billing("nowhere").servers.aligned)
        self.assertFalse(billing.provider_billing("nowhere").volumes.aligned)
        self.assertFalse(billing.provider_billing("scaleway").volumes.aligned)

    def test_a_provider_object_may_carry_its_own_volume_rule(self):
        class Own:
            name = "hetzner"
            volume_billing = billing.Rule(billing.UNKNOWN, "", "test")
        self.assertFalse(billing.volume_rule(Own()).aligned)
        self.assertTrue(billing.volume_rule(type("P", (), {"name": "hetzner"})()).aligned)

    def test_no_source_is_a_private_or_internal_address(self):
        text = repr(billing.BILLING)
        for url in re.findall(r"https?://[^\s'\")]+", text):
            self.assertNotRegex(url, r"localhost|127\.|\.internal|\.local\b")


class WindowTest(unittest.TestCase):
    def window(self, minutes, **kw):
        return billing.release_window(T0, T0 + minutes * M, **kw)

    def test_just_created(self):
        w = self.window(0)
        self.assertFalse(w.release)
        self.assertEqual(T0 + 60 * M, w.paid_until)
        self.assertEqual((T0 + 44 * M, T0 + 54 * M), (w.opens, w.closes))

    def test_mid_hour_waits(self):
        self.assertFalse(self.window(31).release)
        self.assertFalse(self.window(43).release)

    def test_the_pass_before_the_window_waits_only_while_the_next_pass_is_in_time(self):
        self.assertFalse(self.window(42).release)                    # next pass 53:00 + 1m slack = 53:00 <= 54:00 is fine
        w = self.window(43, interval=timedelta(minutes=10), slack=timedelta(minutes=1))
        self.assertFalse(w.release)                                   # 43 + 11 = 54: just in time
        w = billing.release_window(T0, T0 + 43 * M + timedelta(seconds=1))
        self.assertTrue(w.release)
        self.assertEqual("next_pass_too_late", w.why)

    def test_inside_the_window_releases(self):
        for minute in (44, 50, 54):
            w = self.window(minute)
            self.assertTrue(w.release, minute)
            self.assertEqual("window", w.why)

    def test_in_the_margin_before_the_boundary_it_still_releases(self):
        self.assertTrue(self.window(59).release)
        self.assertTrue(self.window(59.9).release)

    def test_past_the_boundary_the_next_hours_window_applies(self):
        w = self.window(60)
        self.assertFalse(w.release)
        self.assertEqual(T0 + 120 * M, w.paid_until)
        self.assertFalse(self.window(61).release)
        self.assertTrue(self.window(104).release)

    def test_the_boundary_itself_starts_a_new_billed_hour(self):
        self.assertEqual(T0 + 60 * M, billing.paid_until(T0, T0 + 59 * M + timedelta(seconds=59)))
        self.assertEqual(T0 + 120 * M, billing.paid_until(T0, T0 + 60 * M))

    def test_a_deadline_is_a_promise_the_window_never_passes(self):
        self.assertTrue(self.window(31, deadline=T0 + 40 * M).release)
        self.assertEqual("deadline", self.window(31, deadline=T0 + 40 * M).why)
        self.assertFalse(self.window(31, deadline=T0 + 90 * M).release)
        self.assertTrue(self.window(80, deadline=T0 + 90 * M).release)
        self.assertTrue(self.window(31, deadline=T0 + 10 * M).release)           # already past

    def test_the_margin_is_configurable(self):
        w = self.window(0, margin=timedelta(minutes=20))
        self.assertEqual((T0 + 30 * M, T0 + 40 * M), (w.opens, w.closes))

    def test_a_pass_every_ten_minutes_releases_inside_the_paid_hour_whatever_the_phase_or_margin(self):
        for offset in range(0, 600, 7):                               # any phase of the timer
            for margin in (4, 6, 15, 30):
                for hour in range(3):
                    start = T0 + hour * 60 * M
                    end, closes = start + 60 * M, start + 60 * M - timedelta(minutes=margin)
                    released = next(now for now in (start + timedelta(seconds=offset + 600 * k) for k in range(8))
                                    if billing.release_window(T0, now, margin=timedelta(minutes=margin)).release)
                    self.assertLessEqual(released, closes, (offset, margin, hour))
                    self.assertLess(released, end)
                    self.assertGreaterEqual(released, closes - 11 * M)


class AnchorTest(unittest.TestCase):
    """anchor(provider times, local response-arrival times, record time, now): the earliest wins, so a skewed provider
    clock can only move the window earlier."""

    def test_the_provider_time_wins_when_it_is_the_earliest(self):
        self.assertEqual((T0, "provider"), billing.anchor([T0], [T0 + 2 * M], T0 - M, T0 + 10 * M))

    def test_a_provider_clock_ahead_of_ours_is_replaced_by_the_arrival_time(self):
        self.assertEqual((T0 + M, "received"), billing.anchor([T0 + 30 * M], [T0 + M], T0, T0 + 10 * M))

    def test_a_provider_time_in_the_future_with_no_arrival_time_is_clamped_to_now(self):
        self.assertEqual((T0, "provider"), billing.anchor([T0 + 10 * M], [], T0 - 5 * M, T0))

    def test_a_provider_clock_a_little_behind_keeps_the_hour_early(self):
        self.assertEqual((T0 - 3 * M, "provider"), billing.anchor([T0 - 3 * M], [T0], T0 - M, T0 + 5 * M))

    def test_a_provider_time_long_before_our_own_record_is_not_believed(self):
        self.assertEqual((T0, "received"), billing.anchor([T0 - 50 * M], [T0], T0 - M, T0 + 5 * M))

    def test_the_earliest_of_the_server_and_the_disk_is_the_anchor(self):
        server, disk = T0 + 20 * timedelta(seconds=1), T0
        self.assertEqual((disk, "provider"), billing.anchor([server, disk], [server, disk], T0 - M, T0 + M))

    def test_a_time_without_a_zone_is_no_clock_at_all(self):
        naive = datetime(2026, 11, 8, 11, 0, 0)
        self.assertEqual((T0, "received"), billing.anchor([naive], [T0], T0 - M, T0 + M))
        self.assertEqual((T0 - M, "record"), billing.anchor([naive], [naive], T0 - M, T0 + M))
        self.assertEqual((None, "none"), billing.anchor([naive], None, naive, T0))

    def test_a_missing_provider_time_falls_back_to_the_arrival_then_the_record(self):
        self.assertEqual((T0 - 2 * M, "received"), billing.anchor([None], [T0 - 2 * M], T0 - 5 * M, T0 + 10 * M))
        self.assertEqual((T0 - 5 * M, "record"), billing.anchor([None], [None], T0 - 5 * M, T0 + 10 * M))

    def test_nothing_known(self):
        self.assertEqual((None, "none"), billing.anchor(None, None, None, T0))

    def test_a_record_time_ahead_of_now_is_clamped_too(self):
        self.assertEqual((T0, "record"), billing.anchor([], [], T0 + 5 * M, T0))

    def test_the_anchor_never_follows_the_clock_ahead_so_the_first_hour_ends_an_hour_after_it(self):
        start, _ = billing.anchor([T0 + 20 * timedelta(seconds=1)], [], None, T0)
        self.assertEqual((T0 + 60 * M, 1), (billing.paid_until(start, T0), billing.billed_hours(start, T0)))


class ConstantsTest(unittest.TestCase):
    def test_the_reaper_interval_is_the_installed_timer_interval(self):
        from motel import installer
        self.assertEqual(timedelta(minutes=installer.INTERVAL_MINUTES), billing.REAP_INTERVAL)

    def test_the_margin_floor_leaves_room_for_a_cleanup(self):
        self.assertEqual(timedelta(minutes=4), billing.MIN_MARGIN)
        self.assertGreater(billing.MIN_MARGIN, timedelta(minutes=3))   # a cleanup budget is up to 3 minutes per lease

    def test_the_provider_rules_say_what_is_inferred(self):
        self.assertTrue(billing.provider_billing("hetzner").volumes.inferred)
        self.assertFalse(billing.provider_billing("hetzner").servers.inferred)
        self.assertIn("inferred", billing.provider_billing("hetzner").volumes.note)
        self.assertIn("ASSUMPTION", open(billing.__file__, encoding="utf-8").read())


class BilledHoursTest(unittest.TestCase):
    def test_started_hours(self):
        for minutes, hours in ((0, 1), (1, 1), (60, 1), (60.5, 2), (61, 2), (119, 2), (120, 2), (121, 3)):
            self.assertEqual(hours, billing.billed_hours(T0, T0 + minutes * M), minutes)

    def test_an_end_before_the_start_is_one_hour(self):
        self.assertEqual(1, billing.billed_hours(T0, T0 - 5 * M))


if __name__ == "__main__":
    unittest.main()
