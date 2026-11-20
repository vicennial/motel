"""SDK tests: run ``motel.sdk`` against a fake ``motel`` executable that prints canned JSON and exit codes.

No sudo, no network, no real motel install. Rules are matched on the leading arguments (``--json`` is ignored).
"""

import asyncio
import json
import os
import stat
import sys
import tempfile
import unittest
import warnings
from decimal import Decimal
from pathlib import Path

from motel import Motel
from motel.sdk import (
    AsyncMotel,
    BadReply,
    Busy,
    GuardRefused,
    InventoryIncomplete,
    MotelError,
    NotFound,
    NotInstalled,
    NotSetUp,
    NoVacancy,
    ProviderError,
    Timeout,
    UsageError,
)

FAKE = """#!{python}
import json, os, sys, time
args = [a for a in sys.argv[1:] if a != "--json"]
with open(os.environ["FAKE_MOTEL_DIR"] + "/log.jsonl", "a") as f:
    f.write(json.dumps({{"args": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
with open(os.environ["FAKE_MOTEL_DIR"] + "/rules.json") as f:
    rules = json.load(f)
for rule in rules:
    if args[:len(rule["match"])] == rule["match"]:
        time.sleep(rule.get("sleep", 0))
        sys.stdout.write(rule.get("stdout", ""))
        sys.stderr.write(rule.get("stderr", ""))
        sys.exit(rule.get("exit", 0))
sys.stderr.write("motel: fake has no rule for %s. Try: motel --help\\n" % args)
sys.exit(2)
"""

ROOM = {
    "id": "video-render-k3f9", "task": "video-render", "owner": "alice", "uid": 1000, "size_gb": 50, "suite": False,
    "floor": "f-3a1c", "state": "active", "created": "2026-03-01T12:40:00Z", "expires": "2026-03-01T18:40:00Z",
    "max_expires": "2026-03-15T12:40:00Z", "time_left_h": 6.0, "warn": False,
    "path": "/mnt/motel/f-3a1c/rooms/video-render-k3f9", "floor_free_gb": 310.5, "floor_used_pct": 38.0, "future_key": 1,
}
CABIN = {
    "schema": "motel/v1", "lease_id": "swift-gull-0123456789ab", "name": "motel-swift-gull-0123456789ab", "state": "active",
    "task": "swift-gull", "owner": "alice", "created_at": "2026-03-01T12:40:00Z", "expires_at": "2026-03-01T14:40:00Z",
    "offer": {"server_type": "cpx32", "location": "fsn1", "total_hourly_net_eur": "0.0200"},
    "cost_estimate_net_eur": "0.0100", "address": "192.0.2.10", "time_left_s": 7000, "warnings": ["be careful"],
}


def ok(**doc):
    return {"stdout": json.dumps({"schema": "motel/v1", "ok": True, **doc})}


def fail(code, error, hint="motel ls --mine"):
    return {"exit": code, "stdout": json.dumps({"schema": "motel/v1", "ok": False, "error": error, "hint": hint, "code": code})}


class SdkTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)
        self.binary = self.dir / "motel"
        self.binary.write_text(FAKE.format(python=sys.executable))
        self.binary.chmod(self.binary.stat().st_mode | stat.S_IXUSR)
        self.rules = []
        self.motel = Motel(str(self.binary), timeout=10, env={**os.environ, "FAKE_MOTEL_DIR": str(self.dir)})
        self.save()

    def save(self):
        (self.dir / "rules.json").write_text(json.dumps(self.rules))

    def rule(self, *match, **response):
        self.rules.append({"match": list(match), **response})
        self.save()

    def calls(self):
        path = self.dir / "log.jsonl"
        return [json.loads(line)["args"] for line in path.read_text().splitlines()] if path.exists() else []


