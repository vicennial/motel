"""Provider write boundary for cabins (Hetzner Cloud). Standard library only.

The root-owned lease record is provenance, never a list of arbitrary provider IDs. Every write first
re-proves that the token's project holds nothing but motel cabins and cannot see this host. This module has no
generic write API, and no endpoint or token override from CLI input.
"""

from __future__ import annotations

import copy
import ipaddress
import json
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import UTC, datetime

from motel.cabins.errors import ApiError, GuardRefused, Pending, Unavailable
from motel.cabins.util import (
    EXPIRY_LABEL_RE,
    LEASE_SCHEMA,
    OWNER_RE,
    host_cidr,
    is_pos_int,
)

API = "https://api.hetzner.cloud/v1"
LEASE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?-[0-9a-f]{12}$")
MAX_IPS_PER_RULE = 100
HETZNER_METADATA = "169.254.169.254/32"
HETZNER_DHCP = "172.31.1.1/32"
MANAGED_BY = "motel"
ROLE = "cabin"
IMAGE_ROLE = "tour-image"
IMAGE_NAME_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
# Never reachable from a cabin's public interface, whatever the config says.
RESERVED_V4 = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.0.0.0/24", "192.0.2.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
    "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4"))


class Transport:
    """Fixed HTTPS destination. Never logs tokens, request bodies or user-data."""

    def __init__(self, token):
        if not isinstance(token, str) or not token or any(c.isspace() for c in token):
            raise GuardRefused("malformed provider credential")
        self.__token = token

        class NoRedirect(urllib.request.HTTPRedirectHandler):
            def redirect_request(self, *args, **kwargs):
                return None
        # Neither ambient proxies nor redirects may redirect authenticated writes.
        self.__opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect())

    def request(self, method, path, body=None):
        if not path.startswith("/") or path.startswith("//") or "#" in path:
            raise GuardRefused("invalid provider path")
        req = urllib.request.Request(
            API + path, data=None if body is None else json.dumps(body).encode(), method=method,
            headers={"Authorization": "Bearer " + self.__token, "Content-Type": "application/json",
                     "User-Agent": "motel-cabins/1"})
        try:
            with self.__opener.open(req, timeout=30) as response:
                raw = response.read()
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            try:
                code = json.loads(exc.read()).get("error", {}).get("code", "api_error")
            except (ValueError, AttributeError):
                code = "api_error"
            raise ApiError(exc.code, code if isinstance(code, str) and re.fullmatch(r"[a-z_]{1,80}", code)
                           else "api_error") from None
        except (urllib.error.URLError, OSError, ValueError):
            raise ApiError(0, "transport_or_response_error") from None


# --- network rules ----------------------------------------------------------------------------

def egress_rules(resolvers, excluded) -> list:
    """Outbound allow rules for the cabin firewall.

    HTTP/HTTPS reach the public IPv4 internet minus reserved ranges, this host and any excluded address. DNS
    reaches only the configured resolvers. Hetzner metadata and the DHCP gateway stay reachable for boot.
    """
    excluded_nets = [ipaddress.ip_network(f"{ip}/32") for ip in excluded]
    blocked = list(RESERVED_V4) + excluded_nets
    allowed = [ipaddress.ip_network("0.0.0.0/0")]
    for net in blocked:
        nxt = []
        for candidate in allowed:
            if candidate.overlaps(net):
                if net.supernet_of(candidate):
                    continue
                nxt.extend(candidate.address_exclude(net))
            else:
                nxt.append(candidate)
        allowed = nxt
    allowed = sorted(ipaddress.collapse_addresses(allowed))
    for ip in resolvers:
        if any(ipaddress.ip_address(ip) in net for net in blocked):
            from motel.cabins.errors import PolicyRefused
            raise PolicyRefused("a DNS resolver is an excluded or reserved address",
                                "set cabins.dns_resolvers in /etc/motel/motel.toml")
    resolver_nets = [f"{ip}/32" for ip in resolvers]
    rules = []
    for proto in ("udp", "tcp"):
        rules.append({"direction": "out", "protocol": proto, "port": "53",
                      "destination_ips": list(resolver_nets), "description": "dns"})
    rules.append({"direction": "out", "protocol": "udp", "port": "67",
                  "destination_ips": [HETZNER_DHCP], "description": "dhcp"})
    rules.append({"direction": "out", "protocol": "tcp", "port": "80",
                  "destination_ips": [HETZNER_METADATA], "description": "metadata"})
    cidrs = [str(n) for n in allowed]
    for port in ("80", "443"):
        for start in range(0, len(cidrs), MAX_IPS_PER_RULE):
            rules.append({"direction": "out", "protocol": "tcp", "port": port,
                          "destination_ips": cidrs[start:start + MAX_IPS_PER_RULE],
                          "description": "http" if port == "80" else "https"})
    return rules


