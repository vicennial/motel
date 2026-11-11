"""Linode (Akamai Cloud) provider (standard library only). BETA: not verified against the live API.

Linode volumes have a label and a list of tags but no key/value labels, so motel's labels are stored as tags
(see ``_common.encode_tags``; Linode limits a tag to 50 characters). A volume is created blank and attached to
the host in the same call; the desk formats it. See ``docs/providers/linode.md`` for the sources of every API
detail used here.
"""

from __future__ import annotations

import json
import re
import time
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

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

API_BASE = "https://api.linode.com/v4"
METADATA_BASE = "http://169.254.169.254/v1"
ID_RE = re.compile(r"^[0-9]{1,18}$")
LABEL_RE = re.compile(r"^[A-Za-z](?:(?!--|__)[A-Za-z0-9_-])+$")
REGION_RE = re.compile(r"^[a-z0-9-]{2,32}$")
PATH_PREFIX = "/dev/disk/by-id/scsi-0Linode_Volume_"
PATH_RE = re.compile(r"^/dev/disk/by-id/scsi-0Linode_Volume_[A-Za-z0-9_-]{1,32}$")
MAX_TAG = 50
MAX_LABEL = 32
PAGE_SIZE = 100
# USD per GB-month, https://www.akamai.com/cloud/pricing/block-storage (volumes "start at $0.10/GB per month";
# the live price comes from GET /volumes/types and this table is the fallback).
PRICE_USD_GB_MONTH = Decimal("0.10")
CURRENCY = "USD"


def _describe(raw: bytes) -> str:
    try:
        errors = (json.loads(raw) or {}).get("errors") or []
        return short("; ".join(f"{e.get('field') or ''} {e.get('reason', '')}".strip() for e in errors[:3]))
    except (ValueError, AttributeError, TypeError):
        return ""


def _check_id(value, what: str = "volume") -> str:
    text = str(value)
    if not ID_RE.fullmatch(text):
        raise ProviderError(f"refusing odd {what} id {text!r}")
    return text


def _parse_time(text):
    try:
        dt = datetime.fromisoformat(text.replace("Z", "+00:00")) if text else None
    except (ValueError, AttributeError):
        return None
    return dt.replace(tzinfo=UTC) if dt is not None and dt.tzinfo is None else dt  # Linode times are UTC


def _locked(doc: dict) -> bool:
    """True when the (v4beta-only) ``locks`` field carries a delete lock; the v4 API omits it."""
    for lock in doc.get("locks") or []:
        kind = lock.get("lock_type") or lock.get("type") if isinstance(lock, dict) else lock
        if isinstance(kind, str) and kind.startswith("cannot_delete"):
            return True
    return False


def parse_volume(doc: dict) -> Volume:
    linode = doc.get("linode_id")
    return Volume(id=str(doc["id"]), name=str(doc.get("label", "")), size_gb=int(doc["size"]),
                  server_id=None if linode is None else str(linode), labels=decode_tags(doc.get("tags")),
                  delete_protected=_locked(doc), created=_parse_time(doc.get("created")), location=doc.get("region"))