class RoomTest(SdkTest):
    def test_checkin_builds_argv_and_parses_the_room(self):
        self.rule("checkin", **ok(room=ROOM))
        room = self.motel.checkin(50, "3h", task="video-render", suite=True)
        self.assertEqual(self.calls(), [["checkin", "50G", "--for", "3h", "--task", "video-render", "--suite", "--json"]])
        self.assertEqual((room.id, room.path, room.size_gb, room.time_left_h), ("video-render-k3f9", ROOM["path"], 50, 6.0))
        self.assertEqual(room.raw["future_key"], 1)  # unknown keys stay reachable

    def test_size_forms(self):
        self.rule("checkin", **ok(room=ROOM))
        self.motel.checkin("1T")
        self.motel.checkin("200G")
        self.assertEqual([c[1] for c in self.calls()], ["1T", "200G"])

    def test_context_manager_checks_out_on_exit(self):
        self.rule("checkin", **ok(room=ROOM))
        self.rule("checkout", **ok(room={**ROOM, "state": "lost-and-found"}))
        with self.motel.room(50, task="video-render") as room:
            self.assertEqual(room.state, "active")
        self.assertEqual(self.calls()[-1], ["checkout", "video-render-k3f9", "--json"])
        self.assertEqual(room.state, "lost-and-found")  # updated in place by the checkout

    def test_context_manager_now_and_keep(self):
        self.rule("checkin", **ok(room=ROOM))
        self.rule("checkout", **ok(room=ROOM))
        with self.motel.room(50, now=True):
            pass
        self.assertEqual(self.calls()[-1], ["checkout", "video-render-k3f9", "--now", "--json"])
        before = len(self.calls())
        with self.motel.room(50, keep=True):
            pass
        self.assertEqual(len(self.calls()), before + 1)  # checkin only

    def test_checkout_runs_when_the_block_raises_and_failure_does_not_mask_it(self):
        self.rule("checkin", **ok(room=ROOM))
        self.rule("checkout", **fail(4, "files are open in video-render-k3f9", "cd out of the room"))
        with warnings.catch_warnings(record=True) as caught:
            warnings.simplefilter("always")
            with self.assertRaises(ZeroDivisionError), self.motel.room(50):
                1 / 0  # noqa: B018
        self.assertEqual(self.calls()[-1][0], "checkout")
        self.assertTrue(any("could not check out" in str(w.message) for w in caught))

    def test_checkout_failure_after_a_clean_block_raises(self):
        self.rule("checkin", **ok(room=ROOM))
        self.rule("checkout", **fail(4, "files are open in video-render-k3f9", "cd out of the room"))
        with self.assertRaises(Busy), self.motel.room(50):
            pass

    def test_no_checkout_when_checkin_fails(self):
        self.rule("checkin", **fail(3, "no vacancy: the monthly cap is reached", "motel ls --mine"))
        with self.assertRaises(NoVacancy), self.motel.room(50):
            self.fail("body must not run")
        self.assertEqual([c[0] for c in self.calls()], ["checkin"])

    def test_overview_and_rooms(self):
        self.rule("ls", **ok(rooms=[ROOM], floors=[{"id": "f-3a1c"}], capacity={"floors": 1, "monthly_cap_eur": 30.0}))
        view = self.motel.overview("all")
        self.assertEqual(self.calls()[-1], ["ls", "--all", "--json"])
        self.assertEqual((len(view.rooms), view.floors[0]["id"], view.capacity["monthly_cap_eur"]), (1, "f-3a1c", 30.0))
        self.assertEqual(self.motel.rooms()[0].id, "video-render-k3f9")

    def test_extend_status_restore_and_room_helpers(self):
        self.rule("extend", **ok(room={**ROOM, "expires": "2026-03-02T00:00:00Z"}))
        self.rule("status", **ok(room=ROOM))
        self.rule("restore", **ok(room=ROOM))
        self.assertEqual(self.motel.extend("video-render-k3f9", "2h").expires, "2026-03-02T00:00:00Z")
        self.assertEqual(self.calls()[-1], ["extend", "video-render-k3f9", "--for", "2h", "--json"])
        room = self.motel.status("video-render-k3f9")
        room.extend("1h")
        self.assertEqual(room.expires, "2026-03-02T00:00:00Z")
        self.motel.restore("video-render-k3f9", "6h")
        self.assertEqual(self.calls()[-1], ["restore", "video-render-k3f9", "--for", "6h", "--json"])

    def test_doctor_failure_is_data(self):
        checks = [{"check": "host set up", "level": "ok", "ok": True, "detail": "provider local"},
                  {"check": "token", "level": "fail", "ok": False, "detail": "missing"}]
        self.rule("doctor", exit=5, stdout=json.dumps({"schema": "motel/v1", "ok": False, "checks": checks}))
        report = self.motel.doctor()
        self.assertFalse(report.ok)
        self.assertEqual([c.check for c in report.failures], ["token"])

    def test_doctor_ok(self):
        self.rule("doctor", **ok(checks=[{"check": "a", "level": "warn", "ok": True, "detail": ""}]))
        self.assertTrue(self.motel.doctor().ok)


