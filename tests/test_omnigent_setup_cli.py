"""`motel omnigent setup | status | mcp-config` against a fake machine: no cloud, no real Omnigent, no uv."""
import contextlib
import io
import json
import os
import stat
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path

from motel import cli as top
from motel.omnigent_setup import cli
from motel.omnigent_setup import probe as probes
from motel.omnigent_setup.probe import Deps, parse_server_argv, scan_servers

CABINS_OK = (0, {"ok": True, "checks": [{"check": "config", "ok": True}, {"check": "cap", "ok": True}]})
FIRST_CABIN = "First cabin"


class Machine:
    """A fake host: an Omnigent environment on disk, plus fake answers for the Python and uv commands run against it."""

    def __init__(self, root: Path, *, kind="uv tool", provider=False, uv=True):
        self.root = root
        self.env = root / "tools" / "omnigent"
        (self.env / "bin").mkdir(parents=True)
        self.python = str(self.env / "bin" / "python")
        Path(self.python).write_text("#!/bin/sh\n")
        os.chmod(self.python, 0o755)
        self.exe = self.env / "bin" / "omnigent"
        self.exe.write_text(f"#!{self.python}\n# entry script\n")
        os.chmod(self.exe, 0o755)
        (root / "bin").mkdir()
        self.link = root / "bin" / "omnigent"
        self.link.symlink_to(self.exe)
        if kind != "system":
            (self.env / "pyvenv.cfg").write_text("home = /usr/bin\n")
        if kind == "uv tool":
            (self.env / "uv-receipt.toml").write_text("[tool]\n")
        self.uv = str(root / "bin" / "uv") if uv else None
        self.present = True
        self.version = "0.21.0"
        self.provider = provider
        self.provider_error = None
        self.install_rc = 0
        self.install_changes_version = False
        self.yaml_available = True
        self.verify_error = None
        self.on_verify = None
        self.cabins = CABINS_OK
        self.servers = []
        self.commands = []
        self.verified = []
        self.home = root / "home"
        self.home.mkdir()

    def which(self, name):
        if name in ("omnigent", "omni"):
            return str(self.link) if self.present and name == "omnigent" else None
        return self.uv if name == "uv" else None

    def run(self, argv, *, input=None, timeout=60):
        self.commands.append(list(argv))
        if argv[0] == self.python and "-I" in argv:
            code = argv[-1]
            if "importlib.metadata as m" in code:
                return 0, self.version + "\n", ""
            if "entry_points" in code:
                info = {"entry": self.provider, "loads": self.provider and not self.provider_error, "registry": self.provider or None,
                        "motel_version": "0.1.0" if self.provider else None, "error": self.provider_error}
                return 0, json.dumps(info) + "\n", ""
            if "yaml" in code:
                request = json.loads(input)
                self.verified.append(request)
                if self.on_verify:
                    self.on_verify()
                if not self.yaml_available:
                    return 0, json.dumps({"available": False}) + "\n", ""
                return 0, json.dumps({"available": True, "ok": self.verify_error is None, "error": self.verify_error}) + "\n", ""
        if argv[1:3] == ["pip", "install"] or argv[1:4] == ["-m", "pip", "install"]:
            if self.install_rc:
                return self.install_rc, "", "error: no matching distribution\n"
            self.provider = True
            if self.install_changes_version:
                self.version = "0.20.0"
            return 0, "installed\n", ""
        raise AssertionError(f"unexpected command {argv}")

    def deps(self):
        return Deps(which=self.which, run=self.run, environ={}, home=str(self.home),
                    now=lambda: datetime(2026, 11, 14, 10, 30, 5, tzinfo=UTC), cabin_doctor=lambda: self.cabins,
                    servers=lambda: self.servers, motel_spec=lambda: "motel==0.1.0")

    @property
    def installs(self):
        return [c for c in self.commands if "install" in c]


