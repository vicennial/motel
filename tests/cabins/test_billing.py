"""Hourly billing for cabins: an idle cabin is sealed at once but deleted in the release window before the next billed
hour. TTL, lifetime, explicit checkout and a provider whose billing is not hourly are not touched."""
import json
import os
from datetime import timedelta

import pytest
from cabin_fakes import World, activity

from motel import billing
from motel.cabins.render import cabin_line, render_cabin
from motel.cabins.util import iso

IDLE = [activity(idle_s=1900)]


@pytest.fixture
def world(tmp_path):
    return World(str(tmp_path))


def server_deletes(world):
    return [w for w in world.hz.writes if w[0] == "DELETE" and w[1].startswith("/servers/")]


def idle_cabin(world, c=None, **kw):
    c = c or world.controller()
    view = world.checkin(c, **kw)
    world.guest.activity_docs = list(IDLE)
    return c, view


def sealed(world, view):
    return bool((world.store.load(view["lease_id"]).get("admission") or {}).get("sealed_at"))


# ---------------------------------------------------------------- idle: seal at once, delete in the window

def test_an_idle_cabin_is_sealed_at_once_and_held_until_its_release_window(world):
    c, view = idle_cabin(world)
    world.at_minute(31)
    result = c.reap(apply=True)
    assert sealed(world, view)                                   # no new work can start from now on
    assert (view["lease_id"], ("seal",)) in world.guest.calls
    assert result["actions"] == [] and not server_deletes(world)
    [held] = result["held"]
    assert held["lease"] == view["lease_id"] and held["reason"] == "idle"
    assert held["release_from"] == "2026-11-08T12:44:00Z" and held["release_by"] == "2026-11-08T12:54:00Z"
    assert held["paid_until"] == "2026-11-08T13:00:00Z" and held["billing_source"] == "provider"
    world.at_minute(41)                                          # a later pass, still before the window: no guest call, no delete
    calls = len(world.guest.calls)
    assert c.reap(apply=True)["actions"] == [] and len(world.guest.calls) == calls and world.hz.servers


def test_it_is_deleted_by_the_first_pass_inside_the_window(world):
    c, view = idle_cabin(world)
    world.at_minute(31)
    c.reap(apply=True)
    world.at_minute(45)
    result = c.reap(apply=True)
    assert [(a["reason"], a["state"]) for a in result["actions"]] == [("idle", "released")]
    assert not world.hz.servers and result["held"] == []


def test_a_pass_whose_successor_would_come_too_late_deletes_now(world):
    c, view = idle_cabin(world)
    world.at_minute(31)
    c.reap(apply=True)
    world.clock.now = world.clock.start + timedelta(minutes=43)          # next pass at 54:00 at the latest: in time, wait
    assert c.reap(apply=True)["actions"] == []
    world.clock.now = world.clock.start + timedelta(minutes=43, seconds=30)      # next pass would be at 54:30: too late
    assert c.reap(apply=True)["actions"][0]["state"] == "released"


def test_a_cabin_found_idle_in_the_last_minutes_of_the_hour_is_deleted_at_once(world):
    c, view = idle_cabin(world)
    world.at_minute(57)
    assert c.reap(apply=True)["actions"][0]["reason"] == "idle" and not world.hz.servers


def test_a_cabin_never_goes_into_a_new_billed_hour_when_a_pass_can_prevent_it(world):
    c, view = idle_cabin(world)
    for minute in range(31, 70, 10):                             # a pass every 10 minutes, as the timer does
        world.at_minute(minute)
        c.reap(apply=True)
        if not world.hz.servers:
            assert minute < 60
            return
    pytest.fail("the cabin was carried into the second billed hour")


def test_a_pass_that_missed_the_boundary_waits_for_the_next_hours_window(world):
    c, view = idle_cabin(world)
    world.at_minute(70)                                          # no pass ran in the first hour: the second is paid for
    assert c.reap(apply=True)["actions"] == [] and sealed(world, view)
    world.at_minute(105)
    assert c.reap(apply=True)["actions"][0]["reason"] == "idle"


