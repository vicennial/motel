"""`motel --help`, `motel cabin --help` and the errors of a host that has no front desk, through the real entry points."""
import io
import json
import os
import re
import subprocess
import sys
import unittest
from pathlib import Path
from unittest import mock

from motel import cli, errors
from motel.tours import cli as tour_cli

SRC = str(Path(__file__).resolve().parent.parent / "src")


def motel(*argv, env=None):
    """The real command in a subprocess (python -m motel.cli), with the in-tree package first on the path."""
    env = {**os.environ, "PYTHONPATH": SRC, "NO_COLOR": "1", **(env or {})}
    proc = subprocess.run([sys.executable, "-m", "motel.cli", *argv], capture_output=True, text=True, env=env, stdin=subprocess.DEVNULL)
    return proc.returncode, proc.stdout, proc.stderr


class TopLevelHelpTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.code, cls.out, cls.err = motel("--help")

    def test_it_exits_0_and_prints_to_stdout(self):
        self.assertEqual((self.code, self.err), (0, ""))

    def test_every_top_level_command_is_listed_with_a_description(self):
        names = {m.group(1): m.group(3).strip() for m in re.finditer(r"^  (\w+)( \(\w+\))?\s{2,}(\S.*)$", self.out, re.M)}
        expected = set(cli.ROUTES) | set(cli.ROOM_VERBS) | {"doctor"}
        self.assertEqual(expected, expected & set(names), self.out)
        self.assertTrue(all(len(text) > 12 for name, text in names.items() if name in expected), names)

    def test_the_commands_are_grouped_rooms_cabins_tours_setup_in_that_order(self):
        headings = [self.out.index(h) for h in ("\nrooms", "\ncabins", "\ntours", "\nsetup")]
        self.assertEqual(headings, sorted(headings))
        rooms, cabins, tours, setup = (self.out[a:b] for a, b in zip(headings, [*headings[1:], len(self.out)], strict=True))
        for verb in ("checkin", "ls", "extend", "checkout", "restore"):
            self.assertRegex(rooms, rf"(?m)^  {verb}\b", verb)
        self.assertRegex(cabins, r"(?m)^  cabin\b")
        self.assertRegex(tours, r"(?m)^  tour\b")
        for word in ("try", "install", "doctor", "mcp", "skill"):
            self.assertRegex(setup, rf"(?m)^  {word}\b", word)

    def test_the_lines_fit_a_terminal(self):
        self.assertLessEqual(max(len(line) for line in self.out.splitlines()), 100)

    def test_a_new_routed_command_cannot_be_left_out_of_the_help(self):
        listed = {name for _title, items in cli.HELP_GROUPS for name, _text in items}
        self.assertTrue(set(cli.ROUTES) <= listed, set(cli.ROUTES) - listed)


class CabinAndTourHelpTest(unittest.TestCase):
    def test_cabin_help_is_written_text_with_examples_and_the_receipt(self):
        code, out, _err = motel("cabin", "--help")
        self.assertEqual(code, 0)
        self.assertNotIn("``main(argv)``", out)
        self.assertNotIn("calls the root-owned front desk", out)
        for needle in ("EXPERIMENTAL", "deletion is final", "started hour", "receipt", "examples:", "motel cabin run -- make test",
                       "motel try --cabin"):
            self.assertIn(needle, out)

    def test_cabin_with_no_verb_prints_the_help_and_exits_2(self):
        code, out, _err = motel("cabin")
        self.assertEqual(code, 2)
        self.assertIn("examples:", out)

    def test_tour_help_names_the_receipt(self):
        code, out, _err = motel("tour", "--help")
        self.assertEqual(code, 0)
        self.assertIn("receipt", out)
        self.assertIn("motel tour wait TOUR", out)


