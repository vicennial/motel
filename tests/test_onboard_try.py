"""`motel try`: the end-to-end transcript, exit codes, pacing, colour and cleanup."""

import contextlib
import io
import json
import os
import subprocess
import tempfile
import unittest
from datetime import UTC, datetime
from pathlib import Path
from unittest import mock

from motel import cli
from motel.onboard import demo
from motel.sysops import RealSysOps

START = datetime(2026, 1, 1, 12, 0, tzinfo=UTC)
REAL_DIRS = ("/mnt/motel", "/etc/motel", "/var/lib/motel")


class FakeTty(io.StringIO):
    def isatty(self):
        return True


def commands(transcript):
    return [c for step in transcript["steps"] for c in step["commands"]]


def find(transcript, prefix):
    return [c for c in commands(transcript) if c["command"].startswith(prefix)]


class Quiet:
    """Forbid anything that could touch the real host while the tour runs."""

    def __enter__(self):
        boom = mock.Mock(side_effect=AssertionError("motel try must not run external commands"))
        self.patches = [mock.patch.object(subprocess, "run", boom), mock.patch.object(subprocess, "Popen", boom),
                        mock.patch.object(os, "system", boom),
                        mock.patch.object(RealSysOps, "__init__", mock.Mock(side_effect=AssertionError("RealSysOps used")))]
        for patch in self.patches:
            patch.start()
        return self

    def __exit__(self, *exc):
        for patch in self.patches:
            patch.stop()


class TranscriptTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with Quiet():
            cls.result = demo.run_demo(out=io.StringIO(), quiet=True, start=START)
        cls.t = cls.result.transcript

    def test_step_titles_in_story_order(self):
        self.assertEqual([s["title"] for s in self.t["steps"]],
                         ["Check in", "Use it", "See the room", "Late checkout", "Nobody checks out", "Grace runs out",
                          "Change your mind", "No vacancy", "Check out"])

    def test_room_states_follow_the_lifecycle(self):
        states = [c["room"]["state"] for c in commands(self.t) if "room" in c]
        # checkin, extend, status (grace), restore, checkout --now
        self.assertEqual(states, ["active", "active", "grace", "active", "purged"])

    def test_checkin_prints_only_the_path_on_stdout_and_the_room_is_private(self):
        [checkin] = find(self.t, "D=$(motel checkin")
        self.assertEqual(checkin["exit_code"], 0)
        self.assertIn("Disposable", checkin["stderr"])
        [echo] = find(self.t, 'echo "$D"')
        self.assertRegex(echo["stdout"], r"^/mnt/motel/f-[0-9a-f]+/rooms/video-render-[a-z0-9]{4}$")
        self.assertNotIn(self.result.sandbox, json.dumps(self.t))

    def test_file_survives_lost_and_found_and_restore(self):
        listings = find(self.t, 'ls "$D"')
        self.assertEqual([(c["exit_code"], bool(c["stdout"])) for c in listings], [(0, True), (2, False), (0, True)])
        for listing in (listings[0], listings[2]):
            self.assertIn("notes.txt", listing["stdout"])
        self.assertIn("No such file", listings[1]["stderr"])

    def test_housekeeping_moves_the_room_aside_and_later_retires_the_floor(self):
        reaps = find(self.t, "sudo motel-desk reap")
        self.assertEqual(len(reaps), 2)
        self.assertIn("moved to lost & found", reaps[0]["stdout"])
        self.assertIn("unmount, detach, delete", reaps[1]["stdout"])
        lost = [c for c in find(self.t, "motel ls")][-1]
        self.assertIn("lost-and-found", lost["stdout"])

    def test_no_vacancy_is_exit_3_with_a_next_command(self):
        [refusal] = find(self.t, "motel checkin 90G")
        self.assertEqual(refusal["exit_code"], 3)
        self.assertIn("no vacancy", refusal["stderr"])
        self.assertTrue(refusal["stderr"].rstrip().endswith("Try: motel ls --mine"))

    def test_every_other_motel_command_exits_0(self):
        bad = [c["command"] for c in commands(self.t)
               if c["exit_code"] != 0 and not c["command"].startswith(("ls ", "motel checkin 90G"))]
        self.assertEqual(bad, [])

    def test_clock_is_simulated_and_moves_forward(self):
        self.assertEqual(self.t["started"], "2026-01-01T12:00:00Z")
        self.assertGreater(self.t["ended"], self.t["started"])
        self.assertTrue(self.t["simulated"])

    def test_ends_with_real_install_commands(self):
        text = [n["command"] for n in self.t["next"]]
        self.assertIn("sudo motel-desk setup hetzner", text)
        self.assertIn("motel doctor", text)
        self.assertTrue(any(c.startswith("motel checkin") for c in text))


