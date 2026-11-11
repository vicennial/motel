import copy
import ipaddress

import pytest
from cabin_fakes import HOST, HOST_IPV4, HOST_IPV6, World

from motel.cabins import provider
from motel.cabins.errors import ApiError, GuardRefused, Unavailable
from motel.cabins.provider import (
    GuardedApi,
    egress_rules,
    ingress_rules,
    list_all,
    selector,
)
from motel.cabins.util import host_cidr


def test_host_cidr_accepts_only_single_hosts():
    assert host_cidr("203.0.113.10") == "203.0.113.10/32"
    assert host_cidr("2001:db8::10") == "2001:db8::10/128"
    for bad in ("0.0.0.0/0", "10.0.0.0/8", "203.0.113.0/24", "::/0", "0.0.0.0", "127.0.0.1", "169.254.1.1",
                "224.0.0.1", "not-an-address", None, 5, "203.0.113.10/31"):
        assert host_cidr(bad) is None


def test_ingress_is_only_tcp22_from_exact_host_addresses():
    sources = [HOST_IPV4 + "/32", HOST_IPV6 + "/128"]
    rules = ingress_rules({"mode": "ssh-allowlist", "sources": sources})
    assert rules == [{"direction": "in", "protocol": "tcp", "port": "22", "source_ips": sorted(sources),
                      "description": "ssh from the host"}]
    assert ingress_rules({"mode": "tailscale", "sources": []}) == []


@pytest.mark.parametrize("sources", [None, [], ["0.0.0.0/0"], ["::/0"], ["203.0.113.0/24"], ["203.0.113.10"],
                                      ["203.0.113.10/32", "203.0.113.10/32"], ["10.0.0.0/8"]])
def test_ingress_refuses_wildcards_subnets_and_empty(sources):
    with pytest.raises(GuardRefused):
        ingress_rules({"mode": "ssh-allowlist", "sources": sources})


def test_egress_excludes_reserved_ranges_and_the_host():
    rules = egress_rules(["185.12.64.1"], [HOST_IPV4])
    web = [ipaddress.ip_network(c) for r in rules if r["port"] in ("80", "443") and "destination_ips" in r
           and r["description"] in ("http", "https") for c in r["destination_ips"]]
    for address in ("10.1.2.3", "100.64.0.1", "192.168.1.1", "169.254.169.254", "127.0.0.1", HOST_IPV4):
        assert not any(ipaddress.ip_address(address) in net for net in web), address
    assert any(ipaddress.ip_address("93.184.216.34") in net for net in web)
    assert all(r["direction"] == "out" for r in rules)


def test_egress_refuses_a_reserved_dns_resolver():
    from motel.cabins.errors import PolicyRefused
    with pytest.raises(PolicyRefused):
        egress_rules(["10.0.0.2"], [])


def test_list_all_follows_pagination_and_never_returns_a_partial_list(tmp_path):
    w = World(str(tmp_path))
    api = GuardedApi(w.hz, HOST)
    for i in range(120):
        w.hz.add_volume(f"motel-v{i}", {"x": "1"})
    assert len(list_all(api, "volumes", selector(x="1"))) == 120
    w.hz.fail[("GET", "/volumes")] = [None, ApiError(503, "boom")]
    with pytest.raises(ApiError):
        list_all(api, "volumes", selector(x="1"))


def isolated_world(tmp_path):
    w = World(str(tmp_path))
    return w, GuardedApi(w.hz, HOST)


def test_isolated_project_passes_for_empty_project(tmp_path):
    _w, api = isolated_world(tmp_path)
    assert api.isolated_project() is True


def test_isolated_project_refuses_when_host_is_visible(tmp_path):
    w, api = isolated_world(tmp_path)
    w.hz.visible.add(HOST)
    with pytest.raises(GuardRefused, match="can see the host"):
        api.isolated_project()


def test_isolated_project_refuses_foreign_server_or_volume(tmp_path):
    w, api = isolated_world(tmp_path)
    w.hz.add_server("web-prod", {})
    with pytest.raises(GuardRefused, match="dedicated cabin project"):
        api.isolated_project()
    w2, api2 = isolated_world(tmp_path / "b")
    w2.hz.add_volume("motel-looking", {"managed-by": "motel", "role": "cabin", "controller": "42"})
    with pytest.raises(GuardRefused):
        api2.isolated_project()


