"""The cabin receipt: the controller's own started-hour numbers, stated once under the stable `receipt` key."""
import pytest
from cabin_fakes import World

from motel import billing
from motel.cabins.util import parse_iso
from motel.receipts import cabin_receipt


@pytest.fixture
def world(tmp_path):
    return World(str(tmp_path))


def test_the_offer_carries_the_server_size_the_provider_reported(world):
    view = world.checkin(world.controller())
    assert view["offer"]["vcpus"] == 8 and view["offer"]["memory_gb"] == 16.0
    assert view["receipt"]["vcpus"] == 8 and view["receipt"]["memory_gb"] == 16.0 and view["receipt"]["server_type"] == "cpx52"


def test_a_released_cabin_receipt_follows_the_controllers_started_hour_numbers(world):
    c = world.controller()
    view = world.checkin(c)
    world.at_minute(5)
    done = c.checkout(view["lease_id"])
    r = done["receipt"]
    assert r["final"] is True and r["servers_left"] == 0
    assert r["billed_hours"] == done["billing"]["billed_hours"] == 1 and r["basis"] == "started_hours"
    assert r["estimated_total_net_eur"] == done["cost_estimate_net_eur"]          # the receipt states the view's number, never its own
    assert r["hourly_net_eur"] == done["offer"]["total_hourly_net_eur"]
    created, released = parse_iso(done["created_at"]), parse_iso(done["released_at"])
    assert r["wall_seconds"] == int((released - created).total_seconds())
    assert r["currency"] == "EUR" and r["location"] == done["offer"]["location"]


def test_sixty_five_minutes_bill_two_started_hours_in_the_receipt(world):
    c = world.controller()
    view = world.checkin(c)
    world.at_minute(65)
    done = c.checkout(view["lease_id"])
    r = done["receipt"]
    hourly = float(done["offer"]["total_hourly_net_eur"])
    assert r["billed_hours"] == 2 and float(r["estimated_total_net_eur"]) == pytest.approx(2 * hourly, abs=1e-3)
    assert r["wall_seconds"] >= 65 * 60 - 60


def test_a_cabin_that_is_not_released_says_one_server_is_left(world):
    c = world.controller()
    view = world.checkin(c)
    world.at_minute(10)
    r = c.view(world.store.load(view["lease_id"]))["receipt"]
    assert r["final"] is False and r["servers_left"] == 1 and r["wall_seconds"] == pytest.approx(10 * 60, abs=60)


def test_a_provider_without_hourly_billing_gets_a_prorated_receipt_with_no_hours(world):
    c = world.controller(billing_rule=billing.Rule(billing.UNKNOWN, "", "test"))
    view = world.checkin(c)
    world.at_minute(30)
    r = c.view(world.store.load(view["lease_id"]))["receipt"]
    assert r["billed_hours"] is None and r["basis"] == "prorated"
    assert float(r["estimated_total_net_eur"]) == pytest.approx(float(view["offer"]["total_hourly_net_eur"]) * 0.5, abs=1e-3)


def test_the_extra_disk_is_named_and_priced_into_the_hourly_total(world):
    c = world.controller()
    view = world.checkin(c, disk_gb=100)
    assert view["receipt"]["disk_gb"] == 100
    assert float(view["receipt"]["hourly_net_eur"]) == pytest.approx(float(view["offer"]["total_hourly_net_eur"]))
    assert float(view["offer"]["disk_hourly_net_eur"]) > 0


def test_a_checkout_after_the_server_may_still_exist_does_not_claim_zero_servers(world):
    rec = world.store.load(world.checkin(world.controller())["lease_id"])
    c = world.controller()
    pending = dict(c.view(rec), state="cleanup_pending")
    assert cabin_receipt(pending)["servers_left"] == 1 and cabin_receipt(pending)["final"] is False

