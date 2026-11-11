"""A pure in-memory provider for tests and for people writing their own adapter."""

from __future__ import annotations

from dataclasses import replace
from datetime import UTC, datetime
from decimal import Decimal

from motel.errors import InventoryIncomplete, NotFound, ProviderError
from motel.providers.base import HostIdentity, Inventory, Volume


class FakeProvider:
    name = "fake"
    currency = "EUR"
    formats_on_create = True
    gb_bytes = 2 ** 30

    def __init__(self, server_id: str = "1000001", location: str = "fsn1", price: str = "0.05",
                 clock=lambda: datetime(2026, 1, 1, tzinfo=UTC)):
        self.server_id, self.location, self.price, self.clock = str(server_id), location, Decimal(price), clock
        self.volumes: dict = {}
        self.calls: list = []
        self.next_id = 900001
        self.lose_next_create = False  # create succeeds at the "cloud" but the response is lost
        self.drop_next_create = False  # the create request never reaches the cloud
        self.fail_after_create = False  # the volume is created, then the wait for the create action fails
        self.fail_next: dict = {}  # op name -> ProviderError to raise once
        self.incomplete = False

    # -- test helpers
    def add(self, vol_id, size_gb=50, labels=None, server_id="self", name=None, protected=False, created=None):
        vol_id = str(vol_id)
        self.volumes[vol_id] = Volume(
            id=vol_id, name=name or f"vol-{vol_id}", size_gb=size_gb,
            server_id=self.server_id if server_id == "self" else server_id,
            labels=dict(labels or {}), delete_protected=protected, created=created or self.clock(),
            location=self.location)
        return self.volumes[vol_id]

    def _maybe_fail(self, op):
        err = self.fail_next.pop(op, None)
        if err:
            raise err

    def ops(self, op: str) -> list:
        return [c for c in self.calls if c[0] == op]

    # -- interface
    def whoami(self):
        return HostIdentity(self.server_id, self.location, "fake")

    def visible(self):
        return Inventory(volumes=tuple(self.volumes.values()), servers=(self.server_id,), complete=not self.incomplete)

    def price_gb_month(self, location):
        self.calls.append(("price", location))
        return self.price

    def get_volume(self, vol_id):
        self.calls.append(("get", vol_id))
        try:
            return self.volumes[str(vol_id)]
        except KeyError:
            raise NotFound(f"no volume {vol_id}") from None

    def create_volume(self, name, size_gb, location, labels, server_id=None):
        self.calls.append(("create", name, size_gb, dict(labels), server_id))
        self._maybe_fail("create")
        if self.drop_next_create:
            self.drop_next_create = False
            raise ProviderError("timeout before the request was delivered", indeterminate=True)
        vol = self.add(self.next_id, size_gb, labels, server_id=server_id, name=name)
        self.next_id += 1
        if self.fail_after_create:  # the volume exists, but waiting for the create action failed (as on Hetzner)
            self.fail_after_create = False
            raise ProviderError(f"volume {vol.id} was created but did not become ready", indeterminate=True,
                                volume_id=vol.id)
        if self.lose_next_create:
            self.lose_next_create = False
            raise ProviderError("timeout waiting for the create response", indeterminate=True)
        return vol

    def attach(self, vol_id, server_id):
        self.calls.append(("attach", str(vol_id), server_id))
        self._maybe_fail("attach")
        self.volumes[str(vol_id)] = replace(self.volumes[str(vol_id)], server_id=str(server_id))

    def detach(self, vol_id):
        self.calls.append(("detach", str(vol_id)))
        self._maybe_fail("detach")
        self.volumes[str(vol_id)] = replace(self.volumes[str(vol_id)], server_id=None)

    def resize(self, vol_id, size_gb):
        self.calls.append(("resize", str(vol_id), size_gb))
        self._maybe_fail("resize")
        if size_gb <= self.volumes[str(vol_id)].size_gb:  # Hetzner rejects a resize that does not grow the volume
            raise ProviderError(f"HTTP 422 invalid_input: size {size_gb} is not larger than {self.volumes[str(vol_id)].size_gb}")
        self.volumes[str(vol_id)] = replace(self.volumes[str(vol_id)], size_gb=size_gb)

    def delete(self, vol_id):
        self.calls.append(("delete", str(vol_id)))
        self._maybe_fail("delete")
        vol = self.volumes[str(vol_id)]
        assert vol.server_id is None and not vol.delete_protected, "fake provider: the cloud would refuse this delete"
        del self.volumes[str(vol_id)]

    def device_path(self, vol_id):
        return f"/dev/fake/{vol_id}"

    def expected_serial(self, vol_id):
        return str(vol_id)

    def list_owned(self, labels):
        self.calls.append(("list", dict(labels)))
        if self.incomplete:
            raise InventoryIncomplete("the provider returned a partial volume list", "motel doctor")
        return [v for v in self.volumes.values() if all(v.labels.get(k) == val for k, val in labels.items())]
