"""Lease lifecycle against the Hetzner fake: checkin, access modes, caps, disk, extend, checkout, reconcile."""
import json
import os
from datetime import timedelta

import pytest
from cabin_fakes import (
    HOST,
    HOST_IPV4,
    HOST_IPV6,
    SECRET_AUTHKEY,
    TAG,
    World,
    activity,
    pubkey,
)

from motel.cabins.errors import (
    ApiError,
    Fail,
    Guarded,
    GuestUnknown,
    NotFound,
    PolicyRefused,
    Prerequisite,
    Unavailable,
    Usage,
)

H = timedelta(hours=1)


def slurp(path):
    with open(path) as handle:
        return handle.read()


@pytest.fixture
def world(tmp_path):
    return World(str(tmp_path))


def firewall_in(world, view):
    return [r for r in world.hz.firewalls[view["firewall_id"]]["rules"] if r["direction"] == "in"]


# ---------------------------------------------------------------- ssh-allowlist (default)

def test_checkin_creates_firewall_server_and_returns_ssh_target(world):
    view = world.checkin()
    assert view["state"] == "active"
    assert view["access"]["mode"] == "ssh-allowlist"
    assert view["ssh"]["user"] == "cabin" and view["ssh"]["host"] == view["address"]
    assert view["address"].startswith("49.12.0.")
    assert view["lease_id"].startswith("installer-")
    server = world.hz.servers[view["server_id"]]
    assert server["labels"]["managed-by"] == "motel" and server["labels"]["role"] == "cabin"
    assert server["labels"]["controller"] == str(HOST) and server["labels"]["owner"] == "alice"
    assert server["public_net"]["ipv6"] is None                 # IPv4 only
    assert [f["id"] for f in server["public_net"]["firewalls"]] == [view["firewall_id"]]


def test_firewall_allows_only_exact_host_addresses_on_tcp22(world):
    view = world.checkin()
    rules = firewall_in(world, view)
    assert len(rules) == 1
    assert rules[0]["protocol"] == "tcp" and rules[0]["port"] == "22"
    assert rules[0]["source_ips"] == sorted([HOST_IPV4 + "/32", HOST_IPV6 + "/128"])
    assert not any(s.endswith("/0") or s in ("0.0.0.0", "::") for r in rules for s in r["source_ips"])


def test_ipv4_only_host_gets_only_a_v4_source(tmp_path):
    w = World(str(tmp_path), ipv6=False)
    rules = firewall_in(w, w.checkin())
    assert rules[0]["source_ips"] == [HOST_IPV4 + "/32"]


def test_no_firewall_or_server_is_created_before_the_intent_is_recorded(world):
    seen = []

    def on_write(method, path, body):
        recs = world.store.all()
        seen.append((method, path, [r["state"] for r in recs]))
    world.hz.on_write = on_write
    world.checkin()
    assert seen[0][:2] == ("POST", "/firewalls") and seen[0][2] == ["creating_firewall"]
    assert [p for _m, p, _s in seen if _m == "POST"] == ["/firewalls", "/servers"]


def test_user_data_pins_the_host_key_and_never_holds_provider_secrets(world):
    view = world.checkin()
    user_data = next(w[2]["user_data"] for w in world.hz.writes if w[1] == "/servers")
    assert "ed25519_private" in user_data and "cabin-guest-expiry.timer" in user_data
    assert "FAKE-ROOT-PASSWORD" not in user_data
    rec = world.store.load(view["lease_id"])
    assert rec["host_key"].startswith("ssh-ed25519 ")
    known = slurp(os.path.join(world.store.key_path(rec["lease_id"]), "known_hosts"))
    assert known.startswith(rec["name"] + " ssh-ed25519 ")


def test_private_material_and_root_password_are_not_in_the_lease_record(world):
    world.checkin()
    leases = "".join(slurp(os.path.join(world.store.leases, n)) for n in os.listdir(world.store.leases)
                     if n.endswith(".json"))
    ledger = slurp(os.path.join(world.store.root, "guestbook.jsonl"))
    for text in (leases, ledger):
        assert "PRIVATE KEY" not in text and "root_password" not in text and "FAKE-ROOT-PASSWORD" not in text


def test_input_validation(world):
    c = world.controller()
    with pytest.raises(Usage):
        world.checkin(c, task="Bad Task!")
    with pytest.raises(Usage):
        world.checkin(c, owner="bad owner")
    with pytest.raises(Usage):
        world.checkin(c, client_key="ssh-rsa AAAA")
    with pytest.raises(PolicyRefused, match="24h"):
        world.checkin(c, ttl=timedelta(hours=25))
    with pytest.raises(PolicyRefused, match="not allowed"):
        world.checkin(c, server_type="ccx63")
    assert world.hz.writes == []


def test_default_type_and_ttl_come_from_config(world):
    view = world.controller().checkin(task="t", owner="alice", client_key=pubkey("U"))
    assert view["offer"]["server_type"] == "cpx32"
    assert 7100 < view["time_left_s"] <= 7200


def test_price_cap_is_applied_on_live_price_including_ipv4(world):
    world.hz.types["cpx52"] = "0.1995"          # + 0.0008 IPv4 = 0.2003 > 0.2
    with pytest.raises(PolicyRefused, match="above the cap"):
        world.checkin()
    assert world.hz.writes == []


def test_unavailable_type_and_wrong_location_are_refused(world):
    world.hz.type_locations = [{"name": "nbg1"}]
    with pytest.raises(PolicyRefused, match="unavailable in"):
        world.checkin()


def test_missing_prerequisites_are_listed(tmp_path):
    w = World(str(tmp_path), ready=False)
    with pytest.raises(Prerequisite, match="not identified"):
        w.checkin()
    assert w.hz.writes == []


def test_receipt_must_match_the_installed_token(world):
    world.hashes["hetzner"] = "c" * 64
    with pytest.raises(Prerequisite, match="receipt"):
        world.checkin()


