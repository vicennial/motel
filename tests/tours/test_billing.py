"""Tours and hourly billing: the budget reservation covers the started hours a cabin can be billed for, and a tour booking
finds room when the only thing in its way is a sealed (idle) cabin held for its release window."""
import math
from datetime import timedelta
from decimal import Decimal

import pytest
from cabin_fakes import make_config
from rig import SyncExecutor

from motel import billing
from motel.cabins.controller import EXPIRY_GRACE
from motel.cabins.util import iso
from motel.tours import pipeline as pl
from motel.tours.supervisor import Supervisor


def reserved_by_the_attempt(rig, monkeypatch, tid):
    """The reservation the first attempt wrote, captured right before it books a cabin."""
    seen = {}

    class Stop(BaseException):
        pass

    def stop(self, ctx):
        seen["reserved"] = Decimal(rig.store.load_attempt(tid, ctx.rec["id"])["reserved_eur"])
        seen["cabin_ttl"] = ctx.cabin_ttl
        seen["server_type"] = ctx.guest["server_type"]
        raise Stop()
    monkeypatch.setattr(pl.AttemptRunner, "_phase_book", stop)
    with pytest.raises(Stop):
        Supervisor(rig.env, tid, executor_factory=SyncExecutor).run()
    return seen


@pytest.mark.parametrize("ttl_s", [60, 20 * 60, 21 * 60, 50 * 60, 51 * 60, 3600, 5400, 3 * 3600])
def test_the_reservation_covers_the_cabin_ttl_plus_the_grace_in_started_hours(rig, monkeypatch, ttl_s):
    tid = rig.start([{"id": "g", "prompt": "p", "secrets": ["anthropic"], "budget_usd": 0.5, "ttl": f"{ttl_s // 60}m"}])
    seen = reserved_by_the_attempt(rig, monkeypatch, tid)
    hourly = Decimal(rig.world.controller().offer(seen["server_type"], seen["cabin_ttl"])["total_hourly_net_eur"])
    hours = math.ceil((seen["cabin_ttl"] + EXPIRY_GRACE).total_seconds() / 3600)
    assert seen["reserved"] == hourly * hours


@pytest.mark.parametrize("ttl_s", [60, 21 * 60, 51 * 60, 5400, 3 * 3600])
def test_a_cabin_deleted_by_its_expiry_plus_grace_is_billed_at_most_the_reserved_hours_when_the_clocks_agree(ttl_s):
    """Deleted no later than its expiry plus the grace, counted from motel's own creation time, a cabin is billed at most the
    hours reserved. This is the covered case only: a late pass or a provider clock behind motel's can add one hour (see
    the next test)."""
    from datetime import UTC, datetime
    start = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    cabin_ttl = timedelta(seconds=ttl_s) + pl.SETUP_MARGIN
    reserved_hours = math.ceil((cabin_ttl + EXPIRY_GRACE).total_seconds() / 3600)
    for extra in range(0, 61, 5):                     # deleted up to a minute (in 5 s steps) before the latest moment
        deleted = start + cabin_ttl + EXPIRY_GRACE - timedelta(seconds=extra)
        assert billing.billed_hours(start, deleted) <= reserved_hours


@pytest.mark.parametrize("ttl_s", [21 * 60, 51 * 60, 3600])
def test_a_late_reaper_pass_can_bill_one_started_hour_more_than_was_reserved_and_no_more(ttl_s):
    from datetime import UTC, datetime
    start = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    cabin_ttl = timedelta(seconds=ttl_s) + pl.SETUP_MARGIN
    reserved_hours = math.ceil((cabin_ttl + EXPIRY_GRACE).total_seconds() / 3600)
    latest = start + cabin_ttl + EXPIRY_GRACE
    late = [billing.billed_hours(start, latest + timedelta(seconds=s)) - reserved_hours for s in range(0, 11 * 60 + 1, 30)]
    assert max(late) <= 1                              # one pass (10 minutes) and its slack can cross at most one hour boundary


def test_the_recorded_cost_of_a_finished_guest_is_within_what_it_reserved(rig):
    doc = rig.run_tour()
    record = rig.result_record(doc["id"], "fix-auth")
    attempt = rig.attempts(doc["id"])[0]
    assert Decimal(str(record["verified"]["cost_eur"])) <= Decimal(attempt["reserved_eur"])
    assert float(record["verified"]["cost_eur"]) > 0


def test_a_tour_booking_makes_room_by_deleting_a_sealed_held_cabin_when_the_cap_is_full(rig):
    world = rig.world
    world.cfg = make_config(max_concurrent=1)
    held = world.checkin(world.controller(uid=rig.alice.uid), task="idle", owner="alice")
    rec = world.store.load(held["lease_id"])
    rec["admission"] = {"sealed_at": iso(world.clock()), "verified_idle_seconds": 1900.0}
    world.store.save(rec)
    doc = rig.run_tour()
    assert doc["status"] == "done"
    assert world.store.load(held["lease_id"])["state"] == "released"
    assert held["server_id"] not in rig.servers_left()


def test_a_tour_booking_is_still_refused_when_the_only_cabin_is_not_sealed(rig):
    world = rig.world
    world.cfg = make_config(max_concurrent=1)
    busy = world.checkin(world.controller(uid=rig.alice.uid), task="busy", owner="alice")
    doc = rig.run_tour()
    assert doc["status"] != "done"
    assert world.store.load(busy["lease_id"])["state"] == "active"