def test_dry_run_reports_held_cabins_and_plans_only_what_the_pass_would_delete(world):
    c, view = idle_cabin(world)
    world.at_minute(31)
    plan = c.reap(apply=False)
    assert plan["planned"] == [] and [h["lease"] for h in plan["held"]] == [view["lease_id"]]
    assert not sealed(world, view)                               # a dry run seals nothing
    world.at_minute(50)
    plan = c.reap(apply=False)
    assert [p["reason"] for p in plan["planned"]] == ["idle"] and plan["held"] == []


def test_the_release_margin_is_configurable(world):
    world.cfg = world.cfg.__class__(**{**world.cfg.__dict__, "release_margin": timedelta(minutes=20)})
    c, view = idle_cabin(world)
    world.at_minute(31)                                          # window: minutes 30 to 40
    assert c.reap(apply=True)["actions"][0]["reason"] == "idle"


def test_a_busy_cabin_is_never_released_by_the_window(world):
    c, view = idle_cabin(world)
    world.guest.activity_docs = [activity(active=True)]
    world.at_minute(50)
    assert c.reap(apply=True)["actions"] == [] and world.hz.servers and not sealed(world, view)


# ---------------------------------------------------------------- promises: TTL, lifetime, checkout

def test_a_ttl_ending_before_the_window_is_not_stretched(world):
    c, view = idle_cabin(world, ttl=timedelta(minutes=40))
    world.at_minute(31)                                          # the next pass would come after the TTL: delete now
    assert c.reap(apply=True)["actions"][0]["reason"] == "idle" and not world.hz.servers


def test_a_held_cabin_is_deleted_before_a_ttl_that_ends_inside_the_hold(world):
    c, view = idle_cabin(world, ttl=timedelta(minutes=90))
    world.at_minute(31)
    c.reap(apply=True)
    assert world.hz.servers                                      # the window of hour 1 is the target: 44 to 54
    world.at_minute(61)                                          # missed it; hour 2's window (104-114) is past the TTL
    assert c.reap(apply=True)["actions"] == [] and world.hz.servers
    world.at_minute(80)                                          # the next pass (91:00) would be after the TTL (90:00)
    assert c.reap(apply=True)["actions"][0]["reason"] == "idle"


def test_a_sealed_cabin_is_never_kept_past_its_ttl_even_before_the_window(world):
    c, view = idle_cabin(world)
    world.at_minute(31)
    c.reap(apply=True)
    rec = world.store.load(view["lease_id"])
    rec["expires_at"] = iso(world.clock() - timedelta(minutes=1))       # the TTL has just passed
    world.store.save(rec)
    assert c.reap(apply=True)["actions"][0]["reason"] == "idle"


def test_a_busy_cabin_is_deleted_at_its_ttl_plus_grace_whatever_the_paid_hour(world):
    c = world.controller()
    world.checkin(c, ttl=timedelta(minutes=30))
    world.guest.activity_docs = [activity(active=True)]
    world.at_minute(39)
    assert c.reap(apply=True)["actions"] == []
    world.at_minute(41)                                          # 30 + 10 minutes grace; 19 minutes of the paid hour remain
    assert c.reap(apply=True)["actions"][0]["reason"] == "ttl" and not world.hz.servers


def test_the_maximum_lifetime_bounds_the_hold_too(world):
    c, view = idle_cabin(world)
    world.at_minute(31)
    c.reap(apply=True)
    rec = world.store.load(view["lease_id"])
    rec["lifetime_ends_at"] = iso(world.clock() + timedelta(minutes=5))
    world.store.save(rec)
    assert c.reap(apply=True)["actions"][0]["reason"] == "idle"


def test_an_explicit_checkout_deletes_at_once_inside_a_paid_hour(world):
    c = world.controller()
    view = world.checkin(c)
    world.at_minute(5)
    done = c.checkout(view["lease_id"])
    assert done["state"] == "released" and not world.hz.servers
    assert len(server_deletes(world)) == 1
    assert done["billing"]["billed_hours"] == 1                  # and the hour is not refunded


