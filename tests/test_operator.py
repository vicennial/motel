"""Operator verbs under sudo, and the sudoers rule that keeps them away from allow_users."""

import fnmatch
import io
import json
import os
import shlex
import unittest
from pathlib import Path
from unittest import mock

from helpers import Fixture
from motel import desk, installer
from motel.rooms import Caller


class RealCallerTest(unittest.TestCase):
    def test_under_sudo_the_caller_is_the_invoking_user_and_the_process_is_an_operator(self):
        with mock.patch("os.geteuid", return_value=0), mock.patch.dict(os.environ, {"SUDO_UID": str(os.getuid())}):
            caller = desk.real_caller()
        self.assertEqual(caller.uid, os.getuid())
        self.assertTrue(caller.operator)

    def test_a_root_login_without_sudo_uid_is_uid_0(self):
        env = {k: v for k, v in os.environ.items() if k != "SUDO_UID"}
        with mock.patch("os.geteuid", return_value=0), mock.patch.dict(os.environ, env, clear=True):
            caller = desk.real_caller()
        self.assertEqual((caller.uid, caller.name), (0, "root"))

    def test_an_unprivileged_process_is_refused(self):
        with mock.patch("os.geteuid", return_value=1000), self.assertRaises(desk.Guard):
            desk.real_caller()

    def test_garbage_in_sudo_uid_is_not_trusted(self):
        with mock.patch("os.geteuid", return_value=0), mock.patch.dict(os.environ, {"SUDO_UID": "12; rm -rf"}):
            self.assertEqual(desk.real_caller().uid, 0)


class OperatorVerbsTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        # an admin who ran `sudo motel-desk ...`: euid 0, SUDO_UID = the admin, who is NOT in allow_users
        self.admin = Caller(self.fx.uid + 77, self.fx.uid + 77, "admin", operator=True)

    def test_reap_and_setup_work_for_an_operator_even_if_not_in_allow_users(self):
        code, out, _ = self.fx.desk("reap", "--apply", "--json", caller=self.admin)
        self.assertEqual((code, json.loads(out)["ok"]), (0, True))
        local = Fixture(receipt=False)
        self.addCleanup(local.close)
        from motel.providers.local import LocalProvider
        cfg_file = Path(local.paths.config_file)
        cfg_file.write_text(cfg_file.read_text().replace('provider = "hetzner"', 'provider = "local"'))
        out = io.StringIO()
        code = desk.run(["setup", "local", "--json"], paths=local.paths, caller=self.admin, sysops=local.sysops, clock=local.clock,
                        out=out, err=io.StringIO(), local_factory=lambda: LocalProvider(str(local.root / "l"), runner=lambda *a, **k: ""))
        self.assertEqual(code, 0, out.getvalue())

    def test_reap_before_setup_is_a_quiet_no_op_but_not_once_rooms_exist(self):
        # the housekeeping timer runs between `install` and `setup`; it must not leave a failed unit behind
        fresh = Fixture(receipt=False)
        self.addCleanup(fresh.close)
        code, out, _ = fresh.desk("reap", "--apply", "--json", caller=self.admin)
        doc = json.loads(out)
        self.assertEqual((code, doc["ok"], doc["set_up"], doc["actions"]), (0, True, False, []))
        code, out, _ = fresh.desk("reap", "--apply", caller=self.admin)
        self.assertEqual(code, 0)
        self.assertIn("nothing to reap", out)
        self.assertIsNone(doc["host"])
        self.assertTrue(doc["run_at"].endswith("Z"))
        # a plain user is still refused before the no-op
        code, out, _ = fresh.desk("reap", "--apply", "--json", caller=Caller(self.admin.uid, self.admin.gid, "admin"))
        self.assertNotEqual(code, 0)
        # a room record without a receipt is a broken host, not a fresh one: the reap still fails loudly
        os.makedirs(os.path.join(fresh.paths.state_dir, "rooms"), exist_ok=True)
        Path(fresh.paths.state_dir, "rooms", "r-1.json").write_text("{}")
        code, out, _ = fresh.desk("reap", "--apply", "--json", caller=self.admin)
        self.assertEqual((code, json.loads(out)["code"]), (8, 8))

    def test_a_plain_user_is_still_refused(self):
        plain = Caller(self.admin.uid, self.admin.gid, "admin")  # same uid, but not a verified euid-0 desk
        for argv in (["reap", "--apply", "--json"], ["setup", "local", "--json"]):
            self.assertEqual(self.fx.desk(*argv, caller=plain)[0], 3)

    def test_the_admin_is_not_a_user_of_rooms(self):
        self.assertEqual(self.fx.desk("ls", "--json", caller=self.admin)[0], 3)


