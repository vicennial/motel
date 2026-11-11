"""Recording fakes for the cabin tests: Hetzner API, tailnet API, guest channel, clock. No network."""
import copy
import io
import json
import os
import shutil
import threading
import urllib.parse
from datetime import UTC, datetime, timedelta

from motel.cabins import config as cfgmod
from motel.cabins import controller as ctl
from motel.cabins import guest_agent as guest_mod
from motel.cabins.errors import ApiError, GuestUnknown
from motel.cabins.provider import GuardedApi
from motel.cabins.store import Store
from motel.cabins.tailnet import TailnetApi

HERE = os.path.dirname(os.path.abspath(__file__))
LIB = os.path.join(os.path.dirname(os.path.dirname(HERE)), "src", "motel", "cabins")

HOST = 1000001
TAG = "tag:motel-cabin"
HZ_TOKEN_HASH = "a" * 64
TS_CRED_HASH = "b" * 64
HOST_IPV4 = "203.0.113.10"
HOST_IPV6 = "2001:db8::10"
SECRET_AUTHKEY = "tskey-auth-kFAKE123CNTRL-secretsecretsecret"
CTRL4, CTRL6 = "100.64.0.1", "fd7a:115c:a1e0::1"
OWNER = "owner@example.invalid"


class Clock:
    def __init__(self, start=datetime(2026, 11, 8, 12, 0, 0, tzinfo=UTC)):
        self.start = self.now = start
        self.lock = threading.Lock()

    def __call__(self):
        return self.now

    def advance(self, **kw):
        with self.lock:
            self.now += timedelta(**kw)

    def sleep(self, seconds):
        self.advance(seconds=seconds)


def pubkey(seed):
    return "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + (seed * 43)[:43]


def fake_keygen(directory, name, comment):
    fill = "H" if "host" in name else "C"
    private = "-----BEGIN OPENSSH PRIVATE KEY-----\n" + (fill * 70 + "\n") * 3 + "-----END OPENSSH PRIVATE KEY-----\n"
    public = pubkey("H" if "host" in name else "C")
    path = os.path.join(directory, name)
    with open(path, "w") as handle:
        handle.write(private)
    with open(path + ".pub", "w") as handle:
        handle.write(public + " " + comment + "\n")
    return private, public


