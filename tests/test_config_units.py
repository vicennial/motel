"""Sizes, durations, names and strict configuration validation."""

import tempfile
import unittest
from datetime import timedelta
from pathlib import Path

from helpers import BASE_TOML
from motel.config import Config
from motel.errors import ConfigError, NotSetUp, Usage
from motel.units import canonical_minutes, check_room, check_task, fmt_td, parse_duration, parse_size_gb, round_up10


class UnitsTest(unittest.TestCase):
    def test_sizes(self):
        self.assertEqual([parse_size_gb(t) for t in ("200G", "200g", "200GB", "1T", "1TB", "200")], [200, 200, 200, 1024, 1024, 200])
        for bad in ("", "0", "0G", "-5G", "1.5G", "G", "200M", "200 G", "1e3", "9999999G", "../1"):
            with self.subTest(bad), self.assertRaises(Usage):
                parse_size_gb(bad)

    def test_durations(self):
        self.assertEqual(parse_duration("90m"), timedelta(minutes=90))
        self.assertEqual(parse_duration("6h"), timedelta(hours=6))
        self.assertEqual(parse_duration("3d"), timedelta(days=3))
        self.assertEqual(parse_duration("0h", allow_zero=True), timedelta(0))
        for bad in ("", "6", "6 h", "6H", "1.5h", "-1h", "0h", "h", "6s", "9999999h"):
            with self.subTest(bad), self.assertRaises(Usage):
                parse_duration(bad)

    def test_formatting(self):
        self.assertEqual([fmt_td(timedelta(minutes=m)) for m in (90, 360, 4320, 0)], ["90m", "6h", "3d", "0m"])
        self.assertEqual(canonical_minutes(timedelta(hours=6)), "360m")
        # The CLI forwards durations to the desk in canonical minutes; long ones must still parse,
        # so an over-long request reaches the cap check (exit 3) instead of failing as bad usage (exit 2).
        for days in (8, 30, 365):
            delta = timedelta(days=days)
            self.assertEqual(parse_duration(canonical_minutes(delta)), delta)
        self.assertEqual(round_up10(1), 10)
        self.assertEqual(round_up10(51), 60)

    def test_names(self):
        for ok in ("video-render", "a", "A_b-1"):
            check_task(ok)
        for bad in ("", "-x", "a/b", "a b", "a" * 33, "é"):
            with self.assertRaises(Usage):
                check_task(bad)
        check_room("video-render-k3f9")
        for bad in ("video-render", "x-ABCD", "../etc-abcd", "x-abc", "x-abcde", "a/b-abcd"):
            with self.assertRaises(Usage):
                check_room(bad)


class ConfigTest(unittest.TestCase):
    def load(self, text):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp, "motel.toml")
            path.write_text(text)
            return Config.load(str(path))

    def test_defaults_match_the_design(self):
        cfg = self.load('[host]\nprovider = "hetzner"\nallow_users = ["alice"]\n')
        self.assertEqual((cfg.mount_root, cfg.filesystem, cfg.default_ttl, cfg.max_ttl_call, cfg.max_lifetime),
                         ("/mnt/motel", "ext4", timedelta(hours=6), timedelta(days=7), timedelta(days=14)))
        self.assertEqual((cfg.grace, cfg.lost_and_found, cfg.min_floor_gb), (timedelta(hours=6), timedelta(hours=24), 50))
        self.assertEqual((cfg.max_floors, cfg.max_floor_gb, cfg.max_total_gb, cfg.monthly_eur), (3, 500, 1000, 30.0))
        self.assertEqual((cfg.server_id, cfg.location), ("auto", "auto"))

    def test_full_example(self):
        cfg = self.load(BASE_TOML)
        self.assertEqual(cfg.allow_users, ("alice", "@agents"))
        self.assertEqual(cfg.price_gb_month_eur, 0.05)

    def test_every_malformed_config_is_rejected_with_a_clear_message(self):
        good = '[host]\nprovider = "hetzner"\nallow_users = ["alice"]\n'
        cases = {
            "missing provider": '[host]\nallow_users = ["alice"]\n',
            "unknown provider": '[host]\nprovider = "nimbus"\nallow_users = ["alice"]\n',
            "no users": '[host]\nprovider = "hetzner"\n',
            "ALL user": '[host]\nprovider = "hetzner"\nallow_users = ["ALL"]\n',
            "user pattern": '[host]\nprovider = "hetzner"\nallow_users = ["a*"]\n',
            "unknown host key": good + "colour = 1\n",
            "unknown section": good + "[extra]\nx = 1\n",
            "bad ttl": good + '[rooms]\ndefault_ttl = "6 hours"\n',
            "ttl type": good + "[rooms]\ndefault_ttl = 6\n",
            "default over max": good + '[rooms]\ndefault_ttl = "9d"\n',
            "call over lifetime": good + '[rooms]\nmax_ttl_call = "30d"\n',
            "root mount": good + '[rooms]\nmount_root = "/"\n',
            "/mnt mount": good + '[rooms]\nmount_root = "/mnt"\n',
            "relative mount": good + '[rooms]\nmount_root = "mnt/motel"\n',
            "dotdot mount": good + '[rooms]\nmount_root = "/mnt/../etc/motel"\n',
            "etc mount": good + '[rooms]\nmount_root = "/etc/motel"\n',
            "xfs": good + '[rooms]\nfilesystem = "xfs"\n',
            "negative cap": good + "[rooms.caps]\nmax_floors = 0\n",
            "bool as int": good + "[rooms.caps]\nmax_floors = true\n",
            "float gb": good + "[rooms.caps]\nmax_floor_gb = 1.5\n",
            "floor over total": good + "[rooms.caps]\nmax_floor_gb = 900\nmax_total_gb = 100\n",
            "negative eur": good + "[rooms.caps]\nmonthly_eur = -1\n",
            "bad never touch": '[host]\nprovider = "hetzner"\nallow_users = ["a"]\nnever_touch = ["../x"]\n',
            "bad server id": '[host]\nprovider = "hetzner"\nallow_users = ["a"]\nserver_id = "x y"\n',
        }
        for label, text in cases.items():
            with self.subTest(label), self.assertRaises(ConfigError) as raised:
                self.load(text)
            self.assertTrue(raised.exception.line().startswith("motel: "))
            self.assertTrue(raised.exception.line().endswith("motel doctor"))

    def test_invalid_toml_and_missing_file(self):
        with self.assertRaises(ConfigError):
            self.load("[host\n")
        with self.assertRaises(NotSetUp):
            Config.load("/nonexistent/motel.toml")

    def test_zero_durations_allowed_only_where_documented(self):
        cfg = self.load('[host]\nprovider = "hetzner"\nallow_users = ["a"]\n[rooms]\nlost_and_found = "0h"\ngrace = "0m"\n')
        self.assertEqual(cfg.lost_and_found, timedelta(0))
        with self.assertRaises(ConfigError):
            self.load('[host]\nprovider = "hetzner"\nallow_users = ["a"]\n[rooms]\ndefault_ttl = "0h"\n')


if __name__ == "__main__":
    unittest.main()
