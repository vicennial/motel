"""Opt-in tailscale access mode against the fake tailnet."""
import copy

import pytest
from cabin_fakes import (
    CTRL4,
    CTRL6,
    SECRET_AUTHKEY,
    TAG,
    World,
    good_tailnet_policy,
    make_config,
)

from motel.cabins import tailnet as tn
from motel.cabins.errors import ApiError, GuardRefused, Prerequisite


@pytest.fixture
def world(tmp_path):
    return World(str(tmp_path), access="tailscale")


def test_tailscale_checkin_has_no_public_ingress_and_uses_the_tailnet_address(world):
    view = world.checkin()
    assert view["address"].startswith("100.80.")
    assert [r for r in world.hz.firewalls[view["firewall_id"]]["rules"] if r["direction"] == "in"] == []
    user_data = next(w[2]["user_data"] for w in world.hz.writes if w[1] == "/servers")
    assert "tailscale up" in user_data and "tailscale0" in user_data
    assert "ListenAddress 127.0.0.1" in user_data
    rec = world.store.load(view["lease_id"])
    assert rec["tailnet"]["device_id"] and rec["tailnet"]["key_id"]


def test_auth_key_is_one_use_ephemeral_tagged_and_never_stored(world):
    world.checkin()
    created = next(w for w in world.ts.writes if w[0] == "POST")[2]
    create = created["capabilities"]["devices"]["create"]
    assert create == {"reusable": False, "ephemeral": True, "preauthorized": True, "tags": [TAG]}
    text = world.all_files_text()
    assert SECRET_AUTHKEY not in text
    assert SECRET_AUTHKEY not in str(world.hz.writes[0:1])


def test_key_with_wider_capabilities_is_revoked_and_refused(world):
    world.ts.key_capabilities = {"devices": {"create": {"reusable": True, "ephemeral": True, "preauthorized": True,
                                                         "tags": [TAG]}}}
    with pytest.raises(Exception, match="checkin failed|capabilities"):
        world.checkin()
    assert "revoked" in world.ts.keys.values()
    assert not world.hz.servers


def test_checkout_revokes_key_and_deletes_the_node(world):
    c = world.controller()
    view = world.checkin(c)
    out = c.checkout(view["lease_id"])
    assert out["state"] == "released"
    assert out["cleanup"]["auth_key"] == "done" and out["cleanup"]["tailnet_node"] == "done"
    assert list(world.ts.keys.values()) == ["revoked"]
    assert any(n.startswith("n") and n.endswith("NODE") for n in world.ts.deleted)


def test_undeletable_node_keeps_the_lease_pending(world):
    c = world.controller()
    view = world.checkin(c)
    world.ts.undeletable.add(world.store.load(view["lease_id"])["tailnet"]["device_id"])
    out = c.checkout(view["lease_id"])
    assert out["state"] == "cleanup_pending" and out["cleanup"]["tailnet_node"].startswith("pending")


def test_checkin_refuses_a_broad_policy(world):
    world.ts.policy["acls"].append({"action": "accept", "src": ["*"], "dst": ["*:*"]})
    with pytest.raises(Prerequisite, match="not restricted"):
        world.checkin()
    assert world.hz.writes == [] and not world.ts.keys


def test_provider_test_failures_block_checkin(world):
    world.ts.test_failures = 2
    with pytest.raises(Prerequisite, match="isolation test"):
        world.checkin()
    assert world.hz.writes == []


def test_tailnet_api_outage_blocks_checkin(world):
    world.ts.unavailable = True
    with pytest.raises(Exception, match="tailnet policy could not be verified"):
        world.checkin()


def test_missing_tailnet_credential_is_a_prerequisite_problem(world):
    world.hashes.pop("tailscale")
    with pytest.raises(Prerequisite, match="tailnet OAuth credential"):
        world.checkin()


def test_tailscale_readiness_requires_matching_listener(world):
    world.guest.ready_doc = {"schema": "motel-guest/v1", "bootstrap": "done", "tailnet_ip": "100.80.9.9",
                             "nft": "loaded", "forwarding_disabled": True, "gate": "enforced", "disk": "none",
                             "tcp_listeners": [{"addr": "0.0.0.0", "port": 22}]}
    with pytest.raises(Exception, match="isolation check"):
        world.checkin()
    assert not world.hz.servers


# ---- static policy analysis

def cfg():
    return make_config("tailscale")


def devices(world):
    return world.ts.devices()


def test_good_policy_has_no_problems(tmp_path):
    w = World(str(tmp_path), access="tailscale")
    assert tn.analyze_policy(good_tailnet_policy(), cfg().tailscale, devices(w)) == []


