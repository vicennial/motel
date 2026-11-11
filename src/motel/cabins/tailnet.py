"""Tailscale access mode (opt-in). Everything tailnet-specific lives here.

A cabin joins the tailnet with a one-use, ephemeral, pre-authorized key for a configurable tag and is reached
over SSH on its tailnet address only. Before every checkin the effective tailnet policy is verified live:

* the controller (this host) is identified by its exact tailnet addresses, which must belong to exactly one
  device that is authorized, not external and advertises no routes;
* a static analysis of acls/grants/ssh/tagOwners/autoApprovers/nodeAttrs proves that the cabin tag can
  initiate nothing and that only the controller can reach it, on TCP/22 only (unknown selectors count as broad);
* provider-evaluated ACL tests (read-only) deny the cabin every existing peer and other cabins.

``setup tailscale`` runs the same checks and stores nothing until they pass.
"""

from __future__ import annotations

import ipaddress
import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import timedelta

from motel.cabins.errors import ApiError, Fail, GuardRefused, Prerequisite, Unavailable
from motel.cabins.util import parse_iso

TS_API = "https://api.tailscale.com/api/v2"
TS_PATH_RE = re.compile(
    r"^/(?:tailnet/[A-Za-z0-9.@_-]{1,100}/(?:acl|acl/validate|devices(?:\?fields=all)?|keys|keys/[A-Za-z0-9]{4,64})"
    r"|device/[A-Za-z0-9]{4,64}(?:\?fields=all)?)$")
TS_ID_RE = re.compile(r"^[A-Za-z0-9]{4,64}$")
TAG_RE = re.compile(r"^tag:[a-z0-9][a-z0-9-]{0,60}$")
AUTHKEY_RE = re.compile(r"^tskey-auth-[A-Za-z0-9-]{8,200}$")
TAILNET_V4 = ipaddress.ip_network("100.64.0.0/10")
TAILNET_V6 = ipaddress.ip_network("fd7a:115c:a1e0::/48")
AUTOGROUP_USERS = frozenset({
    "autogroup:member", "autogroup:self", "autogroup:admin", "autogroup:owner", "autogroup:it-admin",
    "autogroup:network-admin", "autogroup:billing-admin", "autogroup:auditor", "autogroup:shared",
    "autogroup:nonroot", "autogroup:internet"})


