"""The GCP provider against a fake Compute Engine API and a fake metadata server on a loopback socket."""

import json
import unittest
from decimal import Decimal
from urllib.parse import parse_qs

from _aws_gcp_http import FakeClock, LoopbackServer

from motel.errors import InventoryIncomplete, NotFound, ProviderError
from motel.providers.gcp import GcpProvider

PROJECT, ZONE, VM = "demo-project-1", "us-central1-a", "build-vm"
DISKS = f"/compute/v1/projects/{PROJECT}/zones/{ZONE}/disks"
INST = f"/compute/v1/projects/{PROJECT}/zones/{ZONE}/instances"


class FakeGce:
    def __init__(self):
        self.disks, self.attached, self.ops, self.fail = {}, {}, {}, {}  # attached: disk -> deviceName
        self.n, self.page_size, self.token_calls, self.bad_meta, self.bad_auth = 0, 500, 0, 0, 0
        self.pending_polls = 1
        self.stuck = False
        self.request_ids = []
        self.unreachable = False
        self.other_zone_disks = []

    def j(self, status, doc):
        return status, {"Content-Type": "application/json"}, json.dumps(doc).encode()

    def err(self, status, message, reason="x"):
        return self.j(status, {"error": {"code": status, "message": message, "status": "ERR", "errors": [{"reason": reason}]}})

    def handle(self, req):
        if req.path.startswith("/computeMetadata/v1/"):
            return self.meta(req)
        if req.headers.get("authorization") != "Bearer ya29.TOKEN":
            self.bad_auth += 1
            return self.err(401, "bad token")
        key = f"{req.method} {req.path.removeprefix('/compute/v1')}"
        for needle, queue in self.fail.items():
            if key.startswith(needle) and queue:
                return self.err(*queue.pop(0))
        q = {k: v[0] for k, v in parse_qs(req.query).items()}
        body = json.loads(req.body) if req.body else None
        return self.route(req.method, req.path, q, body)

    def meta(self, req):
        if req.headers.get("metadata-flavor") != "Google":
            self.bad_meta += 1
            return 403, {}, b"missing flavor"
        table = {"project/project-id": PROJECT, "instance/zone": f"projects/123456/zones/{ZONE}", "instance/name": VM}
        p = req.path.removeprefix("/computeMetadata/v1/")
        if p in table:
            return 200, {}, table[p].encode()
        if p == "instance/service-accounts/default/token":
            self.token_calls += 1
            return self.j(200, {"access_token": "ya29.TOKEN", "expires_in": 3599, "token_type": "Bearer"})
        return 404, {}, b""

    def op(self, op_type, error=None):
        self.n += 1
        name = f"operation-{self.n}"
        self.ops[name] = {"polls": 0, "error": error}
        return {"name": name, "operationType": op_type, "status": "PENDING", "zone": f"https://x/zones/{ZONE}"}

    def disk_doc(self, name):
        d = self.disks[name]
        users = [f"https://x/zones/{ZONE}/instances/{VM}"] if name in self.attached else []
        return {"name": name, "sizeGb": str(d["sizeGb"]), "labels": d["labels"], "zone": f"https://x/zones/{ZONE}", "users": users,
                "creationTimestamp": "2026-11-09T04:00:00.000-07:00", "status": "READY", "type": d["type"]}

    def route(self, method, path, q, body):
        base = f"/compute/v1/projects/{PROJECT}"
        if method == "GET" and "/operations/" in path:
            name = path.rsplit("/", 1)[1]
            o = self.ops[name]
            o["polls"] += 1
            done = o["polls"] > self.pending_polls and not self.stuck
            doc = {"name": name, "status": "DONE" if done else "RUNNING", "zone": f"https://x/zones/{ZONE}", "operationType": "x"}
            if done and o["error"]:
                doc["error"] = {"errors": [o["error"]]}
            return self.j(200, doc)
        if method == "GET" and path == f"{base}/aggregated/disks":
            items = {f"zones/{ZONE}": {"disks": [self.disk_doc(n) for n in self.disks]}}
            if self.other_zone_disks:
                items["zones/europe-west4-a"] = {"disks": self.other_zone_disks}
            if self.unreachable:
                items["zones/asia-east1-a"] = {"warning": {"code": "UNREACHABLE"}}
            return self.j(200, {"items": items})
        if method == "GET" and path == DISKS:
            names = list(self.disks)
            start = int(q.get("pageToken", "0"))
            chunk = names[start:start + self.page_size]
            doc = {"items": [self.disk_doc(n) for n in chunk]}
            if start + self.page_size < len(names):
                doc["nextPageToken"] = str(start + self.page_size)
            return self.j(200, doc)
        if method == "POST" and path == DISKS:
            self.request_ids.append(q.get("requestId"))
            assert body["type"].endswith(f"/diskTypes/{self.disk_type_expected}")
            self.disks[body["name"]] = {"sizeGb": int(body["sizeGb"]), "labels": body["labels"], "type": body["type"], "body": body}
            return self.j(200, self.op("insert"))
        if path.startswith(DISKS + "/"):
            rest = path[len(DISKS) + 1:].split("/")
            name = rest[0]
            if name not in self.disks:
                return self.err(404, f"disk {name} not found", "notFound")
            if method == "GET" and len(rest) == 1:
                return self.j(200, self.disk_doc(name))
            if method == "DELETE":
                if name in self.attached:
                    return self.j(200, self.op("delete", {"code": "RESOURCE_IN_USE_BY_ANOTHER_RESOURCE", "message": "in use"}))
                del self.disks[name]
                return self.j(200, self.op("delete"))
            if method == "POST" and rest[1:] == ["resize"]:
                self.disks[name]["sizeGb"] = int(body["sizeGb"])
                return self.j(200, self.op("resize"))
        if path.startswith(INST + "/"):
            rest = path[len(INST) + 1:].split("/")
            if rest[0] != VM:
                return self.err(404, "no such instance", "notFound")
            if method == "GET" and len(rest) == 1:
                return self.j(200, {"name": VM, "disks": [{"source": f"https://x/zones/{ZONE}/disks/{n}", "deviceName": dev}
                                                           for n, dev in self.attached.items()]})
            if method == "POST" and rest[1] == "attachDisk":
                name = body["source"].rsplit("/", 1)[1]
                assert body["deviceName"] == name and body["autoDelete"] is False and body["mode"] == "READ_WRITE"
                self.attached[name] = body["deviceName"]
                return self.j(200, self.op("attachDisk"))
            if method == "POST" and rest[1] == "detachDisk":
                dev = q["deviceName"]
                for n, d in list(self.attached.items()):
                    if d == dev:
                        del self.attached[n]
                return self.j(200, self.op("detachDisk"))
        return self.err(404, f"{method} {path}", "notFound")