def test_checkout_of_a_sealed_held_cabin_is_immediate_too(world):
    c, view = idle_cabin(world)
    world.at_minute(31)
    c.reap(apply=True)
    assert c.checkout(view["lease_id"])["state"] == "released" and not world.hz.servers


# ---------------------------------------------------------------- unknown or non-hourly billing: no change

@pytest.mark.parametrize("granularity", [billing.UNKNOWN, billing.PER_SECOND])
def test_a_provider_without_hourly_billing_is_released_as_soon_as_it_is_sealed(world, granularity):
    rule = billing.Rule(granularity, "https://example.invalid/pricing", "test")
    c = world.controller(billing_rule=rule)
    view = world.checkin(c)
    world.guest.activity_docs = list(IDLE)
    world.at_minute(31)
    result = c.reap(apply=True)
    assert result["held"] == [] and result["actions"][0]["reason"] == "idle" and not world.hz.servers
    assert view["lease_id"]


def test_unknown_billing_keeps_the_old_fractional_estimate_and_shows_no_billed_hours(world):
    c = world.controller(billing_rule=billing.Rule(billing.UNKNOWN, "", "test"))
    view = world.checkin(c)
    world.at_minute(30)
    out = c.view(world.store.load(view["lease_id"]))
    assert out["billing"] == {"granularity": "unknown", "billed_hours": None, "paid_until": None,
                              "release_window": None, "source": None}
    hourly = float(view["offer"]["total_hourly_net_eur"])
    assert float(out["cost_estimate_net_eur"]) == pytest.approx(hourly * 0.5, abs=1e-3)


# ---------------------------------------------------------------- the clock: provider time, skew

def test_the_provider_created_and_arrival_times_are_recorded_and_the_earliest_is_the_anchor(world):
    c, view = idle_cabin(world)
    rec = world.store.load(view["lease_id"])
    assert rec["server_created_at"] == "2026-11-08T12:00:00Z" and rec["server_received_at"] == "2026-11-08T12:00:00Z"
    rec["server_created_at"] = "2026-11-08T11:57:00Z"            # the cloud says the server is 3 minutes older
    world.store.save(rec)
    world.at_minute(41)                                          # 44 minutes into the provider's hour: its window is open
    assert c.reap(apply=True)["actions"][0]["reason"] == "idle"


def test_a_cabin_the_lease_clock_would_still_hold_is_the_same_cabin_without_provider_times(world):
    c, view = idle_cabin(world)
    rec = world.store.load(view["lease_id"])
    for key in ("server_created_at", "server_received_at"):
        rec.pop(key)                                             # only the lease's own (earlier) time is known
    world.store.save(rec)
    world.at_minute(25)
    result = c.reap(apply=True)
    assert result["actions"] == [] and world.hz.servers and result["held"][0]["billing_source"] == "record"


def test_a_provider_clock_far_ahead_is_replaced_by_the_time_the_response_arrived(world):
    c, view = idle_cabin(world)
    rec = world.store.load(view["lease_id"])
    rec["server_created_at"] = iso(world.clock() + timedelta(hours=1))   # absurd: the provider clock is wrong
    world.store.save(rec)
    world.at_minute(31)
    result = c.reap(apply=True)
    assert result["held"][0]["billing_source"] == "received"
    assert result["held"][0]["paid_until"] == "2026-11-08T13:00:00Z"     # the arrival time anchors the hour


def test_a_provider_time_without_a_zone_is_ignored_not_trusted(world):
    c, view = idle_cabin(world)
    rec = world.store.load(view["lease_id"])
    rec["server_created_at"] = "2026-11-08T11:00:00"                     # no zone: could be any hour
    world.store.save(rec)
    world.at_minute(31)
    assert c.reap(apply=True)["held"][0]["billing_source"] == "received"


def test_a_provider_time_a_few_seconds_ahead_is_never_later_than_now(world):
    c, view = idle_cabin(world)
    rec = world.store.load(view["lease_id"])
    rec["server_created_at"] = iso(world.clock() + timedelta(seconds=20))
    world.store.save(rec)
    out = c.view(world.store.load(view["lease_id"]))
    assert out["billing"]["billed_hours"] == 1 and out["billing"]["paid_until"] == "2026-11-08T13:00:00Z"


