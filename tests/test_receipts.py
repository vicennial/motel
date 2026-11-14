"""``motel.receipts``: the stable receipt dicts and the stderr blocks, from record fixtures (no cloud, no clock)."""
import re
from datetime import UTC, datetime, timedelta

from motel import receipts
from motel.tours import supervisor

OFFER = {"server_type": "cpx32", "location": "fsn1", "vcpus": 4, "memory_gb": 8.0, "total_hourly_net_eur": "0.0318"}


def released(hours=1, seconds=300, **over):
    start = datetime(2026, 11, 8, 12, 0, tzinfo=UTC)
    view = {"state": "released", "created_at": "2026-11-08T12:00:00Z", "released_at": (start + timedelta(seconds=seconds)).strftime("%Y-%m-%dT%H:%M:%SZ"),
            "offer": OFFER, "billing": {"granularity": "started_hour", "billed_hours": hours},
            "cost_estimate_net_eur": f"{0.0318 * hours:.4f}"}
    view.update(over)
    return view


def test_the_terminal_tour_states_match_the_supervisors():
    assert receipts.TERMINAL_TOUR == supervisor.TERMINAL_TOUR


def test_a_cabin_without_an_offer_has_no_receipt():
    assert receipts.cabin_receipt({"state": "released"}) is None
    assert receipts.cabin_receipt({}) is None
    assert receipts.render_cabin_receipt(None) == []


def test_cabin_receipt_has_the_documented_keys_and_states_the_views_numbers():
    r = receipts.cabin_receipt(released())
    assert sorted(r) == ["basis", "billed_hours", "currency", "disk_gb", "estimated_total_net_eur", "final", "hourly_net_eur",
                         "location", "memory_gb", "server_type", "servers_left", "vcpus", "wall_seconds"]
    assert (r["wall_seconds"], r["billed_hours"], r["servers_left"], r["final"]) == (300, 1, 0, True)
    assert (r["hourly_net_eur"], r["estimated_total_net_eur"], r["currency"]) == ("0.0318", "0.0318", "EUR")


def test_a_live_cabin_uses_now_for_its_wall_time_and_is_not_final():
    r = receipts.cabin_receipt(released(state="active", released_at=None), datetime(2026, 11, 8, 12, 30, tzinfo=UTC))
    assert r["wall_seconds"] == 1800 and r["final"] is False and r["servers_left"] == 1


def test_the_block_lists_type_size_wall_time_hours_price_total_and_servers_left():
    lines = receipts.render_cabin_receipt(receipts.cabin_receipt(released(hours=2, seconds=3900)), "swift-gull-0123456789ab")
    text = "\n".join(lines)
    assert lines[0] == "motel: receipt for cabin swift-gull-0123456789ab (released)"
    for needle in ("cpx32 (4 vCPU, 8 GB RAM) in fsn1", "1h 05m 00s", "2 started hours", "EUR 0.0318/h net of VAT", "provider list price",
                   "est. total    EUR 0.0636 net", "servers left  0"):
        assert needle in text, needle


def test_the_block_leaves_out_what_it_does_not_know():
    view = released(offer={"server_type": "cpx32", "total_hourly_net_eur": "0.0318"})
    text = "\n".join(receipts.render_cabin_receipt(receipts.cabin_receipt(view)))
    assert "vCPU" not in text and "GB RAM" not in text and "cpx32" in text


def test_a_cabin_that_may_still_bill_is_not_reported_as_zero_servers():
    text = "\n".join(receipts.render_cabin_receipt(receipts.cabin_receipt(released(state="cleanup_pending", released_at=None))))
    assert "servers left  1" in text and "may still be billing" in text and "servers left  0" not in text