class Base(unittest.TestCase):
    disk_type = "pd-balanced"

    def setUp(self):
        self.fake = FakeGce()
        self.fake.disk_type_expected = self.disk_type
        self.server = LoopbackServer(self.fake.handle).__enter__()
        self.addCleanup(self.server.__exit__, None, None, None)
        self.clock = FakeClock()
        self.provider = self.make()

    def make(self, **kw):
        kw.setdefault("disk_type", self.disk_type)
        return GcpProvider(base=self.server.url + "/compute/v1", metadata_base=self.server.url + "/computeMetadata/v1",
                           sleep=self.clock.sleep, monotonic=self.clock.monotonic, **kw)

    def labels(self, floor="f-1a2b"):
        return {"managed-by": "motel", "role": "floor", "host": VM, "floor": floor, "suite": "0", "created": "20261109t1100z"}


class TestIdentity(Base):
    def test_whoami_and_metadata_flavor_header(self):
        who = self.provider.whoami()
        self.assertEqual((who.server_id, who.location, who.provider), (VM, ZONE, "gcp"))
        self.assertEqual(self.fake.bad_meta, 0)

    def test_token_cached_and_sent_only_as_bearer(self):
        self.provider.list_owned({})
        self.provider.list_owned({})
        self.assertEqual(self.fake.token_calls, 1)
        api = [r for r in self.server.requests if r.path.startswith("/compute/")]
        self.assertTrue(all(r.headers["authorization"] == "Bearer ya29.TOKEN" for r in api))

    def test_token_refreshed_after_expiry(self):
        self.provider.list_owned({})
        self.clock.now += 4000
        self.provider.list_owned({})
        self.assertEqual(self.fake.token_calls, 2)

    def test_not_a_gce_vm(self):
        p = GcpProvider(base=self.server.url, metadata_base="http://127.0.0.1:9/x")
        with self.assertRaises(ProviderError):
            p.whoami()


