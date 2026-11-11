"""Unit tests for the live harness itself, run against a fake `hcloud`, `ssh`, `scp` and `ssh-keygen` (no cloud, no network, no root).

    python3 -m unittest discover -s tests/live -p 'test_harness.py' -v
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import shlex
import signal
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from types import SimpleNamespace
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
for extra in (HERE, os.path.join(HERE, "fakes")):
    if extra not in sys.path:
        sys.path.insert(0, extra)

import fakeworld  # noqa: E402
import run_live  # noqa: E402
from motel_live import cabins as cabins_mod  # noqa: E402
from motel_live import core  # noqa: E402
from motel_live import omnigent_phase as omni_mod  # noqa: E402
from motel_live import rooms as rooms_mod  # noqa: E402
from motel_live.cloud import Hcloud, server_ip_ids  # noqa: E402

from platform_marks import linux_only  # noqa: E402

SECRET = "tok-" + "Zq9XkLm2PvB7nRt4WcYd8HsJ5eGa3UfNo1"  # the rooms project's token
SECRET_CABINS = "tok-" + "Cb4MnVx8RzQ2LpT6YgH9KdJ3WsA7UeF5Ni0"  # the cabins project's token
PYTHON = sys.executable
CABINS_CHECK = "cabins: config accepted, cabin setup with the cabins token file in the cabins project, doctor ok"
ROOMS_FINAL = "teardown: final inventory equals the initial inventory"
CABINS_FINAL = "teardown: cabins project final inventory equals its initial inventory"
CABINS_PREFLIGHT = "preflight: the cabins project holds nothing outside this run"


class FakeClock(core.Clock):
    """Sleeping advances virtual time, here and in the fake world, without waiting."""

    def __init__(self, on_sleep=None):
        self.offset, self.sleeps, self.on_sleep = 0.0, 0, on_sleep

    def time(self):
        return time.time() + self.offset

    def sleep(self, seconds):
        self.sleeps += 1
        if self.on_sleep:
            self.on_sleep(self.sleeps)
        self.offset += seconds
        with fakeworld.world_lock():   # a fake ssh of the holder may be writing the world at this moment
            w = fakeworld.load()
            w["offset"] += seconds
            fakeworld.save(w)


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        root = self.tmp.name
        self.world_dir = os.path.join(root, "world")
        self.bin = os.path.join(root, "bin")
        self.out = os.path.join(root, "out")
        os.makedirs(self.world_dir)
        os.makedirs(self.bin)
        fake = shlex.quote(os.path.join(HERE, "fakes", "fakeworld.py"))
        for tool in ("hcloud", "ssh", "scp", "ssh-keygen"):
            self.write_bin(tool, f"exec {shlex.quote(PYTHON)} {fake} {tool} \"$@\"")
        # Wrappers as in a real run: "WRAPPER --" then the hcloud arguments. hcloud-cabins reaches the cabins project,
        # hcloud-same is a (mistaken) second wrapper that reaches the rooms project again.
        self.write_bin("hcloud-cabins", f'[ "$1" = -- ] && shift; FAKE_PROJECT=cabins exec {shlex.quote(PYTHON)} {fake} hcloud "$@"')
        self.write_bin("hcloud-same", f'[ "$1" = -- ] && shift; exec {shlex.quote(PYTHON)} {fake} hcloud "$@"')
        self.token = os.path.join(root, "token")
        self.cabins_token = os.path.join(root, "cabins-token")
        for path, value in ((self.token, SECRET), (self.cabins_token, SECRET_CABINS)):
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(value + "\n")
        with open(os.path.join(self.world_dir, "tokens.json"), "w", encoding="utf-8") as handle:
            json.dump({SECRET: "rooms", SECRET_CABINS: "cabins"}, handle)
        self.env = {"PATH": self.bin + os.pathsep + os.environ["PATH"], "FAKE_WORLD": self.world_dir}
        saved = {k: os.environ.get(k) for k in self.env}
        os.environ.update(self.env)
        self.addCleanup(lambda: [os.environ.__setitem__(k, v) if v is not None else os.environ.pop(k, None) for k, v in saved.items()])
        # the negative port-22 probe runs on THIS machine; it must never touch the network in tests
        self.probes = []
        self.probe_open = False
        clean = mock.patch.object(cabins_mod.CabinsScenario, "CLEAN_TIMEOUT", 30)  # failure cases would otherwise poll for 5 virtual minutes
        clean.start()
        self.addCleanup(clean.stop)
        patcher = mock.patch.object(cabins_mod, "tcp_open", side_effect=lambda host, port, timeout=10: self._probe(host, port))
        patcher.start()
        self.addCleanup(patcher.stop)

    def _probe(self, host, port):
        self.probes.append((host, port))
        return self.probe_open

    def write_bin(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(f"#!/bin/sh\n{body}\n")
        os.chmod(path, os.stat(path).st_mode | stat.S_IXUSR)

    @property
    def cabin_args(self):
        return ["--cabins", "--cabins-hcloud-cmd", "hcloud-cabins --", "--cabins-token-file", self.cabins_token]

    # ------------------------------------------------------------- helpers
    def world(self):
        return fakeworld.load()

    def seed(self, fn, project="rooms"):
        with fakeworld.world_lock():
            w = fakeworld.load(project)
            fn(w)
            fakeworld.save(w, project)

    def cabins_world(self):
        return fakeworld.load("cabins")

    def add_server(self, w, name, labels, ip=None):
        sid = fakeworld.new_id(w)
        w["servers"][sid] = {"id": sid, "name": name, "labels": labels, "ip": ip or f"198.51.100.{len(w['servers']) + 1}"}
        return sid

    def add_volume(self, w, name, labels, size=10, server=None):
        vid = fakeworld.new_id(w)
        w["volumes"][vid] = {"id": vid, "name": name, "labels": labels, "size": size, "server": server}
        return vid

    def run_main(self, *args, clock=None, token=True):
        argv = list(args)
        if token:
            argv += ["--token-file", self.token]
        argv += ["--out-dir", self.out]
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            rc = run_live.main(argv, clock=clock or FakeClock())
        return rc, out.getvalue(), err.getvalue()

    def report(self):
        with open(os.path.join(self.out, "report.json"), encoding="utf-8") as handle:
            return json.load(handle)

    def statuses(self, report=None):
        return {c["name"]: c["status"] for c in (report or self.report())["checks"]}

    def assert_project_empty(self):
        w = self.world()
        self.assertEqual({}, w["servers"], "servers left behind")
        self.assertEqual({}, w["volumes"], "volumes left behind")
        self.assertEqual({}, w["ssh_keys"], "ssh keys left behind")
        self.assert_cabins_project_empty()

    def assert_cabins_project_empty(self):
        c = self.cabins_world()
        for table in ("servers", "volumes", "firewalls", "ssh_keys"):
            self.assertEqual({}, c[table], f"cabins project: {table} left behind")

    def log_text(self):
        with open(os.path.join(self.out, "log.txt"), encoding="utf-8") as handle:
            return handle.read()


class FullRun(Base):
    def test_full_rooms_scenario_passes_tears_down_and_never_leaks_the_token(self):
        self.seed(lambda w: w.update(modes=["leak"]))  # the fake desk echoes the token to stderr; the harness must redact it
        rc, out, err = self.run_main("--hcloud-cmd", "hcloud")
        report = self.report()
        failed = [(c["name"], c["detail"]) for c in report["checks"] if c["status"] == "FAIL"]
        self.assertEqual([], failed)
        self.assertEqual(0, rc, out)
        st = self.statuses(report)
        self.assertEqual("PASS", st["rooms: the pre-existing bystander volume is untouched (id, size, labels, attachment, protection)"])
        self.assertEqual("PASS", st["rooms: desk killed mid-checkin (volume created) - reap reconciles with no orphan"])
        self.assertEqual("PASS", st["teardown: final inventory equals the initial inventory"])
        self.assert_project_empty()
        # redaction: the token is in no log, report, console output, or hcloud/ssh argument
        def read(name):
            with open(os.path.join(self.out, name), encoding="utf-8") as handle:
                return handle.read()
        blobs = [out, err, read("log.txt"), read("report.json"), json.dumps(self.world()["calls"])]
        for blob in blobs:
            self.assertNotIn(SECRET, blob)
        self.assertIn(core.REDACTED, blobs[2], "the leak should have been redacted, not just absent")
        self.assertIn("--token-file /root/.motel-live-token", blobs[2])
        self.assertTrue(report["spend_estimate"]["billed_hours"] >= 1)
        self.assertFalse(os.path.exists(os.path.join(self.out, "id_ed25519")), "the private key must be removed at the end")

    ROOMS_RETIRE = ("rooms: checkout --now of every room, then reap retires every floor (a shared floor in its release "
                    "window, a suite floor after retire_after) and deletes every motel volume")

    def test_a_shared_floor_is_held_to_its_release_window_and_a_suite_floor_is_not(self):
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud")
        report = self.report()
        self.assertEqual("PASS", self.statuses(report)[self.ROOMS_RETIRE], out)
        check = [c for c in report["checks"] if c["name"] == self.ROOMS_RETIRE][0]
        log = "\n".join(check["evidence"])                    # the evidence names the window the shared floor was held to
        self.assertRegex(log, r"shared floor \d+: deleted between \d+s and \d+s after creation; expected \d+s to \d+s")

    def test_a_floor_that_held_no_listed_room_is_judged_without_the_early_delete_bound_and_the_evidence_says_so(self):
        real = rooms_mod.RoomsScenario.motel

        def motel(scenario, *args, **kw):
            res, doc = real(scenario, *args, **kw)
            if args[:1] == ("ls",) and not kw and scenario.clock.time() > 0:
                for room in doc.get("rooms") or []:
                    room["floor"] = "f-unlisted"                  # the floor of these rooms is no longer visible
            return res, doc
        with mock.patch.object(rooms_mod.RoomsScenario, "motel", motel):
            rc, out, _ = self.run_main("--hcloud-cmd", "hcloud")
        report = self.report()
        self.assertEqual("PASS", self.statuses(report)[self.ROOMS_RETIRE], out)
        check = [c for c in report["checks"] if c["name"] == self.ROOMS_RETIRE][0]
        evidence = "\n".join(check["evidence"])
        self.assertIn("is not observable", evidence)
        self.assertIn("lower bound", evidence)

    def test_a_floor_retired_the_moment_retire_after_is_over_fails_the_rooms_check(self):
        self.seed(lambda w: w.update(modes=["floor-retired-at-once"]))
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud")
        self.assertEqual(1, rc)
        failed = [c for c in self.report()["checks"] if c["status"] == "FAIL"]
        self.assertEqual([self.ROOMS_RETIRE], [c["name"] for c in failed], failed)
        self.assertIn("saves nothing", failed[0]["detail"])

    def test_every_provider_write_carries_the_run_label(self):
        self.run_main("--hcloud-cmd", "hcloud")
        run_id = self.report()["run_id"]
        for call in self.world()["calls"]:
            if call[1] == "create":
                self.assertIn(f"motel-live={run_id}", call, f"unlabelled create: {call}")


class CabinsPhase(Base):
    """The cabins phase runs in a second, dedicated project: its own wrapper, its own token, its own preflight and inventory."""

    def test_cabins_phase_skips_cleanly_when_cabin_setup_cannot_take_a_token_file(self):
        self.seed(lambda w: w.update(modes=["interactive-cabin-setup"]))
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args)
        self.assertEqual(0, rc, out)
        st = self.statuses()
        self.assertEqual("SKIP", st[CABINS_CHECK])
        self.assertEqual(1, sum(1 for name in st if name.startswith("cabins:")), "no other cabin check may run after the skip")
        self.assert_project_empty()

    def test_the_test_servers_primary_ips_do_not_fail_the_cabin_residue_checks(self):
        # the fake cloud, like Hetzner, gives the rooms project's test server an unlabelled IPv4 and IPv6 of its own
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args, "--cabins-skip", "idle,ttl,disk")
        self.assertEqual(0, rc, out)
        self.assertIn("primary-ip list", " ".join(" ".join(c) for c in self.world()["calls"]))
        self.assertEqual({"PASS"}, {s for n, s in self.statuses().items() if n.startswith("cabins:")})
        self.assertEqual("PASS", self.statuses()[ROOMS_FINAL])

    def test_full_run_covers_both_projects_end_to_end(self):
        self.seed(lambda w: w.update(modes=["leak"]))  # the fake desk echoes both tokens to stderr; the harness must redact them
        rc, out, err = self.run_main("--hcloud-cmd", "hcloud", *self.cabin_args)
        report = self.report()
        self.assertEqual([], [(c["name"], c["detail"]) for c in report["checks"] if c["status"] == "FAIL"])
        self.assertEqual(0, rc, out)
        st = self.statuses(report)
        cabin_checks = {name: status for name, status in st.items() if name.startswith("cabins:")}
        self.assertGreaterEqual(len(cabin_checks), 9, cabin_checks)
        self.assertEqual({"PASS"}, set(cabin_checks.values()), cabin_checks)
        for name in (CABINS_PREFLIGHT, ROOMS_FINAL, CABINS_FINAL):
            self.assertEqual("PASS", st[name], name)
        self.assertEqual({"server", "volume", "ssh-key", "firewall", "primary-ip", "floating-ip", "network"},
                         set(report["initial_inventory_cabins"]))
        self.assert_project_empty()
        # each project got its own token: the rooms token for `setup`, the cabins token for `cabin setup`, never mixed
        w, cw = self.world(), self.cabins_world()
        self.assertEqual(SECRET, w["fs"]["/etc/motel/credentials/hetzner.token"]["content"].strip())
        self.assertEqual(SECRET_CABINS, w["fs"]["/etc/motel/credentials/cabins.token"]["content"].strip())
        self.assertNotIn("/root/.motel-live-cabins-token", w["fs"], "the cabins token was left on the server")
        self.assertNotIn("/root/.motel-live-token", w["fs"])
        log = self.log_text()
        scps = [line for line in log.splitlines() if line.startswith("$ scp ")]
        to_rooms = [line for line in scps if "/root/.motel-live-token" in line]
        to_cabins = [line for line in scps if "/root/.motel-live-cabins-token" in line]
        self.assertTrue(to_rooms and to_cabins)
        self.assertTrue(all(f" {self.token} " in line and self.cabins_token not in line for line in to_rooms), to_rooms)
        self.assertTrue(all(f" {self.cabins_token} " in line and self.token not in line for line in to_cabins), to_cabins)
        self.assertIn("motel-desk cabin setup hetzner --token-file /root/.motel-live-cabins-token", log)
        self.assertNotIn("motel-desk cabin setup hetzner --token-file /root/.motel-live-token", log)
        # neither token is anywhere
        blobs = [out, err, log, json.dumps(report), json.dumps(w["calls"]), json.dumps(cw["calls"])]
        for blob in blobs:
            self.assertNotIn(SECRET, blob)
            self.assertNotIn(SECRET_CABINS, blob)
        self.assertIn(core.REDACTED, log)
        # the harness only ever READS the cabins project (cabins are created and deleted by motel itself)
        self.assertEqual([], [c for c in cw["calls"] if c[1] in ("create", "delete", "disable-protection")], cw["calls"])
        self.assertTrue(cw["calls"], "the cabins project was never queried")
        # everything the harness creates in the rooms project is labelled for this run
        for call in w["calls"]:
            if call[1] == "create":
                self.assertIn(f"motel-live={report['run_id']}", call)

    def test_cabin_checks_run_on_the_test_server_and_only_the_negative_probe_runs_here(self):
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args, "--cabins-skip", "idle,ttl,disk")
        self.assertEqual(0, rc, out)
        st = self.statuses()
        self.assertEqual("PASS", st["cabins: SSH on the cabin's public address is reachable from the test server (the allowlisted address)"])
        self.assertEqual("PASS", st["cabins: SSH on the cabin's public address is closed to the operator's machine (not allowlisted)"])
        local = [line for line in self.log_text().splitlines() if line.startswith("$ ") and not line.startswith(("$ ssh ", "$ scp "))]
        repo = os.path.dirname(os.path.dirname(HERE))      # a checkout directory may itself be named motel-cabin-...
        for line in local:
            line = line.replace(repo, "<repo>")
            self.assertNotRegex(line, r"motel[ -]cabin|cabin (run|checkin|checkout|ls|reap|setup|doctor)", f"cabin command ran locally: {line}")
        ssh_lines = [line for line in self.log_text().splitlines() if line.startswith("$ ssh ")]
        self.assertTrue(any("/dev/tcp/" in line for line in ssh_lines), "the reachability connect was not made over ssh to the test server")
        self.assertTrue(any("/usr/local/bin/motel cabin run" in line or "motel cabin run" in line for line in ssh_lines))
        # the only connection made from this machine is the negative probe, to a cabin address, on port 22
        self.assertEqual(1, len(self.probes), self.probes)
        self.assertEqual(22, self.probes[0][1])
        self.assertTrue(self.probes[0][0].startswith("192.0.2."), self.probes)

    def test_the_operators_machine_reaching_port_22_fails_the_run(self):
        self.probe_open = True  # the firewall let the operator in: the allowlist is wrong
        rc, _, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args, "--cabins-skip", "idle,ttl,disk,run")
        self.assertEqual(1, rc)
        self.assertEqual("FAIL", self.statuses()["cabins: SSH on the cabin's public address is closed to the operator's machine (not allowlisted)"])
        self.assert_project_empty()

    def test_missing_cabins_options_skip_the_phase_with_a_clear_message(self):
        for extra, missing in ((["--cabins"], "--cabins-hcloud-cmd and --cabins-token-file"),
                               (["--cabins", "--cabins-hcloud-cmd", "hcloud-cabins --"], "--cabins-token-file"),
                               (["--cabins", "--cabins-token-file", self.cabins_token], "--cabins-hcloud-cmd")):
            with self.subTest(extra=extra):
                fakeworld.save(fakeworld.load("cabins"), "cabins")
                rc, out, err = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *extra)
                self.assertEqual(0, rc, out)
                self.assertIn("cabins phase skipped", err)
                self.assertIn(missing, err)
                st = self.statuses()
                self.assertEqual("SKIP", st["cabins: phase skipped"])
                self.assertEqual(1, sum(1 for name in st if name.startswith("cabins:")))
                self.assertNotIn(CABINS_PREFLIGHT, st, "the cabins project must not be touched when the phase is skipped")
                self.assertEqual([], self.cabins_world()["calls"])
                self.assert_project_empty()

    def test_rooms_only_run_never_touches_the_cabins_project(self):
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms")
        self.assertEqual(0, rc, out)
        self.assertEqual([], self.cabins_world()["calls"])

    def test_the_rooms_token_in_the_cabins_slot_fails_the_run_where_cabin_setup_refuses_the_host_project(self):
        # a second token file whose project is, in fact, the rooms project: the toy's cabin setup refuses it exactly like motel (rc 6)
        wrong = os.path.join(self.tmp.name, "wrong-token")
        value = "tok-" + "Rw7TkHn3QzV9BcLx5MdG2sPy8JfA4UeY6Ni1"
        with open(wrong, "w", encoding="utf-8") as handle:
            handle.write(value + "\n")
        with open(os.path.join(self.world_dir, "tokens.json"), "w", encoding="utf-8") as handle:
            json.dump({SECRET: "rooms", SECRET_CABINS: "cabins", value: "rooms"}, handle)
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", "--cabins", "--cabins-hcloud-cmd", "hcloud-cabins --",
                                   "--cabins-token-file", wrong)
        self.assertEqual(1, rc, out)
        failed = [c for c in self.report()["checks"] if c["status"] == "FAIL"]
        self.assertEqual([CABINS_CHECK], [c["name"] for c in failed])
        self.assertIn("rc=6", failed[0]["detail"])
        self.assertIn("dedicated cabin project required", failed[0]["detail"])
        self.assertNotIn(value, self.log_text())
        self.assert_project_empty()

    def test_cabins_created_in_the_wrong_project_fail_the_run_and_are_swept(self):
        # simulated fault: motel's cabin resources land in the rooms project, and the firewall is also forgotten at checkout
        self.seed(lambda w: w.update(modes=["misroute", "leak-cabin-firewall"]))
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args, "--cabins-skip", "idle,ttl,disk,run")
        self.assertEqual(1, rc, out)
        st = self.statuses()
        self.assertEqual("FAIL", st["cabins: the cabin firewall admits only the test server's own address (/32) to SSH"])
        self.assertEqual("FAIL", st["cabins: after the phase no cabin server, firewall, address or volume remains"])
        detail = [c for c in self.report()["checks"] if c["name"].startswith("cabins: after the phase")][0]["detail"]
        self.assertIn("ROOMS project (wrong place)", detail)
        self.assertEqual("FAIL", st[ROOMS_FINAL], "a leak into the rooms project must fail the run even though it was swept")
        self.assertEqual("PASS", st[CABINS_FINAL])
        self.assert_project_empty()

    def test_a_cabin_remnant_in_the_cabins_project_fails_the_run_and_is_swept(self):
        self.seed(lambda w: w.update(modes=["leak-cabin-firewall"]))  # checkout forgets the firewall
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args, "--cabins-skip", "idle,ttl,disk,run")
        self.assertEqual(1, rc, out)
        st = self.statuses()
        self.assertEqual("FAIL", st["cabins: checkout deletes the cabin, its firewall and its address"])
        self.assertEqual("FAIL", st["teardown: no cabin remnants of this run in the cabins project"])
        self.assertEqual("PASS", st[ROOMS_FINAL])
        self.assertEqual("PASS", st[CABINS_FINAL], "after the sweep the cabins project equals its initial inventory")
        self.assert_project_empty()

    def test_no_sweep_keeps_the_cabin_remnant_and_exits_4_and_cleanup_finishes_it(self):
        self.seed(lambda w: w.update(modes=["leak-cabin-firewall"]))
        rc, _, err = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args, "--cabins-skip", "idle,ttl,disk,run",
                                   "--no-sweep")
        self.assertEqual(4, rc)
        self.assertIn("--cabins-hcloud-cmd", err, "the cleanup hint must name both wrappers")
        self.assertEqual(1, len(self.cabins_world()["firewalls"]))
        self.assertEqual("FAIL", self.statuses()[CABINS_FINAL])
        run_id = self.report()["run_id"]
        # without the cabins wrapper the cabins project cannot be swept: say so loudly
        rc, out, err = self.run_main("--cleanup", run_id, "--hcloud-cmd", "hcloud", token=False)
        self.assertEqual(4, rc)
        self.assertIn("--cabins-hcloud-cmd", err)
        self.assertEqual(1, len(self.cabins_world()["firewalls"]))
        rc, out, _ = self.run_main("--cleanup", run_id, "--hcloud-cmd", "hcloud", "--cabins-hcloud-cmd", "hcloud-cabins --", token=False)
        self.assertEqual(0, rc, out)
        self.assert_project_empty()

    def test_a_failed_delete_in_the_cabins_project_exits_4(self):
        self.seed(lambda w: w.update(modes=["leak-cabin-firewall"]))
        self.seed(lambda w: w.update(fail=["firewall delete"]), project="cabins")
        rc, _, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args, "--cabins-skip", "idle,ttl,disk,run")
        self.assertEqual(4, rc)
        self.assertTrue(self.cabins_world()["firewalls"])

    def test_both_projects_must_end_equal_to_their_initial_inventory(self):
        # a pre-existing firewall is not allowed in the cabins project (preflight), but ssh keys are: they are carried through unchanged
        self.seed(lambda w: w["ssh_keys"].update({"k9": {"id": "k9", "name": "operator", "labels": {}}}), project="cabins")
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args, "--cabins-skip", "idle,ttl,disk,run,firewall")
        self.assertEqual(0, rc, out)
        self.assertEqual(["k9"], list(self.cabins_world()["ssh_keys"]))
        self.assertEqual("PASS", self.statuses()[CABINS_FINAL])

    def test_the_cabins_project_must_not_be_the_rooms_project(self):
        rc, out, err = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", "--cabins", "--cabins-hcloud-cmd", "hcloud",
                                     "--cabins-token-file", self.cabins_token)
        self.assertEqual(2, rc)
        self.assertIn("same command", err)
        # a different command that nevertheless reaches the rooms project: caught once the test server exists
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", "--cabins", "--cabins-hcloud-cmd", "hcloud-same --",
                                   "--cabins-token-file", self.cabins_token)
        self.assertEqual(1, rc, out)
        failed = [c for c in self.report()["checks"] if c["status"] == "FAIL"]
        self.assertEqual([CABINS_CHECK], [c["name"] for c in failed])
        self.assertIn("reaches the rooms project", failed[0]["detail"])
        self.assert_project_empty()

    def test_the_same_token_for_both_projects_is_a_usage_error(self):
        rc, _, err = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", "--cabins", "--cabins-hcloud-cmd", "hcloud-cabins --",
                                   "--cabins-token-file", self.token)
        self.assertEqual(2, rc)
        self.assertIn("same token", err)
        self.assertEqual([], self.world()["calls"])

    def test_a_cabins_fragment_that_would_overwrite_the_rooms_token_is_refused(self):
        frag = os.path.join(self.tmp.name, "cabins.toml")
        with open(frag, "w", encoding="utf-8") as handle:
            handle.write('[cabins]\nenabled = true\ntoken_file = "/etc/motel/credentials/hetzner.token"\n')
        rc, _, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args, "--cabins-config", frag)
        self.assertEqual(1, rc)
        detail = [c for c in self.report()["checks"] if c["status"] == "FAIL"][0]["detail"]
        self.assertIn("overwrite", detail)
        self.assert_project_empty()


    # ---- hourly billing: a shared cabin, and an idle cabin that waits for its release window
    def cabin_checkins(self):
        return [line for line in self.log_text().splitlines() if line.startswith("$ ssh ") and " cabin checkin" in line]

    def test_the_firewall_ssh_and_disk_checks_share_one_cabin(self):
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args, "--cabins-skip", "run,idle,ttl")
        self.assertEqual(0, rc, out)
        checkins = self.cabin_checkins()
        self.assertEqual(1, len(checkins), checkins)
        self.assertIn("--disk 10G", checkins[0])
        st = self.statuses()
        for name in ("cabins: the cabin firewall admits only the test server's own address (/32) to SSH",
                     "cabins: SSH on the cabin's public address is reachable from the test server (the allowlisted address)",
                     "cabins: SSH on the cabin's public address is closed to the operator's machine (not allowlisted)",
                     "cabins: --disk attaches a volume to the cabin (the checkout below removes it)",
                     "cabins: checkout deletes the cabin, its firewall, its address and its disk"):
            self.assertEqual("PASS", st[name], name)
        self.assert_project_empty()

    def test_without_the_disk_check_the_shared_cabin_has_no_disk(self):
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args, "--cabins-skip", "run,idle,ttl,disk")
        self.assertEqual(0, rc, out)
        checkins = self.cabin_checkins()
        self.assertEqual(1, len(checkins))
        self.assertNotIn("--disk", checkins[0])
        self.assertEqual("PASS", self.statuses()["cabins: checkout deletes the cabin, its firewall and its address"])

    def test_the_disk_check_alone_takes_its_own_cabin_and_still_checks_it_out(self):
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args, "--cabins-skip", "run,idle,ttl,firewall")
        self.assertEqual(0, rc, out)
        self.assertEqual(1, len(self.cabin_checkins()))
        self.assertEqual("PASS", self.statuses()["cabins: --disk attaches a volume to the cabin (the checkout below removes it)"])
        self.assert_project_empty()

    def test_ttl_idle_checkin_ephemeral_run_and_checkout_cleanup_keep_their_own_cabins(self):
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args)
        self.assertEqual(0, rc, out)
        tasks = [line for line in self.cabin_checkins()]
        # one shared cabin, plus the TTL cabin and the idle cabin; `cabin run` (twice) makes its own ephemeral cabins
        self.assertEqual(3, len(tasks), tasks)
        self.assertEqual(1, sum("--task ttl-test" in t for t in tasks))
        self.assertEqual(1, sum("--task idle-test" in t for t in tasks))
        self.assertEqual(2, sum(" cabin run " in line for line in self.log_text().splitlines() if line.startswith("$ ssh ")))

    def test_an_idle_cabin_deleted_the_moment_it_is_idle_fails_the_idle_check(self):
        self.seed(lambda w: w.update(modes=["cabin-idle-delete-at-once"]))
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args, "--cabins-skip", "run,firewall,disk")
        self.assertEqual(1, rc)
        failed = [c for c in self.report()["checks"] if c["status"] == "FAIL"]
        self.assertEqual(1, len(failed), failed)
        self.assertIn("release window", failed[0]["name"])
        self.assertIn("saves nothing", failed[0]["detail"])

    def test_an_idle_cabin_that_waits_for_its_release_window_passes_and_the_ttl_cabin_is_not_held(self):
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", *self.cabin_args, "--cabins-skip", "run,firewall,disk")
        self.assertEqual(0, rc, out)
        st = self.statuses()
        self.assertEqual("PASS", st["cabins: a cabin past its 5m TTL is hard-deleted by reap"])
        idle = [n for n in st if n.startswith("cabins: an idle cabin is sealed")]
        self.assertEqual(1, len(idle), list(st))
        self.assertEqual("PASS", st[idle[0]])


class Preflight(Base):
    def test_refuses_when_the_project_holds_unlabelled_servers_or_volumes(self):
        self.seed(lambda w: self.add_server(w, "permanent-host", {}))
        rc, out, err = self.run_main("--hcloud-cmd", "hcloud")
        self.assertEqual(3, rc)
        self.assertEqual("FAIL", self.report()["checks"][0]["status"])
        w = self.world()
        self.assertFalse([c for c in w["calls"] if c[1] in ("create", "delete")], "nothing may be created or deleted on refusal")
        self.assertEqual(1, len(w["servers"]))

    def test_refuses_for_a_volume_labelled_for_another_run(self):
        self.seed(lambda w: self.add_volume(w, "left-over", {"motel-live": "someone-elses-run"}))
        rc, _, _ = self.run_main("--hcloud-cmd", "hcloud")
        self.assertEqual(3, rc)
        self.assertEqual(1, len(self.world()["volumes"]))
        self.assertFalse([c for c in self.world()["calls"] if c[1] in ("create", "delete")])

    def test_refuses_when_the_cabins_project_holds_anything_outside_the_run(self):
        for what, seed in (("server", lambda w: self.add_server(w, "someones-box", {})),
                           ("volume", lambda w: self.add_volume(w, "data", {"motel-live": "another-run"})),
                           ("firewall", lambda w: w["firewalls"].update({"77": {"id": "77", "name": "web", "labels": {}}}))):
            with self.subTest(what=what):
                fakeworld.save(fakeworld.load("cabins"), "cabins")
                self.seed(seed, project="cabins")
                before = json.dumps(self.cabins_world(), sort_keys=True)
                rc, out, err = self.run_main("--hcloud-cmd", "hcloud", *self.cabin_args)
                self.assertEqual(3, rc, out)
                self.assertEqual("FAIL", self.statuses()[CABINS_PREFLIGHT])
                self.assertIn("cabins project already holds", [c for c in self.report()["checks"] if c["name"] == CABINS_PREFLIGHT][0]["detail"])
                w = self.world()
                self.assertEqual({}, w["servers"], "nothing may be created in the rooms project either")
                self.assertFalse([c for c in w["calls"] if c[1] in ("create", "delete")])
                after = self.cabins_world()
                self.assertFalse([c for c in after["calls"] if c[1] in ("create", "delete")])
                self.assertEqual(json.loads(before)["servers"], after["servers"])
                os.remove(fakeworld.world_path("cabins"))

    def test_bad_arguments_are_usage_errors(self):
        self.assertEqual(2, self.run_main("--run-id", "x;rm -rf", token=True)[0])
        self.assertEqual(2, self.run_main(token=False)[0])
        self.assertEqual(2, self.run_main("--cleanup", "BAD ID")[0])


class Teardown(Base):
    def test_failure_during_provisioning_still_destroys_what_was_created(self):
        self.seed(lambda w: w.update(fail=["volume create"]))
        rc, _, _ = self.run_main("--hcloud-cmd", "hcloud")
        self.assertEqual(1, rc)
        self.assert_project_empty()
        self.assertEqual("FAIL", self.statuses()["provision: pre-create the bystander volume attached to the server (before motel setup)"])

    def test_failure_in_the_middle_of_the_scenario_still_destroys_everything(self):
        os.unlink(self.token)
        with open(self.token, "w", encoding="utf-8") as handle:
            handle.write("short")  # the fake desk refuses a token this short, so setup fails after the server and bystander exist
        rc, _, _ = self.run_main("--hcloud-cmd", "hcloud")
        self.assertEqual(1, rc)
        self.assertEqual("FAIL", self.statuses()["setup: motel-desk setup hetzner --token-file discovers the server and records the bystander as never-touch"])
        self.assert_project_empty()

    def test_keyboard_interrupt_mid_run_destroys_everything(self):
        def boom(n):
            if n == 4:
                raise KeyboardInterrupt
        rc, _, _ = self.run_main("--hcloud-cmd", "hcloud", clock=FakeClock(boom))
        self.assertEqual(130, rc)
        self.assert_project_empty()
        self.assertIn("run interrupted", self.statuses())

    def test_sigterm_behaves_like_ctrl_c(self):
        def term(n):
            if n == 4:
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.2)
        rc, _, err = self.run_main("--hcloud-cmd", "hcloud", clock=FakeClock(term))
        self.assertEqual(130, rc, [(c["name"], c["status"], c["detail"]) for c in self.report()["checks"] if c["status"] == "FAIL"])
        self.assert_project_empty()

    def test_a_failed_delete_is_reported_loudly_with_exit_4(self):
        self.seed(lambda w: w.update(fail=["server delete"]))
        rc, _, err = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms")
        self.assertEqual(4, rc)
        self.assertIn("--cleanup", err)
        self.assertTrue(self.world()["servers"])
        self.seed(lambda w: w.update(fail=[]))
        run_id = self.report()["run_id"]
        rc, out, _ = self.run_main("--cleanup", run_id, "--hcloud-cmd", "hcloud", token=False)
        self.assertEqual(0, rc, out)
        self.assert_project_empty()

    def test_leaked_cabin_style_resources_are_flagged_and_swept(self):
        self.seed(lambda w: w.update(modes=["leak-fw"]))  # a firewall appears during the run that was not in the initial inventory
        rc, _, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms")
        final = [c for c in self.report()["checks"] if c["name"].startswith("teardown: final")][0]
        self.assertEqual("FAIL", final["status"])  # a leak fails the run even when sweeping it stops the billing
        self.assertEqual(1, rc)
        self.assertTrue(any("firewall 900" in e for e in final["evidence"]))
        self.assertEqual({}, self.world()["firewalls"])

    def test_no_sweep_leaves_the_leak_and_fails(self):
        self.seed(lambda w: w.update(modes=["leak-fw"]))
        rc, _, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", "--no-sweep")
        self.assertEqual(4, rc)
        self.assertIn("900", self.world()["firewalls"])


class CleanupMode(Base):
    def test_cleanup_deletes_only_what_belongs_to_the_run(self):
        ids = {}

        def seed(w):
            run = "20261108-000000-aaaa"
            ids["srv"] = self.add_server(w, "mine", {"motel-live": run})
            ids["bystander"] = self.add_volume(w, "bystander", {"motel-live": run, "role": "bystander"}, server=ids["srv"])
            ids["floor"] = self.add_volume(w, "motel-f-1", {"managed-by": "motel", "role": "floor", "host": ids["srv"]}, server=ids["srv"])
            w["ssh_keys"]["k1"] = {"id": "k1", "name": "mine", "labels": {"motel-live": run}}
            # things that must survive
            ids["other_srv"] = self.add_server(w, "permanent", {})
            ids["other_vol"] = self.add_volume(w, "precious", {}, server=ids["other_srv"])
            ids["foreign_motel"] = self.add_volume(w, "motel-f-2", {"managed-by": "motel", "role": "floor", "host": "424242"})
            ids["othrun_srv"] = self.add_server(w, "other-run", {"motel-live": "20261108-000000-bbbb"})
            ids["othrun_vol"] = self.add_volume(w, "other-run-vol", {"motel-live": "20261108-000000-bbbb"}, server=ids["othrun_srv"])
            w["ssh_keys"]["k2"] = {"id": "k2", "name": "other", "labels": {"motel-live": "20261108-000000-bbbb"}}
            w["ssh_keys"]["k3"] = {"id": "k3", "name": "personal", "labels": {}}
        self.seed(seed)
        rc, out, err = self.run_main("--cleanup", "20261108-000000-aaaa", "--hcloud-cmd", "hcloud", token=False)
        self.assertEqual(0, rc, out + err)
        w = self.world()
        self.assertEqual({ids["other_srv"], ids["othrun_srv"]}, set(w["servers"]))
        self.assertEqual({ids["other_vol"], ids["foreign_motel"], ids["othrun_vol"]}, set(w["volumes"]))
        self.assertEqual({"k2", "k3"}, set(w["ssh_keys"]))
        doc = json.loads(out)
        self.assertEqual([ids["foreign_motel"]], doc["left_alone_not_provably_this_run"])
        deletes = [c for c in w["calls"] if c[1] == "delete"]
        self.assertTrue(all(c[2] not in (ids["other_srv"], ids["other_vol"], ids["foreign_motel"], ids["othrun_srv"], ids["othrun_vol"], "k2", "k3")
                            for c in deletes), deletes)

    def test_cleanup_uses_the_recorded_server_id_when_the_server_is_already_gone(self):
        ids = {}

        def seed(w):
            ids["floor"] = self.add_volume(w, "motel-f-1", {"managed-by": "motel", "role": "floor", "host": "777"})
        self.seed(seed)
        os.makedirs(self.out)
        with open(os.path.join(self.out, "state.json"), "w", encoding="utf-8") as handle:
            json.dump({"run_id": "20261108-000000-cccc", "server_ids": ["777"]}, handle)
        rc, out, _ = self.run_main("--cleanup", "20261108-000000-cccc", "--hcloud-cmd", "hcloud", token=False)
        self.assertEqual(0, rc, out)
        self.assertEqual({}, self.world()["volumes"])


class CleanupBothProjects(Base):
    def test_cleanup_sweeps_this_runs_cabins_and_leaves_everything_else_alone(self):
        run, other = "20261108-000000-aaaa", "20261108-000000-bbbb"
        ids = {}

        def rooms(w):
            ids["srv"] = self.add_server(w, "mine", {"motel-live": run})
            ids["other_srv"] = self.add_server(w, "permanent", {})

        self.seed(rooms)
        host = ids["srv"]

        def cabin_labels(controller, lease):
            return {"managed-by": "motel", "role": "cabin", "controller": controller, "lease": lease}

        def cabins(w):
            ids["c_srv"] = self.add_server(w, "motel-job-aaaa", cabin_labels(host, "job-aaaa"))
            ids["c_vol"] = self.add_volume(w, "motel-job-aaaa", cabin_labels(host, "job-aaaa"), server=ids["c_srv"])
            w["firewalls"]["fw1"] = {"id": "fw1", "name": "motel-job-aaaa", "labels": cabin_labels(host, "job-aaaa")}
            ids["foreign_srv"] = self.add_server(w, "motel-job-bbbb", cabin_labels("424242", "job-bbbb"))  # another controller's cabin
            ids["personal"] = self.add_server(w, "my-own-box", {})  # not a cabin at all
            ids["other_run"] = self.add_server(w, "motel-live-other", {"motel-live": other})

        self.seed(cabins, project="cabins")
        os.makedirs(self.out)
        with open(os.path.join(self.out, "state.json"), "w", encoding="utf-8") as handle:
            json.dump({"run_id": run, "server_ids": [host], "cabins_project": True}, handle)
        rc, out, err = self.run_main("--cleanup", run, "--hcloud-cmd", "hcloud", "--cabins-hcloud-cmd", "hcloud-cabins --", token=False)
        self.assertEqual(0, rc, out + err)
        doc = json.loads(out)
        self.assertEqual(set(), {d for d in doc["cabins_project"]["deleted"]} - {f"server {ids['c_srv']}", f"volume {ids['c_vol']}", "firewall fw1"})
        self.assertEqual([f"server {ids['foreign_srv']}"], doc["cabins_project"]["left_alone_not_provably_this_run"])
        self.assertEqual({ids["other_srv"]}, set(self.world()["servers"]))
        c = self.cabins_world()
        self.assertEqual({ids["foreign_srv"], ids["personal"], ids["other_run"]}, set(c["servers"]))
        self.assertEqual({}, c["volumes"])
        self.assertEqual({}, c["firewalls"])
        for call in c["calls"]:
            if call[1] == "delete":
                self.assertNotIn(call[2], (ids["foreign_srv"], ids["personal"], ids["other_run"]))


class ToursPhase(Base):
    """The tours phase: install with --tours, two tours of command guests (four cabins), isolation, restart, failure and cancel."""

    FAKE_KEY_MARK = "LIVEFAKE"

    @property
    def tour_args(self):
        return ["--hcloud-cmd", "hcloud", "--skip-rooms", "--tours", "--cabins-hcloud-cmd", "hcloud-cabins --",
                "--cabins-token-file", self.cabins_token]

    def failed(self, report=None):
        return {c["name"]: c["detail"] for c in (report or self.report())["checks"] if c["status"] == "FAIL"}

    def run_tours(self, *extra, modes=None, world=None):
        if modes is not None or world is not None:
            self.seed(lambda w: w.update(modes=modes or [], **(world or {})))
        rc, out, err = self.run_main(*self.tour_args, *extra)
        return rc, out, err, self.report()

    def tour_checks(self, report=None):
        return {n: s for n, s in self.statuses(report).items() if n.startswith("tours:")}

    def test_full_run_passes_with_four_cabins_and_leaves_both_projects_empty(self):
        self.seed(lambda w: w.update(modes=["leak"]))
        rc, out, err, report = self.run_tours()
        self.assertEqual({}, self.failed(report))
        self.assertEqual(0, rc, out)
        checks = self.tour_checks(report)
        self.assertGreaterEqual(len(checks), 22, checks)
        self.assertEqual({"PASS"}, set(checks.values()), checks)
        self.assertEqual("PASS", self.statuses(report)[CABINS_FINAL])
        self.assert_project_empty()
        # cost: two tours of two cabins each, every one billed at least one hour, none left listed
        cost = report["tours_cabins"]
        self.assertEqual((4, 4), (cost["count"], cost["expected"]))
        self.assertTrue(all(c["billed_hours"] >= 1 and c["deleted"] and c["lease"] for c in cost["cabins"]), cost)
        self.assertEqual(4, cost["billed_hours"])
        self.assertEqual(4, len({c["server"] for c in cost["cabins"]}))
        # install order and what the server saw
        log = self.log_text()
        self.assertIn("install.sh --users alice --provider hetzner --cabins --tours --upgrade", log)
        self.assertIn("systemd-analyze verify /etc/systemd/system/motel-concierge.service /etc/systemd/system/motel-concierge.socket", log)
        self.assertIn("systemctl restart motel-concierge.service", log)
        self.assertIn("tour cancel", log)
        w = self.world()
        self.assertEqual(2, len(w["tours"]))
        self.assertEqual({"alpha", "bravo"}, {g["id"] for t in w["tours"].values() for g in t["guests"] if g["status"] == "done"})
        self.assertEqual({"fails": "failed", "hangs": "cancelled"},
                         {g["id"]: g["status"] for t in w["tours"].values() for g in t["guests"] if g["id"] in ("fails", "hangs")})
        self.assertNotIn("sampler", w, "the process sampler unit was left running")
        self.assertEqual(2, len(w["repo"]["branches"]))
        # the fake key: written root:concierge 0640, never in a log, report or console
        key = w["fs"]["/etc/motel/llm-keys/anthropic.key"]
        self.assertEqual("640", key["mode"])
        self.assertIn(self.FAKE_KEY_MARK, key["content"])
        self.assertTrue(key["content"].startswith("sk-ant-"))
        blobs = [out, err, log, json.dumps(report), json.dumps(w["calls"]), json.dumps(self.cabins_world()["calls"])]
        for blob in blobs:
            self.assertNotIn(key["content"].strip(), blob)
            self.assertNotIn(SECRET, blob)
            self.assertNotIn(SECRET_CABINS, blob)
        # the harness only reads the cabins project; motel creates and deletes the cabins
        self.assertEqual([], [c for c in self.cabins_world()["calls"] if c[1] in ("create", "delete", "disable-protection")])

    def test_tours_alone_sets_cabins_up_but_skips_the_cabin_checks_and_cabins_adds_them(self):
        rc, out, _, report = self.run_tours()
        self.assertEqual(0, rc, out)
        names = [n for n in self.statuses(report) if n.startswith("cabins:")]
        self.assertEqual([CABINS_CHECK, "cabins: after the phase no cabin server, firewall, address or volume remains"], names)
        self.assert_project_empty()
        rc, out, _, report = self.run_tours("--cabins", "--cabins-skip", "idle,ttl,disk")
        self.assertEqual(0, rc, out)
        names = [n for n in self.statuses(report) if n.startswith("cabins:")]
        self.assertIn("cabins: `cabin run -- uname -a` passes the exit code and output through", names)
        self.assertGreaterEqual(len(self.tour_checks(report)), 22)
        self.assertEqual({}, self.failed(report))

    def test_skipped_parts_drop_their_checks_and_their_cabins(self):
        rc, out, _, report = self.run_tours("--tours-skip", "isolation,restart,failure")
        self.assertEqual(0, rc, out)
        self.assertEqual({}, self.failed(report))
        checks = self.tour_checks(report)
        for needle in ("arbitrary outside host", "allowlisted egress", "placeholder token", "real key is never", "systemctl restart", "second tour", "cancel"):
            self.assertFalse([n for n in checks if needle in n and "after" not in n], (needle, list(checks)))
        cost = report["tours_cabins"]
        self.assertEqual((2, 2), (cost["count"], cost["expected"]))
        self.assertEqual(1, len(self.world()["tours"]))
        self.assert_project_empty()

    def test_the_phase_is_skipped_not_failed_when_the_cabins_options_are_missing(self):
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", "--tours")
        self.assertEqual(0, rc, out)
        st = self.statuses()
        self.assertEqual("SKIP", st["tours: phase skipped"])
        self.assertEqual("SKIP", st["cabins: phase skipped"])
        self.assertEqual([], [n for n in st if n.startswith("tours:") and n != "tours: phase skipped"])

    def test_an_older_systemd_needs_no_fdstore_signal(self):
        rc, out, _, report = self.run_tours(world={"systemd": 252})
        self.assertEqual({}, self.failed(report))
        self.assertEqual(0, rc, out)

    FAULTS = {
        "bad-unit": "systemd-analyze verify",
        "no-fdstore": "descriptor store",
        "bad-inspect-dir": "is an unprivileged system user",
        "inspect-as-root": "bundle inspection ran as",
        "no-prlimit": "bundle inspection ran as",
        "bundle-mismatch": "`tour result` for alpha",
        "listener-lost": "`tour wait` succeeds",
        "egress-open": "arbitrary outside host",
        "key-on-guest": "real key is never on the guest",
        "cancel-leaks": "`tour cancel` while hangs runs",
        "fail-not-reported": "exits nonzero is reported failed",
        "extra-cabin": "cost: the cabins",
    }

    def check_fault(self, mode):
        needle = self.FAULTS[mode]
        rc, out, _, report = self.run_tours(modes=[mode])
        failed = self.failed(report)
        self.assertNotEqual(0, rc, f"{mode}: the run passed")
        self.assertTrue([n for n in failed if needle in n], f"{mode}: expected a failed check with {needle!r}, got {list(failed)}")
        if mode == "listener-lost":
            self.assertIn("concierge_listener_lost", " ".join(failed.values()))
        if mode == "extra-cabin":
            self.assertEqual(5, report["tours_cabins"]["count"])
        # whatever the product leaks, the harness's own teardown still leaves both projects as they were
        self.assertEqual("PASS", self.statuses(report)[CABINS_FINAL], mode)
        self.assertEqual("PASS", self.statuses(report)[ROOMS_FINAL], mode)

    def test_a_stuck_boot_is_diagnosed_cancelled_and_does_not_start_a_second_tour(self):
        """Cabins that never finish booking: the failed checks say why, full artifacts are written and scrubbed, the first tour
        is cancelled and ended with its cabins deleted before anything else, and no second tour is started."""
        rc, out, _, report = self.run_tours(modes=["boot-stuck"])
        self.assertNotEqual(0, rc)
        failed = self.failed(report)
        run_check = next(n for n in failed if "systemctl restart motel-concierge" in n)
        for needle in ("timed out after 600s waiting for guest alpha", "guest alpha is 'booking'", "phase checkin",
                       "last probe: guest bootstrap running (stage tour-firewall; cloud-init error)"):
            self.assertIn(needle, failed[run_check])
        names = self.statuses(report)
        self.assertEqual("SKIP", names["tours: second tour skipped"])
        self.assertEqual("PASS", names["tours: the main tour, cancelled after a failed check, ends with its cabins deleted"])
        self.assertFalse([n for n in names if "a second tour starts" in n], "a second tour was started on cabins that cannot boot")
        self.assertEqual(2, report["tours_cabins"]["count"])
        base = os.path.join(self.out, "tours-diagnostics")
        folders = sorted(os.listdir(base))
        self.assertTrue(folders and folders[0].startswith("01-tours-systemctl-restart-motel-concierge"), folders)
        self.assertEqual(folders, [os.path.basename(d) for d in report["tours_diagnostics"]])
        first = os.path.join(base, folders[0])
        tid = next(d for d in os.listdir(first) if d.startswith("t-"))
        for name in ("index.json", "tour-ls.json", "journal.txt", "cabin-ls.json", "cabins-servers.json", "tcp22-from-the-test-server.txt",
                     f"{tid}/status.json", f"{tid}/state.txt", *[f"{tid}/logs-alpha-{s}.json" for s in ("stdout", "stderr", "runner", "boot")],
                     f"{tid}/logs-bravo-boot.json"):
            self.assertTrue(os.path.exists(os.path.join(first, name)), name)
        with open(os.path.join(first, "tcp22-from-the-test-server.txt"), encoding="utf-8") as handle:
            reach = handle.read()
        self.assertEqual(2, reach.count("TCP 22 from the test server: connected; banner: SSH-2.0-OpenSSH_9.6p1"), reach)
        with open(os.path.join(first, f"{tid}/status.json"), encoding="utf-8") as handle:
            status = handle.read()
        self.assertIn('"stage": "tour-firewall"', status)                      # unclipped: the whole document, not 400 characters
        with open(os.path.join(first, "index.json"), encoding="utf-8") as handle:
            index = json.load(handle)
        self.assertEqual([], index["errors"])
        everything = ""
        for root, _dirs, files in os.walk(base):
            for f in files:
                with open(os.path.join(root, f), encoding="utf-8") as handle:
                    everything += handle.read()
        self.assertIn("[REDACTED]", everything)
        for secret in ("sk-ant-LEAKY1234567890abcdef", "BEGIN OPENSSH PRIVATE KEY", SECRET, SECRET_CABINS):
            self.assertNotIn(secret, everything)
        # the command log, the report and the console never see a key that a diagnostic command printed either
        for name in ("log.txt", "report.json"):
            with open(os.path.join(self.out, name), encoding="utf-8") as handle:
                text = handle.read()
            for secret in ("sk-ant-LEAKY1234567890abcdef", "BEGIN OPENSSH PRIVATE KEY", "AAAAB3NzaC1"):
                self.assertNotIn(secret, text, name)
        for secret in ("sk-ant-LEAKY1234567890abcdef", "BEGIN OPENSSH PRIVATE KEY", "AAAAB3NzaC1"):
            self.assertNotIn(secret, out, "console")
        with open(os.path.join(self.out, "log.txt"), encoding="utf-8") as handle:
            self.assertIn("journalctl", handle.read())          # the command that printed it was logged
        self.assertEqual("PASS", names[CABINS_FINAL])
        self.assert_project_empty()

    def test_a_passing_tours_run_writes_no_diagnostics(self):
        rc, _out, _err, report = self.run_tours()
        self.assertEqual(0, rc)
        self.assertFalse(os.path.exists(os.path.join(self.out, "tours-diagnostics")))
        self.assertNotIn("tours_diagnostics", report)

    def test_the_tours_config_fragment_is_accepted_by_the_real_motel_config(self):
        import tomllib
        sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))
        try:
            from motel.config import Config
        except ImportError:
            self.skipTest("motel is not importable from this checkout")
        from motel_live import tours as tours_mod
        from motel_live.cabins import CABIN_CONFIG
        from motel_live.rooms import rooms_config
        text = (rooms_config() + CABIN_CONFIG.replace("{cabin_type}", "cx23").replace("{idle}", "30m")
                + tours_mod.TOURS_CONFIG.replace("{egress}", "pypi.org"))
        cfg = Config.from_dict(tomllib.loads(text))
        self.assertIsNone(cfg.cabins_problem)
        self.assertIsNone(cfg.tours_problem)
        limits = cfg.tours
        self.assertEqual((True, 2, 2, ("pypi.org",), ("anthropic",)),
                         (limits.enabled, limits.max_concurrent_guests, limits.max_guests_per_tour, limits.egress_allow, limits.bindings))
        # the tour specs the harness writes are accepted by the real spec parser
        from motel.tours import spec as spec_mod
        self.assertTrue(hasattr(spec_mod, "SHARED_KEYS"))
        doc = json.loads(tours_mod.tour_spec("x", [tours_mod.sh_guest("g", "true", secrets=["anthropic"], budget_usd=0.01, egress=["pypi.org"])],
                                             "cx23"))
        self.assertLessEqual(set(doc["defaults"]), spec_mod.SHARED_KEYS)
        self.assertLessEqual(set(doc["guests"][0]) - {"id", "argv"}, spec_mod.SHARED_KEYS | spec_mod.GUEST_ONLY_KEYS)
        self.assertLessEqual(set(doc), spec_mod.TOP_KEYS)


class OmnigentPhase(Base):
    """The omnigent phase: install, a disposable server, one managed session that makes the provider check in ONE cabin, the host
    registering, a command, keep_alive, terminate, and a teardown that leaves no server, no process and no cabin."""

    NAMES = {
        "install": "omnigent: omnigent==0.21.0 and motel (the shipped ref) install into a venv as alice; `omnigent sandbox create --help` lists motel",
        "server": "omnigent: a disposable server starts on a free loopback port (not 6767) with a throwaway HOME and OMNIGENT_HARNESS_TMP_PARENT, "
                  "no key, and reports the motel provider",
        "provision": "omnigent: a managed session makes the provider check in exactly one cabin (visible in `motel cabin ls`)",
        "register": "omnigent: the host on the cabin registers with the server through the relay",
        "run": "omnigent: a command run in the sandbox through the provider returns its output",
        "shell": "omnigent: the session's shell endpoint runs a command on the cabin host and returns its output",
        "keepalive": "omnigent: keep_alive extends the cabin's checkout time",
        "terminate": "omnigent: terminate (the server's session delete) checks the cabin out and it reaches released",
        "stopped": "omnigent: the disposable server is stopped (its recorded process group, nothing else) and no longer answers",
        "clean": "omnigent: after the phase no cabin server, firewall, address or volume remains",
        "cost": "omnigent: cost: the cabins this phase created, in billed hours",
    }

    def setUp(self):
        super().setUp()
        # Safety net: this harness must never run an `omnigent` command on the operator's machine (a live server may run there).
        self.marker = os.path.join(self.tmp.name, "omnigent-ran-locally")
        self.write_bin("omnigent", f"echo ran > {shlex.quote(self.marker)}; exit 99")
        for name, value in (("REGISTER_TIMEOUT", 60), ("PROVISION_TIMEOUT", 60), ("SHELL_TIMEOUT", 40), ("RELEASE_TIMEOUT", 40), ("HEALTH_TIMEOUT", 30)):
            patcher = mock.patch.object(omni_mod.OmnigentScenario, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    @property
    def omni_args(self):
        return ["--hcloud-cmd", "hcloud", "--skip-rooms", "--omnigent", "--cabins-hcloud-cmd", "hcloud-cabins --",
                "--cabins-token-file", self.cabins_token]

    def failed(self, report=None):
        return {c["name"]: c["detail"] for c in (report or self.report())["checks"] if c["status"] == "FAIL"}

    def run_omni(self, *extra, modes=None, clock=None):
        if modes is not None:
            self.seed(lambda w: w.update(modes=modes))
        rc, out, err = self.run_main(*self.omni_args, *extra, clock=clock)
        return rc, out, err, self.report()

    def omni_checks(self, report=None):
        return {n: s for n, s in self.statuses(report).items() if n.startswith("omnigent:")}

    def omni_world(self):
        return self.world().get("omni", {})

    def assert_clean(self, report):
        self.assertEqual("PASS", self.statuses(report)[CABINS_FINAL])
        self.assertEqual("PASS", self.statuses(report)[ROOMS_FINAL])
        self.assert_project_empty()
        self.assertFalse((self.omni_world().get("server") or {}).get("running"), "the disposable server is still running")
        self.assertFalse(os.path.exists(self.marker), "the harness ran an omnigent command on the operator's machine")

    def test_full_run_passes_with_one_cabin_and_leaves_nothing_behind(self):
        self.seed(lambda w: w.update(modes=["leak"]))
        rc, out, err, report = self.run_omni()
        self.assertEqual({}, self.failed(report))
        self.assertEqual(0, rc, out)
        checks = self.omni_checks(report)
        self.assertEqual({"PASS"}, set(checks.values()), checks)
        self.assertEqual(set(self.NAMES.values()), set(checks), "a check is missing or unexpected")
        self.assert_clean(report)
        # the cost check: exactly one cabin, billed at least an hour, not left listed
        cost = report["omnigent_cabins"]
        self.assertEqual((1, 1), (cost["count"], cost["expected"]))
        self.assertTrue(all(c["billed_hours"] >= 1 and c["deleted"] and c["lease"] for c in cost["cabins"]), cost)
        # what the server saw: the server, the config, the omnigent venv and the session
        w = self.world()
        srv, log = w["omni"]["server"], self.log_text()
        self.assertGreater(srv["port"], 1023)
        self.assertNotEqual(6767, srv["port"])
        self.assertTrue(srv["home"].endswith("/home"))
        config = w["fs"][f"{srv['root']}/config.yaml"]["content"]
        for needle in ("provider: motel", f"server_url: http://127.0.0.1:{srv['port']}", "tunnel: auto", "harness_packages: []", "ttl: 20m",
                       f"motel_binary: {srv['root']}/venv/bin/motel", 'omnigent_version: "0.21.0"'):
            self.assertIn(needle, config)
        for needle in ("OMNIGENT_HARNESS_TMP_PARENT", "env -i", "--no-open", "sqlite:///", "setsid nohup"):
            self.assertIn(needle, log)
        self.assertIn("/v1/sessions", log)
        self.assertEqual(0, len(w["omni"]["sessions"]), "the session was not deleted")
        self.assertEqual(["ag_polly"], w["omni"]["agents_used"], "the default agent is polly")
        # nothing a phase check printed or wrote carries a token, key or secret-looking assignment
        for blob in (out, err, log, json.dumps(report)):
            self.assertNotIn(SECRET, blob)
            self.assertNotIn(SECRET_CABINS, blob)
        self.assertFalse(os.path.exists(os.path.join(self.out, "omnigent-diagnostics")), "a passing run wrote diagnostics")
        self.assertNotIn("omnigent_diagnostics", report)
        # the harness only reads the cabins project; motel creates and deletes the cabins
        self.assertEqual([], [c for c in self.cabins_world()["calls"] if c[1] in ("create", "delete", "disable-protection")])

    def test_the_checks_prove_what_they_name(self):
        rc, out, _, report = self.run_omni()
        self.assertEqual(0, rc, out)
        evidence = {c["name"]: "\n".join(c["evidence"]) for c in report["checks"]}
        self.assertIn("lines naming motel", evidence[self.NAMES["install"]])
        self.assertIn("sandbox_providers ['motel']", evidence[self.NAMES["server"]])
        self.assertNotIn("KEY", evidence[self.NAMES["server"]])
        self.assertIn("agent", evidence[self.NAMES["provision"]])
        self.assertRegex(evidence[self.NAMES["provision"]], r"cabin managed-\d+-0-\d+ \(task 'managed-")
        self.assertRegex(evidence[self.NAMES["register"]], r"host host_\d+ \(managed-\d+\) is online, sandbox_provider motel")
        self.assertIn("timeline:", evidence[self.NAMES["register"]])
        self.assertIn("stdout", evidence[self.NAMES["run"]])
        self.assertRegex(evidence[self.NAMES["keepalive"]], r"checkout time \S+ -> \S+")
        self.assertIn("after the session delete", evidence[self.NAMES["terminate"]])
        # the keep-alive probe really moved the checkout time forward (the fake world measures it the way the harness does)
        self.assertIn("released", evidence[self.NAMES["terminate"]])

    def test_the_agent_can_be_chosen_and_an_unknown_one_fails_the_provision_check(self):
        rc, out, _, report = self.run_omni("--omnigent-agent", "assistant")
        self.assertEqual(0, rc, out)
        self.assertEqual(["ag_claude"], self.omni_world()["agents_used"])
        rc, out, _, report = self.run_omni("--omnigent-agent", "nonesuch")
        self.assertNotEqual(0, rc)
        self.assertIn("nonesuch", self.failed(report)[self.NAMES["provision"]])
        self.assert_clean(report)

    def test_skipped_parts_drop_their_checks(self):
        rc, out, _, report = self.run_omni("--omnigent-skip", "shell,keepalive")
        self.assertEqual(0, rc, out)
        checks = self.omni_checks(report)
        self.assertNotIn(self.NAMES["shell"], checks)
        self.assertNotIn(self.NAMES["keepalive"], checks)
        self.assertEqual({"PASS"}, set(checks.values()))
        self.assert_clean(report)

    def test_omnigent_alone_sets_cabins_up_but_skips_the_cabin_checks_and_cabins_adds_them(self):
        rc, out, _, report = self.run_omni()
        self.assertEqual(0, rc, out)
        names = [n for n in self.statuses(report) if n.startswith("cabins:")]
        self.assertEqual([CABINS_CHECK, "cabins: after the phase no cabin server, firewall, address or volume remains"], names)
        rc, out, _, report = self.run_omni("--cabins", "--cabins-skip", "idle,ttl,disk")
        self.assertEqual(0, rc, out)
        self.assertIn("cabins: `cabin run -- uname -a` passes the exit code and output through", self.statuses(report))
        self.assertEqual({}, self.failed(report))

    def test_the_phase_is_skipped_not_failed_when_the_cabins_options_are_missing(self):
        rc, out, _ = self.run_main("--hcloud-cmd", "hcloud", "--skip-rooms", "--omnigent")
        self.assertEqual(0, rc, out)
        st = self.statuses()
        self.assertEqual("SKIP", st["omnigent: phase skipped"])
        self.assertEqual("SKIP", st["cabins: phase skipped"])
        self.assertEqual([], [n for n in st if n.startswith("omnigent:") and n != "omnigent: phase skipped"])

    def test_the_phase_is_skipped_when_the_cabins_setup_did_not_pass(self):
        self.seed(lambda w: w.update(modes=["interactive-cabin-setup"]))
        rc, out, _, report = self.run_omni()
        self.assertEqual(0, rc, out)
        self.assertEqual("SKIP", self.statuses(report)["omnigent: phase skipped"])
        self.assertEqual([], [n for n in self.omni_checks(report) if n != "omnigent: phase skipped"])
        self.assert_project_empty()

    def test_a_version_that_is_not_a_release_is_a_usage_error(self):
        for bad in ("0.21.0.dev1", "latest", "0.21.0 && id", '0.21.0"'):
            rc, _out, err, = self.run_main(*self.omni_args, "--omnigent-version", bad)
            self.assertEqual(2, rc, bad)
            self.assertIn("--omnigent-version", err)
        self.assertFalse(os.path.exists(self.out), "a usage error must not create anything")
        self.assertTrue(omni_mod.valid_version("0.21.0") and omni_mod.valid_version("0.22.0rc1"))

    def test_a_pinned_version_is_installed(self):
        rc, out, _, report = self.run_omni("--omnigent-version", "0.16.0")
        self.assertEqual(0, rc, [(n, d) for n, d in self.failed(report).items()])
        self.assertEqual("0.16.0", self.omni_world()["version"])
        self.assertIn('omnigent_version: "0.16.0"', self.world()["fs"][f"{self.omni_world()['server']['root']}/config.yaml"]["content"])

    # -- failures: the check that proves it fails, the rest still cleans up
    FAULTS = {
        "omni-install-fails": "install",
        "omni-no-provider": "install",
        "omni-key-in-env": "server",
        "omni-session-rejected": "provision",
        "omni-extra-cabin": "provision",
        "omni-keepalive-noop": "keepalive",
        "omni-terminate-leaks": "terminate",
        "omni-no-runner": "shell",
        "omni-server-sticks": "stopped",
    }

    def check_fault(self, mode):
        rc, out, _, report = self.run_omni(modes=[mode])
        failed = self.failed(report)
        self.assertNotEqual(0, rc, f"{mode}: the run passed")
        self.assertIn(self.NAMES[self.FAULTS[mode]], failed, f"{mode}: expected that check to fail, got {list(failed)}")
        self.assertEqual("PASS", self.statuses(report)[CABINS_FINAL], mode)
        self.assertEqual("PASS", self.statuses(report)[ROOMS_FINAL], mode)
        self.assert_project_empty()      # whatever the product leaks, the harness's own teardown still leaves both projects as they were
        self.assertFalse(os.path.exists(self.marker), mode)
        if mode != "omni-server-sticks":
            self.assertFalse((self.omni_world().get("server") or {}).get("running"), mode)
        return report

    def test_a_fault_that_stops_the_phase_early_runs_no_later_check(self):
        report = self.check_fault("omni-install-fails")
        self.assertEqual([self.NAMES["install"]], [n for n in self.omni_checks(report) if n != self.NAMES["clean"] and n != self.NAMES["cost"]])

    def test_extra_cabins_fail_the_one_cabin_rule_and_the_cost_check(self):
        report = self.check_fault("omni-extra-cabin")
        self.assertEqual(2, report["omnigent_cabins"]["count"])
        self.assertIn(self.NAMES["cost"], self.failed(report))
        self.assertIn("expected exactly one", self.failed(report)[self.NAMES["provision"]])

    def test_a_terminate_that_leaves_the_cabin_active_fails_the_check_and_the_harness_still_checks_it_out(self):
        report = self.check_fault("omni-terminate-leaks")
        self.assertIn("active", self.failed(report)[self.NAMES["terminate"]])
        self.assertEqual({"PASS"}, {s for n, s in self.omni_checks(report).items() if n in (self.NAMES["clean"], self.NAMES["stopped"])})

    def test_a_shell_endpoint_failure_does_not_hide_keep_alive_or_terminate(self):
        report = self.check_fault("omni-no-runner")
        st = self.omni_checks(report)
        self.assertEqual("PASS", st[self.NAMES["keepalive"]])
        self.assertEqual("PASS", st[self.NAMES["terminate"]])
        self.assertIn("--omnigent-skip shell", self.failed(report)[self.NAMES["shell"]])

    def test_a_server_that_will_not_stop_fails_the_check(self):
        report = self.check_fault("omni-server-sticks")
        self.assertIn("still-running", self.failed(report)[self.NAMES["stopped"]])

    def test_host_never_registers_is_diagnosed_and_everything_is_cleaned_up(self):
        rc, out, err, report = self.run_omni(modes=["omni-host-never", "leak"])
        self.assertNotEqual(0, rc)
        failed = self.failed(report)
        self.assertEqual([self.NAMES["register"]], [n for n in failed if n.startswith("omnigent:")], failed)
        self.assertIn("timed out after 60s waiting for the omnigent host on the cabin to register", failed[self.NAMES["register"]])
        names = self.omni_checks(report)
        self.assertNotIn(self.NAMES["run"], names, "no command is run on a host that never registered")
        self.assertNotIn(self.NAMES["terminate"], names)
        self.assertEqual("PASS", names[self.NAMES["stopped"]])
        self.assertEqual("PASS", names[self.NAMES["clean"]])
        self.assertEqual("PASS", names[self.NAMES["cost"]])
        self.assert_clean(report)
        # the diagnostics: scrubbed, and they say what the server and the cabin were doing
        base = os.path.join(self.out, "omnigent-diagnostics")
        folders = sorted(os.listdir(base))
        self.assertTrue(folders and folders[0].startswith("01-omnigent-the-host-on-the-cabin-registers"), folders)
        self.assertEqual(folders, [os.path.basename(d) for d in report["omnigent_diagnostics"]])
        first = os.path.join(base, folders[0])
        files = set(os.listdir(first))
        cabin_files = {f for f in files if f.startswith("cabin-side-logs-")}
        self.assertEqual(1, len(cabin_files), files)
        for name in ("index.json", "server-and-provider-logs.txt", "api-info.json", "api-hosts.json", "api-session.json", "timeline.txt",
                     "cabin-ls.json", "cabins-servers.json"):
            self.assertIn(name, files)
        self.assertTrue([f for f in files if f.startswith("cabin-status-")])
        with open(os.path.join(first, "index.json"), encoding="utf-8") as handle:
            self.assertEqual([], json.load(handle)["errors"])
        with open(os.path.join(first, "timeline.txt"), encoding="utf-8") as handle:
            self.assertIn("session sandbox stage: starting", handle.read())
        everything = ""
        for root, _dirs, found in os.walk(base):
            for f in found:
                with open(os.path.join(root, f), encoding="utf-8") as handle:
                    everything += handle.read()
        self.assertIn("[REDACTED]", everything)
        for secret in ("launchbearer1234567", "launchsecret12345", SECRET, SECRET_CABINS):
            self.assertNotIn(secret, everything)
        self.assertIn("OMNIGENT_HOST_LAUNCH_TOKEN=[REDACTED]", everything)
        for name in ("log.txt", "report.json"):
            with open(os.path.join(self.out, name), encoding="utf-8") as handle:
                self.assertNotIn("launchsecret12345", handle.read(), name)

    def test_a_launch_that_fails_is_reported_with_the_servers_reason_and_cleaned_up(self):
        rc, out, _, report = self.run_omni(modes=["omni-launch-failed"])
        self.assertNotEqual(0, rc)
        self.assertIn("the host did not register within 120s", self.failed(report)[self.NAMES["register"]])
        self.assert_clean(report)
        self.assertTrue(os.path.isdir(os.path.join(self.out, "omnigent-diagnostics")))

    def test_a_passing_run_writes_no_diagnostics(self):
        rc, _out, _err, report = self.run_omni()
        self.assertEqual(0, rc)
        self.assertFalse(os.path.exists(os.path.join(self.out, "omnigent-diagnostics")))

    def test_an_interrupt_mid_phase_deletes_everything(self):
        """Ctrl-C while the host is still registering: the session, the server, the cabin and its firewall and address all go."""
        def boom(n):
            w = fakeworld.load()
            if w.get("omni", {}).get("sessions") and not getattr(boom, "fired", False):
                boom.fired = True
                raise KeyboardInterrupt
        rc, out, _, report = self.run_omni(clock=FakeClock(boom))
        self.assertTrue(getattr(boom, "fired", False), "the interrupt never fired")
        self.assertEqual(130, rc)
        self.assertIn("run interrupted", self.statuses(report))
        self.assertEqual("PASS", self.omni_checks(report)[self.NAMES["clean"]])
        self.assertEqual("PASS", self.omni_checks(report)[self.NAMES["stopped"]])
        self.assert_clean(report)
        self.assertEqual({}, self.omni_world()["sessions"], "the session was not deleted")
        self.assertEqual({}, self.world().get("leases", {}), "a cabin lease is still live")

    def test_sigterm_mid_phase_behaves_like_ctrl_c(self):
        def term(n):
            w = fakeworld.load()
            if w.get("omni", {}).get("sessions") and not getattr(term, "fired", False):
                term.fired = True
                os.kill(os.getpid(), signal.SIGTERM)
                time.sleep(0.2)
        rc, out, _, report = self.run_omni(clock=FakeClock(term))
        self.assertEqual(130, rc)
        self.assert_clean(report)
        self.assertEqual({}, self.world().get("leases", {}))

    def test_the_server_config_is_valid_yaml_and_accepted_by_the_providers_own_config_model(self):
        try:
            import yaml
        except ImportError:
            self.skipTest("pyyaml is not installed")
        doc = yaml.safe_load(omni_mod.config_yaml("http://127.0.0.1:41000", "/x/venv/bin/motel", "0.21.0", "cx23"))
        self.assertEqual("motel", doc["sandbox"]["provider"])
        self.assertEqual("http://127.0.0.1:41000", doc["sandbox"]["server_url"])
        self.assertEqual([], doc["sandbox"]["motel"]["harness_packages"])
        sys.path.insert(0, os.path.join(HERE, "..", "..", "src"))
        try:
            from omnigent.community.sandbox.motel.config import MotelSandboxConfig
        except ImportError:
            self.skipTest("pydantic is not importable here")
        finally:
            sys.path.pop(0)
        cfg = MotelSandboxConfig(**doc["sandbox"]["motel"])
        self.assertEqual(("20m", "auto", "0.21.0", []), (cfg.ttl, cfg.tunnel, cfg.omnigent_version, cfg.harness_packages))
        self.assertEqual("auto", MotelSandboxConfig(**{**doc["sandbox"]["motel"], "tunnel": "auto"}).tunnel)

    def test_the_scripts_are_valid_shell_and_python(self):
        from motel_live import remote
        for name in ("OMNI_PUT_SCRIPT", "OMNI_INSTALL_SCRIPT", "OMNI_HELP_SCRIPT", "OMNI_START_SCRIPT", "OMNI_ENV_NAMES_SCRIPT",
                     "OMNI_STOP_SCRIPT", "OMNI_LOGS_SCRIPT", "OMNI_CABIN_LOGS_SCRIPT", "OMNI_COUNT_SCRIPT"):
            proc = subprocess.run(["sh", "-n", "-c", getattr(remote, name)], capture_output=True, text=True)   # -n: parse only, run nothing
            self.assertEqual(0, proc.returncode, f"{name}: {proc.stderr}")
        for name in ("OMNI_API_PY", "OMNI_PROVIDER_PY", "OMNI_PORT_PY"):
            compile(getattr(remote, name), name, "exec")

    @linux_only
    def test_the_start_and_stop_scripts_stop_only_what_they_started(self):
        """Run the real start and stop scripts against a stand-in for the server that is not Omnigent: the recorded process
        group (the stand-in and its child) goes, the clean environment and the explicit port and database are used, and
        a stale pid file that names someone else's process is left alone."""
        from motel_live import remote
        root = os.path.join(self.tmp.name, "omni-root")
        os.makedirs(os.path.join(root, "venv", "bin"))
        for d in ("home", "tmp", "data"):
            os.makedirs(os.path.join(root, d))
        stand_in = os.path.join(root, "venv", "bin", "omnigent")
        with open(stand_in, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\necho \"args: $*\"\nenv | cut -d= -f1 | sort | tr '\\n' ' '\necho\nsleep 300 &\nsleep 300\n")
        os.chmod(stand_in, 0o755)
        sh = lambda script, *a: subprocess.run(["sh", "-c", script, "sh", root, *a], capture_output=True, text=True, timeout=60)  # noqa: E731
        started = sh(remote.OMNI_START_SCRIPT, "41999")
        pid = started.stdout.strip().splitlines()[-1]
        self.assertTrue(pid.isdigit(), started)
        try:
            deadline = time.time() + 10
            while time.time() < deadline and "args:" not in (open(os.path.join(root, "server.log"), encoding="utf-8").read() if os.path.exists(os.path.join(root, "server.log")) else ""):
                time.sleep(0.1)
            with open(os.path.join(root, "server.log"), encoding="utf-8") as handle:
                log = handle.read()
            self.assertIn("--host 127.0.0.1 --port 41999", log)
            self.assertIn(f"--database-uri sqlite:///{root}/data/chat.db", log)
            self.assertIn("OMNIGENT_HARNESS_TMP_PARENT", log)
            self.assertNotIn("SECRET_FROM_CALLER", log)
            names = sh(remote.OMNI_ENV_NAMES_SCRIPT).stdout.split()
            self.assertEqual({"HOME", "PATH", "LANG", "PYTHONUNBUFFERED", "OMNIGENT_HARNESS_TMP_PARENT"}, set(names))
            os.kill(int(pid), 0)                                              # alive
            stopped = sh(remote.OMNI_STOP_SCRIPT)
            self.assertEqual(f"stopped {pid}", stopped.stdout.strip(), stopped)
            self.assertFalse(os.path.exists(f"/proc/{pid}"), "the server's process group is still running")
            self.assertEqual("no-record", sh(remote.OMNI_STOP_SCRIPT).stdout.strip())
            # a recorded pid that is somebody else's process is never signalled
            bystander = subprocess.Popen(["sleep", "30"])
            try:
                with open(os.path.join(root, "server.pid"), "w", encoding="utf-8") as handle:
                    handle.write(f"{bystander.pid}\n")
                answer = sh(remote.OMNI_STOP_SCRIPT)
                self.assertIsNone(bystander.poll(), "the stop script signalled a process that is not this phase's server")
                self.assertTrue(answer.stdout.startswith(("still-running", "stopped")), answer)
            finally:
                bystander.kill()
                bystander.wait()
        finally:
            sh(remote.OMNI_STOP_SCRIPT)


