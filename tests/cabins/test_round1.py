"""Review round 1: hostile guests, bounded guest I/O, delete-path identity, cleanup without config, and lows."""
import json
import os
import subprocess
import sys
from datetime import timedelta

import pytest
from cabin_fakes import HOST, World, activity, make_config, pubkey

from motel.cabins import cli, desk, guestchan, userdata
from motel.cabins import config as cfgmod
from motel.cabins.errors import (
    Fail,
    Guarded,
    GuardRefused,
    GuestUnknown,
    PolicyRefused,
    Usage,
)
from motel.cabins.guestchan import (
    GuestChannel,
    classify_activity,
    validate_ready,
    validate_seal,
)
from motel.cabins.store import Store

LABELS = {"managed-by": "motel", "role": "cabin", "controller": str(HOST)}


@pytest.fixture
def world(tmp_path):
    return World(str(tmp_path))


# ================================================================ H1: hostile guest replies

HOSTILE_ACTIVITY = [
    {"schema": "motel-guest/v1", "sample_age_s": 1, "active": True, "reasons": 5},
    {"sample_age_s": 1, "active": True, "reasons": [1, 2]},
    {"sample_age_s": 1, "active": False, "reasons": "x" * 100000, "unknown": []},
    {"sample_age_s": 1, "active": False, "reasons": [], "unknown": 7, "idle_seconds": 99999},
    {"sample_age_s": "1", "active": False, "reasons": [], "unknown": [], "idle_seconds": 99999},
    {"sample_age_s": True, "active": False, "reasons": [], "unknown": [], "idle_seconds": 99999},
    {"sample_age_s": float("nan"), "active": False, "reasons": [], "unknown": [], "idle_seconds": 99999},
    {"sample_age_s": 1, "active": "yes", "reasons": [], "unknown": [], "idle_seconds": 99999},
    {"sample_age_s": 1, "active": False, "reasons": [], "unknown": [], "idle_seconds": "99999"},
    {"sample_age_s": 1, "active": False, "reasons": [], "unknown": [], "idle_seconds": float("inf")},
    {"sample_age_s": 1, "active": False, "reasons": [], "unknown": [], "idle_seconds": 99999,
     "sealed_at": {"a": 1}},
    {"sample_age_s": 1, "active": True, "reasons": [{"a": [[[[]]]]}]},
    {"sample_age_s": 1, "active": True, "reasons": ["Bad Reason With Spaces"]},
    {"sample_age_s": 1, "active": True, "reasons": ["a"] * 1000},
    [], "text", 5, None,
]


@pytest.mark.parametrize("doc", HOSTILE_ACTIVITY)
def test_malformed_activity_is_unknown_never_idle_and_never_raises(doc):
    verdict, _detail = classify_activity(doc, 1800)
    assert verdict == "unknown"


def test_wellformed_activity_still_classifies():
    assert classify_activity(activity(idle_s=2000), 1800)[0] == "idle"
    assert classify_activity(activity(active=True), 1800)[0] == "active"


@pytest.mark.parametrize("doc", [None, [], {"bootstrap": 5}, {"bootstrap": "x" * 500},
                                  {"bootstrap": "done", "tcp_listeners": 5, "nft": "loaded", "gate": "enforced",
                                   "forwarding_disabled": True},
                                  {"bootstrap": "done", "tcp_listeners": ["x"], "nft": "loaded", "gate": "enforced",
                                   "forwarding_disabled": True},
                                  {"bootstrap": "done", "tcp_listeners": [{"addr": 5, "port": 22}], "nft": "loaded",
                                   "gate": "enforced", "forwarding_disabled": True},
                                  {"bootstrap": "done", "tcp_listeners": [{"addr": "0.0.0.0", "port": "22"}],
                                   "nft": "loaded", "gate": "enforced", "forwarding_disabled": True},
                                  {"bootstrap": "done", "tcp_listeners": [], "nft": ["x"], "gate": "enforced",
                                   "forwarding_disabled": True},
                                  {"bootstrap": "done", "tcp_listeners": [], "nft": "loaded", "gate": "enforced",
                                   "forwarding_disabled": "yes"},
                                  {"bootstrap": "done", "tcp_listeners": [], "nft": "loaded", "gate": "enforced",
                                   "forwarding_disabled": True, "disk": {"x": 1}},
                                  {"bootstrap": "done", "tcp_listeners": [{"addr": "a", "port": 1}] * 1000,
                                   "nft": "loaded", "gate": "enforced", "forwarding_disabled": True}])