class FakeHetzner:
    """Hetzner Cloud API double with labels, pagination, firewalls, volumes and primary IPs."""

    def __init__(self, clock):
        self.clock = clock
        self.lock = threading.RLock()
        self.servers, self.firewalls, self.primary_ips, self.volumes = {}, {}, {}, {}
        self.writes, self.reads = [], []
        self.next_id = 5000
        self.visible = set()
        self.unavailable = False
        self.fail = {}            # (method, path) -> list of 'lost' | ApiError instance
        self.boot_gets = 2
        self.firewall_apply_gets = 1
        self.delete_delay_gets = 0
        self.delete_action_gets = 0     # polls of the server delete action that still say "running"
        self.delete_action_fails = False    # the delete action ends in error and the server stays
        self.ip_lag_gets = 0            # reads of an auto-delete IPv4 that still find it, assigned, after its server went
        self.actions = {}
        self.auto_delete_ip = True
        self.types = {"cpx32": "0.0310", "cpx52": "0.1610", "cpx62": "0.2083"}
        self.type_locations = None
        self.ipv4_price = "0.0008"
        self.volume_gb_month = "0.0440"
        self.on_write = None
        self.hide_volumes_gets = 0      # server GETs that still report no attached volume
        self.list_calls = []

    def _id(self):
        self.next_id += 1
        return self.next_id

    @staticmethod
    def _labels_match(obj, sel):
        if not sel:
            return True
        labels = obj.get("labels") or {}
        return all(labels.get(k) == v for k, v in (p.split("=", 1) for p in sel.split(",")))

    def _list(self, kind, items, query):
        q = dict(urllib.parse.parse_qsl(query))
        per, page = int(q.get("per_page", 25)), int(q.get("page", 1))
        matched = [copy.deepcopy({k: v for k, v in o.items() if not k.startswith("_")})
                   for _, o in sorted(items.items()) if self._labels_match(o, q.get("label_selector"))]
        chunk = matched[(page - 1) * per: page * per]
        nxt = page + 1 if page * per < len(matched) else None
        return {kind: chunk, "meta": {"pagination": {"page": page, "next_page": nxt}}}

    def request(self, method, path, body=None):
        with self.lock:
            key = (method, path.split("?")[0])
            plan = self.fail.get(key)
            mode = plan.pop(0) if plan else None
            if isinstance(mode, ApiError):
                if method != "GET":
                    self.writes.append((method, path, "failed"))
                raise mode
            if self.unavailable:
                raise ApiError(503, "unavailable")
            if method == "GET":
                self.reads.append(path)
                result = self._get(path)
            else:
                self.writes.append((method, path, copy.deepcopy(body)))
                if self.on_write:
                    self.on_write(method, path, body)
                result = self._write(method, path, body)
            if mode == "lost":
                raise ApiError(0, "transport_or_response_error")
            return result

    def _get(self, path):
        base, _, query = path.partition("?")
        parts = base.strip("/").split("/")
        if parts == ["servers"]:
            self.list_calls.append("servers")
            return self._list("servers", self.servers, query or "per_page=50")
        if parts == ["firewalls"]:
            return self._list("firewalls", self.firewalls, query)
        if parts == ["volumes"]:
            return self._list("volumes", self.volumes, query or "per_page=50")
        if parts[0] == "servers" and len(parts) == 2:
            sid = int(parts[1])
            if sid == HOST:
                if sid in self.visible:
                    return {"server": {"id": sid, "name": "the-host", "labels": {}}}
                raise ApiError(404, "not_found")
            obj = self.servers.get(sid)
            if obj is None:
                raise ApiError(404, "not_found")
            if obj.get("_deleting") is not None:
                obj["_deleting"] -= 1
                if obj["_deleting"] < 0:
                    self._remove_server(sid)
                    raise ApiError(404, "not_found")
            obj["_gets"] = obj.get("_gets", 0) + 1
            if obj["_gets"] >= self.boot_gets:
                obj["status"] = "running"
            if obj["_gets"] >= self.firewall_apply_gets:
                for fw in obj["public_net"]["firewalls"]:
                    fw["status"] = "applied"
            out = {k: copy.deepcopy(v) for k, v in obj.items() if not k.startswith("_")}
            if obj["_gets"] <= self.hide_volumes_gets:
                out["volumes"] = []
            return {"server": out}
        if parts[0] == "firewalls" and len(parts) == 2:
            obj = self.firewalls.get(int(parts[1]))
            if obj is None:
                raise ApiError(404, "not_found")
            return {"firewall": copy.deepcopy(obj)}
        if parts[0] == "volumes" and len(parts) == 2:
            obj = self.volumes.get(int(parts[1]))
            if obj is None:
                raise ApiError(404, "not_found")
            return {"volume": copy.deepcopy(obj)}
        if parts[0] == "actions" and len(parts) == 2:
            act = self.actions.get(int(parts[1]))
            if act is None:
                raise ApiError(404, "not_found")
            if act["left"] > 0:
                act["left"] -= 1
            elif act["fail"]:
                act["status"] = "error"
                return {"action": {"id": act["id"], "status": "error", "command": "delete_server",
                                   "error": {"code": "action_failed", "message": "x"}}}
            elif act["status"] == "running":
                act["status"] = "success"
                if act["server"] in self.servers:
                    self._remove_server(act["server"])
            return {"action": {"id": act["id"], "status": act["status"], "command": "delete_server"}}
        if parts[0] == "primary_ips" and len(parts) == 2:
            ipid = int(parts[1])
            obj = self.primary_ips.get(ipid)
            if obj is None:
                raise ApiError(404, "not_found")
            if obj.get("_lag") is not None:
                obj["_lag"] -= 1
                if obj["_lag"] < 0:
                    del self.primary_ips[ipid]
                    raise ApiError(404, "not_found")
            return {"primary_ip": {k: copy.deepcopy(v) for k, v in obj.items() if not k.startswith("_")}}
        if parts == ["server_types"]:
            name = dict(urllib.parse.parse_qsl(query)).get("name")
            if name not in self.types:
                return {"server_types": []}
            st = {"name": name, "architecture": "x86", "deprecation": None,
                  "prices": [{"location": "fsn1", "price_hourly": {"net": self.types[name], "gross": "0"}}]}
            if self.type_locations is not None:
                st["locations"] = self.type_locations
            return {"server_types": [st]}
        if parts == ["pricing"]:
            return {"pricing": {"currency": "EUR",
                                "primary_ips": [{"type": "ipv4", "prices": [
                                    {"location": "fsn1", "price_hourly": {"net": self.ipv4_price}}]}],
                                "volume": {"price_per_gb_month": {"net": self.volume_gb_month}}}}
        if parts == ["images"]:
            return {"images": [{"id": 161547269, "name": "ubuntu-24.04", "architecture": "x86", "type": "system",
                                "status": "available", "deprecated": None}]}
        raise ApiError(404, "not_found")

    def _remove_server(self, sid):
        obj = self.servers.pop(sid)
        for fw in self.firewalls.values():
            fw["applied_to"] = [a for a in fw["applied_to"] if a["server"]["id"] != sid]
        for vid in obj.get("volumes", []):
            if vid in self.volumes:
                self.volumes[vid]["server"] = None
        ip = self.primary_ips.get(obj["public_net"]["ipv4"]["id"])
        if ip:
            if ip["auto_delete"] and self.ip_lag_gets:
                ip["_lag"] = self.ip_lag_gets           # still listed, still assigned to the dead server
            elif ip["auto_delete"]:
                del self.primary_ips[ip["id"]]
            else:
                ip["assignee_id"] = ip["assignee_type"] = None

    def add_server(self, name, labels, firewall_id=None, user_data=None, volumes=()):
        sid, ipid = self._id(), self._id()
        self.primary_ips[ipid] = {"id": ipid, "ip": f"49.12.0.{sid % 250}", "assignee_id": sid,
                                  "assignee_type": "server", "protection": {"delete": False}, "auto_delete": self.auto_delete_ip}
        fws = [{"id": firewall_id, "status": "pending"}] if firewall_id else []
        self.servers[sid] = {"id": sid, "name": name, "labels": dict(labels), "status": "initializing",
                             "protection": {"delete": False, "rebuild": False}, "private_net": [],
                             "volumes": list(volumes), "load_balancers": [], "backup_window": None,
                             "_user_data": user_data,
                             "public_net": {"ipv4": {"id": ipid, "ip": self.primary_ips[ipid]["ip"]},
                                            "ipv6": None, "firewalls": fws},
                             "created": self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")}
        if firewall_id:
            self.firewalls[firewall_id]["applied_to"].append({"type": "server", "server": {"id": sid}})
        for vid in volumes:
            self.volumes[vid]["server"] = sid
        return sid

    def add_volume(self, name, labels, size=50):
        vid = self._id()
        self.volumes[vid] = {"id": vid, "name": name, "labels": dict(labels), "size": size, "server": None,
                             "protection": {"delete": False},
                             "created": self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")}
        return vid

    def _write(self, method, path, body):
        parts = path.strip("/").split("/")
        if method == "POST" and parts == ["firewalls"]:
            fid = self._id()
            self.firewalls[fid] = {"id": fid, "name": body["name"], "labels": dict(body["labels"]),
                                   "rules": copy.deepcopy(body["rules"]), "applied_to": []}
            return {"firewall": copy.deepcopy(self.firewalls[fid]), "actions": []}
        if method == "POST" and parts == ["volumes"]:
            vid = self.add_volume(body["name"], body["labels"], body["size"])
            return {"volume": copy.deepcopy(self.volumes[vid]), "action": {}}
        if method == "POST" and parts == ["servers"]:
            fid = body["firewalls"][0]["firewall"]
            sid = self.add_server(body["name"], body["labels"], fid, body["user_data"], body.get("volumes", ()))
            server = {k: copy.deepcopy(v) for k, v in self.servers[sid].items() if not k.startswith("_")}
            return {"server": server, "action": {}, "root_password": "FAKE-ROOT-PASSWORD-DO-NOT-STORE"}
        if method == "PUT" and parts[0] == "servers":
            self.servers[int(parts[1])]["labels"] = dict(body["labels"])
            return {"server": {}}
        if method == "PUT" and parts[0] == "volumes":
            self.volumes[int(parts[1])]["labels"] = dict(body["labels"])
            return {"volume": {}}
        if method == "DELETE" and parts[0] == "servers":
            sid = int(parts[1])
            aid = self._id()
            self.actions[aid] = {"id": aid, "server": sid, "left": self.delete_action_gets, "status": "running",
                                 "fail": self.delete_action_fails}
            if self.delete_action_fails:
                self.actions[aid]["left"] = 0   # fails on the first poll; the server, firewall and address stay
            elif self.delete_action_gets:
                pass                    # the server, its firewall and its address stay until the action is done
            elif self.delete_delay_gets:
                self.servers[sid]["_deleting"] = self.delete_delay_gets
                self.actions[aid]["status"] = "success"
            else:
                self._remove_server(sid)
                self.actions[aid]["status"] = "success"
            return {"action": {"id": aid, "status": self.actions[aid]["status"], "command": "delete_server"}}
        if method == "DELETE" and parts[0] == "firewalls":
            fid = int(parts[1])
            if self.firewalls[fid]["applied_to"]:
                raise ApiError(422, "resource_in_use")
            del self.firewalls[fid]
            return {}
        if method == "DELETE" and parts[0] == "primary_ips":
            ipid = int(parts[1])
            ip = self.primary_ips.get(ipid)
            if ip is None:
                raise ApiError(404, "not_found")
            if ip.get("_lag") is not None or ip["assignee_id"] in self.servers:
                raise ApiError(422, "resource_in_use")
            del self.primary_ips[ipid]
            return {}
        if method == "DELETE" and parts[0] == "volumes":
            vid = int(parts[1])
            if self.volumes[vid]["server"] is not None:
                raise ApiError(422, "volume_attached")
            del self.volumes[vid]
            return {}
        raise ApiError(404, "not_found")


class FakeTailnet:
    """Tailnet API double. A running server enrolls as a tagged node."""

    def __init__(self, hetzner, clock):
        self.hz, self.clock = hetzner, clock
        self.lock = threading.RLock()
        self.policy = good_tailnet_policy()
        self.test_failures = 0
        self.tests_seen = []
        self.writes = []
        self.keys = {}
        self.deleted = set()
        self.enrolled = {}
        self.undeletable = set()
        base = {"user": OWNER, "tags": [], "authorized": True, "isExternal": False, "advertisedRoutes": [], "enabledRoutes": [],
                    "created": "2026-01-01T00:00:00Z"}
        self.extra_devices = [
            dict(base, nodeId="nCTRL0001", hostname="the-host", addresses=[CTRL4, CTRL6]),
            dict(base, nodeId="nUSER0001", hostname="laptop", addresses=["100.64.0.2", "fd7a:115c:a1e0::2"])]
        self.enroll = True
        self.unavailable = False
        self.key_capabilities = None

    def devices(self):
        if self.enroll:
            for sid, srv in sorted(self.hz.servers.items()):
                node = f"n{sid}NODE"
                if srv.get("status") == "running" and node not in self.enrolled:
                    self.enrolled[node] = {"nodeId": node, "hostname": srv["name"], "tags": [TAG],
                                           "enabledRoutes": [], "advertisedRoutes": [],
                                           "addresses": [f"100.80.{sid // 250 % 250}.{sid % 250}",
                                                         f"fd7a:115c:a1e0::{sid:x}"],
                                           "created": self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")}
        out = list(self.extra_devices) + [d for n, d in sorted(self.enrolled.items()) if n not in self.deleted]
        return copy.deepcopy(out)

    def request(self, method, path, body=None):
        with self.lock:
            if self.unavailable:
                raise ApiError(503, "tailnet_api_error")
            if method != "GET" and not path.endswith("/acl/validate"):
                self.writes.append((method, path, copy.deepcopy(body)))
            if path == "/tailnet/-/acl":
                return copy.deepcopy(self.policy)
            if path == "/tailnet/-/acl/validate":
                self.tests_seen.append(copy.deepcopy(body))
                return {"message": "test(s) failed", "data": [{}] * self.test_failures} if self.test_failures else {}
            if path == "/tailnet/-/devices?fields=all":
                return {"devices": self.devices()}
            if method == "POST" and path == "/tailnet/-/keys":
                kid = f"kFAKE{len(self.keys) + 1}CNTRL"
                self.keys[kid] = "active"
                caps = self.key_capabilities or body["capabilities"]
                return {"id": kid, "key": SECRET_AUTHKEY, "capabilities": copy.deepcopy(caps)}
            if method == "DELETE" and path.startswith("/tailnet/-/keys/"):
                kid = path.rsplit("/", 1)[1]
                if kid not in self.keys:
                    raise ApiError(404, "tailnet_api_error")
                self.keys[kid] = "revoked"
                return {}
            if method == "GET" and path.startswith("/device/"):
                node = path.rsplit("/", 1)[1].split("?")[0]
                for dev in self.devices():
                    if dev["nodeId"] == node:
                        return dev
                raise ApiError(404, "tailnet_api_error")
            if method == "DELETE" and path.startswith("/device/"):
                node = path.rsplit("/", 1)[1]
                if node not in self.undeletable:
                    self.deleted.add(node)
                return {}
            raise ApiError(404, "tailnet_api_error")


def good_tailnet_policy():
    return {"tagOwners": {TAG: ["autogroup:admin"]},
            "groups": {"group:admins": ["alice@example.invalid"]},
            "acls": [{"action": "accept", "src": ["autogroup:member"], "dst": ["autogroup:member:*"]},
                     {"action": "accept", "src": [CTRL4, CTRL6], "dst": [TAG + ":22"], "proto": "tcp"}],
            "ssh": [{"action": "check", "src": ["autogroup:member"], "dst": ["autogroup:self"],
                     "users": ["autogroup:nonroot"]}]}


class FakeGuest:
    """Controller-side guest channel double."""

    def __init__(self):
        self.lock = threading.Lock()
        self.ready_doc = None
        self.forward_port = None         # what a tour guest's agent would report (see Controller.readiness_problems)
        self.activity_docs = []          # consumed in order; last one repeats
        self.calls = []
        self.fail = False
        self.seal_docs = [{"schema": "motel-guest/v1", "sealed": True, "sealed_at": 1.0,
                           "verified_idle_seconds": 1900.0}]

    def call(self, rec, *words, timeout=60):
        with self.lock:
            self.calls.append((rec["lease_id"], words))
            if self.fail:
                raise GuestUnknown("guest query failed")
            if words == ("ready",):
                if self.ready_doc is not None:
                    return copy.deepcopy(self.ready_doc)
                ip = rec["access"]["address"]
                if rec["access"]["mode"] == "tailscale":
                    listeners = [{"addr": ip, "port": 22}, {"addr": "127.0.0.53", "port": 53},
                                 {"addr": ip, "port": 41112}]
                    tail = ip
                else:
                    listeners = [{"addr": "0.0.0.0", "port": 22}, {"addr": "127.0.0.53", "port": 53}]
                    tail = None
                return {"schema": "motel-guest/v1", "bootstrap": "done", "tailnet_ip": tail, "nft": "loaded",
                        "forwarding_disabled": True, "gate": "enforced",
                        "disk": "mounted" if rec.get("volume_id") else "none", "tcp_listeners": listeners,
                        "forward_port": self.forward_port}
            if words == ("activity",):
                if not self.activity_docs:
                    raise GuestUnknown("no activity configured")
                doc = self.activity_docs.pop(0) if len(self.activity_docs) > 1 else self.activity_docs[0]
                if isinstance(doc, Exception):
                    raise doc
                return copy.deepcopy(doc)
            if words == ("seal",):
                doc = self.seal_docs.pop(0) if len(self.seal_docs) > 1 else self.seal_docs[0]
                if isinstance(doc, Exception):
                    raise doc
                return copy.deepcopy(doc)
            if words and words[0] == "set-expiry":
                return {"schema": "motel-guest/v1", "expires_label": words[1]}
            raise GuestUnknown("unknown verb")


def activity(active=False, idle_s=0, age=30, unknown=()):
    return {"schema": "motel-guest/v1", "sample_age_s": age, "active": active,
            "reasons": ["ssh_session_or_process"] if active else [], "unknown": list(unknown),
            "idle_seconds": idle_s}


def section(access="ssh-allowlist", **over):
    data = {"enabled": True, "access": access, "types": ["cpx32", "cpx52", "cpx62"], "max_eur_per_hour": 0.2,
            "default_ttl": "2h", "location": "auto", "max_concurrent": 4}
    if access == "tailscale":
        data["tailscale"] = {"tag": TAG, "controller_addresses": [CTRL4, CTRL6], "allowed_tag_owners": ["autogroup:admin"],
                             "protected_test_destinations": [], "protected_device_ids": ["nCTRL0001"]}
    data.update(over)
    return data


def make_config(access="ssh-allowlist", **over):
    return cfgmod.parse_section(section(access, **over), allow_users=("alice",))


def receipt(access="ssh-allowlist", ipv6=True):
    doc = {"host": {"server_id": HOST, "public_ipv4": HOST_IPV4, "public_ipv6": HOST_IPV6 if ipv6 else None,
                    "location": "fsn1", "verified_at": "2026-11-08T10:00:00Z"},
           "hetzner": {"verified_at": "2026-11-08T10:00:00Z", "token_sha256": HZ_TOKEN_HASH,
                       "protected_not_found": [HOST]}}
    if access == "tailscale":
        doc["tailscale"] = {"verified_at": "2026-11-08T10:00:00Z", "credential_sha256": TS_CRED_HASH}
    return doc


def read_lib(name, mode="r"):
    with open(os.path.join(LIB, name), mode) as handle:
        return handle.read()


class World:
    """One controller wired to fakes under a temporary state directory."""

    def __init__(self, tmp, access="ssh-allowlist", ready=True, cfg=None, ipv6=True):
        self.clock = Clock()
        self.hz = FakeHetzner(self.clock)
        self.ts = FakeTailnet(self.hz, self.clock)
        self.guest = FakeGuest()
        self.tmp, self.access = tmp, access
        self.store = Store(os.path.join(tmp, "state"), clock=self.clock, host_id=HOST)
        self.cfg = cfg or make_config(access)
        self.receipt = receipt(access, ipv6) if ready else {}
        self.hashes = {"hetzner": HZ_TOKEN_HASH, "tailscale": TS_CRED_HASH}
        self.meta = {"instance-id": str(HOST), "public-ipv4": HOST_IPV4}

    def controller(self, uid=1000, **over):
        api = GuardedApi(self.hz, HOST)
        tailnet = None
        if self.cfg.access == "tailscale":
            ts = self.cfg.tailscale
            tailnet = TailnetApi(self.ts, tag=ts.tag, tailnet=ts.tailnet, protected_devices=ts.protected_device_ids)
        kwargs = {"receipt": self.receipt, "hetzner": api, "tailnet": tailnet, "guest": self.guest,
                      "credential_hashes": self.hashes, "caller_uid": uid, "clock": self.clock, "sleep": self.clock.sleep,
                      "keygen": fake_keygen, "template": read_lib("cloud-init.yaml"),
                      "guest_agent": read_lib("guest_agent.py", "rb"), "fetch_metadata": self.meta.__getitem__}
        kwargs.update(over)
        return ctl.Controller(self.cfg, self.store, **kwargs)

    def checkin(self, controller=None, **kw):
        controller = controller or self.controller()
        args = {"server_type": "cpx52", "ttl": timedelta(hours=2), "task": "installer", "owner": "alice",
                    "client_key": pubkey("U")}
        args.update(kw)
        return controller.checkin(**args)

    def at_minute(self, minute):
        """Put the clock ``minute`` minutes after the start of the test. Cabins the test checked in first were created
        (by the provider, too) within a few seconds of the start, so this is a point inside their first billed hour."""
        self.clock.now = self.clock.start + timedelta(minutes=minute)

    def all_files_text(self):
        out = []
        for base, _dirs, files in os.walk(self.store.root):
            for name in files:
                with open(os.path.join(base, name), "rb") as handle:
                    out.append(handle.read().decode("utf-8", "replace"))
        return "\n".join(out)


class RealGuestChannel:
    """Routes controller guest calls into a real guest agent on a temporary fixture (fake /proc, gate, lock)."""

    def __init__(self, tmp, clock):
        self.clock = clock
        self.root = os.path.join(tmp, "guest")
        self.proc = os.path.join(tmp, "proc")
        for d in ("ctl", "gate", "sessions", "jobs"):
            os.makedirs(os.path.join(self.root, d), exist_ok=True)
        os.makedirs(self.proc, exist_ok=True)
        open(os.path.join(self.root, "gate", "admission.lock"), "w").close()
        self.lease_file = os.path.join(tmp, "lease.json")
        with open(self.lease_file, "w") as handle:
            json.dump({"expires": "2030-01-01T00:00:00Z", "grace_s": 600}, handle)
        self.containers = 0
        self.next_pid = 7000
        self.calls = []
        self.before_seal = None
        self.poweroffs = []
        self.g = guest_mod.Guest(root=self.root, proc=self.proc, lease_file=self.lease_file,
                                 now=lambda: self.clock().timestamp(), uid_of=lambda _n: 1500,
                                 docker=lambda: self.containers, poweroff=lambda: self.poweroffs.append(True),
                                 pid=self._pid, seal_runner=self._seal)

    def _pid(self):
        self.next_pid += 1
        return self.next_pid

    def _seal(self):
        if self.before_seal:
            hook, self.before_seal = self.before_seal, None
            hook()
        return self.g.seal()

    def add_proc(self, pid, loginuid=1500):
        os.makedirs(os.path.join(self.proc, str(pid)), exist_ok=True)
        with open(os.path.join(self.proc, str(pid), "loginuid"), "w") as handle:
            handle.write(str(loginuid))

    def end_proc(self, pid):
        shutil.rmtree(os.path.join(self.proc, str(pid)), ignore_errors=True)

    def connect(self, original=""):
        """An SSH session through the forced-command gate. Returns (status, pid or None)."""
        execs = []
        status = self.g.gate(original, exec_fn=lambda path, argv: execs.append(argv), err=io.StringIO())
        if not execs:
            return status, None
        pid = self.next_pid
        self.add_proc(pid)
        return 0, pid

    def idle_for(self, minutes):
        for _ in range(minutes):
            self.g.sample()
            self.clock.advance(minutes=1)
        self.g.sample()

    def call(self, rec, *words, timeout=60):
        self.calls.append(words)
        try:
            return self.g.ctl(" ".join(words))
        except SystemExit as exc:
            raise GuestUnknown(str(exc)) from None