def ingress_rules(access: dict) -> list:
    """The only inbound rule a cabin firewall may have: TCP/22 from this host, in ssh-allowlist mode.

    Every source is one exact host address. A wildcard, a subnet or an empty list is refused here and again by
    the guard, and tailscale mode has no inbound rule at all.
    """
    if access.get("mode") != "ssh-allowlist":
        return []
    sources = access.get("sources")
    if not isinstance(sources, list) or not sources:
        raise GuardRefused("ssh-allowlist access needs the host's exact public address")
    clean = []
    for src in sources:
        cidr = host_cidr(src)
        if cidr is None or cidr != src:
            raise GuardRefused("firewall ingress source must be one exact host address (/32 or /128)")
        clean.append(cidr)
    if len(set(clean)) != len(clean):
        raise GuardRefused("duplicate firewall ingress source")
    return [{"direction": "in", "protocol": "tcp", "port": "22", "source_ips": sorted(clean),
             "description": "ssh from the host"}]


def _norm_in(rule) -> tuple:
    return (rule.get("direction"), rule.get("protocol"), str(rule.get("port")),
            tuple(sorted(rule.get("source_ips") or [])))


# --- listing helpers --------------------------------------------------------------------------

def selector(**labels) -> str:
    return ",".join(f"{k}={v}" for k, v in labels.items())


def list_all(api, kind: str, label_selector: str, max_pages: int = 100) -> list:
    """Complete paginated listing. Any gap raises; a partial list is never returned."""
    out, page = [], 1
    for _ in range(max_pages):
        query = urllib.parse.urlencode({"label_selector": label_selector, "per_page": 50, "page": page})
        doc = api.get(f"/{kind}?{query}")
        items = doc.get(kind) if isinstance(doc, dict) else None
        if not isinstance(items, list):
            raise Unavailable(f"incomplete provider inventory for {kind}")
        out.extend(items)
        nxt = ((doc.get("meta") or {}).get("pagination") or {}).get("next_page")
        if nxt is None:
            return out
        if not is_pos_int(nxt) or nxt <= page:
            raise Unavailable(f"invalid pagination for {kind}")
        page = nxt
    raise Unavailable(f"provider inventory for {kind} exceeds page limit")


def lease_labels(rec) -> dict:
    return {"managed-by": MANAGED_BY, "role": ROLE, "controller": str(rec["host_id"]),
            "lease": rec["lease_id"], "intent": rec["intent_nonce"]}


# --- guarded API ------------------------------------------------------------------------------

