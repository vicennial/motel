"""Per-user ceilings and disk use, settlement after a crash, SIGTERM, a broken config, and who counts as an operator."""
import json
import os
import signal
import threading

import doubles
import pytest
from cabin_fakes import World  # noqa: F401  (keeps the shared fakes on the path)
from rig import SyncExecutor

from motel.cabins.desk import Caller
from motel.tours import config as tconfig
from motel.tours import desk, recovery
from motel.tours import pipeline as pl
from motel.tours.errors import Refused
from motel.tours.supervisor import Supervisor, stop_on_sigterm


def inputs_of(rig, tid):
    return os.listdir(rig.store.path(tid, "inputs"))


# -- M2: staged files do not pile up --------------------------------------------------------------------------------------

def test_inputs_are_deleted_when_a_tour_finishes(rig):
    doc = rig.run_tour()
    assert doc["status"] == "done" and inputs_of(rig, doc["id"]) == []


def test_inputs_are_deleted_when_a_tour_is_cancelled_and_the_loop_cannot_fill_the_disk(rig):
    rig.set_limits(max_active_tours_per_user=100)
    for _ in range(8):
        tid = rig.start()
        assert inputs_of(rig, tid) != []
        rig.service.cancel(rig.alice, tid)                 # no supervisor: the desk finishes the tour at once
        assert rig.store.load(tid)["status"] == "cancelled" and inputs_of(rig, tid) == []
    assert rig.service.reap(apply=True)["actions"] == []   # nothing left to drop


def test_a_user_has_a_ceiling_on_unfinished_tours(rig):
    ids = [rig.start() for _ in range(3)]
    with pytest.raises(Refused, match="3 unfinished"):
        rig.start()
    assert len(rig.store.ids()) == 3                        # the refusal left nothing behind
    rig.service.cancel(rig.alice, ids[0])
    rig.start()                                             # a slot is free again
    rig.start(caller=rig.root)                              # the ceiling is per user


def test_a_user_has_a_ceiling_on_staged_megabytes(rig):
    rig.set_limits(max_staged_mb_per_user=0)
    with pytest.raises(Refused, match="staged"):
        rig.start()
    assert rig.store.ids() == []


def test_start_refuses_when_the_disk_would_run_low(rig):
    rig.service.free_bytes = lambda path: 2 * 1024 ** 3 + 5       # 2 GiB + 5 bytes free; staging needs more than 5 bytes
    with pytest.raises(Refused, match="free disk space"):
        rig.start()
    assert rig.store.ids() == []
    rig.service.free_bytes = lambda path: 100 * 1024 ** 3
    rig.start()


def test_reap_drops_staged_inputs_that_a_finished_tour_still_holds(rig):
    tid = rig.run_tour()["id"]
    with open(rig.store.input_path(tid, "left-over"), "wb") as handle:
        handle.write(b"x" * 10)
    dry = rig.service.reap(apply=False)
    assert {"tour": tid, "action": "drop_staged_inputs"} in dry["actions"] and inputs_of(rig, tid) == ["left-over"]
    rig.service.reap(apply=True)
    assert inputs_of(rig, tid) == []


def test_the_ceiling_keys_are_validated_and_read_from_the_config():
    limits = tconfig.Limits.from_table({"enabled": True, "max_active_tours_per_user": 2, "max_staged_mb_per_user": 64})
    assert (limits.max_active_tours_per_user, limits.max_staged_mb_per_user) == (2, 64)
    assert tconfig.Limits().max_active_tours_per_user == 3 and tconfig.Limits().max_staged_mb_per_user == 1024
    for bad in ({"max_active_tours_per_user": 0}, {"max_staged_mb_per_user": "big"}, {"max_active_tours_per_user": True}):
        with pytest.raises(Exception, match="whole number"):
            tconfig.Limits.from_table({"enabled": True, **bad})


# -- L5: money and tokens survive a crash -----------------------------------------------------------------------------------