class TestVolumes(Base):
    def test_create_attach_labels_device_and_request_id(self):
        vol = self.provider.create_volume("motel-f-1a2b-cd34", 50, ZONE, self.labels(), server_id=VM)
        self.assertEqual((vol.id, vol.name, vol.size_gb, vol.server_id, vol.location), ("motel-f-1a2b-cd34", "motel-f-1a2b-cd34", 50, VM, ZONE))
        self.assertEqual(vol.labels, self.labels())
        self.assertEqual(self.fake.attached, {"motel-f-1a2b-cd34": "motel-f-1a2b-cd34"})
        self.assertEqual(self.provider.device_path(vol.id), "/dev/disk/by-id/google-motel-f-1a2b-cd34")
        self.assertEqual(self.provider.expected_serial(vol.id), "motel-f-1a2b-cd34")
        self.assertEqual(len(self.fake.request_ids[0]), 36)
        self.assertEqual(self.fake.bad_auth, 0)

    def test_request_id_is_stable_per_disk_name(self):
        self.provider.create_volume("motel-f-1a2b-cd34", 50, ZONE, self.labels())
        self.fake.disks.clear()
        self.provider.create_volume("motel-f-1a2b-cd34", 50, ZONE, self.labels())
        self.assertEqual(self.fake.request_ids[0], self.fake.request_ids[1])

    def test_create_without_server(self):
        vol = self.provider.create_volume("motel-f-1a2b-cd34", 50, ZONE, self.labels())
        self.assertIsNone(vol.server_id)
        self.assertEqual(self.fake.attached, {})

    def test_operation_error_in_create_is_reported_for_reconciliation(self):
        orig = self.fake.op
        self.fake.op = lambda t, e=None: orig(t, {"code": "QUOTA_EXCEEDED", "message": "Quota 'DISKS_TOTAL_GB' exceeded"})
        with self.assertRaises(ProviderError) as ctx:
            self.provider.create_volume("motel-f-1a2b-cd34", 50, ZONE, self.labels())
        self.assertTrue(ctx.exception.indeterminate)  # create failed inside the wait: the desk reconciles by label
        self.assertIn("QUOTA_EXCEEDED", ctx.exception.message)

    def test_attach_error_after_create_is_indeterminate_and_findable(self):
        self.fake.fail[f"POST /projects/{PROJECT}/zones/{ZONE}/instances"] = [(403, "no compute.instances.attachDisk", "forbidden")]
        with self.assertRaises(ProviderError) as ctx:
            self.provider.create_volume("motel-f-1a2b-cd34", 50, ZONE, self.labels(), server_id=VM)
        self.assertTrue(ctx.exception.indeterminate)
        self.assertEqual([v.name for v in self.provider.list_owned({"floor": "f-1a2b"})], ["motel-f-1a2b-cd34"])

    def test_5xx_on_insert_is_indeterminate_not_retried(self):
        self.fake.fail["POST " + DISKS.removeprefix("/compute/v1")] = [(503, "backend down", "backendError2")]
        with self.assertRaises(ProviderError) as ctx:
            self.provider.create_volume("motel-f-1a2b-cd34", 50, ZONE, self.labels())
        self.assertTrue(ctx.exception.indeterminate)
        self.assertEqual(len([r for r in self.server.requests if r.method == "POST"]), 1)

    def test_rate_limited_write_is_retried(self):
        self.fake.fail["POST " + DISKS.removeprefix("/compute/v1")] = [(429, "slow", "rateLimitExceeded"), (403, "slow", "rateLimitExceeded")]
        self.provider.create_volume("motel-f-1a2b-cd34", 50, ZONE, self.labels())
        self.assertEqual(self.clock.slept[:2], [1.0, 2.0])
        self.assertEqual(len(self.fake.disks), 1)

    def test_plain_403_is_not_retried(self):
        self.fake.fail["POST " + DISKS.removeprefix("/compute/v1")] = [(403, "denied", "forbidden")]
        with self.assertRaises(ProviderError) as ctx:
            self.provider.create_volume("motel-f-1a2b-cd34", 50, ZONE, self.labels())
        self.assertFalse(ctx.exception.indeterminate)

    def test_read_retries_on_5xx(self):
        self.fake.fail["GET " + DISKS.removeprefix("/compute/v1")] = [(500, "x", "x"), (503, "x", "x")]
        self.assertEqual(self.provider.list_owned({}), [])

    def test_operation_timeout_is_indeterminate(self):
        self.fake.stuck = True
        with self.assertRaises(ProviderError) as ctx:
            self.provider.create_volume("motel-f-1a2b-cd34", 50, ZONE, self.labels())
        self.assertTrue(ctx.exception.indeterminate)
        self.assertGreaterEqual(self.clock.now, 1300.0)

    def test_wrong_zone_or_server_refused(self):
        with self.assertRaises(ProviderError):
            self.provider.create_volume("motel-f-1a2b-cd34", 50, "europe-west4-a", self.labels())
        with self.assertRaises(ProviderError):
            self.provider.create_volume("motel-f-1a2b-cd34", 50, ZONE, self.labels(), server_id="other-vm")

    def test_bad_labels_refused_before_any_call(self):
        with self.assertRaises(ProviderError):
            self.provider.create_volume("motel-f-1a2b-cd34", 50, ZONE, {"Managed": "x"})
        with self.assertRaises(ProviderError):
            self.provider.create_volume("motel-f-1a2b-cd34", 50, ZONE, {"host": "Has.Dots"})
        self.assertEqual(self.fake.disks, {})

    def test_detach_uses_device_name_from_instance_and_delete(self):
        vol = self.provider.create_volume("motel-f-1a2b-cd34", 50, ZONE, self.labels(), server_id=VM)
        with self.assertRaises(ProviderError):
            self.provider.delete(vol.id)  # still attached: the operation reports the error
        self.provider.detach(vol.id)
        self.assertIsNone(self.provider.get_volume(vol.id).server_id)
        n = len(self.server.requests)
        self.provider.detach(vol.id)  # already detached: only the read
        self.assertEqual(len(self.server.requests), n + 1)
        self.provider.delete(vol.id)
        with self.assertRaises(NotFound):
            self.provider.get_volume(vol.id)

    def test_resize_grow_only(self):
        vol = self.provider.create_volume("motel-f-1a2b-cd34", 50, ZONE, self.labels(), server_id=VM)
        self.provider.resize(vol.id, 80)
        self.assertEqual(self.provider.get_volume(vol.id).size_gb, 80)
        with self.assertRaises(ProviderError):
            self.provider.resize(vol.id, 80)

    def test_odd_names_refused(self):
        for bad in ("Upper", "a/b", "../x", "", "-x", "x" * 64):
            with self.assertRaises(ProviderError):
                self.provider.get_volume(bad)

    def test_disk_attached_elsewhere_reports_that_server(self):
        self.fake.disks["motel-x"] = {"sizeGb": 10, "labels": {}, "type": "t"}
        self.fake.attached["motel-x"] = "motel-x"
        doc = self.fake.disk_doc("motel-x")
        doc["users"] = [f"https://x/zones/{ZONE}/instances/other-vm", f"https://x/zones/{ZONE}/instances/third"]
        self.fake.disk_doc = lambda name, d=doc: d
        self.assertEqual(self.provider.get_volume("motel-x").server_id, "other-vm,third")  # never equals this host
        with self.assertRaises(ProviderError):
            self.provider.detach("motel-x")