def _fault_test(mode):
    def test(self):
        self.check_fault(mode)
    return test


for _mode in OmnigentPhase.FAULTS:
    setattr(OmnigentPhase, "test_fault_" + _mode.replace("-", "_"), _fault_test(_mode))


class OmnigentDryRun(Base):
    def test_dry_run_prints_every_omnigent_command_and_executes_nothing(self):
        rc, out, err = self.run_main("--dry-run", "--hcloud-cmd", "hcloud", "--omnigent", "--cabins-hcloud-cmd", "hcloud-cabins --",
                                     "--cabins-token-file", "/nonexistent/cabins-token")
        self.assertEqual(0, rc)
        self.assertFalse(os.path.exists(os.path.join(self.world_dir, "world.json")), "the fake cloud was contacted")
        self.assertFalse(os.path.exists(self.out), "dry-run must not create files")
        for needle in ("python3-venv", "omnigent==$2", "sandbox create --help", "OMNIGENT_HARNESS_TMP_PARENT", "setsid nohup", "/v1/sessions",
                       "keep_alive", "DELETE", "cabin status", "kill -TERM"):
            self.assertIn(needle, err)
        self.assertIn("omnigent: omnigent==0.21.0", out)
        self.assertIn("PLAN", out)
        self.assertNotIn("FAIL", out)