def test_a_disk_created_before_the_server_moves_the_anchor_to_the_earlier_time(world):
    c = world.controller()
    view = world.checkin(c, disk_gb=10)
    rec = world.store.load(view["lease_id"])
    assert rec["volume_created_at"] and rec["volume_received_at"]
    rec["volume_created_at"] = "2026-11-08T11:56:00Z"                    # the disk is 4 minutes older than the server
    world.store.save(rec)
    world.guest.activity_docs = list(IDLE)
    world.at_minute(40)                                                  # 44 minutes into the disk's hour
    result = c.reap(apply=True)
    assert result["actions"][0]["reason"] == "idle" and not world.hz.servers


# ---------------------------------------------------------------- cost display

def test_the_estimate_counts_started_hours_from_creation(world):
    c = world.controller()
    view = world.checkin(c)
    hourly = float(view["offer"]["total_hourly_net_eur"])
    rec = lambda: world.store.load(view["lease_id"])  # noqa: E731
    world.at_minute(5)
    out = c.view(rec())
    assert out["billing"]["billed_hours"] == 1 and float(out["cost_estimate_net_eur"]) == pytest.approx(hourly, abs=1e-3)
    assert out["billing"]["paid_until"] == "2026-11-08T13:00:00Z"
    assert out["billing"]["release_window"] == {"opens": "2026-11-08T12:44:00Z", "closes": "2026-11-08T12:54:00Z"}
    world.at_minute(61)
    out = c.view(rec())
    assert out["billing"]["billed_hours"] == 2 and float(out["cost_estimate_net_eur"]) == pytest.approx(2 * hourly, abs=1e-3)


def test_a_released_cabins_estimate_stops_at_its_release(world):
    c = world.controller()
    view = world.checkin(c)
    world.at_minute(5)
    c.checkout(view["lease_id"])
    world.at_minute(200)
    out = c.view(world.store.load(view["lease_id"]))
    assert out["state"] == "released" and out["billing"]["billed_hours"] == 1 and out["billing"]["paid_until"] is None


def test_ls_status_and_checkout_text_state_the_billed_hours(world):
    c = world.controller()
    view = world.checkin(c)
    world.at_minute(65)
    out = c.view(world.store.load(view["lease_id"]))
    assert "est. EUR" in cabin_line(out) and "for 2 started hours" in cabin_line(out)
    text = render_cabin(out)
    assert "for 2 started hours" in text and "refunds nothing" in text and "paid until 2026-11-08T14:00:00Z" in text
    done = c.checkout(view["lease_id"])
    assert "refunds nothing" in render_cabin(done) and "paid until" not in render_cabin(done)


# ---------------------------------------------------------------- sealed cabins: extend, display, priority, deferral

def seal(world, lease_id, minute=None):
    """Mark a lease sealed (as the guest's `seal` and the reaper would have) without a reap pass."""
    rec = world.store.load(lease_id)
    rec["admission"] = {"sealed_at": iso(world.clock()), "verified_idle_seconds": 1900.0}
    world.store.save(rec)


def test_extend_on_a_sealed_cabin_is_refused_with_the_existing_refusal_shape(world):
    from motel.cabins.errors import PolicyRefused
    c, view = idle_cabin(world)
    world.at_minute(31)
    c.reap(apply=True)
    assert sealed(world, view)
    world.at_minute(33)
    before = world.store.load(view["lease_id"])["expires_at"]
    with pytest.raises(PolicyRefused) as exc:
        c.extend(view["lease_id"], timedelta(hours=3))
    assert exc.value.code == 3                                    # the same exit code as the other refusals
    assert "this cabin is idle and sealed; check in a new one" in exc.value.line()
    assert exc.value.hint == "motel cabin checkin"
    assert world.store.load(view["lease_id"])["expires_at"] == before
    assert not [w for w in world.hz.writes if w[0] == "PUT"]      # nothing was written at the provider


def test_extend_on_an_unsealed_cabin_still_works(world):
    c = world.controller()
    view = world.checkin(c)
    world.at_minute(5)
    assert c.extend(view["lease_id"], timedelta(hours=3))["expires_at"] > view["expires_at"]


