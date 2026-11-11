"""The loop-file provider with a fake command runner (no root, no loop devices)."""

import json
import os
import tempfile
import unittest
from pathlib import Path

from motel.errors import NotFound, ProviderError
from motel.providers.local import LocalProvider


class Runner:
    def __init__(self):
        self.calls, self.loops = [], {}

    def __call__(self, argv, timeout=0):
        self.calls.append(argv)
        name = os.path.basename(argv[0])
        if name == "losetup" and argv[1] == "-j":
            return f"{self.loops[argv[2]]}: [64769]:1 ({argv[2]})\n" if argv[2] in self.loops else ""
        if name == "losetup" and argv[1] == "--find":
            self.loops[argv[3]] = f"/dev/loop{len(self.loops) + 7}"
            return self.loops[argv[3]] + "\n"
        if name == "losetup" and argv[1] == "-d":
            self.loops = {k: v for k, v in self.loops.items() if v != argv[2]}
        return ""


class LocalProviderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.run = Runner()
        self.p = LocalProvider(os.path.join(self.tmp.name, "vols"), runner=self.run)

    def test_create_makes_a_sparse_file_formats_it_and_records_labels(self):
        vol = self.p.create_volume("motel-f-1", 2, "local", {"managed-by": "motel"}, server_id="local")
        img = Path(self.tmp.name, "vols", f"{vol.id}.img")
        self.assertEqual(img.stat().st_size, 2 * 1024 ** 3)
        self.assertLess(img.stat().st_blocks * 512, 10 * 1024 * 1024)  # sparse
        self.assertTrue(any(os.path.basename(c[0]) == "mkfs.ext4" and c[-1] == str(img) for c in self.run.calls))
        self.assertEqual(self.p.get_volume(vol.id).labels, {"managed-by": "motel"})
        self.assertEqual(self.p.list_owned({"managed-by": "motel"})[0].id, vol.id)
        self.assertEqual(self.p.list_owned({"managed-by": "other"}), [])

    def test_device_path_attaches_a_loop_device_once(self):
        vol = self.p.create_volume("motel-f-1", 1, "local", {}, server_id="local")
        self.assertEqual(self.p.device_path(vol.id), self.p.device_path(vol.id))
        self.assertEqual(len([c for c in self.run.calls if "--find" in c]), 1)
        self.assertIsNone(self.p.expected_serial(vol.id))

    def test_detach_delete_resize(self):
        vol = self.p.create_volume("motel-f-1", 1, "local", {}, server_id="local")
        self.p.device_path(vol.id)
        self.p.resize(vol.id, 3)
        self.assertEqual(self.p.get_volume(vol.id).size_gb, 3)
        with self.assertRaises(ProviderError):
            self.p.resize(vol.id, 1)
        with self.assertRaises(ProviderError):
            self.p.delete(vol.id)  # still attached
        self.p.detach(vol.id)
        self.assertIsNone(self.p.get_volume(vol.id).server_id)
        self.p.delete(vol.id)
        with self.assertRaises(NotFound):
            self.p.get_volume(vol.id)

    def test_protected_volumes_are_not_deleted(self):
        vol = self.p.create_volume("motel-f-1", 1, "local", {}, server_id=None)
        meta = Path(self.tmp.name, "vols", f"{vol.id}.json")
        doc = json.loads(meta.read_text())
        doc["protected"] = True
        meta.write_text(json.dumps(doc))
        with self.assertRaises(ProviderError):
            self.p.delete(vol.id)

    def test_odd_ids_are_rejected(self):
        for bad in ("../x", "l1/../../etc", "x1", ""):
            with self.assertRaises(ProviderError):
                self.p.get_volume(bad)

    def test_whoami_and_price(self):
        self.assertEqual(self.p.whoami().server_id, "local")
        self.assertEqual(str(self.p.price_gb_month("local")), "0")


if __name__ == "__main__":
    unittest.main()