class DryRun(Base):
    def test_dry_run_executes_nothing_and_prints_every_command(self):
        rc, out, err = self.run_main("--dry-run", "--hcloud-cmd", "hcloud --wrapper-arg", "--cabins", "--cabins-hcloud-cmd",
                                     "hcloud-cabins --cabins-arg --", "--cabins-token-file", "/nonexistent/cabins-token")
        self.assertEqual(0, rc)
        self.assertFalse(os.path.exists(os.path.join(self.world_dir, "world.json")), "the fake cloud was contacted")
        self.assertFalse(os.path.exists(os.path.join(self.world_dir, "world-cabins.json")), "the fake cabins cloud was contacted")
        for needle in ("hcloud-cabins --cabins-arg -- server list", "motel-desk cabin setup hetzner --token-file /root/.motel-live-cabins-token",
                       "/nonexistent/cabins-token"):
            self.assertIn(needle, err)
        self.assertFalse(os.path.exists(self.out), "dry-run must not create files")
        for needle in ("hcloud --wrapper-arg server create", "scp ", "motel-desk setup hetzner --token-file /root/.motel-live-token",
                       "checkin", "reboot", "cabin"):
            self.assertIn(needle, err)
        self.assertIn("PLAN", out)
        self.assertNotIn("FAIL", out)


