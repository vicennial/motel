"""The unprivileged CLI and the desk behind it: argument validation, output contract, exit codes."""

import io
import json
import os
import unittest

from helpers import Fixture
from motel import __version__, cli, desk
from motel.errors import EXIT_NOT_SET_UP, INSTALL_HINT


class Harness:
    """Runs `motel ...` in-process, with the desk replaced by an in-process call on a fixture."""

    def __init__(self, fx):
        self.fx, self.seen = fx, []

    def runner(self, argv):
        self.seen.append(argv)
        out, err = io.StringIO(), io.StringIO()
        code = desk.run(argv, paths=self.fx.paths, caller=self.fx.caller, make_provider=lambda c, p: self.fx.provider,
                        sysops=self.fx.sysops, clock=self.fx.clock, out=out, err=err, groups_fn=lambda c: set())
        return code, out.getvalue(), err.getvalue()

    def __call__(self, *argv, runner=None):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(list(argv), runner=runner or self.runner, out=out, err=err)
        return code, out.getvalue(), err.getvalue()


class CliTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.motel = Harness(self.fx)

    def test_version(self):
        code, out, _ = self.motel("--version")
        self.assertEqual((code, out.strip()), (0, f"motel {__version__}"))

    def test_checkin_prints_only_the_path_on_stdout(self):
        code, out, err = self.motel("checkin", "10G", "--for", "2h", "--task", "video-render")
        self.assertEqual(code, 0, err)
        self.assertEqual(out.count("\n"), 1)
        self.assertTrue(os.path.isdir(out.strip()))
        self.assertIn("video-render-", out)
        self.assertIn("Disposable", err)
        self.assertEqual(self.motel.seen[0], ["checkin", "--size", "10G", "--task", "video-render", "--for", "120m", "--json"])

    def test_acquire_alias_and_plain_gb(self):
        code, out, _ = self.motel("acquire", "5")
        self.assertEqual(code, 0)
        self.assertEqual(self.motel.seen[0][:3], ["checkin", "--size", "5G"])

    def test_json_shape_for_checkin_status_ls(self):
        code, out, _ = self.motel("checkin", "10G", "--json")
        doc = json.loads(out)
        self.assertEqual(doc["schema"], "motel/v1")
        self.assertTrue(doc["ok"])
        room = doc["room"]
        for key in ("id", "task", "size_gb", "floor", "suite", "created", "expires", "max_expires", "owner", "state", "time_left_h", "path"):
            self.assertIn(key, room)
        code, out, _ = self.motel("status", room["id"], "--json")
        self.assertEqual(json.loads(out)["room"]["id"], room["id"])
        code, out, _ = self.motel("ls", "--json")
        listing = json.loads(out)
        self.assertEqual([r["id"] for r in listing["rooms"]], [room["id"]])
        self.assertIn("capacity", listing)
        self.assertIn("floors", listing)
        code, out, _ = self.motel("status", "--json")  # `status` without a room is `ls`
        self.assertIn("rooms", json.loads(out))

    def test_ls_human_output_and_all(self):
        self.motel("checkin", "10G", "--task", "a")
        code, out, _ = self.motel("ls")
        self.assertIn("a-", out)
        self.assertIn("floors 1/3", out)
        code, _, _ = self.motel("ls", "--all")
        self.assertEqual(self.motel.seen[-1], ["ls", "--all", "--json"])

    def test_extend_checkout_restore_round_trip(self):
        _, out, _ = self.motel("checkin", "10G", "--json")
        rid = json.loads(out)["room"]["id"]
        code, out, _ = self.motel("extend", rid, "--for", "3h", "--json")
        self.assertEqual(code, 0)
        code, out, _ = self.motel("checkout", rid, "--json")
        self.assertEqual(json.loads(out)["room"]["state"], "lost-and-found")
        code, out, _ = self.motel("restore", rid, "--json")
        self.assertEqual(json.loads(out)["room"]["state"], "active")
        code, out, _ = self.motel("release", rid, "--now", "--json")
        self.assertEqual(json.loads(out)["room"]["state"], "purged")
        self.assertEqual(self.motel.seen[-1], ["checkout", rid, "--now", "--json"])

    def test_arguments_are_validated_before_the_desk_is_ever_called(self):
        bad = [
            ("checkin",), ("checkin", "lots"), ("checkin", "0"), ("checkin", "10G", "--for", "tomorrow"), ("checkin", "10G", "--task", "a/b"),
            ("checkin", "10G", "--task", "$(id)"), ("extend", "x-abcd"), ("extend", "x-abcd", "--for", "6 hours"),
            ("extend", "/mnt/motel/f-1234/rooms/x-abcd", "--for", "1h"), ("checkout", "../../etc"), ("checkout", "123456789"),
            ("status", "x-ABCD"), ("restore", "x"), ("checkin", "10G", "--device", "/dev/sda"), ("checkin", "10G", "--volume", "123"),
            ("ls", "--mine", "--all"), ("frobnicate",), (),
        ]
        for argv in bad:
            with self.subTest(argv):
                code, _, err = self.motel(*argv)
                self.assertEqual(code, 2)
                self.assertTrue(err.startswith("motel: "), err)
                self.assertTrue(err.strip().endswith(("motel --help", "200G", "motel ls --mine", "200G --task video-render")), err)
        self.assertEqual(self.motel.seen, [])

    def test_unknown_options_cannot_smuggle_raw_ids(self):
        code, _, _ = self.motel("checkin", "10G", "--json", "--provider-volume=123")
        self.assertEqual(code, 2)

    def test_every_error_line_starts_with_motel_and_ends_with_a_command(self):
        cases = [("checkout", "nope-ab12"), ("extend", "nope-ab12", "--for", "1h"), ("checkin", "9999G")]
        for argv in cases:
            code, _, err = self.motel(*argv)
            self.assertNotEqual(code, 0)
            line = err.strip()
            self.assertTrue(line.startswith("motel: ") and ". Try: motel" in line, line)
            self.assertEqual(len(line.splitlines()), 1)

    def test_exit_codes(self):
        self.assertEqual(self.motel("checkout", "nope-ab12")[0], 7)
        self.assertEqual(self.motel("checkin", "9999G")[0], 3)
        _, out, _ = self.motel("checkin", "10G", "--json")
        rid = json.loads(out)["room"]["id"]
        self.fx.sysops.busy[json.loads(out)["room"]["path"]] = ["1 sh"]
        self.assertEqual(self.motel("checkout", rid)[0], 4)

    def test_json_errors_are_json_on_stdout(self):
        code, out, err = self.motel("checkout", "nope-ab12", "--json")
        doc = json.loads(out)
        self.assertEqual((code, doc["ok"], doc["code"]), (7, False, 7))
        self.assertIn("hint", doc)

    def test_warning_before_checkout_goes_to_stderr(self):
        _, out, _ = self.motel("checkin", "10G", "--for", "90m", "--json")
        rid = json.loads(out)["room"]["id"]
        code, out, err = self.motel("status", rid)
        self.assertIn("checks out in", err)
        self.assertIn(f"motel extend {rid} --for 6h", err)
        self.assertNotIn("checks out", out)

    def test_desk_missing_is_exit_8(self):
        def missing(argv):
            raise cli.NotSetUp("the motel front desk is not installed on this host", INSTALL_HINT)
        code, _, err = self.motel("ls", runner=missing)
        self.assertEqual(code, EXIT_NOT_SET_UP)
        self.assertTrue(err.strip().endswith(INSTALL_HINT))

    def test_sudo_refusal_is_explained(self):
        code, _, err = self.motel("ls", runner=lambda argv: (1, "", "sudo: a password is required\n"))
        self.assertEqual(code, 5)
        self.assertIn("allow_users", err)

    def test_garbage_from_the_desk_is_an_error(self):
        code, _, err = self.motel("ls", runner=lambda argv: (0, "not json", ""))
        self.assertEqual(code, 5)
        self.assertTrue(err.startswith("motel: "))

    def test_doctor_runs_through_the_desk(self):
        code, out, _ = self.motel("doctor", "--json")
        self.assertIn("checks", json.loads(out))


class DeskTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def test_desk_rejects_what_the_cli_would_never_send(self):
        for argv in (["checkin"], ["checkin", "--size", "10G", "--volume", "1"], ["checkout", "/etc/passwd"],
                     ["extend", "x-abcd"], ["frobnicate"], ["checkin", "--size", "10G", "--task", "../x"], ["setup", "nimbus"],
                     ["checkin", "--siz", "10G"]):
            with self.subTest(argv):
                code, _, err = self.fx.desk(*argv, "--json")
                self.assertEqual(code, 2)
        self.assertFalse(self.fx.provider.calls)

    def test_users_not_in_allow_users_are_refused(self):
        from motel.rooms import Caller
        stranger = Caller(self.fx.uid + 5, self.fx.uid + 5, "mallory")
        code, out, _ = self.fx.desk("ls", "--json", caller=stranger)
        self.assertEqual(code, 3)
        self.assertIn("allow_users", json.loads(out)["error"])
        member = Caller(self.fx.uid + 6, self.fx.uid + 6, "bob")
        self.assertEqual(self.fx.desk("ls", "--json", caller=member, groups=["agents"])[0], 0)
        self.assertEqual(self.fx.desk("ls", "--json", caller=member, groups=["other"])[0], 3)

    def test_operator_verbs_are_root_only(self):
        for argv in (["reap", "--apply"], ["setup", "local"]):
            code, out, _ = self.fx.desk(*argv, "--json")
            self.assertEqual(code, 3)

    def test_reap_report_shape_and_root_run(self):
        from motel.rooms import Caller
        code, out, _ = self.fx.desk("reap", "--json", caller=Caller(0, 0, "root"))
        doc = json.loads(out)
        for key in ("actions", "warnings", "refused", "orphans", "errors", "floors", "rooms", "ok", "apply"):
            self.assertIn(key, doc)
        self.assertEqual(code, 0)

    def test_reap_exits_5_when_housekeeping_has_errors(self):
        from motel.rooms import Caller
        self.fx.provider.incomplete = True
        code, out, _ = self.fx.desk("reap", "--apply", "--json", caller=Caller(0, 0, "root"))
        self.assertEqual(code, 5)

    def test_config_error_blocks_commands_with_a_clear_message(self):
        with open(self.fx.paths.config_file, "a") as handle:
            handle.write("\n[rooms]\ngrace = 5\n")
        code, out, err = self.fx.desk("ls")
        self.assertEqual(code, 5)
        self.assertIn("motel.toml", err)
        self.assertTrue(err.startswith("motel: "))

    def test_missing_receipt_is_exit_8(self):
        os.unlink(self.fx.paths.receipt_file)
        code, _, err = self.fx.desk("ls")
        self.assertEqual(code, 8)
        self.assertIn("setup", err)

    def test_doctor_reports_failures_with_exit_5_and_still_works_without_setup(self):
        os.unlink(self.fx.paths.receipt_file)
        code, out, _ = self.fx.desk("doctor", "--json")
        doc = json.loads(out)
        self.assertEqual(code, 5)
        self.assertFalse(doc["ok"])

    def test_doctor_on_a_good_install(self):
        code, out, _ = self.fx.desk("doctor")
        self.assertIn("host set up", out)
        self.assertIn("what else the token can see", out)

    def test_a_crash_inside_the_desk_is_a_json_error_not_a_traceback(self):
        self.fx.provider.whoami = lambda: 1 / 0
        code, out, err = self.fx.desk("checkin", "--size", "10G", "--json")
        self.assertEqual(code, 5)
        self.assertEqual(json.loads(out)["ok"], False)