def test_host_identity_is_reverified_before_checkin(world):
    world.meta["instance-id"] = "42"
    with pytest.raises(Guarded, match="not the host"):
        world.checkin()
    world.meta["instance-id"] = str(HOST)
    world.meta["public-ipv4"] = "198.51.100.7"
    with pytest.raises(Guarded, match="address changed"):
        world.checkin()
    assert world.hz.writes == []


def test_checkin_refuses_when_the_token_can_see_the_host(world):
    world.hz.visible.add(HOST)
    with pytest.raises(Guarded):
        world.checkin()
    assert world.hz.writes == []


def test_checkin_refuses_in_a_project_with_foreign_servers(world):
    world.hz.add_server("production-web", {})
    with pytest.raises(Guarded, match="dedicated cabin project"):
        world.checkin()
    assert world.hz.writes == []


# ---------------------------------------------------------------- capacity

def test_capacity_counts_pending_cleanup_and_other_users(world):
    world.cfg = __import__("cabin_fakes").make_config(max_concurrent=2)
    c = world.controller()
    a = world.checkin(c)
    world.checkin(world.controller(uid=1001), task="other")
    with pytest.raises(PolicyRefused, match="no vacancy"):
        world.checkin(c, task="third")
    world.hz.fail[("DELETE", f"/servers/{a['server_id']}")] = [ApiError(503, "x")]
    c.checkout(a["lease_id"])          # leaves cleanup_pending: capacity still consumed
    with pytest.raises(PolicyRefused, match="no vacancy"):
        world.checkin(c, task="fourth")


# ---------------------------------------------------------------- disk

def test_disk_is_created_attached_priced_and_cleaned_up(world):
    c = world.controller()
    view = world.checkin(c, disk_gb=100)
    assert view["volume_id"] in world.hz.volumes
    assert world.hz.volumes[view["volume_id"]]["size"] == 100
    assert world.hz.volumes[view["volume_id"]]["server"] == view["server_id"]
    body = next(w[2] for w in world.hz.writes if w[1] == "/servers")
    assert body["volumes"] == [view["volume_id"]] and body["automount"] is False
    assert float(view["offer"]["disk_hourly_net_eur"]) > 0
    user_data = body["user_data"]
    assert f"scsi-0HC_Volume_{view['volume_id']}" in user_data
    out = c.checkout(view["lease_id"])
    assert out["state"] == "released"
    assert not world.hz.volumes and not world.hz.servers and not world.hz.firewalls
    kinds = [(m, p.split("/")[1]) for m, p, _b in world.hz.writes if m == "DELETE"]
    assert kinds == [("DELETE", "servers"), ("DELETE", "firewalls"), ("DELETE", "volumes")]


def test_disk_size_bounds(world):
    c = world.controller()
    for bad in (5, 600):
        with pytest.raises(PolicyRefused, match="--disk"):
            world.checkin(c, disk_gb=bad)


def test_disk_cost_counts_against_the_hourly_cap(world):
    world.cfg = __import__("cabin_fakes").make_config(max_eur_per_hour=0.17, max_disk_gb=10240)
    world.hz.volume_gb_month = "5.0"
    with pytest.raises(PolicyRefused, match="above the cap"):
        world.checkin(disk_gb=500)


def test_disk_not_mounted_is_a_failed_bootstrap_and_cleans_up(world):
    world.guest.ready_doc = {"schema": "motel-guest/v1", "bootstrap": "done", "tailnet_ip": None, "nft": "loaded",
                             "forwarding_disabled": True, "gate": "enforced", "disk": "failed",
                             "tcp_listeners": [{"addr": "0.0.0.0", "port": 22}]}
    with pytest.raises(Fail, match="extra disk"):
        world.checkin(disk_gb=50)
    assert not world.hz.servers and not world.hz.volumes and not world.hz.firewalls


def test_volume_creation_failure_leaves_nothing_behind(world):
    world.hz.fail[("POST", "/volumes")] = [ApiError(422, "invalid_input")]
    with pytest.raises(Fail):
        world.checkin(disk_gb=50)
    assert not world.hz.firewalls and not world.hz.volumes
    assert world.store.all()[0]["state"] == "released"


# ---------------------------------------------------------------- bootstrap and readiness

def test_bootstrap_failure_cleans_up_everything(world):
    world.guest.ready_doc = {"schema": "motel-guest/v1", "bootstrap": "done", "tailnet_ip": None, "nft": "missing",
                             "forwarding_disabled": True, "gate": "enforced", "disk": "none",
                             "tcp_listeners": [{"addr": "0.0.0.0", "port": 22}]}
    with pytest.raises(Fail, match="packet filter"):
        world.checkin()
    assert not world.hz.servers and not world.hz.firewalls
    rec = world.store.all()[0]
    assert rec["state"] == "released"


def test_unexpected_public_listener_fails_readiness(world):
    world.guest.ready_doc = {"schema": "motel-guest/v1", "bootstrap": "done", "tailnet_ip": None, "nft": "loaded",
                             "forwarding_disabled": True, "gate": "enforced", "disk": "none",
                             "tcp_listeners": [{"addr": "0.0.0.0", "port": 22}, {"addr": "0.0.0.0", "port": 8080}]}
    with pytest.raises(Fail, match="isolation check"):
        world.checkin()


def test_bootstrap_deadline_times_out_and_deletes(world):
    world.guest.ready_doc = {"schema": "motel-guest/v1", "bootstrap": "pending"}
    with pytest.raises(Fail, match="bootstrap deadline"):
        world.checkin()
    assert not world.hz.servers and not world.hz.firewalls


def test_firewall_not_yet_applied_is_retried_not_trusted(world):
    world.hz.firewall_apply_gets = 3
    view = world.checkin()
    assert view["state"] == "active"


# ---------------------------------------------------------------- reconciliation of lost responses

def test_lost_create_response_reconciles_by_unique_intent(world):
    world.hz.fail[("POST", "/servers")] = ["lost"]
    view = world.checkin()
    assert view["state"] == "active"
    assert len(world.hz.servers) == 1
    assert len([w for w in world.hz.writes if w[0] == "POST" and w[1] == "/servers"]) == 1