class TestListing(Base):
    def seed(self, n):
        for i in range(n):
            self.fake.disks[f"motel-d{i}"] = {"sizeGb": 10, "labels": {"managed-by": "motel", "floor": f"f-{i}"}, "type": "t"}

    def test_pagination_via_page_token(self):
        self.fake.page_size = 2
        self.seed(5)
        self.assertEqual(len(self.provider.list_owned({"managed-by": "motel"})), 5)
        self.assertEqual(len([r for r in self.server.requests if r.path == DISKS and "pageToken" in r.query]), 2)

    def test_label_filter_is_exact(self):
        self.seed(3)
        self.assertEqual([v.labels["floor"] for v in self.provider.list_owned({"managed-by": "motel", "floor": "f-1"})], ["f-1"])

    def test_visible_is_project_wide_and_includes_this_vm(self):
        self.seed(1)
        self.fake.other_zone_disks = [{"name": "scratch", "sizeGb": "100", "zone": "https://x/zones/europe-west4-a", "labels": {}}]
        inv = self.provider.visible()
        self.assertEqual(sorted(v.id for v in inv.volumes), ["motel-d0", "scratch"])
        self.assertEqual(inv.servers, (VM,))

    def test_unreachable_location_is_incomplete(self):
        self.fake.unreachable = True
        with self.assertRaises(InventoryIncomplete):
            self.provider.visible()

    def test_listing_error_is_incomplete(self):
        self.fake.fail["GET " + DISKS.removeprefix("/compute/v1")] = [(403, "no", "forbidden")]
        with self.assertRaises(InventoryIncomplete):
            self.provider.list_owned({})