class Case(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name).resolve()
        self.m = Machine(self.root)
        self.config = self.root / "server.yaml"

    def go(self, *args, machine=None):
        machine = machine or self.m
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(list(args), deps=machine.deps(), out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def setup(self, *args, **kw):
        return self.go("setup", "--config", str(self.config), *args, **kw)

    def other(self, name, **kw):
        path = self.root / name
        path.mkdir()
        return Machine(path, **kw)

    def backups(self):
        return sorted(p.name for p in self.root.glob("server.yaml.bak-*"))


class FailingStepsTest(Case):
    def test_cabins_not_installed_stops_at_step_1_and_points_at_the_first_cabin_quick_start(self):
        self.m.cabins = (8, {"ok": False, "error": "the front desk is not installed (/usr/local/sbin/motel-desk is missing)",
                             "hint": "sudo uvx --from motel motel install"})
        code, out, err = self.setup()
        self.assertEqual(code, 8)
        self.assertIn("[1/4] Cabins work: FAILED", out)
        self.assertNotIn("[2/4]", out)
        self.assertTrue(err.startswith("motel: ") and "Try: " in err)
        self.assertIn(FIRST_CABIN, err)
        self.assertIn("docs/cabins.md", err)
        self.assertEqual(self.m.commands, [])
        self.assertFalse(self.config.exists())

    def test_a_failing_prerequisite_check_points_at_the_quick_start_and_another_failing_check_at_cabin_doctor(self):
        self.m.cabins = (5, {"ok": False, "checks": [{"check": "setup and prerequisites", "ok": False, "detail": "no token"}]})
        self.assertIn(FIRST_CABIN, self.setup()[2])
        self.m.cabins = (5, {"ok": False, "checks": [{"check": "provider API", "ok": False, "detail": "401"}]})
        code, _out, err = self.setup()
        self.assertEqual(code, 8)
        self.assertIn("Try: motel cabin doctor", err)
        self.assertIn("provider API", err)

    def test_a_cleanup_pending_cabin_alone_does_not_stop_setup(self):
        self.m.cabins = (5, {"ok": False, "checks": [{"check": "cabin cap", "ok": True},
                                                     {"check": "no cleanup-pending cabins", "ok": False, "detail": "x"}]})
        code, out, _err = self.setup("--dry-run")
        self.assertEqual(code, 0)
        self.assertIn("still bills", out)

    def test_a_cabin_check_that_raises_is_a_failed_step_not_a_traceback(self):
        def boom():
            raise RuntimeError("desk exploded")
        deps = self.m.deps()
        deps.cabin_doctor = boom
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["setup", "--config", str(self.config)], deps=deps, out=out, err=err)
        self.assertEqual(code, 8)
        self.assertIn("desk exploded", out.getvalue())

    def test_omnigent_missing_stops_at_step_2_and_says_how_to_install_it(self):
        self.m.present = False
        code, out, err = self.setup()
        self.assertEqual(code, 8)
        self.assertIn("[1/4] Cabins work: ok", out)
        self.assertIn("[2/4] Omnigent is found: FAILED", out)
        self.assertIn("Try: uv tool install omnigent --with motel", err)
        self.assertEqual(self.m.installs, [])

    def test_an_omnigent_whose_version_cannot_be_read_is_not_found(self):
        self.m.version = ""
        code, out, err = self.setup()
        self.assertEqual(code, 8)
        self.assertIn("[2/4]", out)
        self.assertIn("cannot read the omnigent version", err)

    def test_a_failed_install_stops_at_step_3_with_the_command_to_run_by_hand(self):
        self.m.install_rc = 1
        code, out, err = self.setup()
        self.assertEqual(code, 5)
        self.assertIn("[3/4] The provider is visible to Omnigent: FAILED", out)
        self.assertIn(f"Try: {self.m.uv} pip install --python {self.m.python} motel==0.1.0", err)
        self.assertIn("no matching distribution", err)
        self.assertFalse(self.config.exists())

    def test_an_install_that_changes_the_omnigent_version_is_an_error(self):
        self.m.install_changes_version = True
        code, _out, err = self.setup()
        self.assertEqual(code, 5)
        self.assertIn("omnigent version changed", err)
        self.assertFalse(self.config.exists())

    def test_an_entry_point_that_does_not_load_is_reported_with_the_reason(self):
        self.m.provider, self.m.provider_error = True, "ImportError: cannot import name 'SandboxLauncher'"
        code, _out, err = self.setup()
        self.assertEqual(code, 5)
        self.assertIn("SandboxLauncher", err)

    def test_a_system_python_is_never_installed_into(self):
        machine = self.other("sys", kind="system")
        code, out, err = self.go("setup", "--config", str(self.config), machine=machine)
        self.assertEqual(code, 8)
        self.assertIn("[3/4]", out)
        self.assertIn("not a virtual environment", err)
        self.assertEqual(machine.installs, [])


class InstallTest(Case):
    def test_the_provider_is_installed_with_uv_pip_into_the_omnigent_environment(self):
        code, out, _err = self.setup()
        self.assertEqual(code, 0)
        self.assertEqual(self.m.installs, [[self.m.uv, "pip", "install", "--python", self.m.python, "motel==0.1.0"]])
        self.assertIn("[3/4] The provider is visible to Omnigent: installed", out)

    def test_without_uv_it_falls_back_to_the_environments_own_pip(self):
        machine = self.other("nouv", uv=False)
        self.go("setup", "--config", str(self.config), machine=machine)
        self.assertEqual(machine.installs, [[machine.python, "-m", "pip", "install", "motel==0.1.0"]])

    def test_a_provider_that_is_already_visible_is_not_installed_again(self):
        self.m.provider = True
        code, out, _err = self.setup()
        self.assertEqual(code, 0)
        self.assertEqual(self.m.installs, [])
        self.assertIn("[3/4] The provider is visible to Omnigent: ok", out)