def test_malformed_readiness_replies_are_rejected(doc):
    with pytest.raises(GuestUnknown):
        validate_ready(doc)


def test_seal_validation_defaults_to_not_sealed():
    for doc in (None, [], {"sealed": "true"}, {"sealed": 1, "verified_idle_seconds": 5000},
                {"sealed": True, "verified_idle_seconds": "5000"}, {"sealed": True}):
        sealed, verified, _reason = validate_seal(doc)
        assert not (sealed and verified is not None)
    assert validate_seal({"sealed": True, "verified_idle_seconds": 2000.0})[:2] == (True, 2000.0)
    assert len(validate_seal({"sealed": False, "reason": "x" * 5000})[2]) <= 200


def test_hostile_guest_cannot_stop_the_reaper_for_other_leases(tmp_path):
    """Reviewer repro: reasons=<int> aborted reap before a later, expired lease was deleted."""
    w = World(str(tmp_path))
    a = w.checkin(task="aaa")
    w.clock.advance(minutes=1)
    b = w.checkin(task="zzz", controller=w.controller(uid=1001))
    victim = w.store.load(b["lease_id"])
    victim["expires_at"] = "2026-11-08T11:00:00+00:00"
    w.store.save(victim)
    orig = w.guest.call

    def hostile(rec, *words, timeout=60):
        if rec["lease_id"] == a["lease_id"] and words == ("activity",):
            return {"schema": "motel-guest/v1", "sample_age_s": 1, "active": True, "reasons": 5}
        return orig(rec, *words, timeout=timeout)
    w.guest.call = hostile
    result = w.controller(uid=0).reap(apply=True)
    assert w.store.load(b["lease_id"])["state"] == "released" and b["server_id"] not in w.hz.servers
    assert a["server_id"] in w.hz.servers                      # unknown activity is never idle
    assert result["errors"] == []


def test_unexpected_exception_for_one_lease_is_recorded_and_the_pass_continues(tmp_path):
    w = World(str(tmp_path))
    a = w.checkin(task="aaa")
    b = w.checkin(task="zzz", controller=w.controller(uid=1001))
    for lid in (a["lease_id"], b["lease_id"]):
        rec = w.store.load(lid)
        rec["expires_at"] = "2026-11-08T11:00:00+00:00"
        w.store.save(rec)
    c = w.controller(uid=0)
    real = c._cleanup
    boom = {"n": 0}

    def flaky(rec, reason, *budgets):
        boom["n"] += 1
        if rec["lease_id"] == a["lease_id"]:
            raise RuntimeError("unexpected")
        return real(rec, reason, *budgets)
    c._cleanup = flaky
    result = c.reap(apply=True)
    assert [e["lease"] for e in result["errors"]] == [a["lease_id"]]
    assert w.store.load(b["lease_id"])["state"] == "released"


def test_hostile_ready_reply_fails_bootstrap_cleanly(world):
    world.guest.ready_doc = {"schema": "motel-guest/v1", "bootstrap": "done", "tcp_listeners": [None],
                             "nft": "loaded", "gate": "enforced", "forwarding_disabled": True}
    with pytest.raises(Fail, match="bootstrap deadline"):
        world.checkin()
    assert not world.hz.servers and not world.hz.firewalls


def test_hostile_seal_reply_does_not_crash_or_delete(world):
    c = world.controller()
    world.checkin(c)
    world.guest.activity_docs = [activity(idle_s=5000)]
    world.guest.seal_docs = [{"schema": "motel-guest/v1", "sealed": True, "verified_idle_seconds": {"x": 1}}]
    assert c.reap(apply=True)["actions"] == [] and world.hz.servers