def test_lost_firewall_response_does_not_create_a_second_firewall(world):
    world.hz.fail[("POST", "/firewalls")] = ["lost"]
    world.checkin()
    assert len(world.hz.firewalls) == 1


def test_unrecorded_resources_are_reported_not_adopted(world):
    labels = {"managed-by": "motel", "role": "cabin", "controller": str(HOST), "lease": "ghost-aaaaaaaaaaaa",
              "intent": "1" * 32}
    ghost = world.hz.add_server("motel-ghost-aaaaaaaaaaaa", labels)
    c = world.controller(uid=0)
    status = c.status()
    assert [o["id"] for o in status["orphans"]] == [ghost]
    assert status["verified_empty"] is False
    reaped = c.reap(apply=True)
    assert ghost in world.hz.servers                       # never deleted without a root-owned intent
    assert reaped["orphans"] and reaped["actions"] == []


def test_ordinary_users_see_only_their_cabins_but_orphan_count_is_global(world):
    world.checkin(world.controller(uid=1000), task="mine")
    world.checkin(world.controller(uid=1001), task="theirs")
    labels = {"managed-by": "motel", "role": "cabin", "controller": str(HOST), "lease": "ghost-aaaaaaaaaaaa",
              "intent": "1" * 32}
    world.hz.add_server("motel-ghost-aaaaaaaaaaaa", labels)
    doc = world.controller(uid=1000).status()
    assert [c["task"] for c in doc["cabins"]] == ["mine"]
    assert doc["orphans"] == [] and doc["orphan_count"] == 1
    assert doc["global"]["live_or_pending_cabins"] == 2


def test_other_users_cabins_are_not_found(world):
    view = world.checkin(world.controller(uid=1000))
    other = world.controller(uid=1001)
    with pytest.raises(NotFound):
        other.status(view["lease_id"])
    with pytest.raises(NotFound):
        other.checkout(view["lease_id"])
    with pytest.raises(NotFound):
        other.extend(view["lease_id"], H)
    assert world.controller(uid=0).status(view["lease_id"])["cabins"]


# ---------------------------------------------------------------- extend

def test_extend_updates_provider_label_and_guest_guard(world):
    c = world.controller()
    view = world.checkin(c)
    world.clock.advance(minutes=30)
    out = c.extend(view["lease_id"], timedelta(hours=3))
    assert out["time_left_s"] == 3 * 3600
    label = (world.clock() + timedelta(hours=3)).strftime("%Y%m%dT%H%M%SZ")
    server = world.hz.servers[view["server_id"]]
    assert server["labels"]["expires"] == label
    assert (view["lease_id"], ("set-expiry", label)) in world.guest.calls


def test_extend_respects_limits(world):
    c = world.controller()
    view = world.checkin(c)
    with pytest.raises(PolicyRefused, match="already runs"):
        c.extend(view["lease_id"], timedelta(minutes=10))
    with pytest.raises(PolicyRefused, match="24h"):
        c.extend(view["lease_id"], timedelta(hours=25))
    for _ in range(12):
        try:
            c.extend(view["lease_id"], timedelta(hours=24))
        except PolicyRefused as exc:
            assert "maximum lifetime" in exc.message
            break
        world.clock.advance(hours=20)
    else:
        pytest.fail("max lifetime was never enforced")