for _mode in ToursPhase.FAULTS:
    setattr(ToursPhase, "test_fault_" + _mode.replace("-", "_"), lambda self, _m=_mode: self.check_fault(_m))


class ToursDryRun(Base):
    def test_dry_run_prints_every_tours_command_and_executes_nothing(self):
        rc, out, err = self.run_main("--dry-run", "--hcloud-cmd", "hcloud", "--tours", "--cabins-hcloud-cmd", "hcloud-cabins --",
                                     "--cabins-token-file", "/nonexistent/cabins-token")
        self.assertEqual(0, rc)
        self.assertFalse(os.path.exists(os.path.join(self.world_dir, "world.json")), "the fake cloud was contacted")
        self.assertFalse(os.path.exists(os.path.join(self.world_dir, "world-cabins.json")), "the fake cabins cloud was contacted")
        self.assertFalse(os.path.exists(self.out), "dry-run must not create files")
        for needle in ("install.sh --users alice --provider hetzner --cabins --tours --upgrade", "systemd-analyze verify",
                       "motel-concierge.service", "systemd-run --unit=motel-live-sampler", "tour start", "tour wait", "tour result",
                       "tour fetch", "tour cancel", "tour logs", "systemctl restart motel-concierge.service",
                       "/var/lib/motel-inspect", "/etc/motel/llm-keys/anthropic.key"):
            self.assertIn(needle, err)
        self.assertNotIn("sk-ant-", err.replace("sk-ant-[REDACTED]", ""), "the fake key must be redacted even in a dry run")
        self.assertIn("PLAN", out)
        self.assertNotIn("FAIL", out)