def test_isolated_project_distrusts_404_without_inventory(tmp_path):
    w, api = isolated_world(tmp_path)
    w.hz.unavailable = True
    with pytest.raises(ApiError):
        api.isolated_project()


def test_no_write_happens_when_isolation_fails(tmp_path):
    w, api = isolated_world(tmp_path)
    rec = intent_record(w)
    w.hz.add_server("web-prod", {})
    with pytest.raises(GuardRefused):
        api.create_firewall(rec, provider.egress_rules(["185.12.64.1"], []) + ingress_rules(rec["access"]))
    assert w.hz.writes == []


def intent_record(w):
    c = w.controller()
    c.require_ready()
    return c._intent("build", "alice", __import__("datetime").timedelta(hours=1),
                     c.offer("cpx32", __import__("datetime").timedelta(hours=1)), "ssh-ed25519 " + "A" * 43,
                     {"checked_at": None}, None)


def test_create_firewall_refuses_public_ingress_even_if_rules_are_forged(tmp_path):
    w, api = isolated_world(tmp_path)
    rec = intent_record(w)
    forged = provider.egress_rules(["185.12.64.1"], []) + [
        {"direction": "in", "protocol": "tcp", "port": "22", "source_ips": ["0.0.0.0/0"], "description": "x"}]
    with pytest.raises(GuardRefused, match="inbound rules differ"):
        api.create_firewall(rec, forged)
    assert w.hz.writes == []


def test_create_server_cannot_adopt_or_use_foreign_volume(tmp_path):
    w, api = isolated_world(tmp_path)
    rec = intent_record(w)
    fw = api.create_firewall(rec, provider.egress_rules(["185.12.64.1"], []) + ingress_rules(rec["access"]))
    rec["firewall_id"] = fw["firewall"]["id"]
    rec["volume_id"] = w.hz.add_volume("motel-other", {})      # not this lease's volume
    with pytest.raises(GuardRefused):
        api.create_server(rec, server_type="cpx32", location="fsn1", image=1, user_data="x")


def test_delete_requires_recorded_provenance(tmp_path):
    w, api = isolated_world(tmp_path)
    labels = {"managed-by": "motel", "role": "cabin", "controller": str(HOST)}
    other = w.hz.add_server("motel-other-aaaaaaaaaaaa", dict(labels, lease="other-aaaaaaaaaaaa", intent="0" * 32))
    rec = intent_record(w)
    rec["server_id"] = other
    with pytest.raises(GuardRefused):
        api.delete_server(rec)
    assert other in w.hz.servers


def test_firewall_deletion_refused_while_server_exists(tmp_path):
    w = World(str(tmp_path))
    c = w.controller()
    view = w.checkin(c)
    rec = w.store.load(view["lease_id"])
    with pytest.raises(GuardRefused):
        c.hz.delete_firewall(rec)
    assert rec["firewall_id"] in w.hz.firewalls


def test_transport_rejects_bad_paths_and_tokens():
    with pytest.raises(GuardRefused):
        provider.Transport("has space")
    t = provider.Transport("tok")
    for path in ("//evil", "no-slash", "/x#frag"):
        with pytest.raises(GuardRefused):
            t.request("GET", path)


def test_guarded_get_only_allows_known_read_endpoints(tmp_path):
    _w, api = isolated_world(tmp_path)
    for path in ("/ssh_keys", "/servers/1/actions/poweroff", "/networks", "/floating_ips"):
        with pytest.raises(GuardRefused):
            api.get(path)


def test_lease_labels_shape(tmp_path):
    rec = copy.deepcopy(intent_record(World(str(tmp_path))))
    labels = provider.lease_labels(rec)
    assert labels["managed-by"] == "motel" and labels["role"] == "cabin" and labels["controller"] == str(HOST)
    assert len(labels["intent"]) == 32


def test_unavailable_class_exists():
    assert issubclass(Unavailable, Exception)
