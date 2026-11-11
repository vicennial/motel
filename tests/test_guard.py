"""The allowlist guard, never-touch, forged labels, orphans and lost create responses."""

import unittest

from helpers import BASE_TOML, SERVER, Fixture, toml_with
from motel.errors import Guard, InventoryIncomplete, ProviderError
from motel.guard import check_owned
from motel.providers.base import Volume

GOOD_LABELS = {"managed-by": "motel", "role": "floor", "host": SERVER, "floor": "f-0001"}


def vol(**over):
    base = dict(id="7", name="motel-f-0001-ab", size_gb=50, server_id=SERVER, labels=dict(GOOD_LABELS), delete_protected=False)
    base.update(over)
    return Volume(**base)


def record(**over):
    base = {"id": "f-0001", "volume_id": "7", "state": "retiring"}
    base.update(over)
    return base


def check(v=None, rec="default", action="delete", never=()):
    check_owned(v or vol(), record() if rec == "default" else rec, action=action, server_id=SERVER, never_touch=never)


class GuardUnitTest(unittest.TestCase):
    def test_a_fully_matching_volume_passes(self):
        check()
        check(action="detach")
        check(vol(server_id=None))  # detached before delete

    def test_every_missing_condition_refuses(self):
        cases = {
            "no record": dict(rec=None),
            "other record": dict(rec=record(volume_id="8")),
            "wrong record state": dict(rec=record(state="active")),
            "never touch": dict(never=("7",)),
            "no managed-by": dict(v=vol(labels={**GOOD_LABELS, "managed-by": "other"})),
            "no role": dict(v=vol(labels={k: v for k, v in GOOD_LABELS.items() if k != "role"})),
            "other host label": dict(v=vol(labels={**GOOD_LABELS, "host": "99"})),
            "floor label mismatch": dict(v=vol(labels={**GOOD_LABELS, "floor": "f-0002"})),
            "bad name": dict(v=vol(name="prod-data")),
            "delete protected": dict(v=vol(delete_protected=True)),
            "other server": dict(v=vol(server_id="99")),
        }
        for label, kw in cases.items():
            with self.subTest(label), self.assertRaises(Guard):
                check(**kw)

    def test_detach_and_mount_need_attachment_to_this_server(self):
        for action, state in (("detach", "retiring"), ("mount", "active"), ("resize", "active")):
            with self.subTest(action), self.assertRaises(Guard):
                check(vol(server_id=None), record(state=state), action=action)

    def test_attach_refuses_a_volume_that_is_attached_elsewhere(self):
        with self.assertRaises(Guard):
            check(vol(server_id="99"), record(state="created"), action="attach")
        check(vol(server_id=None), record(state="created"), action="attach")


class GuardIntegrationTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture(never_touch=["555"])
        self.addCleanup(self.fx.close)

    def sneaky(self, vid, **kw):
        labels = {"managed-by": "motel", "role": "floor", "host": SERVER, "floor": "f-dead"}
        self.fx.provider.add(vid, 50, labels=kw.pop("labels", labels), name=kw.pop("name", f"motel-sneaky-{vid}"), **kw)

    def reap_after_a_week(self):
        self.fx.later(days=9)
        return self.fx.rooms.reap(True)

    def test_forged_labels_without_a_record_are_never_touched_only_reported(self):
        self.sneaky(1)  # perfect labels and name, but motel never recorded an intent for it
        self.sneaky(2, server_id=None)
        report = self.reap_after_a_week()
        self.assertEqual(set(self.fx.provider.volumes), {"1", "2"})
        self.assertFalse([c for c in self.fx.provider.calls if c[0] in ("detach", "delete", "resize", "attach")])
        self.assertEqual(len(report["orphans"]), 2)

    def test_never_touch_ids_survive_even_with_a_record(self):
        room = self.fx.checkin(10)
        floor = self.fx.ctx.store.floor(room["floor"])
        vid = floor["volume_id"]
        floor.update(state="retiring")
        self.fx.ctx.store.save_floor(floor)
        self.fx.rooms.never = self.fx.rooms.never | {vid}
        with self.assertRaises(Guard):
            self.fx.rooms._retire_floor(floor)
        self.assertIn(vid, self.fx.provider.volumes)

    def test_config_can_add_never_touch_ids_but_the_receipt_set_stays(self):
        fx = Fixture(BASE_TOML.replace('allow_users = ["alice", "@agents"]', 'allow_users = ["alice"]\nnever_touch = [777]'), never_touch=["555"])
        self.addCleanup(fx.close)
        self.assertEqual(fx.rooms.never, {"555", "777"})

    def test_unlabelled_protected_and_foreign_volumes_are_untouched(self):
        self.sneaky(3, labels={}, name="motel-nolabels")
        self.sneaky(4, protected=True)
        self.sneaky(5, name="not-motel")
        self.sneaky(6, server_id="999")
        self.sneaky(555)
        self.reap_after_a_week()
        self.assertEqual(set(self.fx.provider.volumes), {"3", "4", "5", "6", "555"})

    def test_a_recorded_floor_whose_volume_is_delete_protected_is_not_deleted(self):
        room = self.fx.checkin(10)
        self.fx.rooms.checkout(self.fx.caller, room["id"], True)
        vid = self.fx.ctx.store.floor(room["floor"])["volume_id"]
        from dataclasses import replace
        self.fx.provider.volumes[vid] = replace(self.fx.provider.volumes[vid], delete_protected=True)
        self.fx.later(minutes=20)
        self.fx.rooms.reap(True)
        self.fx.later(minutes=20)
        report = self.fx.rooms.reap(True)
        self.assertIn(vid, self.fx.provider.volumes)
        self.assertTrue(any("delete protection" in r for r in report["refused"]))
        self.assertFalse(self.fx.provider.ops("delete"))

    def test_relabelled_floor_is_refused_before_detach(self):
        room = self.fx.checkin(10)
        self.fx.rooms.checkout(self.fx.caller, room["id"], True)
        vid = self.fx.ctx.store.floor(room["floor"])["volume_id"]
        from dataclasses import replace
        self.fx.provider.volumes[vid] = replace(self.fx.provider.volumes[vid], labels={})
        self.fx.later(minutes=20)
        self.fx.rooms.reap(True)
        self.fx.later(minutes=20)
        self.fx.rooms.reap(True)
        self.assertFalse(self.fx.provider.ops("detach") or self.fx.provider.ops("delete"))

    def test_floor_attached_to_another_server_is_an_orphan_report(self):
        room = self.fx.checkin(10)
        vid = self.fx.ctx.store.floor(room["floor"])["volume_id"]
        from dataclasses import replace
        self.fx.provider.volumes[vid] = replace(self.fx.provider.volumes[vid], server_id="999")
        report = self.fx.rooms.reap(True)
        self.assertTrue(any("attached to server 999" in o for o in report["orphans"]))
        self.assertFalse(self.fx.provider.ops("detach"))

    def test_a_vanished_empty_floor_is_closed_out(self):
        room = self.fx.checkin(10)
        self.fx.rooms.checkout(self.fx.caller, room["id"], True)
        vid = self.fx.ctx.store.floor(room["floor"])["volume_id"]
        del self.fx.provider.volumes[vid]
        self.fx.rooms.reap(True)
        self.assertEqual(self.fx.ctx.store.floor(room["floor"])["state"], "deleted")

    def test_wrong_device_serial_or_filesystem_refuses_to_mount(self):
        self.fx.sysops.serial_override["/dev/fake/900001"] = "something-else"
        with self.assertRaises(Guard):
            self.fx.checkin(10)
        self.assertFalse([c for c in self.fx.sysops.calls if c[0] == "mount"])

    def test_non_ext4_refuses_to_mount(self):
        self.fx.sysops.fstype_override["/dev/fake/900001"] = "xfs"
        with self.assertRaises(Guard):
            self.fx.checkin(10)


