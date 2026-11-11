"""The Hetzner provider against a small fake API on a loopback socket (no network, no token)."""

import json
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

from motel.errors import InventoryIncomplete, NotFound, ProviderError
from motel.providers.hetzner import HetznerProvider


class FakeHetzner:
    def __init__(self):
        self.volumes, self.requests, self.next_id = {}, [], 100
        self.fail = {}  # (method, path-prefix) -> list of status codes to return first
        self.page_size = 50
        self.truncate_total = None
        self.create_status = "success"  # status of the create action in the POST response
        self.action_poll = None  # what GET /actions/N returns instead of success

    def handler(outer):  # noqa: N805
        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _send(self, code, doc):
                body = json.dumps(doc).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _handle(self, method):
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else None
                url = urlparse(self.path)
                outer.requests.append((method, url.path, url.query, body, self.headers.get("Authorization")))
                for (m, prefix), codes in outer.fail.items():
                    if m == method and url.path.startswith(prefix) and codes:
                        return self._send(codes.pop(0), {"error": {"code": "x", "message": "injected"}})
                p = url.path
                if method == "GET" and p == "/servers":
                    return self._send(200, {"servers": [{"id": 1000001}, {"id": 5}], "meta": {"pagination": {"next_page": None}}})
                if method == "GET" and p == "/pricing":
                    return self._send(200, {"pricing": {"volume": {"price_per_gb_month": {"net": "0.0440", "gross": "0.0524"}}}})
                if method == "GET" and p == "/volumes":
                    q = parse_qs(url.query)
                    vols = list(outer.volumes.values())
                    for sel in (q.get("label_selector") or [""])[0].split(","):
                        if "=" in sel:
                            k, v = sel.split("=", 1)
                            vols = [x for x in vols if x["labels"].get(k) == v]
                    page = int((q.get("page") or ["1"])[0])
                    chunk = vols[(page - 1) * outer.page_size: page * outer.page_size]
                    more = page * outer.page_size < len(vols)
                    total = outer.truncate_total if outer.truncate_total is not None else len(vols)
                    return self._send(200, {"volumes": chunk, "meta": {"pagination": {"next_page": page + 1 if more else None, "total_entries": total}}})
                if method == "POST" and p == "/volumes":
                    vid = outer.next_id
                    outer.next_id += 1
                    vol = {"id": vid, "name": body["name"], "size": body["size"], "server": body.get("server"), "labels": body["labels"],
                           "protection": {"delete": False}, "created": "2026-01-01T00:00:00+00:00", "location": {"name": "fsn1"}}
                    outer.volumes[vid] = vol
                    return self._send(201, {"volume": vol, "action": {"id": 1, "status": outer.create_status, "command": "create_volume"}, "next_actions": []})
                if p.startswith("/volumes/"):
                    parts = p.split("/")
                    vid = int(parts[2])
                    if vid not in outer.volumes:
                        return self._send(404, {"error": {"code": "not_found", "message": "nope"}})
                    if method == "GET" and len(parts) == 3:
                        return self._send(200, {"volume": outer.volumes[vid]})
                    if method == "DELETE":
                        del outer.volumes[vid]
                        return self._send(204 if False else 200, {})
                    if method == "POST" and len(parts) == 5:
                        act = parts[4]
                        if act == "detach":
                            outer.volumes[vid]["server"] = None
                        if act == "attach":
                            outer.volumes[vid]["server"] = body["server"]
                        if act == "resize":
                            outer.volumes[vid]["size"] = body["size"]
                        return self._send(201, {"action": {"id": 7, "status": "running", "command": act}})
                if p.startswith("/actions/"):
                    if outer.action_poll:
                        return self._send(200, {"action": {"id": int(p.split("/")[2]), "command": "create_volume", **outer.action_poll}})
                    return self._send(200, {"action": {"id": int(p.split("/")[2]), "status": "success", "command": "x"}})
                return self._send(404, {"error": {"code": "not_found", "message": p}})

            do_GET = lambda self: self._handle("GET")  # noqa: E731
            do_POST = lambda self: self._handle("POST")  # noqa: E731
            do_DELETE = lambda self: self._handle("DELETE")  # noqa: E731
        return H