def channel(reply, **kw):
    seen = []

    def runner(argv, timeout):
        seen.append(argv)
        if isinstance(reply, Exception):
            raise reply
        return 0, reply
    ch = GuestChannel("/nonexistent", runner=runner, **kw)
    ch.seen = seen
    return ch


REC = {"lease_id": "build-0123456789ab", "name": "motel-build-0123456789ab",
       "access": {"mode": "ssh-allowlist", "address": "203.0.113.7"}}


@pytest.mark.parametrize("reply", [b"[" * 60000 + b"]" * 60000, b"{\"a\":" * 30000 + b"1" + b"}" * 30000,
                                    b"\xff\xfe\x00", b"not json", b"[]", b"5", b'{"schema": "wrong"}',
                                    b"", b"x" * (guestchan.MAX_REPLY + 1)])
def test_hostile_wire_replies_become_guest_unknown(reply):
    with pytest.raises(GuestUnknown):
        channel(reply).call(REC, "activity")


def test_runner_errors_become_guest_unknown():
    for exc in (OSError("x"), subprocess.TimeoutExpired("ssh", 5), subprocess.SubprocessError("y")):
        with pytest.raises(GuestUnknown):
            channel(exc).call(REC, "activity")


# ================================================================ M1: bounded guest I/O, unprivileged user

def test_default_runner_kills_output_over_the_cap():
    argv = [sys.executable, "-c", "import sys,time\nwhile True: sys.stdout.write('x'*65536); sys.stdout.flush()"]
    with pytest.raises(GuestUnknown, match="too large"):
        guestchan.default_runner(argv, 20, cap=100_000)


def test_default_runner_times_out_and_kills(tmp_path):
    marker = tmp_path / "alive"
    argv = [sys.executable, "-c", f"import time,pathlib\ntime.sleep(5)\npathlib.Path({str(marker)!r}).write_text('x')"]
    with pytest.raises(subprocess.TimeoutExpired):
        guestchan.default_runner(argv, 0.5)
    import time
    time.sleep(0.2)
    assert not marker.exists()


def test_default_runner_returns_small_output_and_ignores_stderr():
    argv = [sys.executable, "-c", "import sys\nsys.stderr.write('e'*1000000)\nprint('{\"ok\": 1}')"]
    rc, out = guestchan.default_runner(argv, 20)
    assert rc == 0 and out.strip() == b'{"ok": 1}'


def test_guest_ssh_runs_as_the_unprivileged_user_via_setpriv():
    ch = channel(b'{"schema": "motel-guest/v1"}', run_as=("motel-cabin", 991, 992),
                 exists=lambda p: p == "/usr/bin/setpriv")
    ch.call(REC, "activity")
    argv = ch.seen[0]
    assert argv[:9] == ["/usr/bin/setpriv", "--reuid", "991", "--regid", "992", "--clear-groups",
                        "--no-new-privs", "--", "/usr/bin/ssh"]


def test_runuser_is_the_fallback_and_nothing_means_refusal():
    ch = channel(b'{"schema": "motel-guest/v1"}', run_as=("motel-cabin", 991, 992),
                 exists=lambda p: p == "/usr/sbin/runuser")
    ch.call(REC, "activity")
    assert ch.seen[0][:4] == ["/usr/sbin/runuser", "-u", "motel-cabin", "--"]
    none = channel(b'{"schema": "motel-guest/v1"}', run_as=("motel-cabin", 991, 992), exists=lambda p: False)
    with pytest.raises(GuestUnknown, match="neither setpriv nor runuser"):
        none.call(REC, "activity")
    assert none.seen == [] and "neither" in none.unavailable_reason()


def test_missing_system_user_refuses_the_channel_instead_of_running_as_root():
    ch = channel(b'{"schema": "motel-guest/v1"}', run_as=("motel-cabin", None, None), exists=lambda p: True)
    with pytest.raises(GuestUnknown, match="does not exist"):
        ch.call(REC, "activity")
    assert ch.seen == []


def test_checkin_reports_an_unusable_guest_channel_as_missing_prerequisite(world):
    class Bad:
        def unavailable_reason(self):
            return "neither setpriv nor runuser is installed"
    from motel.cabins.errors import Prerequisite
    with pytest.raises(Prerequisite, match="guest channel cannot run"):
        world.checkin(world.controller(guest=Bad()))
    assert world.hz.writes == []