class CleanupTest(unittest.TestCase):
    def test_sandbox_is_removed_and_nothing_real_is_touched(self):
        before = {d: os.path.exists(d) for d in REAL_DIRS}
        with Quiet():
            result = demo.run_demo(out=io.StringIO(), quiet=True, start=START)
        self.assertTrue(result.removed)
        self.assertFalse(os.path.exists(result.sandbox))
        self.assertEqual({d: os.path.exists(d) for d in REAL_DIRS}, before)
        self.assertEqual(result.transcript["cleanup"], {"sandbox_removed": True, "touched_real_mounts": False})

    def test_mounts_are_recorded_not_performed_and_stay_inside_the_sandbox(self):
        with Quiet():
            result = demo.run_demo(out=io.StringIO(), quiet=True, start=START)
        mounts = [c for c in result.sysops_calls if c[0] in ("mount", "umount")]
        self.assertTrue(mounts)
        for call in mounts:
            self.assertTrue(call[-1].startswith(result.sandbox + "/mnt/motel/"), call)
        self.assertEqual({c[0] for c in result.provider_calls} - {"list", "price", "get", "create", "attach", "detach", "delete", "resize"},
                         set())

    def test_sandbox_is_removed_after_an_error_mid_tour(self):
        seen = []

        def explode(_seconds):
            seen.append(sorted(Path(tempfile.gettempdir()).glob("motel-try-*")))
            raise RuntimeError("boom")

        before = set(Path(tempfile.gettempdir()).glob("motel-try-*"))
        with Quiet(), self.assertRaises(RuntimeError):
            demo.run_demo(out=io.StringIO(), fast=False, sleep=explode, start=START)
        self.assertTrue(seen and set(seen[0]) - before, "the sandbox existed while the tour ran")
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("motel-try-*")), before)

    def test_ctrl_c_exits_130_and_cleans_up(self):
        before = set(Path(tempfile.gettempdir()).glob("motel-try-*"))
        out, err = io.StringIO(), io.StringIO()
        with Quiet():
            code = demo.main([], out=out, err=err, sleep=mock.Mock(side_effect=KeyboardInterrupt))
        self.assertEqual(code, 130)
        self.assertEqual(set(Path(tempfile.gettempdir()).glob("motel-try-*")), before)


class CommandLineTest(unittest.TestCase):
    def run_try(self, *args, out=None, sleep=None):
        out, err = out or io.StringIO(), io.StringIO()
        with Quiet():
            code = demo.main(list(args), out=out, err=err, sleep=sleep or mock.Mock())
        return code, out.getvalue(), err.getvalue()

    def test_route_from_the_motel_cli_json(self):
        out, err = io.StringIO(), io.StringIO()
        with Quiet(), contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(["try", "--json"])
        self.assertEqual((code, err.getvalue()), (0, ""))
        doc = json.loads(out.getvalue())
        self.assertEqual((doc["schema"], doc["ok"]), ("motel/try/v1", True))
        self.assertEqual(len(doc["steps"]), 9)

    def test_json_has_no_narration_and_never_pauses(self):
        sleep = mock.Mock()
        code, out, _ = self.run_try("--json", sleep=sleep)
        self.assertEqual(code, 0)
        json.loads(out)
        sleep.assert_not_called()

    def test_fast_never_pauses_and_default_takes_about_30_seconds(self):
        sleep = mock.Mock()
        self.run_try("--fast", sleep=sleep)
        sleep.assert_not_called()
        sleep = mock.Mock()
        self.run_try(sleep=sleep)
        total = sum(c.args[0] for c in sleep.call_args_list)
        self.assertTrue(20 <= total <= 40, total)

    def test_plain_output_when_not_a_terminal(self):
        code, out, _ = self.run_try("--fast")
        self.assertEqual(code, 0)
        self.assertNotIn("\033", out)
        self.assertIn("What you just saw", out)
        self.assertIn("[exit 3]", out)

    def test_color_on_a_terminal_unless_disabled(self):
        env = {k: v for k, v in os.environ.items() if k not in ("NO_COLOR", "TERM")}
        with mock.patch.dict(os.environ, env, clear=True):
            _, colored, _ = self.run_try("--fast", out=FakeTty())
            _, flagged, _ = self.run_try("--fast", "--no-color", out=FakeTty())
        with mock.patch.dict(os.environ, {**env, "NO_COLOR": "1"}, clear=True):
            _, via_env, _ = self.run_try("--fast", out=FakeTty())
        self.assertIn("\033[", colored)
        self.assertNotIn("\033", flagged)
        self.assertNotIn("\033", via_env)

    def test_unknown_flag_is_a_usage_error(self):
        code, out, err = self.run_try("--bogus")
        self.assertEqual(code, 2)
        self.assertTrue(err.startswith("motel:") and err.rstrip().endswith("Try: motel try --help"))


if __name__ == "__main__":
    unittest.main()