class DryRunTest(Case):
    def test_a_dry_run_changes_nothing_and_says_what_it_would_do(self):
        self.config.write_text("admins: [a]\n")
        before = self.config.read_bytes()
        code, out, _err = self.setup("--dry-run")
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_bytes(), before)
        self.assertEqual(self.backups(), [])
        self.assertEqual(self.m.installs, [])
        self.assertFalse(self.m.provider)
        self.assertIn("would install", out)
        self.assertIn(f"Would run: {self.m.uv} pip install --python", out)
        self.assertIn(f"would add the block to {self.config}", out)
        self.assertIn("Dry run: nothing was changed", out)

    def test_a_dry_run_creates_no_file_and_no_directory(self):
        target = self.root / "newdir" / "server.yaml"
        code, out, _err = self.go("setup", "--config", str(target), "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"would create {target}", out)
        self.assertFalse(target.parent.exists())

    def test_a_dry_run_still_stops_at_the_first_failed_step(self):
        self.m.present = False
        code, _out, _err = self.setup("--dry-run")
        self.assertEqual(code, 8)

    def test_a_dry_run_checks_the_edit_without_asking_omnigent_to_parse_it_before_motel_is_installed(self):
        self.setup("--dry-run")
        self.assertEqual([r["parse"] for r in self.m.verified], [False])
        self.m.provider = True
        self.setup("--dry-run")
        self.assertTrue(self.m.verified[-1]["parse"])

    def test_the_dry_run_json(self):
        code, out, _err = self.setup("--dry-run", "--json")
        doc = json.loads(out)
        self.assertEqual(code, 0)
        self.assertEqual((doc["schema"], doc["ok"], doc["dry_run"], doc["changed"]), ("motel/v1", True, True, False))
        self.assertEqual([s["status"] for s in doc["steps"]], ["ok", "ok", "would install", "would change"])
        self.assertEqual(doc["config"]["action"], "create")
        self.assertEqual(doc["next"]["verify"], "omnigent sandbox create --help")


class WriteTest(Case):
    ORIGINAL = "# my config\ndatabase_uri: sqlite:///x.db   # keep me\nadmins:\n  - alice@example.com\n"

    def test_the_block_is_appended_after_a_timestamped_backup_and_the_rest_is_untouched(self):
        self.config.write_text(self.ORIGINAL)
        os.chmod(self.config, 0o640)
        code, out, _err = self.setup("--server-type", "cpx32")
        self.assertEqual(code, 0)
        text = self.config.read_text()
        self.assertTrue(text.startswith(self.ORIGINAL))
        self.assertIn("sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n  motel:\n    server_type: cpx32\n", text)
        self.assertEqual(self.backups(), ["server.yaml.bak-20261114T103005"])
        backup = self.root / self.backups()[0]
        self.assertEqual(backup.read_text(), self.ORIGINAL)
        self.assertEqual(stat.S_IMODE(os.stat(self.config).st_mode), 0o640)
        self.assertEqual(stat.S_IMODE(os.stat(backup).st_mode), 0o640)
        self.assertEqual([p.name for p in self.root.iterdir() if p.name.startswith(".motel-omnigent-")], [])
        self.assertIn(f"(backup: {backup})", out)

    def test_a_new_file_is_private_and_its_directory_is_created(self):
        target = self.root / "newdir" / "server.yaml"
        code, _out, _err = self.go("setup", "--config", str(target))
        self.assertEqual(code, 0)
        self.assertEqual(stat.S_IMODE(os.stat(target).st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(os.stat(target.parent).st_mode), 0o700)
        self.assertIn("provider: motel", target.read_text())

    def test_a_second_run_changes_nothing_and_says_so(self):
        self.config.write_text(self.ORIGINAL)
        self.assertEqual(self.setup("--ttl", "2h")[0], 0)
        first = self.config.read_bytes()
        backups = self.backups()
        code, out, _err = self.setup("--ttl", "2h")
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_bytes(), first)
        self.assertEqual(self.backups(), backups)
        self.assertIn("[4/4] The sandbox block is written: unchanged", out)
        self.assertIn("Nothing changed.", out)
        self.assertEqual(len(self.m.installs), 1)          # the provider install is not repeated either

    def test_a_different_server_url_flag_updates_the_line_and_keeps_its_comment(self):
        self.config.write_text("sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767   # loopback\nafter: 1\n")
        code, _out, _err = self.setup("--server-url", "http://127.0.0.1:7000")
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_text(), "sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:7000   # loopback\nafter: 1\n")

    def test_a_symlinked_config_is_edited_in_place_and_stays_a_link(self):
        real = self.root / "real.yaml"
        real.write_text(self.ORIGINAL)
        self.config.symlink_to(real)
        self.assertEqual(self.setup()[0], 0)
        self.assertTrue(self.config.is_symlink())
        self.assertIn("provider: motel", real.read_text())

    def test_the_edited_text_is_checked_by_omnigents_own_yaml_before_anything_is_written(self):
        self.config.write_text(self.ORIGINAL)
        self.m.verify_error = "ValueError: a key other than sandbox would change"
        code, out, err = self.setup()
        self.assertEqual(code, 3)
        self.assertEqual(self.config.read_text(), self.ORIGINAL)
        self.assertEqual(self.backups(), [])
        self.assertIn("did not pass the check", err)
        self.assertIn("sandbox:\n  provider: motel", out)             # the block to paste
        self.assertEqual(self.m.verified[0]["old"], self.ORIGINAL)

    def test_without_a_yaml_library_in_omnigents_environment_the_text_checks_stand_alone(self):
        self.m.yaml_available = False
        self.assertEqual(self.setup()[0], 0)
        self.assertTrue(self.config.exists())

    def test_a_file_that_changes_during_setup_is_not_overwritten(self):
        self.config.write_text(self.ORIGINAL)
        self.m.on_verify = lambda: self.config.write_text(self.ORIGINAL + "late: 1\n")
        code, _out, err = self.setup()
        self.assertEqual(code, 5)
        self.assertIn("changed while setup was running", err)
        self.assertEqual(self.config.read_text(), self.ORIGINAL + "late: 1\n")

    def test_a_config_that_is_not_a_file_is_refused(self):
        self.config.mkdir()
        code, _out, err = self.setup()
        self.assertEqual(code, 5)
        self.assertIn("not a regular file", err)


