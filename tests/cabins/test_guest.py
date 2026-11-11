"""Guest agent: admission gate, activity, seal, expiry timer. Runs the real code against a fixture directory."""
import base64
import json
import os

import pytest
from cabin_fakes import Clock, RealGuestChannel, World

from motel.cabins.errors import GuestUnknown


@pytest.fixture
def ch(tmp_path):
    return RealGuestChannel(str(tmp_path), Clock())


def sealed(ch):
    return ch.g.sealed() is not None


def test_idle_cabin_seals_after_30_clean_minutes(ch):
    ch.idle_for(31)
    doc = ch.g.seal()
    assert doc["sealed"] is True and doc["verified_idle_seconds"] >= 1800
    assert sealed(ch)


def test_seal_refused_before_30_minutes(ch):
    ch.idle_for(20)
    doc = ch.g.seal()
    assert doc["sealed"] is False and "shorter than 30 min" in doc["reason"]
    assert not sealed(ch)


def test_lease_file_can_raise_but_not_lower_the_idle_threshold(ch):
    with open(ch.lease_file, "w") as handle:
        json.dump({"idle_s": 60, "expires": "2030-01-01T00:00:00Z"}, handle)
    assert ch.g.idle_threshold() == 1800
    with open(ch.lease_file, "w") as handle:
        json.dump({"idle_s": 3600}, handle)
    assert ch.g.idle_threshold() == 3600
    ch.idle_for(40)
    assert ch.g.seal()["sealed"] is False


def test_open_session_blocks_idle_and_seal(ch):
    ch.idle_for(31)
    status, pid = ch.connect()
    assert status == 0
    doc = ch.g.seal()
    assert doc["sealed"] is False and "busy" in doc["reason"]
    ch.end_proc(pid)
    ch.g.sample()
    assert ch.g.activity()["idle_seconds"] == 0.0           # an admission restarts the interval


def test_session_admitted_after_seal_is_refused(ch):
    ch.idle_for(31)
    assert ch.g.seal()["sealed"]
    status, pid = ch.connect()
    assert status == 75 and pid is None


def test_background_process_of_the_cabin_login_counts_as_activity(ch):
    ch.idle_for(5)
    ch.add_proc(9100)                 # e.g. nohup'd build that outlived its session
    ch.g.sample()
    doc = ch.g.activity()
    assert doc["active"] is True and "ssh_session_or_process" in doc["reasons"]
    assert ch.g.seal()["sealed"] is False


def test_containers_count_as_activity(ch):
    ch.containers = 2
    ch.idle_for(31)
    assert ch.g.activity()["active"] is True
    assert ch.g.seal()["sealed"] is False


def test_unreadable_docker_is_unknown_not_idle(ch):
    def broken():
        raise OSError("docker down")
    ch.g.docker = broken
    ch.idle_for(31)
    doc = ch.g.activity()
    assert doc["unknown"] and ch.g.seal()["sealed"] is False


def test_sampling_gap_restarts_the_idle_interval(ch):
    ch.idle_for(20)
    ch.clock.advance(minutes=10)                 # sampler dead for 10 minutes
    ch.g.sample()
    assert ch.g.activity()["idle_seconds"] < 60


def test_stale_sample_cannot_seal(ch):
    ch.idle_for(31)
    ch.clock.advance(minutes=5)
    doc = ch.g.seal()
    assert doc["sealed"] is False and "stale" in doc["reason"]


def test_session_admitted_and_gone_between_samples_still_blocks_the_seal(ch):
    ch.idle_for(31)
    _status, pid = ch.connect()          # admitted after the last sample ...
    ch.end_proc(pid)                     # ... and already finished again
    doc = ch.g.seal()
    assert doc["sealed"] is False and "admitted during the idle interval" in doc["reason"]


def test_run_refused_after_expiry_or_seal(ch, tmp_path):
    payload = base64.b64encode(json.dumps(["true"]).encode()).decode()

    class Child:
        pid = 1

        def wait(self):
            return 0
    with open(ch.lease_file, "w") as handle:
        json.dump({"expires": "2020-01-01T00:00:00Z"}, handle)
    ch.clock.now = ch.clock.now.replace(year=2026)
    assert ch.g.run(payload, popen=lambda *a, **k: Child()) == 75
    with open(ch.lease_file, "w") as handle:
        json.dump({"expires": "2099-01-01T00:00:00Z"}, handle)
    assert ch.g.run(payload, popen=lambda *a, **k: Child()) == 0