def test_desk_looks_up_the_system_user(monkeypatch):
    import pwd

    def missing(_name):
        raise KeyError
    monkeypatch.setattr(pwd, "getpwnam", missing)
    assert desk.cabin_user() is None
    monkeypatch.setattr(pwd, "getpwnam", lambda n: type("E", (), {"pw_uid": 991, "pw_gid": 992})())
    assert desk.cabin_user() == ("motel-cabin", 991, 992)


def test_checkin_writes_keys_outside_the_root_only_state_directory(tmp_path):
    w = World(str(tmp_path))
    w.store = Store(str(tmp_path / "state2"), clock=w.clock, host_id=HOST, keys_dir=str(tmp_path / "keys"))
    view = w.checkin()
    assert os.path.exists(os.path.join(str(tmp_path / "keys"), view["lease_id"], "controller_ed25519"))
    assert not os.path.exists(os.path.join(str(tmp_path / "state2"), "keys"))


# ================================================================ M2: delete path = identity only

def test_firewall_that_never_applies_does_not_leak_the_server(tmp_path):
    """Reviewer repro: bootstrap stuck on 'firewall not applied', then delete refused for the same reason."""
    w = World(str(tmp_path))
    w.hz.firewall_apply_gets = 10 ** 9
    with pytest.raises(Fail):
        w.checkin()
    assert not w.hz.servers and not w.hz.firewalls
    assert w.store.all()[0]["state"] == "released"


