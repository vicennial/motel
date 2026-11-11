"""Round 3: a bad key-tree layout must never stop cleanup; layout is checked lazily and against the real gid."""
import json
import os
import time

import pytest
from cabin_fakes import HOST, World, pubkey

from motel.cabins.errors import GuestUnknown, Prerequisite
from motel.cabins.guestchan import GuestChannel
from motel.cabins.store import Store, check_key_layout

GOOD = b'{"schema": "motel-guest/v1"}'


def break_keys(kind, keys, victim=None):
    if kind == "group-writable":
        os.chmod(keys, 0o2770)
    elif kind == "symlink":
        os.rename(keys, str(keys) + ".moved")
        os.symlink(victim, keys)
    elif kind == "world-writable":
        os.chmod(keys, 0o777)


KINDS = ["group-writable", "symlink", "world-writable"]


@pytest.mark.parametrize("kind", KINDS)
def test_checkout_and_reap_still_delete_servers_with_a_bad_key_tree(tmp_path, kind):
    w = World(str(tmp_path))
    w.store = Store(str(tmp_path / "state2"), clock=w.clock, host_id=HOST, keys_dir=str(tmp_path / "keys"))
    victim = tmp_path / "victim"
    victim.mkdir()
    c = w.controller()
    a = w.checkin(c, task="aaa")
    b = w.checkin(c, task="bbb")
    break_keys(kind, tmp_path / "keys", victim)
    # a brand-new Store (what every desk verb builds) must not refuse the broken tree
    fresh = Store(str(tmp_path / "state2"), clock=w.clock, host_id=HOST, keys_dir=str(tmp_path / "keys"))
    w.store = fresh
    c2 = w.controller()
    assert c2.checkout(a["lease_id"])["state"] == "released"
    w.clock.advance(hours=3)
    out = w.controller(uid=0).reap(apply=True)
    assert out["actions"][0]["state"] == "released" and not w.hz.servers and not w.hz.firewalls
    assert b["lease_id"]
    assert os.listdir(victim) == []


def test_checkout_with_a_foreign_owned_key_tree_still_releases(tmp_path, monkeypatch):
    w = World(str(tmp_path))
    w.store = Store(str(tmp_path / "state2"), clock=w.clock, host_id=HOST, keys_dir=str(tmp_path / "keys"))
    c = w.controller()
    view = w.checkin(c)
    monkeypatch.setattr(os, "geteuid", lambda: 12345)        # as if keys/ were owned by motel-cabin
    fresh = Store(str(tmp_path / "state2"), clock=w.clock, host_id=HOST, keys_dir=str(tmp_path / "keys"))
    w.store = fresh
    assert w.controller().checkout(view["lease_id"])["state"] == "released"
    assert not w.hz.servers


@pytest.mark.parametrize("kind", KINDS)
def test_desk_reap_and_checkout_exit_zero_with_a_bad_key_tree(tmp_path, kind):
    from test_desk import Env
    env = Env(tmp_path)
    leases = []
    for task in ("aaa", "bbb"):
        rc, out, _ = env.call("checkin", "--task", task, "--client-key", pubkey("U"), "--json")
        assert rc == 0
        leases.append(json.loads(out)["cabin"]["lease_id"])
    victim = tmp_path / "victim"
    victim.mkdir()
    break_keys(kind, env.paths.keys_dir, victim)
    rc, out, _ = env.call("checkout", leases[0], "--json")
    assert rc == 0 and json.loads(out)["cabin"]["state"] == "released"
    env.w.clock.advance(hours=3)
    rc, out, _ = env.call("reap", "--apply", "--json", uid=0, name="root")
    assert rc == 0 and not env.w.hz.servers
    assert os.listdir(victim) == []


# ---- the guest channel and checkin use the layout check

def channel(problem, **kw):
    seen = []
    ch = GuestChannel("/nonexistent", runner=lambda argv, t: (seen.append(argv), (0, GOOD))[1],
                      layout_check=lambda: problem, **kw)
    ch.seen = seen
    return ch


REC = {"lease_id": "build-0123456789ab", "name": "motel-build-0123456789ab",
       "access": {"mode": "ssh-allowlist", "address": "203.0.113.7"}}


def test_a_bad_layout_makes_the_guest_unknown_and_runs_nothing():
    ch = channel("the keys directory is writable by its group or others")
    with pytest.raises(GuestUnknown, match="key layout unusable"):
        ch.call(REC, "activity")
    assert ch.seen == [] and "writable" in ch.unavailable_reason()
    ok = channel(None)
    assert ok.call(REC, "activity")["schema"] == "motel-guest/v1" and ok.unavailable_reason() is None


def test_checkin_is_refused_before_any_provider_call_when_the_layout_is_bad(tmp_path):
    w = World(str(tmp_path))
    with pytest.raises(Prerequisite, match="guest channel cannot run") as exc:
        w.checkin(w.controller(guest=channel("the keys directory must be root:991 mode 2750")))
    assert "docs/cabins.md" in exc.value.message and "install --cabins --upgrade" in exc.value.message
    assert w.hz.writes == [] and w.hz.reads == []


def test_doctor_reports_the_layout_problem(tmp_path):
    w = World(str(tmp_path))
    report = w.controller(guest=channel("the keys directory is a symlink or not a directory")).doctor()
    bad = next(c for c in report["checks"] if c["check"] == "setup and prerequisites")
    assert not bad["ok"] and "symlink" in bad["detail"]