def test_the_attempt_knows_its_reservation_as_soon_as_the_tour_does(rig, monkeypatch):
    tid = rig.start()
    seen = []

    class Boom(BaseException):
        pass

    def die(self, ctx):
        seen.append((rig.store.load_attempt(tid, ctx.rec["id"])["reserved_eur"], rig.store.load(tid)["budget"]["reserved_eur"]))
        raise Boom()
    monkeypatch.setattr(pl.AttemptRunner, "_phase_book", die)
    with pytest.raises(Boom):
        Supervisor(rig.env, tid, executor_factory=SyncExecutor).run()
    on_disk_attempt, on_disk_tour = seen[0]
    assert float(on_disk_attempt) > 0 and on_disk_attempt == on_disk_tour


def test_a_placeholder_whose_issue_call_died_is_still_revoked(rig):
    original, issued = rig.concierge.issue, []

    def issue(**kw):
        token = original(**kw)
        issued.append(token)
        raise RuntimeError("the reply was lost")            # the concierge made the token; the engine never saw it
    rig.concierge.issue = issue
    doc = rig.run_tour()
    assert issued and not rig.concierge.is_valid(issued[0])
    assert doc["guests"]["fix-auth"]["status"] in ("done", "failed")


def test_settling_the_same_attempt_twice_moves_no_money_twice(rig):
    doc = rig.run_tour()
    tid = doc["id"]
    before = rig.store.load(tid)["budget"]
    rec = rig.attempts(tid)[0]
    rec["status"] = "running"                                # as if a restarted supervisor found it half finished
    rig.store.save_attempt(tid, rec)
    recovery.recover_attempt(rig.runner(), rig.store.load(tid), rec)
    assert rig.store.load(tid)["budget"] == before


# -- L6: SIGTERM runs the teardown -------------------------------------------------------------------------------------------

def test_sigterm_sets_the_stop_flag_and_restores_the_old_handler():
    event, old = threading.Event(), signal.getsignal(signal.SIGTERM)
    with stop_on_sigterm(event):
        signal.raise_signal(signal.SIGTERM)
        assert event.is_set()
    assert signal.getsignal(signal.SIGTERM) is old


def test_a_stop_while_the_guest_runs_still_collects_revokes_and_deletes(rig):
    rig.transport.block_until_cancel = True
    rig.transport.on_run = lambda card: rig.env.stop.set()
    tid = rig.start()
    doc = Supervisor(rig.env, tid, executor_factory=SyncExecutor).run()
    assert doc["status"] == "running"                                  # not finished: a restart carries on
    guest = rig.store.load(tid)["guests"]["fix-auth"]
    assert guest["status"] == "failed" and guest["cleanup"] == "deleted" and rig.servers_left() == []
    assert all(not rig.concierge.is_valid(t) for t in rig.concierge.tokens.values())
    assert "stop" in rig.log.kinds() and "list_out" in rig.log.kinds()
    assert "supervisor was stopped" in rig.result_record(tid, "fix-auth")["error"]


def test_a_stop_before_the_command_requeues_the_guest_for_the_restarted_supervisor(rig):
    original = rig.transport.preflight

    def preflight(target, forward_port, **kw):
        original(target, forward_port, **kw)
        rig.env.stop.set()
    rig.transport.preflight = preflight
    tid = rig.start()
    doc = Supervisor(rig.env, tid, executor_factory=SyncExecutor).run()
    assert doc["status"] == "running" and rig.store.load(tid)["guests"]["fix-auth"]["status"] == "queued"
    assert rig.servers_left() == [] and rig.transport.leases_run == []
    rig.env.stop.clear()
    rig.transport.preflight = original
    rig.supervise(tid)
    assert rig.store.load(tid)["status"] == "done"


# -- L7: a broken [tours] never stops cleanup or rooms ------------------------------------------------------------------------

def test_a_missing_or_invalid_tours_table_gives_cleanup_defaults(tmp_path):
    missing = tmp_path / "none.toml"
    assert tconfig.load_cleanup_limits(str(missing)).enabled is False
    broken = tmp_path / "broken.toml"
    broken.write_text("[tours]\nenabled = true\nmax_concurrent_guests = \"many\"\n")
    assert tconfig.load_cleanup_limits(str(broken)).enabled is False
    garbage = tmp_path / "garbage.toml"
    garbage.write_text("[tours\n")
    assert tconfig.load_cleanup_limits(str(garbage)).enabled is False
    with pytest.raises(Exception):  # noqa: B017  start still insists on a valid table
        tconfig.load_limits(str(broken))