class OtherProviderTest(Case):
    MODAL = "sandbox:\n  provider: modal\n  server_url: https://omni.example.com\nafter: 1\n"

    def test_a_block_for_another_provider_is_refused_and_the_exact_block_is_printed(self):
        self.config.write_text(self.MODAL)
        code, out, err = self.setup()
        self.assertEqual(code, 3)
        self.assertEqual(self.config.read_text(), self.MODAL)
        self.assertEqual(self.backups(), [])
        self.assertIn("[4/4] The sandbox block is written: not written", out)
        self.assertIn("sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n", out)
        self.assertIn("provider modal", err)
        self.assertIn("Try: paste the block above", err)

    def test_yes_replaces_it_keeps_the_old_block_as_comments_and_makes_a_backup(self):
        self.config.write_text(self.MODAL)
        code, _out, _err = self.setup("--yes")
        self.assertEqual(code, 0)
        text = self.config.read_text()
        self.assertIn("# sandbox:\n#   provider: modal\n", text)
        self.assertIn("\nsandbox:\n  provider: motel\n", text)
        self.assertTrue(text.endswith("after: 1\n"))
        self.assertEqual((self.root / self.backups()[0]).read_text(), self.MODAL)

    def test_the_providers_list_shape_gets_motel_added_to_the_list(self):
        original = ("name: x   # keep\nsandbox:\n  server_url: https://omni.example.com\n  providers:\n    - provider: modal\n"
                    "    - provider: e2b\n  reaper:\n    enabled: true\n")
        self.config.write_text(original)
        code, _out, _err = self.setup()
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_text(), original.replace("    - provider: e2b\n", "    - provider: e2b\n    - provider: motel\n"))

    def test_the_json_failure_names_the_failed_step_and_carries_the_block(self):
        self.config.write_text(self.MODAL)
        code, out, err = self.setup("--json")
        doc = json.loads(out)
        self.assertEqual((code, err), (3, ""))
        self.assertEqual((doc["ok"], doc["code"]), (False, 3))
        self.assertIn("provider modal", doc["error"])
        self.assertTrue(doc["hint"])
        self.assertEqual(doc["steps"][-1]["ok"], False)
        self.assertIn("provider: motel", doc["config"]["block"])


class WhereTheConfigLivesTest(Case):
    def test_the_default_is_the_omnigent_user_config_that_its_own_server_reads(self):
        code, out, _err = self.go("setup", "--dry-run")
        self.assertEqual(code, 0)
        self.assertIn(f"would create {self.m.home}/.omnigent/config.yaml", out)
        self.assertIn("omnigent stop, then omnigent start", out)         # the server Omnigent starts itself reads this file
        self.assertIn(f"omnigent server -c {self.m.home}/.omnigent/config.yaml", out)

    def test_omnigent_config_home_moves_the_default(self):
        deps = self.m.deps()
        deps.environ = {"OMNIGENT_CONFIG_HOME": str(self.root / "elsewhere")}
        out = io.StringIO()
        cli.main(["setup", "--dry-run"], deps=deps, out=out, err=io.StringIO())
        self.assertIn(f"would create {self.root}/elsewhere/config.yaml", out.getvalue())

    def test_the_config_of_a_running_server_wins_over_the_default_and_its_port_sets_the_url(self):
        self.m.servers = [{"pid": 4242, "config": str(self.config), "port": 7001}]
        self.config.write_text("a: 1\n")
        code, out, _err = self.go("setup")
        self.assertEqual(code, 0)
        self.assertIn("server_url: http://127.0.0.1:7001", self.config.read_text())
        self.assertIn("The running server (pid 4242) uses this file", out)

    def test_a_running_server_that_ignores_the_users_config_is_told_to_use_dash_c_and_not_to_run_omnigent_start(self):
        self.m.servers = [{"pid": 77, "config": None, "port": 6767}]
        deps = self.m.deps()
        deps.environ = {"OMNIGENT_CONFIG_HOME": str(self.root)}
        out = io.StringIO()
        cli.main(["setup", "--config", str(self.root / "config.yaml")], deps=deps, out=out, err=io.StringIO())
        self.assertIn("start it with -c", out.getvalue())
        self.assertNotIn("omnigent stop, then omnigent start", out.getvalue())

    def test_a_running_server_without_the_file_is_told_to_add_dash_c(self):
        self.m.servers = [{"pid": 4242, "config": None, "port": None}]
        _code, out, _err = self.setup()
        self.assertIn("A server is running (pid 4242) that was not started with this file, so it ignores the block", out)
        self.assertIn(f"omnigent server -c {self.config}", out)
        self.assertNotIn("omnigent stop, then omnigent start", out)      # this file is not the one Omnigent's own server reads
        self.assertIn("Setup does not restart it", out)
        self.assertIn("omnigent sandbox create --help", out)
        self.assertEqual([c for c in self.m.commands if "stop" in c or "restart" in c], [])

    def test_setup_never_runs_anything_but_the_python_probes_and_the_install(self):
        self.setup()
        for command in self.m.commands:
            self.assertTrue(command[0] in (self.m.python, self.m.uv), command)