def test_reap_relies_on_ttl_when_the_layout_is_bad(tmp_path):
    w = World(str(tmp_path))
    c = w.controller()
    view = w.checkin(c)
    bad = w.controller(uid=0, guest=channel("the keys directory is writable by its group or others"))
    assert bad.reap(apply=True)["actions"] == []                          # activity unknown, never idle
    w.clock.advance(hours=3)
    assert bad.reap(apply=True)["actions"][0]["state"] == "released" and view["server_id"] not in w.hz.servers


# ---- check_key_layout against the system user's primary gid

def tree(tmp_path, home_mode=0o750, keys_mode=0o2750):
    home = tmp_path / "home"
    keys = home / "keys"
    keys.mkdir(parents=True)
    os.chmod(home, home_mode)
    os.chmod(keys, keys_mode)
    return home, keys


def test_layout_accepts_the_documented_tree(tmp_path):
    _home, keys = tree(tmp_path)
    assert check_key_layout(str(keys), os.getgid()) is None
    assert check_key_layout(str(keys)) is None


def test_layout_requires_the_primary_group_and_exact_mode(tmp_path):
    _home, keys = tree(tmp_path)
    assert "primary group" in check_key_layout(str(keys), os.getgid() + 1)
    os.chmod(keys, 0o750)
    assert "2750" in check_key_layout(str(keys), os.getgid())            # not setgid
    os.chmod(keys, 0o2770)
    assert "writable" in check_key_layout(str(keys), os.getgid())


def test_layout_requires_a_traversable_unwritable_root_owned_home(tmp_path):
    home, keys = tree(tmp_path, home_mode=0o700)
    assert "home" in check_key_layout(str(keys), os.getgid())             # group cannot traverse
    os.chmod(home, 0o770)
    assert "home" in check_key_layout(str(keys), os.getgid())             # group can write the home
    os.chmod(home, 0o750)
    assert check_key_layout(str(keys), os.getgid()) is None


def test_layout_rejects_symlinks_missing_and_foreign_owner(tmp_path, monkeypatch):
    home, keys = tree(tmp_path)
    assert "missing" in check_key_layout(str(home / "nope"), os.getgid())
    os.symlink(keys, home / "link")
    assert "symlink" in check_key_layout(str(home / "link"))
    monkeypatch.setattr(os, "geteuid", lambda: 4242)
    assert "not owned by root" in check_key_layout(str(keys))


# ---- key_access_problems

def test_key_access_problems_uses_the_given_gid_and_survives_races(tmp_path, monkeypatch):
    store = Store(str(tmp_path / "s"), host_id=HOST, keys_dir=str(tmp_path / "keys"))
    with store.key_dir_fd("build-0123456789ab") as dfd:
        store.write_key(dfd, "controller_ed25519", "K\n")
    assert store.key_access_problems(os.getgid()) == []
    assert store.key_access_problems(os.getgid() + 1)                     # wrong primary group is a problem
    real_lstat, real_listdir = os.lstat, os.listdir

    def racing_lstat(path, *a, **k):
        if str(path).endswith("controller_ed25519"):
            raise FileNotFoundError(path)
        return real_lstat(path, *a, **k)
    monkeypatch.setattr(os, "lstat", racing_lstat)
    assert store.key_access_problems(os.getgid()) == []                   # entry vanished: skipped, no crash

    def denied_lstat(path, *a, **k):
        if str(path).endswith("controller_ed25519"):
            raise PermissionError(path)
        return real_lstat(path, *a, **k)
    monkeypatch.setattr(os, "lstat", denied_lstat)
    assert store.key_access_problems(os.getgid()) == ["build-0123456789ab/controller_ed25519"]

    def vanishing_listdir(path):
        if str(path).endswith("build-0123456789ab"):
            raise FileNotFoundError(path)
        return real_listdir(path)
    monkeypatch.setattr(os, "lstat", real_lstat)
    monkeypatch.setattr(os, "listdir", vanishing_listdir)
    assert store.key_access_problems(os.getgid()) == []


# ---- scratch hygiene

def test_stale_scratch_directories_are_removed_when_new_ones_are_made(tmp_path):
    store = Store(str(tmp_path / "s"), host_id=HOST, keys_dir=str(tmp_path / "keys"))
    old, fresh, other = (os.path.join(store.tmp, n) for n in ("keygen-old", "keygen-fresh", "something-else"))
    for path in (old, fresh, other):
        os.mkdir(path)
        open(os.path.join(path, "controller_ed25519"), "w").close()
    long_ago = time.time() - 7200
    os.utime(old, (long_ago, long_ago))
    os.utime(other, (long_ago, long_ago))
    new = store.scratch()
    assert not os.path.exists(old) and os.path.exists(fresh) and os.path.exists(other) and os.path.exists(new)


def test_reap_apply_cleans_stale_scratch_after_a_crash(tmp_path):
    w = World(str(tmp_path))
    stale = os.path.join(w.store.tmp, "keygen-crashed")
    os.mkdir(stale)
    os.utime(stale, (time.time() - 7200,) * 2)
    w.controller(uid=0).reap(apply=False)
    assert os.path.exists(stale)
    w.controller(uid=0).reap(apply=True)
    assert not os.path.exists(stale)