class TestHyperdiskAndPrice(Base):
    disk_type = "hyperdisk-balanced"

    def test_hyperdisk_type_and_iops_are_sent(self):
        p = self.make(provisioned_iops=3000, provisioned_throughput=140)
        p.create_volume("motel-f-1a2b-cd34", 50, ZONE, self.labels())
        body = self.fake.disks["motel-f-1a2b-cd34"]["body"]
        self.assertEqual((body["provisionedIops"], body["provisionedThroughput"]), ("3000", "140"))

    def test_price_table(self):
        self.assertEqual(GcpProvider(disk_type="pd-balanced").price_gb_month("us-central1-a"), Decimal("0.10"))
        with self.assertRaises(ProviderError) as ctx:
            self.provider.price_gb_month(ZONE)  # hyperdisk-balanced has no built-in price
        self.assertIn("price_gb_month_eur", ctx.exception.hint)
        p = self.make(prices={"hyperdisk-balanced": {"us-central1": Decimal("0.08")}})
        self.assertEqual(p.price_gb_month(ZONE), Decimal("0.08"))
        self.assertEqual(p.currency, "USD")

    def test_bad_type(self):
        with self.assertRaises(ProviderError):
            GcpProvider(disk_type="local-ssd")


class TestNoRedirect(unittest.TestCase):
    def test_redirect_is_refused(self):
        def handler(req):
            if req.path.startswith("/computeMetadata"):
                return 200, {}, json.dumps({"access_token": "t", "expires_in": 100}).encode() if "token" in req.path else b"demo-project-1"
            return 302, {"Location": "http://127.0.0.1:9/evil"}, b""
        with LoopbackServer(handler) as srv:
            p = GcpProvider(base=srv.url + "/compute/v1", metadata_base=srv.url + "/computeMetadata/v1", sleep=lambda s: None)
            p._ident = (PROJECT, ZONE, VM)
            with self.assertRaises(ProviderError):
                p.delete("motel-x")
            self.assertEqual(len([r for r in srv.requests if r.path.startswith("/compute/v1")]), 1)


class TestDocs(unittest.TestCase):
    def test_role_json_in_the_docs_parses(self):
        import re
        from pathlib import Path
        text = (Path(__file__).resolve().parents[2] / "docs" / "providers" / "gcp.md").read_text()
        blocks = [json.loads(b) for b in re.findall(r"```json\n(.*?)```", text, re.S)]
        roles = [b for b in blocks if "includedPermissions" in b]
        self.assertEqual(len(roles), 2)
        self.assertTrue(all(p.startswith("compute.") for r in roles for p in r["includedPermissions"]))
        self.assertTrue(any("expression" in b for b in blocks))


if __name__ == "__main__":
    unittest.main()