class RoomsResidue(unittest.TestCase):
    """The harness's own test server owns primary IPs (IPv4 and IPv6) that Hetzner creates with it: unlabelled,
    Hetzner-named, deleted with the server. They are not cabin residue in the rooms project."""

    SERVER = "175576093"
    STATE = {"server_id": SERVER, "key_id": "9", "bystander": {"id": "107428715"},
             "server_ip_ids": ["161833968", "161833969"]}

    @staticmethod
    def ip(name_id, assignee):
        return {"name": f"primary_ip-{name_id}", "labels": {}, "assignee": assignee}

    def scenario(self, inventory, state=None, initial=None):
        ctx = SimpleNamespace(initial=initial or {}, cabins_initial={}, state=self.STATE if state is None else state,
                              hc=SimpleNamespace(inventory=lambda kinds: inventory))
        scenario = cabins_mod.CabinsScenario.__new__(cabins_mod.CabinsScenario)
        scenario.ctx, scenario.hc = ctx, ctx.hc
        return scenario

    def test_the_test_servers_own_primary_ips_are_not_residue(self):
        inv = {"primary-ip": {"161833968": self.ip(161833968, self.SERVER), "161833969": self.ip(161833969, self.SERVER)}}
        self.assertEqual({}, self.scenario(inv).rooms_new("primary-ip", inv))

    def test_the_assignment_alone_identifies_them_when_the_ids_were_not_recorded(self):
        inv = {"primary-ip": {"161833968": self.ip(161833968, self.SERVER)}}
        state = {k: v for k, v in self.STATE.items() if k != "server_ip_ids"}
        self.assertEqual({}, self.scenario(inv, state).rooms_new("primary-ip", inv))

    def test_the_recorded_ids_alone_identify_them_when_the_assignment_is_not_reported(self):
        inv = {"primary-ip": {"161833968": {"name": "primary_ip-161833968", "labels": {}}}}
        self.assertEqual({}, self.scenario(inv).rooms_new("primary-ip", inv))

    def test_an_address_that_is_not_the_test_servers_is_still_reported(self):
        inv = {"primary-ip": {"161833968": self.ip(161833968, self.SERVER),
                              "5": self.ip(5, "999"),                     # a cabin's address in the wrong project
                              "6": self.ip(6, None)}}                     # an orphaned one
        self.assertEqual({"5", "6"}, set(self.scenario(inv).rooms_new("primary-ip", inv)))

    def test_other_kinds_and_initial_resources_are_unchanged(self):
        inv = {"firewall": {"7": {"name": "motel-x", "labels": {"role": "cabin"}, "assignee": None},
                            "8": {"name": "was-here", "labels": {}, "assignee": None}},
               "server": {self.SERVER: {"name": "own", "labels": {}, "assignee": None}}}
        scenario = self.scenario(inv, initial={"firewall": {"8": {}}})
        self.assertEqual({"7"}, set(scenario.rooms_new("firewall", inv)))
        self.assertEqual({}, scenario.rooms_new("server", inv))

    def test_ids_are_read_from_the_server_description(self):
        doc = {"public_net": {"ipv4": {"id": 161833968, "ip": "198.51.100.1"}, "ipv6": {"id": 161833969, "ip": "x"}}}
        self.assertEqual(["161833968", "161833969"], server_ip_ids(doc))
        self.assertEqual(["1"], server_ip_ids({"public_net": {"ipv4": {"id": 1}, "ipv6": {"id": 0}}}))
        self.assertEqual([], server_ip_ids({"public_net": {"ipv4": {"ip": "198.51.100.1"}}}))
        self.assertEqual([], server_ip_ids({}))


