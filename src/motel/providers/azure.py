"""Azure Managed Disks provider (standard library only). BETA: not verified against the live API.

Talks to Azure Resource Manager (ARM) with the token of the VM's managed identity, read from the Instance
Metadata Service (IMDS). Subscription, resource group, VM name and region all come from the IMDS compute
metadata, so the provider is scoped to the resource group of the VM it runs on and nothing else.

* A volume id is the managed disk NAME (unique inside the resource group). A server id is the VM name,
  lower-cased (ARM names are case-insensitive, and ``managedBy`` may come back in another case).
* Labels are native Azure tags.
* Attach and detach are a read-modify-write of the VM's ``storageProfile.dataDisks`` guarded by the VM's ETag
  (``If-Match``). A PATCH replaces the whole array, so a blind write could drop a disk somebody else just
  attached. On HTTP 412 the VM is read again and the change recomputed. A VM without an ETag is never written.
* The ARM token goes only to the ARM host. Redirects are refused, and every ``nextLink`` and operation URL is
  checked to stay on the ARM host before it is followed. IMDS is called without proxies.
* A write whose response is lost raises ``ProviderError(indeterminate=True)`` and is never retried blindly
  (only a 429, a request the service rejected, is).

Sources (checked 2026-11-09) are listed in docs/providers/azure.md.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime
from decimal import Decimal, InvalidOperation

from motel import __version__
from motel.errors import InventoryIncomplete, NotFound, ProviderError
from motel.providers.base import HostIdentity, Inventory, Volume

ARM_BASE = "https://management.azure.com"
IMDS_BASE = "http://169.254.169.254"
RETAIL_BASE = "https://prices.azure.com"
COMPUTE_API = "2025-04-01"  # Microsoft.Compute/virtualMachines (has etag, If-Match, diskControllerType)
DISK_API = "2025-01-02"  # Microsoft.Compute/disks
IMDS_API = "2025-04-07"
TOKEN_API = "2018-02-01"
MAX_PAGES = 200
MAX_LUN = 64
MIN_GB, MAX_GB = 1, 32767
TIER_GB = (4, 8, 16, 32, 64, 128, 256, 512, 1024, 2048, 4096, 8192, 16384, 32767)

SUB_RE = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
RG_RE = re.compile(r"^[A-Za-z0-9_().-]{1,90}$")
VM_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
DISK_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,79}$")
REGION_RE = re.compile(r"^[a-z0-9]{2,40}$")
TAG_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,126}$")

# retail-price lookup per disk sku: (productName, skuName, meterName, GiB of the reference tier)
PRICE_KEYS = {
    "StandardSSD_LRS": ("Standard SSD Managed Disks", "E10 LRS", "E10 LRS Disk", 128),
    "StandardSSD_ZRS": ("Standard SSD Managed Disks", "E10 ZRS", "E10 ZRS Disk", 128),
    "Premium_LRS": ("Premium SSD Managed Disks", "P10 LRS", "P10 LRS Disk", 128),
    "Standard_LRS": ("Standard HDD Managed Disks", "S10 LRS", "S10 LRS Disk", 128),
}


class PreconditionFailed(ProviderError):
    """HTTP 412: the If-Match ETag no longer matches (somebody changed the VM)."""


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def _default_open(req, timeout):
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


def _imds_open(req, timeout):
    return urllib.request.build_opener(urllib.request.ProxyHandler({}), _NoRedirect).open(req, timeout=timeout)


class _Resp:
    def __init__(self, status: int, headers: dict, doc):
        self.status, self.headers, self.doc = status, headers, doc


def _parse_time(text):
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def _disk_name(value) -> str:
    text = str(value)
    if not DISK_RE.fullmatch(text):
        raise ProviderError(f"refusing odd disk name {text!r}")
    return text


def labels_to_tags(labels: dict) -> dict:
    tags = {}
    for key, value in labels.items():
        key, value = str(key), str(value)
        if not TAG_KEY_RE.fullmatch(key):
            raise ProviderError(f"label key {key!r} cannot be stored as an Azure tag")
        if len(value) > 256 or any(c in value for c in "\r\n\x00"):
            raise ProviderError(f"label {key} has a value that cannot be stored as an Azure tag")
        tags[key] = value
    if len(tags) > 50:
        raise ProviderError("an Azure resource holds at most 50 tags")
    return tags


def billable_gb(size_gb: int) -> int:
    """The managed-disk tier a size is billed at (Azure bills the smallest tier that fits, not the exact size)."""
    return next((t for t in TIER_GB if t >= int(size_gb)), int(size_gb))


class AzureProvider:
    name = "azure"
    currency = "EUR"
    formats_on_create = False  # raw disks: the desk makes the filesystem on a proven-blank device
    gb_bytes = 2 ** 30  # diskSizeGB is GiB

    def __init__(self, *, base: str = ARM_BASE, imds_base: str = IMDS_BASE, retail_base: str = RETAIL_BASE,
                 identity: dict | None = None, sku: str = "StandardSSD_LRS", deny_export: bool = True,
                 timeout: float = 30.0, sleep=time.sleep, monotonic=time.monotonic, clock=time.time,
                 opener=_default_open, imds_opener=_imds_open, wait_timeout: float = 900.0):
        """``identity`` selects a user-assigned managed identity: ``{"client_id": ...}`` (or object_id / mi_res_id)."""
        self.base, self.imds_base, self.retail_base = base.rstrip("/"), imds_base.rstrip("/"), retail_base.rstrip("/")
        self.sku, self.deny_export, self.identity_hint = sku, deny_export, dict(identity or {})
        self.timeout, self.sleep, self.monotonic, self.clock = timeout, sleep, monotonic, clock
        self._open, self._imds_open, self.wait_timeout = opener, imds_opener, wait_timeout
        self._ident, self._tok, self._tok_exp, self._luns = None, None, 0.0, {}

    # -- IMDS
    def _imds(self, path: str, query: dict) -> dict:
        url = f"{self.imds_base}{path}?{urllib.parse.urlencode(query)}"
        delay = 2.0
        for attempt in range(5):
            req = urllib.request.Request(url, headers={"Metadata": "true", "User-Agent": f"motel/{__version__}"})
            try:
                with self._imds_open(req, 5.0) as resp:
                    return json.loads(resp.read())
            except urllib.error.HTTPError as exc:
                exc.close()
                if exc.code in (404, 410, 429, 500, 502, 503, 504) and attempt < 4:
                    self.sleep(delay)
                    delay *= 2
                    continue
                raise ProviderError(f"Azure instance metadata {path}: HTTP {exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if attempt < 4:
                    self.sleep(delay)
                    delay *= 2
                    continue
                raise ProviderError(f"cannot read the Azure instance metadata service (is this an Azure VM?): {exc}") from exc
        raise ProviderError("Azure instance metadata: retries exhausted")

    def _compute(self) -> dict:
        return self._imds("/metadata/instance/compute", {"api-version": IMDS_API, "format": "json"})

    def _identity(self) -> dict:
        if self._ident is None:
            doc = self._compute()
            sub, rg, vm = str(doc.get("subscriptionId", "")), str(doc.get("resourceGroupName", "")), str(doc.get("name", ""))
            region = str(doc.get("location", "")).lower()
            if not (SUB_RE.fullmatch(sub) and RG_RE.fullmatch(rg) and VM_RE.fullmatch(vm) and REGION_RE.fullmatch(region)):
                raise ProviderError("the Azure instance metadata service returned an unexpected subscription, group, name or region")
            self._ident = {"sub": sub.lower(), "rg": rg, "vm": vm.lower(), "region": region, "zone": str(doc.get("zone") or "")}
        return self._ident

    def whoami(self) -> HostIdentity:
        ident = self._identity()
        return HostIdentity(ident["vm"], ident["region"], "azure")

    def _token(self) -> str:
        if self._tok is None or self.clock() > self._tok_exp - 300:
            query = {"api-version": TOKEN_API, "resource": ARM_BASE + "/", **self.identity_hint}
            doc = self._imds("/metadata/identity/oauth2/token", query)
            token = doc.get("access_token")
            try:
                expires = float(doc.get("expires_on"))
            except (TypeError, ValueError):
                expires = self.clock() + 600
            if not isinstance(token, str) or not token:
                raise ProviderError("the managed identity returned no token (is a managed identity assigned to this VM?)")
            self._tok, self._tok_exp = token, expires
        return self._tok

    # -- ARM
    def _scope(self) -> str:
        ident = self._identity()
        return f"/subscriptions/{ident['sub']}/resourceGroups/{ident['rg']}/providers/Microsoft.Compute"

    def _url(self, kind: str, name: str | None, api: str) -> str:
        path = f"{self._scope()}/{kind}" + ("" if name is None else f"/{name}")
        return f"{self.base}{path}?api-version={api}"

    def _on_arm(self, url: str) -> str:
        if not (url == self.base or url.startswith(self.base + "/")):
            raise ProviderError("refusing to follow a link that leaves the Azure Resource Manager host")
        return url

    def _send(self, method: str, url: str, body=None, *, headers=None, auth: bool = True) -> _Resp:
        """GET retries on 429/5xx/network errors; writes retry only on 429. 412 raises PreconditionFailed."""
        data = None if body is None else json.dumps(body).encode()
        is_read = method == "GET"
        delay = 2.0
        short = url.split("?")[0].replace(self.base, "")
        for attempt in range(5):
            hdrs = {"Content-Type": "application/json", "User-Agent": f"motel/{__version__}", **(headers or {})}
            if auth:
                hdrs["Authorization"] = f"Bearer {self._token()}"
            req = urllib.request.Request(url, data=data, method=method, headers=hdrs)
            try:
                with self._open(req, self.timeout) as resp:
                    raw = resp.read()
                    return _Resp(resp.status, {k.lower(): v for k, v in resp.headers.items()}, json.loads(raw) if raw else {})
            except urllib.error.HTTPError as exc:
                raw, retry_after = exc.read(), (exc.headers.get("Retry-After") if exc.headers else None)
                exc.close()
                try:
                    err = (json.loads(raw) or {}).get("error") or {}
                except (ValueError, AttributeError):
                    err = {}
                if not isinstance(err, dict):
                    err = {"message": str(err)}
                transient = exc.code in (429, 500, 502, 503, 504)
                if transient and attempt < 4 and (is_read or exc.code == 429):
                    self.sleep(min(float(retry_after), 60.0) if retry_after and retry_after.isdigit() else delay)
                    delay *= 2
                    continue
                if exc.code == 404:
                    raise NotFound(f"{method} {short}: not found") from exc
                message = f"Azure API {method} {short}: HTTP {exc.code} {err.get('code', '')} {str(err.get('message', ''))[:200]}"
                if exc.code == 412:
                    raise PreconditionFailed(message.strip()) from exc
                if exc.code in (401, 403):
                    message += " (check the managed identity's role assignments)"
                raise ProviderError(message.replace("  ", " ").strip(), indeterminate=(not is_read and exc.code >= 500)) from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if is_read and attempt < 4:
                    self.sleep(delay)
                    delay *= 2
                    continue
                raise ProviderError(f"Azure API {method} {short}: {exc}", indeterminate=not is_read) from exc
        raise ProviderError(f"Azure API {method} {short}: retries exhausted", indeterminate=not is_read)

    def _get(self, url: str) -> dict:
        return self._send("GET", self._on_arm(url)).doc

    def _finish(self, resp: _Resp, what: str) -> None:
        """Follow an ARM long-running operation (Azure-AsyncOperation, else Location) to its end."""
        op, loc = resp.headers.get("azure-asyncoperation"), resp.headers.get("location")
        if not op and not (loc and resp.status == 202):
            return
        poll, deadline, wait = self._on_arm(op or loc), self.monotonic() + self.wait_timeout, resp.headers.get("retry-after")
        while True:
            self.sleep(min(max(float(wait), 1.0), 30.0) if wait and wait.isdigit() else 5.0)
            if self.monotonic() > deadline:
                raise ProviderError(f"Azure operation ({what}) did not finish in time", indeterminate=True)
            try:
                step = self._send("GET", poll)
            except NotFound as exc:
                raise ProviderError(f"Azure operation ({what}) disappeared before it reported a result", indeterminate=True) from exc
            wait = step.headers.get("retry-after")
            if op:
                status = str((step.doc or {}).get("status", ""))
                if status == "Succeeded":
                    return
                if status in ("Failed", "Canceled"):
                    err = (step.doc or {}).get("error") or {}
                    raise ProviderError(f"Azure operation ({what}) {status.lower()}: {err.get('code', '')} {str(err.get('message', ''))[:200]}".strip())
            elif step.status != 202:
                return

    def _list(self, url: str, key: str) -> list:
        out, seen = [], 0
        while url:
            seen += 1
            if seen > MAX_PAGES:
                raise InventoryIncomplete(f"the {key} list has too many pages", "motel doctor")
            try:
                doc = self._get(url)
            except (ProviderError, NotFound) as exc:
                raise InventoryIncomplete(f"cannot read the full {key} list: {getattr(exc, 'message', exc)}", "motel doctor") from exc
            out += doc.get("value") or []
            url = doc.get("nextLink")
        return out

    # -- reads
    def _vm_name_of(self, managed_by) -> str | None:
        """Name of the VM in ``managedBy`` if it is in our group, else the full lower-cased id (a foreign holder)."""
        if not managed_by:
            return None
        text = str(managed_by).lower()
        ident = self._identity()
        prefix = f"/subscriptions/{ident['sub']}/resourcegroups/{ident['rg'].lower()}/providers/microsoft.compute/virtualmachines/"
        return text[len(prefix):] if text.startswith(prefix) and "/" not in text[len(prefix):] else text

    def _parse(self, doc: dict) -> Volume:
        props = doc.get("properties") or {}
        return Volume(id=str(doc["name"]), name=str(doc["name"]), size_gb=int(props.get("diskSizeGB") or 0),
                      server_id=self._vm_name_of(doc.get("managedBy")), labels=dict(doc.get("tags") or {}),
                      delete_protected=False, created=_parse_time(props.get("timeCreated")),
                      location=str(doc.get("location", "")).lower() or None)

    def visible(self) -> Inventory:
        disks = self._list(self._url("disks", None, DISK_API), "disks")
        vms = self._list(self._url("virtualMachines", None, COMPUTE_API), "virtual machines")
        return Inventory(volumes=tuple(self._parse(d) for d in disks), servers=tuple(str(v["name"]).lower() for v in vms), complete=True)

    def billable_gb(self, size_gb: int) -> int:
        return billable_gb(size_gb)

    def price_gb_month(self, location: str) -> Decimal:
        """Euro per GiB-month, from Azure's public retail-price API for the 128 GiB tier of the configured sku.

        Azure bills the smallest tier that fits the disk. Standard SSD LRS tiers are linear in GiB, so the 128 GiB
        price over 128 is a good rate; Premium and HDD are close to linear. See ``billable_gb`` for the rounding.
        """
        keys = PRICE_KEYS.get(self.sku)
        region = str(location).lower()
        if keys is None or not REGION_RE.fullmatch(region):
            raise ProviderError(f"no price lookup for disk sku {self.sku} in {location}",
                                "set rooms.price_gb_month_eur in /etc/motel/motel.toml")
        product, sku, meter, gib = keys
        flt = (f"serviceName eq 'Storage' and armRegionName eq '{region}' and priceType eq 'Consumption' "
               f"and productName eq '{product}' and skuName eq '{sku}' and meterName eq '{meter}'")
        url = f"{self.retail_base}/api/retail/prices?" + urllib.parse.urlencode({"currencyCode": "'EUR'", "$filter": flt})
        try:
            doc = self._send("GET", url, auth=False).doc
            item = next(i for i in doc.get("Items") or [] if i.get("meterName") == meter and i.get("currencyCode") == "EUR")
            monthly = Decimal(str(item["retailPrice"]))
            if monthly <= 0:
                raise ValueError("not a positive price")
        except (StopIteration, KeyError, TypeError, ValueError, InvalidOperation, ProviderError) as exc:
            raise ProviderError(f"cannot read the {self.sku} price for {location} from the Azure retail price API",
                                "set rooms.price_gb_month_eur in /etc/motel/motel.toml") from exc
        return (monthly / gib).quantize(Decimal("0.000001"))

    def get_volume(self, vol_id) -> Volume:
        return self._parse(self._get(self._url("disks", _disk_name(vol_id), DISK_API)))

    def list_owned(self, labels: dict) -> list:
        found = self._list(self._url("disks", None, DISK_API), "disks")
        out = [self._parse(d) for d in found]
        return [v for v in out if all(v.labels.get(k) == str(val) for k, val in labels.items())]

    # -- writes
    def create_volume(self, name, size_gb, location, labels, server_id=None) -> Volume:
        ident = self._identity()
        name, size_gb, region = _disk_name(name), int(size_gb), str(location).lower()
        if region != ident["region"]:
            raise ProviderError(f"this VM is in {ident['region']}; a disk in {location} could not be attached")
        if not MIN_GB <= size_gb <= MAX_GB:
            raise ProviderError(f"Azure managed disks are {MIN_GB} to {MAX_GB} GiB, not {size_gb}")
        zone = ident["zone"]
        if server_id is not None:
            vm = self._get_vm(str(server_id))[0]
            zone = (vm.get("zones") or [""])[0]
        props = {"creationData": {"createOption": "Empty"}, "diskSizeGB": size_gb}
        if self.deny_export:
            props["networkAccessPolicy"] = "DenyAll"
        body = {"location": region, "sku": {"name": self.sku}, "tags": labels_to_tags(labels), "properties": props}
        if zone and not self.sku.endswith("_ZRS"):
            body["zones"] = [zone]
        self._finish(self._send("PUT", self._url("disks", name, DISK_API), body), f"create disk {name}")
        vol = self.get_volume(name)
        if server_id is not None:
            try:
                self.attach(name, server_id)
            except ProviderError as exc:
                raise ProviderError(f"disk {name} was created but is not attached: {exc.message}", indeterminate=True) from exc
            vol = self.get_volume(name)
        return vol

    def _get_vm(self, name: str) -> tuple:
        if not VM_RE.fullmatch(str(name)):
            raise ProviderError(f"refusing odd server name {name!r}")
        resp = self._send("GET", self._url("virtualMachines", str(name).lower(), COMPUTE_API))
        etag = resp.doc.get("etag") or resp.headers.get("etag")
        return resp.doc, etag

    @staticmethod
    def _entry_for(disks: list, name: str):
        want = name.lower()
        for entry in disks:
            mid = str(((entry.get("managedDisk") or {}).get("id")) or "").lower()
            if str(entry.get("name", "")).lower() == want or mid.endswith("/disks/" + want):
                return entry
        return None

    def _patch_vm(self, vm_name: str, etag: str, disks: list, what: str) -> None:
        body = {"properties": {"storageProfile": {"dataDisks": disks}}}
        resp = self._send("PATCH", self._url("virtualMachines", vm_name, COMPUTE_API), body, headers={"If-Match": etag})
        self._finish(resp, what)

    def attach(self, vol_id, server_id) -> None:
        name, vm_name = _disk_name(vol_id), str(server_id).lower()
        for _ in range(6):
            vm, etag = self._get_vm(vm_name)
            if not etag:
                raise ProviderError("Azure returned no ETag for the VM; refusing to rewrite its disk list without one")
            disks = list((((vm.get("properties") or {}).get("storageProfile")) or {}).get("dataDisks") or [])
            existing = self._entry_for(disks, name)
            if existing is not None:
                if existing.get("toBeDetached"):
                    raise ProviderError(f"disk {name} is being detached from {vm_name}; try again once that finishes")
                self._luns[name] = int(existing["lun"])
                return
            disk = self._get(self._url("disks", name, DISK_API))
            holder = self._vm_name_of(disk.get("managedBy"))
            if holder not in (None, vm_name):
                raise ProviderError(f"disk {name} is already held by {holder}")
            dz, vz = disk.get("zones") or [], vm.get("zones") or []
            if dz and vz and dz[0] != vz[0]:
                raise ProviderError(f"disk {name} is in zone {dz[0]} but {vm_name} is in zone {vz[0]}")
            used = {int(d["lun"]) for d in disks if d.get("lun") is not None}
            lun = next((n for n in range(MAX_LUN) if n not in used), None)
            if lun is None:
                raise ProviderError(f"{vm_name} has no free LUN for another data disk")
            entry = {"lun": lun, "name": name, "createOption": "Attach", "caching": "None", "deleteOption": "Detach",
                     "managedDisk": {"id": disk["id"]}}
            try:
                self._patch_vm(vm_name, etag, disks + [entry], f"attach {name}")
            except PreconditionFailed:
                continue  # the VM changed since we read it: read it again and recompute the LUN
            self._verify_attached(name, vm_name, lun)
            return
        raise ProviderError(f"{vm_name} kept changing while attaching {name}; try again")

    def _verify_attached(self, name: str, vm_name: str, lun: int) -> None:
        vm = self._get_vm(vm_name)[0]
        entry = self._entry_for(list((((vm.get("properties") or {}).get("storageProfile")) or {}).get("dataDisks") or []), name)
        if entry is None or int(entry["lun"]) != lun:
            raise ProviderError(f"disk {name} is not on {vm_name} at LUN {lun} after the attach", indeterminate=True)
        self._luns[name] = lun

    def detach(self, vol_id) -> None:
        name = _disk_name(vol_id)
        holder = self._vm_name_of(self._get(self._url("disks", name, DISK_API)).get("managedBy"))
        if holder is None:
            self._luns.pop(name, None)
            return
        if not VM_RE.fullmatch(holder):
            raise ProviderError(f"disk {name} is held by {holder}, which motel cannot detach it from")
        for _ in range(6):
            vm, etag = self._get_vm(holder)
            if not etag:
                raise ProviderError("Azure returned no ETag for the VM; refusing to rewrite its disk list without one")
            disks = list((((vm.get("properties") or {}).get("storageProfile")) or {}).get("dataDisks") or [])
            existing = self._entry_for(disks, name)
            if existing is None:
                raise ProviderError(f"disk {name} says it is held by {holder} but {holder} lists no such data disk")
            try:
                self._patch_vm(holder, etag, [d for d in disks if d is not existing], f"detach {name}")
            except PreconditionFailed:
                continue
            self._luns.pop(name, None)
            if self._parse(self._get(self._url("disks", name, DISK_API))).server_id is not None:
                raise ProviderError(f"disk {name} still reports an attachment after the detach", indeterminate=True)
            return
        raise ProviderError(f"{holder} kept changing while detaching {name}; try again")

    def resize(self, vol_id, size_gb) -> None:
        name, size_gb = _disk_name(vol_id), int(size_gb)
        current = self.get_volume(name).size_gb
        if size_gb <= current:
            raise ProviderError(f"size {size_gb} GiB is not larger than {current} GiB (Azure disks only grow)")
        if size_gb > MAX_GB:
            raise ProviderError(f"Azure managed disks are at most {MAX_GB} GiB")
        self._finish(self._send("PATCH", self._url("disks", name, DISK_API), {"properties": {"diskSizeGB": size_gb}}),
                     f"resize disk {name}")
        if self.get_volume(name).size_gb < size_gb:
            raise ProviderError(f"disk {name} did not reach {size_gb} GiB", indeterminate=True)

    def delete(self, vol_id) -> None:
        name = _disk_name(vol_id)
        self._finish(self._send("DELETE", self._url("disks", name, DISK_API)), f"delete disk {name}")

    # -- guest
    def device_path(self, vol_id) -> str:
        """``/dev/disk/azure/scsi1/lun<N>`` (SCSI) or ``/dev/disk/azure/data/by-lun/<N>`` (NVMe) for an attached disk.

        The LUN is read fresh from ARM, never from memory, and cross-checked against the VM's own IMDS view.
        """
        name, vm_name = _disk_name(vol_id), self._identity()["vm"]
        vm = self._get_vm(vm_name)[0]
        profile = ((vm.get("properties") or {}).get("storageProfile")) or {}
        entry = self._entry_for(list(profile.get("dataDisks") or []), name)
        if entry is None or entry.get("toBeDetached"):
            raise ProviderError(f"disk {name} is not attached to this VM")
        lun = int(entry["lun"])
        for seen in ((self._compute().get("storageProfile") or {}).get("dataDisks")) or []:
            if str(seen.get("name", "")).lower() == name.lower() and int(seen.get("lun", lun)) != lun:
                raise ProviderError(f"Azure disagrees with itself about the LUN of {name} ({lun} against {seen.get('lun')})")
        self._luns[name] = lun
        if str(profile.get("diskControllerType", "SCSI")) == "NVMe":
            return f"/dev/disk/azure/data/by-lun/{lun}"
        return f"/dev/disk/azure/scsi1/lun{lun}"

    def expected_serial(self, vol_id) -> None:
        return None  # Azure data disks have no serial the guest can compare; identity rests on the LUN link