class TailnetTransport:
    """Fixed tailnet API destination using an OAuth client. Never logs secrets or bodies."""

    def __init__(self, client_id, client_secret):
        for value in (client_id, client_secret):
            if not isinstance(value, str) or not value or len(value) > 200 or any(c.isspace() for c in value):
                raise Fail("malformed tailnet controller credential")
        self.__client = (client_id, client_secret)
        self.__token, self.__until = None, 0.0

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        self.__opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def __open(self, req):
        try:
            with self.__opener.open(req, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise ApiError(exc.code, "tailnet_api_error") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise ApiError(0, "transport_or_response_error") from None

    def __access(self):
        if self.__token and time.monotonic() < self.__until:
            return self.__token
        body = urllib.parse.urlencode({"client_id": self.__client[0], "client_secret": self.__client[1],
                                       "grant_type": "client_credentials"}).encode()
        doc = self.__open(urllib.request.Request(TS_API + "/oauth/token", data=body, method="POST", headers={
            "Content-Type": "application/x-www-form-urlencoded", "User-Agent": "motel-cabins/1"}))
        token = doc.get("access_token") if isinstance(doc, dict) else None
        if not isinstance(token, str) or not token:
            raise ApiError(0, "tailnet_auth_failed")
        self.__token = token
        self.__until = time.monotonic() + max(60, int(doc.get("expires_in") or 300) - 60)
        return token

    def request(self, method, path, body=None):
        if not TS_PATH_RE.fullmatch(path):
            raise GuardRefused("invalid tailnet path")
        req = urllib.request.Request(
            TS_API + path, data=None if body is None else json.dumps(body).encode(), method=method,
            headers={"Authorization": "Bearer " + self.__access(), "Accept": "application/json",
                     "Content-Type": "application/json", "User-Agent": "motel-cabins/1"})
        return self.__open(req)


class TailnetApi:
    """Narrow client: read policy/devices, run policy tests, mint one tagged one-use ephemeral key, revoke
    it, delete only the cabin's own tagged node."""

    def __init__(self, transport, *, tag: str, tailnet: str = "-", protected_devices=(), authkey_seconds=1500):
        if not TAG_RE.fullmatch(tag):
            raise Fail("invalid tailnet tag")
        self.__t = transport
        self.tag, self.tailnet = tag, tailnet
        self.protected_devices = frozenset(protected_devices)
        self.authkey_seconds = authkey_seconds

    def policy(self):
        doc = self.__t.request("GET", f"/tailnet/{self.tailnet}/acl")
        if not isinstance(doc, dict):
            raise ApiError(0, "tailnet_policy_unreadable")
        return doc

    def run_tests(self, tests) -> int:
        """Run ACL tests against the current policy; returns the number of failures."""
        doc = self.__t.request("POST", f"/tailnet/{self.tailnet}/acl/validate", tests)
        if not isinstance(doc, dict):
            raise ApiError(0, "tailnet_validate_unreadable")
        if not doc.get("message"):
            return 0
        data = doc.get("data")
        return len(data) if isinstance(data, list) and data else 1

    def devices(self) -> list:
        doc = self.__t.request("GET", f"/tailnet/{self.tailnet}/devices?fields=all")
        items = doc.get("devices") if isinstance(doc, dict) else None
        if not isinstance(items, list):
            raise ApiError(0, "tailnet_inventory_incomplete")
        return items

    def create_key(self, rec):
        create = {"reusable": False, "ephemeral": True, "preauthorized": True, "tags": [self.tag]}
        doc = self.__t.request("POST", f"/tailnet/{self.tailnet}/keys", {
            "capabilities": {"devices": {"create": create}}, "expirySeconds": self.authkey_seconds,
            "description": "motel-" + rec["lease_id"][-12:]})
        key_id, key = (doc.get("id"), doc.get("key")) if isinstance(doc, dict) else (None, None)
        got = ((((doc or {}).get("capabilities") or {}).get("devices") or {}).get("create")
               if isinstance(doc, dict) else None)
        if not (isinstance(key_id, str) and TS_ID_RE.fullmatch(key_id)):
            raise ApiError(0, "tailnet_key_response_invalid")
        if not (isinstance(key, str) and AUTHKEY_RE.fullmatch(key)) or got != create:
            self.revoke_key(key_id)
            raise GuardRefused("tailnet key capabilities differ from the one-use ephemeral tag request")
        return key_id, key

    def revoke_key(self, key_id):
        if not (isinstance(key_id, str) and TS_ID_RE.fullmatch(key_id)):
            raise GuardRefused("invalid tailnet key ID")
        try:
            self.__t.request("DELETE", f"/tailnet/{self.tailnet}/keys/{key_id}")
        except ApiError as exc:
            if exc.status != 404:
                raise
        return True

    def get_device(self, node):
        """The device with this immutable ID, or None when the provider reports it absent."""
        if not (isinstance(node, str) and TS_ID_RE.fullmatch(node)):
            raise GuardRefused("invalid tailnet node ID")
        try:
            doc = self.__t.request("GET", f"/device/{node}?fields=all")
        except ApiError as exc:
            if exc.status == 404:
                return None
            raise
        if not isinstance(doc, dict) or (doc.get("nodeId") or doc.get("id")) != node:
            raise ApiError(0, "tailnet_device_unreadable")
        return doc

    def lease_devices(self, rec) -> list:
        return [d for d in self.devices() if d.get("hostname") == rec["name"]]

    def check_device(self, rec, device):
        node = device.get("nodeId") or device.get("id")
        if not (isinstance(node, str) and TS_ID_RE.fullmatch(node)) or node in self.protected_devices:
            raise GuardRefused("tailnet node is protected or has no valid ID")
        if device.get("hostname") != rec["name"] or device.get("tags") != [self.tag] or device.get("isExternal"):
            raise GuardRefused("tailnet node identity or tag differs from the cabin")
        if any(not isinstance(device.get(key), list) for key in ("advertisedRoutes", "enabledRoutes")):
            raise GuardRefused("tailnet route inventory incomplete")
        if device["advertisedRoutes"] or device["enabledRoutes"]:
            raise GuardRefused("tailnet node advertises routes")
        created = parse_iso(device.get("created"))
        if created is None or created < parse_iso(rec["created_at"]) - timedelta(minutes=5):
            raise GuardRefused("tailnet node predates the cabin intent")
        return node

    def delete_device(self, rec, device):
        node = self.check_device(rec, device)
        recorded = (rec.get("tailnet") or {}).get("device_id")
        if recorded is not None and recorded != node:
            raise GuardRefused("tailnet node differs from the recorded cabin node")
        try:
            self.__t.request("DELETE", f"/device/{node}")
        except ApiError as exc:
            if exc.status != 404:
                raise
        return True


def exact_tailnet_address(value):
    """Canonical host address inside the tailnet space only. Never turns a subnet into an exception."""
    if not isinstance(value, str):
        return None
    try:
        net = ipaddress.ip_network(value, strict=True)
    except ValueError:
        return None
    space = TAILNET_V4 if net.version == 4 else TAILNET_V6
    if net.prefixlen != net.max_prefixlen or not net.subnet_of(space):
        return None
    return str(net.network_address)


class TailnetIdentityError(Prerequisite):
    """Only fixed codes; never include provider values in an error."""

    def __init__(self, code):
        self.code = code
        super().__init__(f"tailnet controller identity check failed [{code}]",
                         "check cabins.tailscale.controller_addresses, then: sudo motel-desk setup tailscale")


def controller_identity(cfg, devices):
    """Resolve the configured controller against one fresh, complete device inventory.

    Returns (controller addresses, addresses of every device that is not a cabin). No config entry alone
    grants an exception: the addresses must belong to exactly one real, authorized device.
    """
    def need(ok, code):
        if not ok:
            raise TailnetIdentityError(code)

    configured = {exact_tailnet_address(a) for a in cfg.controller_addresses}
    need(None not in configured and bool(configured), "controller-addresses")
    need(isinstance(devices, list) and bool(devices), "inventory-list")
    ids, owner_of = {}, {}
    for dev in devices:
        need(isinstance(dev, dict), "device-object")
        node = dev.get("nodeId")
        need(isinstance(node, str) and TS_ID_RE.fullmatch(node) and node not in ids, "node-id-unique")
        ids[node] = dev
        addrs = dev.get("addresses")
        need(isinstance(addrs, list) and bool(addrs), "addresses-list")
        for value in addrs:
            addr = exact_tailnet_address(value)
            need(addr is not None and addr not in owner_of, "address-unique")
            owner_of[addr] = node
    nodes = {owner_of.get(a) for a in configured}
    need(len(nodes) == 1 and None not in nodes, "controller-unique-device")
    node = next(iter(nodes))
    dev = ids[node]
    need(cfg.controller_device_id in (None, node), "controller-node-id")
    need({exact_tailnet_address(a) for a in dev["addresses"]} == configured, "controller-address-set")
    need(dev.get("authorized") is True, "authorized")
    need(dev.get("isExternal") is False, "non-external")
    need("isEphemeral" not in dev or dev["isEphemeral"] is False, "lifecycle-contradiction")
    need(dev.get("advertisedRoutes") == [], "advertised-routes-empty")
    need(dev.get("enabledRoutes") == [], "enabled-routes-empty")
    need(cfg.tag not in (dev.get("tags") or []), "controller-has-cabin-tag")
    safe = {a for a, n in owner_of.items() if cfg.tag not in (ids[n].get("tags") or [])}
    return frozenset(configured), frozenset(safe)


def selector_covers_tag(sel, tag, hosts, groups, _depth=0, *, non_guest_addresses=frozenset()) -> bool:
    """True when ``sel`` can match a node tagged only with ``tag``. Unknown means True."""
    if not isinstance(sel, str) or _depth > 5:
        return True
    if sel in ("*", "autogroup:tagged", "autogroup:danger-all") or sel == tag:
        return True
    if sel.startswith("tag:"):
        return False
    if sel.startswith("group:"):
        members = groups.get(sel)
        if not isinstance(members, list):
            return True
        return any(selector_covers_tag(m, tag, hosts, groups, _depth + 1, non_guest_addresses=non_guest_addresses)
                   for m in members)
    if sel.startswith("autogroup:"):
        return sel not in AUTOGROUP_USERS
    if "@" in sel and not sel.startswith("ipset:"):
        return False
    target = hosts.get(sel, sel)
    if exact_tailnet_address(target) in non_guest_addresses:
        return False
    try:
        net = ipaddress.ip_network(target, strict=False)
    except (ValueError, TypeError):
        return True
    # Do not allow IPv4-mapped IPv6 spellings to evade the IPv4 overlap guard.
    return net.overlaps(TAILNET_V4) if net.version == 4 else (
        net.overlaps(TAILNET_V6) or net.overlaps(ipaddress.ip_network("::ffff:0:0/96")))


def split_dst(entry):
    if not isinstance(entry, str) or ":" not in entry:
        return entry, None
    host, ports = entry.rsplit(":", 1)
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    return host, ports


def analyze_policy(doc: dict, cfg, devices) -> list:
    """Static checks that a narrow rule cannot hide a broad one. Returns a list of problems."""
    try:
        controller, non_guest = controller_identity(cfg, devices)
    except Prerequisite as exc:
        return [exc.message]
    tag = cfg.tag
    hosts = doc.get("hosts") if isinstance(doc.get("hosts"), dict) else {}
    groups = doc.get("groups") if isinstance(doc.get("groups"), dict) else {}
    problems = []

    def covers(sel):
        return selector_covers_tag(sel, tag, hosts, groups, non_guest_addresses=non_guest)

    def controller_only(srcs):
        if not isinstance(srcs, list) or not srcs or not all(isinstance(src, str) for src in srcs):
            return False
        return all(exact_tailnet_address(hosts.get(src, src)) in controller for src in srcs)

    for i, rule in enumerate(doc.get("acls") or []):
        if not isinstance(rule, dict):
            problems.append(f"acls[{i}] unreadable")
            continue
        srcs, dsts = rule.get("src") or [], rule.get("dst") or []
        if any(covers(s) for s in srcs):
            problems.append(f"acls[{i}] lets the cabin initiate connections")
        for d in dsts:
            host, ports = split_dst(d)
            if covers(host) and (not controller_only(srcs) or ports != "22"
                                 or rule.get("proto") not in ("tcp", 6, "6")):
                problems.append(f"acls[{i}] grants access to the cabin beyond controller TCP/22")
    for i, rule in enumerate(doc.get("grants") or []):
        if not isinstance(rule, dict):
            problems.append(f"grants[{i}] unreadable")
            continue
        srcs, dsts = rule.get("src") or [], rule.get("dst") or []
        if any(covers(s) for s in srcs):
            problems.append(f"grants[{i}] lets the cabin initiate connections")
        if any(covers(d) for d in dsts) and (
                not controller_only(srcs) or rule.get("app") or (rule.get("ip") or []) != ["tcp:22"]):
            problems.append(f"grants[{i}] grants access to the cabin beyond controller TCP/22")
    for i, rule in enumerate(doc.get("ssh") or []):
        if not isinstance(rule, dict) or any(covers(s) for s in (rule.get("src") or []) + (rule.get("dst") or [])):
            problems.append(f"ssh[{i}] covers the cabin tag; Tailscale SSH is not used")
    owners = (doc.get("tagOwners") or {}).get(tag)
    if not isinstance(owners, list) or not owners:
        problems.append("tagOwners has no owner for the cabin tag")
    elif not set(owners) <= set(cfg.allowed_tag_owners):
        problems.append("cabin tag has owners outside the approved list")
    approvers = doc.get("autoApprovers") or {}
    for kind in ("routes", "exitNode"):
        values = approvers.get(kind) or {}
        flat = [v for vs in values.values() for v in vs] if isinstance(values, dict) else list(values)
        if any(covers(v) for v in flat):
            problems.append(f"autoApprovers.{kind} may approve the cabin")
    for i, attr in enumerate(doc.get("nodeAttrs") or []):
        if (isinstance(attr, dict) and any(covers(t) for t in attr.get("target") or [])
                and any("funnel" in str(a) for a in attr.get("attr") or [])):
            problems.append(f"nodeAttrs[{i}] enables Funnel for the cabin")
    return problems


def policy_destination(host, port):
    """Provider ACL/test syntax requires bracketed IPv6 destinations."""
    try:
        if ipaddress.ip_network(host, strict=False).version == 6:
            return f"[{host}]:{port}"
    except ValueError:
        pass
    return f"{host}:{port}"


def policy_tests(cfg, devices) -> list:
    """Concrete ACL tests run by the provider's own evaluator against the current policy."""
    tag, ports = cfg.tag, sorted(set(cfg.probe_ports))
    controller, _safe = controller_identity(cfg, devices)
    controller_owner = None
    destinations = set(cfg.protected_test_destinations) | {tag}
    peers, identities = set(), set()
    for dev in devices:
        if dev.get("tags") == [tag]:
            continue
        is_controller = any(exact_tailnet_address(a) in controller for a in dev.get("addresses") or [])
        if is_controller:
            controller_owner = dev.get("user")
        for addr in dev.get("addresses") or []:
            try:
                addr = str(ipaddress.ip_address(addr))
            except ValueError:
                continue
            destinations.add(addr)
            if not is_controller:
                peers.add(addr)
        if dev.get("tags"):
            identities.update(t for t in dev["tags"] if isinstance(t, str))
        elif isinstance(dev.get("user"), str) and dev["user"] and not is_controller:
            identities.add(dev["user"])
    identities -= {tag}
    if controller_owner:
        identities.discard(controller_owner)
    tests = []
    deny = [policy_destination(d, p) for d in sorted(destinations) for p in ports]
    for start in range(0, len(deny), 100):
        tests.append({"src": tag, "deny": deny[start:start + 100]})
    for src in sorted(controller):
        tests.append({"src": src, "accept": [f"{tag}:22"], "deny": [f"{tag}:{p}" for p in ports if p != 22]})
    for src in sorted(peers):
        tests.append({"src": src, "deny": [f"{tag}:{p}" for p in ports]})
    for ident in sorted(identities):
        tests.append({"src": ident, "deny": [f"{tag}:{p}" for p in ports]})
    return tests


def security_snapshot(devices):
    """Compare identity/access fields, not volatile lastSeen or client telemetry."""
    keys = ("nodeId", "addresses", "user", "tags", "authorized", "isEphemeral",
            "advertisedRoutes", "enabledRoutes", "isExternal")
    return sorted(json.dumps({k: d.get(k) for k in keys}, sort_keys=True) for d in devices)


def verify_policy(api, cfg):
    """Read-only checks shared by setup and every checkin; never mints a key."""
    try:
        devices = api.devices()
        doc = api.policy()
        problems = analyze_policy(doc, cfg, devices)
        if problems:
            raise Prerequisite("effective tailnet policy is not restricted: " + "; ".join(problems[:5]),
                               "fix the tailnet policy file, then: sudo motel-desk cabin doctor")
        failures = api.run_tests(policy_tests(cfg, devices))
        if failures:
            raise Prerequisite(f"effective tailnet policy failed {failures} isolation test(s)",
                               "sudo motel-desk cabin doctor")
        after = api.devices()
        controller_identity(cfg, after)
        if security_snapshot(after) != security_snapshot(devices) or api.policy() != doc:
            raise Prerequisite("tailnet inventory or policy changed during verification",
                               "sudo motel-desk cabin doctor")
    except ApiError as exc:
        raise Unavailable(f"tailnet policy could not be verified (provider_unavailable:{exc.status}:{exc.code})") \
            from None
    return devices