def test_a_sealed_cabin_shows_when_it_will_be_deleted_in_ls_and_status(world):
    c, view = idle_cabin(world)
    world.at_minute(31)
    c.reap(apply=True)
    out = c.view(world.store.load(view["lease_id"]))
    assert out["sealed"] is True and out["release_by"] == "2026-11-08T12:54:00Z"
    assert "sealed (idle); deleted by 2026-11-08T12:54:00Z" in cabin_line(out)
    assert "sealed (idle); deleted by 2026-11-08T12:54:00Z" in render_cabin(out)
    unsealed = c.view(world.store.load(world.checkin(c)["lease_id"]))
    assert unsealed["sealed"] is False and "sealed" not in cabin_line(unsealed)


def test_a_sealed_cabin_past_its_window_says_the_next_reaper_pass(world):
    c, view = idle_cabin(world)
    world.at_minute(31)
    c.reap(apply=True)
    world.at_minute(57)
    out = c.view(world.store.load(view["lease_id"]))
    assert out["release_by"] is None and "sealed (idle); deleted at the next reaper pass" in cabin_line(out)


def test_the_release_by_shown_never_passes_the_ttl(world):
    c, view = idle_cabin(world, ttl=timedelta(minutes=50))
    seal(world, view["lease_id"])
    out = c.view(world.store.load(view["lease_id"]))
    assert out["release_by"] == "2026-11-08T12:50:00Z"


def test_sealed_leases_are_handled_first_so_slow_leases_cannot_carry_one_into_a_new_hour(world, monkeypatch):
    from motel.cabins import controller as ctl
    monkeypatch.setattr(ctl, "REAP_PASS_BUDGET", timedelta(seconds=30))
    c = world.controller()
    slow = [world.checkin(c, task=t)["lease_id"] for t in ("aaa", "bbb")]
    held = world.checkin(c, task="ccc")["lease_id"]
    for lid in slow:                                             # both past their TTL plus grace: due, and slow to delete
        rec = world.store.load(lid)
        rec["expires_at"] = iso(world.clock() - timedelta(hours=1))
        world.store.save(rec)
    seal(world, held)
    world.at_minute(50)                                          # the sealed lease is inside its release window
    world.hz.delete_action_gets = 10 ** 6                        # every delete is slow
    first = c.reap(apply=True)
    assert [a["lease"] for a in first["actions"]][0] == held
    assert world.store.load(held)["state"] in ("cleanup_pending", "released")
    assert held not in first["deferred"] and set(first["deferred"]) | {a["lease"] for a in first["actions"]} >= set(slow)


def test_the_priority_order_puts_sealed_first_then_due_then_pending_then_idle_candidates(world):
    c = world.controller()
    now = world.clock()
    sealed_rec = {"lease_id": "s", "state": "active", "admission": {"sealed_at": "x"}, "expires_at": iso(now + timedelta(hours=1))}
    due = {"lease_id": "d", "state": "active", "expires_at": iso(now - timedelta(hours=1))}
    live = {"lease_id": "l", "state": "active", "expires_at": iso(now + timedelta(hours=1))}
    pend = {"lease_id": "p", "state": "cleanup_pending", "cleanup": {"server": "pending"}}
    fw_only = {"lease_id": "f", "state": "cleanup_pending", "cleanup": {"server": "done"}}
    order = sorted([live, fw_only, due, pend, sealed_rec], key=lambda r: c._reap_priority(r, now))
    assert [r["lease_id"] for r in order] == ["s", "d", "p", "l", "f"]


