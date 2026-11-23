"""The installer's generated files, checked in a temporary prefix (never as root, never on the real system)."""

import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from motel import installer
from motel.errors import Usage
from platform_marks import linux_only

ROOT = Path(__file__).resolve().parent.parent


class RenderTest(unittest.TestCase):
    def test_sudoers_lists_exactly_the_user_verbs(self):
        text = installer.render_sudoers(["alice", "@agents"])
        self.assertIn("alice,%agents ALL=(root) NOPASSWD:", text)
        for verb in ("checkin", "ls", "status", "extend", "checkout", "restore", "doctor"):
            self.assertIn(f"/usr/local/sbin/motel-desk {verb}", text)
        self.assertNotIn("reap", text.split("NOPASSWD:")[1])
        self.assertNotIn("setup", text.split("NOPASSWD:")[1])
        self.assertIn("!setenv", text)
        self.assertNotIn("ALL=(ALL)", text)

    def test_dangerous_user_names_are_refused(self):
        for bad in ("ALL", "alice, bob", "%wheel", "a*", "", "!alice", "alice\nbob ALL=(ALL) ALL", "#1000", "@ALL", "Alice"):
            with self.subTest(bad), self.assertRaises(Usage):
                installer.render_sudoers([bad])
        with self.assertRaises(Usage):
            installer.render_sudoers([])

    def test_units(self):
        self.assertIn("ExecStart=/usr/local/sbin/motel-desk reap --apply --json", installer.render_service())
        self.assertIn("Type=oneshot", installer.render_service())
        timer = installer.render_timer()
        self.assertIn("OnUnitActiveSec=10min", timer)
        self.assertIn("WantedBy=timers.target", timer)
        self.assertIn("*/10 * * * * root /usr/local/sbin/motel-desk reap --apply --json", installer.render_cron())

    def test_entry_scripts_ignore_the_environment(self):
        text = installer.render_entry("motel.desk")
        self.assertTrue(text.startswith("#!/usr/bin/python3 -I\n"))
        self.assertIn("/usr/local/lib/motel", text)

    def test_default_config_is_valid(self):
        import tomllib

        from motel.config import Config
        for provider in ("hetzner", "local"):
            Config.from_dict(tomllib.loads(installer.render_config(["alice"], provider)))


@linux_only  # install.sh runs the system python3 (older than 3.11 on macOS) and the installer is Linux-only
class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prefix = Path(self.tmp.name)

    def run_sh(self, *args):
        return subprocess.run(["sh", str(ROOT / "install.sh"), "--prefix", str(self.prefix), *args], capture_output=True, text=True)

    def test_dry_run_changes_nothing(self):
        proc = self.run_sh("--dry-run", "--users", "alice")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(list(self.prefix.iterdir()), [])
        self.assertIn("/etc/sudoers.d/motel", proc.stdout)

    def test_install_writes_the_expected_tree(self):
        proc = self.run_sh("--users", "alice,@agents")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        modes = {p: stat.S_IMODE((self.prefix / p).stat().st_mode) for p in (
            "usr/local/sbin/motel-desk", "usr/local/bin/motel", "etc/sudoers.d/motel", "etc/motel", "etc/motel/credentials",
            "var/lib/motel", "etc/motel/motel.toml", "etc/systemd/system/motel-housekeeping.timer",
            "etc/systemd/system/motel-housekeeping.service", "usr/local/lib/motel/motel/desk.py")}
        self.assertEqual(modes["usr/local/sbin/motel-desk"], 0o755)
        self.assertEqual(modes["etc/sudoers.d/motel"], 0o440)
        self.assertEqual(modes["etc/motel/credentials"], 0o700)
        self.assertEqual(modes["var/lib/motel"], 0o700)
        self.assertEqual(modes["usr/local/lib/motel/motel/desk.py"], 0o644)
        self.assertIn('allow_users = ["alice", "@agents"]', (self.prefix / "etc/motel/motel.toml").read_text())
        if shutil.which("visudo", path="/usr/sbin:/usr/bin"):
            self.assertIn("visudo: rule is valid", proc.stdout)

    def test_rerun_keeps_an_existing_config_and_takes_users_from_it(self):
        self.run_sh("--users", "alice")
        config = self.prefix / "etc/motel/motel.toml"
        config.write_text(config.read_text().replace('["alice"]', '["alice", "bob"]'))
        self.run_sh("--users", "someone-else")
        self.assertIn("bob", config.read_text())
        self.assertIn("bob", (self.prefix / "etc/sudoers.d/motel").read_text())
        self.assertNotIn("someone-else", (self.prefix / "etc/sudoers.d/motel").read_text())

    def test_cron_alternative(self):
        self.assertEqual(self.run_sh("--users", "alice", "--cron").returncode, 0)
        self.assertTrue((self.prefix / "etc/cron.d/motel").exists())
        self.assertFalse((self.prefix / "etc/systemd/system/motel-housekeeping.timer").exists())

    def test_bad_users_fail_before_anything_is_written(self):
        proc = self.run_sh("--users", "ALL")
        self.assertNotEqual(proc.returncode, 0)
        self.assertEqual(list(self.prefix.iterdir()), [])

    def test_installed_cli_entry_runs_with_a_scrubbed_interpreter(self):
        self.run_sh("--users", "alice")
        entry = installer.render_entry("motel.cli", lib=str(self.prefix / "usr/local/lib/motel"))
        script = self.prefix / "motel-test"
        script.write_text(entry)
        env = {"PATH": "/usr/bin:/bin", "PYTHONPATH": "/nonexistent"}
        proc = subprocess.run([sys.executable, "-I", str(script), "--version"], capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertTrue(proc.stdout.startswith("motel 0.2.0"))

    def test_no_pycache_or_tests_are_installed(self):
        self.run_sh("--users", "alice")
        found = [p for p in (self.prefix / "usr/local/lib/motel").rglob("*") if p.name in ("__pycache__", "tests") or p.suffix == ".pyc"]
        self.assertEqual(found, [])


if __name__ == "__main__":
    unittest.main()