class StatusTest(Case):
    def test_status_json_before_and_after_setup(self):
        code, out, _err = self.go("status", "--config", str(self.config), "--json")
        doc = json.loads(out)
        self.assertEqual((code, doc["schema"], doc["ok"]), (8, "motel/v1", False))
        by = {c["check"]: c for c in doc["checks"]}
        self.assertEqual(set(by), {"cabins", "omnigent", "provider visible", "config block", "block matches setup", "server"})
        self.assertTrue(by["cabins"]["ok"] and by["omnigent"]["ok"])
        self.assertFalse(by["provider visible"]["ok"] or by["config block"]["ok"] or by["block matches setup"]["ok"])
        self.assertEqual(doc["omnigent"]["version"], "0.21.0")
        self.assertEqual(doc["omnigent"]["kind"], "uv tool")
        self.assertEqual((doc["config"]["exists"], doc["config"]["present"], doc["config"]["matches"]), (False, False, False))
        installs = len(self.m.installs)
        self.assertEqual(installs, 0)                                       # status never installs
        self.assertFalse(self.config.exists())

        self.assertEqual(self.setup()[0], 0)
        code, out, _err = self.go("status", "--config", str(self.config), "--json")
        doc = json.loads(out)
        self.assertEqual((code, doc["ok"]), (0, True))
        self.assertTrue(all(c["ok"] for c in doc["checks"]))

    def test_status_says_when_the_block_differs_from_what_setup_would_write(self):
        self.m.provider = True
        self.config.write_text("sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n")
        code, out, _err = self.go("status", "--config", str(self.config), "--json", "--ttl", "2h")
        doc = json.loads(out)
        by = {c["check"]: c for c in doc["checks"]}
        self.assertTrue(by["config block"]["ok"])
        self.assertFalse(by["block matches setup"]["ok"])
        self.assertEqual((code, doc["config"]["action"]), (8, "update"))

    def test_status_text_is_a_short_table(self):
        code, out, _err = self.go("status", "--config", str(self.config))
        self.assertEqual(code, 8)
        lines = out.splitlines()
        self.assertTrue(lines[0].startswith("cabins "))
        self.assertTrue(any(line.startswith("provider visible") and "fail" in line for line in lines))
        self.assertEqual(lines[-1], "not ready: run motel omnigent setup")

    def test_status_is_read_only(self):
        self.go("status", "--config", str(self.config))
        self.assertEqual(self.m.installs, [])
        self.assertFalse(self.config.exists())
        self.assertEqual(self.m.verified, [])

    def test_status_warns_when_the_running_server_does_not_use_the_file(self):
        self.m.servers = [{"pid": 9, "config": None, "port": None}]
        _code, out, _err = self.go("status", "--config", str(self.config), "--json")
        row = {c["check"]: c for c in json.loads(out)["checks"]}["server"]
        self.assertEqual(row["level"], "warn")

    def test_status_without_cabins_says_how_to_set_them_up(self):
        self.m.cabins = (8, {"ok": False, "error": "the front desk is not installed", "hint": "x"})
        _code, out, _err = self.go("status", "--config", str(self.config))
        self.assertIn(FIRST_CABIN, out)


IMAGES_OK = (0, {"schema": "motel/v1", "ok": True, "images": [
    {"id": 4711, "name": "omnigent", "created": "2026-11-08T12:30:00Z", "image_size_gb": 1.5, "disk_size_gb": 40}]})
IMAGE_LINES = "    image: omnigent\n    bootstrap: none\n"