class HetznerTest(unittest.TestCase):
    def setUp(self):
        self.api = FakeHetzner()
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self.api.handler())
        threading.Thread(target=self.server.serve_forever, args=(0.01,), daemon=True).start()
        self.addCleanup(self.server.shutdown)
        self.addCleanup(self.server.server_close)
        self.sleeps = []
        self.p = HetznerProvider("secret-token", base=f"http://127.0.0.1:{self.server.server_address[1]}",
                                 sleep=self.sleeps.append, metadata_fetch={"instance-id": "1000001", "availability-zone": "fsn1-dc14"}.__getitem__)

    def test_whoami_comes_from_metadata(self):
        who = self.p.whoami()
        self.assertEqual((who.server_id, who.location), ("1000001", "fsn1"))

    def test_create_attaches_in_one_call_and_sends_safe_options(self):
        vol = self.p.create_volume("motel-f-1-ab", 50, "fsn1", {"managed-by": "motel"}, server_id="1000001")
        method, path, _, body, auth = [r for r in self.api.requests if r[0] == "POST"][0]
        self.assertEqual(body["server"], 1000001)
        self.assertNotIn("location", body)
        self.assertIs(body["automount"], False)
        self.assertEqual(body["format"], "ext4")
        self.assertEqual(auth, "Bearer secret-token")
        self.assertEqual((vol.size_gb, vol.server_id), (50, "1000001"))

    def test_actions_are_waited_for(self):
        self.p.create_volume("motel-x", 10, "fsn1", {}, server_id="1000001")
        vid = str(next(iter(self.api.volumes)))
        self.p.resize(vid, 20)
        self.p.detach(vid)
        self.assertEqual(self.p.get_volume(vid).server_id, None)
        self.assertTrue(any(r[1].startswith("/actions/") for r in self.api.requests))

    def test_delete_and_not_found(self):
        self.p.create_volume("motel-x", 10, "fsn1", {}, server_id=None)
        vid = str(next(iter(self.api.volumes)))
        self.p.delete(vid)
        with self.assertRaises(NotFound):
            self.p.get_volume(vid)

    def test_list_owned_uses_a_label_selector_and_follows_pages(self):
        self.api.page_size = 2
        for i in range(5):
            self.p.create_volume(f"motel-{i}", 10, "fsn1", {"managed-by": "motel", "host": "1"}, server_id=None)
        self.p.create_volume("other", 10, "fsn1", {"managed-by": "someone"}, server_id=None)
        owned = self.p.list_owned({"managed-by": "motel", "host": "1"})
        self.assertEqual(len(owned), 5)
        self.assertEqual(len(self.p.visible().volumes), 6)

    def test_truncated_listing_is_an_incomplete_inventory(self):
        self.p.create_volume("motel-0", 10, "fsn1", {"managed-by": "motel"}, server_id=None)
        self.api.truncate_total = 9
        with self.assertRaises(InventoryIncomplete):
            self.p.list_owned({"managed-by": "motel"})

    def test_reads_retry_on_5xx_but_a_create_is_never_retried(self):
        self.api.fail[("GET", "/volumes")] = [503, 503]
        self.assertEqual(self.p.list_owned({"managed-by": "motel"}), [])
        self.assertEqual(len(self.sleeps), 2)
        self.api.fail[("POST", "/volumes")] = [503]
        with self.assertRaises(ProviderError) as raised:
            self.p.create_volume("motel-y", 10, "fsn1", {}, server_id=None)
        self.assertTrue(raised.exception.indeterminate)
        self.assertEqual(len([r for r in self.api.requests if r[0] == "POST" and r[1] == "/volumes"]), 1)

    def test_a_rejected_create_is_definitive_and_429_is_retried(self):
        self.api.fail[("POST", "/volumes")] = [422]
        with self.assertRaises(ProviderError) as raised:
            self.p.create_volume("motel-y", 10, "fsn1", {}, server_id=None)
        self.assertFalse(raised.exception.indeterminate)
        self.api.fail[("POST", "/volumes")] = [429]
        self.p.create_volume("motel-z", 10, "fsn1", {}, server_id=None)

    def test_visible_lists_servers_and_price_is_read_gross(self):
        self.assertEqual(self.p.visible().servers, ("1000001", "5"))
        self.assertEqual(str(self.p.price_gb_month("fsn1")), "0.0524")

    def test_device_path_and_serial_reject_odd_ids(self):
        self.assertEqual(self.p.device_path("123"), "/dev/disk/by-id/scsi-0HC_Volume_123")
        self.assertEqual(self.p.expected_serial("123"), "123")
        for bad in ("../etc", "1 2", "", "12a"):
            with self.assertRaises(ProviderError):
                self.p.device_path(bad)
            with self.assertRaises(ProviderError):
                self.p.get_volume(bad)

    def test_redirects_are_refused_so_the_token_cannot_follow_one(self):
        from motel.providers.hetzner import _NoRedirect
        self.assertIsNone(_NoRedirect().redirect_request(None, None, 302, "", {}, "http://elsewhere"))


if __name__ == "__main__":
    unittest.main()
