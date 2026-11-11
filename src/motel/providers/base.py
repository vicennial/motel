"""The provider interface. Providers speak to a cloud (or to loop files); they never decide policy."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from decimal import Decimal
from typing import Protocol


@dataclass(frozen=True)
class HostIdentity:
    server_id: str
    location: str
    provider: str


@dataclass(frozen=True)
class Volume:
    id: str
    name: str
    size_gb: int
    server_id: str | None
    labels: dict = field(default_factory=dict)
    delete_protected: bool = False
    created: datetime | None = None
    location: str | None = None


@dataclass(frozen=True)
class Inventory:
    """Everything the credentials can see. ``complete`` is false when paging or a call failed part-way."""

    volumes: tuple
    servers: tuple = ()
    complete: bool = True


class VolumeProvider(Protocol):
    """Volume ids are strings. Calls raise ``motel.errors.ProviderError`` (``indeterminate=True`` when a
    write may have happened) or ``motel.errors.InventoryIncomplete``."""

    name: str
    currency: str
    """ISO code the provider's own price list uses ("EUR" or "USD"); ``price_gb_month`` converts to euro where it can."""
    formats_on_create: bool
    """True when a created volume already carries an ext4 filesystem. Providers without the attribute are treated as
    True (the desk reads it with ``getattr(provider, "formats_on_create", True)``). A provider that sets it to False
    hands out blank devices and the desk formats them after proving them blank (``motel.sysops.format_blank_device``)."""

    def whoami(self) -> HostIdentity:
        """Server id and location of THIS host, read from the host's own metadata."""

    def visible(self) -> Inventory:
        """Every volume (and server id) the credentials can see; used by setup and doctor."""

    def price_gb_month(self, location: str) -> Decimal:
        """Euro price of one GB for one month."""

    def get_volume(self, vol_id: str) -> Volume:
        """Fresh read of one volume (raises NotFound)."""

    def create_volume(self, name: str, size_gb: int, location: str, labels: dict,
                      server_id: str | None = None) -> Volume:
        """Create a volume (formatted when ``formats_on_create``); attach it when ``server_id`` is given."""

    def attach(self, vol_id: str, server_id: str) -> None: ...

    def detach(self, vol_id: str) -> None: ...

    def resize(self, vol_id: str, size_gb: int) -> None:
        """Grow only."""

    def delete(self, vol_id: str) -> None: ...

    def device_path(self, vol_id: str) -> str:
        """Stable device path of an attached volume. A provider may raise before the volume is attached (Vultr only
        learns the path then), so the desk asks for it after the attach."""

    def expected_serial(self, vol_id: str) -> str | None:
        """Block-device serial the desk must see before it mounts, or None when not applicable."""

    def list_owned(self, labels: dict) -> list:
        """Volumes whose labels contain all of ``labels`` (raises InventoryIncomplete on partial reads)."""