class ImageTest(Case):
    def deps_with(self, images):
        deps = self.m.deps()
        deps.cabin_images = lambda: images
        return deps

    def status(self, images=IMAGES_OK, *args):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["status", "--config", str(self.config), "--json", *args], deps=self.deps_with(images), out=out, err=err)
        return code, json.loads(out.getvalue())

    def test_setup_image_writes_the_image_and_bootstrap_none_into_the_motel_block(self):
        code, _out, _err = self.setup("--image", "omnigent")
        self.assertEqual(code, 0)
        self.assertEqual(self.config.read_text().split("\n", 1)[1],
                         "sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n  motel:\n" + IMAGE_LINES)

    def test_a_second_run_changes_nothing(self):
        self.setup("--image", "omnigent")
        before = self.config.read_text()
        code, out, _err = self.setup("--image", "omnigent")
        self.assertEqual(code, 0)
        self.assertIn("unchanged", out)
        self.assertEqual(self.config.read_text(), before)
        self.assertEqual(self.backups(), [])

    def test_it_updates_an_existing_block_in_place_and_keeps_its_other_keys_and_comments(self):
        self.m.provider = True
        self.config.write_text("sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n  motel:\n"
                               "    ttl: 2h   # keep\n    bootstrap: pip\n")
        code, _out, _err = self.setup("--image", "omnigent")
        self.assertEqual(code, 0)
        text = self.config.read_text()
        self.assertIn("    ttl: 2h   # keep\n", text)
        self.assertIn("    bootstrap: none\n", text)
        self.assertNotIn("bootstrap: pip", text)
        self.assertEqual(text.count("image: omnigent"), 1)
        self.assertEqual(len(self.backups()), 1)

    def test_it_never_writes_an_image_unless_asked(self):
        self.setup()
        self.assertNotIn("image", self.config.read_text())
        self.assertNotIn("bootstrap", self.config.read_text())

    def test_a_bad_image_name_is_refused_before_anything_runs(self):
        for bad in ("Omni gent", "../x", "ubuntu-24.04", "12345", "x" * 41, "-x"):
            err = io.StringIO()
            code = cli.main(["setup", "--image", bad, "--dry-run"], deps=Deps(), out=io.StringIO(), err=err)
            self.assertEqual(code, 2, bad)
            self.assertIn("Try:", err.getvalue())

    def test_status_reports_that_the_configured_image_exists(self):
        self.setup("--image", "omnigent")
        code, doc = self.status()
        by = {c["check"]: c for c in doc["checks"]}
        self.assertEqual(code, 0)
        self.assertTrue(by["baked image"]["ok"] and "id 4711" in by["baked image"]["detail"])
        self.assertNotIn("bootstrap", by)
        self.assertEqual(doc["image"], {"name": "omnigent", "bootstrap": "none", "exists": True})

    def test_status_fails_and_says_what_to_run_when_the_image_is_not_on_this_host(self):
        self.setup("--image", "omnigent")
        code, doc = self.status((0, {"ok": True, "images": [{"id": 9, "name": "builder"}]}))
        row = {c["check"]: c for c in doc["checks"]}["baked image"]
        self.assertEqual((code, doc["ok"], row["ok"]), (8, False, False))
        self.assertIn("no baked image named omnigent", row["detail"])
        self.assertIn("it offers: builder", row["detail"])
        self.assertIn("motel omnigent bake-script", row["detail"])
        self.assertEqual(doc["image"]["exists"], False)

    def test_status_says_when_the_desk_cannot_list_images(self):
        self.setup("--image", "omnigent")
        code, doc = self.status((2, {"ok": False, "error": "the installed front desk is older", "hint": "sudo motel install --upgrade"}))
        row = {c["check"]: c for c in doc["checks"]}["baked image"]
        self.assertEqual(code, 8)
        self.assertIn("cannot list the baked images: the installed front desk is older", row["detail"])

    def test_status_names_the_ambiguity_when_two_images_share_the_name(self):
        self.setup("--image", "omnigent")
        rows = [{"id": 1, "name": "omnigent"}, {"id": 2, "name": "omnigent"}]
        code, doc = self.status((0, {"ok": True, "images": rows}))
        self.assertEqual(code, 8)
        self.assertIn("more than one baked image is named omnigent (ids 1, 2)", {c["check"]: c for c in doc["checks"]}["baked image"]["detail"])

    def test_status_notes_an_image_without_bootstrap_none_and_asks_the_desk_nothing_without_an_image(self):
        self.m.provider = True
        self.config.write_text("sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n  motel:\n    image: omnigent\n")
        _code, doc = self.status()
        by = {c["check"]: c for c in doc["checks"]}
        self.assertEqual(by["bootstrap"]["level"], "info")
        calls = []
        deps = self.m.deps()
        deps.cabin_images = lambda: calls.append(1) or IMAGES_OK
        self.config.write_text("sandbox:\n  provider: motel\n  server_url: http://127.0.0.1:6767\n")
        cli.main(["status", "--config", str(self.config), "--json"], deps=deps, out=io.StringIO(), err=io.StringIO())
        self.assertEqual(calls, [])

    def test_status_is_still_read_only_with_an_image(self):
        self.setup("--image", "omnigent")
        text = self.config.read_text()
        installs = len(self.m.installs)
        self.status()
        self.assertEqual((self.config.read_text(), len(self.m.installs)), (text, installs))