class ErrorTest(SdkTest):
    def test_exit_codes_map_to_typed_exceptions(self):
        cases = {2: UsageError, 3: NoVacancy, 4: Busy, 5: ProviderError, 6: GuardRefused, 7: NotFound, 8: NotSetUp,
                 9: InventoryIncomplete}
        for code, cls in cases.items():
            with self.subTest(code=code):
                self.rules = [{"match": ["status"], **fail(code, f"boom {code}", f"hint {code}")}]
                self.save()
                with self.assertRaises(cls) as ctx:
                    self.motel.status("some-room")
                exc = ctx.exception
                self.assertEqual((exc.code, exc.hint, exc.message), (code, f"hint {code}", f"boom {code}"))
                self.assertIsInstance(exc, MotelError)
                self.assertEqual(str(exc), f"boom {code}. Try: hint {code}")
                self.assertEqual(exc.payload["code"], code)

    def test_unknown_exit_code_is_a_plain_motel_error(self):
        self.rule("status", **fail(42, "odd"))
        with self.assertRaises(MotelError) as ctx:
            self.motel.status("some-room")
        self.assertEqual((type(ctx.exception), ctx.exception.code), (MotelError, 42))

    def test_error_without_json_falls_back_to_stderr(self):
        self.rule("status", exit=7, stderr="motel: no room nope-ab12. Try: motel ls --mine\n")
        with self.assertRaises(NotFound) as ctx:
            self.motel.status("nope-ab12")
        self.assertEqual((ctx.exception.message, ctx.exception.hint), ("no room nope-ab12", "motel ls --mine"))

    def test_garbled_success_output(self):
        self.rule("status", stdout="not json")
        with self.assertRaises(BadReply):
            self.motel.status("some-room")

    def test_missing_binary(self):
        with self.assertRaises(NotInstalled) as ctx:
            Motel(str(self.dir / "absent")).doctor()
        self.assertEqual(ctx.exception.code, 8)
        self.assertIsInstance(ctx.exception, NotSetUp)

    def test_timeout(self):
        self.rule("status", sleep=5, **ok(room=ROOM))
        slow = Motel(str(self.binary), timeout=0.3, env=self.motel.env)
        with self.assertRaises(Timeout) as ctx:
            slow.status("some-room")
        self.assertEqual(ctx.exception.code, 5)

    def test_client_side_validation_never_reaches_the_cli(self):
        bad = [lambda m: m.checkin("lots"), lambda m: m.checkin(50, "tomorrow"), lambda m: m.checkin(50, task="Bad Task"),
               lambda m: m.status("--now"), lambda m: m.extend("x", "2h"), lambda m: m.extend("some-room", "-1h"),
               lambda m: m.rooms("everyone"), lambda m: m.cabin_run([]), lambda m: m.cabin_run(["a", ""]),
               lambda m: m.cabin_checkin(server_type="--keep")]
        for call in bad:
            with self.assertRaises(UsageError):
                call(self.motel)
        self.assertEqual(self.calls(), [])