class GuardedApi:
    def __init__(self, transport, host_id, extra_protected=()):
        self.__transport = transport
        self.__host_id = int(host_id)
        self.__extra = frozenset(int(x) for x in extra_protected)

    @property
    def host_id(self):
        return self.__host_id

    @property
    def protected(self):
        return self.__extra | {self.__host_id}

    def get(self, path):
        if not re.fullmatch(r"/(servers|firewalls|primary_ips|volumes|server_types|images|pricing|actions)"
                            r"(/[0-9]+)?(?:\?[a-zA-Z0-9_%=,&.-]+)?", path):
            raise GuardRefused("unsupported read endpoint")
        return self.__transport.request("GET", path)

    def _inventory(self, kind):
        page, out = 1, []
        while True:
            path = f"/{kind}?per_page=50" + (f"&page={page}" if page > 1 else "")
            doc = self.get(path)
            if not isinstance(doc.get(kind), list):
                raise GuardRefused("cannot establish authenticated project inventory")
            out.extend(doc[kind])
            nxt = doc.get("meta", {}).get("pagination", {}).get("next_page")
            if nxt is None:
                return out
            if not is_pos_int(nxt) or nxt <= page:
                raise GuardRefused("invalid provider pagination")
            page = nxt

    def isolated_project(self):
        """The project holds only motel cabins and cannot see this host. 404 counts only after a good inventory."""
        for kind in ("servers", "volumes"):
            for obj in self._inventory(kind):
                labels = obj.get("labels") or {}
                if (obj.get("id") in self.protected
                        or labels.get("managed-by") != MANAGED_BY or labels.get("role") != ROLE
                        or labels.get("controller") != str(self.host_id)
                        or not str(obj.get("name", "")).startswith("motel-")):
                    raise GuardRefused("dedicated cabin project required: the project holds this host "
                                       f"or a {kind[:-1]} that motel did not create")
        for obj in self._inventory("firewalls"):
            labels = obj.get("labels") or {}
            if (labels.get("managed-by") != MANAGED_BY or labels.get("role") != ROLE
                    or labels.get("controller") != str(self.host_id)
                    or not str(obj.get("name", "")).startswith("motel-")):
                raise GuardRefused("dedicated cabin project required: the project holds a firewall that motel "
                                   "did not create")
            for attachment in obj.get("applied_to") or []:
                sid = (attachment.get("server") or {}).get("id") if isinstance(attachment, dict) else None
                if (not isinstance(attachment, dict) or attachment.get("type") != "server"
                        or not is_pos_int(sid) or sid in self.protected):
                    raise GuardRefused("a project firewall is attached by selector or to a protected server")
        for sid in self.protected:
            try:
                self.get(f"/servers/{sid}")
            except ApiError as exc:
                if exc.status == 404:
                    continue
                raise GuardRefused("host isolation check failed") from None
            raise GuardRefused("this token can see the host or a protected server; all writes refused")
        return True

    def __write(self, method, path, body=None):
        self.isolated_project()
        return self.__transport.request(method, path, body)

    # -- provenance
    def __intent(self, lease):
        if lease.get("schema") != LEASE_SCHEMA or not LEASE_RE.fullmatch(lease.get("lease_id", "")):
            raise GuardRefused("invalid lease provenance")
        if lease.get("host_id") != self.host_id:
            raise GuardRefused("foreign host provenance")
        if not isinstance(lease.get("intent_nonce"), str) or not re.fullmatch(r"[0-9a-f]{32}", lease["intent_nonce"]):
            raise GuardRefused("missing checkin intent")
        if lease.get("name") != "motel-" + lease["lease_id"]:
            raise GuardRefused("invalid resource name")
        for key in ("server_id",):
            if lease.get(key) in self.protected:
                raise GuardRefused("protected server in lease state")
        return {"managed-by": MANAGED_BY, "role": ROLE, "controller": str(self.host_id),
                "lease": lease["lease_id"], "intent": lease["intent_nonce"]}

    def __creation_labels(self, lease):
        labels = self.__intent(lease)
        stamps = []
        for field in ("created_at", "expires_at"):
            try:
                value = datetime.fromisoformat(lease[field])
                if value.tzinfo is None:
                    raise ValueError("naive time")
            except (KeyError, TypeError, AttributeError, ValueError):
                raise GuardRefused("checkin intent requires explicit UTC creation and expiry") from None
            stamps.append(value.astimezone(UTC))
        if stamps[1] <= stamps[0]:
            raise GuardRefused("expiry must follow creation")
        owner = lease.get("owner")
        if not isinstance(owner, str) or not OWNER_RE.fullmatch(owner):
            raise GuardRefused("checkin intent requires a valid owner")
        labels.update(created=stamps[0].strftime("%Y%m%dT%H%M%SZ"),
                      expires=stamps[1].strftime("%Y%m%dT%H%M%SZ"), owner=owner)
        return labels

    def __object(self, lease, kind, obj, name=None):
        labels = self.__intent(lease)
        expected = lease.get(kind + "_id")
        if not is_pos_int(expected) or obj.get("id") != expected:
            raise GuardRefused("resource lacks recorded checkin provenance")
        if obj.get("name") != (name or lease["name"]) or any(
                (obj.get("labels") or {}).get(k) != v for k, v in labels.items()):
            raise GuardRefused("resource identity or ownership changed")
        if any((obj.get("protection") or {}).values()):
            raise GuardRefused("provider-protected resource; never disable protection")
        return obj

    @staticmethod
    def disk_name(lease):
        return lease["name"] + "-disk"

    # -- reads that prove ownership
    def server(self, lease, *, exposure=True):
        """The lease's server. Identity is always proved (id, name, labels, nonce, protection). With
        ``exposure`` (create, bootstrap, extend) the attachments and firewall state are proved too; the delete
        path skips them so a half-built or tampered server can still be deleted."""
        self.__intent(lease)
        sid = lease.get("server_id")
        if not is_pos_int(sid):
            raise GuardRefused("missing server provenance")
        obj = self.__object(lease, "server", self.get(f"/servers/{sid}")["server"])
        if not exposure:
            return obj
        vid = lease.get("volume_id")
        attached_volumes = sorted(obj.get("volumes") or [])
        if set(attached_volumes) - ({vid} if vid else set()):
            raise GuardRefused("unexpected attached volume")
        if obj.get("private_net") or obj.get("load_balancers"):
            raise GuardRefused("unexpected attached resource")
        if obj.get("backup_window"):
            raise GuardRefused("unexpected backups")
        attached = (obj.get("public_net") or {}).get("firewalls", [])
        if not attached or {f.get("id") for f in attached} != {lease.get("firewall_id")}:
            raise GuardRefused("unexpected server firewall attachment")
        if any(f.get("status") != "applied" for f in attached):
            raise Pending("firewall not applied")
        if vid and attached_volumes != [vid]:
            raise Pending("own volume not attached yet")
        return obj

    def firewall(self, lease, *, empty=False, exposure=True):
        self.__intent(lease)
        fid = lease.get("firewall_id")
        if not is_pos_int(fid):
            raise GuardRefused("missing firewall provenance")
        obj = self.__object(lease, "firewall", self.get(f"/firewalls/{fid}")["firewall"])
        if not exposure:
            return obj
        wanted = [_norm_in(r) for r in ingress_rules(lease.get("access") or {})]
        seen = [_norm_in(r) for r in obj.get("rules", []) if r.get("direction") != "out"]
        if any(r.get("direction") not in ("in", "out") for r in obj.get("rules", [])) or seen != wanted:
            raise GuardRefused("firewall inbound rules differ from the cabin's allowed access (public ingress "
                               "is forbidden)")
        for attachment in obj.get("applied_to", []):
            if empty or attachment.get("type") != "server":
                raise GuardRefused("shared or selector firewall attachment")
            sid = (attachment.get("server") or {}).get("id")
            if not is_pos_int(sid) or sid in self.protected or sid != lease.get("server_id"):
                raise GuardRefused("firewall protects a foreign or protected host")
        return obj

    def primary_ip(self, lease):
        """The IPv4 the provider created with this lease's server. Auto-created addresses carry no labels, so
        provenance is the ID and address recorded from the create response, plus where it is assigned now."""
        self.__intent(lease)
        recorded = lease.get("primary_ipv4") or {}
        pid, sid = recorded.get("id"), lease.get("server_id")
        hint = (f"; delete the primary IP {pid if is_pos_int(pid) else '(see the provider console)'} in the provider "
                "console if it outlived its server; the next reap releases the lease")
        if not is_pos_int(pid) or not is_pos_int(sid) or sid in self.protected:
            raise GuardRefused("missing primary IP provenance" + hint)
        try:
            ok = (isinstance(recorded.get("ip"), str) and ipaddress.ip_address(recorded["ip"]).version == 4)
        except ValueError:
            ok = False
        if not ok:
            raise GuardRefused("missing primary IP provenance (no recorded address)" + hint)
        obj = self.get(f"/primary_ips/{pid}")["primary_ip"]
        if obj.get("id") != pid or obj.get("ip") != recorded["ip"]:
            raise GuardRefused("primary IP identity changed")
        if any((obj.get("protection") or {}).values()):
            raise GuardRefused("provider-protected resource; never disable protection")
        holder = obj.get("assignee_id")
        if holder is not None and (holder != sid or obj.get("assignee_type") != "server"):
            raise GuardRefused("primary IP is assigned to a foreign resource")
        return obj

    def volume(self, lease, *, detached=False, exposure=True):
        self.__intent(lease)
        if not is_pos_int(lease.get("volume_id")):
            raise GuardRefused("missing volume provenance")
        obj = self.__object(lease, "volume", self.get(f"/volumes/{lease['volume_id']}")["volume"],
                            name=self.disk_name(lease))
        if not exposure:
            return obj
        attached = obj.get("server")
        if attached is not None and (detached or attached != lease.get("server_id")):
            raise GuardRefused("volume is attached to a foreign server")
        return obj

    # -- writes
    def create_firewall(self, lease, rules):
        labels = self.__creation_labels(lease)
        if lease.get("firewall_id") is not None or lease.get("server_id") is not None:
            raise GuardRefused("create requires unused checkin intent")
        if not rules or any(r.get("direction") not in ("in", "out") for r in rules):
            raise GuardRefused("explicit firewall rules required")
        wanted = [_norm_in(r) for r in ingress_rules(lease.get("access") or {})]
        if [_norm_in(r) for r in rules if r.get("direction") == "in"] != wanted:
            raise GuardRefused("firewall inbound rules differ from the cabin's allowed access")
        return self.__write("POST", "/firewalls", {"name": lease["name"], "labels": labels,
                                                   "rules": copy.deepcopy(rules)})

    def create_volume(self, lease, *, size_gb, location):
        labels = self.__creation_labels(lease)
        if lease.get("volume_id") is not None or lease.get("server_id") is not None:
            raise GuardRefused("create requires unused checkin intent")
        if not (is_pos_int(size_gb) and 10 <= size_gb <= 10240):
            raise GuardRefused("volume size out of range")
        if not re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", str(location)):
            raise GuardRefused("invalid location")
        return self.__write("POST", "/volumes", {"name": self.disk_name(lease), "size": size_gb,
                                                 "location": location, "labels": labels,
                                                 "format": "ext4", "automount": False})

    def create_server(self, lease, *, server_type, location, image, user_data):
        labels = self.__creation_labels(lease)
        if lease.get("server_id") is not None:
            raise GuardRefused("cannot adopt or replace an existing server")
        self.firewall(lease, empty=True)
        if lease.get("volume_id") is not None:
            self.volume(lease, detached=True)
        if not all(isinstance(x, str) and re.fullmatch(r"[a-z0-9][a-z0-9-]{0,39}", x)
                   for x in (server_type, location)):
            raise GuardRefused("invalid server type or location")
        if not is_pos_int(image):
            raise GuardRefused("a verified numeric system-image ID is required")
        # All references are constructed here. No existing IP, SSH key, network or placement-group ID can
        # enter a create request; the only volume is the one this lease created.
        body = {"name": lease["name"], "server_type": server_type, "location": location, "image": image,
                "labels": labels, "user_data": user_data, "firewalls": [{"firewall": lease["firewall_id"]}],
                "public_net": {"enable_ipv4": True, "enable_ipv6": False}, "start_after_create": True}
        if lease.get("volume_id") is not None:
            body.update(volumes=[lease["volume_id"]], automount=False)
        return self.__write("POST", "/servers", body)

    def extend_server(self, lease, expiry_label):
        obj = self.server(lease)
        self.firewall(lease)
        if not EXPIRY_LABEL_RE.fullmatch(expiry_label):
            raise GuardRefused("invalid expiry label")
        labels = dict(obj["labels"], expires=expiry_label)
        return self.__write("PUT", f"/servers/{obj['id']}", {"labels": labels})

    def extend_volume(self, lease, expiry_label):
        obj = self.volume(lease)
        if not EXPIRY_LABEL_RE.fullmatch(expiry_label):
            raise GuardRefused("invalid expiry label")
        labels = dict(obj["labels"], expires=expiry_label)
        return self.__write("PUT", f"/volumes/{obj['id']}", {"labels": labels})

    def delete_server(self, lease):
        """Request deletion. Returns the provider's reply, whose ``action`` (when present) is polled to the end."""
        obj = self.server(lease, exposure=False)
        return self.__write("DELETE", f"/servers/{obj['id']}")

    def _server_absent_or_refuse(self, lease, what):
        sid = lease.get("server_id")
        if sid is None:
            return
        try:
            self.get(f"/servers/{sid}")
        except ApiError as exc:
            if exc.status != 404:
                raise
        else:
            raise Pending(f"server still exists; cannot delete {what}")

    def delete_firewall(self, lease):
        obj = self.firewall(lease, exposure=False)
        self._server_absent_or_refuse(lease, "firewall")
        return self.__write("DELETE", f"/firewalls/{obj['id']}")

    def delete_primary_ip(self, lease):
        obj = self.primary_ip(lease)
        self._server_absent_or_refuse(lease, "primary IP")
        return self.__write("DELETE", f"/primary_ips/{obj['id']}")

    def delete_volume(self, lease):
        obj = self.volume(lease, exposure=False)
        self._server_absent_or_refuse(lease, "volume")
        if obj.get("server") is not None:
            raise Pending("volume is still attached")
        return self.__write("DELETE", f"/volumes/{obj['id']}")

    # -- tour images (snapshots). Every one carries managed-by=motel, role=tour-image and this host's controller label;
    # nothing else in the project is ever read as an image of ours, listed, or deleted.
    def image_labels(self) -> dict:
        return {"managed-by": MANAGED_BY, "role": IMAGE_ROLE, "controller": str(self.host_id)}

    def _own_image(self, obj):
        if not isinstance(obj, dict):
            raise GuardRefused("not a motel tour image of this host")
        labels = obj.get("labels") or {}
        if (obj.get("type") != "snapshot" or not is_pos_int(obj.get("id"))
                or any(labels.get(k) != v for k, v in self.image_labels().items())
                or not IMAGE_NAME_RE.fullmatch(str(labels.get("image-name", "")))):
            raise GuardRefused("not a motel tour image of this host")
        return obj

    def image(self, image_id):
        """One image, only if it is a tour image this host made."""
        if not is_pos_int(image_id):
            raise GuardRefused("invalid image id")
        return self._own_image(self.get(f"/images/{image_id}")["image"])

    def list_images(self) -> list:
        """Complete listing of this host's tour images. A partial list is never returned."""
        found = list_all(self, "images", selector(**self.image_labels()))
        out = []
        for obj in found:
            try:
                out.append(self._own_image(obj))
            except GuardRefused:
                continue                # an image with our labels but not our shape is not ours to show
        return out

    def create_image(self, lease, *, name):
        """Snapshot the lease's own, powered-off server as a labelled tour image. Returns the created image."""
        labels = self.__intent(lease)
        if not isinstance(name, str) or not IMAGE_NAME_RE.fullmatch(name):
            raise GuardRefused("invalid image name")
        obj = self.server(lease, exposure=False)
        if obj.get("status") != "off":
            raise GuardRefused("the server must be powered off before it is snapshotted")
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
        body = {"type": "snapshot", "description": f"motel tour image {name}",
                "labels": dict(self.image_labels(), **{"image-name": name, "source-lease": labels["lease"],
                                                       "baked": stamp})}
        doc = self.__write("POST", f"/servers/{obj['id']}/actions/create_image", body)
        image = doc.get("image") if isinstance(doc, dict) else None
        if not isinstance(image, dict) or not is_pos_int(image.get("id")):
            raise Unavailable("the provider did not return the new image")
        return image

    def delete_image(self, image_id):
        obj = self.image(image_id)
        if any((obj.get("protection") or {}).values()):
            raise GuardRefused("provider-protected image; never disable protection")
        return self.__write("DELETE", f"/images/{obj['id']}")