class BakeScriptTest(Case):
    def script(self, *args, environ=None, machine=None):
        machine = machine or self.m
        deps = machine.deps()
        deps.environ = environ or {}
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["bake-script", *args], deps=deps, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def test_it_prints_a_script_that_pins_the_version_given(self):
        code, out, err = self.script("--omnigent-version", "0.22.1")
        self.assertEqual(code, 0)
        self.assertTrue(out.startswith("#!/bin/bash\n"))
        self.assertIn("OMNIGENT_VERSION=0.22.1\n", out)
        self.assertIn('"omnigent==$OMNIGENT_VERSION"', out)
        self.assertIn("sudo motel-desk tour bake omnigent --script setup.sh", err)

    def test_the_output_is_stable(self):
        first = self.script("--omnigent-version", "0.22.1")[1]
        self.assertEqual(first, self.script("--omnigent-version", "0.22.1")[1])
        self.assertNotEqual(first, self.script("--omnigent-version", "0.22.2")[1])

    def test_the_version_comes_from_the_flag_then_the_environment_then_the_omnigent_installed_here(self):
        self.assertIn("OMNIGENT_VERSION=0.30.0\n", self.script("--omnigent-version", "0.30.0", environ={"OMNIGENT_VERSION": "0.29.0"})[1])
        self.assertIn("OMNIGENT_VERSION=0.29.0\n", self.script(environ={"OMNIGENT_VERSION": "0.29.0"})[1])
        self.assertIn("OMNIGENT_VERSION=0.21.0\n", self.script()[1])                    # the fake machine has 0.21.0

    def test_without_a_version_and_without_omnigent_here_it_asks_for_one(self):
        self.m.present = False
        code, out, err = self.script()
        self.assertEqual((code, out), (2, ""))
        self.assertIn("--omnigent-version", err)

    def test_a_version_that_is_not_a_release_is_refused(self):
        for bad in ("latest", "1.0.0.dev3", "0.21.0; rm -rf /", "$(id)", "0.21.0 --pre", ""):
            code, out, err = self.script("--omnigent-version", bad)
            self.assertEqual((code, out), (2, ""), bad)
            self.assertIn("Try:", err)

    def test_the_default_installs_the_harness_clis_and_the_flags_change_them(self):
        default = self.script("--omnigent-version", "0.21.0")[1]
        self.assertIn("apt-get install -y -q git tmux curl ca-certificates python3-venv python3-pip python3-yaml nodejs npm", default)
        self.assertIn("npm install -g --prefix /usr/local --no-audit --no-fund --loglevel=error @anthropic-ai/claude-code @openai/codex", default)
        custom = self.script("--omnigent-version", "0.21.0", "--harness-package", "@openai/codex@1.2.3")[1]
        self.assertIn("--loglevel=error @openai/codex@1.2.3\n", custom)
        self.assertNotIn("claude-code", custom)
        none = self.script("--omnigent-version", "0.21.0", "--no-harness")[1]
        self.assertNotIn("npm install", none)
        self.assertNotIn("nodejs", none)
        self.assertIn("python3-yaml\n", none)

    def test_a_bad_package_spec_or_conflicting_flags_are_refused(self):
        for args in (("--harness-package", "x; id"), ("--harness-package", "$(id)"), ("--no-harness", "--harness-package", "x")):
            code, out, _err = self.script("--omnigent-version", "0.21.0", *args)
            self.assertEqual((code, out), (2, ""), args)

    def test_the_script_is_root_only_work_on_the_cabin_and_changes_nothing_here(self):
        _code, out, _err = self.script("--omnigent-version", "0.21.0")
        self.assertEqual(self.m.installs, [])
        self.assertIn("set -euo pipefail", out)
        self.assertIn("export HOME=/root", out)
        self.assertIn('ln -sf "$VENV/bin/omnigent" "$LINK"', out)
        self.assertIn("LINK=/usr/local/bin/omnigent", out)
        self.assertIn("--no-cache-dir", out)
        self.assertIn("rm -rf /root/.npm /root/.cache /root/.npmrc", out)
        for forbidden in ("curl |", "| sh", "| bash", "sk-", "token", "login", "apikey", "API_KEY"):
            self.assertNotIn(forbidden, out)

    @unittest.skipUnless(os.path.exists("/bin/bash") or os.path.exists("/usr/bin/bash"), "bash not installed")
    def test_bash_accepts_the_syntax(self):
        import subprocess
        _code, out, _err = self.script("--omnigent-version", "0.21.0")
        proc = subprocess.run(["bash", "-n"], input=out, capture_output=True, text=True)
        self.assertEqual((proc.returncode, proc.stderr), (0, ""))

    def test_the_help_lists_the_three_commands(self):
        out = io.StringIO()
        cli.main([], out=out, err=io.StringIO())
        text = out.getvalue()
        self.assertIn("motel omnigent bake-script > setup.sh && sudo motel-desk tour bake omnigent --script setup.sh", text)
        self.assertIn("motel omnigent setup --image omnigent", text)


class McpConfigTest(Case):
    def test_it_prints_a_json_snippet_for_motel_mcp_with_tours_and_nothing_else(self):
        code, out, err = self.go("mcp-config")
        self.assertEqual(code, 0)
        doc = json.loads(out)
        self.assertEqual(doc, {"mcpServers": {"motel": {"command": "motel", "args": ["mcp", "--with-tours"]}}})
        self.assertIn("cannot start tours", err)
        self.assertEqual(self.m.commands, [])

    def test_the_snippet_matches_what_the_mcp_server_accepts(self):
        from motel import mcp_server
        args = cli.mcp_snippet()["mcpServers"]["motel"]["args"]
        self.assertEqual(args[0], "mcp")
        *_rest, with_tours = mcp_server._parse_flags(args[1:])
        self.assertTrue(with_tours)


