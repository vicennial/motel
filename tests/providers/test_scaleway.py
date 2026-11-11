"""The Scaleway provider against a loopback fake of the Block, Instance, metadata and catalogue APIs."""

import unittest

from _scw_azure_http import Clock, Loopback, Reply

from motel.errors import InventoryIncomplete, NotFound, ProviderError
from motel.providers.scaleway import CONFLICT, GB, ScalewayProvider, labels_to_tags, tags_to_labels

KEY = "scw-secret-key-0123456789"  # gitleaks:allow (fake test key)
SERVER = "11111111-1111-4111-8111-111111111111"
OTHER_SERVER = "33333333-3333-4333-8333-333333333333"
PROJECT = "22222222-2222-4222-8222-222222222222"
ZONE = "fr-par-1"
LABELS = {"managed-by": "motel", "role": "floor", "host": SERVER, "floor": "f1", "suite": "0", "created": "20261109t0000z"}


def vid(n):
    return f"aaaaaaaa-0000-4000-8000-{n:012d}"


class FakeScw:
    def __init__(self):
        self.volumes, self.servers, self.fail = {}, [{"id": SERVER}, {"id": OTHER_SERVER}], {}
        self.ticks, self.stuck, self.total_lie, self.next_n = 1, False, None, 1
        self.nanos = 129999
        self.metadata = {"id": SERVER, "project": PROJECT, "zone": ZONE, "name": "box"}

    def add(self, n, tags=(), server=None, size_gb=50, status=None, name=None):
        refs = [{"product_resource_type": "instance_server", "product_resource_id": server, "status": "attached"}] if server else []
        self.volumes[vid(n)] = {"id": vid(n), "name": name or f"motel-f{n}-ab", "type": "sbs_5k", "size": size_gb * GB,
                                "project_id": PROJECT, "created_at": "2026-11-01T10:00:00.5Z", "references": refs,
                                "status": status or ("in_use" if server else "available"), "tags": list(tags), "zone": ZONE,
                                "_ticks": 0}
        return self.volumes[vid(n)]

    @staticmethod
    def public(v):
        return {k: val for k, val in v.items() if not k.startswith("_")}

    def handle(self, req):  # noqa: C901
        p = req.path
        if p == "/conf":
            assert "x-auth-token" not in req.headers, "the secret key must never go to the metadata service"
            return Reply(200, self.metadata)
        if p.startswith("/product-catalog"):
            assert "x-auth-token" not in req.headers, "the catalogue is public: no key"
            items = [{"sku": f"/storage/block/volume-low-latency-5k/{ZONE}", "price": {"retail_price": {"currency_code": "EUR", "units": 0, "nanos": self.nanos}}},
                     {"sku": f"/storage/block/volume-low-latency-15k/{ZONE}", "price": {"retail_price": {"currency_code": "EUR", "units": 0, "nanos": 177000}}}]
            return Reply(200, {"products": items, "total_count": 2})
        if req.headers.get("x-auth-token") != KEY:
            return Reply(403, {"type": "denied_authentication", "message": "bad key"})
        for (m, prefix), codes in self.fail.items():
            if m == req.method and p.startswith(prefix) and codes:
                code = codes.pop(0)
                return Reply(code, {"type": "x", "message": "injected"}, {"Retry-After": "1"} if code == 429 else {})
        block = f"/block/v1/zones/{ZONE}/volumes"
        if req.method == "GET" and p == f"/instance/v1/zones/{ZONE}/servers":
            assert req.query.get("project") == PROJECT
            size, page = int(req.query["per_page"]), int(req.query["page"])
            chunk = self.servers[(page - 1) * size: page * size]
            return Reply(200, {"servers": chunk}, {"X-Total-Count": len(self.servers)})
        if p.startswith(f"/instance/v1/zones/{ZONE}/servers/") and req.method == "POST":
            _, _, _, _, _, _, sid, action = p.split("/")
            v = self.volumes[req.body["volume_id"]]
            if action == "attach-volume":
                assert req.body["volume_type"] == "sbs_volume"
                v["references"] = [{"product_resource_type": "instance_server", "product_resource_id": sid, "status": "attaching"}]
                v["status"], v["_ticks"] = "in_use", self.ticks
            else:
                v["references"][0]["status"] = "detaching"
                v["_ticks"] = self.ticks
            v["_action"] = action
            return Reply(200, {"server": {"id": sid}})
        if req.method == "GET" and p == block:
            assert req.query["project_id"] == PROJECT
            size, page = int(req.query["page_size"]), int(req.query["page"])
            vols = [self.public(v) for v in self.volumes.values()]
            total = self.total_lie if self.total_lie is not None else len(vols)
            return Reply(200, {"volumes": vols[(page - 1) * size: page * size], "total_count": total})
        if req.method == "POST" and p == block:
            n, self.next_n = self.next_n + 100, self.next_n + 1
            body = req.body
            assert body["project_id"] == PROJECT and body["perf_iops"] in (5000, 15000)
            v = self.add(n, body["tags"], size_gb=body["from_empty"]["size"] // GB, name=body["name"], status="creating")
            v["_ticks"] = self.ticks
            return Reply(200, self.public(v))
        if p.startswith(block + "/"):
            v = self.volumes.get(p.rsplit("/", 1)[1])
            if v is None:
                return Reply(404, {"type": "not_found", "message": "resource is not found"})
            if req.method == "GET":
                if v["_ticks"] > 0 and not self.stuck:
                    v["_ticks"] -= 1
                    if v["_ticks"] == 0:
                        if v["status"] == "creating":
                            v["status"] = "available"
                        elif v["status"] == "resizing":
                            v["status"] = "available"
                        elif v.get("_action") == "attach-volume":
                            v["references"][0]["status"] = "attached"
                        elif v.get("_action") == "detach-volume":
                            v["references"], v["status"] = [], "available"
                return Reply(200, self.public(v))
            if req.method == "PATCH":
                v["size"], v["status"], v["_ticks"] = req.body["size"], "resizing", self.ticks
                return Reply(200, self.public(v))
            if req.method == "DELETE":
                assert v["status"] != "in_use"
                del self.volumes[v["id"]]
                return Reply(204)
        return Reply(404, {"type": "not_found", "message": f"no route {req.method} {p}"})


class ScalewayTests(unittest.TestCase):
    def setUp(self):
        self.fake = FakeScw()
        self.loop = Loopback(self.fake.handle)
        self.addCleanup(self.loop.close)
        self.clock = Clock()
        self.p = self.make()

    def make(self, **kw):
        return ScalewayProvider(KEY, base=self.loop.base, metadata_base=self.loop.base, sleep=self.clock.sleep,
                                monotonic=self.clock.monotonic, **kw)

    # -- labels <-> tags
    def test_tags_round_trip_and_ignore_foreign_tags(self):
        tags = labels_to_tags(LABELS)
        self.assertIn("managed-by=motel", tags)
        self.assertEqual(tags_to_labels(tags + ["team-blue", "note=a=b"]), {**LABELS, "note": "a=b"})

    def test_duplicate_key_with_two_values_is_a_conflict_that_matches_nothing(self):
        labels = tags_to_labels(["host=a", "host=b", "role=floor", "role=floor"])
        self.assertEqual(labels["host"], CONFLICT)
        self.assertEqual(labels["role"], "floor")
        self.fake.add(1, ["managed-by=motel", f"host={SERVER}", "host=intruder"])
        self.assertEqual(self.p.list_owned({"managed-by": "motel", "host": SERVER}), [])

    def test_labels_that_cannot_be_tags_are_refused(self):
        for bad in ({"bad key": "x"}, {"k=v": "x"}, {"k": ""}, {"k": "line\nbreak"}, {"k": "x" * 200}):
            with self.assertRaises(ProviderError):
                labels_to_tags(bad)

    # -- identity
    def test_whoami_reads_server_and_zone_from_metadata(self):
        who = self.p.whoami()
        self.assertEqual((who.server_id, who.location, who.provider), (SERVER, ZONE, "scaleway"))

    def test_whoami_refuses_odd_metadata(self):
        for patch in ({"id": "7"}, {"zone": "mars"}, {"project": ""}):
            self.fake.metadata = {**self.fake.metadata, **patch}
            with self.assertRaises(ProviderError):
                self.make().whoami()

    def test_zone_falls_back_to_location_zone_id(self):
        self.fake.metadata = {"id": SERVER, "project": PROJECT, "location": {"zone_id": ZONE}}
        self.assertEqual(self.make().whoami().location, ZONE)

    def test_a_configured_zone_must_match_the_host(self):
        with self.assertRaises(ProviderError):
            self.make(zone="nl-ams-1").whoami()

    def test_unreachable_metadata_is_a_provider_error(self):
        self.loop.close()
        with self.assertRaises(ProviderError):
            self.make().whoami()

    # -- reads
    def test_visible_pages_through_volumes_and_servers(self):
        for n in range(1, 121):
            self.fake.add(n, labels_to_tags({"managed-by": "motel"}) if n % 2 else [])
        self.fake.servers = [{"id": f"{n:08d}-0000-4000-8000-000000000000"} for n in range(1, 131)]
        inv = self.p.visible()
        self.assertEqual((len(inv.volumes), len(inv.servers), inv.complete), (120, 130, True))
        self.assertGreater(len(self.loop.seen("GET", f"/block/v1/zones/{ZONE}/volumes")), 2)

    def test_visible_skips_deleted_volumes_and_reports_attachment(self):
        self.fake.add(1, server=SERVER)
        self.fake.add(2, status="deleted")
        inv = self.p.visible()
        self.assertEqual([(v.id, v.server_id, v.size_gb, v.location) for v in inv.volumes], [(vid(1), SERVER, 50, ZONE)])
        self.assertEqual(inv.volumes[0].created.year, 2026)

    def test_visible_raises_when_the_listing_ends_early(self):
        self.fake.add(1)
        self.fake.total_lie = 5
        with self.assertRaises(InventoryIncomplete):
            self.p.visible()

    def test_visible_raises_when_a_page_fails(self):
        self.fake.add(1)
        self.fake.fail[("GET", "/block/")] = [403] * 5
        with self.assertRaises(InventoryIncomplete):
            self.p.visible()

    def test_list_owned_needs_every_label(self):
        self.fake.add(1, labels_to_tags(LABELS))
        self.fake.add(2, labels_to_tags({**LABELS, "host": OTHER_SERVER}))
        self.fake.add(3)
        self.assertEqual([v.id for v in self.p.list_owned({"managed-by": "motel", "host": SERVER})], [vid(1)])
        self.assertEqual(len(self.p.list_owned({"managed-by": "motel"})), 2)

    def test_get_volume_maps_fields_and_missing_is_not_found(self):
        self.fake.add(1, labels_to_tags(LABELS), server=SERVER, size_gb=75)
        vol = self.p.get_volume(vid(1))
        self.assertEqual((vol.size_gb, vol.server_id, vol.labels["floor"], vol.delete_protected), (75, SERVER, "f1", False))
        with self.assertRaises(NotFound):
            self.p.get_volume(vid(9))
        self.fake.add(4, status="deleted")
        with self.assertRaises(NotFound):
            self.p.get_volume(vid(4))
        with self.assertRaises(ProviderError):
            self.p.get_volume("../../etc")

    def test_foreign_holders_never_look_like_this_server(self):
        v = self.fake.add(1, server=OTHER_SERVER)
        self.assertEqual(self.p.get_volume(vid(1)).server_id, OTHER_SERVER)
        v["references"] = [{"product_resource_type": "k8s_node", "product_resource_id": "x", "status": "attached"}]
        self.assertEqual(self.p.get_volume(vid(1)).server_id, "k8s_node:x")
        v["references"][0]["status"] = "detached"
        self.assertIsNone(self.p.get_volume(vid(1)).server_id)

    # -- price
    def test_price_comes_from_the_catalogue_per_730_hours(self):
        self.assertEqual(str(self.p.price_gb_month(ZONE)), "0.094899")
        self.assertEqual(str(self.make(perf_iops=15000).price_gb_month(ZONE)), "0.129210")
        self.assertEqual(self.loop.seen("GET", "/product-catalog")[0].query["zone"], ZONE)

    def test_price_failure_names_the_override(self):
        self.fake.nanos = 0
        with self.assertRaises(ProviderError) as ctx:
            self.p.price_gb_month(ZONE)
        self.assertIn("price_gb_month_eur", ctx.exception.hint)
        with self.assertRaises(ProviderError):
            self.p.price_gb_month("nl-ams-1")

    # -- writes
    def test_create_attaches_in_the_hosts_zone_and_project(self):
        vol = self.p.create_volume("motel-f1-ab", 50, ZONE, LABELS, server_id=SERVER)
        post = self.loop.seen("POST", f"/block/v1/zones/{ZONE}/volumes")[0]
        self.assertEqual(post.body["from_empty"], {"size": 50 * GB})
        self.assertEqual(post.body["name"], "motel-f1-ab")
        self.assertEqual(tags_to_labels(post.body["tags"]), LABELS)
        self.assertEqual((vol.server_id, vol.size_gb, vol.labels["floor"]), (SERVER, 50, "f1"))
        attach = self.loop.seen("POST", f"/instance/v1/zones/{ZONE}/servers/{SERVER}/attach-volume")
        self.assertEqual(attach[0].body["volume_type"], "sbs_volume")
        self.assertTrue(all(r.headers.get("x-auth-token") == KEY for r in self.loop.requests if r.path.startswith(("/block", "/instance"))))

    def test_create_without_a_server_leaves_it_unattached(self):
        vol = self.p.create_volume("motel-f2-ab", 10, ZONE, LABELS)
        self.assertIsNone(vol.server_id)
        self.assertEqual(self.loop.seen("POST", "/instance"), [])

    def test_create_refuses_sizes_and_zones_the_api_would_reject(self):
        for size in (4, 15001):
            with self.assertRaises(ProviderError):
                self.p.create_volume("motel-f1-ab", size, ZONE, LABELS)
        with self.assertRaises(ProviderError):
            self.p.create_volume("motel-f1-ab", 50, "nl-ams-1", LABELS)
        with self.assertRaises(ProviderError):
            self.p.create_volume("motel-f1-ab", 50, ZONE, LABELS, server_id="not-a-uuid")
        self.assertEqual(self.loop.seen("POST", "/block"), [])

    def test_a_create_that_hits_a_server_error_is_indeterminate_and_not_retried(self):
        self.fake.fail[("POST", "/block/")] = [503]
        with self.assertRaises(ProviderError) as ctx:
            self.p.create_volume("motel-f1-ab", 50, ZONE, LABELS)
        self.assertTrue(ctx.exception.indeterminate)
        self.assertEqual(len(self.loop.seen("POST", "/block")), 1)

    def test_a_lost_create_response_is_indeterminate(self):
        calls = []

        def drop(req):
            calls.append(req)
            return Reply(drop=True) if req.method == "POST" else self.fake.handle(req)

        self.loop.handler = drop
        with self.assertRaises(ProviderError) as ctx:
            self.p.create_volume("motel-f1-ab", 50, ZONE, LABELS)
        self.assertTrue(ctx.exception.indeterminate)
        self.assertEqual(len([c for c in calls if c.method == "POST"]), 1)

    def test_a_429_is_retried_for_writes_after_the_hinted_pause(self):
        self.fake.fail[("POST", "/block/")] = [429]
        vol = self.p.create_volume("motel-f1-ab", 50, ZONE, LABELS)
        self.assertEqual(vol.size_gb, 50)
        self.assertIn(1.0, self.clock.slept)

    def test_reads_retry_on_server_errors(self):
        self.fake.add(1)
        self.fake.fail[("GET", "/block/")] = [502, 503]
        self.assertEqual(self.p.get_volume(vid(1)).id, vid(1))

    def test_a_failed_attach_after_create_reports_the_volume_as_indeterminate(self):
        self.fake.fail[("POST", "/instance/")] = [500]
        with self.assertRaises(ProviderError) as ctx:
            self.p.create_volume("motel-f1-ab", 50, ZONE, LABELS, server_id=SERVER)
        self.assertTrue(ctx.exception.indeterminate)
        self.assertEqual(len(self.fake.volumes), 1)

    def test_attach_waits_for_the_reference_to_be_attached(self):
        self.fake.ticks = 3
        self.fake.add(1)
        self.p.attach(vid(1), SERVER)
        self.assertEqual(self.fake.volumes[vid(1)]["references"][0]["status"], "attached")
        self.assertGreaterEqual(len(self.loop.seen("GET", f"/block/v1/zones/{ZONE}/volumes/{vid(1)}")), 3)

    def test_attach_is_idempotent_and_refuses_another_holder(self):
        self.fake.add(1, server=SERVER)
        self.fake.add(2, server=OTHER_SERVER)
        self.p.attach(vid(1), SERVER)
        self.assertEqual(self.loop.seen("POST", "/instance"), [])
        with self.assertRaises(ProviderError):
            self.p.attach(vid(2), SERVER)

    def test_an_attach_that_never_finishes_is_indeterminate(self):
        self.fake.add(1)
        self.fake.stuck = True
        with self.assertRaises(ProviderError) as ctx:
            self.p.attach(vid(1), SERVER)
        self.assertTrue(ctx.exception.indeterminate)

    def test_an_attach_that_goes_to_error_is_a_plain_failure(self):
        v = self.fake.add(1)
        self.fake.stuck = True
        self.loop.handler = lambda req: Reply(200, {**self.fake.public(v), "status": "error"}) if req.method == "GET" and "/block/" in req.path else self.fake.handle(req)
        with self.assertRaises(ProviderError) as ctx:
            self.p.attach(vid(1), SERVER)
        self.assertFalse(ctx.exception.indeterminate)

    def test_detach_uses_the_holder_and_waits(self):
        self.fake.ticks = 2
        self.fake.add(1, server=SERVER)
        self.p.detach(vid(1))
        self.assertEqual(self.fake.volumes[vid(1)]["status"], "available")
        self.assertEqual(self.loop.seen("POST", "/instance")[0].path, f"/instance/v1/zones/{ZONE}/servers/{SERVER}/detach-volume")

    def test_detach_of_an_unattached_volume_does_nothing(self):
        self.fake.add(1)
        self.p.detach(vid(1))
        self.assertEqual(self.loop.seen("POST"), [])

    def test_detach_refuses_a_holder_that_is_not_an_instance(self):
        self.fake.add(1)["references"] = [{"product_resource_type": "k8s_node", "product_resource_id": "x", "status": "attached"}]
        with self.assertRaises(ProviderError):
            self.p.detach(vid(1))

    def test_resize_grows_in_bytes_and_never_shrinks(self):
        self.fake.add(1, server=SERVER, size_gb=50)
        self.p.resize(vid(1), 80)
        self.assertEqual(self.loop.seen("PATCH")[0].body, {"size": 80 * GB})
        self.assertEqual(self.p.get_volume(vid(1)).size_gb, 80)
        for size in (80, 20):
            with self.assertRaises(ProviderError):
                self.p.resize(vid(1), size)
        with self.assertRaises(ProviderError):
            self.p.resize(vid(1), 15001)
        self.assertEqual(len(self.loop.seen("PATCH")), 1)

    def test_delete_and_missing_delete(self):
        self.fake.add(1)
        self.p.delete(vid(1))
        self.assertEqual(self.fake.volumes, {})
        with self.assertRaises(NotFound):
            self.p.delete(vid(1))

    def test_a_bad_key_is_explained(self):
        bad = ScalewayProvider("wrong-key-0123456789", base=self.loop.base, metadata_base=self.loop.base, sleep=self.clock.sleep)
        with self.assertRaises(InventoryIncomplete) as ctx:
            bad.visible()
        self.assertIn("permission sets", ctx.exception.message)
        with self.assertRaises(ProviderError) as ctx:
            bad.delete(vid(1))
        self.assertFalse(ctx.exception.indeterminate)
        self.assertNotIn("wrong-key", ctx.exception.message)

    def test_a_redirect_is_never_followed_so_the_key_cannot_leak(self):
        sink = Loopback(lambda req: Reply(200, {}))
        self.addCleanup(sink.close)
        self.loop.handler = lambda req: Reply(302, None, {"Location": sink.base + "/steal"})
        with self.assertRaises(ProviderError):
            self.p.delete(vid(1))
        self.assertEqual(sink.requests, [])

    # -- guest
    def test_device_path_and_serial_follow_the_documented_naming(self):
        self.assertEqual(self.p.device_path(vid(1)), f"/dev/disk/by-id/scsi-0SCW_sbs_volume-{vid(1)}")
        self.assertEqual(self.p.expected_serial(vid(1)), f"volume-{vid(1)}")
        with self.assertRaises(ProviderError):
            self.p.device_path("../x")


if __name__ == "__main__":
    unittest.main()