class CabinTest(SdkTest):
    def test_checkin_prints_the_bare_cabin_object(self):
        self.rule("cabin", "checkin", stdout=json.dumps(CABIN))
        cabin = self.motel.cabin_checkin("cpx32", "2h", task="swift-gull", disk_gb=100)
        self.assertEqual(self.calls(), [["cabin", "checkin", "--type", "cpx32", "--for", "2h", "--task", "swift-gull", "--disk", "100G", "--json"]])
        self.assertEqual((cabin.id, cabin.server_type, cabin.hourly_eur, cabin.address), ("swift-gull-0123456789ab", "cpx32", Decimal("0.0200"), "192.0.2.10"))
        self.assertEqual(cabin.warnings, ["be careful"])

    def test_checkin_with_a_baked_image_passes_it_and_reads_its_name_back(self):
        baked = {**CABIN, "offer": {**CABIN["offer"], "image_name": "omnigent", "image_type": "snapshot"}}
        self.rule("cabin", "checkin", stdout=json.dumps(baked))
        cabin = self.motel.cabin_checkin(image="omnigent")
        self.assertEqual(self.calls(), [["cabin", "checkin", "--image", "omnigent", "--json"]])
        self.assertEqual(cabin.image, "omnigent")

    def test_a_stock_cabin_has_no_image_name(self):
        self.rule("cabin", "checkin", stdout=json.dumps(CABIN))
        self.assertIsNone(self.motel.cabin_checkin().image)

    def test_a_bad_image_never_reaches_the_cli(self):
        for bad in ("Omni gent", "--keep", "ubuntu-24.04", "", "a" * 41):
            with self.assertRaises(UsageError):
                self.motel.cabin_checkin(image=bad)
        self.assertEqual(self.calls(), [])

    def test_cabin_images_lists_what_the_host_offers(self):
        rows = [{"id": 4711, "name": "omnigent", "created": "2026-11-08T12:30:00Z", "image_size_gb": 1.5, "disk_size_gb": 40}]
        self.rule("cabin", "images", **ok(images=rows))
        self.assertEqual(self.motel.cabin_images(), rows)
        self.assertEqual(self.calls(), [["cabin", "images", "--json"]])

    def test_context_manager_deletes_the_cabin(self):
        self.rule("cabin", "checkin", stdout=json.dumps(CABIN))
        self.rule("cabin", "checkout", **ok(cabin={**CABIN, "state": "released"}))
        with self.motel.cabin(ttl="1h") as cabin:
            self.assertFalse(cabin.released)
        self.assertEqual(self.calls()[-1], ["cabin", "checkout", "swift-gull-0123456789ab", "--json"])
        self.assertTrue(cabin.released)

    def test_cleanup_pending_checkout_raises_exit_9(self):
        self.rule("cabin", "checkin", stdout=json.dumps(CABIN))
        self.rule("cabin", "checkout", exit=9, **ok(cabin={**CABIN, "state": "cleanup_pending"}))
        with self.assertRaises(InventoryIncomplete), self.motel.cabin():
            pass
        self.assertFalse(self.motel.cabin_checkout("swift-gull-0123456789ab", strict=False).released)

    def test_keep(self):
        self.rule("cabin", "checkin", stdout=json.dumps(CABIN))
        with self.motel.cabin(keep=True):
            pass
        self.assertEqual(len(self.calls()), 1)

    def test_cabins_listing_tolerates_exit_9_unless_strict(self):
        self.rule("cabin", "ls", exit=9, stdout=json.dumps({"schema": "motel/v1", "provider": {"inventory": "unavailable"}, "cabins": [CABIN]}))
        self.assertEqual(self.motel.cabins()[0].id, "swift-gull-0123456789ab")
        with self.assertRaises(InventoryIncomplete):
            self.motel.cabins(strict=True)

    def test_status_and_extend_unwrap_the_cabin(self):
        self.rule("cabin", "status", **ok(cabin=CABIN, provider={}))
        self.rule("cabin", "extend", **ok(cabin={**CABIN, "time_left_s": 100}))
        self.assertEqual(self.motel.cabin_status("swift-gull-0123456789ab").time_left_s, 7000)
        self.assertEqual(self.motel.cabin_extend("swift-gull-0123456789ab", "1h").time_left_s, 100)

    def test_cabin_run_reads_the_json_summary_line(self):
        summary = {"schema": "motel/v1", "ok": True, "cabin": "swift-gull-0123456789ab", "exit_code": 0, "kept": False,
                   "released": True, "hourly_net_eur": "0.0200", "cost_estimate_net_eur": "0.0100"}
        self.rule("cabin", "run", exit=0, stdout="tests passed\n\n" + json.dumps(summary) + "\n",
                  stderr="motel: cabin other-id-0123456789ab · EUR 9/h net · ready in 40s\n")
        work = self.dir / "checkout"
        work.mkdir()
        result = self.motel.cabin_run(["make", "test"], sync=str(work), server_type="cpx32", ttl="30m")
        call = json.loads((self.dir / "log.jsonl").read_text().splitlines()[-1])
        self.assertEqual(call["args"], ["cabin", "run", "--json", "--type", "cpx32", "--for", "30m", "--", "make", "test"])
        self.assertEqual(Path(call["cwd"]).resolve(), work.resolve())
        self.assertEqual((result.returncode, result.stdout, result.cabin_id, result.cost_estimate_eur),
                         (0, "tests passed\n", "swift-gull-0123456789ab", Decimal("0.0100")))
        self.assertEqual((result.hourly_eur, result.released, result.kept), (Decimal("0.0200"), True, False))

    def test_cabin_run_without_a_summary_falls_back_to_stderr(self):
        stderr = ("motel: cabin swift-gull-0123456789ab · EUR 0.0200/h net · ready in 40s\n"
                  "motel: released swift-gull-0123456789ab · est. cost EUR 0.0100 net\n")
        self.rule("cabin", "run", exit=0, stdout="tests passed\n", stderr=stderr)
        result = self.motel.cabin_run(["make", "test"])
        self.assertEqual((result.stdout, result.cabin_id, result.cost_estimate_eur, result.released),
                         ("tests passed\n", "swift-gull-0123456789ab", Decimal("0.0100"), True))  # read from the "released" message

    def test_a_command_that_prints_json_is_not_mistaken_for_the_summary(self):
        self.rule("cabin", "run", exit=0, stdout='{"schema": "motel/v1", "ok": true}\n')
        result = self.motel.cabin_run(["make"])
        self.assertEqual(result.stdout, '{"schema": "motel/v1", "ok": true}\n')
        self.assertIsNone(result.released)

    def only(self, *match, **response):
        self.rules.clear()
        self.rule(*match, **response)

    def test_cabin_run_released_reflects_the_checkout(self):
        started = "motel: cabin swift-gull-0123456789ab · EUR 0.0200/h net · ready in 40s\n"
        self.only("cabin", "run", exit=0, stdout="ok\n", stderr=started + "motel: released swift-gull-0123456789ab · "
                  "est. cost EUR 0.0100 net\n")
        self.assertIs(self.motel.cabin_run(["true"]).released, True)
        self.only("cabin", "run", exit=0, stdout="ok\n", stderr=started + "motel: WARNING: checkout of "
                  "swift-gull-0123456789ab is NOT complete (cleanup pending; it still bills until the reaper finishes). "
                  "The exit status is the command's. Try: motel cabin status swift-gull-0123456789ab\n")
        run = self.motel.cabin_run(["true"], check=True)           # a pending cleanup does not raise ...
        self.assertEqual((run.returncode, run.released), (0, False))   # ... the caller checks `released`
        self.only("cabin", "run", exit=4, stderr=started + "motel: checkout of swift-gull-0123456789ab FAILED (busy; "
                  "checkout exit 4). The cabin may still be ACTIVE and billing. Try: motel cabin checkout x\n")
        self.assertIs(self.motel.cabin_run(["true"]).released, False)
        with self.assertRaises(Busy):
            self.motel.cabin_run(["true"], check=True)
        self.only("cabin", "run", exit=0, stdout="ok\n", stderr=started)                 # --keep: not checked out
        self.assertIsNone(self.motel.cabin_run(["true"], keep=True).released)

    def test_cabin_run_nonzero_is_data_unless_check(self):
        self.rule("cabin", "run", exit=3, stderr="motel: tests failed. Try: motel cabin ls\n")
        self.assertEqual(self.motel.cabin_run("make test").returncode, 3)
        self.assertEqual(self.calls()[-1][-3:], ["sh", "-c", "make test"])
        with self.assertRaises(NoVacancy):
            self.motel.cabin_run(["make"], check=True)

    def test_cabin_ssh(self):
        self.rule("cabin", "ssh", stdout="ok\n")
        cabin = self.motel.cabin_ssh("swift-gull-0123456789ab", ["uname", "-a"])
        self.assertEqual(self.calls()[-1], ["cabin", "ssh", "swift-gull-0123456789ab", "--", "uname", "-a"])
        self.assertEqual((cabin.returncode, cabin.stdout, cabin.cabin_id), (0, "ok\n", "swift-gull-0123456789ab"))


class AsyncTest(SdkTest):
    def test_async_room_and_cabin(self):
        self.rule("checkin", **ok(room=ROOM))
        self.rule("checkout", **ok(room=ROOM))
        self.rule("cabin", "checkin", stdout=json.dumps(CABIN))
        self.rule("cabin", "checkout", **ok(cabin={**CABIN, "state": "released"}))
        amotel = AsyncMotel(str(self.binary), 10, env=self.motel.env)

        async def scenario():
            async with amotel.room(50, task="video-render") as room:
                self.assertEqual(room.id, "video-render-k3f9")
            async with amotel.cabin(ttl="1h") as cabin:
                self.assertEqual(cabin.id, "swift-gull-0123456789ab")
            with self.assertRaises(NoVacancy):
                self.rules.insert(0, {"match": ["ls"], **fail(3, "full")})
                self.save()
                await amotel.rooms()

        asyncio.run(scenario())
        self.assertEqual([c[0] if c[0] != "cabin" else c[1] for c in self.calls()],
                         ["checkin", "checkout", "checkin", "checkout", "ls"])


if __name__ == "__main__":
    unittest.main()
