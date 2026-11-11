"""Installer wiring: cabins (sudoers, user and key tree, housekeeping), `motel install` from a wheel layout, --upgrade."""

import io
import os
import shutil
import stat
import subprocess
import sys
import tempfile
import tomllib
import unittest
import unittest.mock
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from motel import installer
from motel.cabins import desk as cabin_desk
from motel.config import Config
from motel.providers import registry

ROOT = Path(__file__).resolve().parent.parent
SRC = ROOT / "src"


def wheel_layout(base: Path) -> Path:
    """What a wheel install looks like: a site-packages directory holding the package and nothing from a checkout."""
    site = base / "site-packages"
    shutil.copytree(SRC / "motel", site / "motel", ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    return site


class SudoersTest(unittest.TestCase):
    def lines(self, text):
        return [c.strip(" \\\n") for c in text.split("NOPASSWD:")[1].split(",")]

    def test_without_cabins_there_is_no_cabin_rule(self):
        self.assertNotIn("cabin", installer.render_sudoers(["alice"]))

    def test_each_user_cabin_verb_is_pinned_on_its_own_line(self):
        commands = self.lines(installer.render_sudoers(["alice"], cabins=True))
        desk = installer.DESK
        for verb in cabin_desk.USER_VERBS:
            self.assertIn(f"{desk} cabin {verb} *", commands)
        self.assertEqual(sorted(c for c in commands if " cabin " in c and not c.endswith("*")),
                         sorted(f"{desk} cabin {v}" for v in ("ls", "doctor")))

    def test_operator_verbs_and_wildcards_are_absent(self):
        text = installer.render_sudoers(["alice"], cabins=True)
        rule = text.split("NOPASSWD:")[1]
        for forbidden in ("cabin setup", "cabin reap", "cabin resolve", "cabin *", "cabin\n"):
            self.assertNotIn(forbidden, rule)
        self.assertEqual(set(installer.CABIN_USER_VERBS), set(cabin_desk.USER_VERBS))
        self.assertTrue(set(cabin_desk.OPERATOR_VERBS).isdisjoint(installer.CABIN_USER_VERBS))

    @unittest.skipUnless(shutil.which("visudo", path="/usr/sbin:/usr/bin"), "visudo not installed")
    def test_visudo_accepts_the_rule(self):
        with tempfile.NamedTemporaryFile("w", suffix=".sudoers") as handle:
            handle.write(installer.render_sudoers(["alice", "@agents"], cabins=True))
            handle.flush()
            proc = subprocess.run([shutil.which("visudo", path="/usr/sbin:/usr/bin"), "-cqf", handle.name], capture_output=True, text=True)
        self.assertEqual(proc.returncode, 0, proc.stderr)


class HousekeepingTest(unittest.TestCase):
    def test_cabins_have_their_own_service_and_timer_so_a_hung_rooms_pass_cannot_delay_them(self):
        rooms = installer.render_service()
        cabin = installer.render_cabin_service()
        self.assertIn("ExecStart=/usr/local/sbin/motel-desk reap --apply --json", rooms)
        self.assertNotIn("cabin", rooms.replace("motel-desk", ""))
        self.assertNotIn("ExecStopPost", rooms)
        self.assertIn("ExecStart=/usr/local/sbin/motel-desk cabin reap --apply --json", cabin)
        self.assertIn("OnUnitActiveSec=10min", installer.render_timer("cabin housekeeping"))

    def test_the_no_systemd_help_mentions_the_cabin_reap(self):
        err = io.StringIO()
        with redirect_stdout(err), self.assertRaises(SystemExit):
            installer.main(["--help"])
        self.assertIn("cabin reap --apply", " ".join(err.getvalue().split()))

    def test_cron_gets_a_second_line(self):
        lines = [x for x in installer.render_cron(cabins=True).splitlines() if not x.startswith("#")]
        self.assertEqual(len(lines), 2)
        self.assertIn("motel-desk cabin reap --apply --json", lines[1])


class ConfigTest(unittest.TestCase):
    def test_every_cloud_renders_a_valid_config(self):
        for name in registry.provider_names():
            for cabins in (False, True):
                with self.subTest(name, cabins=cabins):
                    cfg = Config.from_dict(tomllib.loads(installer.render_config(["alice"], name, cabins)))
                    self.assertEqual((cfg.provider, bool(cfg.cabins and cfg.cabins.enabled)), (name, cabins))

    def test_unknown_cloud_is_refused(self):
        with self.assertRaises(installer.Usage):
            installer.render_config(["alice"], "nimbus")


class InstallTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.prefix = self.base / "root"
        self.prefix.mkdir()

    def install(self, *args, src=None):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = installer.main(["--prefix", str(self.prefix), "--src", str(src or SRC), *args])
        return code, out.getvalue(), err.getvalue()

    def test_cabins_install_the_key_tree_the_units_and_the_rule(self):
        code, out, err = self.install("--users", "alice", "--cabins", "--no-visudo")
        self.assertEqual(code, 0, err)
        keys = self.prefix / "var/lib/motel-cabin/keys"
        self.assertEqual(stat.S_IMODE((self.prefix / "var/lib/motel-cabin").stat().st_mode), 0o750)
        self.assertEqual(stat.S_IMODE(keys.stat().st_mode), 0o2750)
        units = self.prefix / "etc/systemd/system"
        self.assertIn("motel-desk cabin reap", (units / "motel-cabins-housekeeping.service").read_text())
        self.assertTrue((units / "motel-cabins-housekeeping.timer").is_file())
        self.assertNotIn("cabin", (units / "motel-housekeeping.service").read_text().replace("motel-desk", ""))
        self.assertIn("motel-desk cabin checkin *", (self.prefix / "etc/sudoers.d/motel").read_text())
        self.assertTrue(Config.load(str(self.prefix / "etc/motel/motel.toml")).cabins.enabled)
        self.assertTrue((self.prefix / "usr/local/lib/motel/motel/cabins/cloud-init.yaml").is_file())
        self.assertTrue((self.prefix / "usr/local/lib/motel/motel/cabins/guest_agent.py").is_file())

    def test_without_cabins_nothing_of_them_is_installed(self):
        self.assertEqual(self.install("--users", "alice", "--no-visudo")[0], 0)
        self.assertFalse((self.prefix / "var/lib/motel-cabin").exists())
        self.assertNotIn("cabin", (self.prefix / "etc/sudoers.d/motel").read_text())
        self.assertFalse((self.prefix / "etc/systemd/system/motel-cabins-housekeeping.timer").exists())

    def test_the_existing_config_decides_about_cabins(self):
        self.install("--users", "alice", "--no-visudo")
        code, out, _ = self.install("--cabins", "--no-visudo")
        self.assertEqual(code, 0)
        self.assertIn("does not enable [cabins]", out)
        self.assertFalse((self.prefix / "var/lib/motel-cabin").exists())
        config = self.prefix / "etc/motel/motel.toml"
        config.write_text(config.read_text() + '\n[cabins]\nenabled = true\ntypes = ["cpx32"]\n')
        self.install("--no-visudo")
        self.assertTrue((self.prefix / "var/lib/motel-cabin/keys").is_dir())
        self.assertIn("cabin doctor", (self.prefix / "etc/sudoers.d/motel").read_text())

    def test_every_cloud_is_a_provider_choice(self):
        for name in registry.provider_names():
            with self.subTest(name):
                shutil.rmtree(self.prefix)
                self.prefix.mkdir()
                code, out, err = self.install("--users", "alice", "--provider", name, "--no-visudo")
                self.assertEqual(code, 0, err)
                self.assertIn(f"sudo motel-desk setup {name}", out)

    def test_a_wheel_layout_installs_the_package_and_data_files(self):
        site = wheel_layout(self.base)
        code, out, err = self.install("--users", "alice", "--no-visudo", src=site)
        self.assertEqual(code, 0, err)
        lib = self.prefix / "usr/local/lib/motel/motel"
        self.assertTrue((lib / "desk.py").is_file() and (lib / "cabins/cloud-init.yaml").is_file())
        self.assertIn("Next: sudo motel-desk setup hetzner", out)

    def test_dry_run_prefix_from_a_wheel_layout_changes_nothing(self):
        site = wheel_layout(self.base)
        env = {"PATH": "/usr/bin:/bin"}  # no PYTHONPATH: the package must come from the wheel layout alone
        proc = subprocess.run([sys.executable, "-I", "-c",
                               "import sys; sys.path.insert(0, sys.argv[1]); from motel.cli import main; raise SystemExit(main(sys.argv[2:]))",
                               str(site), "install", "--dry-run", "--prefix", str(self.prefix), "--users", "alice", "--cabins"],
                              capture_output=True, text=True, env=env)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(list(self.prefix.iterdir()), [])
        self.assertIn("/etc/sudoers.d/motel", proc.stdout)
        self.assertIn(f"copy {site}/motel", proc.stdout)

    def test_not_root_gets_a_hint(self):
        if os.geteuid() == 0:
            self.skipTest("running as root")
        err = io.StringIO()
        with redirect_stderr(err):
            code = installer.main([])
        self.assertEqual(code, 2)
        self.assertIn("run as root", err.getvalue())
        self.assertIn(installer.INSTALL_CMD, err.getvalue())

    def test_upgrade_replaces_the_code_and_keeps_config_and_state(self):
        self.install("--users", "alice", "--provider", "vultr", "--no-visudo")
        config = self.prefix / "etc/motel/motel.toml"
        config.write_text(config.read_text().replace('["alice"]', '["alice", "bob"]'))
        (self.prefix / "etc/motel/setup-receipt.json").write_text("{}")
        (self.prefix / "var/lib/motel/state.json").write_text("{}")
        (self.prefix / "etc/motel/credentials/vultr.token").write_text("secret")
        stale = self.prefix / "usr/local/lib/motel/motel/desk.py"
        stale.write_text("# old code\n")
        code, out, err = self.install("--upgrade", "--no-visudo")
        self.assertEqual(code, 0, err)
        self.assertIn("upgraded", out)
        self.assertNotEqual(stale.read_text(), "# old code\n")
        self.assertIn('"bob"', config.read_text())
        self.assertIn('provider = "vultr"', config.read_text())
        self.assertEqual((self.prefix / "etc/motel/credentials/vultr.token").read_text(), "secret")
        self.assertEqual((self.prefix / "var/lib/motel/state.json").read_text(), "{}")
        self.assertIn("bob", (self.prefix / "etc/sudoers.d/motel").read_text())

    def test_upgrade_with_nothing_installed_says_so(self):
        code, _, err = self.install("--upgrade", "--no-visudo")
        self.assertEqual(code, 2)
        self.assertIn("nothing to upgrade", err)
        self.assertEqual(list(self.prefix.iterdir()), [])


if __name__ == "__main__":
    unittest.main()


class CleanupSurvivesTest(unittest.TestCase):
    """Disabling cabins means "no new checkins". The reaper and the verbs that end a cabin outlive the switch."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prefix = Path(self.tmp.name) / "root"
        self.prefix.mkdir()
        self.config = self.prefix / "etc/motel/motel.toml"

    def run_install(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = installer.main(["--prefix", str(self.prefix), "--src", str(SRC), "--no-visudo", *args])
        return code, out.getvalue(), err.getvalue()

    def sudoers(self):
        return (self.prefix / "etc/sudoers.d/motel").read_text()

    def with_cabins(self):
        self.assertEqual(self.run_install("--users", "alice,bob", "--cabins")[0], 0)

    def test_disable_then_upgrade_keeps_the_reaper_and_the_verbs_that_end_a_cabin(self):
        self.with_cabins()
        self.config.write_text(self.config.read_text().replace("enabled = true", "enabled = false"))
        code, _, err = self.run_install("--upgrade")
        self.assertEqual(code, 0, err)
        units = self.prefix / "etc/systemd/system"
        self.assertTrue((units / "motel-cabins-housekeeping.timer").is_file())
        rule = self.sudoers().split("NOPASSWD:")[1]
        for verb in ("checkout", "ls", "status"):
            self.assertIn(f"motel-desk cabin {verb} *", rule)
        for verb in ("checkin", "extend"):
            self.assertNotIn(f"motel-desk cabin {verb}", rule)
        self.assertNotIn("cabin setup", rule)
        self.assertTrue((self.prefix / "var/lib/motel-cabin/keys").is_dir())  # the reaper still needs the guest channel

    def test_cabin_state_alone_keeps_cleanup_even_without_a_cabins_table(self):
        self.assertEqual(self.run_install("--users", "alice")[0], 0)
        self.assertNotIn("cabin", self.sudoers())
        (self.prefix / "var/lib/motel/cabins").mkdir(parents=True)
        (self.prefix / "var/lib/motel/cabins/swift-gull-0123456789ab.json").write_text("{}")
        self.assertEqual(self.run_install("--upgrade")[0], 0)
        self.assertIn("motel-desk cabin checkout *", self.sudoers())
        self.assertNotIn("motel-desk cabin checkin", self.sudoers())

    def test_a_toml_typo_makes_every_reinstall_refuse_and_change_nothing(self):
        self.with_cabins()
        self.config.write_text(self.config.read_text() + "\n[rooms\n")
        before = self.sudoers()
        for args in (["--upgrade"], [], ["--users", "mallory"]):
            with self.subTest(args), unittest.mock.patch.dict(os.environ, {"SUDO_USER": "mallory"}):
                code, _, err = self.run_install(*args)
                self.assertEqual(code, 2)
                self.assertIn("cannot be read", err)
        self.assertEqual(self.sudoers(), before)
        self.assertIn("cabin reap", (self.prefix / "etc/systemd/system/motel-cabins-housekeeping.service").read_text())

    def test_an_existing_config_never_falls_back_to_sudo_user(self):
        self.with_cabins()
        self.config.write_text(self.config.read_text().replace('allow_users = ["alice", "bob"]', "allow_users = []"))
        with unittest.mock.patch.dict(os.environ, {"SUDO_USER": "mallory"}):
            code, _, err = self.run_install("--upgrade")
        self.assertEqual(code, 2)
        self.assertNotIn("mallory", self.sudoers())

    def test_the_doctor_uses_the_same_rule(self):
        from helpers import BASE_TOML, Fixture
        fx = Fixture(toml=BASE_TOML + "\n[cabins]\nenabled = false\n")
        self.addCleanup(fx.close)
        sudoers = Path(fx.paths.real("/etc/sudoers.d/motel"))
        sudoers.parent.mkdir(parents=True)
        users = list(fx.cfg.allow_users)
        sudoers.write_text(installer.render_sudoers(users, cabins=installer.CABINS_CLEANUP))
        checks = {c["check"]: c for c in __import__("json").loads(fx.desk("doctor", "--json")[1])["checks"]}
        self.assertTrue(checks["sudoers rule matches allow_users"]["ok"])
        sudoers.write_text(installer.render_sudoers(users, cabins=installer.CABINS_OFF))
        checks = {c["check"]: c for c in __import__("json").loads(fx.desk("doctor", "--json")[1])["checks"]}
        self.assertFalse(checks["sudoers rule matches allow_users"]["ok"])


class SourceTrustTest(unittest.TestCase):
    """Root copies this tree and later runs it: refuse anything somebody else can change or that carries a link."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.src = wheel_layout(self.base)
        self.prefix = self.base / "root"
        self.prefix.mkdir()

    def install(self, **kw):
        args = dict(users=["alice"], provider="hetzner", systemd=True, cron=False, dry_run=False, visudo=False, log=lambda *_: None,
                    audit=False, audit_src=True, owner_uid=os.getuid())
        args.update(kw)
        return installer.install(str(self.prefix), str(self.src), **args)

    def refused(self, **kw):
        with self.assertRaises(installer.Usage) as ctx:
            self.install(**kw)
        self.assertFalse((self.prefix / "usr/local/lib/motel/motel").exists())
        return ctx.exception.message

    def test_a_clean_root_owned_tree_installs(self):
        self.install()
        self.assertTrue((self.prefix / "usr/local/lib/motel/motel/desk.py").is_file())

    def test_a_tree_owned_by_somebody_else_is_refused(self):
        self.assertIn("owned by uid", self.refused(owner_uid=os.getuid() + 1))

    def test_a_group_or_world_writable_file_is_refused(self):
        for mode in (0o664, 0o646):
            with self.subTest(mode):
                os.chmod(self.src / "motel/desk.py", mode)
                self.assertIn("writable by group or others", self.refused())
        os.chmod(self.src / "motel/desk.py", 0o644)

    def test_a_group_writable_directory_is_refused(self):
        os.chmod(self.src / "motel/cabins", 0o775)
        self.assertIn("writable by group or others", self.refused())

    def test_a_symlink_to_a_secret_is_refused_even_without_the_audit(self):
        secret = self.base / "secret.txt"
        secret.write_text("hunter2")
        os.symlink(secret, self.src / "motel/leak.txt")
        message = self.refused(audit_src=False)
        self.assertIn("symbolic link", message)
        self.assertFalse(list(self.prefix.rglob("leak.txt")))

    def test_a_symlinked_directory_and_a_fifo_are_refused(self):
        os.symlink("/etc", self.src / "motel/etc")
        self.assertIn("symbolic link", self.refused(audit_src=False))
        os.unlink(self.src / "motel/etc")
        os.mkfifo(self.src / "motel/pipe")
        self.assertIn("not a regular file", self.refused(audit_src=False))

    def test_the_dry_run_refuses_too(self):
        os.symlink("/etc/hostname", self.src / "motel/leak.txt")
        with self.assertRaises(installer.Usage):
            self.install(dry_run=True, audit_src=False)

    def test_the_directories_above_the_source_must_be_root_owned(self):
        problems = installer.audit_source(str(self.src / "motel"), owner_uid=os.getuid(), chain=True)
        self.assertTrue(any("/tmp" in p or "not root" in p or "writable" in p for p in problems), problems)

    def test_the_staging_copy_is_private_and_the_swap_keeps_the_old_copy_if_it_fails(self):
        self.install()
        installed = self.prefix / "usr/local/lib/motel/motel"
        (installed / "marker.txt").write_text("old")
        real_rename = os.rename

        def failing(src, dst, *a, **k):
            if str(src).endswith("motel.new"):
                raise OSError("simulated failure")
            return real_rename(src, dst, *a, **k)
        os.rename = failing
        try:
            with self.assertRaises(installer.SystemFault) as ctx:
                self.install()
        finally:
            os.rename = real_rename
        self.assertIn("could not put the new copy in place", ctx.exception.message)
        self.assertIn("simulated failure", ctx.exception.message)
        self.assertTrue(ctx.exception.hint)
        self.assertFalse((self.prefix / "usr/local/lib/motel/motel.new").exists(), "the staging directory is cleaned up")
        self.assertEqual((installed / "marker.txt").read_text(), "old")  # the working install was put back
        self.assertFalse((self.prefix / "usr/local/lib/motel/motel.old").exists())

    def test_leftovers_of_an_interrupted_install_are_cleared(self):
        leftover = self.prefix / "usr/local/lib/motel/motel.new"
        leftover.mkdir(parents=True)
        (leftover / "junk").write_text("x")
        self.install()
        self.assertFalse(leftover.exists())


    def test_a_failed_copy_is_a_motel_message_with_a_hint_and_leaves_the_old_copy(self):
        self.install()
        installed = self.prefix / "usr/local/lib/motel/motel"
        (installed / "marker.txt").write_text("old")
        out, err = io.StringIO(), io.StringIO()
        with unittest.mock.patch("shutil.copytree", side_effect=OSError(28, "No space left on device", "/usr/local/lib/motel/motel.new")):
            with redirect_stdout(out), redirect_stderr(err):
                code = installer.main(["--prefix", str(self.prefix), "--src", str(self.src), "--no-visudo", "--upgrade"])
        self.assertEqual(code, installer.SystemFault.code)
        self.assertRegex(err.getvalue(), r"^motel install: could not copy the package into .*No space left on device.*\. Try: .+\n$")
        self.assertNotIn("Traceback", err.getvalue())
        self.assertEqual((installed / "marker.txt").read_text(), "old")
        self.assertFalse((self.prefix / "usr/local/lib/motel/motel.new").exists())

    def test_an_unexpected_file_error_elsewhere_is_a_message_too(self):
        err = io.StringIO()
        with unittest.mock.patch.object(installer.Installer, "mkdir", side_effect=PermissionError(13, "Permission denied", "/etc/motel")):
            with redirect_stdout(io.StringIO()), redirect_stderr(err):
                code = installer.main(["--prefix", str(self.prefix), "--src", str(self.src), "--no-visudo", "--users", "alice"])
        self.assertEqual(code, installer.SystemFault.code)
        self.assertIn("Permission denied", err.getvalue())
        self.assertIn("Try:", err.getvalue())

    def test_a_run_that_died_between_the_two_renames_is_recovered_not_deleted(self):
        self.install()
        lib = self.prefix / "usr/local/lib/motel"
        (lib / "motel" / "marker.txt").write_text("working copy")
        os.rename(lib / "motel", lib / "motel.old")  # the state after the first rename and before the second
        (lib / "motel.new").mkdir()
        self.install()
        self.assertFalse((lib / "motel.old").exists())
        self.assertFalse((lib / "motel.new").exists())
        self.assertTrue((lib / "motel" / "desk.py").is_file())

    def test_a_dead_run_with_nothing_in_place_gets_its_working_copy_back_before_a_failed_copy(self):
        self.install()
        lib = self.prefix / "usr/local/lib/motel"
        (lib / "motel" / "marker.txt").write_text("working copy")
        os.rename(lib / "motel", lib / "motel.old")
        (lib / "motel.new").mkdir()  # the finished new copy beside it: the signature of a death between the renames
        with unittest.mock.patch("shutil.copytree", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(installer.SystemFault):
                self.install()
        self.assertEqual((lib / "motel" / "marker.txt").read_text(), "working copy")


    def test_an_old_copy_without_a_new_one_is_not_the_signature_of_a_dead_run(self):
        self.install()
        lib = self.prefix / "usr/local/lib/motel"
        (lib / "motel" / "marker.txt").write_text("x")
        os.rename(lib / "motel", lib / "motel.old")      # no motel.new beside it: nothing says a swap was under way
        self.install()
        self.assertFalse((lib / "motel.old").exists())
        self.assertFalse((lib / "motel" / "marker.txt").exists())
        self.assertTrue((lib / "motel" / "desk.py").is_file())

    def test_a_failed_first_install_says_nothing_was_installed(self):
        with unittest.mock.patch("shutil.copytree", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(installer.SystemFault) as ctx:
                self.install()
        self.assertIn("nothing was installed", ctx.exception.message)
        self.assertNotIn("installed copy was not changed", ctx.exception.message)

    def test_a_failed_upgrade_says_the_installed_copy_was_not_changed(self):
        self.install()
        with unittest.mock.patch("shutil.copytree", side_effect=OSError(28, "No space left on device")):
            with self.assertRaises(installer.SystemFault) as ctx:
                self.install()
        self.assertIn("the installed copy was not changed", ctx.exception.message)


class StateProbeTest(unittest.TestCase):
    """Only "does not exist" means no cabin state; an unreadable receipt or directory counts as state."""

    def setUp(self):
        if os.geteuid() == 0:
            self.skipTest("root can read anything, so permissions cannot hide the state")
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)

    def locked(self, path):
        path.chmod(0o000)
        self.addCleanup(path.chmod, 0o755)

    def test_missing_state_is_none(self):
        self.assertFalse(installer.cabin_state_exists(str(self.base / "nope"), str(self.base / "no-receipt")))

    def test_an_unreadable_state_directory_counts_as_in_use(self):
        state = self.base / "cabins"
        state.mkdir()
        self.locked(state)
        self.assertTrue(installer.cabin_state_exists(str(state), str(self.base / "no-receipt")))

    def test_a_receipt_behind_an_unsearchable_directory_counts_as_in_use(self):
        etc = self.base / "etc"
        etc.mkdir()
        (etc / "receipt.json").write_text("{}")
        self.locked(etc)
        self.assertTrue(installer.cabin_state_exists(str(self.base / "nope"), str(etc / "receipt.json")))

    def test_the_desk_uses_the_same_rule(self):
        from motel.cabins.config import CabinPaths
        paths = CabinPaths(str(self.base / "root"))
        self.assertFalse(cabin_desk.cabins_in_use(paths))
        leases = Path(paths.state_dir) / "leases"
        leases.mkdir(parents=True)
        self.assertFalse(cabin_desk.cabins_in_use(paths))
        self.locked(leases)
        self.assertTrue(cabin_desk.cabins_in_use(paths))
        self.locked(Path(paths.state_dir))
        self.assertTrue(cabin_desk.cabins_in_use(paths))


class InterpreterWarningTest(unittest.TestCase):
    def test_a_python_in_a_directory_others_can_write_is_reported(self):
        with tempfile.TemporaryDirectory() as tmp:
            exe = Path(tmp) / "python3"
            exe.write_text("")
            Path(tmp).chmod(0o777)
            problems = installer.interpreter_problems(str(exe), owner_uid=os.getuid())
        self.assertTrue(any("writable by group or others" in p for p in problems), problems)

    def test_the_system_python_is_clean(self):
        if not os.path.exists("/usr/bin/python3") or os.stat("/usr/bin").st_uid != 0:
            self.skipTest("no root-owned /usr/bin/python3 here")
        self.assertEqual([], installer.interpreter_problems("/usr/bin/python3"))


class OffModeTest(unittest.TestCase):
    """Cabins off and no cabin state: the cabin reaper has no job, and an earlier install's unit and cron line go away."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.prefix = Path(self.tmp.name) / "root"
        self.prefix.mkdir()
        self.units = self.prefix / "etc/systemd/system"

    def run_install(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = installer.main(["--prefix", str(self.prefix), "--src", str(SRC), "--no-visudo", *args])
        return code, out.getvalue(), err.getvalue()

    def forget_cabins(self):
        """Take the cabins out of the config and clear the cabin state, as an operator who never used them would have it."""
        config = self.prefix / "etc/motel/motel.toml"
        config.write_text(config.read_text().split("[cabins]")[0])
        shutil.rmtree(self.prefix / "var/lib/motel/cabins", ignore_errors=True)

    def test_the_cabin_unit_and_timer_are_removed(self):
        self.assertEqual(self.run_install("--users", "alice", "--cabins")[0], 0)
        self.assertTrue((self.units / installer.CABIN_TIMER).is_file())
        self.forget_cabins()
        code, out, err = self.run_install("--upgrade")
        self.assertEqual(code, 0, err)
        self.assertFalse((self.units / installer.CABIN_TIMER).exists())
        self.assertFalse((self.units / installer.CABIN_SERVICE).exists())
        self.assertTrue((self.units / "motel-housekeeping.timer").is_file(), "the rooms timer stays")
        self.assertIn(f"remove {installer.UNIT_DIR}/{installer.CABIN_TIMER}", out)

    def test_cabin_state_keeps_the_unit(self):
        self.assertEqual(self.run_install("--users", "alice", "--cabins")[0], 0)
        self.forget_cabins()
        (self.prefix / "var/lib/motel/cabins").mkdir(parents=True)
        (self.prefix / "var/lib/motel/cabins/swift-gull-0123456789ab.json").write_text("{}")
        self.assertEqual(self.run_install("--upgrade")[0], 0)
        self.assertTrue((self.units / installer.CABIN_TIMER).is_file())

    def test_the_cron_line_goes_away(self):
        self.assertEqual(self.run_install("--users", "alice", "--cabins", "--cron")[0], 0)
        cron = self.prefix / "etc/cron.d/motel"
        self.assertIn("cabin reap", cron.read_text())
        self.forget_cabins()
        self.assertEqual(self.run_install("--upgrade", "--cron")[0], 0)
        self.assertNotIn("cabin", cron.read_text())
        self.assertIn("reap --apply", cron.read_text())

    def test_a_dry_run_removes_nothing(self):
        self.assertEqual(self.run_install("--users", "alice", "--cabins")[0], 0)
        self.forget_cabins()
        code, out, _ = self.run_install("--upgrade", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"remove {installer.UNIT_DIR}/{installer.CABIN_TIMER}", out)
        self.assertTrue((self.units / installer.CABIN_TIMER).is_file())


class CabinModeTest(unittest.TestCase):
    def test_no_config_but_cabin_state_still_gets_a_reaper(self):
        self.assertEqual(installer.cabin_mode(None, state_exists=True), installer.CABINS_CLEANUP)

    def test_no_config_no_state_depends_on_the_flag(self):
        self.assertIs(installer.cabin_mode(None, state_exists=False), installer.CABINS_OFF)
        self.assertIs(installer.cabin_mode(None, state_exists=False, wanted=True), installer.CABINS_ON)
        self.assertIs(installer.cabin_mode(None, state_exists=True, wanted=True), installer.CABINS_ON)


class UpgradeFromItselfTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.base = Path(self.tmp.name)
        self.prefix = self.base / "root"
        self.prefix.mkdir()

    def run_from(self, site: Path, *args):
        env = {"PATH": "/usr/bin:/bin"}
        return subprocess.run([sys.executable, "-I", "-c",
                               "import sys; sys.path.insert(0, sys.argv[1]); from motel.installer import main; raise SystemExit(main(sys.argv[2:]))",
                               str(site), "--prefix", str(self.prefix), "--no-visudo", *args],
                              capture_output=True, text=True, env=env)

    def test_the_installed_copy_cannot_upgrade_itself(self):
        site = wheel_layout(self.base)
        first = self.run_from(site, "--users", "alice")
        self.assertEqual(first.returncode, 0, first.stderr)
        installed_lib = self.prefix / "usr/local/lib/motel"
        before = (installed_lib / "motel/desk.py").read_text()
        proc = self.run_from(installed_lib, "--upgrade")  # the default --src is where the module lives: the installed copy
        self.assertEqual(proc.returncode, 2, proc.stdout)
        self.assertIn("installed copy itself", proc.stderr)
        self.assertIn("version 0.1.0", proc.stderr)
        self.assertIn("uvx --from motel==", proc.stderr)
        self.assertEqual((installed_lib / "motel/desk.py").read_text(), before)

    def test_an_upgrade_from_another_source_prints_both_versions(self):
        site = wheel_layout(self.base)
        self.run_from(site, "--users", "alice")
        proc = self.run_from(site, "--upgrade")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertRegex(proc.stdout, r"motel \S+ -> \S+")
