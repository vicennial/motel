"""Safety-core tests ported from the original private implementation and adapted to the cabin layout."""
import json
import os
import threading
from datetime import timedelta

import pytest
from cabin_fakes import HOST, World, activity, make_config, pubkey

from motel.cabins.errors import ApiError, Busy, Fail, GuardRefused, PolicyRefused
from motel.cabins.provider import GuardedApi

LABELS = {"managed-by": "motel", "role": "cabin", "controller": str(HOST)}


@pytest.fixture
def world(tmp_path):
    return World(str(tmp_path))


def deletes(world):
    return [w for w in world.hz.writes if w[0] == "DELETE"]


# ---- tampered records and forged provider state

def test_tampered_record_naming_the_host_never_writes_to_it(world):
    c = world.controller()
    view = world.checkin(c)
    rec = world.store.load(view["lease_id"])
    rec["server_id"] = HOST
    world.store.save(rec)
    out = c.checkout(view["lease_id"])
    assert out["state"] == "cleanup_pending"
    assert not [w for w in deletes(world) if f"/servers/{HOST}" in w[1]]


def test_tampered_record_naming_a_foreign_server_never_deletes_it(world):
    c = world.controller()
    view = world.checkin(c)
    foreign = world.hz.add_server("motel-foreign-aaaaaaaaaaaa",
                                  dict(LABELS, lease="foreign-aaaaaaaaaaaa", intent="2" * 32))
    rec = world.store.load(view["lease_id"])
    rec["server_id"] = foreign
    world.store.save(rec)
    c.checkout(view["lease_id"])
    assert foreign in world.hz.servers