if __name__ == "__main__":
    unittest.main()


class CabinRoutingTest(unittest.TestCase):
    def test_cabin_routes_to_the_cabins_module_with_the_remaining_arguments(self):
        import sys
        import types
        from unittest import mock
        seen = []
        fake = types.ModuleType("motel.cabins.cli")
        fake.main = lambda argv: seen.append(argv) or 0
        with mock.patch.dict(sys.modules, {"motel.cabins.cli": fake}):
            self.assertEqual(cli.main(["cabin", "ls", "--json"], out=io.StringIO(), err=io.StringIO()), 0)
        self.assertEqual(seen, [["ls", "--json"]])

    def test_cabin_exit_code_is_passed_through(self):
        import sys
        import types
        from unittest import mock
        fake = types.ModuleType("motel.cabins.cli")
        fake.main = lambda argv: 7
        with mock.patch.dict(sys.modules, {"motel.cabins.cli": fake}):
            self.assertEqual(cli.main(["cabin", "x"], out=io.StringIO(), err=io.StringIO()), 7)

    def test_without_the_cabins_module_it_is_exit_2(self):
        from unittest import mock
        err = io.StringIO()
        missing = ModuleNotFoundError("No module named 'motel.cabins'", name="motel.cabins")
        with mock.patch("importlib.import_module", side_effect=missing):
            code = cli.main(["cabin", "ls"], out=io.StringIO(), err=err)
        self.assertEqual(code, 2)
        self.assertTrue(err.getvalue().startswith("motel: cabins are not installed"))
        self.assertTrue(err.getvalue().strip().endswith("motel --help"))
        out = io.StringIO()
        with mock.patch("importlib.import_module", side_effect=missing):
            cli.main(["cabin", "ls", "--json"], out=out, err=io.StringIO())
        self.assertEqual(json.loads(out.getvalue())["code"], 2)

    def test_a_broken_cabins_module_is_not_reported_as_missing(self):
        from unittest import mock
        broken = ModuleNotFoundError("No module named 'something_else'", name="something_else")
        with mock.patch("importlib.import_module", side_effect=broken), self.assertRaises(ModuleNotFoundError):
            cli.main(["cabin", "ls"], out=io.StringIO(), err=io.StringIO())


