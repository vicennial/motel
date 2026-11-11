"""The DigitalOcean provider against a small fake API on a loopback socket (no network, no token)."""

import json
import unittest
import uuid
from decimal import Decimal
from unittest import mock

from contract import ProviderContract
from fakehttp import FakeApi

from motel.errors import ProviderError
from motel.providers import digitalocean
from motel.providers._common import encode_tags
from motel.providers.digitalocean import DigitalOceanProvider

HOST = "400100001"
REGION = "nyc3"


class FakeDigitalOcean(FakeApi):
    def __init__(self):
        super().__init__()
        self.volumes, self.n = {}, 0
        self.page_size, self.truncate_total, self.wedged = 50, None, False
        self.actions = {}

    def wedge(self):
        self.wedged = True

    def add_volume(self, name, size, labels, attached=None):
        self.n += 1
        vid = str(uuid.UUID(int=self.n))
        self.volumes[vid] = {"id": vid, "name": name, "size_gigabytes": size, "droplet_ids": [int(attached)] if attached else [],
                             "tags": encode_tags(labels, 255), "region": {"slug": REGION, "name": "New York 3"},
                             "created_at": "2026-11-09T12:00:00Z"}
        return vid

    def route(self, method, path, query, body):
        parts = path.strip("/").split("/")
        if method == "GET" and path == "/droplets":
            return 200, {"droplets": [{"id": int(HOST)}, {"id": 5}], "links": {}, "meta": {"total": 2}}
        if path == "/volumes":
            if method == "POST":
                import re
                if not re.fullmatch(r"[a-z][a-z0-9-]{0,63}", body["name"]) or "filesystem_type" in body:
                    return 422, {"id": "unprocessable_entity", "message": "bad name"}
                vid = self.add_volume(body["name"], body["size_gigabytes"], {}, None)
                self.volumes[vid]["tags"] = body["tags"]
                self.volumes[vid]["region"]["slug"] = body["region"]
                return 201, {"volume": self.volumes[vid]}
            vols = list(self.volumes.values())
            page, size = int(query.get("page", 1)), min(self.page_size, int(query.get("per_page", 20)))
            chunk = vols[(page - 1) * size: page * size]
            more = page * size < len(vols)
            total = self.truncate_total if self.truncate_total is not None else len(vols)
            return 200, {"volumes": chunk, "links": {"pages": {"next": f"x?page={page + 1}"} if more else {}}, "meta": {"total": total}}
        vid = parts[1] if len(parts) > 1 else None
        if vid not in self.volumes:
            return 404, {"id": "not_found", "message": "nope"}
        vol = self.volumes[vid]
        if len(parts) == 2 and method == "GET":
            return 200, {"volume": vol}
        if len(parts) == 2 and method == "DELETE":
            if vol["droplet_ids"]:
                return 422, {"id": "unprocessable_entity", "message": "attached"}
            del self.volumes[vid]
            return 204, None
        if len(parts) == 3 and parts[2] == "actions" and method == "POST":
            kind = body["type"]
            if kind == "attach":
                vol["droplet_ids"] = [body["droplet_id"]]
            elif kind == "detach":
                vol["droplet_ids"] = []
            elif kind == "resize":
                if body["size_gigabytes"] <= vol["size_gigabytes"]:
                    return 422, {"id": "unprocessable_entity", "message": "only upwards"}
                vol["size_gigabytes"] = body["size_gigabytes"]
            aid = len(self.actions) + 1
            self.actions[aid] = {"id": aid, "type": f"{kind}_volume", "status": "in-progress"}
            return 202, {"action": self.actions[aid]}
        if len(parts) == 4 and parts[2] == "actions" and method == "GET":
            action = self.actions[int(parts[3])]
            if not self.wedged:
                action["status"] = "completed"
            return 200, {"action": action}
        return 404, {"id": "not_found", "message": path}