def test_a_held_lease_that_is_deferred_by_the_budget_is_released_by_the_next_pass_in_its_window(world, monkeypatch):
    from motel.cabins import controller as ctl
    monkeypatch.setattr(ctl, "REAP_PASS_BUDGET", timedelta(seconds=30))
    c = world.controller()
    ids = [world.checkin(c, task=t)["lease_id"] for t in ("aaa", "bbb")]
    for lid in ids:
        seal(world, lid)
    world.at_minute(46)
    world.hz.delete_action_gets = 10 ** 6
    first = c.reap(apply=True)
    assert len(first["deferred"]) == 1 and first["deferred"][0] in ids          # one was reached, one was left
    left = first["deferred"][0]
    assert world.store.load(left)["state"] == "active" and sealed(world, {"lease_id": left})
    world.hz.delete_action_gets = 0
    world.at_minute(52)                                          # the next pass, still inside the window
    second = c.reap(apply=True)
    assert left in [a["lease"] for a in second["actions"]] and world.store.load(left)["state"] == "released"


# ---------------------------------------------------------------- vacancy: a held cabin makes room

@pytest.fixture
def small_world(tmp_path):
    from cabin_fakes import make_config
    return World(str(tmp_path), cfg=make_config(max_concurrent=2))


def test_a_checkin_that_would_find_no_vacancy_deletes_the_earliest_due_held_cabin_and_is_admitted(small_world):
    w = small_world
    c = w.controller()
    first = w.checkin(c, task="aaa")
    w.at_minute(5)
    second = w.checkin(c, task="bbb")
    seal(w, first["lease_id"])
    seal(w, second["lease_id"])                                  # the first one is due before the second
    w.at_minute(10)
    third = w.checkin(c, task="ccc", owner="bob")
    assert third["state"] == "active"
    assert w.store.load(first["lease_id"])["state"] == "released" and first["server_id"] not in w.hz.servers
    assert w.store.load(second["lease_id"])["state"] == "active" and second["server_id"] in w.hz.servers
    with open(os.path.join(w.store.root, "guestbook.jsonl"), encoding="utf-8") as handle:
        events = [json.loads(line) for line in handle if '"idle_evicted_for_checkin"' in line]
    assert len(events) == 1
    assert (events[0]["lease"], events[0]["evicted_task"], events[0]["evicted_owner"]) == (first["lease_id"], "aaa", "alice")
    assert (events[0]["for_task"], events[0]["for_owner"]) == ("ccc", "bob")


def test_without_a_sealed_cabin_the_cap_still_refuses(small_world):
    from motel.cabins.errors import PolicyRefused
    w = small_world
    c = w.controller()
    a, b = w.checkin(c, task="aaa"), w.checkin(c, task="bbb")
    with pytest.raises(PolicyRefused, match="no vacancy"):
        w.checkin(c, task="ccc")
    assert a["server_id"] in w.hz.servers and b["server_id"] in w.hz.servers


def test_a_held_cabin_whose_delete_is_still_pending_does_not_make_room(small_world):
    from cabin_fakes import ApiError

    from motel.cabins.errors import PolicyRefused
    w = small_world
    c = w.controller()
    a, _b = w.checkin(c, task="aaa"), w.checkin(c, task="bbb")
    seal(w, a["lease_id"])
    w.hz.fail[("DELETE", f"/servers/{a['server_id']}")] = [ApiError(403, "forbidden")]
    with pytest.raises(PolicyRefused, match="no vacancy"):
        w.checkin(c, task="ccc")
    assert w.store.load(a["lease_id"])["state"] == "cleanup_pending"


def test_a_busy_held_cabin_is_skipped_for_the_next_one(small_world):
    w = small_world
    c = w.controller()
    a, b = w.checkin(c, task="aaa"), w.checkin(c, task="bbb")
    seal(w, a["lease_id"])
    seal(w, b["lease_id"])
    with w.store.lease_lock(a["lease_id"]):                      # a reaper pass is working on the first one
        w.checkin(c, task="ccc")
    assert w.store.load(a["lease_id"])["state"] == "active" and w.store.load(b["lease_id"])["state"] == "released"


def test_an_unsealed_idle_candidate_is_never_taken_for_room(small_world):
    from motel.cabins.errors import PolicyRefused
    w = small_world
    c = w.controller()
    w.checkin(c, task="aaa")
    w.checkin(c, task="bbb")
    w.guest.activity_docs = list(IDLE)                           # idle, but not sealed: nothing is touched
    with pytest.raises(PolicyRefused):
        w.checkin(c, task="ccc")