class LinodeProvider:
    name = "linode"
    currency = CURRENCY
    formats_on_create = False  # the API cannot format a volume; the desk runs mkfs on a blank, verified device

    def __init__(self, token: str, *, base: str = API_BASE, timeout: float = 30.0, sleep=time.sleep,
                 monotonic=time.monotonic, opener=default_open, metadata_fetch=None,
                 eur_per_usd: Decimal = Decimal("1"), serial_check: bool = True):
        self.sleep, self.monotonic = sleep, monotonic
        self.api = ApiClient("Linode", token, base, _describe, timeout=timeout, sleep=sleep, opener=opener)
        self._metadata = metadata_fetch or self._fetch_metadata
        self.eur_per_usd, self.serial_check = Decimal(eur_per_usd), serial_check

    def request(self, method, path, body=None):
        return self.api.request(method, path, body)

    # -- paging and waiting
    def pages(self, path: str, what: str) -> list:
        """All items of a paginated listing; raises InventoryIncomplete if the listing cannot be completed."""
        out, page = [], 1
        sep = "&" if "?" in path else "?"
        for _ in range(MAX_PAGES):
            try:
                doc = self.request("GET", f"{path}{sep}page_size={PAGE_SIZE}&page={page}")
            except ProviderError as exc:
                raise InventoryIncomplete(f"cannot read the full {what} list: {exc.message}", "motel doctor") from exc
            out += doc.get("data") or []
            pages = doc.get("pages")
            if isinstance(pages, int) and page < pages:
                page += 1
                continue
            total = doc.get("results")
            if isinstance(total, int) and len(out) < total:
                raise InventoryIncomplete(f"the {what} list ended early ({len(out)} of {total})", "motel doctor")
            return out
        raise InventoryIncomplete(f"the {what} list has too many pages", "motel doctor")

    def wait_for(self, vol_id: str, done, what: str, timeout: float = 240.0) -> dict:
        """Poll the volume until ``done(doc)`` is true. Linode volume changes finish on the volume itself."""
        deadline = self.monotonic() + timeout
        while True:
            doc = self.request("GET", f"/volumes/{vol_id}")
            if done(doc):
                return doc
            if self.monotonic() > deadline:
                raise ProviderError(f"Linode volume {vol_id}: {what} timed out (status {doc.get('status')})",
                                    indeterminate=True)
            self.sleep(2.0)

    # -- metadata
    @staticmethod
    def _fetch_metadata(path: str) -> str:
        """The Metadata service needs a short-lived token first (PUT /v1/token), then ``Metadata-Token``."""
        token = fetch_text(f"{METADATA_BASE}/token", method="PUT", headers={"Metadata-Token-Expiry-Seconds": "300"})
        return fetch_text(f"{METADATA_BASE}/{path}", headers={"Metadata-Token": token, "Accept": "application/json"})

    def whoami(self) -> HostIdentity:
        try:
            doc = json.loads(self._metadata("instance"))
            server_id, region = str(doc["id"]), str(doc["region"])
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("cannot read this Linode's id from the Linode Metadata service "
                                f"(is this a Linode in a region that has it?): {exc}") from exc
        if not ID_RE.fullmatch(server_id):
            raise ProviderError(f"unexpected Linode id {server_id!r} from the metadata service")
        if not REGION_RE.fullmatch(region):
            raise ProviderError(f"unexpected region {region!r} from the metadata service")
        return HostIdentity(server_id, region, "linode")

    # -- reads
    def visible(self) -> Inventory:
        servers = self.pages("/linode/instances", "Linode")
        volumes = self.pages("/volumes", "volume")
        return Inventory(volumes=tuple(parse_volume(v) for v in volumes),
                         servers=tuple(str(s["id"]) for s in servers), complete=True)

    def price_gb_month(self, location: str) -> Decimal:
        """Euro price of one GB-month. The live price (per region) is read from ``/volumes/types``; the table is
        the fallback when that call fails. The result is the USD price times ``eur_per_usd``."""
        usd = PRICE_USD_GB_MONTH
        try:
            for kind in self.request("GET", "/volumes/types").get("data") or []:
                for row in kind.get("region_prices") or []:
                    if row.get("id") == location:
                        return to_eur(Decimal(str(row["monthly"])), self.eur_per_usd)
                if kind.get("price"):
                    usd = Decimal(str(kind["price"]["monthly"]))
                    break
        except (ProviderError, KeyError, TypeError, ValueError, InvalidOperation, ArithmeticError):
            usd = PRICE_USD_GB_MONTH
        return to_eur(usd, self.eur_per_usd)

    def get_volume(self, vol_id) -> Volume:
        return parse_volume(self.request("GET", f"/volumes/{_check_id(vol_id)}"))

    def list_owned(self, labels: dict) -> list:
        # Tags are matched here over the full listing (the X-Filter semantics for tag arrays are not documented).
        vols = [parse_volume(v) for v in self.pages("/volumes", "volume")]
        return [v for v in vols if matches(v.labels, labels)]

    # -- writes
    def create_volume(self, name, size_gb, location, labels, server_id=None) -> Volume:
        if len(name) > MAX_LABEL or not LABEL_RE.fullmatch(name):
            raise ProviderError(f"volume name {name!r} is not a valid Linode label (letters, digits, - and _, "
                                "starting with a letter, no -- or __, at most 32 characters)")
        body = {"label": name, "size": int(size_gb), "tags": encode_tags(labels, MAX_TAG)}
        if server_id is not None:
            body["linode_id"] = int(_check_id(server_id, "Linode"))  # the volume lands in that Linode's region
        else:
            if not REGION_RE.fullmatch(str(location)):
                raise ProviderError(f"odd region {location!r}")
            body["region"] = location
        doc = self.request("POST", "/volumes", body)
        vid = _check_id(doc["id"])
        try:
            done = self.wait_for(vid, lambda d: d.get("status") == "active" and (server_id is None or d.get("io_ready")),
                                 "creation")
        except ProviderError as exc:
            raise ProviderError(f"volume {vid} was created but is not ready: {exc.message}", exc.hint,
                                indeterminate=True) from exc
        vol = parse_volume(done)
        if location and vol.location and vol.location != location:
            raise ProviderError(f"volume {vid} was created in {vol.location}, not {location}", indeterminate=True)
        return vol

    def attach(self, vol_id, server_id) -> None:
        vid = _check_id(vol_id)
        sid = _check_id(server_id, "Linode")
        self.request("POST", f"/volumes/{vid}/attach", {"linode_id": int(sid)})
        self.wait_for(vid, lambda d: str(d.get("linode_id")) == sid and bool(d.get("io_ready")), "attach")

    def detach(self, vol_id) -> None:
        vid = _check_id(vol_id)
        if self.get_volume(vid).server_id is None:
            return  # already detached
        self.request("POST", f"/volumes/{vid}/detach", {})
        self.wait_for(vid, lambda d: d.get("linode_id") is None, "detach")

    def resize(self, vol_id, size_gb) -> None:
        vid = _check_id(vol_id)
        current = self.get_volume(vid)
        if int(size_gb) <= current.size_gb:
            raise ProviderError(f"Linode volumes only grow: {size_gb} GB is not larger than {current.size_gb} GB")
        self.request("POST", f"/volumes/{vid}/resize", {"size": int(size_gb)})
        self.wait_for(vid, lambda d: d.get("status") == "active" and int(d.get("size", 0)) == int(size_gb), "resize")

    def delete(self, vol_id) -> None:
        self.request("DELETE", f"/volumes/{_check_id(vol_id)}")

    # -- guest device
    def _by_id_path(self, vol_id) -> str:
        """The API reports the guest path itself (``filesystem_path``); it is checked and otherwise built from the label."""
        doc = self.request("GET", f"/volumes/{_check_id(vol_id)}")
        path = doc.get("filesystem_path")
        if isinstance(path, str) and PATH_RE.fullmatch(path):
            return path
        label = str(doc.get("label", ""))
        if not LABEL_RE.fullmatch(label):
            raise ProviderError(f"volume {vol_id} has an unexpected label {label!r}")
        return PATH_PREFIX + label

    def device_path(self, vol_id) -> str:
        return self._by_id_path(vol_id)

    def expected_serial(self, vol_id) -> str | None:
        """The SCSI serial udev turns into the by-id link is the volume label (check on first live use)."""
        return self._by_id_path(vol_id)[len(PATH_PREFIX):] if self.serial_check else None