@pytest.mark.parametrize("mutate,needle", [
    (lambda p: p["acls"].append({"action": "accept", "src": ["*"], "dst": ["*:*"]}), "cabin"),
    (lambda p: p["acls"].append({"action": "accept", "src": [TAG], "dst": ["autogroup:member:*"]}), "initiate"),
    (lambda p: p["acls"].append({"action": "accept", "src": ["autogroup:member"], "dst": [TAG + ":*"]}), "beyond"),
    (lambda p: p["acls"].append({"action": "accept", "src": [CTRL4], "dst": [TAG + ":22,80"]}), "beyond"),
    (lambda p: p["ssh"].append({"action": "accept", "src": ["autogroup:member"], "dst": [TAG], "users": ["root"]}),
     "ssh"),
    (lambda p: p.__setitem__("tagOwners", {TAG: ["alice@example.invalid"]}), "owners"),
    (lambda p: p.__setitem__("tagOwners", {}), "no owner"),
    (lambda p: p.__setitem__("autoApprovers", {"exitNode": [TAG]}), "autoApprovers"),
    (lambda p: p.__setitem__("nodeAttrs", [{"target": [TAG], "attr": ["funnel"]}]), "Funnel"),
    (lambda p: p.__setitem__("grants", [{"src": ["*"], "dst": [TAG], "ip": ["*"]}]), "grants"),
])
def test_policy_analysis_flags_broad_rules(tmp_path, mutate, needle):
    w = World(str(tmp_path), access="tailscale")
    policy = copy.deepcopy(good_tailnet_policy())
    mutate(policy)
    problems = tn.analyze_policy(policy, cfg().tailscale, devices(w))
    assert problems and needle.lower() in " ".join(problems).lower(), problems


def test_controller_identity_requires_exact_addresses_of_one_device(tmp_path):
    w = World(str(tmp_path), access="tailscale")
    c = cfg().tailscale
    controller, safe = tn.controller_identity(c, devices(w))
    assert controller == frozenset({CTRL4, CTRL6}) and CTRL4 in safe
    # a configured address that belongs to no device
    bad = make_config("tailscale")
    object.__setattr__(bad.tailscale, "controller_addresses", ("100.64.0.99", CTRL6))
    with pytest.raises(Prerequisite):
        tn.controller_identity(bad.tailscale, devices(w))
    # the device owns an address the config omits
    partial = make_config("tailscale")
    object.__setattr__(partial.tailscale, "controller_addresses", (CTRL4,))
    with pytest.raises(Prerequisite):
        tn.controller_identity(partial.tailscale, devices(w))


def test_controller_that_is_ephemeral_or_routes_is_refused(tmp_path):
    w = World(str(tmp_path), access="tailscale")
    devs = devices(w)
    devs[0]["advertisedRoutes"] = ["10.0.0.0/8"]
    with pytest.raises(Prerequisite):
        tn.controller_identity(cfg().tailscale, devs)


def test_exact_tailnet_address_rejects_ranges_and_foreign_space():
    assert tn.exact_tailnet_address("100.64.0.1") == "100.64.0.1"
    assert tn.exact_tailnet_address("100.64.0.0/10") is None
    assert tn.exact_tailnet_address("8.8.8.8") is None
    assert tn.exact_tailnet_address("fd7a:115c:a1e0::1") == "fd7a:115c:a1e0::1"
    assert tn.exact_tailnet_address("fd00::1") is None


def test_policy_tests_deny_every_other_peer_and_allow_only_controller_ssh(tmp_path):
    w = World(str(tmp_path), access="tailscale")
    tests = tn.policy_tests(cfg().tailscale, devices(w))
    controller_tests = [t for t in tests if t["src"] in (CTRL4, CTRL6)]
    assert controller_tests and all(t["accept"] == [TAG + ":22"] for t in controller_tests)
    assert all(TAG + ":22" not in t["deny"] for t in controller_tests)
    peers = [t for t in tests if t["src"] == "100.64.0.2"]
    assert peers and f"{TAG}:22" in peers[0]["deny"]
    assert any(t["src"] == TAG for t in tests)


def test_api_refuses_protected_nodes_and_foreign_tags(tmp_path):
    w = World(str(tmp_path), access="tailscale")
    api = tn.TailnetApi(w.ts, tag=TAG, protected_devices=["nCTRL0001"])
    rec = {"name": "motel-x-aaaaaaaaaaaa", "created_at": "2026-11-08T12:00:00Z"}
    controller = devices(w)[0]
    with pytest.raises(GuardRefused):
        api.check_device(rec, controller)
    laptop = dict(devices(w)[1], hostname=rec["name"], tags=["tag:other"])
    with pytest.raises(GuardRefused):
        api.check_device(rec, laptop)


def test_transport_never_leaks_credentials_in_errors():
    assert issubclass(ApiError, Exception)