class McpRoutingTest(unittest.TestCase):
    def test_mcp_routes_to_the_mcp_module_with_the_remaining_arguments(self):
        import sys
        import types
        from unittest import mock
        seen = []
        fake = types.ModuleType("motel.mcp_server")
        fake.main = lambda argv: seen.append(argv) or 0
        with mock.patch.dict(sys.modules, {"motel.mcp_server": fake}):
            self.assertEqual(cli.main(["mcp", "--stdio"], out=io.StringIO(), err=io.StringIO()), 0)
        self.assertEqual(seen, [["--stdio"]])

    def test_without_the_module_it_is_exit_2(self):
        from unittest import mock
        err = io.StringIO()
        missing = ModuleNotFoundError("No module named 'motel.mcp_server'", name="motel.mcp_server")
        with mock.patch("importlib.import_module", side_effect=missing):
            code = cli.main(["mcp"], out=io.StringIO(), err=err)
        self.assertEqual(code, 2)
        self.assertIn("MCP server not installed", err.getvalue())
        self.assertTrue(err.getvalue().strip().endswith("motel --help"))


class NotSetUpHintTest(unittest.TestCase):
    def test_installed_but_not_set_up_prints_the_one_stable_command(self):
        import os as _os
        fx = Fixture()
        self.addCleanup(fx.close)
        _os.unlink(fx.paths.receipt_file)
        code, out, err = Harness(fx)("ls")
        self.assertEqual(code, 8)
        self.assertTrue(err.strip().endswith("Try: sudo motel-desk setup hetzner|local"), err)

    def test_desk_binary_absent_prints_install_sh(self):
        def absent(argv):
            raise cli.NotSetUp("the motel front desk is not installed on this host", INSTALL_HINT)
        code, _, err = Harness(Fixture.__new__(Fixture))("ls", runner=absent)
        self.assertEqual(code, 8)
        self.assertTrue(err.strip().endswith("Try: " + INSTALL_HINT))

    def test_missing_token_uses_the_same_stable_command(self):
        from motel.desk import NotSetUp
        self.assertEqual(NotSetUp("no token").hint, "sudo motel-desk setup hetzner|local")


class JsonContractTest(unittest.TestCase):
    """stdout is exactly one JSON object for every verb with --json; warnings stay on stderr."""

    def test_one_object_on_stdout_for_every_verb(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        motel = Harness(fx)
        _, out, _ = motel("checkin", "10G", "--for", "90m", "--json")
        rid = json.loads(out)["room"]["id"]
        calls = [("status", rid), ("ls",), ("ls", "--all"), ("status",), ("extend", rid, "--for", "1h"), ("checkout", rid),
                 ("restore", rid), ("checkout", rid, "--now"), ("doctor",), ("checkout", "nope-ab12"), ("checkin", "oops")]
        for argv in calls:
            with self.subTest(argv):
                code, out, _ = motel(*argv, "--json")
                doc = json.loads(out)  # raises unless stdout is a single JSON document
                self.assertIsInstance(doc, dict)
                self.assertEqual(doc["schema"], "motel/v1")
                self.assertEqual(doc["ok"], code == 0)
                if argv[0] == "doctor":
                    self.assertIn("checks", doc)
                elif not doc["ok"]:
                    self.assertEqual(set(doc), {"schema", "ok", "error", "hint", "code"})  # pinned: do not add keys
                    self.assertEqual(doc["code"], code)
                    self.assertIs(doc["ok"], False)
                    self.assertIsInstance(doc["error"], str)
                    self.assertTrue(doc["hint"].startswith(("motel", "sudo")))


def test_a_purged_room_shows_deleted_not_time_left():
    from motel.render import room_line
    line = room_line({"id": "r-1", "state": "purged", "size_gb": 10, "time_left_h": 6.0, "owner": "alice", "path": None})
    assert "deleted" in line and "left" not in line
