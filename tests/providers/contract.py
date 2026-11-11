"""Behaviour every volume provider must show, run against each provider's own loopback fake API.

A concrete test class mixes this in with ``unittest.TestCase`` and supplies the hooks listed on
:class:`ProviderContract`. Nothing here talks to the network or uses a real credential.
"""

from __future__ import annotations

import inspect
from decimal import Decimal

from fakehttp import TOKEN, Clock, serve

from motel.errors import InventoryIncomplete, NotFound, ProviderError
from motel.providers.base import HostIdentity, Inventory, Volume, VolumeProvider

LABELS = {"managed-by": "motel", "role": "floor", "host": "HOSTID", "floor": "f-ab12", "suite": "0",
          "created": "20261109t1200z"}


class ProviderContract:
    """Hooks a subclass provides:

    ``new_api()`` the fake API; ``new_provider(base, clock)``; ``HOST``, ``REGION``, ``NAME`` (a valid volume name);
    ``BAD_IDS`` (ids the provider must refuse); ``LIST_PATH`` and ``CREATE_PATH`` (for failure injection);
    ``ATTACH_FAIL_PREFIX`` (POST path prefix of the attach call that follows a create, or None);
    ``api.add_volume(name, size, labels, attached)``; ``api.wedge()`` (make waited-for operations never finish);
    ``check_device(vol, path, serial)``.
    """

    ATTACH_FAIL_PREFIX = None
    NAME = "motel-f-ab12-cd34"

    def setUp(self):
        self.api = self.new_api()
        self.clock = Clock()
        self.base = serve(self, self.api)
        self.p = self.new_provider(self.base, self.clock)
        self.labels = {**LABELS, "host": self.HOST}

    def make(self, suffix="cd34", size=50, attached=True, labels=None):
        return self.p.create_volume(f"motel-f-ab12-{suffix}", size, self.REGION, labels or self.labels,
                                    server_id=self.HOST if attached else None)

    # -- shape
    def test_provider_has_the_exact_protocol_surface(self):
        for attr, member in inspect.getmembers(VolumeProvider, inspect.isfunction):
            if attr.startswith("_"):
                continue
            mine = getattr(self.p, attr)
            want = list(inspect.signature(member).parameters)[1:]
            have = list(inspect.signature(mine).parameters)
            self.assertEqual(have, want, attr)
        self.assertIsInstance(self.p.name, str)
        self.assertIsInstance(self.p.currency, str)
        self.assertIs(self.p.formats_on_create, False)

    def test_whoami_comes_from_metadata(self):
        who = self.p.whoami()
        self.assertEqual(who, HostIdentity(self.HOST, self.REGION, self.p.name))

    def test_whoami_refuses_odd_metadata(self):
        p = self.new_provider(self.base, self.clock, metadata=self.ODD_METADATA)
        with self.assertRaises(ProviderError):
            p.whoami()
        with self.assertRaises(ProviderError):
            self.new_provider(self.base, self.clock, metadata=self.BROKEN_METADATA).whoami()

    # -- create and read back
    def test_create_attached_round_trips_the_labels(self):
        vol = self.make()
        self.assertIsInstance(vol, Volume)
        self.assertEqual((vol.name, vol.size_gb, vol.server_id, vol.location), (self.NAME, 50, self.HOST, self.REGION))
        self.assertEqual(vol.labels, self.labels)
        self.assertFalse(vol.delete_protected)
        fresh = self.p.get_volume(vol.id)
        self.assertEqual((fresh.name, fresh.labels, fresh.server_id), (self.NAME, self.labels, self.HOST))
        self.assertIsInstance(vol.id, str)

    def test_create_unattached_leaves_the_volume_free(self):
        vol = self.make(attached=False)
        self.assertIsNone(vol.server_id)
        self.assertIsNone(self.p.get_volume(vol.id).server_id)

    def test_the_token_goes_only_into_the_authorization_header(self):
        self.make()
        self.p.visible()
        for _, path, query, body, auth, headers in self.api.requests:
            self.assertEqual(auth, f"Bearer {TOKEN}")
            self.assertNotIn(TOKEN, path + repr(query) + repr(body))
            self.assertEqual([k for k, v in headers.items() if TOKEN in v], ["Authorization"])

    def test_a_bad_name_or_label_is_refused_before_any_request(self):
        before = len(self.api.requests)
        with self.assertRaises(ProviderError):
            self.p.create_volume("Not A Valid Name!", 50, self.REGION, self.labels, server_id=None)
        with self.assertRaises(ProviderError):
            self.p.create_volume(self.NAME, 50, self.REGION, {"Bad Key": "x"}, server_id=None)
        with self.assertRaises(ProviderError):
            self.p.create_volume(self.NAME, 50, self.REGION, {"host": "x" * 400}, server_id=None)
        self.assertEqual(len(self.api.requests), before)

    # -- ownership listing
    def test_list_owned_matches_every_requested_label_and_nothing_foreign(self):
        a, b = self.make("aaaa"), self.make("bbbb", labels={**self.labels, "floor": "f-ffff"})
        self.api.add_volume("customer-data", 100, {}, attached=None)
        self.api.add_volume("motel-f-ab12-zzzz", 100, {"managed-by": "someone-else"}, attached=None)
        ids = {v.id for v in self.p.list_owned({"managed-by": "motel", "role": "floor", "host": self.HOST})}
        self.assertEqual(ids, {a.id, b.id})
        self.assertEqual([v.id for v in self.p.list_owned({"floor": "f-ffff", "managed-by": "motel"})], [b.id])
        self.assertEqual(self.p.list_owned({"managed-by": "motel", "host": "other"}), [])
        self.assertEqual(len(self.p.visible().volumes), 4)

    def test_list_owned_follows_pages(self):
        self.api.page_size = 2
        for i in range(5):
            self.make(f"v{i:03d}", attached=False)
        self.api.add_volume("other", 10, {}, attached=None)
        self.assertEqual(len(self.p.list_owned({"managed-by": "motel"})), 5)
        self.assertEqual(len(self.p.visible().volumes), 6)

    def test_a_truncated_listing_is_an_incomplete_inventory(self):
        self.make(attached=False)
        self.api.truncate_total = 9
        with self.assertRaises(InventoryIncomplete):
            self.p.list_owned({"managed-by": "motel"})
        with self.assertRaises(InventoryIncomplete):
            self.p.visible()

    def test_a_listing_that_fails_is_an_incomplete_inventory_not_an_empty_one(self):
        self.api.fail[("GET", self.LIST_PATH)] = [500] * 6
        with self.assertRaises(InventoryIncomplete):
            self.p.list_owned({"managed-by": "motel"})

    def test_visible_lists_servers(self):
        inv = self.p.visible()
        self.assertIsInstance(inv, Inventory)
        self.assertIn(self.HOST, inv.servers)
        self.assertTrue(inv.complete)

    # -- lifecycle
    def test_resize_detach_delete(self):
        vol = self.make()
        self.p.resize(vol.id, 80)
        self.assertEqual(self.p.get_volume(vol.id).size_gb, 80)
        self.p.detach(vol.id)
        self.assertIsNone(self.p.get_volume(vol.id).server_id)
        self.p.detach(vol.id)  # already detached: nothing to do, not an error
        self.p.attach(vol.id, self.HOST)
        self.assertEqual(self.p.get_volume(vol.id).server_id, self.HOST)
        self.p.detach(vol.id)
        self.p.delete(vol.id)
        with self.assertRaises(NotFound):
            self.p.get_volume(vol.id)

    def test_resize_only_grows(self):
        vol = self.make()
        for size in (50, 40):
            with self.assertRaises(ProviderError):
                self.p.resize(vol.id, size)
        self.assertEqual(self.p.get_volume(vol.id).size_gb, 50)

    def test_operations_that_never_finish_time_out_as_indeterminate(self):
        vol = self.make()
        self.api.wedge()
        with self.assertRaises(ProviderError) as raised:
            self.p.resize(vol.id, 90)
        self.assertTrue(raised.exception.indeterminate)

    def test_a_failed_attach_after_create_is_reported_as_unresolved(self):
        if self.ATTACH_FAIL_PREFIX is None:
            self.skipTest("this provider attaches inside the create call")
        self.api.fail[("POST", self.ATTACH_FAIL_PREFIX)] = [422]
        with self.assertRaises(ProviderError) as raised:
            self.make()
        self.assertTrue(raised.exception.indeterminate)
        self.assertEqual(len(self.p.list_owned({"managed-by": "motel"})), 1)  # the desk finds it by label

    # -- guest device
    def test_device_path_and_serial(self):
        vol = self.make()
        self.check_device(vol, self.p.device_path(vol.id), self.p.expected_serial(vol.id))

    def test_serial_check_can_be_switched_off(self):
        vol = self.make()
        p = self.new_provider(self.base, self.clock, serial_check=False)
        self.assertIsNone(p.expected_serial(vol.id))
        self.assertTrue(p.device_path(vol.id).startswith("/dev/disk/by-id/"))

    # -- price
    def test_price_is_a_euro_decimal_from_the_table_and_the_rate(self):
        price = self.p.price_gb_month(self.REGION)
        self.assertIsInstance(price, Decimal)
        self.assertEqual(price, self.PRICE)
        half = self.new_provider(self.base, self.clock, eur_per_usd=Decimal("0.5"))
        self.assertEqual(half.price_gb_month(self.REGION), (self.PRICE / 2).quantize(Decimal("0.0001")))

    # -- ids
    def test_odd_ids_are_refused_before_any_request(self):
        before = len(self.api.requests)
        for bad in self.BAD_IDS:
            for call in (self.p.get_volume, self.p.device_path, self.p.expected_serial, self.p.delete, self.p.detach):
                with self.assertRaises(ProviderError):
                    call(bad)
            with self.assertRaises(ProviderError):
                self.p.attach(bad, self.HOST)
            with self.assertRaises(ProviderError):
                self.p.resize(bad, 99)
        for bad in self.BAD_IDS:
            with self.assertRaises(ProviderError):
                self.p.attach(self.GOOD_VOLUME_ID, bad)
        self.assertEqual(len(self.api.requests), before)

    # -- failure handling
    def test_reads_retry_on_5xx_but_a_create_is_never_retried(self):
        self.api.fail[("GET", self.LIST_PATH)] = [503, 503]
        self.assertEqual(self.p.list_owned({"managed-by": "motel"}), [])
        self.assertEqual(len(self.clock.sleeps), 2)
        self.api.fail[("POST", self.CREATE_PATH)] = [503]
        with self.assertRaises(ProviderError) as raised:
            self.make(attached=False)
        self.assertTrue(raised.exception.indeterminate)
        self.assertEqual(len([r for r in self.api.requests if r[0] == "POST" and r[1] == self.CREATE_PATH]), 1)

    def test_a_rejected_write_is_definitive_and_429_is_retried(self):
        self.api.fail[("POST", self.CREATE_PATH)] = [422]
        with self.assertRaises(ProviderError) as raised:
            self.make(attached=False)
        self.assertFalse(raised.exception.indeterminate)
        self.api.fail[("POST", self.CREATE_PATH)] = [429, 429]
        self.make("retry", attached=False)
        self.assertEqual(len(self.api.posts(self.CREATE_PATH)), 1 + 3)

    def test_retry_after_is_honoured_but_bounded(self):
        self.api.reply_headers = {"Retry-After": "7"}
        self.api.fail[("GET", self.LIST_PATH)] = [429]
        self.p.list_owned({})
        self.assertEqual(self.clock.sleeps[0], 7.0)
        self.api.reply_headers = {"Retry-After": "100000"}
        self.api.fail[("GET", self.LIST_PATH)] = [429]
        self.p.list_owned({})
        self.assertEqual(self.clock.sleeps[1], 60.0)

    def test_a_lost_write_response_is_indeterminate(self):
        dead = self.new_provider("http://127.0.0.1:9", self.clock)  # nothing listens here
        with self.assertRaises(ProviderError) as raised:
            dead.create_volume(self.NAME, 50, self.REGION, self.labels, server_id=None)
        self.assertTrue(raised.exception.indeterminate)
        with self.assertRaises(ProviderError) as raised:
            dead.get_volume(self.GOOD_VOLUME_ID)
        self.assertFalse(raised.exception.indeterminate)

    def test_redirects_are_refused_so_the_token_cannot_follow_one(self):
        from motel.providers._common import NoRedirect
        self.assertIsNone(NoRedirect().redirect_request(None, None, 302, "", {}, "http://elsewhere"))

    def test_a_missing_volume_is_not_found(self):
        with self.assertRaises(NotFound):
            self.p.get_volume(self.GOOD_VOLUME_ID)