def test_a_bad_tours_table_does_not_break_the_core_config(tmp_path):
    from helpers import BASE_TOML
    from motel.config import Config
    path = tmp_path / "motel.toml"
    path.write_text(BASE_TOML + '\n[tours]\nenabled = true\nmax_concurrent_guests = "many"\n')
    cfg = Config.load(str(path))                       # rooms keep working
    assert cfg.tours is None and "tours" in (cfg.tours_problem or "")


# -- L8: who is an operator ----------------------------------------------------------------------------------------------------

class Cfg:
    allow_users = ("alice",)


def test_only_root_and_non_tour_administrators_are_operators():
    sudo_alice = Caller(uid=1000, gid=1000, name="alice", operator=True)       # what real_caller makes of any sudo user
    sudo_carol = Caller(uid=1001, gid=1001, name="carol", operator=True)       # an administrator who is not a tour user
    root = Caller(uid=0, gid=0, name="root", operator=True)
    plain = Caller(uid=1002, gid=1002, name="dave")
    groups = lambda c: set()  # noqa: E731
    assert desk.is_operator(root, Cfg, groups)
    assert desk.is_operator(sudo_carol, Cfg, groups)
    assert not desk.is_operator(sudo_alice, Cfg, groups)                        # in allow_users: a user, whatever the flag says
    assert not desk.is_operator(plain, Cfg, groups)


def test_a_tour_user_through_sudo_cannot_run_operator_verbs_or_list_everyone(rig):
    from test_tours_desk import desk as run_desk
    sudo_alice = Caller(uid=rig.alice.uid, gid=rig.alice.gid, name="alice", operator=True)
    code, _out, err = run_desk(rig, ["reap"], caller=sudo_alice)
    assert code != 0 and "operator command" in err.getvalue()
    rig.start(caller=rig.root)
    code, out, _ = run_desk(rig, ["ls", "--all", "--json"], caller=sudo_alice)
    assert code == 0 and json.loads(out.getvalue())["tours"] == []             # only her own tours


def test_root_sees_everyones_tours_with_ls_all(rig):
    from test_tours_desk import desk as run_desk
    rig.start(caller=rig.root)
    rig.start()
    code, out, _ = run_desk(rig, ["ls", "--all", "--json"], caller=rig.root)
    assert code == 0 and len(json.loads(out.getvalue())["tours"]) == 2


# -- L9: who may use the own-login token -----------------------------------------------------------------------------------------

OWN = [{"id": "me", "prompt": "do it", "auth": "own-login", "secrets": []}]


def test_an_empty_allow_users_means_nobody_may_use_own_login(rig):
    from motel.tours import ownlogin
    rig.set_limits(own_login_enabled=True, own_login_allow_users=())
    ownlogin.set_token(rig.token_file, (doubles.OWN_TOKEN + "\n").encode())
    with pytest.raises(Refused, match="allow_users"):
        rig.start(OWN)
    rig.set_limits(own_login_allow_users=("bob",))
    with pytest.raises(Refused, match="allow_users"):
        rig.start(OWN)                                                          # alice is not bob
    rig.set_limits(own_login_allow_users=("alice",))
    rig.start(OWN)


def test_an_allowed_group_is_honoured(rig):
    from motel.tours import ownlogin
    rig.set_limits(own_login_enabled=True, own_login_allow_users=("@agents",))
    ownlogin.set_token(rig.token_file, (doubles.OWN_TOKEN + "\n").encode())
    rig.service.groups_fn = lambda caller: {"agents"} if caller.name == "alice" else set()
    rig.start(OWN)
    with pytest.raises(Refused):
        rig.start(OWN, caller=rig.bob)


def test_allow_users_is_validated():
    ok = tconfig.Limits.from_table({"enabled": True, "own_login": {"enabled": True, "allow_users": ["alice", "@agents"]}})
    assert ok.own_login_allow_users == ("alice", "@agents")
    for bad in (["ALICE"], "alice", [1], ["a b"]):
        with pytest.raises(Exception, match="allow_users"):
            tconfig.Limits.from_table({"enabled": True, "own_login": {"allow_users": bad}})
