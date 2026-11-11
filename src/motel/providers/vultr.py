"""Vultr provider (standard library only). BETA: not verified against the live API.

Vultr Block Storage has a free-text label and nothing else (no tags, no key/value labels), so the volume name
and all of motel's labels share the one label (see ``_common.encode_label``). A volume is created blank and
attached with a second call; the desk formats it. See ``docs/providers/vultr.md`` for the sources of every API
detail used here.
"""

from __future__ import annotations

import json
import re
import time
import urllib.parse
from datetime import datetime
from decimal import Decimal

from motel.errors import InventoryIncomplete, ProviderError
from motel.providers._common import (
    MAX_PAGES,
    ApiClient,
    decode_label,
    default_open,
    encode_label,
    fetch_text,
    matches,
    short,
    to_eur,
)
from motel.providers.base import HostIdentity, Inventory, Volume

API_BASE = "https://api.vultr.com/v2"
METADATA_BASE = "http://169.254.169.254"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
REGION_RE = re.compile(r"^[a-z0-9]{2,12}$")
MOUNT_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")
BLOCK_TYPES = ("high_perf", "storage_opt")
MAX_LABEL = 255  # the API reference does not state a limit; a longer label is refused rather than truncated
PER_PAGE = 500
SERIAL_LEN = 20  # virtio-blk serials are at most 20 bytes, so the by-id link carries the first 20 of mount_id
# USD per GB-month. NVMe (high_perf) and HDD (storage_opt) block storage; see docs/providers/vultr.md for the
# sources and for why these figures need confirming against the pricing page.
PRICE_USD_GB_MONTH = {"high_perf": Decimal("0.10"), "storage_opt": Decimal("0.04")}
CURRENCY = "USD"


def _describe(raw: bytes) -> str:
    try:
        return short((json.loads(raw) or {}).get("error", ""))
    except (ValueError, AttributeError):
        return ""


def _check_id(value, what: str = "volume") -> str:
    text = str(value).lower()
    if not UUID_RE.fullmatch(text):
        raise ProviderError(f"refusing odd {what} id {str(value)!r}")
    return text


def _parse_time(text):
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None
    except (ValueError, AttributeError):
        return None


def parse_volume(doc: dict) -> Volume:
    name, labels = decode_label(doc.get("label", ""))
    return Volume(id=str(doc["id"]).lower(), name=name, size_gb=int(doc["size_gb"]),
                  server_id=str(doc.get("attached_to_instance") or "").lower() or None, labels=labels,
                  delete_protected=False, created=_parse_time(doc.get("date_created")), location=doc.get("region"))


