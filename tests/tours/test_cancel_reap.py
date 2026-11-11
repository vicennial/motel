"""Cancel order, orphaned tours, and what `tour reap --apply` does."""

TWO = [{"id": "a", "prompt": "do a", "secrets": ["anthropic"], "budget_usd": 0.5},
       {"id": "b", "prompt": "do b", "secrets": ["anthropic"], "budget_usd": 0.5}]


def test_cancel_while_running_revokes_stops_collects_then_deletes(rig):
    tid = rig.start()
    rig.transport.block_until_cancel = True
    rig.transport.on_run = lambda card: rig.store.request_cancel(tid)
    rig.supervise(tid)
    order = rig.log.kinds()
    after = order[order.index("run") + 1:]
    assert after[0] == "revoke"
    assert after.index("revoke") < after.index("stop") < after.index("list_out") < len(after)
    assert rig.servers_left() == []                                    # the delete came last: nothing is left
    doc = rig.store.load(tid)
    assert doc["status"] == "cancelled" and doc["guests"]["fix-auth"]["status"] == "cancelled"
    assert doc["guests"]["fix-auth"]["cleanup"] == "deleted"
    record = rig.result_record(tid, "fix-auth")
    assert record["partial"] is True and record["status"] == "cancelled"


def test_cancel_before_anything_ran_creates_no_cabin(rig):
    tid = rig.start(TWO)
    rig.store.request_cancel(tid)
    rig.supervise(tid)
    doc = rig.store.load(tid)
    assert doc["status"] == "cancelled" and "run" not in rig.log.kinds() and rig.servers_left() == []


def test_cancel_with_no_supervisor_finishes_the_tour_in_the_desk(rig):
    tid = rig.start(TWO)                                               # launched, but our fake launcher runs nothing
    reply = rig.service.cancel(rig.alice, tid)
    assert reply["cancelling"] is True
    doc = rig.store.load(tid)
    assert doc["status"] == "cancelled" and rig.servers_left() == []
    assert ("revoke", tid) in rig.log                                  # tour-wide revoke first


def test_cancel_of_a_finished_tour_is_a_no_op(rig):
    doc = rig.run_tour()
    reply = rig.service.cancel(rig.alice, doc["id"])
    assert reply["already_finished"] is True


def test_only_the_owner_can_cancel(rig):
    import pytest

    from motel.tours.errors import NotFound
    tid = rig.start()
    with pytest.raises(NotFound):
        rig.service.cancel(rig.bob, tid)
    assert not rig.store.cancel_requested(tid)


def test_reap_restarts_a_dead_supervisor_then_gives_up_and_cancels(rig):
    tid = rig.start()
    rig.launched.clear()
    for _ in range(8):
        rig.service.reap(apply=True)
        if rig.store.load(tid)["status"] == "cancelled":
            break
    doc = rig.store.load(tid)
    assert len(rig.launched) == 3                                       # restarted at most three times
    assert doc["status"] == "cancelled"
    assert rig.servers_left() == []


def test_reap_dry_run_changes_nothing(rig):
    tid = rig.start()
    rig.launched.clear()
    reply = rig.service.reap(apply=False)
    assert reply["applied"] is False and reply["actions"][0]["action"] == "restart_supervisor"
    assert rig.launched == [] and rig.store.load(tid)["supervisor"]["restarts"] == 0


def test_reap_leaves_a_live_supervisor_alone(rig):
    rig.start()
    rig.unit_is_active = True
    assert rig.service.reap(apply=True)["actions"] == []


def test_a_restarted_supervisor_recovers_an_attempt_the_old_one_left_half_done(rig, monkeypatch):
    from motel.tours.pipeline import AttemptRunner
    tid = rig.start()
    real = AttemptRunner._teardown

    def die(self, ctx):
        raise SystemExit("the supervisor process went away")           # after the run, before collect / revoke / delete
    monkeypatch.setattr(AttemptRunner, "_teardown", die)
    try:
        rig.supervise(tid)
    except SystemExit:
        pass
    assert rig.servers_left() != [] and rig.attempts(tid)[0]["status"] not in ("done", "failed")
    monkeypatch.setattr(AttemptRunner, "_teardown", real)
    rig.supervise(tid)                                                  # what the restarted supervisor does first
    attempts = rig.attempts(tid)
    assert rig.servers_left() == [] and all(a["cleanup"] == "deleted" for a in attempts)
    assert rig.store.load(tid)["status"] in ("done", "failed")
    assert rig.attempts(tid)[0]["id"] in rig.concierge.revoked


def test_reap_removes_unfinished_staging_and_old_finished_tours(rig):
    tid = rig.start()
    rig.store.update(tid, lambda d: d.update(status="staging"))
    rig.clock.sleep(2 * 3600)
    rig.service.reap(apply=True)
    assert not rig.store.exists(tid)
    done = rig.run_tour()
    rig.service.reap(apply=True)
    assert rig.store.exists(done["id"])                                 # kept: results live for 14 days
    rig.clock.sleep(15 * 86400)
    rig.service.reap(apply=True)
    assert not rig.store.exists(done["id"])


def test_a_finished_tour_is_never_run_again(rig):
    doc = rig.run_tour()
    calls = len(rig.log)
    rig.supervise(doc["id"])
    assert len(rig.log) == calls