class SudoersRuleTest(unittest.TestCase):
    """Evaluates the rendered rule the way sudo does (argument glob after the command path)."""

    DESK = "/usr/local/sbin/motel-desk"

    def allowed_commands(self):
        text = installer.render_sudoers(["alice", "@agents"]).split("NOPASSWD:")[1].replace("\\\n", " ")
        return [c.strip() for c in text.split(",") if c.strip()]

    def sudo_allows(self, args: str) -> bool:
        line = f"{self.DESK} {args}".rstrip()
        return any(fnmatch.fnmatchcase(line, spec) or line == spec for spec in self.allowed_commands())

    def test_user_verbs_are_allowed(self):
        for args in ("checkin --size 10G --json", "ls --json", "ls", "status x-abcd --json", "extend x-abcd --for 60m --json",
                     "checkout x-abcd --now --json", "restore x-abcd --json", "doctor --json"):
            self.assertTrue(self.sudo_allows(args), args)

    def test_operator_verbs_never_match_however_they_are_dressed_up(self):
        tricks = ["setup", "setup local", "setup hetzner --token-file /etc/shadow", "reap", "reap --apply --json",
                  "--json setup local", "--json reap", " setup", "-h", "", "--help", "SETUP local", "./setup", "../sbin/motel-desk setup",
                  "x setup", "none --json reap", "checkin2 reap", "ls2 reap", "lsx", "status2"]
        for args in tricks:
            self.assertFalse(self.sudo_allows(args), repr(args))

    def test_glob_matches_that_sudo_allows_never_reach_an_operator_verb_in_the_desk(self):
        """Anything after an allowed first word is passed on; the desk's own parser must not turn it into setup/reap."""
        parser = desk.build_parser()
        smuggled = ["ls setup local", "ls reap --apply", "checkin --size 1G reap", "status setup", "extend x-abcd reap",
                    "doctor setup hetzner", "checkout reap --apply", "ls --all setup", "restore x-abcd --for 60m reap",
                    "ls -- setup", "checkin --size 1G --task setup", "checkin --size 1G -- reap"]
        for args in smuggled:
            argv = shlex.split(args)
            if not self.sudo_allows(args):
                continue
            try:
                parsed = parser.parse_args(argv)
            except desk.Usage:
                continue
            self.assertIn(parsed.verb, desk.USER_VERBS, args)

    def test_the_rule_never_names_an_operator_verb(self):
        text = installer.render_sudoers(["alice"])
        for verb in desk.OPERATOR_VERBS:
            self.assertNotIn(f"motel-desk {verb}", text)

    def test_sudoers_pins_the_first_argument_for_every_entry(self):
        for spec in self.allowed_commands():
            rest = spec[len(self.DESK):].strip()
            self.assertIn(rest.split()[0], desk.USER_VERBS, spec)


class DoctorSudoersTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)
        self.path = self.fx.paths.real("/etc/sudoers.d/motel")
        os.makedirs(os.path.dirname(self.path))

    def check(self):
        code, out, _ = self.fx.desk("doctor", "--json")
        return {c["check"]: c for c in json.loads(out)["checks"]}["sudoers rule matches allow_users"]

    def test_matching_rule_is_ok(self):
        Path(self.path).write_text(installer.render_sudoers(["alice", "@agents"]))
        self.assertEqual(self.check()["level"], "ok")

    def test_a_drifted_or_hand_edited_rule_fails(self):
        Path(self.path).write_text(installer.render_sudoers(["alice", "@agents"]) + "alice ALL=(ALL) NOPASSWD: ALL\n")
        self.assertEqual(self.check()["level"], "fail")
        Path(self.path).write_text(installer.render_sudoers(["alice"]))
        self.assertEqual(self.check()["level"], "fail")

    def test_missing_rule_is_a_warning(self):
        self.assertEqual(self.check()["level"], "warn")


if __name__ == "__main__":
    unittest.main()