class VultrProvider:
    name = "vultr"
    currency = CURRENCY
    formats_on_create = False  # the API cannot format a volume; the desk runs mkfs on a blank, verified device

    def __init__(self, token: str, *, base: str = API_BASE, timeout: float = 30.0, sleep=time.sleep,
                 monotonic=time.monotonic, opener=default_open, metadata_fetch=None,
                 eur_per_usd: Decimal = Decimal("1"), block_type: str = "high_perf", serial_check: bool = True):
        if block_type not in BLOCK_TYPES:
            raise ProviderError(f"block_type must be one of {', '.join(BLOCK_TYPES)}")
        self.sleep, self.monotonic = sleep, monotonic
        self.api = ApiClient("Vultr", token, base, _describe, timeout=timeout, sleep=sleep, opener=opener)
        self._metadata = metadata_fetch or self._fetch_metadata
        self.eur_per_usd, self.block_type, self.serial_check = Decimal(eur_per_usd), block_type, serial_check

    def request(self, method, path, body=None):
        return self.api.request(method, path, body)

    # -- paging and waiting
    def pages(self, path: str, key: str) -> list:
        """All items of a cursor-paginated listing; raises InventoryIncomplete if it cannot be completed."""
        out, cursor, total = [], "", None
        sep = "&" if "?" in path else "?"
        for _ in range(MAX_PAGES):
            url = f"{path}{sep}per_page={PER_PAGE}" + (f"&cursor={urllib.parse.quote(cursor, safe='')}" if cursor else "")
            try:
                doc = self.request("GET", url)
            except ProviderError as exc:
                raise InventoryIncomplete(f"cannot read the full {key} list: {exc.message}", "motel doctor") from exc
            out += doc.get(key) or []
            meta = doc.get("meta") or {}
            total = meta.get("total", total)
            cursor = ((meta.get("links") or {}).get("next")) or ""
            if not cursor:
                if isinstance(total, int) and len(out) < total:
                    raise InventoryIncomplete(f"the {key} list ended early ({len(out)} of {total})", "motel doctor")
                return out
            if not re.fullmatch(r"[A-Za-z0-9_=+/.-]{1,512}", cursor):
                raise InventoryIncomplete(f"the {key} list returned an odd page cursor", "motel doctor")
        raise InventoryIncomplete(f"the {key} list has too many pages", "motel doctor")

    def wait_for(self, vol_id: str, done, what: str, timeout: float = 240.0) -> dict:
        """Poll the block until ``done(block)`` is true. Vultr block changes finish on the block itself."""
        deadline = self.monotonic() + timeout
        while True:
            block = self.request("GET", f"/blocks/{vol_id}").get("block") or {}
            if done(block):
                return block
            if self.monotonic() > deadline:
                raise ProviderError(f"Vultr block {vol_id}: {what} timed out (status {block.get('status')})",
                                    indeterminate=True)
            self.sleep(2.0)

    # -- metadata
    @staticmethod
    def _fetch_metadata(path: str) -> str:
        return fetch_text(f"{METADATA_BASE}/{path}")

    def whoami(self) -> HostIdentity:
        try:
            doc = json.loads(self._metadata("v1.json"))
            server_id = str(doc.get("instance-v2-id") or self._metadata("v1/instance-v2-id")).lower()
            region = str((doc.get("region") or {})["regioncode"]).lower()
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("cannot read this instance's id from the Vultr metadata service "
                                f"(is this a Vultr Cloud Compute instance?): {exc}") from exc
        if not UUID_RE.fullmatch(server_id):
            raise ProviderError(f"unexpected instance id {server_id!r} from the metadata service")
        if not REGION_RE.fullmatch(region):
            raise ProviderError(f"unexpected region {region!r} from the metadata service")
        return HostIdentity(server_id, region, "vultr")

    # -- reads
    def visible(self) -> Inventory:
        servers = self.pages("/instances", "instances")
        volumes = self.pages("/blocks", "blocks")
        return Inventory(volumes=tuple(parse_volume(v) for v in volumes),
                         servers=tuple(str(s["id"]).lower() for s in servers), complete=True)

    def price_gb_month(self, location: str) -> Decimal:
        """Euro price of one GB-month: the USD table price for ``block_type`` times ``eur_per_usd``."""
        return to_eur(PRICE_USD_GB_MONTH[self.block_type], self.eur_per_usd)

    def get_volume(self, vol_id) -> Volume:
        return parse_volume(self.request("GET", f"/blocks/{_check_id(vol_id)}")["block"])

    def list_owned(self, labels: dict) -> list:
        vols = [parse_volume(v) for v in self.pages("/blocks", "blocks")]  # the API has no label filter
        return [v for v in vols if matches(v.labels, labels)]

    # -- writes
    def create_volume(self, name, size_gb, location, labels, server_id=None) -> Volume:
        if not REGION_RE.fullmatch(str(location)):
            raise ProviderError(f"odd region {location!r}")
        body = {"region": location, "size_gb": int(size_gb), "block_type": self.block_type,
                "label": encode_label(name, labels, MAX_LABEL)}
        block = self.request("POST", "/blocks", body).get("block") or {}
        vid = _check_id(block.get("id", ""))
        try:
            ready = self.wait_for(vid, lambda b: b.get("status") == "active", "creation")
            if server_id is not None:
                self.attach(vid, server_id)
                ready = self.request("GET", f"/blocks/{vid}")["block"]
        except ProviderError as exc:
            # The block exists: report it as unresolved so the desk looks it up by label instead of giving up.
            raise ProviderError(f"block {vid} was created but is not ready: {exc.message}", exc.hint,
                                indeterminate=True) from exc
        return parse_volume(ready)

    def attach(self, vol_id, server_id) -> None:
        vid, sid = _check_id(vol_id), _check_id(server_id, "instance")
        self.request("POST", f"/blocks/{vid}/attach", {"instance_id": sid, "live": True})
        self.wait_for(vid, lambda b: str(b.get("attached_to_instance") or "").lower() == sid
                      and b.get("status") == "active", "attach")

    def detach(self, vol_id) -> None:
        vid = _check_id(vol_id)
        if self.get_volume(vid).server_id is None:
            return  # already detached
        self.request("POST", f"/blocks/{vid}/detach", {"live": True})
        self.wait_for(vid, lambda b: not b.get("attached_to_instance") and b.get("status") == "active", "detach")

    def resize(self, vol_id, size_gb) -> None:
        vid = _check_id(vol_id)
        current = self.get_volume(vid)
        if int(size_gb) <= current.size_gb:
            raise ProviderError(f"Vultr volumes only grow: {size_gb} GB is not larger than {current.size_gb} GB")
        self.request("PATCH", f"/blocks/{vid}", {"size_gb": int(size_gb)})
        self.wait_for(vid, lambda b: b.get("status") == "active" and int(b.get("size_gb", 0)) == int(size_gb), "resize")

    def delete(self, vol_id) -> None:
        self.request("DELETE", f"/blocks/{_check_id(vol_id)}")

    # -- guest device
    def _mount_serial(self, vol_id) -> str:
        """The virtio serial: the first 20 characters of the block's ``mount_id`` (only set while attached)."""
        mount = str(self.request("GET", f"/blocks/{_check_id(vol_id)}")["block"].get("mount_id") or "")
        if not MOUNT_RE.fullmatch(mount):
            raise ProviderError(f"block {vol_id} has no usable mount_id (it is attached only after an attach call)")
        return mount[:SERIAL_LEN]

    def device_path(self, vol_id) -> str:
        return f"/dev/disk/by-id/virtio-{self._mount_serial(vol_id)}"

    def expected_serial(self, vol_id) -> str | None:
        return self._mount_serial(vol_id) if self.serial_check else None