class Units(unittest.TestCase):
    def test_redactor(self):
        r = core.Redactor()
        r.add(SECRET)
        self.assertEqual(f"a {core.REDACTED} b", r(f"a {SECRET} b"))
        self.assertEqual(f"Authorization: Bearer {core.REDACTED}", r("Authorization: Bearer abcdef"))
        self.assertEqual("short", r("short"))

    def test_hcloud_wrapper_is_split_not_shell_evaluated(self):
        self.assertEqual(["hcloud-wrap", "motel-test", "--access", "write", "--"], run_live.split("x", "hcloud-wrap motel-test --access write --"))
        self.assertEqual(["a b", "c"], run_live.split("x", "'a b' c"))
        r = core.Runner(dry_run=True, redact=core.Redactor())
        Hcloud(r, ["w", ";", "rm"], dry=True).run("server", "list")
        self.assertEqual([["w", ";", "rm", "server", "list"]], r.history)

    def test_foreign_and_diff(self):
        inv = {"server": {"1": {"name": "a", "labels": {"motel-live": "r"}}, "2": {"name": "b", "labels": {}}},
               "volume": {"3": {"name": "c", "labels": {"motel-live": "other"}}}}
        self.assertEqual(["server 2 (b)", "volume 3 (c)"], Hcloud.foreign(inv, "r"))
        d = Hcloud.diff({"server": {"2": {"name": "b", "labels": {}}}}, {"server": {"2": {"name": "b", "labels": {}}, "5": {"name": "z", "labels": {}}}})
        self.assertEqual(["server 5 (z)"], d["added"])
        inv["firewall"] = {"9": {"name": "f", "labels": {}}}
        self.assertEqual(["server 2 (b)", "volume 3 (c)"], Hcloud.foreign(inv, "r"), "the rooms preflight only looks at servers and volumes")
        self.assertEqual(["server 2 (b)", "volume 3 (c)", "firewall 9 (f)"],
                         Hcloud.foreign(inv, "r", kinds=("server", "volume", "firewall")))

    def test_check_blocks_record_pass_fail_skip(self):
        rep = core.Report("rid-0001", core.Redactor())
        with rep.check("ok"):
            pass
        with rep.check("bad") as c:
            c.require(False, "nope")
        with rep.check("skipped") as c:
            c.skip("why")
        with rep.check("boom"):
            raise RuntimeError("x")
        self.assertEqual(["PASS", "FAIL", "SKIP", "FAIL"], [c["status"] for c in rep.checks])
        with self.assertRaises(KeyboardInterrupt):
            with rep.check("ctrl-c"):
                raise KeyboardInterrupt
        self.assertEqual("FAIL", rep.checks[-1]["status"])

    def test_fail_hooks_run_for_findings_but_not_for_an_abort_or_an_interrupt(self):
        rep = core.Report("rid-0002", core.Redactor())
        seen = []
        rep.fail_hooks.append(seen.append)
        with rep.check("assertion") as c:
            c.require(False, "nope")
        with rep.check("error"):
            raise RuntimeError("x")
        self.assertEqual(["assertion", "error"], seen)
        with rep.check("aborted") as _:
            pass
        with self.assertRaises(core.Abort):
            with rep.check("abort"):
                raise core.Abort("stop the run")
        with self.assertRaises(KeyboardInterrupt):
            with rep.check("interrupt"):
                raise KeyboardInterrupt
        self.assertEqual(["FAIL", "FAIL"], [c["status"] for c in rep.checks[-2:]])
        self.assertEqual(["assertion", "error"], seen, "an Abort or an interrupt ends the run; no evidence is collected for it")


