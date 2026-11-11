"""DigitalOcean provider (standard library only). BETA: not verified against the live API.

DigitalOcean Block Storage volumes have a name and a list of free-form tags but no key/value labels, so motel's
labels are stored as tags (see ``_common.encode_tags``). A volume is created blank and attached with a second
call; the desk formats it. See ``docs/providers/digitalocean.md`` for the sources of every API detail used here.
"""

from __future__ import annotations

import json
import re
import time
from datetime import datetime
from decimal import Decimal

from motel.errors import InventoryIncomplete, ProviderError
from motel.providers._common import (
    MAX_PAGES,
    ApiClient,
    decode_tags,
    default_open,
    encode_tags,
    fetch_text,
    matches,
    short,
    to_eur,
)
from motel.providers.base import HostIdentity, Inventory, Volume

API_BASE = "https://api.digitalocean.com/v2"
METADATA_BASE = "http://169.254.169.254/metadata/v1"
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
SERVER_RE = re.compile(r"^[0-9]{1,18}$")
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,63}$")
REGION_RE = re.compile(r"^[a-z0-9]{2,12}$")
MAX_TAG = 255
PER_PAGE = 200
# USD per GiB-month, https://docs.digitalocean.com/products/volumes/details/pricing/ ("last verified 26 Oct 2026").
PRICE_USD_GB_MONTH = Decimal("0.10")
CURRENCY = "USD"


def _describe(raw: bytes) -> str:
    try:
        doc = json.loads(raw) or {}
        return short(f"{doc.get('id', '')} {doc.get('message', '')}")
    except (ValueError, AttributeError):
        return ""


def _check_volume_id(vol_id) -> str:
    text = str(vol_id).lower()
    if not UUID_RE.fullmatch(text):
        raise ProviderError(f"refusing odd volume id {str(vol_id)!r}")
    return text


def _check_server_id(server_id) -> str:
    text = str(server_id)
    if not SERVER_RE.fullmatch(text):
        raise ProviderError(f"refusing odd droplet id {text!r}")
    return text


def _parse_time(text):
    try:
        return datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None
    except (ValueError, AttributeError):
        return None


def parse_volume(doc: dict) -> Volume:
    droplets = sorted(str(i) for i in (doc.get("droplet_ids") or []))
    return Volume(id=str(doc["id"]).lower(), name=str(doc.get("name", "")), size_gb=int(doc["size_gigabytes"]),
                  server_id=",".join(droplets) if droplets else None,  # more than one id can never equal this host
                  labels=decode_tags(doc.get("tags")), delete_protected=False,
                  created=_parse_time(doc.get("created_at")), location=(doc.get("region") or {}).get("slug"))