def guest(gid, *, hours=1, seconds=600, usd="0.5000", cost=None, cleanup="deleted", **over):
    cab = receipts.cabin_receipt(released(hours=hours, seconds=seconds))
    res = {"cost_eur": cost or f"{0.0318 * hours:.4f}", "usd": usd, "metered": True, "auth": "api-key", "cabin": cab}
    res.update(over)
    return {"id": gid, "status": "done", "cleanup": cleanup, "attempts": 1, "result": res}


def tour(*guests, status="done", spent="0.0636"):
    return {"id": "t-3fa91c0b72", "status": status, "guests": list(guests), "budget": {"max_eur": "5.0", "reserved_eur": "0", "spent_eur": spent}}


def test_a_tour_receipt_has_a_line_per_guest_and_totals_for_servers_and_model_use():
    r = receipts.tour_receipt(tour(guest("a", usd="0.5"), guest("b", hours=2, usd="0.25"), spent="0.0954"))
    assert r["final"] is True and r["servers_left"] == 0 and r["currency"] == "EUR"
    assert [g["id"] for g in r["guests"]] == ["a", "b"] and r["guests"][1]["billed_hours"] == 2
    assert r["guests"][0]["server_type"] == "cpx32" and r["guests"][0]["model_usd"] == "0.5000"
    assert r["servers"] == {"estimated_total_net_eur": "0.0954", "billed_hours": 3, "max_eur": "5.0000"}
    assert r["model"] == {"usd": "0.7500", "metered_guests": 2, "unmetered_guests": 0}


def test_the_tour_block_shows_the_table_the_totals_and_servers_left():
    text = "\n".join(receipts.render_tour_receipt(receipts.tour_receipt(tour(guest("fix-auth"), guest("lint", usd="0"))), "t-3fa91c0b72"))
    assert text.splitlines()[0] == "motel: receipt for tour t-3fa91c0b72"
    assert re.search(r"fix-auth\s+cpx32 \(4 vCPU, 8 GB RAM\)\s+10m 00s\s+1\s+EUR 0\.0318\s+USD 0\.5000", text)
    assert "servers       EUR 0.0636 net (2 started hours; provider list price, net of VAT)" in text
    assert "model use     USD 0.5000 through the concierge (2 guests metered)" in text
    assert "servers left  0" in text


def test_an_own_login_guest_is_reported_as_not_metered_and_adds_no_spend():
    own = guest("mine", auth="own-login", metered=False, usd=None)
    r = receipts.tour_receipt(tour(own, guest("b")))
    assert r["model"] == {"usd": "0.5000", "metered_guests": 1, "unmetered_guests": 1}
    text = "\n".join(receipts.render_tour_receipt(r))
    assert "not metered" in text and "1 guest(s) on your own login are not metered" in text


def test_servers_left_counts_guests_whose_cabin_may_still_exist():
    r = receipts.tour_receipt(tour(guest("a"), guest("b", cleanup="deletion_pending")))
    assert r["servers_left"] == 1
    text = "\n".join(receipts.render_tour_receipt(r))
    assert "servers left  1 (a server may still be billing" in text and "servers left  0" not in text


def test_a_guest_that_never_got_a_server_is_listed_without_one():
    never = {"id": "z", "status": "cancelled", "cleanup": "deleted", "attempts": 0, "result": None}
    r = receipts.tour_receipt(tour(never, status="cancelled", spent="0"))
    assert r["guests"][0]["server_type"] is None and r["model"]["usd"] is None
    assert "no server booked" in "\n".join(receipts.render_tour_receipt(r))


def test_a_tour_that_is_still_running_is_not_final():
    assert receipts.tour_receipt(tour(guest("a"), status="running"))["final"] is False


def test_the_servers_total_is_the_budgets_settled_spend_so_a_retry_is_counted():
    r = receipts.tour_receipt(tour(dict(guest("a"), attempts=2), spent="0.0636"))
    assert r["servers"]["estimated_total_net_eur"] == "0.0636"
    assert "(2 attempts)" in "\n".join(receipts.render_tour_receipt(r))
