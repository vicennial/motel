"""Volume ids are never confused with stale records: local-provider id reuse and duplicate live records."""

import os
import unittest
from pathlib import Path

from helpers import SERVER, Fixture
from motel.errors import Guard
from motel.providers.local import LocalProvider
from motel.rooms import Rooms
from test_local_provider import Runner


def local_fixture():
    fx = Fixture()
    fx.provider = LocalProvider(os.path.join(fx.root, "vols"), runner=Runner(), server_id=SERVER)
    fx.ctx.provider = fx.provider
    fx.rooms = Rooms(fx.ctx)
    return fx


class LocalIdTest(unittest.TestCase):
    def test_ids_are_monotonic_even_after_deleting_the_newest_volume(self):
        fx = local_fixture()
        self.addCleanup(fx.close)
        a = fx.provider.create_volume("motel-x", 1, "local", {}, server_id=None)
        fx.provider.delete(a.id)
        b = fx.provider.create_volume("motel-y", 1, "local", {}, server_id=None)
        self.assertNotEqual(a.id, b.id)
        self.assertEqual(int(b.id[1:]), int(a.id[1:]) + 1)
        self.assertEqual(Path(fx.root, "vols", ".next-id").read_text(), b.id[1:])

    def test_checkin_checkout_retire_checkin_repeated_with_the_local_provider(self):
        fx = local_fixture()
        self.addCleanup(fx.close)
        seen = []
        for round_ in range(6):
            room = fx.checkin(10, task=f"r{round_}")
            seen.append(fx.ctx.store.floor(room["floor"])["volume_id"])
            fx.rooms.checkout(fx.caller, room["id"], True)
            fx.later(minutes=1)
            fx.rooms.reap(True)
            fx.later(minutes=16)
            report = fx.rooms.reap(True)
            self.assertFalse(report["errors"] or report["refused"], report)
            self.assertEqual(fx.provider.list_owned({}), [])
        self.assertEqual(len(set(seen)), 6)  # no id was ever reused
        self.assertEqual({f["state"] for f in fx.ctx.store.floors()}, {"deleted"})

    def test_a_stale_deleted_record_never_authorises_a_new_volume(self):
        fx = local_fixture()
        self.addCleanup(fx.close)
        room = fx.checkin(10)
        fx.rooms.checkout(fx.caller, room["id"], True)
        fx.later(minutes=1)
        fx.rooms.reap(True)
        fx.later(minutes=16)
        fx.rooms.reap(True)
        stale = fx.ctx.store.floor(room["floor"])
        self.assertEqual(stale["state"], "deleted")
        self.assertIsNone(fx.ctx.store.floor_by_volume(stale["volume_id"]))  # terminal records do not match
        orphan = fx.provider.create_volume("motel-imposter", 1, "local", {"managed-by": "motel", "role": "floor", "host": SERVER,
                                                                         "floor": stale["id"]}, server_id=SERVER)
        report = fx.rooms.reap(True)
        self.assertTrue(any(orphan.id in o for o in report["orphans"]), report)


class DuplicateRecordTest(unittest.TestCase):
    def test_two_live_records_for_one_volume_stop_everything(self):
        fx = Fixture()
        self.addCleanup(fx.close)
        room = fx.checkin(10)
        floor = fx.ctx.store.floor(room["floor"])
        twin = dict(floor, id="f-ffff")
        fx.ctx.store.save_floor(twin)
        with self.assertRaises(Guard):
            fx.ctx.store.floor_by_volume(floor["volume_id"])
        with self.assertRaises(Guard):
            fx.rooms._guard(floor["volume_id"], "mount")


if __name__ == "__main__":
    unittest.main()