class ParserAndRoutingTest(unittest.TestCase):
    def test_no_verb_prints_the_help_and_exits_2(self):
        out = io.StringIO()
        self.assertEqual(cli.main([], out=out, err=io.StringIO()), 2)
        self.assertIn("examples:", out.getvalue())
        self.assertIn("motel omnigent setup --dry-run", out.getvalue())

    def test_help_exits_0(self):
        out = io.StringIO()
        self.assertEqual(cli.main(["--help"], out=out, err=io.StringIO()), 0)
        self.assertIn("EXPERIMENTAL", out.getvalue())

    def test_a_bad_flag_is_a_usage_error_with_a_next_command(self):
        err = io.StringIO()
        self.assertEqual(cli.main(["setup", "--nope"], out=io.StringIO(), err=err), 2)
        self.assertRegex(err.getvalue(), r"^motel: .*Try: motel omnigent --help\n$")

    def test_a_bad_value_is_refused_before_anything_runs(self):
        for flag, value in (("--server-url", "not a url"), ("--ttl", "soon"), ("--server-type", "CPX 32")):
            err = io.StringIO()
            code = cli.main(["setup", flag, value, "--dry-run"], deps=Deps(), out=io.StringIO(), err=err)
            self.assertEqual(code, 2, flag)
            self.assertIn("Try:", err.getvalue())

    def test_motel_routes_the_word_omnigent(self):
        self.assertIn("omnigent", top.ROUTES)
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.assertEqual(top.main(["omnigent", "--help"]), 0)
        self.assertIn("motel omnigent setup", out.getvalue())

    def test_the_top_level_help_lists_it(self):
        self.assertIn("omnigent", {name for _title, items in top.HELP_GROUPS for name, _text in items})


class ServerScanTest(unittest.TestCase):
    def test_argv_of_an_omnigent_server(self):
        self.assertEqual(parse_server_argv(["/x/bin/python", "/x/bin/omnigent", "server", "-c", "conf.yaml", "--port", "7001"], "/srv/o"),
                         {"config": "/srv/o/conf.yaml", "port": 7001})
        self.assertEqual(parse_server_argv(["omni", "server", "--config=/etc/o.yaml", "--port=6767", "--no-open"]),
                         {"config": "/etc/o.yaml", "port": 6767})
        self.assertEqual(parse_server_argv(["omnigent", "server"]), {"config": None, "port": None})
        self.assertEqual(parse_server_argv(["omnigent", "server", "-c", "rel.yaml"], None)["config"], None)       # cwd unreadable
        self.assertEqual(parse_server_argv(["omnigent", "server", "-c", "/abs.yaml"], None)["config"], "/abs.yaml")
        spawned = ["/x/bin/python", "-P", "-m", "omnigent.cli", "server", "--host", "127.0.0.1", "--port", "6767", "--config", "/u/config.yaml"]
        self.assertEqual(parse_server_argv(spawned), {"config": "/u/config.yaml", "port": 6767})

    def test_other_commands_are_not_servers(self):
        for argv in (["omnigent", "run"], ["omnigent"], ["vim", "server"], ["/x/python", "-m", "http.server"], []):
            self.assertIsNone(parse_server_argv(argv), argv)

    def test_scan_reads_a_proc_tree_of_this_user(self):
        with tempfile.TemporaryDirectory() as tmp:
            for pid, argv in (("100", ["omnigent", "server", "-c", "a.yaml"]), ("101", ["bash"]), ("x", ["omnigent", "server"])):
                d = Path(tmp) / pid
                d.mkdir()
                (d / "cmdline").write_bytes("\0".join(argv).encode() + b"\0")
                if pid != "x":
                    os.symlink("/srv/work", d / "cwd")
            found = scan_servers(os.getuid(), tmp)
            self.assertEqual(found, [{"pid": 100, "config": "/srv/work/a.yaml", "port": None}])
            self.assertEqual(scan_servers(os.getuid() + 1, tmp), [])
        self.assertEqual(scan_servers(os.getuid(), "/nonexistent-proc"), [])


class ProbeTest(Case):
    def test_the_omnigent_environment_is_found_behind_the_symlink_through_the_shebang(self):
        env = probes.find_omnigent(self.m.deps())
        self.assertEqual((env.python, env.kind, env.version), (self.m.python, "uv tool", "0.21.0"))
        self.assertEqual(env.root, str(self.m.env))

    def test_a_plain_virtualenv_and_an_env_style_shebang(self):
        machine = self.other("venv2", kind="virtualenv")
        self.assertEqual(probes.find_omnigent(machine.deps()).kind, "virtualenv")
        machine.exe.write_text("#!/usr/bin/env python3\n")
        self.assertEqual(probes.find_omnigent(machine.deps()).python, machine.python)      # falls back to the sibling python

    def test_motel_requirement_is_a_path_or_the_released_version(self):
        spec = probes.motel_requirement()
        self.assertTrue(os.path.isdir(spec) or spec.startswith("motel"), spec)

    def test_the_provider_probe_script_is_valid_python(self):
        compile(probes.PROVIDER_PROBE, "probe", "exec")
        compile(cli.VERIFY_SCRIPT, "verify", "exec")


if __name__ == "__main__":
    unittest.main()
