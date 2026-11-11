"""The Azure provider against a loopback fake of IMDS (compute + managed-identity token), ARM and the retail price API."""

import copy
import unittest

from _scw_azure_http import Clock, Loopback, Reply

from motel.errors import InventoryIncomplete, NotFound, ProviderError
from motel.providers.azure import AzureProvider, billable_gb, labels_to_tags

SUB = "0aaaaaaa-1111-4222-8333-444444444444"
RG = "motel-rg"
VM = "box1"
REGION = "westeurope"
SCOPE = f"/subscriptions/{SUB}/resourceGroups/{RG}/providers/Microsoft.Compute"
LABELS = {"managed-by": "motel", "role": "floor", "host": VM, "floor": "f1", "suite": "0", "created": "20261109t0000z"}


def disk_id(name):
    return f"{SCOPE}/disks/{name}"


class FakeAzure:
    def __init__(self):
        self.base = None
        self.disks, self.fail, self.ops, self.before_patch = {}, {}, {}, []
        self.page_size, self.op_polls, self.op_result, self.stuck = 50, 1, "Succeeded", False
        self.no_etag, self.etag_n, self.token_n, self.now = False, 1, 0, 1_800_000_000.0
        self.next_link_override = None
        self.vm = {"name": VM, "location": REGION, "zones": ["1"], "etag": '"1"',
                   "properties": {"storageProfile": {"dataDisks": [], "diskControllerType": "SCSI"}}}
        self.imds_disks = None  # None: mirror the VM
        self.price_items = [{"meterName": "E10 LRS Disk", "currencyCode": "EUR", "retailPrice": 8.618, "productName": "Standard SSD Managed Disks"}]

    # -- fixtures
    def add_disk(self, name, tags=None, size=50, managed_by=None, zones=("1",), location=REGION):
        self.disks[name] = {"id": disk_id(name), "name": name, "location": location, "zones": list(zones), "tags": dict(tags or {}),
                            "sku": {"name": "StandardSSD_LRS"}, "managedBy": managed_by,
                            "properties": {"diskSizeGB": size, "timeCreated": "2026-11-01T10:00:00.1234567+00:00", "diskState": "Attached" if managed_by else "Unattached"}}
        return self.disks[name]

    def vm_disks(self):
        return self.vm["properties"]["storageProfile"]["dataDisks"]

    def foreign_attach(self, name, lun):
        """Somebody else attaches a disk to the VM (changes the ETag)."""
        self.add_disk(name, managed_by=f"{SCOPE}/virtualMachines/{VM}")
        self.vm_disks().append({"lun": lun, "name": name, "createOption": "Attach", "caching": "ReadOnly", "writeAcceleratorEnabled": False,
                                "deleteOption": "Detach", "diskSizeGB": 128, "toBeDetached": False,
                                "managedDisk": {"id": disk_id(name), "storageAccountType": "Premium_LRS"}})
        self.bump()

    def bump(self):
        self.etag_n += 1
        self.vm["etag"] = f'"{self.etag_n}"'

    def vm_doc(self):
        doc = copy.deepcopy(self.vm)
        if self.no_etag:
            doc.pop("etag")
        return doc

    # -- http
    def _op(self, kind):
        n = len(self.ops) + 1
        self.ops[n] = {"polls": 0, "kind": kind}
        return {"Azure-AsyncOperation": f"{self.base}/ops/{n}"}

    def handle(self, req):  # noqa: C901
        p = req.path
        if p == "/metadata/instance/compute":
            assert req.headers.get("metadata") == "true" and "x-forwarded-for" not in req.headers
            assert req.query["api-version"] and req.query["format"] == "json"
            seen = self.vm_disks() if self.imds_disks is None else self.imds_disks
            return Reply(200, {"subscriptionId": SUB, "resourceGroupName": RG, "name": VM, "location": REGION, "zone": "1", "vmId": "abc",
                               "storageProfile": {"dataDisks": [{"lun": str(d["lun"]), "name": d["name"]} for d in seen]}})
        if p == "/metadata/identity/oauth2/token":
            assert req.headers.get("metadata") == "true"
            assert req.query["resource"] == "https://management.azure.com/"
            self.token_n += 1
            return Reply(200, {"access_token": f"tok-{self.token_n}", "expires_on": str(int(self.now + 3600)), "resource": req.query["resource"]})
        if p == "/api/retail/prices":
            assert "authorization" not in req.headers, "the ARM token must not go to the price API"
            return Reply(200, {"Items": self.price_items, "NextPageLink": None, "Count": len(self.price_items)})
        if p.startswith("/ops/"):
            assert req.headers["authorization"].startswith("Bearer tok-")
            op = self.ops[int(p.rsplit("/", 1)[1])]
            op["polls"] += 1
            if self.stuck or op["polls"] <= self.op_polls:
                return Reply(200, {"status": "InProgress"}, {"Retry-After": "1"})
            if self.op_result == "Failed":
                return Reply(200, {"status": "Failed", "error": {"code": "BadThing", "message": "it broke"}})
            return Reply(200, {"status": "Succeeded"})
        assert req.headers.get("authorization", "").startswith("Bearer tok-"), "ARM calls need the managed-identity token"
        for (m, prefix), codes in self.fail.items():
            if m == req.method and p.startswith(prefix) and codes:
                code = codes.pop(0)
                return Reply(code, {"error": {"code": "Injected", "message": "injected"}}, {"Retry-After": "2"} if code == 429 else {})
        if not p.startswith(SCOPE + "/"):
            return Reply(404, {"error": {"code": "NotFound", "message": p}})
        rest = p[len(SCOPE) + 1:].split("/")
        kind, name = rest[0], (rest[1] if len(rest) > 1 else None)
        if kind == "virtualMachines":
            assert req.query["api-version"] == "2025-04-01"
            if name is None:
                return Reply(200, {"value": [{"name": VM}, {"name": "other-vm"}]})
            if name.lower() != VM:
                return Reply(404, {"error": {"code": "ResourceNotFound", "message": "no such vm"}})
            if req.method == "GET":
                return Reply(200, self.vm_doc(), {"ETag": self.vm["etag"]})
            if req.method == "PATCH":
                if self.before_patch:
                    self.before_patch.pop(0)()
                if req.headers.get("if-match") != self.vm["etag"]:
                    return Reply(412, {"error": {"code": "PreconditionFailed", "message": "etag"}})
                new = req.body["properties"]["storageProfile"]["dataDisks"]
                gone = {d["name"] for d in self.vm_disks()} - {d["name"] for d in new}
                for d in new:
                    if d["name"] in self.disks:
                        self.disks[d["name"]]["managedBy"] = f"{SCOPE}/virtualmachines/{VM.upper()}"
                for g in gone:
                    self.disks[g]["managedBy"] = None
                self.vm["properties"]["storageProfile"]["dataDisks"] = new
                self.bump()
                return Reply(202, None, self._op("vm"))
        if kind == "disks":
            assert req.query["api-version"] == "2025-01-02"
            if name is None:
                names = sorted(self.disks)
                start = int(req.query.get("skip", 0))
                chunk = [self.disks[n] for n in names[start:start + self.page_size]]
                doc = {"value": chunk}
                if start + self.page_size < len(names):
                    doc["nextLink"] = self.next_link_override or f"{self.base}{p}?api-version=2025-01-02&skip={start + self.page_size}"
                return Reply(200, doc)
            if req.method == "PUT":
                b = req.body
                assert b["properties"]["creationData"] == {"createOption": "Empty"}
                self.add_disk(name, b["tags"], b["properties"]["diskSizeGB"], zones=b.get("zones", []), location=b["location"])
                self.disks[name]["_body"] = b
                return Reply(201, {k: v for k, v in self.disks[name].items() if k != "_body"}, self._op("disk"))
            if name not in self.disks:
                return Reply(404, {"error": {"code": "ResourceNotFound", "message": "no such disk"}})
            d = self.disks[name]
            if req.method == "GET":
                return Reply(200, {k: v for k, v in d.items() if k != "_body"})
            if req.method == "PATCH":
                d["properties"]["diskSizeGB"] = req.body["properties"]["diskSizeGB"]
                return Reply(200, {k: v for k, v in d.items() if k != "_body"}, self._op("disk"))
            if req.method == "DELETE":
                assert d["managedBy"] is None
                del self.disks[name]
                return Reply(202, None, self._op("disk"))
        return Reply(404, {"error": {"code": "NotFound", "message": f"{req.method} {p}"}})


class AzureTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeAzure()
        self.loop = Loopback(self.fake.handle)
        self.fake.base = self.loop.base
        self.addCleanup(self.loop.close)
        self.clock = Clock()
        self.p = self.make()

    def make(self, **kw):
        return AzureProvider(base=self.loop.base, imds_base=self.loop.base, retail_base=self.loop.base, sleep=self.clock.sleep,
                             monotonic=self.clock.monotonic, clock=lambda: self.fake.now + self.clock.now - 1000.0, **kw)

    # -- identity and token
    def test_whoami_reads_the_vm_name_and_region_from_imds(self):
        who = self.p.whoami()
        self.assertEqual((who.server_id, who.location, who.provider), (VM, REGION, "azure"))

    def test_whoami_refuses_odd_metadata(self):
        base = self.fake.handle
        for patch in ({"name": "../x"}, {"subscriptionId": "nope"}, {"location": "Mars Base"}, {"resourceGroupName": "a/b"}):
            def handler(req, patch=patch):
                reply = base(req)
                if req.path == "/metadata/instance/compute":
                    reply.doc = {**reply.doc, **patch}
                return reply
            self.loop.handler = handler
            with self.assertRaises(ProviderError):
                self.make().whoami()

    def test_the_token_is_cached_then_renewed_before_it_expires(self):
        self.fake.add_disk("d1")
        self.p.get_volume("d1")
        self.p.get_volume("d1")
        self.assertEqual(self.fake.token_n, 1)
        self.clock.now += 3400  # inside the five-minute safety margin
        self.p.get_volume("d1")
        self.assertEqual(self.fake.token_n, 2)
        self.assertEqual(self.loop.seen("GET", SCOPE + "/disks/d1")[-1].headers["authorization"], "Bearer tok-2")

    def test_a_user_assigned_identity_is_requested_by_client_id(self):
        self.make(identity={"client_id": "cid-1"}).whoami()
        self.fake.add_disk("d1")
        self.make(identity={"client_id": "cid-1"}).get_volume("d1")
        self.assertEqual(self.loop.seen("GET", "/metadata/identity")[0].query["client_id"], "cid-1")

    def test_imds_is_retried_when_it_is_updating(self):
        calls = {"n": 0}
        base = self.fake.handle

        def handler(req):
            if req.path == "/metadata/instance/compute":
                calls["n"] += 1
                if calls["n"] < 3:
                    return Reply(404, {"error": "updating"})
            return base(req)
        self.loop.handler = handler
        self.assertEqual(self.make().whoami().server_id, VM)
        self.assertEqual(calls["n"], 3)

    def test_no_identity_on_the_vm_is_explained(self):
        base = self.fake.handle
        self.loop.handler = lambda req: Reply(200, {"access_token": ""}) if req.path.startswith("/metadata/identity") else base(req)
        with self.assertRaises(ProviderError) as ctx:
            self.make().get_volume("d1")
        self.assertIn("managed identity", ctx.exception.message)

    # -- reads
    def test_visible_follows_next_link_and_lists_vms(self):
        for n in range(120):
            self.fake.add_disk(f"disk{n:03d}", managed_by=f"{SCOPE}/virtualMachines/Box1" if n == 0 else None)
        inv = self.p.visible()
        self.assertEqual((len(inv.volumes), inv.servers, inv.complete), (120, (VM, "other-vm"), True))
        by = {v.id: v for v in inv.volumes}
        self.assertEqual(by["disk000"].server_id, VM)  # ARM's case is normalised
        self.assertIsNone(by["disk001"].server_id)

    def test_a_next_link_that_leaves_arm_is_refused_and_gets_no_token(self):
        sink = Loopback(lambda req: Reply(200, {"value": []}))
        self.addCleanup(sink.close)
        for n in range(3):
            self.fake.add_disk(f"disk{n}")
        self.fake.page_size = 1
        self.fake.next_link_override = sink.base + "/steal?x=1"
        with self.assertRaises(InventoryIncomplete):
            self.p.visible()
        self.assertEqual(sink.requests, [])

    def test_visible_raises_when_a_page_fails(self):
        self.fake.add_disk("d1")
        self.fake.fail[("GET", SCOPE + "/disks")] = [403]
        with self.assertRaises(InventoryIncomplete) as ctx:
            self.p.visible()
        self.assertIn("role assignments", ctx.exception.message)

    def test_get_volume_maps_fields(self):
        self.fake.add_disk("d1", labels_to_tags(LABELS), size=75, managed_by=f"{SCOPE}/virtualMachines/{VM}")
        vol = self.p.get_volume("d1")
        self.assertEqual((vol.id, vol.name, vol.size_gb, vol.server_id, vol.location, vol.delete_protected), ("d1", "d1", 75, VM, REGION, False))
        self.assertEqual(vol.labels, LABELS)
        self.assertEqual(vol.created.year, 2026)
        with self.assertRaises(NotFound):
            self.p.get_volume("missing")
        with self.assertRaises(ProviderError):
            self.p.get_volume("../vm")

    def test_a_disk_held_by_another_group_never_looks_like_this_vm(self):
        self.fake.add_disk("d1", managed_by="/subscriptions/x/resourceGroups/OTHER/providers/Microsoft.Compute/virtualMachines/box1")
        self.assertNotEqual(self.p.get_volume("d1").server_id, VM)
        self.fake.add_disk("d2", managed_by=f"{SCOPE}/virtualMachines/other-vm")
        self.assertEqual(self.p.get_volume("d2").server_id, "other-vm")

    def test_list_owned_needs_every_label(self):
        self.fake.add_disk("a", LABELS)
        self.fake.add_disk("b", {**LABELS, "host": "other-vm"})
        self.fake.add_disk("c")
        self.assertEqual([v.id for v in self.p.list_owned({"managed-by": "motel", "host": VM})], ["a"])

    # -- price
    def test_price_per_gib_comes_from_the_retail_api_for_the_configured_sku(self):
        self.assertEqual(str(self.p.price_gb_month(REGION)), "0.067328")
        query = self.loop.seen("GET", "/api/retail")[0].query
        self.assertIn("armRegionName eq 'westeurope'", query["$filter"])
        self.assertIn("skuName eq 'E10 LRS'", query["$filter"])
        self.assertEqual(query["currencyCode"], "'EUR'")

    def test_price_failures_and_unknown_skus_point_at_the_override(self):
        self.fake.price_items = []
        for provider, region in ((self.p, REGION), (self.make(sku="UltraSSD_LRS"), REGION), (self.p, "../x")):
            with self.assertRaises(ProviderError) as ctx:
                provider.price_gb_month(region)
            self.assertIn("price_gb_month_eur", ctx.exception.hint)

    def test_billable_size_is_the_next_tier(self):
        self.assertEqual([billable_gb(n) for n in (1, 4, 5, 100, 128, 129, 200, 1025, 40000)], [4, 4, 8, 128, 128, 256, 256, 2048, 40000])
        self.assertEqual(self.p.billable_gb(200), 256)

    # -- create
    def test_create_builds_a_zonal_tagged_disk_and_attaches_it_at_lun_0(self):
        vol = self.p.create_volume("motel-f1-ab", 50, REGION, LABELS, server_id=VM)
        body = self.fake.disks["motel-f1-ab"]["_body"]
        self.assertEqual((body["sku"], body["zones"], body["location"]), ({"name": "StandardSSD_LRS"}, ["1"], REGION))
        self.assertEqual((body["tags"], body["properties"]["diskSizeGB"], body["properties"]["networkAccessPolicy"]), (LABELS, 50, "DenyAll"))
        self.assertEqual((vol.server_id, vol.size_gb, vol.labels["floor"]), (VM, 50, "f1"))
        entry = self.fake.vm_disks()[0]
        self.assertEqual((entry["lun"], entry["name"], entry["createOption"], entry["managedDisk"]["id"]), (0, "motel-f1-ab", "Attach", disk_id("motel-f1-ab")))
        self.assertGreaterEqual(len(self.loop.seen("GET", "/ops/")), 2)  # it waited for both operations

    def test_zrs_disks_carry_no_zone_and_export_denial_is_optional(self):
        self.p = self.make(sku="StandardSSD_ZRS", deny_export=False)
        self.p.create_volume("motel-f1-ab", 50, REGION, LABELS)
        body = self.fake.disks["motel-f1-ab"]["_body"]
        self.assertNotIn("zones", body)
        self.assertNotIn("networkAccessPolicy", body["properties"])
        self.assertEqual(self.loop.seen("PATCH"), [])

    def test_create_refuses_what_arm_would_reject(self):
        for args in (("motel-f1-ab", 0, REGION), ("motel-f1-ab", 40000, REGION), ("motel-f1-ab", 50, "northeurope"), ("../x", 50, REGION)):
            with self.assertRaises(ProviderError):
                self.p.create_volume(*args, LABELS)
        with self.assertRaises(ProviderError):
            self.p.create_volume("motel-f1-ab", 50, REGION, {"bad/key": "x"})
        self.assertEqual(self.loop.seen("PUT"), [])

    def test_a_failed_attach_after_create_is_indeterminate(self):
        self.fake.fail[("PATCH", SCOPE + "/virtualMachines")] = [500]
        with self.assertRaises(ProviderError) as ctx:
            self.p.create_volume("motel-f1-ab", 50, REGION, LABELS, server_id=VM)
        self.assertTrue(ctx.exception.indeterminate)
        self.assertIn("motel-f1-ab", self.fake.disks)

    def test_a_server_error_on_create_is_indeterminate_and_not_retried(self):
        self.fake.fail[("PUT", SCOPE + "/disks")] = [503]
        with self.assertRaises(ProviderError) as ctx:
            self.p.create_volume("motel-f1-ab", 50, REGION, LABELS)
        self.assertTrue(ctx.exception.indeterminate)
        self.assertEqual(len(self.loop.seen("PUT")), 1)

    def test_a_lost_create_response_is_indeterminate(self):
        base = self.fake.handle
        self.loop.handler = lambda req: Reply(drop=True) if req.method == "PUT" else base(req)
        with self.assertRaises(ProviderError) as ctx:
            self.p.create_volume("motel-f1-ab", 50, REGION, LABELS)
        self.assertTrue(ctx.exception.indeterminate)
        self.assertEqual(len(self.loop.seen("PUT")), 1)

    def test_429_is_retried_for_writes_after_retry_after(self):
        self.fake.fail[("PUT", SCOPE + "/disks")] = [429]
        self.p.create_volume("motel-f1-ab", 50, REGION, LABELS)
        self.assertEqual(len(self.loop.seen("PUT")), 2)
        self.assertIn(2.0, self.clock.slept)

    # -- long-running operations
    def test_a_failed_operation_is_a_plain_failure(self):
        self.fake.op_result = "Failed"
        with self.assertRaises(ProviderError) as ctx:
            self.p.create_volume("motel-f1-ab", 50, REGION, LABELS)
        self.assertFalse(ctx.exception.indeterminate)
        self.assertIn("BadThing", ctx.exception.message)

    def test_an_operation_that_never_finishes_is_indeterminate(self):
        self.fake.stuck = True
        with self.assertRaises(ProviderError) as ctx:
            self.p.create_volume("motel-f1-ab", 50, REGION, LABELS)
        self.assertTrue(ctx.exception.indeterminate)

    def test_an_operation_url_off_arm_is_refused(self):
        base = self.fake.handle

        def handler(req):
            reply = base(req)
            if req.method == "PUT":
                reply.headers["Azure-AsyncOperation"] = "http://127.0.0.1:9/ops/1"
            return reply
        self.loop.handler = handler
        with self.assertRaises(ProviderError):
            self.p.create_volume("motel-f1-ab", 50, REGION, LABELS)

    def test_location_polling_ends_on_the_first_non_202(self):
        state = {"n": 0}
        base = self.fake.handle

        def handler(req):
            if req.method == "DELETE":
                return Reply(202, None, {"Location": f"{self.loop.base}/loc/1", "Retry-After": "3"})
            if req.path == "/loc/1":
                state["n"] += 1
                return Reply(202 if state["n"] < 3 else 200, None)
            return base(req)
        self.loop.handler = handler
        self.fake.add_disk("d1")
        self.p.delete("d1")
        self.assertEqual(state["n"], 3)

    # -- attach and the ETag
    def test_attach_takes_the_lowest_free_lun_and_keeps_every_other_field(self):
        for name, lun in (("x0", 0), ("x1", 1), ("x3", 3)):
            self.fake.foreign_attach(name, lun)
        before = copy.deepcopy(self.fake.vm_disks())
        self.fake.add_disk("d1")
        self.p.attach("d1", VM)
        after = self.fake.vm_disks()
        self.assertEqual(after[:3], before)  # nothing about the existing disks was rewritten
        self.assertEqual((after[3]["lun"], after[3]["name"]), (2, "d1"))
        patch = self.loop.seen("PATCH", SCOPE + "/virtualMachines")[0]
        self.assertTrue(patch.headers["if-match"].startswith('"'))

    def test_a_concurrent_change_gives_412_then_the_vm_is_read_again(self):
        self.fake.add_disk("d1")
        self.fake.before_patch.append(lambda: self.fake.foreign_attach("sneaky", 0))
        self.p.attach("d1", VM)
        entries = {d["name"]: d for d in self.fake.vm_disks()}
        self.assertEqual(set(entries), {"sneaky", "d1"})  # the other writer's disk was not clobbered
        self.assertEqual((entries["sneaky"]["lun"], entries["d1"]["lun"]), (0, 1))
        self.assertEqual(entries["sneaky"]["caching"], "ReadOnly")
        patches = self.loop.seen("PATCH", SCOPE + "/virtualMachines")
        self.assertEqual(len(patches), 2)
        self.assertNotEqual(patches[0].headers["if-match"], patches[1].headers["if-match"])
        self.assertEqual([len(p.body["properties"]["storageProfile"]["dataDisks"]) for p in patches], [1, 2])

    def test_a_vm_that_keeps_changing_is_given_up_on(self):
        self.fake.add_disk("d1")
        self.fake.before_patch.extend([lambda n=n: self.fake.bump() for n in range(10)])
        with self.assertRaises(ProviderError) as ctx:
            self.p.attach("d1", VM)
        self.assertIn("kept changing", ctx.exception.message)
        self.assertEqual(self.fake.vm_disks(), [])

    def test_a_vm_without_an_etag_is_never_written(self):
        self.fake.add_disk("d1")
        self.fake.no_etag = True
        base = self.fake.handle
        self.loop.handler = lambda req: Reply(200, self.fake.vm_doc()) if req.method == "GET" and "/virtualMachines/" in req.path else base(req)
        with self.assertRaises(ProviderError) as ctx:
            self.p.attach("d1", VM)
        self.assertIn("ETag", ctx.exception.message)
        self.assertEqual(self.loop.seen("PATCH"), [])

    def test_the_etag_header_is_used_when_the_body_has_none(self):
        self.fake.add_disk("d1")
        self.fake.no_etag = True
        base = self.fake.handle
        self.loop.handler = lambda req: Reply(200, self.fake.vm_doc(), {"ETag": self.fake.vm["etag"]}) if req.method == "GET" and "/virtualMachines/" in req.path else base(req)
        self.p.attach("d1", VM)
        self.assertEqual(self.loop.seen("PATCH")[0].headers["if-match"], '"1"')

    def test_attach_is_idempotent(self):
        self.fake.add_disk("d1")
        self.p.attach("d1", VM)
        self.p.attach("d1", VM.upper())
        self.assertEqual(len(self.loop.seen("PATCH")), 1)

    def test_attach_refuses_a_disk_held_elsewhere_or_in_another_zone(self):
        self.fake.add_disk("held", managed_by=f"{SCOPE}/virtualMachines/other-vm")
        self.fake.add_disk("z2", zones=("2",))
        for name in ("held", "z2"):
            with self.assertRaises(ProviderError):
                self.p.attach(name, VM)
        with self.assertRaises(NotFound):
            self.p.attach("d1", "no-such-vm")
        self.assertEqual(self.loop.seen("PATCH"), [])

    def test_attach_refuses_when_the_vm_is_full_of_luns(self):
        for lun in range(64):
            self.fake.vm_disks().append({"lun": lun, "name": f"f{lun}", "managedDisk": {"id": disk_id(f"f{lun}")}})
        self.fake.add_disk("d1")
        with self.assertRaises(ProviderError):
            self.p.attach("d1", VM)

    def test_a_disk_being_detached_is_not_treated_as_attached(self):
        self.fake.foreign_attach("d1", 0)
        self.fake.vm_disks()[0]["toBeDetached"] = True
        with self.assertRaises(ProviderError):
            self.p.attach("d1", VM)

    def test_a_lost_attach_response_is_indeterminate_and_a_retry_is_safe(self):
        self.fake.add_disk("d1")
        base = self.fake.handle
        self.loop.handler = lambda req: Reply(drop=True) if req.method == "PATCH" else base(req)
        with self.assertRaises(ProviderError) as ctx:
            self.p.attach("d1", VM)
        self.assertTrue(ctx.exception.indeterminate)
        self.loop.handler = base
        self.p.attach("d1", VM)  # the earlier attempt never reached the fake, so this one performs it
        self.assertEqual([d["name"] for d in self.fake.vm_disks()], ["d1"])

    # -- detach, resize, delete
    def test_detach_removes_only_this_disk_under_the_etag(self):
        self.fake.foreign_attach("keep", 0)
        self.fake.add_disk("d1")
        self.p.attach("d1", VM)
        self.p.detach("d1")
        self.assertEqual([d["name"] for d in self.fake.vm_disks()], ["keep"])
        self.assertIsNone(self.fake.disks["d1"]["managedBy"])
        self.assertEqual(self.fake.disks["keep"]["managedBy"].lower(), f"{SCOPE}/virtualMachines/{VM}".lower())
        self.assertTrue(all(p.headers.get("if-match") for p in self.loop.seen("PATCH")))

    def test_detach_retries_after_a_412(self):
        self.fake.add_disk("d1")
        self.p.attach("d1", VM)
        self.fake.before_patch.append(lambda: self.fake.foreign_attach("sneaky", 5))
        self.p.detach("d1")
        self.assertEqual([d["name"] for d in self.fake.vm_disks()], ["sneaky"])

    def test_detach_of_an_unattached_disk_does_nothing(self):
        self.fake.add_disk("d1")
        self.p.detach("d1")
        self.assertEqual(self.loop.seen("PATCH"), [])

    def test_detach_refuses_a_foreign_holder_and_an_inconsistent_vm(self):
        self.fake.add_disk("d1", managed_by="/subscriptions/x/resourceGroups/other/providers/Microsoft.Compute/virtualMachines/box1")
        with self.assertRaises(ProviderError):
            self.p.detach("d1")
        self.fake.add_disk("d2", managed_by=f"{SCOPE}/virtualMachines/{VM}")  # the VM does not list it
        with self.assertRaises(ProviderError):
            self.p.detach("d2")
        self.assertEqual(self.loop.seen("PATCH"), [])

    def test_resize_grows_in_gib_and_never_shrinks(self):
        self.fake.add_disk("d1", size=50)
        self.p.resize("d1", 80)
        self.assertEqual(self.loop.seen("PATCH", SCOPE + "/disks/d1")[0].body, {"properties": {"diskSizeGB": 80}})
        self.assertEqual(self.p.get_volume("d1").size_gb, 80)
        for size in (80, 10, 40000):
            with self.assertRaises(ProviderError):
                self.p.resize("d1", size)

    def test_resize_that_arm_refuses_is_a_plain_failure(self):
        self.fake.add_disk("d1")
        self.fake.fail[("PATCH", SCOPE + "/disks")] = [409]
        with self.assertRaises(ProviderError) as ctx:
            self.p.resize("d1", 80)
        self.assertFalse(ctx.exception.indeterminate)

    def test_delete_waits_and_missing_is_not_found(self):
        self.fake.add_disk("d1")
        self.p.delete("d1")
        self.assertEqual(self.fake.disks, {})
        with self.assertRaises(NotFound):
            self.p.delete("d1")

    # -- guest
    def test_device_path_is_the_scsi_lun_link_or_the_nvme_link(self):
        self.fake.foreign_attach("x0", 0)
        self.fake.add_disk("d1")
        self.p.attach("d1", VM)
        self.assertEqual(self.p.device_path("d1"), "/dev/disk/azure/scsi1/lun1")
        self.fake.vm["properties"]["storageProfile"]["diskControllerType"] = "NVMe"
        self.assertEqual(self.p.device_path("d1"), "/dev/disk/azure/data/by-lun/1")

    def test_device_path_is_read_fresh_not_remembered(self):
        self.fake.add_disk("d1")
        self.p.attach("d1", VM)
        self.assertEqual(self.p.device_path("d1"), "/dev/disk/azure/scsi1/lun0")
        self.fake.vm_disks()[0]["lun"] = 7  # re-attached elsewhere behind our back
        self.assertEqual(self.p.device_path("d1"), "/dev/disk/azure/scsi1/lun7")

    def test_device_path_refuses_when_imds_and_arm_disagree_or_the_disk_is_absent(self):
        self.fake.add_disk("d1")
        self.p.attach("d1", VM)
        self.fake.imds_disks = [{"lun": 4, "name": "d1"}]
        with self.assertRaises(ProviderError) as ctx:
            self.p.device_path("d1")
        self.assertIn("disagrees", ctx.exception.message)
        self.fake.imds_disks = []  # IMDS lagging behind is tolerated
        self.assertEqual(self.p.device_path("d1"), "/dev/disk/azure/scsi1/lun0")
        with self.assertRaises(ProviderError):
            self.p.device_path("unattached")

    def test_azure_has_no_guest_serial(self):
        self.assertIsNone(self.p.expected_serial("d1"))

    def test_a_redirect_is_never_followed_so_the_token_cannot_leak(self):
        sink = Loopback(lambda req: Reply(200, {}))
        self.addCleanup(sink.close)
        self.fake.add_disk("d1")
        base = self.fake.handle
        self.loop.handler = lambda req: Reply(307, None, {"Location": sink.base + "/steal"}) if req.path.startswith(SCOPE) else base(req)
        with self.assertRaises(ProviderError):
            self.p.get_volume("d1")
        self.assertEqual(sink.requests, [])


if __name__ == "__main__":
    unittest.main()
