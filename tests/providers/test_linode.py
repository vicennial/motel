"""The Linode (Akamai Cloud) provider against a small fake API on a loopback socket (no network, no token)."""

import json
import re
import unittest
from decimal import Decimal
from unittest import mock

from contract import ProviderContract
from fakehttp import FakeApi, serve

from motel.errors import ProviderError
from motel.providers import linode
from motel.providers._common import encode_tags
from motel.providers.linode import LinodeProvider

HOST = "30100001"
REGION = "us-iad"


class FakeLinode(FakeApi):
    def __init__(self):
        super().__init__()
        self.volumes, self.n, self.pending = {}, 1000, {}
        self.page_size, self.truncate_total, self.wedged, self.force_region = 50, None, False, None
        self.types = {"data": [{"id": "volume", "price": {"hourly": 0.00015, "monthly": 0.1},
                                "region_prices": [{"id": "id-cgk", "hourly": 0.00018, "monthly": 0.12}]}]}

    def wedge(self):
        self.wedged = True

    def add_volume(self, name, size, labels, attached=None):
        self.n += 1
        self.volumes[self.n] = {"id": self.n, "label": name, "size": size, "status": "active", "region": REGION,
                                "linode_id": int(attached) if attached else None, "io_ready": bool(attached),
                                "tags": encode_tags(labels, 50), "created": "2026-11-09T12:00:00",
                                "filesystem_path": f"/dev/disk/by-id/scsi-0Linode_Volume_{name}"}
        return str(self.n)

    def _view(self, vol):
        """A GET settles a pending change after one poll, unless the API is wedged."""
        left = self.pending.get(vol["id"])
        if left is not None and not self.wedged:
            if left <= 0:
                self.pending.pop(vol["id"])
                vol["status"], vol["io_ready"] = "active", vol["linode_id"] is not None
            else:
                self.pending[vol["id"]] = left - 1
        return vol

    def route(self, method, path, query, body):
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/linode/instances":
            return 200, {"data": [{"id": int(HOST)}, {"id": 5}], "page": 1, "pages": 1, "results": 2}
        if method == "GET" and path == "/volumes/types":
            return 200, self.types
        if path == "/volumes":
            if method == "POST":
                errors = []
                if not re.fullmatch(r"[a-zA-Z]((?!--|__)[a-zA-Z0-9_-])+", body.get("label", "")) or len(body["label"]) > 32:
                    errors.append({"field": "label", "reason": "bad label"})
                if body.get("size", 0) < 10:
                    errors.append({"field": "size", "reason": "too small"})
                if errors:
                    return 400, {"errors": errors}
                vid = int(self.add_volume(body["label"], body["size"], {}, body.get("linode_id")))
                vol = self.volumes[vid]
                vol.update(tags=body["tags"], status="creating", io_ready=False, region=self.force_region or (REGION if body.get("linode_id") else body["region"]))
                self.pending[vid] = 1
                return 200, vol
            vols = list(self.volumes.values())
            page, size = int(query.get("page", 1)), min(self.page_size, int(query.get("page_size", 100)))
            pages = max(1, -(-len(vols) // size))
            total = self.truncate_total if self.truncate_total is not None else len(vols)
            return 200, {"data": vols[(page - 1) * size: page * size], "page": page, "pages": pages, "results": total}
        vid = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else None
        if vid not in self.volumes:
            return 404, {"errors": [{"reason": "Not found"}]}
        vol = self.volumes[vid]
        if len(parts) == 2 and method == "GET":
            return 200, self._view(vol)
        if len(parts) == 2 and method == "DELETE":
            if vol["linode_id"] is not None:
                return 400, {"errors": [{"reason": "still attached"}]}
            del self.volumes[vid]
            return 200, {}
        if len(parts) == 3 and method == "POST":
            if parts[2] == "attach":
                vol["linode_id"], vol["io_ready"] = body["linode_id"], False
                self.pending[vid] = 1
                return 200, vol
            if parts[2] == "detach":
                vol["linode_id"], vol["io_ready"] = None, False
                return 200, {}
            if parts[2] == "resize":
                if body["size"] <= vol["size"]:
                    return 400, {"errors": [{"field": "size", "reason": "cannot shrink"}]}
                vol["size"], vol["status"] = body["size"], "resizing"
                self.pending[vid] = 1
                return 200, vol
        return 404, {"errors": [{"reason": path}]}


class LinodeTest(ProviderContract, unittest.TestCase):
    HOST, REGION = HOST, REGION
    NAME = "motel-f-ab12-cd34"
    BAD_IDS = ("../etc", "1 2", "", "12a", "g" * 36)
    GOOD_VOLUME_ID = "999"
    LIST_PATH, CREATE_PATH, ATTACH_FAIL_PREFIX = "/volumes", "/volumes", None
    ODD_METADATA = {"instance": json.dumps({"id": "12a", "region": REGION})}
    BROKEN_METADATA = {}
    PRICE = Decimal("0.10")

    def new_api(self):
        return FakeLinode()

    def new_provider(self, base, clock, metadata=None, serial_check=True, eur_per_usd=Decimal("1")):
        meta = {"instance": json.dumps({"id": int(HOST), "region": REGION, "label": "box"})} if metadata is None else metadata
        return LinodeProvider("test-token-not-secret-0123456789", base=base, sleep=clock.sleep, monotonic=clock.monotonic,
                              metadata_fetch=meta.__getitem__, serial_check=serial_check, eur_per_usd=eur_per_usd)

    def check_device(self, vol, path, serial):
        self.assertEqual(path, f"/dev/disk/by-id/scsi-0Linode_Volume_{vol.name}")
        self.assertEqual(serial, vol.name)

    # -- specifics
    def test_create_attaches_in_the_same_call_and_names_no_region(self):
        self.make()
        body = self.api.posts("/volumes")[0][3]
        self.assertEqual(body["linode_id"], int(HOST))
        self.assertNotIn("region", body)
        self.assertNotIn("encryption", body)  # the account default applies
        self.assertEqual(body["label"], self.NAME)
        self.assertIn("m_managed-by_motel", body["tags"])
        self.assertTrue(all(3 <= len(t) <= 50 for t in body["tags"]))
        self.assertEqual(len(self.api.posts("/volumes")), 1)  # no separate attach call

    def test_create_unattached_names_the_region(self):
        self.make(attached=False)
        body = self.api.posts("/volumes")[0][3]
        self.assertEqual((body["region"], "linode_id" in body), (REGION, False))

    def test_a_volume_created_in_another_region_is_reported(self):
        self.api.force_region = "eu-west"
        with self.assertRaises(ProviderError) as raised:
            self.make(attached=False)
        self.assertTrue(raised.exception.indeterminate)

    def test_waits_until_the_volume_is_active_and_io_ready(self):
        self.make()
        polls = [r for r in self.api.requests if r[0] == "GET" and re.fullmatch(r"/volumes/\d+", r[1])]
        self.assertGreaterEqual(len(polls), 2)
        self.assertTrue(self.clock.sleeps)

    def test_labels_follow_the_linode_rules(self):
        for bad in ("1motel", "motel--x", "motel__x", "m" + "x" * 32, "motel x", "m"):
            with self.assertRaises(ProviderError):
                self.p.create_volume(bad, 50, REGION, self.labels)

    def test_tags_stay_within_fifty_characters_or_are_refused(self):
        self.p.create_volume(self.NAME, 50, REGION, {"managed-by": "motel", "host": "x" * 40})  # m_host_ + 40 = 47
        with self.assertRaises(ProviderError):
            self.p.create_volume("motel-f-ab12-zz99", 50, REGION, {"host": "x" * 50})

    def test_the_minimum_size_is_left_to_the_api(self):
        with self.assertRaises(ProviderError) as raised:
            self.p.create_volume(self.NAME, 5, REGION, self.labels)
        self.assertFalse(raised.exception.indeterminate)
        self.assertIn("size", raised.exception.message)

    def test_look_alike_tags_never_count_as_motel_labels(self):
        real = self.make()
        vid = self.api.add_volume("motel-f-ab12-fake", 50, {}, None)
        self.api.volumes[int(vid)]["tags"] = ["managed-by:motel", "M_managed-by_motel", "m_managed-by_mo_74el", "m_managed-by_", "motel"]
        self.assertEqual([v.id for v in self.p.list_owned({"managed-by": "motel"})], [real.id])
        self.assertEqual(self.p.get_volume(vid).labels, {"managed-by": ""})  # only the empty value is canonical here

    def test_created_is_read_as_utc_and_locks_mean_delete_protection(self):
        vol = self.make()
        self.assertEqual(vol.created.utcoffset().total_seconds(), 0)
        doc = self.api.volumes[int(vol.id)]
        doc["locks"] = [{"lock_type": "cannot_delete"}]
        self.assertTrue(self.p.get_volume(vol.id).delete_protected)
        doc["locks"] = []
        self.assertFalse(self.p.get_volume(vol.id).delete_protected)

    def test_the_guest_path_comes_from_the_api_when_it_is_sane(self):
        vol = self.make()
        doc = self.api.volumes[int(vol.id)]
        doc["filesystem_path"] = "/dev/disk/by-id/scsi-0Linode_Volume_other"
        self.assertEqual(self.p.device_path(vol.id), "/dev/disk/by-id/scsi-0Linode_Volume_other")
        self.assertEqual(self.p.expected_serial(vol.id), "other")
        for odd in ("/dev/sda", "/dev/disk/by-id/scsi-0Linode_Volume_../x", "", None):
            doc["filesystem_path"] = odd
            self.assertEqual(self.p.device_path(vol.id), f"/dev/disk/by-id/scsi-0Linode_Volume_{vol.name}")

    def test_the_price_comes_from_the_volume_types_endpoint_with_a_table_fallback(self):
        self.assertEqual(self.p.price_gb_month("id-cgk"), Decimal("0.1200"))
        self.assertEqual(self.p.price_gb_month(REGION), Decimal("0.1000"))
        self.api.types = {"data": [{"id": "volume", "price": {"monthly": 0.2}}]}
        self.assertEqual(self.p.price_gb_month(REGION), Decimal("0.2000"))
        for odd in ({"data": []}, {"data": [{"id": "volume"}]}, {"data": [{"price": {"monthly": "free"}}]}):
            self.api.types = odd
            self.assertEqual(self.p.price_gb_month(REGION), Decimal("0.1000"))
        self.api.fail[("GET", "/volumes/types")] = [500] * 6
        self.assertEqual(self.p.price_gb_month(REGION), Decimal("0.1000"))
        self.assertEqual(self.new_provider(self.base, self.clock, eur_per_usd=Decimal("0.9")).price_gb_month(REGION), Decimal("0.0900"))

    def test_the_default_metadata_reader_does_the_token_handshake(self):
        meta = FakeApi()
        seen = []

        def route(method, path, query, body):
            seen.append((method, path))
            if method == "PUT" and path == "/token":
                return 200, "metadata-token-1"
            auth = meta.requests[-1][5]
            if path == "/instance" and auth.get("Metadata-Token") == "metadata-token-1" and auth.get("Accept") == "application/json":
                return 200, json.dumps({"id": int(HOST), "region": REGION})
            return 403, "no"
        meta.route = route
        url = serve(self, meta)
        with mock.patch.object(linode, "METADATA_BASE", url):
            p = LinodeProvider("test-token-not-secret-0123456789", base=self.base)
            who = p.whoami()
        self.assertEqual((who.server_id, who.location), (HOST, REGION))
        self.assertEqual(seen[0], ("PUT", "/token"))
        self.assertIn("Metadata-Token-Expiry-Seconds", meta.requests[0][5])

    def test_a_listing_that_reports_more_results_than_it_returned_is_incomplete(self):
        from motel.errors import InventoryIncomplete
        self.make(attached=False)
        self.api.truncate_total = 3
        with self.assertRaises(InventoryIncomplete):
            self.p.visible()


if __name__ == "__main__":
    unittest.main()