def test_pending_firewall_blocks_extend_but_not_delete(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.firewall_apply_gets = 10 ** 9
    for fw in world.hz.servers[view["server_id"]]["public_net"]["firewalls"]:
        fw["status"] = "pending"
    with pytest.raises(Fail):
        c.extend(view["lease_id"], timedelta(hours=3))
    assert c.checkout(view["lease_id"])["state"] == "released" and not world.hz.servers


def test_unexpected_attached_volume_blocks_exposure_checks_but_not_delete(world):
    c = world.controller()
    view = world.checkin(c)
    stray = world.hz.add_volume("motel-stray-disk", dict(LABELS, lease="stray-aaaaaaaaaaaa", intent="3" * 32))
    world.hz.servers[view["server_id"]]["volumes"] = [stray]
    with pytest.raises(GuardRefused, match="unexpected attached volume"):
        c.hz.server(world.store.load(view["lease_id"]))
    assert c.checkout(view["lease_id"])["state"] == "released"
    assert stray in world.hz.volumes                                    # a volume we do not own is untouched


def test_volume_never_attached_does_not_block_delete(world):
    """Reviewer repro: attach never happened; delete used to be refused."""
    c = world.controller()
    view = world.checkin(c, disk_gb=50)
    world.hz.servers[view["server_id"]]["volumes"] = []
    world.hz.volumes[view["volume_id"]]["server"] = None
    out = c.checkout(view["lease_id"])
    assert out["state"] == "released" and not world.hz.servers and not world.hz.volumes


def test_delete_still_requires_identity(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.servers[view["server_id"]]["labels"]["lease"] = "someone-elses-aaaaaaaaaaaa"
    assert c.checkout(view["lease_id"])["state"] == "cleanup_pending"
    assert view["server_id"] in world.hz.servers


def test_firewall_not_created_by_motel_makes_the_project_unusable(world):
    world.hz.firewalls[999] = {"id": 999, "name": "office", "labels": {}, "rules": [], "applied_to": []}
    with pytest.raises(Guarded, match="firewall that motel did not create"):
        world.checkin()
    assert world.hz.writes == []


def test_label_selector_firewall_is_refused(world):
    world.hz.firewalls[998] = {"id": 998, "name": "motel-sneaky", "labels": dict(LABELS), "rules": [],
                               "applied_to": [{"type": "label_selector", "label_selector": {"selector": "all"}}]}
    with pytest.raises(Guarded, match="selector"):
        world.checkin()
    assert world.hz.writes == []


def test_firewall_attached_to_the_host_is_refused(world):
    world.hz.firewalls[997] = {"id": 997, "name": "motel-x", "labels": dict(LABELS), "rules": [],
                               "applied_to": [{"type": "server", "server": {"id": HOST}}]}
    with pytest.raises(Guarded):
        world.checkin()


def test_setup_refuses_a_project_with_a_foreign_firewall(tmp_path):
    from motel.cabins import setup as su
    from motel.cabins.config import CabinPaths
    from test_setup import metadata
    w = World(str(tmp_path / "w"))
    w.hz.firewalls[999] = {"id": 999, "name": "office", "labels": {}, "rules": [], "applied_to": []}
    paths = CabinPaths(str(tmp_path / "root"))
    os.makedirs(os.path.dirname(paths.config_file))
    with pytest.raises(Guarded):
        su.setup_hetzner(paths, make_config(), token_reader=lambda _p: "hcloud-token-abcdefghijklmnop",
                         fetch=metadata(), transport_factory=lambda _t: w.hz)


# ================================================================ M4: own volume attaches late

def test_bootstrap_tolerates_the_own_volume_attaching_late(world):
    world.hz.hide_volumes_gets = 4
    view = world.checkin(disk_gb=50)
    assert view["state"] == "active" and view["volume_id"] in world.hz.volumes


def test_bootstrap_never_tolerates_a_foreign_volume(world):
    stray = world.hz.add_volume("motel-stray-disk", dict(LABELS, lease="stray-aaaaaaaaaaaa", intent="3" * 32))
    orig = world.hz.on_write

    def attach(method, path, body):
        if (method, path) == ("POST", "/servers"):
            body["volumes"] = list(body.get("volumes", [])) + [stray]
        if orig:
            orig(method, path, body)
    world.hz.on_write = attach
    with pytest.raises(Fail):
        world.checkin(disk_gb=50)
    assert not world.hz.servers


# ================================================================ M3: cleanup without a usable config

TOML = '[host]\nallow_users = ["alice"]\n\n[cabins]\nenabled = true\ntypes = ["cpx32", "cpx52"]\nmax_eur_per_hour = 0.2\n'


def desk_env(tmp_path):
    from test_desk import Env
    return Env(tmp_path)


def run_desk(env, *argv, **kw):
    return env.call(*argv, **kw)


@pytest.mark.parametrize("replacement", [
    "[host]\nallow_users = [\"alice\"]\n",                                              # no [cabins] table
    TOML.replace("enabled = true", "enabled = false"),                                  # disabled
    TOML + "max_concurrent = 0\n",                                                      # invalid unrelated key
    TOML + "surprise = 1\n",                                                            # unknown key
    TOML.replace('types = ["cpx32", "cpx52"]', "types = 5"),                            # invalid types
    "this is = = not toml",                                                             # unreadable file
])
def test_reap_and_checkout_work_without_a_usable_cabins_config(tmp_path, replacement):
    env = desk_env(tmp_path)
    rc, out, _ = env.call("checkin", "--task", "build", "--client-key", pubkey("U"), "--json")
    lease = json.loads(out)["cabin"]["lease_id"]
    _rc2, out2, _ = env.call("checkin", "--task", "other", "--client-key", pubkey("U"), "--json")
    lease2 = json.loads(out2)["cabin"]["lease_id"]
    with open(env.paths.config_file, "w") as handle:
        handle.write(replacement)
    if "not toml" not in replacement:
        assert env.call("checkin", "--task", "late", "--client-key", pubkey("U"), "--json")[0] != 0   # still refused
    if "not toml" not in replacement:
        rc, out, _ = env.call("checkout", lease, "--json")
        assert rc == 0 and json.loads(out)["cabin"]["state"] == "released"
    env.w.clock.advance(hours=3)
    rc, out, _ = env.call("reap", "--apply", "--json", uid=0, name="root")
    doc = json.loads(out)
    assert rc == 0 and doc["actions"] and not env.w.hz.servers
    assert lease2


def test_load_cleanup_config_keeps_valid_keys_and_drops_bad_ones(tmp_path):
    path = tmp_path / "motel.toml"
    path.write_text('[host]\nallow_users = ["alice"]\n[cabins]\nenabled = true\nidle_timeout = "45m"\n'
                    'max_concurrent = 0\ntoken_file = "relative"\n')
    cfg = cfgmod.load_cleanup_config(str(path))
    assert cfg.idle_timeout == timedelta(minutes=45) and cfg.max_concurrent == 4
    assert cfg.token_file == "/etc/motel/credentials/cabins.token" and cfg.allow_users == ("alice",)
    assert cfg.enabled is False
    assert cfgmod.load_cleanup_config(str(tmp_path / "missing.toml")).enabled is False


def test_new_checkins_stay_refused_when_cabins_are_disabled(tmp_path):
    env = desk_env(tmp_path)
    with open(env.paths.config_file, "w") as handle:
        handle.write(TOML.replace("enabled = true", "enabled = false"))
    assert env.call("checkin", "--task", "x", "--client-key", pubkey("U"), "--json")[0] == 8


# ================================================================ lows

def test_default_type_is_the_first_one_listed(tmp_path):
    w = World(str(tmp_path))
    w.cfg = make_config(types=["cpx52", "cpx32"])
    view = w.controller().checkin(task="t", owner="alice", client_key=pubkey("U"))
    assert view["offer"]["server_type"] == "cpx52"


def test_owner_label_must_end_alphanumeric(world):
    from motel.cabins.util import OWNER_RE
    for bad in ("alice_", "bob-", "carol.", "-dave", "x" * 41):
        assert not OWNER_RE.fullmatch(bad), bad
    for good in ("alice", "a", "al_ice", "uid-1000", "A.b-c9"):
        assert OWNER_RE.fullmatch(good), good
    with pytest.raises(Usage):
        world.checkin(owner="alice_")
    assert desk.owner_label(desk.Caller(1000, 1000, "alice_")) == "uid-1000"
    assert desk.owner_label(desk.Caller(1001, 1001, "bob")) == "bob"


def test_doctor_for_a_user_names_only_their_own_stuck_cabins(world):
    from motel.cabins.errors import ApiError
    mine = world.checkin(world.controller(uid=1000), task="mine")
    theirs = world.checkin(world.controller(uid=1001), task="theirs")
    for view in (mine, theirs):
        world.hz.fail[("DELETE", f"/servers/{view['server_id']}")] = [ApiError(503, "x")]
    world.controller(uid=1000).checkout(mine["lease_id"])
    world.controller(uid=1001).checkout(theirs["lease_id"])
    user = world.controller(uid=1000).doctor()
    stuck = next(c for c in user["checks"] if c["check"] == "no cleanup-pending cabins")
    assert mine["lease_id"] in stuck["detail"] and theirs["lease_id"] not in stuck["detail"]
    assert "belonging to other users" in stuck["detail"]
    root = world.controller(uid=0).doctor()
    stuck = next(c for c in root["checks"] if c["check"] == "no cleanup-pending cabins")
    assert mine["lease_id"] in stuck["detail"] and theirs["lease_id"] in stuck["detail"]


def test_guest_firewall_drops_metadata_service_egress():
    for mode, sources in (("ssh-allowlist", ["203.0.113.10/32"]), ("tailscale", [])):
        nft = userdata.guest_nft(mode, sources)
        assert "ip daddr 169.254.169.254 drop" in nft
        out_chain = nft[nft.index("chain output"):nft.index("chain forward")]
        assert "169.254.169.254" in out_chain


def test_bootstrap_scrubs_every_cloud_init_copy_of_user_data(world):
    world.checkin()
    user_data = next(w[2]["user_data"] for w in world.hz.writes if w[1] == "/servers")
    for needle in ("/run/cloud-init/instance-data-sensitive.json", "/var/lib/cloud/instances/*/user-data.txt*",
                   "/var/lib/cloud/instance/user-data.txt*", "/var/lib/cloud/data/instance-data-sensitive.json"):
        assert needle in user_data


def test_tailscale_enrollment_key_is_revoked_right_after_enrollment(tmp_path):
    w = World(str(tmp_path), access="tailscale")
    view = w.checkin()
    assert list(w.ts.keys.values()) == ["revoked"]            # before any checkout
    rec = w.store.load(view["lease_id"])
    assert rec["cleanup"]["auth_key"] == "done" and rec["tailnet"]["key_revoked_at"]


def test_failed_early_revoke_is_retried_at_checkout(tmp_path):
    w = World(str(tmp_path), access="tailscale")
    real = w.ts.request
    state = {"fail": True}

    def flaky(method, path, body=None):
        if method == "DELETE" and "/keys/" in path and state["fail"]:
            state["fail"] = False
            raise __import__("motel.cabins.errors", fromlist=["ApiError"]).ApiError(503, "x")
        return real(method, path, body)
    w.ts.request = flaky
    c = w.controller()
    view = w.checkin(c)
    assert any("not revoked yet" in x for x in view["warnings"])
    assert c.checkout(view["lease_id"])["cleanup"]["auth_key"] == "done"
    assert list(w.ts.keys.values()) == ["revoked"]


def test_custom_dns_resolvers_reach_the_guest(tmp_path):
    w = World(str(tmp_path))
    w.cfg = make_config(dns_resolvers=["9.9.9.9", "1.1.1.1"])
    w.checkin()
    user_data = next(x[2]["user_data"] for x in w.hz.writes if x[1] == "/servers")
    assert "DNS=9.9.9.9 1.1.1.1" in user_data and "FallbackDNS=" in user_data and "Domains=~." in user_data
    rules = [r for r in next(x[2] for x in w.hz.writes if x[1] == "/firewalls")["rules"] if r.get("port") == "53"]
    assert all(r["destination_ips"] == ["9.9.9.9/32", "1.1.1.1/32"] for r in rules)


def test_default_dns_resolvers_are_configured_too(world):
    world.checkin()
    user_data = next(x[2]["user_data"] for x in world.hz.writes if x[1] == "/servers")
    assert "DNS=185.12.64.1 185.12.64.2" in user_data


def test_ssh_config_quotes_paths_and_validates_values(tmp_path):
    view = {"ssh": {"host_key_alias": "motel-build-0123456789ab", "host": "203.0.113.7", "user": "cabin", "port": 22}}
    text = cli.ssh_config_text("/state/my dir/lease", view)
    assert 'IdentityFile "/state/my dir/lease/id_ed25519"' in text
    assert 'UserKnownHostsFile "/state/my dir/lease/known_hosts"' in text
    assert '"/state/100%%/' in cli.ssh_config_text("/state/100%/", view) or "100%%" in cli.ssh_config_text(
        "/state/100%/", view)
    for bad in ('/state/a"b', "/state/a\nProxyCommand evil", "/state/a\\b", "relative"):
        with pytest.raises(Usage):
            cli.ssh_config_text(bad, view)
    for key, value in (("host", "203.0.113.7\nProxyCommand x"), ("host_key_alias", "motel x\nHost *"),
                       ("user", "root"), ("port", 2222)):
        broken = {"ssh": dict(view["ssh"], **{key: value})}
        with pytest.raises(Usage):
            cli.ssh_config_text("/state/x", broken)


def test_guest_creates_the_work_directory_for_jobs_and_sessions(tmp_path, monkeypatch):
    from cabin_fakes import Clock, RealGuestChannel
    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("HOME", str(home))
    ch = RealGuestChannel(str(tmp_path / "g"), Clock())
    status, _pid = ch.connect()
    assert status == 0 and (home / "work").is_dir()                      # a session creates ~/work
    (home / "work").rmdir()
    import base64

    class Child:
        pid = 1

        def wait(self):
            return 0
    payload = base64.b64encode(json.dumps({"argv": ["true"], "cwd": "work"}).encode()).decode()
    seen = {}
    assert ch.g.run(payload, popen=lambda argv, **kw: seen.update(kw) or Child()) == 0
    assert (home / "work").is_dir() and seen["cwd"] == str(home / "work")


# ---- rate limiting of unprivileged provider reads

def server_lists(world):
    return len(world.hz.list_calls)


def test_user_reads_are_served_from_a_30_second_cache(world):
    c = world.controller(uid=1000)
    world.checkin(c)
    base = server_lists(world)
    for _ in range(20):
        c.status()
        world.controller(uid=1001).status()
    first = server_lists(world) - base
    assert first <= 1
    world.clock.advance(seconds=29)
    c.status()
    assert server_lists(world) - base == first
    world.clock.advance(seconds=2)
    c.status()
    assert server_lists(world) - base == first + 1


def test_user_doctor_is_cached_but_root_is_always_live(world):
    world.checkin(world.controller(uid=1000))
    user = world.controller(uid=1000)
    user.doctor()
    base = server_lists(world)
    for _ in range(10):
        user.doctor()
    assert server_lists(world) == base
    root = world.controller(uid=0)
    root.doctor()
    assert server_lists(world) > base


def test_writes_invalidate_the_cache(world):
    c = world.controller(uid=1000)
    view = world.checkin(c)
    assert c.status()["cabins"][0]["server_present"] is True
    c.checkout(view["lease_id"])
    assert c.status()["cabins"][0].get("server_present") is False


def test_reaper_always_reads_live(world):
    c = world.controller(uid=0)
    world.checkin(world.controller(uid=1000))
    c.reap(apply=False)
    base = server_lists(world)
    c.reap(apply=False)
    assert server_lists(world) > base


# ---- operator: resolve

def stuck_tailnet_world(tmp_path):
    w = World(str(tmp_path), access="tailscale")
    c = w.controller()
    view = w.checkin(c)
    w.ts.undeletable.add(w.store.load(view["lease_id"])["tailnet"]["device_id"])
    out = c.checkout(view["lease_id"])
    assert out["state"] == "cleanup_pending"
    return w, c, view


def test_resolve_forget_node_finishes_a_stuck_cleanup(tmp_path):
    w, c, view = stuck_tailnet_world(tmp_path)
    out = c.resolve(view["lease_id"], forget_node=True)
    assert out["state"] == "released" and not w.hz.servers
    assert any("forgotten by the operator" in x for x in out["warnings"])
    with open(os.path.join(w.store.root, "guestbook.jsonl")) as handle:
        assert "operator_forget_node" in handle.read()


def test_resolve_needs_the_flag_and_a_cleanup_pending_cabin(tmp_path, world):
    with pytest.raises(Usage):
        world.controller().resolve("x-0123456789ab", forget_node=False)
    view = world.checkin()
    with pytest.raises(PolicyRefused, match="cleanup-pending"):
        world.controller().resolve(view["lease_id"], forget_node=True)


def test_resolve_does_not_forget_other_pending_items(tmp_path):
    from motel.cabins.errors import ApiError
    w = World(str(tmp_path), access="tailscale")
    c = w.controller()
    view = w.checkin(c)
    w.ts.undeletable.add(w.store.load(view["lease_id"])["tailnet"]["device_id"])
    w.hz.fail[("DELETE", f"/servers/{view['server_id']}")] = [ApiError(503, "x")] * 3
    c.checkout(view["lease_id"])
    out = c.resolve(view["lease_id"], forget_node=True)
    assert out["state"] == "cleanup_pending" and out["cleanup"]["server"].startswith("pending")
    assert out["cleanup"]["tailnet_node"] == "done"


def test_resolve_is_an_operator_verb_through_the_desk(tmp_path):
    env = desk_env(tmp_path)
    rc, out, _ = env.call("resolve", "build-0123456789ab", "--forget-node", "--json")
    assert rc == 3 and "operator" in json.loads(out)["error"]
    rc, out, _ = env.call("resolve", "build-0123456789ab", "--json", uid=0, name="root")
    assert rc == 2                                                       # --forget-node is required


def test_user_verbs_exclude_every_operator_verb():
    """The sudoers rule must pin `cabin <verb>` per user verb, never `cabin *`."""
    assert set(desk.USER_VERBS) == {"checkin", "ls", "status", "extend", "checkout", "doctor", "images"}
    for verb in ("setup", "reap", "resolve"):
        assert verb not in desk.USER_VERBS and verb in desk.OPERATOR_VERBS
    assert not set(desk.USER_VERBS) & set(desk.OPERATOR_VERBS)