class NoFrontDeskTest(unittest.TestCase):
    """A host that has not installed anything is told how to install, or to try the demo: never to run the failing command."""

    def assertInstallAdvice(self, line):
        self.assertIn("front desk is not installed", line)
        self.assertIn("Try: sudo uvx --from motel motel install --users $USER", line)
        self.assertIn("motel try", line)
        self.assertNotIn("Try: motel doctor", line)
        self.assertNotIn("sudo refused", line)

    def test_motel_doctor_without_a_desk_exits_8_with_the_install_command_and_the_demo(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "DESK", "/nonexistent/motel-desk"):
            code = cli.main(["doctor"], out=out, err=err)          # the real default runner: it looks for the desk first
        self.assertEqual((code, out.getvalue()), (8, ""))
        self.assertInstallAdvice(err.getvalue())

    def test_the_json_error_carries_the_same_hint(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(cli, "DESK", "/nonexistent/motel-desk"):
            code = cli.main(["doctor", "--json"], out=out, err=err)
        doc = json.loads(out.getvalue())
        self.assertEqual((code, doc["code"], doc["ok"]), (8, 8, False))
        self.assertIn("motel try", doc["hint"])
        self.assertTrue(doc["hint"].startswith("sudo uvx --from motel motel install --users $USER"))

    def test_the_other_verbs_say_the_same(self):
        for verb in (["ls"], ["checkin", "10G"], ["extend", "job-ab12", "--for", "2h"], ["checkout", "job-ab12"], ["restore", "job-ab12"]):
            out, err = io.StringIO(), io.StringIO()
            with mock.patch.object(cli, "DESK", "/nonexistent/motel-desk"):
                code = cli.main(verb, out=out, err=err)
            self.assertEqual(code, 8, verb)
            self.assertInstallAdvice(err.getvalue())

    def test_tours_without_a_desk_say_the_same(self):
        out, err = io.StringIO(), io.StringIO()
        with mock.patch.object(tour_cli, "DESK", "/nonexistent/motel-desk"):
            code = tour_cli.main(["ls"], out=out, err=err)
        self.assertEqual(code, 8)
        self.assertInstallAdvice(err.getvalue())


class SudoRefusalTest(unittest.TestCase):
    """The desk is installed but sudo will not run it for this user."""

    def test_a_plain_refusal_names_allow_users_and_what_to_ask_the_admin_for(self):
        out, err = io.StringIO(), io.StringIO()
        code = cli.main(["ls"], runner=lambda argv: (1, "", "sudo: a password is required\n"), out=out, err=err)
        line = err.getvalue()
        self.assertEqual(code, 5)
        self.assertIn("not in allow_users", line)
        self.assertIn("Try: ask the host admin to add you to allow_users in /etc/motel/motel.toml", line)
        self.assertIn("sudo uvx --from motel motel install --upgrade", line)
        self.assertNotIn("Try: motel doctor", line)

    def test_a_container_with_no_new_privileges_is_named(self):
        err = "sudo: The \"no new privileges\" flag is set, which prevents sudo from running as root.\n"
        out, errs = io.StringIO(), io.StringIO()
        code = cli.main(["ls"], runner=lambda argv: (1, "", err), out=out, err=errs)
        self.assertEqual(code, 5)
        self.assertIn("no-new-privileges", errs.getvalue())
        self.assertNotIn("allow_users", errs.getvalue())

    def test_tours_explain_a_refusal_the_same_way(self):
        out, err = io.StringIO(), io.StringIO()
        code = tour_cli.main(["ls"], runner=lambda args, stdin=None: (1, b"", b"sudo: a password is required\n"), out=out, err=err)
        self.assertEqual(code, 5)
        self.assertIn("allow_users in /etc/motel/motel.toml", err.getvalue())
        self.assertNotIn("Try: motel doctor", err.getvalue())

    def test_the_helpers_are_the_single_source(self):
        self.assertEqual(errors.not_installed().code, errors.EXIT_NOT_SET_UP)
        self.assertEqual(errors.desk_refusal("").code, errors.EXIT_ERROR)


if __name__ == "__main__":
    unittest.main()