def test_run_rejects_bad_payloads_and_paths(ch):
    def enc(obj):
        return base64.b64encode(json.dumps(obj).encode()).decode()
    assert ch.g.run("not base64!!") == 64
    assert ch.g.run(enc("string")) == 64
    assert ch.g.run(enc([])) == 64
    assert ch.g.run(enc({"argv": ["ls"], "cwd": "/etc"})) == 64
    assert ch.g.run(enc({"argv": ["ls"], "cwd": "../x"})) == 64


def test_run_passes_the_exit_status_and_cwd(ch):
    calls = []

    class Child:
        pid = 4242

        def wait(self):
            return 3

    def popen(argv, **kw):
        calls.append((argv, kw))
        return Child()
    payload = base64.b64encode(json.dumps({"argv": ["make", "test"], "cwd": "work"}).encode()).decode()
    assert ch.g.run(payload, popen=popen) == 3
    assert calls[0][0] == ["make", "test"] and calls[0][1]["cwd"].endswith("/work")


# ---- dead-man's switch

def test_expiry_timer_powers_off_only_after_expiry_plus_grace_plus_margin(ch):
    with open(ch.lease_file, "w") as handle:
        json.dump({"expires": "2026-11-08T14:00:00Z", "grace_s": 600}, handle)
    ch.clock.now = ch.clock.now.replace(hour=14, minute=0)
    assert ch.g.expire_check()["powered_off"] is False
    ch.clock.advance(minutes=12)
    assert ch.g.expire_check()["powered_off"] is False          # still inside grace + 120 s margin
    ch.clock.advance(minutes=1)
    assert ch.g.expire_check()["powered_off"] is True and ch.poweroffs == [True]


def test_set_expiry_extends_the_guest_deadline(ch):
    with open(ch.lease_file, "w") as handle:
        json.dump({"expires": "2026-11-08T14:00:00Z", "grace_s": 600}, handle)
    ch.g.set_expiry("20261108T200000Z")
    ch.clock.now = ch.clock.now.replace(hour=15)
    assert ch.g.expire_check()["powered_off"] is False


def test_unknown_expiry_never_powers_off(ch):
    os.unlink(ch.lease_file)
    ch.clock.advance(days=30)
    assert ch.g.expire_check()["powered_off"] is False


def test_ctl_only_allows_fixed_commands(ch):
    for bad in ("", "bash", "set-expiry", "set-expiry abc", "activity; id", "seal now"):
        with pytest.raises(SystemExit):
            ch.g.ctl(bad)


def test_ready_reports_state_without_trusting_cached_flags(ch):
    ch.g.security_probe = lambda: {"nft": "loaded", "forwarding_disabled": True, "gate": "enforced"}
    with open(os.path.join(ch.root, "ready.json"), "w") as handle:
        json.dump({"bootstrap": "done", "disk": "mounted", "nft": "loaded", "gate": "enforced"}, handle)
    doc = ch.g.ready()
    assert doc["bootstrap"] == "done" and doc["disk"] == "mounted" and doc["gate"] == "enforced"
    ch.g.security_probe = dict
    assert ch.g.ready()["gate"] == "missing" and ch.g.ready()["nft"] == "missing"


# ---- end to end: controller reaping against the real guest agent

def test_controller_reap_end_to_end_with_real_guest(tmp_path):
    w = World(str(tmp_path))
    ch = RealGuestChannel(str(tmp_path / "g"), w.clock)
    w.guest.call = lambda rec, *words, timeout=60: (w.guest.__class__.call(w.guest, rec, *words)
                                                    if words == ("ready",) else ch.call(rec, *words))
    c = w.controller()
    view = w.checkin(c)
    ch.idle_for(46)                                   # idle long enough; 46 minutes in is inside the release window
    result = c.reap(apply=True)
    assert result["actions"][0]["reason"] == "idle" and result["actions"][0]["state"] == "released"
    assert view["lease_id"]


def test_controller_does_not_reap_a_cabin_with_an_open_session(tmp_path):
    w = World(str(tmp_path))
    ch = RealGuestChannel(str(tmp_path / "g"), w.clock)
    w.guest.call = lambda rec, *words, timeout=60: (w.guest.__class__.call(w.guest, rec, *words)
                                                    if words == ("ready",) else ch.call(rec, *words))
    c = w.controller()
    w.checkin(c)
    ch.idle_for(31)
    _status, _pid = ch.connect()
    ch.g.sample()
    assert c.reap(apply=True)["actions"] == []
    assert w.hz.servers


def test_guest_unknown_wraps_systemexit(ch):
    with pytest.raises(GuestUnknown):
        ch.call({"lease_id": "x"}, "bogus")
