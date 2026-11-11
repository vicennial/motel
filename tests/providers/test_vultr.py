"""The Vultr provider against a small fake API on a loopback socket (no network, no token)."""

import json
import unittest
import uuid
from decimal import Decimal
from unittest import mock

from contract import ProviderContract
from fakehttp import FakeApi, serve

from motel.errors import InventoryIncomplete, ProviderError
from motel.providers import vultr
from motel.providers._common import encode_label
from motel.providers.vultr import VultrProvider

HOST = str(uuid.UUID(int=0xA0A0))
REGION = "ewr"


class FakeVultr(FakeApi):
    def __init__(self):
        super().__init__()
        self.blocks, self.n, self.pending = {}, 0, {}
        self.page_size, self.truncate_total, self.wedged = 50, None, False

    def wedge(self):
        self.wedged = True

    def add_volume(self, name, size, labels, attached=None):
        self.n += 1
        bid = str(uuid.UUID(int=self.n))
        self.blocks[bid] = {"id": bid, "date_created": "2026-11-09T12:00:00+00:00", "cost": 1, "status": "active", "size_gb": size,
                            "region": REGION, "attached_to_instance": attached or "", "label": encode_label(name, labels, 255),
                            "mount_id": f"ewr-{self.n:012x}" if attached else "", "block_type": "high_perf"}
        return bid

    def _view(self, block):
        left = self.pending.get(block["id"])
        if left is not None and not self.wedged:
            if left <= 0:
                self.pending.pop(block["id"])
                block["status"] = "active"
            else:
                self.pending[block["id"]] = left - 1
        return block

    def route(self, method, path, query, body):
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/instances":
            return 200, {"instances": [{"id": HOST}, {"id": str(uuid.UUID(int=5))}], "meta": {"total": 2, "links": {"next": "", "prev": ""}}}
        if path == "/blocks":
            if method == "POST":
                if body.get("block_type") not in ("high_perf", "storage_opt") or not body.get("region") or body["size_gb"] < 1:
                    return 400, {"error": "invalid request", "status": 400}
                bid = self.add_volume("x", body["size_gb"], {}, None)
                block = self.blocks[bid]
                block.update(label=body["label"], status="pending", region=body["region"], block_type=body["block_type"])
                self.pending[bid] = 1
                return 202, {"block": block}
            blocks = list(self.blocks.values())
            start = int(query["cursor"][1:]) if query.get("cursor") else 0
            end = start + self.page_size
            total = self.truncate_total if self.truncate_total is not None else len(blocks)
            nxt = f"c{end}" if end < len(blocks) else ""
            return 200, {"blocks": blocks[start:end], "meta": {"total": total, "links": {"next": nxt, "prev": ""}}}
        bid = parts[1] if len(parts) > 1 else None
        if bid not in self.blocks:
            return 404, {"error": "Invalid block storage ID.", "status": 404}
        block = self.blocks[bid]
        if len(parts) == 2 and method == "GET":
            return 200, {"block": self._view(block)}
        if len(parts) == 2 and method == "DELETE":
            if block["attached_to_instance"]:
                return 400, {"error": "attached", "status": 400}
            del self.blocks[bid]
            return 204, None
        if len(parts) == 2 and method == "PATCH":
            if body["size_gb"] <= block["size_gb"]:
                return 400, {"error": "cannot shrink", "status": 400}
            block.update(size_gb=body["size_gb"], status="pending")
            self.pending[bid] = 1
            return 202, None
        if len(parts) == 3 and method == "POST" and parts[2] == "attach":
            block.update(attached_to_instance=body["instance_id"], mount_id="ewr-0123456789abcdef0123456789", status="pending")
            self.pending[bid] = 1
            return 202, None
        if len(parts) == 3 and method == "POST" and parts[2] == "detach":
            block.update(attached_to_instance="", mount_id="", status="pending")
            self.pending[bid] = 1
            return 202, None
        return 404, {"error": path, "status": 404}