def test_forged_provider_labels_block_deletion(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.servers[view["server_id"]]["labels"]["intent"] = "f" * 32
    out = c.checkout(view["lease_id"])
    assert out["state"] == "cleanup_pending" and view["server_id"] in world.hz.servers


def test_renamed_resource_blocks_deletion(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.servers[view["server_id"]]["name"] = "something-else"
    assert c.checkout(view["lease_id"])["state"] == "cleanup_pending"


def test_protected_resources_are_never_deleted(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.servers[view["server_id"]]["protection"]["delete"] = True
    assert c.checkout(view["lease_id"])["state"] == "cleanup_pending"
    assert view["server_id"] in world.hz.servers


def test_extra_firewall_attachment_or_rules_are_refused(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.firewalls[view["firewall_id"]]["rules"].append(
        {"direction": "in", "protocol": "tcp", "port": "22", "source_ips": ["0.0.0.0/0"]})
    with pytest.raises(GuardRefused, match="inbound rules"):
        c.hz.firewall(world.store.load(view["lease_id"]))
    # Exposure problems block create/extend but never block deleting our own resources.
    assert c.checkout(view["lease_id"])["state"] == "released"


def test_unexpected_attachment_to_the_server_is_refused(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.servers[view["server_id"]]["private_net"] = [{"network": 1}]
    with pytest.raises(GuardRefused, match="attached resource"):
        c.hz.server(world.store.load(view["lease_id"]))


def test_project_is_rechecked_for_every_write(world):
    api = GuardedApi(world.hz, HOST)
    c = world.controller(hetzner=api)
    view = world.checkin(c)
    before = len(world.hz.reads)
    world.hz.on_write = None
    c.extend(view["lease_id"], timedelta(hours=3))
    assert len(world.hz.reads) > before
    world.hz.add_server("production-web", {})
    with pytest.raises(Fail):
        c.extend(view["lease_id"], timedelta(hours=4))


def test_creation_publishes_expiry_with_the_first_provider_write(world):
    world.checkin()
    labels = [w[2]["labels"] for w in world.hz.writes if w[0] == "POST"]
    assert all("expires" in x and x["expires"].endswith("Z") for x in labels)
    assert not [w for w in world.hz.writes if w[0] == "PUT"]          # no extra label write needed


def test_no_generic_write_surface(world):
    api = GuardedApi(world.hz, HOST)
    public = {n for n in dir(api) if not n.startswith("_")}
    assert not {"post", "put", "delete", "request", "write", "transport"} & public


def test_create_has_no_caller_controlled_attachment_and_no_public_ipv6(world):
    world.checkin()
    body = next(w[2] for w in world.hz.writes if w[1] == "/servers")
    assert body["public_net"] == {"enable_ipv4": True, "enable_ipv6": False}
    assert set(body) <= {"name", "server_type", "location", "image", "labels", "user_data", "firewalls", "public_net",
                         "start_after_create", "volumes", "automount"}


# ---- reconciliation

def test_ambiguous_failure_with_nothing_created_retries_once(world):
    world.hz.fail[("POST", "/servers")] = [ApiError(503, "x")]
    assert world.checkin()["state"] == "active"
    assert len(world.hz.servers) == 1


def test_repeated_ambiguity_aborts_and_cleans_up(world):
    world.hz.fail[("POST", "/servers")] = [ApiError(503, "x"), ApiError(503, "x")]
    with pytest.raises(Fail):
        world.checkin()
    assert not world.hz.servers and not world.hz.firewalls


def test_definitive_client_error_is_not_retried(world):
    world.hz.fail[("POST", "/servers")] = [ApiError(422, "invalid_input"), ApiError(422, "invalid_input")]
    with pytest.raises(Fail):
        world.checkin()
    assert len([w for w in world.hz.writes if w[:2] == ("POST", "/servers")]) == 1


def test_crash_after_server_create_is_recovered_by_intent_and_destroyed(world):
    """Simulate a process killed after the provider created the server but before the record learned its ID."""
    from motel.cabins.provider import egress_rules, ingress_rules
    c = world.controller()
    rec = c._intent("t", "alice", timedelta(hours=1), c.offer("cpx32", timedelta(hours=1)), pubkey("U"),
                    {"checked_at": None}, None)
    fw = c.hz.create_firewall(rec, egress_rules(c.cfg.dns_resolvers, c.excluded_ipv4()) + ingress_rules(rec["access"]))
    rec.update(firewall_id=fw["firewall"]["id"], state="creating_server")
    rec["cleanup"]["firewall"] = "present"
    world.store.save(rec)
    c.hz.create_server(rec, server_type="cpx32", location="fsn1", image=161547269, user_data="x")
    assert rec["server_id"] is None and len(world.hz.servers) == 1       # created at the provider, unrecorded
    world.clock.advance(minutes=30)
    result = world.controller().reap(apply=True)
    assert result["actions"][0]["state"] == "released"
    assert not world.hz.servers and not world.hz.firewalls


def test_stale_bootstrap_record_is_reconciled_by_reaper(world):
    c = world.controller()
    rec = c._intent("t", "alice", timedelta(hours=1), c.offer("cpx32", timedelta(hours=1)), pubkey("U"),
                    {"checked_at": None}, None)
    rec["state"] = "creating_firewall"
    world.store.save(rec)
    assert c.reap(apply=True)["actions"] == []
    world.clock.advance(minutes=25)
    assert c.reap(apply=True)["actions"][0]["reason"] == "bootstrap_deadline"


# ---- capacity and concurrency

def test_concurrent_checkins_reserve_only_the_remaining_slots(tmp_path):
    w = World(str(tmp_path))
    w.cfg = make_config(max_concurrent=2)
    results, errors = [], []

    def go(i):
        try:
            results.append(w.checkin(w.controller(), task=f"t{i}"))
        except PolicyRefused as exc:
            errors.append(exc)
    threads = [threading.Thread(target=go, args=(i,)) for i in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert len(results) == 2 and len(errors) == 3
    assert len({r["lease_id"] for r in results}) == 2 and len(w.hz.servers) == 2


def test_unreadable_record_still_consumes_a_slot(tmp_path):
    w = World(str(tmp_path))
    w.cfg = make_config(max_concurrent=1)
    with open(os.path.join(w.store.leases, "broken-aaaaaaaaaaaa.json"), "w") as handle:
        handle.write("{")
    with pytest.raises(PolicyRefused, match="no vacancy"):
        w.checkin()


def test_only_released_frees_capacity(tmp_path):
    w = World(str(tmp_path))
    w.cfg = make_config(max_concurrent=1)
    c = w.controller()
    first = w.checkin(c)
    c.checkout(first["lease_id"])
    assert w.checkin(c, task="second")["state"] == "active"


def test_locked_lease_is_reported_busy_by_the_reaper(world):
    c = world.controller()
    view = world.checkin(c)
    world.clock.advance(hours=3)
    with world.store.lease_lock(view["lease_id"], blocking=True):
        result = world.controller().reap(apply=True)
    assert result["busy"] == [view["lease_id"]] and world.hz.servers


def test_checkout_while_locked_is_busy(world):
    c = world.controller()
    view = world.checkin(c)
    with world.store.lease_lock(view["lease_id"], blocking=True), pytest.raises(Busy):
        c.checkout(view["lease_id"])


def test_release_and_reap_race_deletes_once(world):
    c = world.controller()
    view = world.checkin(c)
    world.clock.advance(hours=3)
    c.checkout(view["lease_id"])
    world.controller().reap(apply=True)
    assert len([w for w in deletes(world) if w[1].startswith("/servers/")]) == 1


# ---- idle sealing behaviour of the controller

def test_lost_seal_reply_leaves_the_guest_sealed_and_the_next_pass_completes(world):
    c = world.controller()
    view = world.checkin(c)
    world.guest.activity_docs = [activity(idle_s=1900)]
    world.at_minute(50)
    world.guest.seal_docs = [GuestUnknownError()]
    first = c.reap(apply=True)
    assert first["actions"] == [] and any("seal not confirmed" in w for w in first["warnings"])
    world.guest.seal_docs = [{"schema": "motel-guest/v1", "sealed": True, "already": True,
                              "verified_idle_seconds": 1900.0}]
    second = c.reap(apply=True)
    assert second["actions"][0]["reason"] == "idle" and view["lease_id"]


def GuestUnknownError():
    from motel.cabins.errors import GuestUnknown
    return GuestUnknown("reply lost")


def test_seal_recorded_before_interrupted_cleanup_finishes_without_new_checks(world):
    c = world.controller()
    view = world.checkin(c)
    world.guest.activity_docs = [activity(idle_s=1900)]
    world.at_minute(50)
    world.hz.fail[("DELETE", f"/servers/{view['server_id']}")] = [ApiError(503, "x")]
    first = c.reap(apply=True)
    assert first["actions"][0]["state"] == "cleanup_pending"
    assert world.store.load(view["lease_id"])["admission"]["sealed_at"]
    calls = len(world.guest.calls)
    second = c.reap(apply=True)
    assert second["actions"][0]["state"] == "released" and len(world.guest.calls) == calls


def test_expiry_in_grace_can_be_extended_but_not_after_grace(world):
    c = world.controller()
    view = world.checkin(c)
    world.clock.advance(hours=2, minutes=5)
    assert c.extend(view["lease_id"], timedelta(hours=1))["expired"] is False
    world.clock.advance(hours=1, minutes=11)
    with pytest.raises(PolicyRefused, match="past its expiry grace"):
        c.extend(view["lease_id"], timedelta(hours=1))


def test_status_released_lease_with_surviving_server_is_reported(world):
    c = world.controller(uid=0)
    view = world.checkin(c)
    rec = world.store.load(view["lease_id"])
    rec["state"] = "released"
    world.store.save(rec)
    doc = c.status()
    assert doc["orphans"] and "released but the resource still exists" in doc["orphans"][0]["note"]
    assert doc["verified_empty"] is False


def test_api_unavailable_is_not_an_empty_status(world):
    c = world.controller(uid=0)
    world.hz.unavailable = True
    doc = c.status()
    assert doc["provider"]["inventory"] == "unavailable" and doc["verified_empty"] is False


def test_complete_empty_inventory_is_verified_empty(world):
    doc = world.controller(uid=0).status()
    assert doc["verified_empty"] is True and doc["provider"]["inventory"] == "complete"


def test_json_documents_carry_the_schema(world):
    doc = world.controller().status()
    assert doc["schema"] == "motel/v1"
    json.dumps(doc)


# ---- tailnet node recovery (tailscale mode)

@pytest.fixture
def tsworld(tmp_path):
    return World(str(tmp_path), access="tailscale")


def test_absent_recorded_node_is_done(tsworld):
    c = tsworld.controller()
    view = tsworld.checkin(c)
    node = tsworld.store.load(view["lease_id"])["tailnet"]["device_id"]
    tsworld.ts.deleted.add(node)
    assert c.checkout(view["lease_id"])["cleanup"]["tailnet_node"] == "done"


def test_no_minted_key_means_no_node(tsworld):
    c = tsworld.controller()
    tsworld.ts.request = lambda *a, **k: (_ for _ in ()).throw(ApiError(403, "tailnet_api_error")) \
        if a[0] == "POST" and a[1].endswith("/keys") else FakeTs.request(tsworld.ts, *a, **k)
    with pytest.raises(Fail):
        tsworld.checkin(c)
    rec = tsworld.store.all()[0]
    assert rec["cleanup"]["auth_key"] == "not_created" and rec["state"] == "released"


class FakeTs:
    from cabin_fakes import FakeTailnet
    request = FakeTailnet.request


def test_unrecorded_node_is_recovered_only_when_unambiguous(tsworld):
    c = tsworld.controller()
    view = tsworld.checkin(c)
    rec = tsworld.store.load(view["lease_id"])
    node = rec["tailnet"]["device_id"]
    rec["tailnet"]["device_id"] = None
    tsworld.store.save(rec)
    out = c.checkout(view["lease_id"])
    assert out["state"] == "released" and node in tsworld.ts.deleted


def test_unrelated_node_in_the_enrollment_window_keeps_recovery_pending(tsworld):
    c = tsworld.controller()
    view = tsworld.checkin(c)
    rec = tsworld.store.load(view["lease_id"])
    rec["tailnet"]["device_id"] = None
    tsworld.store.save(rec)
    tsworld.ts.enrolled["nSTRANGER1"] = {"nodeId": "nSTRANGER1", "hostname": "someone-elses", "tags": [],
                                         "enabledRoutes": [], "advertisedRoutes": [], "addresses": ["100.90.0.9"],
                                         "created": tsworld.clock().strftime("%Y-%m-%dT%H:%M:%SZ")}
    out = c.checkout(view["lease_id"])
    assert out["state"] == "cleanup_pending" and "nSTRANGER1" not in tsworld.ts.deleted