class DigitalOceanTest(ProviderContract, unittest.TestCase):
    HOST, REGION = HOST, REGION
    BAD_IDS = ("../etc", "1 2", "", "12a", "g" * 36)
    GOOD_VOLUME_ID = str(uuid.UUID(int=999))
    LIST_PATH, CREATE_PATH, ATTACH_FAIL_PREFIX = "/volumes", "/volumes", "/volumes/"
    ODD_METADATA = {"id": "12a", "region": REGION}
    BROKEN_METADATA = {}
    PRICE = Decimal("0.10")

    def new_api(self):
        return FakeDigitalOcean()

    def new_provider(self, base, clock, metadata=None, serial_check=True, eur_per_usd=Decimal("1")):
        meta = {"id": HOST, "region": REGION} if metadata is None else metadata
        return DigitalOceanProvider("test-token-not-secret-0123456789", base=base, sleep=clock.sleep, monotonic=clock.monotonic,
                                    metadata_fetch=meta.__getitem__, serial_check=serial_check, eur_per_usd=eur_per_usd)

    def check_device(self, vol, path, serial):
        self.assertEqual(path, f"/dev/disk/by-id/scsi-0DO_Volume_{vol.name}")
        self.assertEqual(serial, vol.name)

    # -- specifics
    def test_create_sends_a_blank_volume_with_encoded_tags_then_attaches_by_droplet_id(self):
        vol = self.make()
        create = self.api.posts("/volumes")[0][3]
        self.assertEqual(create["name"], self.NAME)
        self.assertEqual(create["region"], REGION)
        self.assertEqual(create["size_gigabytes"], 50)
        self.assertNotIn("filesystem_type", create)  # DigitalOcean would auto-mount a pre-formatted volume
        self.assertIn("m_managed-by_motel", create["tags"])
        self.assertIn(f"m_host_{HOST}", create["tags"])
        self.assertIn("m_created_20261109t1200z", create["tags"])
        self.assertTrue(all(len(t) <= 255 for t in create["tags"]))
        attach = self.api.posts(f"/volumes/{vol.id}/actions")[0][3]
        self.assertEqual(attach, {"type": "attach", "droplet_id": int(HOST)})

    def test_detach_uses_the_droplet_the_volume_is_on_and_resize_names_the_region(self):
        vol = self.make()
        self.p.resize(vol.id, 70)
        self.p.detach(vol.id)
        actions = [r[3] for r in self.api.posts(f"/volumes/{vol.id}/actions")]
        self.assertEqual(actions[1], {"type": "resize", "size_gigabytes": 70, "region": REGION})
        self.assertEqual(actions[2], {"type": "detach", "droplet_id": int(HOST)})

    def test_action_polling_uses_the_volume_action_endpoint(self):
        vol = self.make()
        polls = [r for r in self.api.requests if r[0] == "GET" and "/actions/" in r[1]]
        self.assertTrue(polls and all(r[1].startswith(f"/volumes/{vol.id}/actions/") for r in polls))

    def test_names_must_follow_the_digitalocean_rules(self):
        for bad in ("1motel", "Motel-x", "motel_x", "m" * 65, "motel x"):
            with self.assertRaises(ProviderError):
                self.p.create_volume(bad, 50, REGION, self.labels)

    def test_look_alike_tags_never_count_as_motel_labels(self):
        real = self.make()
        vid = self.api.add_volume("motel-f-ab12-fake", 50, {}, None)
        fakes = ["managed-by:motel", "M_managed-by_motel", "m_managed-by_motel_", "m_managed-by_mote_6c", "m_managed-by_mo_74el",
                 "m_role_floor", "m_host_" + HOST]
        self.api.volumes[vid]["tags"] = fakes  # none of the managed-by spellings is the canonical one
        self.assertEqual([v.id for v in self.p.list_owned({"managed-by": "motel"})], [real.id])
        self.assertEqual(self.p.get_volume(vid).labels, {"role": "floor", "host": HOST})
        # The canonical tag plus a second, different value for the same key: the key is dropped and matches nothing.
        self.api.volumes[vid]["tags"] = ["m_managed-by_motel", "m_managed-by_motel_00"]
        self.assertEqual(self.p.get_volume(vid).labels, {})
        self.assertEqual([v.id for v in self.p.list_owned({"managed-by": "motel"})], [real.id])
        self.api.volumes[vid]["tags"] = ["m_host_1", "m_host_2", "m_floor_f-1"]
        self.assertEqual(self.p.get_volume(vid).labels, {"floor": "f-1"})

    def test_several_attached_droplets_never_equal_this_host(self):
        vid = self.api.add_volume("motel-f-ab12-multi", 50, {}, None)
        self.api.volumes[vid]["droplet_ids"] = [int(HOST), 7]
        self.assertNotEqual(self.p.get_volume(vid).server_id, HOST)
        with self.assertRaises(ProviderError):
            self.p.detach(vid)

    def test_the_default_metadata_reader_uses_the_documented_paths(self):
        meta = FakeApi()
        meta.route = lambda method, path, query, body: (200, {"/id": HOST, "/region": REGION}[path])
        from fakehttp import serve
        url = serve(self, meta)
        with mock.patch.object(digitalocean, "METADATA_BASE", url):
            p = DigitalOceanProvider("test-token-not-secret-0123456789", base=self.base)
            self.assertEqual((p.whoami().server_id, p.whoami().location), (HOST, REGION))

    def test_prices_are_usd_converted_with_the_configured_rate(self):
        p = self.new_provider(self.base, self.clock, eur_per_usd=Decimal("0.92"))
        self.assertEqual(p.price_gb_month("fra1"), Decimal("0.0920"))
        self.assertEqual(p.currency, "USD")

    def test_error_text_carries_the_api_message_but_never_the_token(self):
        self.api.fail[("POST", "/volumes")] = [422]
        with self.assertRaises(ProviderError) as raised:
            self.make(attached=False)
        self.assertIn("injected", raised.exception.message)
        self.assertNotIn("test-token", raised.exception.message)
        json.dumps(raised.exception.as_json())


if __name__ == "__main__":
    unittest.main()