class FakeWorldFile(unittest.TestCase):
    def test_concurrent_saves_never_leave_a_torn_world_file(self):
        """Fake commands run as separate processes and save the world; a shared temporary name let two writers interleave."""
        import threading
        with tempfile.TemporaryDirectory() as tmp:
            old = os.environ.get("FAKE_WORLD")
            os.environ["FAKE_WORLD"] = tmp
            self.addCleanup(lambda: os.environ.__setitem__("FAKE_WORLD", old) if old is not None else os.environ.pop("FAKE_WORLD", None))
            fakeworld.save({"next_id": 1, "pad": "x" * 200000})
            errors = []

            def write(n):
                for i in range(40):
                    fakeworld.save({"next_id": n * 1000 + i, "pad": ("y" * (100000 + 7000 * n))})

            def read():
                for _ in range(200):
                    try:
                        json.loads(open(fakeworld.world_path(), encoding="utf-8").read())
                    except Exception as exc:  # noqa: BLE001
                        errors.append(repr(exc))
            threads = [threading.Thread(target=write, args=(n,)) for n in range(6)] + [threading.Thread(target=read) for _ in range(2)]
            [t.start() for t in threads]
            [t.join() for t in threads]
            self.assertEqual([], errors)
            self.assertEqual(["world.json"], os.listdir(tmp), "no temporary file is left behind")