class VultrTest(ProviderContract, unittest.TestCase):
    HOST, REGION = HOST, REGION
    BAD_IDS = ("../etc", "1 2", "", "12a", "g" * 36, "123456")
    GOOD_VOLUME_ID = str(uuid.UUID(int=999))
    LIST_PATH, CREATE_PATH, ATTACH_FAIL_PREFIX = "/blocks", "/blocks", "/blocks/"
    ODD_METADATA = {"v1.json": json.dumps({"instance-v2-id": "nope", "region": {"regioncode": "EWR"}})}
    BROKEN_METADATA = {}
    PRICE = Decimal("0.10")

    def new_api(self):
        return FakeVultr()

    def new_provider(self, base, clock, metadata=None, serial_check=True, eur_per_usd=Decimal("1"), block_type="high_perf"):
        meta = ({"v1.json": json.dumps({"instance-v2-id": HOST.upper(), "region": {"regioncode": "EWR"}, "instanceid": "9"})}
                if metadata is None else metadata)
        return VultrProvider("test-token-not-secret-0123456789", base=base, sleep=clock.sleep, monotonic=clock.monotonic,
                             metadata_fetch=meta.__getitem__, serial_check=serial_check, eur_per_usd=eur_per_usd, block_type=block_type)

    def check_device(self, vol, path, serial):
        mount = self.api.blocks[vol.id]["mount_id"]
        self.assertEqual(path, f"/dev/disk/by-id/virtio-{mount[:20]}")
        self.assertEqual(serial, mount[:20])
        self.assertLessEqual(len(serial), 20)

    # -- specifics
    def test_create_puts_the_name_and_every_label_into_the_one_label_then_attaches_live(self):
        vol = self.make()
        body = self.api.posts("/blocks")[0][3]
        self.assertEqual(body["region"], REGION)
        self.assertEqual(body["block_type"], "high_perf")
        self.assertEqual(body["size_gb"], 50)
        self.assertTrue(body["label"].startswith(f"{self.NAME}~managed-by=motel~role=floor~host={HOST}~floor=f-ab12~suite=0~created="))
        self.assertLessEqual(len(body["label"]), 255)
        attach = self.api.posts(f"/blocks/{vol.id}/attach")[0][3]
        self.assertEqual(attach, {"instance_id": HOST, "live": True})

    def test_labels_with_awkward_characters_round_trip_exactly(self):
        awkward = {"managed-by": "motel", "note": "a~b=c%d éü \x00", "empty": "", "host": HOST}
        vol = self.p.create_volume(self.NAME, 50, REGION, awkward, server_id=None)
        self.assertEqual(self.p.get_volume(vol.id).labels, awkward)
        self.assertEqual(self.p.get_volume(vol.id).name, self.NAME)
        label = self.api.blocks[vol.id]["label"]
        self.assertEqual(label.count("~"), len(awkward))  # no raw delimiter inside a value
        self.assertNotIn(" ", label)

    def test_look_alike_labels_never_count_as_motel_labels(self):
        real = self.make()
        bid = self.api.add_volume("motel-f-ab12-fake", 50, {}, None)
        for text in ("motel-f-ab12-fake~managed-by=motel~extra", "motel-f-ab12-fake~managed-by=mote%6c", "motel-f-ab12-fake~Managed-By=motel",
                     "motel-f-ab12-fake~managed-by=motel~managed-by=other", "motel-f-ab12-fake~managed-by=motel%", "motel-f-ab12-fake~managed-by=motel%zz",
                     "motel f ab12~managed-by=motel", "managed-by=motel", "motel-f-ab12-fake~~managed-by=motel"):
            self.api.blocks[bid]["label"] = text
            self.assertEqual(self.p.get_volume(bid).labels, {}, text)
            self.assertEqual([v.id for v in self.p.list_owned({"managed-by": "motel"})], [real.id], text)
        self.api.blocks[bid]["label"] = "motel-f-ab12-fake~managed-by=motel"
        self.assertEqual(self.p.get_volume(bid).labels, {"managed-by": "motel"})
        self.assertEqual(self.p.get_volume(bid).name, "motel-f-ab12-fake")

    def test_a_volume_with_a_plain_label_has_that_label_as_its_name_and_no_labels(self):
        bid = self.api.add_volume("Remote-Block-Storage", 40, {}, None)
        vol = self.p.get_volume(bid)
        self.assertEqual((vol.name, vol.labels), ("Remote-Block-Storage", {}))

    def test_a_label_that_does_not_fit_is_refused_not_truncated(self):
        with self.assertRaises(ProviderError):
            self.p.create_volume(self.NAME, 50, REGION, {"host": "x" * 300})
        self.assertEqual(self.api.posts("/blocks"), [])

    def test_the_device_needs_an_attached_volume_and_is_cut_to_the_virtio_serial_length(self):
        free = self.make(attached=False)
        with self.assertRaises(ProviderError):
            self.p.device_path(free.id)
        self.p.attach(free.id, HOST)
        self.assertEqual(self.p.device_path(free.id), "/dev/disk/by-id/virtio-ewr-0123456789abcdef")
        self.api.blocks[free.id]["mount_id"] = "../x"
        with self.assertRaises(ProviderError):
            self.p.expected_serial(free.id)

    def test_block_type_sets_the_price_and_is_validated(self):
        hdd = self.new_provider(self.base, self.clock, block_type="storage_opt")
        self.assertEqual(hdd.price_gb_month(REGION), Decimal("0.0400"))
        hdd.create_volume(self.NAME, 50, REGION, self.labels)
        self.assertEqual(self.api.posts("/blocks")[0][3]["block_type"], "storage_opt")
        with self.assertRaises(ProviderError):
            self.new_provider(self.base, self.clock, block_type="nvme")

    def test_whoami_lowercases_the_region_and_the_id_and_has_a_fallback_for_the_id(self):
        who = self.p.whoami()
        self.assertEqual((who.server_id, who.location), (HOST, "ewr"))
        meta = {"v1.json": json.dumps({"region": {"regioncode": "EWR"}}), "v1/instance-v2-id": HOST}
        self.assertEqual(self.new_provider(self.base, self.clock, metadata=meta).whoami().server_id, HOST)

    def test_the_default_metadata_reader_uses_the_documented_paths(self):
        meta = FakeApi()
        meta.route = lambda method, path, query, body: (200, json.dumps({"instance-v2-id": HOST, "region": {"regioncode": "EWR"}}))
        url = serve(self, meta)
        with mock.patch.object(vultr, "METADATA_BASE", url):
            who = VultrProvider("test-token-not-secret-0123456789", base=self.base).whoami()
        self.assertEqual((who.server_id, who.location), (HOST, REGION))
        self.assertEqual(meta.requests[0][1], "/v1.json")

    def test_cursor_pagination_is_followed_and_checked_against_the_total(self):
        self.api.page_size = 2
        for i in range(5):
            self.make(f"p{i:03d}", attached=False)
        self.assertEqual(len(self.p.visible().volumes), 5)
        listing = [r[2].get("cursor") for r in self.api.requests if r[:2] == ("GET", "/blocks")]
        self.assertEqual(listing[-3:], [None, "c2", "c4"])
        self.api.truncate_total = 8
        with self.assertRaises(InventoryIncomplete):
            self.p.visible()

    def test_an_odd_cursor_is_an_incomplete_inventory(self):
        self.make(attached=False)
        real = self.api.route
        self.api.route = lambda m, p, q, b: (200, {"blocks": [], "meta": {"total": 5, "links": {"next": "a b&c=d"}}}) if p == "/blocks" and m == "GET" else real(m, p, q, b)
        with self.assertRaises(InventoryIncomplete):
            self.p.list_owned({})

    def test_a_missing_block_id_in_the_create_answer_is_refused(self):
        real = self.api.route
        self.api.route = lambda m, p, q, b: (202, {"block": {}}) if m == "POST" and p == "/blocks" else real(m, p, q, b)
        with self.assertRaises(ProviderError):
            self.make(attached=False)


if __name__ == "__main__":
    unittest.main()