class ReconcileTest(unittest.TestCase):
    def setUp(self):
        self.fx = Fixture()
        self.addCleanup(self.fx.close)

    def test_lost_create_response_is_reconciled_by_label_without_a_second_volume(self):
        self.fx.provider.lose_next_create = True
        room = self.fx.checkin(10)
        self.assertEqual(len(self.fx.provider.volumes), 1)
        self.assertEqual(len(self.fx.provider.ops("create")), 1)
        self.assertEqual(self.fx.ctx.store.floor(room["floor"])["state"], "active")

    def test_a_create_that_never_arrived_stays_reserved_then_is_abandoned(self):
        self.fx.provider.drop_next_create = True
        with self.assertRaises(ProviderError) as raised:
            self.fx.checkin(10)
        self.assertTrue(raised.exception.indeterminate)
        [floor] = self.fx.ctx.store.floors()
        self.assertEqual(floor["state"], "intent")
        self.fx.later(hours=2)
        self.fx.rooms.reap(True)
        self.assertEqual(self.fx.ctx.store.floors()[0]["state"], "abandoned")

    def test_a_late_volume_for_an_abandoned_intent_is_matched_by_its_unique_label_and_name(self):
        self.fx.provider.drop_next_create = True
        with self.assertRaises(ProviderError):
            self.fx.checkin(10)
        [floor] = self.fx.ctx.store.floors()
        self.fx.provider.add(42, 50, labels=floor["labels"], name=floor["name"])
        self.fx.rooms.reconcile()
        self.assertEqual(self.fx.ctx.store.floors()[0]["volume_id"], "42")

    def test_same_labels_but_a_different_name_is_not_adopted(self):
        self.fx.provider.drop_next_create = True
        with self.assertRaises(ProviderError):
            self.fx.checkin(10)
        [floor] = self.fx.ctx.store.floors()
        self.fx.provider.add(42, 50, labels=floor["labels"], name="motel-impostor")
        _, orphans = self.fx.rooms.reconcile()
        self.assertIsNone(self.fx.ctx.store.floors()[0]["volume_id"])
        self.assertEqual([o.id for o in orphans], ["42"])

    def test_a_definitive_provider_refusal_frees_the_reservation(self):
        self.fx.provider.fail_next["create"] = ProviderError("HTTP 422 invalid_input")
        with self.assertRaises(ProviderError):
            self.fx.checkin(10)
        self.assertEqual(self.fx.ctx.store.floors()[0]["state"], "failed")
        self.assertEqual(self.fx.rooms._capacity()["total_gb"], 0)
        self.assertTrue(self.fx.checkin(10))

    def test_incomplete_inventory_refuses_checkin_with_exit_9(self):
        self.fx.provider.incomplete = True
        with self.assertRaises(InventoryIncomplete) as raised:
            self.fx.checkin(10)
        self.assertEqual(raised.exception.code, 9)
        self.assertFalse(self.fx.provider.ops("create"))

    def test_incomplete_inventory_reports_in_housekeeping_but_still_expires_rooms(self):
        room = self.fx.checkin(10, hours=1)
        self.fx.provider.incomplete = True
        self.fx.later(hours=2)
        report = self.fx.rooms.reap(True)
        self.assertTrue(report["errors"])
        self.assertEqual(self.fx.ctx.store.room(room["id"])["state"], "grace")

    def test_failed_resize_keeps_the_true_size_in_the_record(self):
        fx = Fixture(toml_with(max_floors=1))
        self.addCleanup(fx.close)
        fx.checkin(40)
        fx.provider.fail_next["resize"] = ProviderError("boom")
        with self.assertRaises(ProviderError):
            fx.checkin(30)
        self.assertEqual(fx.ctx.store.floors()[0]["size_gb"], 50)


if __name__ == "__main__":
    unittest.main()