class IdleReleaseBounds(unittest.TestCase):
    """The window maths the idle and floor checks expect, against the product's own arithmetic (motel.billing)."""

    def test_a_cabin_idle_at_30_minutes_waits_for_the_window_of_its_first_hour(self):
        # the window opens at minute 44; the product also releases from 43:00 when the next pass would be too late
        self.assertEqual((43 * 60, 3600), cabins_mod.idle_release_bounds(30 * 60))

    def test_a_cabin_idle_inside_the_window_or_its_margin_is_deleted_at_once(self):
        self.assertEqual((50 * 60, 3600), cabins_mod.idle_release_bounds(50 * 60))
        self.assertEqual((58 * 60, 3600), cabins_mod.idle_release_bounds(58 * 60))

    def test_a_cabin_idle_after_the_hour_turns_waits_for_the_next_window(self):
        self.assertEqual((103 * 60, 7200), cabins_mod.idle_release_bounds(61 * 60))
        self.assertEqual((163 * 60, 10800), cabins_mod.idle_release_bounds(2 * 3600 + 5))

    def test_the_bounds_never_start_before_the_idle_time(self):
        for idle in range(1800, 5 * 3600, 137):
            lo, hi = cabins_mod.idle_release_bounds(idle)
            self.assertGreaterEqual(lo, idle)
            self.assertLessEqual(lo, hi)
            self.assertGreater(hi, idle)

    def test_a_reaper_run_every_60_seconds_releases_inside_the_bounds_whatever_the_phase(self):
        """The harness reaps every minute, the product's `next_pass_too_late` rule then releases from minute 43, not 44. The
        bounds must hold the product's real first release, for every cadence phase and margin the config allows."""
        from datetime import UTC, datetime, timedelta

        from motel import billing
        t0 = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
        for margin_min in (4, 6, 10, 30):
            for eligible in (1800, 2400, 2580, 2700, 3300, 3590, 3600, 3700, 5400):
                lo, hi = core.release_bounds(eligible, margin_min * 60)
                for phase in range(0, 60, 7):
                    t = eligible + phase
                    while not billing.release_window(t0, t0 + timedelta(seconds=t), margin=timedelta(minutes=margin_min)).release:
                        t += 60
                    self.assertGreaterEqual(t, lo, (margin_min, eligible, phase, t, lo))          # not early
                    self.assertGreaterEqual(t, eligible)
                    self.assertLessEqual(t, hi, (margin_min, eligible, phase, t, hi))             # before the hour ends
                if eligible < 2400 and margin_min == 6:
                    self.assertEqual(2580, lo)              # a check that waited for minute 44 would reject a release at 43:00

    def test_the_fake_world_decides_exactly_as_the_product_does(self):
        from datetime import UTC, datetime, timedelta
        from types import SimpleNamespace

        from motel import billing
        t0 = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
        for margin_min in (4, 6, 30):
            toy = SimpleNamespace(minutes=lambda key, default, m=margin_min: timedelta(minutes=m))
            for seconds in range(0, 3 * 3600, 30):
                now = t0 + timedelta(seconds=seconds)
                want = billing.release_window(t0, now, margin=timedelta(minutes=margin_min)).release
                got = fakeworld.Toy.release_due(toy, t0.strftime("%Y-%m-%dT%H:%M:%SZ"), now)
                self.assertEqual(want, got, (margin_min, seconds))

    def test_parse_minutes(self):
        self.assertEqual(30, cabins_mod.parse_minutes("30m"))
        self.assertEqual(120, cabins_mod.parse_minutes("2h"))
        with self.assertRaises(AssertionError):
            cabins_mod.parse_minutes("soon")

    def test_parse_epoch_reads_what_hcloud_and_the_fake_print(self):
        self.assertEqual(1794139200.0, rooms_mod.parse_epoch("2026-11-08T12:00:00Z"))
        self.assertEqual(1794139200.0, rooms_mod.parse_epoch("2026-11-08T12:00:00+00:00"))
        self.assertIsNone(rooms_mod.parse_epoch(None))


class SourceLocationTest(unittest.TestCase):
    """The installer refuses a source that anybody but root can change; the harness must hand it one it accepts."""

    REPO = os.path.dirname(os.path.dirname(HERE))

    def unpack(self, parent, *, umask="0022", chmod=True):
        from motel_live import session

        tarball = os.path.join(parent, "motel-src.tar.gz")
        archive = session.archive_argv(self.REPO, "HEAD", tarball)
        archive[2] = f"tar.umask={umask}"
        proc = subprocess.run(archive, capture_output=True, text=True)
        if proc.returncode != 0:
            self.skipTest(f"no git checkout to archive: {proc.stderr.strip()[:100]}")
        for argv in session.unpack_argvs(tarball, parent):
            if argv[0] == "chmod" and not chmod:
                continue
            if argv[0] == "tar" and not chmod:
                argv = [*argv, "-p"]  # root keeps the archived modes; a user's umask would hide the problem
            subprocess.run(argv, check=True, capture_output=True)
        return os.path.join(parent, "motel-src")

    def test_the_extract_location_is_root_only_and_not_a_shared_temp_directory(self):
        from motel_live import session

        self.assertTrue(session.REMOTE_SRC.startswith("/root/"), session.REMOTE_SRC)
        self.assertTrue(session.REMOTE_TARBALL.startswith("/root/"), session.REMOTE_TARBALL)
        self.assertIn(["chmod", "0700", session.REMOTE_SRC], session.unpack_argvs())

    def test_the_real_source_audit_accepts_what_the_harness_unpacks(self):
        from motel.installer import audit_source

        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as td:
            parent = os.path.join(td, "root")
            os.mkdir(parent, 0o700)
            src = self.unpack(parent)
            self.assertEqual(0o700, stat.S_IMODE(os.stat(src).st_mode))
            self.assertEqual([], audit_source(os.path.join(src, "src", "motel"), owner_uid=os.getuid(), chain=False))
            self.assertTrue(os.path.exists(os.path.join(src, "install.sh")))
            self.assertFalse(os.path.exists(os.path.join(parent, "motel-src.tar.gz")), "the tarball is removed")

    def test_git_default_umask_would_have_been_refused(self):
        # The reason for `tar.umask=0022` and the chmod: git's own default writes 0775 directories, which the audit rejects.
        from motel.installer import audit_source

        with tempfile.TemporaryDirectory(dir=os.environ.get("TMPDIR")) as td:
            parent = os.path.join(td, "root")
            os.mkdir(parent, 0o700)
            src = self.unpack(parent, umask="0002", chmod=False)
            problems = audit_source(os.path.join(src, "src", "motel"), owner_uid=os.getuid(), chain=False)
            self.assertTrue(any("writable by group or others" in p for p in problems), problems[:3])


class WorldLockTest(unittest.TestCase):
    """The fake world is one file that every fake command and the fake clock read, change and write back. Two of those that overlap
    used to drop each other's update (the holder's pid file vanished, so the holder was never seen); they now take turns."""

    def test_a_fake_command_waits_while_someone_else_holds_the_world(self):
        with tempfile.TemporaryDirectory() as world, mock.patch.dict(os.environ, {"FAKE_WORLD": world}):
            key = os.path.join(world, "key")
            fake = os.path.join(HERE, "fakes", "fakeworld.py")
            with fakeworld.world_lock():
                with fakeworld.world_lock():   # re-entrant: the fake clock takes it inside a seed
                    pass
                proc = subprocess.Popen([sys.executable, fake, "ssh-keygen", "-f", key], env=dict(os.environ),
                                        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                try:
                    with self.assertRaises(subprocess.TimeoutExpired):
                        proc.wait(timeout=1.5)
                    self.assertFalse(os.path.exists(key), "the command ran while the world was held")
                except BaseException:
                    proc.kill()
                    raise
            self.assertEqual(0, proc.wait(timeout=30))
            self.assertTrue(os.path.exists(key))

    def test_a_waiting_command_lets_go_so_another_can_run(self):
        with tempfile.TemporaryDirectory() as world, mock.patch.dict(os.environ, {"FAKE_WORLD": world}):
            fake = os.path.join(HERE, "fakes", "fakeworld.py")
            with fakeworld.world_lock(), fakeworld.world_unlocked():
                key = os.path.join(world, "key")
                done = subprocess.run([sys.executable, fake, "ssh-keygen", "-f", key], env=dict(os.environ), timeout=60,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
                self.assertEqual(0, done.returncode)
            self.assertEqual(0, fakeworld._LOCK["depth"])


if __name__ == "__main__":
    unittest.main()