def test_extend_failure_keeps_the_old_expiry(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.fail[("PUT", f"/servers/{view['server_id']}")] = [ApiError(503, "x")]
    with pytest.raises(Unavailable):
        c.extend(view["lease_id"], timedelta(hours=5))
    assert world.store.load(view["lease_id"])["expires_at"] == view["expires_at"]


def test_extend_survives_unreachable_guest_with_a_warning(world):
    c = world.controller()
    view = world.checkin(c)
    world.guest.fail = True
    out = c.extend(view["lease_id"], timedelta(hours=5))
    assert any("guest expiry guard" in w for w in out["warnings"])


# ---------------------------------------------------------------- checkout and cleanup

def test_checkout_verifies_absence_of_every_resource_and_is_idempotent(world):
    c = world.controller()
    view = world.checkin(c)
    out = c.checkout(view["lease_id"])
    assert out["state"] == "released" and set(out["cleanup"].values()) <= {"done", "not_created"}
    assert not world.hz.servers and not world.hz.firewalls and not world.hz.primary_ips
    assert c.checkout(view["lease_id"])["state"] == "released"
    assert not os.path.exists(os.path.join(world.store.keys, view["lease_id"]))


def deletes(world):
    return [w for w in world.hz.writes if w[0] == "DELETE"]


def elapsed(world, since):
    return (world.clock.now - since).total_seconds()


def test_checkout_waits_for_slow_server_deletion(world):
    world.hz.delete_delay_gets = 3
    c = world.controller()
    view = world.checkin(c)
    assert c.checkout(view["lease_id"])["state"] == "released"


def test_checkout_never_deletes_the_firewall_before_the_server_is_gone(world):
    world.hz.delete_delay_gets = 1000
    c = world.controller()
    view = world.checkin(c)
    out = c.checkout(view["lease_id"])
    assert out["state"] == "cleanup_pending"
    assert view["firewall_id"] in world.hz.firewalls
    assert not [w for w in world.hz.writes if w[0] == "DELETE" and "firewalls" in w[1]]
    assert out["cleanup"]["firewall"].startswith("pending")


def test_failed_cleanup_stays_visible_and_the_reaper_retries(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.fail[("DELETE", f"/servers/{view['server_id']}")] = [ApiError(503, "x")]
    out = c.checkout(view["lease_id"])
    assert out["state"] == "cleanup_pending"
    assert c.status()["verified_empty"] is False
    assert c.status(view["lease_id"])["cabins"][0]["state"] == "cleanup_pending"
    result = c.reap(apply=True)
    assert result["actions"][0]["state"] == "released"
    assert c.status()["verified_empty"] is True


def test_cleanup_refuses_all_writes_if_the_project_is_not_isolated(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.add_server("production-web", {})
    out = c.checkout(view["lease_id"])
    assert out["state"] == "cleanup_pending"
    assert not [w for w in world.hz.writes if w[0] == "DELETE"]


def test_primary_ip_without_auto_delete_is_deleted_by_its_recorded_id(world):
    world.hz.auto_delete_ip = False
    c = world.controller()
    view = world.checkin(c)
    ipid = world.store.load(view["lease_id"])["primary_ipv4"]["id"]
    out = c.checkout(view["lease_id"])
    assert out["state"] == "released" and not world.hz.primary_ips
    assert [w[1] for w in deletes(world) if "primary_ips" in w[1]] == [f"/primary_ips/{ipid}"]


def test_a_tampered_primary_ip_record_never_deletes_a_foreign_or_mismatched_address(world):
    c = world.controller()
    view = world.checkin(c)
    foreign = world.hz.add_server("motel-foreign-aaaaaaaaaaaa", {          # looks like a cabin: project stays isolated
        "managed-by": "motel", "role": "cabin", "controller": str(HOST), "lease": "foreign-aaaaaaaaaaaa",
        "intent": "2" * 32})
    fip = world.hz.servers[foreign]["public_net"]["ipv4"]
    rec = world.store.load(view["lease_id"])
    rec["primary_ipv4"] = {"id": fip["id"], "ip": fip["ip"]}            # assigned to a server that is not ours
    world.store.save(rec)
    out = c.checkout(view["lease_id"])
    assert out["cleanup"]["server"] == "done" and out["cleanup"]["firewall"] == "done"      # the guard under test
    assert out["state"] == "cleanup_pending" and "assigned to a foreign resource" in out["cleanup"]["primary_ip"]
    assert fip["id"] in world.hz.primary_ips and foreign in world.hz.servers
    rec["primary_ipv4"] = {"id": fip["id"], "ip": "198.51.100.9"}       # address differs from the recorded one
    world.store.save(rec)
    c.reap(apply=True)
    assert fip["id"] in world.hz.primary_ips
    assert not [w for w in deletes(world) if "primary_ips" in w[1]]


def test_checkin_failure_in_the_middle_leaves_cleanup_visible(world):
    world.hz.fail[("POST", "/servers")] = [ApiError(500, "server_error"), ApiError(500, "server_error")]
    with pytest.raises(Fail):
        world.checkin()
    assert not world.hz.firewalls
    assert world.store.all()[0]["state"] == "released"


# ---------------------------------------------------------------- reaper

def test_reap_dry_run_changes_nothing_and_reports_the_plan(world):
    c = world.controller()
    view = world.checkin(c)
    world.clock.advance(hours=2, minutes=11)
    plan = c.reap(apply=False)
    assert plan["planned"] == [{"lease": view["lease_id"], "reason": "ttl"}]
    assert world.hz.servers
    assert not [w for w in world.hz.writes if w[0] == "DELETE"]


def test_reap_deletes_after_ttl_plus_grace_even_if_busy(world):
    c = world.controller()
    world.checkin(c)
    world.guest.activity_docs = [activity(active=True)]
    world.clock.advance(hours=2, minutes=5)
    first = c.reap(apply=True)
    assert first["actions"] == [] and any("expired" in w for w in first["warnings"])
    assert world.hz.servers
    world.clock.advance(minutes=6)
    second = c.reap(apply=True)
    assert second["actions"][0]["reason"] == "ttl" and second["actions"][0]["state"] == "released"
    assert not world.hz.servers


def test_reap_warns_before_expiry(world):
    c = world.controller()
    world.checkin(c)
    world.guest.activity_docs = [activity(active=True)]
    world.clock.advance(hours=1, minutes=10)
    assert any("expires soon" in w for w in c.reap(apply=True)["warnings"])


def test_idle_cabin_is_sealed_then_deleted(world):
    c = world.controller()
    view = world.checkin(c)
    world.guest.activity_docs = [activity(idle_s=1900)]
    world.at_minute(50)                                  # inside the release window of the first billed hour
    result = c.reap(apply=True)
    assert result["actions"][0]["reason"] == "idle"
    assert result["actions"][0]["state"] == "released"
    assert not world.hz.servers
    assert (view["lease_id"], ("seal",)) in world.guest.calls


def test_busy_unknown_or_short_idle_is_never_reaped(world):
    c = world.controller()
    world.checkin(c)
    for doc in (activity(active=True), activity(idle_s=600), activity(unknown=["containers"], idle_s=5000),
                activity(age=900, idle_s=5000), GuestUnknown("nope")):
        world.guest.activity_docs = [doc]
        result = c.reap(apply=True)
        assert result["actions"] == [] and world.hz.servers, doc


def test_idle_dry_run_does_not_seal(world):
    c = world.controller()
    world.checkin(c)
    world.guest.activity_docs = [activity(idle_s=1900)]
    world.at_minute(50)
    plan = c.reap(apply=False)
    assert plan["planned"][0]["reason"] == "idle"
    assert not any(words == ("seal",) for _l, words in world.guest.calls)


def test_refused_or_unverified_seal_prevents_idle_deletion(world):
    c = world.controller()
    world.checkin(c)
    world.guest.activity_docs = [activity(idle_s=1900)]
    world.guest.seal_docs = [{"schema": "motel-guest/v1", "sealed": False, "reason": "busy: ssh_session"}]
    result = c.reap(apply=True)
    assert result["actions"] == [] and any("seal refused" in w for w in result["warnings"])
    world.guest.seal_docs = [{"schema": "motel-guest/v1", "sealed": True, "verified_idle_seconds": 60.0}]
    assert c.reap(apply=True)["actions"] == []
    assert world.hz.servers


def test_reap_removes_cabins_stuck_in_bootstrap_after_the_deadline(world):
    c = world.controller()
    rec = c._intent("t", "alice", H, c.offer("cpx32", H), pubkey("U"), {"checked_at": None}, None)
    rec["state"] = "bootstrapping"
    world.store.save(rec)
    assert c.reap(apply=True)["actions"] == []
    world.clock.advance(minutes=21)
    result = c.reap(apply=True)
    assert result["actions"][0]["reason"] == "bootstrap_deadline"
    assert result["actions"][0]["state"] == "released"


def test_unreadable_record_is_an_error_and_blocks_verified_empty(world):
    c = world.controller()
    with open(os.path.join(world.store.leases, "broken-aaaaaaaaaaaa.json"), "w") as handle:
        handle.write("{not json")
    assert c.reap(apply=True)["errors"]
    assert c.status()["verified_empty"] is False


def test_reap_without_a_provider_token_reports_incomplete_inventory(world):
    c = world.controller(hetzner=None)
    result = c.reap(apply=True)
    assert result["provider"]["inventory"] == "unavailable"


# ---------------------------------------------------------------- views, doctor

def test_view_reports_cost_and_deadlines(world):
    c = world.controller()
    view = world.checkin(c)
    world.clock.advance(hours=1)
    again = c.status(view["lease_id"])["cabins"][0]
    assert 3500 < again["time_left_s"] <= 3600 and again["expired"] is False
    assert again["destroy_after"].endswith("Z")
    assert float(again["cost_estimate_net_eur"]) > 0


def test_doctor_passes_on_a_healthy_setup_and_flags_pending_cleanup(world):
    c = world.controller()
    assert c.doctor()["ok"] is True
    world.hz.fail[("DELETE", f"/servers/{world.checkin(c)['server_id']}")] = [ApiError(503, "x")]
    c.checkout(world.store.all()[0]["lease_id"])
    report = c.doctor()
    assert report["ok"] is False
    assert any(x["check"] == "no cleanup-pending cabins" and not x["ok"] for x in report["checks"])


def test_lease_record_names_no_personal_data(world):
    view = world.checkin()
    rec = json.dumps(world.store.load(view["lease_id"]))
    assert TAG not in rec and SECRET_AUTHKEY not in rec


# ---- checkout waits for the provider (a delete action, firewalls and addresses that are still in use)

def test_firewall_in_use_for_n_polls_is_deleted_once_it_is_free(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.fail[("DELETE", f"/firewalls/{view['firewall_id']}")] = [ApiError(422, "resource_in_use")] * 3
    out = c.checkout(view["lease_id"])
    assert out["state"] == "released" and not world.hz.firewalls and not world.hz.primary_ips
    assert len([w for w in deletes(world) if "/firewalls/" in w[1]]) == 4       # three refusals, then the delete
    assert world.store.load(view["lease_id"])["errors"] == []


def test_locked_and_rate_limited_refusals_are_waited_out_too(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.fail[("DELETE", f"/firewalls/{view['firewall_id']}")] = [ApiError(423, "locked"),
                                                                      ApiError(429, "rate_limit_exceeded")]
    before = world.clock.now
    assert c.checkout(view["lease_id"])["state"] == "released"
    assert elapsed(world, before) >= 10         # a rate limit is never hammered


def test_the_server_delete_action_is_followed_before_the_firewall_and_address_are_touched(world):
    world.hz.delete_action_gets = 4
    c = world.controller()
    view = world.checkin(c)
    out = c.checkout(view["lease_id"])
    assert out["state"] == "released" and not world.hz.servers and not world.hz.firewalls
    kinds = [w[1].split("/")[1] for w in deletes(world)]
    assert kinds[0] == "servers" and "firewalls" in kinds
    assert [w for w in world.hz.writes if w[2] == "failed"] == []              # no refused attempt on the way
    assert len([r for r in world.hz.reads if r.startswith("/actions/")]) >= 5


def test_an_address_that_stays_assigned_after_its_server_is_waited_out(world):
    world.hz.ip_lag_gets = 3
    c = world.controller()
    view = world.checkin(c)
    out = c.checkout(view["lease_id"])
    assert out["state"] == "released" and not world.hz.primary_ips and not world.hz.firewalls


def test_a_server_delete_action_that_outlasts_the_budget_leaves_cleanup_pending_and_the_reaper_finishes(world):
    world.hz.delete_action_gets = 10 ** 6
    c = world.controller()
    view = world.checkin(c)
    before = world.clock.now
    out = c.checkout(view["lease_id"])
    assert out["state"] == "cleanup_pending" and out["cleanup"]["server"].startswith("pending")
    assert 100 <= elapsed(world, before) <= 200                                  # bounded
    assert not [w for w in deletes(world) if "/firewalls/" in w[1] or "primary_ips" in w[1]]
    assert c.status()["verified_empty"] is False
    world.hz.delete_action_gets = 0                                              # the provider recovers
    result = c.reap(apply=True)
    assert result["actions"][0]["state"] == "released" and not result["errors"]
    assert not world.hz.servers and not world.hz.firewalls and not world.hz.primary_ips
    assert c.status()["verified_empty"] is True


def test_a_firewall_that_stays_in_use_past_the_budget_leaves_cleanup_pending_then_the_reaper_finishes(world):
    c = world.controller()
    view = world.checkin(c)
    key = ("DELETE", f"/firewalls/{view['firewall_id']}")
    world.hz.fail[key] = [ApiError(422, "resource_in_use")] * 1000
    before = world.clock.now
    out = c.checkout(view["lease_id"])
    assert out["state"] == "cleanup_pending"
    assert out["cleanup"]["firewall"] == "pending: provider_unavailable:422:resource_in_use"
    assert out["cleanup"]["server"] == "done" and out["cleanup"]["primary_ip"] == "done"
    assert elapsed(world, before) <= 200
    world.hz.fail[key] = []
    result = c.reap(apply=True)
    assert result["actions"][0]["state"] == "released"
    assert not world.hz.firewalls and not world.hz.primary_ips and not world.hz.servers


def test_a_refusal_that_time_cannot_fix_is_not_retried(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.fail[("DELETE", f"/firewalls/{view['firewall_id']}")] = [ApiError(403, "forbidden")] * 5
    before = world.clock.now
    out = c.checkout(view["lease_id"])
    assert out["state"] == "cleanup_pending" and out["cleanup"]["firewall"].endswith("forbidden")
    assert len([w for w in world.hz.writes if "/firewalls/" in w[1]]) == 1
    assert elapsed(world, before) < 10


def test_a_failed_server_delete_action_is_reported_and_retried_by_the_reaper(world):
    world.hz.delete_action_fails = True
    c = world.controller()
    view = world.checkin(c)
    out = c.checkout(view["lease_id"])
    assert out["state"] == "cleanup_pending" and "action failed" in out["cleanup"]["server"]
    assert view["server_id"] in world.hz.servers and view["firewall_id"] in world.hz.firewalls    # left in place
    assert not [w for w in deletes(world) if "/firewalls/" in w[1] or "primary_ips" in w[1]]
    world.hz.delete_action_fails = False
    assert c.reap(apply=True)["actions"][0]["state"] == "released"
    assert not world.hz.servers and not world.hz.firewalls and not world.hz.primary_ips


def test_waiting_never_weakens_the_delete_guards(world):
    """A retry loop must not walk past the label guard: a forged server is never deleted, however long we wait."""
    world.hz.delete_action_gets = 3
    c = world.controller()
    view = world.checkin(c)
    world.hz.servers[view["server_id"]]["labels"]["intent"] = "f" * 32
    out = c.checkout(view["lease_id"])
    assert out["state"] == "cleanup_pending" and view["server_id"] in world.hz.servers
    assert not deletes(world)


# ---- review round 2: rate limits, provenance, 404 on retry, reaper budgets, stale snapshots

def recording(world):
    sleeps = []
    return sleeps, world.controller(sleep=lambda s: (sleeps.append(s), world.clock.sleep(s)))


def test_a_rate_limited_primary_ip_delete_waits_at_least_ten_seconds(world):
    sleeps, c = recording(world)
    view = world.checkin(c)
    del sleeps[:]                                  # bootstrap polling sleeps 10 s itself: only cleanup counts
    ipid = world.store.load(view["lease_id"])["primary_ipv4"]["id"]
    world.hz.primary_ips[ipid]["auto_delete"] = False
    world.hz.fail[("DELETE", f"/primary_ips/{ipid}")] = [ApiError(429, "rate_limit_exceeded")]
    assert c.checkout(view["lease_id"])["state"] == "released"
    assert max(sleeps) >= 10 and not world.hz.primary_ips


def test_a_rate_limited_action_poll_waits_at_least_ten_seconds(world):
    sleeps, c = recording(world)
    view = world.checkin(c)
    del sleeps[:]
    original, seen = world.hz._get, []

    def limited(path):
        if path.startswith("/actions/") and not seen:
            seen.append(path)
            raise ApiError(429, "rate_limit_exceeded")
        return original(path)
    world.hz._get = limited
    assert c.checkout(view["lease_id"])["state"] == "released"
    assert seen and max(sleeps) >= 10


def test_a_rate_limited_read_during_cleanup_is_waited_out_not_an_error(world):
    sleeps, c = recording(world)
    view = world.checkin(c)
    del sleeps[:]
    world.hz.fail[("GET", f"/firewalls/{view['firewall_id']}")] = [ApiError(429, "rate_limit_exceeded")]
    out = c.checkout(view["lease_id"])
    assert out["state"] == "released" and 10 in sleeps and world.store.load(view["lease_id"])["errors"] == []


def test_a_read_that_stays_rate_limited_past_the_budget_leaves_cleanup_pending(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.fail[("GET", f"/firewalls/{view['firewall_id']}")] = [ApiError(429, "rate_limit_exceeded")] * 1000
    before = world.clock.now
    out = c.checkout(view["lease_id"])
    assert out["state"] == "cleanup_pending" and out["cleanup"]["firewall"].startswith("pending")
    assert elapsed(world, before) <= 200


def provenance_api(world, view, **over):
    from motel.cabins.provider import GuardedApi
    rec = world.store.load(view["lease_id"])
    rec.update(over)
    return GuardedApi(world.hz, HOST), rec


def gone_server_with_a_lingering_ip(world):
    """The server is deleted but its address (auto_delete off) is left unassigned: the state in which the delete
    guard, not the "server still exists" wait, decides."""
    view = world.checkin()
    rec = world.store.load(view["lease_id"])
    world.hz.primary_ips[rec["primary_ipv4"]["id"]]["auto_delete"] = False
    world.hz._remove_server(view["server_id"])
    return view, rec, world.hz.primary_ips[rec["primary_ipv4"]["id"]]


def refused(call, rec):
    """The guard's own refusal: a GuardRefused that is not the transient Pending."""
    from motel.cabins.errors import GuardRefused, Pending
    with pytest.raises(GuardRefused) as caught:
        call(rec)
    assert not isinstance(caught.value, Pending), "refused only because the server still exists"
    return str(caught.value)


def test_primary_ip_guard_accepts_full_provenance_once_the_server_is_gone(world):
    view, rec, ip = gone_server_with_a_lingering_ip(world)
    api, rec = provenance_api(world, view)
    api.delete_primary_ip(rec)
    assert ip["id"] not in world.hz.primary_ips


@pytest.mark.parametrize("tamper", [
    lambda r: {"primary_ipv4": {"id": r["primary_ipv4"]["id"], "ip": None}},
    lambda r: {"primary_ipv4": {"id": r["primary_ipv4"]["id"]}},
    lambda r: {"primary_ipv4": {"id": r["primary_ipv4"]["id"], "ip": "not-an-ip"}},
    lambda r: {"primary_ipv4": {"id": r["primary_ipv4"]["id"], "ip": "2001:db8::1"}},
    lambda r: {"primary_ipv4": {"id": None, "ip": r["primary_ipv4"]["ip"]}},
    lambda r: {"primary_ipv4": None},
    lambda r: {"server_id": None},
], ids=["ip-none", "ip-missing", "ip-garbage", "ip-v6", "id-none", "no-record", "server-id-none"])
def test_primary_ip_guard_fails_closed_without_full_provenance(world, tamper):
    view, rec, ip = gone_server_with_a_lingering_ip(world)
    api, rec = provenance_api(world, view)
    api, bad = provenance_api(world, view, **tamper(rec))
    refused(api.primary_ip, bad)
    refused(api.delete_primary_ip, bad)
    assert ip["id"] in world.hz.primary_ips and not [w for w in deletes(world) if "primary_ips" in w[1]]


def test_the_missing_provenance_refusal_says_how_to_clean_up(world):
    view, rec, _ = gone_server_with_a_lingering_ip(world)
    api, bad = provenance_api(world, view, primary_ipv4={"id": rec["primary_ipv4"]["id"], "ip": None})
    message = refused(api.delete_primary_ip, bad)
    assert f"delete the primary IP {rec['primary_ipv4']['id']} in the provider console" in message
    assert "the next reap releases the lease" in message


def test_primary_ip_guard_refuses_a_foreign_assignee_type_a_protected_or_a_changed_address(world):
    view, rec, ip = gone_server_with_a_lingering_ip(world)
    api, rec = provenance_api(world, view)
    for change in ({"assignee_id": view["server_id"], "assignee_type": "load_balancer"}, {"assignee_id": 424242,
                   "assignee_type": "server"}, {"protection": {"delete": True}}, {"ip": "198.51.100.77"}):
        saved = {k: ip.get(k) for k in change}
        ip.update(change)
        refused(api.primary_ip, rec)
        refused(api.delete_primary_ip, rec)
        ip.update(saved)
    assert not [w for w in deletes(world) if "primary_ips" in w[1]]
    api.delete_primary_ip(rec)                                                    # untampered: accepted
    assert ip["id"] not in world.hz.primary_ips


def test_only_a_validated_ipv4_string_is_recorded(world):
    from motel.cabins.controller import primary_ipv4_record
    assert primary_ipv4_record({"id": 7, "ip": "203.0.113.5"}) == {"id": 7, "ip": "203.0.113.5"}
    for bad in (None, "", "x", " 203.0.113.5", "203.0.113.5 ", "2001:db8::1", "203.0.113.5/32", 5, ["203.0.113.5"]):
        assert primary_ipv4_record({"id": 7, "ip": bad}) == {"id": 7, "ip": None}, bad
    assert primary_ipv4_record({"id": "7", "ip": "203.0.113.5"}) == {"id": None, "ip": "203.0.113.5"}
    assert primary_ipv4_record(None) == {"id": None, "ip": None}


def test_a_reconciled_server_records_a_validated_address(world):
    world.hz.fail[("POST", "/servers")] = ["lost"]
    view = world.checkin()
    rec = world.store.load(view["lease_id"])
    assert rec["primary_ipv4"]["ip"] == world.hz.primary_ips[rec["primary_ipv4"]["id"]]["ip"]


def test_a_404_from_the_delete_means_the_server_is_gone_not_an_error(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.fail[("DELETE", f"/servers/{view['server_id']}")] = [ApiError(423, "locked")]
    original = world.hz._write

    def vanishing(method, path, body):
        if method == "DELETE" and path.startswith("/servers/"):
            world.hz._remove_server(int(path.rsplit("/", 1)[1]))
            raise ApiError(404, "not_found")
        return original(method, path, body)
    world.hz._write = vanishing
    out = c.checkout(view["lease_id"])
    assert out["state"] == "released" and world.store.load(view["lease_id"])["errors"] == []
    assert out["cleanup"]["server"] == "done" and not world.hz.firewalls and not world.hz.primary_ips


def test_the_reaper_uses_a_shorter_cleanup_budget_than_interactive_checkout(world):
    world.hz.delete_action_gets = 10 ** 6
    c = world.controller()
    view = world.checkin(c)
    world.clock.advance(hours=2, minutes=11)
    before = world.clock.now
    out = c.reap(apply=True)
    assert out["actions"][0]["state"] == "cleanup_pending" and out["actions"][0]["lease"] == view["lease_id"]
    assert 40 <= elapsed(world, before) <= 75                                   # ~45 s server wait, 60 s overall
    other = world.checkin(c, task="second")
    before = world.clock.now
    c.checkout(other["lease_id"])
    assert elapsed(world, before) >= 100                                        # interactive checkout waits longer


def test_the_reap_pass_is_bounded_and_leaves_the_rest_for_the_next_pass(world, monkeypatch):
    from motel import cabins
    monkeypatch.setattr(cabins.controller, "REAP_PASS_BUDGET", timedelta(seconds=30))
    world.hz.delete_action_gets = 10 ** 6
    c = world.controller()
    leases = [world.checkin(c, task=t)["lease_id"] for t in ("aaa", "bbb", "ccc")]
    world.clock.advance(hours=2, minutes=11)
    first = c.reap(apply=True)
    handled = [a["lease"] for a in first["actions"]]
    assert len(handled) == 1 and sorted(first["deferred"] + handled) == sorted(leases)
    assert any("left for the next pass" in w for w in first["warnings"])
    world.hz.delete_action_gets = 0
    second = c.reap(apply=True)
    assert second["actions"][0]["lease"] != handled[0]                         # deferred leases come first
    for _ in range(3):
        c.reap(apply=True)
    assert all(world.store.load(lid)["state"] == "released" for lid in leases)
    assert not world.hz.servers and not world.hz.firewalls


def test_a_dry_run_is_never_cut_short(world, monkeypatch):
    from motel import cabins
    monkeypatch.setattr(cabins.controller, "REAP_PASS_BUDGET", timedelta(seconds=0))
    c = world.controller()
    for t in ("aaa", "bbb"):
        world.checkin(c, task=t)
    world.clock.advance(hours=2, minutes=11)
    plan = c.reap(apply=False)
    assert len(plan["planned"]) == 2 and plan["deferred"] == []


def test_the_reaper_handles_billing_leases_before_cleanup_pending_ones(world):
    c = world.controller()
    pending = world.checkin(c, task="aaa")["lease_id"]
    idle = world.checkin(c, task="bbb")["lease_id"]
    expired = world.checkin(c, task="ccc")["lease_id"]
    world.hz.fail[("DELETE", f"/servers/{world.store.load(pending)['server_id']}")] = [ApiError(503, "x")]
    assert c.checkout(pending)["state"] == "cleanup_pending"
    rec = world.store.load(expired)
    rec["expires_at"] = "2026-11-08T11:00:00+00:00"                              # an hour past its expiry
    world.store.save(rec)
    world.guest.activity_docs = [activity(idle_s=1900)]
    world.at_minute(50)
    plan = c.reap(apply=False)
    assert [p["lease"] for p in plan["planned"]] == [expired, pending, idle]
    assert [p["reason"] for p in plan["planned"]] == ["ttl", "checkout", "idle"]


def test_a_lease_released_after_the_snapshot_is_left_alone(world):
    c = world.controller()
    view = world.checkin(c)
    world.hz.fail[("DELETE", f"/servers/{view['server_id']}")] = [ApiError(503, "x")]
    assert c.checkout(view["lease_id"])["state"] == "cleanup_pending"
    stale = world.store.all()                                                     # the reaper's snapshot
    assert c.checkout(view["lease_id"])["state"] == "released"                    # released before it takes the lock
    world.store.all = lambda: stale
    world.clock.advance(minutes=30)                                               # past any bootstrap deadline
    writes = len(world.hz.writes)
    result = c.reap(apply=True)
    assert result["actions"] == [] and result["errors"] == []
    assert world.store.load(view["lease_id"])["state"] == "released" and len(world.hz.writes) == writes


def test_reap_reason_has_no_reason_for_a_released_or_unknown_state(world):
    c = world.controller()
    view = world.checkin(c)
    c.checkout(view["lease_id"])
    world.clock.advance(minutes=30)
    rec = world.store.load(view["lease_id"])
    assert rec["state"] == "released" and c._reap_reason(rec, world.clock(), {"warnings": []}, True) is None
    rec.pop("bootstrap_deadline_at", None)
    assert c._reap_reason(rec, world.clock(), {"warnings": []}, True) is None


# ---- review round 3: a malformed lease never stops the pass; class order

EXPIRED = "2026-11-08T11:00:00+00:00"


@pytest.mark.parametrize("damage, expect_error", [
    ({"expires_at": "not a time"}, True),
    ({"expires_at": "2026-11-08T11:00:00"}, True),                          # naive: no time zone
    ({"updated_at": 12345, "expires_at": EXPIRED}, False),                  # same class as the good lease: keys compare
    ({"expires_at": None}, True),
], ids=["unparseable-expiry", "naive-expiry", "non-string-updated-at", "null-expiry"])
def test_a_malformed_lease_never_stops_the_reap_pass(world, damage, expect_error):
    c = world.controller()
    bad = world.checkin(c, task="aaa")["lease_id"]
    good = world.checkin(c, task="bbb")["lease_id"]
    rec = world.store.load(good)
    rec["expires_at"] = "2026-11-08T11:00:00+00:00"                      # long expired
    world.store.save(rec)
    broken = world.store.load(bad)
    broken.update(damage)
    import json as _json
    with open(os.path.join(world.store.leases, bad + ".json"), "w") as handle:      # bypass save(): it stamps updated_at
        _json.dump(broken, handle)
    result = c.reap(apply=True)
    assert world.store.load(good)["state"] == "released"                 # B is reaped whatever A looks like
    assert good in [a["lease"] for a in result["actions"]]
    assert world.store.load(bad)["state"] == ("active" if expect_error else "released")   # never deleted on a guess
    errors = [e["lease"] for e in result["errors"] if e.get("lease") == bad]
    assert errors == ([bad] if expect_error else [])                     # its own error is recorded, nobody else's
    plan = c.reap(apply=False)                                           # and a dry run survives it too
    assert plan["schema"]


def test_many_slow_active_leases_do_not_starve_a_pending_lease_whose_server_is_alive(world, monkeypatch):
    from motel import cabins
    monkeypatch.setattr(cabins.controller, "REAP_PASS_BUDGET", timedelta(seconds=30))
    c = world.controller()
    pending = world.checkin(c, task="zzz")["lease_id"]
    world.hz.fail[("DELETE", f"/servers/{world.store.load(pending)['server_id']}")] = [ApiError(503, "x")]
    assert c.checkout(pending)["state"] == "cleanup_pending"
    assert world.store.load(pending)["cleanup"]["server"].startswith("pending")          # its server is still alive
    actives = [world.checkin(c, task=t)["lease_id"] for t in ("aaa", "bbb")]
    world.guest.activity_docs = [activity(idle_s=1900)]                                   # idle: each would be reaped ...
    world.at_minute(50)
    world.hz.delete_action_gets = 10 ** 6                                                 # ... but its delete is slow
    first = c.reap(apply=True)
    assert [a["lease"] for a in first["actions"]] == [pending]                           # billing-certain goes first
    assert sorted(first["deferred"]) == sorted(actives)
    assert world.store.load(pending)["cleanup"]["server"].startswith("pending")


def test_a_pending_lease_with_its_server_done_comes_after_the_other_classes(world):
    c = world.controller()
    fw_only = world.checkin(c, task="aaa")["lease_id"]
    live = world.checkin(c, task="bbb")["lease_id"]
    world.hz.fail[("DELETE", f"/firewalls/{world.store.load(fw_only)['firewall_id']}")] = [ApiError(403, "x")] * 5
    assert c.checkout(fw_only)["state"] == "cleanup_pending"
    assert world.store.load(fw_only)["cleanup"]["server"] == "done"
    world.guest.activity_docs = [activity(idle_s=1900)]
    world.at_minute(50)
    assert [p["lease"] for p in c.reap(apply=False)["planned"]] == [live, fw_only]


def test_a_read_that_never_stops_being_rate_limited_ends_pending_not_in_an_exception(world):
    c = world.controller(sleep=lambda _s: None)                         # a frozen clock: only the read count bounds it
    view = world.checkin(c)
    world.hz.fail[("GET", f"/firewalls/{view['firewall_id']}")] = [ApiError(429, "rate_limit_exceeded")] * 1000
    out = c.checkout(view["lease_id"])
    assert out["state"] == "cleanup_pending" and out["cleanup"]["firewall"].startswith("pending")