class DigitalOceanProvider:
    name = "digitalocean"
    currency = CURRENCY  # prices in the table are in this currency; price_gb_month converts to euro
    formats_on_create = False  # the API can format, but DigitalOcean then auto-mounts the volume; motel formats it
    gb_bytes = 2 ** 30  # sizes are GiB

    def __init__(self, token: str, *, base: str = API_BASE, timeout: float = 30.0, sleep=time.sleep,
                 monotonic=time.monotonic, opener=default_open, metadata_fetch=None,
                 eur_per_usd: Decimal = Decimal("1"), serial_check: bool = True):
        self.sleep, self.monotonic = sleep, monotonic
        self.api = ApiClient("DigitalOcean", token, base, _describe, timeout=timeout, sleep=sleep, opener=opener)
        self._metadata = metadata_fetch or self._fetch_metadata
        self.eur_per_usd, self.serial_check = Decimal(eur_per_usd), serial_check
        self._names: dict = {}  # volume names cannot be changed, so they are cached

    def request(self, method, path, body=None):
        return self.api.request(method, path, body)

    # -- paging
    def pages(self, path: str, key: str) -> list:
        """All items of a paginated listing; raises InventoryIncomplete if the listing cannot be completed."""
        out, page = [], 1
        sep = "&" if "?" in path else "?"
        for _ in range(MAX_PAGES):
            try:
                doc = self.request("GET", f"{path}{sep}per_page={PER_PAGE}&page={page}")
            except ProviderError as exc:
                raise InventoryIncomplete(f"cannot read the full {key} list: {exc.message}", "motel doctor") from exc
            out += doc.get(key) or []
            if ((doc.get("links") or {}).get("pages") or {}).get("next"):
                page += 1
                continue
            total = (doc.get("meta") or {}).get("total")
            if isinstance(total, int) and len(out) < total:
                raise InventoryIncomplete(f"the {key} list ended early ({len(out)} of {total})", "motel doctor")
            return out
        raise InventoryIncomplete(f"the {key} list has too many pages", "motel doctor")

    def wait(self, vol_id: str, action: dict, timeout: float = 240.0) -> None:
        """Poll a volume action until it is no longer in progress."""
        deadline = self.monotonic() + timeout
        current = action
        while current.get("status") == "in-progress":
            if self.monotonic() > deadline:
                raise ProviderError(f"DigitalOcean action {current.get('type')} {current.get('id')} timed out",
                                    indeterminate=True)
            self.sleep(2.0)
            path = f"/volumes/{vol_id}/actions/{int(current['id'])}"
            current = self.request("GET", path).get("action") or {}
        if current.get("status") != "completed":
            raise ProviderError(f"DigitalOcean action {current.get('type')} did not complete: {current.get('status')}")

    # -- metadata
    @staticmethod
    def _fetch_metadata(path: str) -> str:
        return fetch_text(f"{METADATA_BASE}/{path}")

    def whoami(self) -> HostIdentity:
        try:
            server_id = self._metadata("id")
            region = self._metadata("region")
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("cannot read this droplet's id from the DigitalOcean metadata service "
                                f"(is this a DigitalOcean Droplet?): {exc}") from exc
        if not SERVER_RE.fullmatch(server_id):
            raise ProviderError(f"unexpected droplet id {server_id!r} from the metadata service")
        if not REGION_RE.fullmatch(region):
            raise ProviderError(f"unexpected region {region!r} from the metadata service")
        return HostIdentity(server_id, region, "digitalocean")

    # -- reads
    def visible(self) -> Inventory:
        droplets = self.pages("/droplets", "droplets")
        volumes = self.pages("/volumes", "volumes")
        return Inventory(volumes=tuple(parse_volume(v) for v in volumes),
                         servers=tuple(str(d["id"]) for d in droplets), complete=True)

    def price_gb_month(self, location: str) -> Decimal:
        """Euro price of one GB-month: the USD table price times ``eur_per_usd`` (volumes cost the same everywhere)."""
        return to_eur(PRICE_USD_GB_MONTH, self.eur_per_usd)

    def get_volume(self, vol_id) -> Volume:
        vol = parse_volume(self.request("GET", f"/volumes/{_check_volume_id(vol_id)}")["volume"])
        self._names[vol.id] = vol.name
        return vol

    def list_owned(self, labels: dict) -> list:
        # The volumes endpoint filters only by name and region, so tags are matched here over the full listing.
        vols = [parse_volume(v) for v in self.pages("/volumes", "volumes")]
        return [v for v in vols if matches(v.labels, labels)]

    # -- writes
    def create_volume(self, name, size_gb, location, labels, server_id=None) -> Volume:
        if not NAME_RE.fullmatch(name):
            raise ProviderError(f"volume name {name!r} is not valid for DigitalOcean "
                                "(lower-case letters, digits and -, starting with a letter, at most 64 characters)")
        if not REGION_RE.fullmatch(str(location)):
            raise ProviderError(f"odd region {location!r}")
        body = {"name": name, "size_gigabytes": int(size_gb), "region": location, "tags": encode_tags(labels, MAX_TAG)}
        # No filesystem_type: a pre-formatted volume would be auto-mounted by DigitalOcean on attach.
        vol = parse_volume(self.request("POST", "/volumes", body)["volume"])
        self._names[vol.id] = vol.name
        if server_id is None:
            return vol
        try:
            self.attach(vol.id, server_id)
            return self.get_volume(vol.id)
        except ProviderError as exc:
            # The volume exists: report it as unresolved so the desk looks it up by label instead of giving up.
            raise ProviderError(f"volume {vol.id} was created but could not be attached: {exc.message}", exc.hint,
                                indeterminate=True) from exc

    def attach(self, vol_id, server_id) -> None:
        vid = _check_volume_id(vol_id)
        doc = self.request("POST", f"/volumes/{vid}/actions",
                           {"type": "attach", "droplet_id": int(_check_server_id(server_id))})
        self.wait(vid, doc.get("action") or {})

    def detach(self, vol_id) -> None:
        vid = _check_volume_id(vol_id)
        current = self.get_volume(vid)
        if current.server_id is None:
            return  # already detached
        if not SERVER_RE.fullmatch(current.server_id):
            raise ProviderError(f"volume {vid} is attached to {current.server_id!r}; refusing to detach it")
        doc = self.request("POST", f"/volumes/{vid}/actions", {"type": "detach", "droplet_id": int(current.server_id)})
        self.wait(vid, doc.get("action") or {})

    def resize(self, vol_id, size_gb) -> None:
        vid = _check_volume_id(vol_id)
        current = self.get_volume(vid)
        if int(size_gb) <= current.size_gb:
            raise ProviderError(f"DigitalOcean volumes only grow: {size_gb} GB is not larger than {current.size_gb} GB")
        body = {"type": "resize", "size_gigabytes": int(size_gb)}
        if current.location:
            body["region"] = current.location
        self.wait(vid, self.request("POST", f"/volumes/{vid}/actions", body).get("action") or {})

    def delete(self, vol_id) -> None:
        self.request("DELETE", f"/volumes/{_check_volume_id(vol_id)}")

    # -- guest device
    def _volume_name(self, vol_id) -> str:
        vid = _check_volume_id(vol_id)
        if vid not in self._names:
            self.get_volume(vid)
        name = self._names[vid]
        if not NAME_RE.fullmatch(name):
            raise ProviderError(f"volume {vid} has an unexpected name {name!r}")
        return name

    def device_path(self, vol_id) -> str:
        return f"/dev/disk/by-id/scsi-0DO_Volume_{self._volume_name(vol_id)}"

    def expected_serial(self, vol_id) -> str | None:
        """The SCSI serial that udev turns into the by-id link is the volume name (check on first live use)."""
        return self._volume_name(vol_id) if self.serial_check else None
