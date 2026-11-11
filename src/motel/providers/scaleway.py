"""Scaleway Block Storage provider (standard library only). BETA: not verified against the live API.

Volumes are Block Storage (SBS) volumes of the Block API (``/block/v1/zones/{zone}``), attached and detached
through the Instance API (``/instance/v1/zones/{zone}/servers/{id}/attach-volume``). Everything is scoped to the
zone and the project of the host this runs on, both read from the Instance metadata service.

The secret key goes only into the ``X-Auth-Token`` header of requests to the API host. Redirects are refused so
the header can never follow a redirect elsewhere. Writes that may have happened but whose response was lost raise
``ProviderError(indeterminate=True)`` and are never retried blindly (only a 429, a request the API rejected, is).

Labels are stored as Scaleway tags ``key=value`` (split on the first ``=``). A tag without ``=`` is not a label.
A key that appears twice with different values reads as a conflict and matches nothing, so a foreign tag can
never make a volume look like one of ours.

Sources (checked 2026-11-09), all listed in docs/providers/scaleway.md.
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

API_BASE = "https://api.scaleway.com"
METADATA_BASE = "http://169.254.42.42"
GB = 10**9  # the Block API counts bytes; its size granularity is 1 GB = 10^9 bytes
MIN_GB, MAX_GB = 5, 15000  # sbs_5k / sbs_15k: 5 GB to 15 TB
HOURS_PER_MONTH = Decimal(730)
PERF_IOPS = (5000, 15000)
UUID_RE = re.compile(r"^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$")
ZONE_RE = re.compile(r"^[a-z]{2}-[a-z]{3}-[1-9]$")
TAG_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,62}$")
MAX_PAGES = 200
PAGE_SIZE = 50
CONFLICT = "\x00conflict"  # label value for a key that carries two different values


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def _default_open(req, timeout):
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


def _check_id(value, what: str = "volume") -> str:
    text = str(value).lower()
    if not UUID_RE.fullmatch(text):
        raise ProviderError(f"refusing odd {what} id {str(value)!r}")
    return text


def _parse_time(text):
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def labels_to_tags(labels: dict) -> list:
    tags = []
    for key, value in sorted(labels.items()):
        key, value = str(key), str(value)
        if not TAG_KEY_RE.fullmatch(key):
            raise ProviderError(f"label key {key!r} cannot be stored as a Scaleway tag")
        if not value or len(value) > 120 or any(c in value for c in "\r\n\x00") or value != value.strip():
            raise ProviderError(f"label {key} has a value that cannot be stored as a Scaleway tag")
        tags.append(f"{key}={value}")
    return tags


def tags_to_labels(tags) -> dict:
    labels: dict = {}
    for tag in tags or []:
        key, sep, value = str(tag).partition("=")
        if not sep or not TAG_KEY_RE.fullmatch(key):
            continue
        labels[key] = CONFLICT if key in labels and labels[key] != value else value
    return labels


def _attachment(doc: dict):
    """Server id (or a foreign marker) the volume is referenced by, or None when nothing holds it."""
    held = []
    for ref in doc.get("references") or []:
        if ref.get("status") == "detached":
            continue
        kind, rid = ref.get("product_resource_type"), str(ref.get("product_resource_id") or "?")
        held.append(rid.lower() if kind == "instance_server" else f"{kind}:{rid}")
    if not held:
        return None
    return held[0] if len(set(held)) == 1 else "+".join(sorted(set(held)))


def parse_volume(doc: dict, zone: str | None = None) -> Volume:
    return Volume(id=str(doc["id"]).lower(), name=str(doc.get("name", "")), size_gb=int(doc["size"]) // GB,
                  server_id=_attachment(doc), labels=tags_to_labels(doc.get("tags")), delete_protected=False,
                  created=_parse_time(doc.get("created_at")), location=doc.get("zone") or zone)


class ScalewayProvider:
    name = "scaleway"
    currency = "EUR"
    formats_on_create = False  # raw volumes: the desk makes the filesystem on a proven-blank device
    gb_bytes = GB  # decimal gigabytes: sizes in the guest are multiples of 10^9 bytes

    def __init__(self, secret_key: str, *, base: str = API_BASE, metadata_base: str = METADATA_BASE, zone: str | None = None,
                 project_id: str | None = None, perf_iops: int = 5000, timeout: float = 30.0, sleep=time.sleep,
                 monotonic=time.monotonic, opener=_default_open, wait_timeout: float = 240.0):
        if perf_iops not in PERF_IOPS:
            raise ProviderError(f"perf_iops must be one of {PERF_IOPS}")
        self._key = secret_key
        self.base, self.metadata_base = base.rstrip("/"), metadata_base.rstrip("/")
        self.timeout, self.sleep, self.monotonic, self._open = timeout, sleep, monotonic, opener
        self.perf_iops, self.wait_timeout = perf_iops, wait_timeout
        self._zone, self._project, self._ident = zone, project_id, None

    # -- http
    def _call(self, method: str, path: str, body=None, *, query=None, auth: bool = True) -> tuple:
        """Returns ``(document, headers)``. GET retries on 429/5xx/network errors; writes retry only on 429."""
        data = None if body is None else json.dumps(body).encode()
        is_read = method == "GET"
        url = self.base + path + ("?" + urllib.parse.urlencode(query) if query else "")
        delay = 2.0
        for attempt in range(5):
            headers = {"Content-Type": "application/json", "User-Agent": f"motel/{__version__}"}
            if auth:
                headers["X-Auth-Token"] = self._key
            req = urllib.request.Request(url, data=data, method=method, headers=headers)
            try:
                with self._open(req, self.timeout) as resp:
                    raw = resp.read()
                    return (json.loads(raw) if raw else {}), dict(resp.headers.items())
            except urllib.error.HTTPError as exc:
                raw, retry_after = exc.read(), exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                try:
                    err = json.loads(raw) if raw else {}
                except ValueError:
                    err = {}
                transient = exc.code in (429, 500, 502, 503, 504)
                if transient and attempt < 4 and (is_read or exc.code == 429):
                    self.sleep(min(float(retry_after), 60.0) if retry_after and retry_after.isdigit() else delay)
                    delay *= 2
                    continue
                if exc.code == 404:
                    raise NotFound(f"{method} {path}: not found") from exc
                message = str(err.get("message", ""))[:200] if isinstance(err, dict) else ""
                kind = str(err.get("type", "")) if isinstance(err, dict) else ""
                hint = " (check the API key's project and permission sets)" if exc.code in (401, 403) else ""
                raise ProviderError(f"Scaleway API {method} {path}: HTTP {exc.code} {kind} {message}{hint}".replace("  ", " ").strip(),
                                    indeterminate=(not is_read and exc.code >= 500)) from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if is_read and attempt < 4:
                    self.sleep(delay)
                    delay *= 2
                    continue
                raise ProviderError(f"Scaleway API {method} {path}: {exc}", indeterminate=not is_read) from exc
        raise ProviderError(f"Scaleway API {method} {path}: retries exhausted", indeterminate=not is_read)

    def request(self, method: str, path: str, body=None, *, query=None, auth: bool = True) -> dict:
        return self._call(method, path, body, query=query, auth=auth)[0]

    def _pages(self, path: str, key: str, *, query: dict, size_param: str) -> list:
        """All items of a page/size listing; raises InventoryIncomplete when the listing cannot be completed."""
        out = []
        for page in range(1, MAX_PAGES + 1):
            try:
                doc, headers = self._call("GET", path, query={**query, "page": page, size_param: PAGE_SIZE})
            except ProviderError as exc:
                raise InventoryIncomplete(f"cannot read the full {key} list: {exc.message}", "motel doctor") from exc
            chunk = doc.get(key) or []
            out += chunk
            total = doc.get("total_count")
            if total is None:
                total = {k.lower(): v for k, v in headers.items()}.get("x-total-count")
            try:
                total = int(total) if total is not None else None
            except ValueError:
                total = None
            if not chunk or len(chunk) < PAGE_SIZE or (total is not None and len(out) >= total):
                if total is not None and len(out) < total:
                    raise InventoryIncomplete(f"the {key} list ended early ({len(out)} of {total})", "motel doctor")
                return out
        raise InventoryIncomplete(f"the {key} list has too many pages", "motel doctor")

    # -- identity (Instance metadata service)
    def _metadata(self) -> dict:
        req = urllib.request.Request(f"{self.metadata_base}/conf?format=json", headers={"User-Agent": f"motel/{__version__}"})
        try:
            with self._open(req, 5.0) as resp:
                return json.loads(resp.read())
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("cannot read this server's identity from the Scaleway metadata service "
                                f"(is this a Scaleway Instance?): {exc}") from exc

    def _identity(self) -> dict:
        if self._ident is None:
            doc = self._metadata()
            server = str(doc.get("id", "")).lower()
            zone = str(doc.get("zone") or (doc.get("location") or {}).get("zone_id") or "")
            project = str(doc.get("project", "")).lower()
            if not UUID_RE.fullmatch(server) or not UUID_RE.fullmatch(project) or not ZONE_RE.fullmatch(zone):
                raise ProviderError("the Scaleway metadata service returned an unexpected id, project or zone")
            if self._zone not in (None, zone):
                raise ProviderError(f"configured zone {self._zone} but this server is in {zone}")
            if self._project not in (None, project):
                raise ProviderError("configured project is not the project of this server")
            self._ident = {"server": server, "zone": zone, "project": project}
        return self._ident

    def whoami(self) -> HostIdentity:
        ident = self._identity()
        return HostIdentity(ident["server"], ident["zone"], "scaleway")

    def _scope(self, location: str | None = None) -> tuple:
        ident = self._identity()
        if location not in (None, "", ident["zone"]):
            raise ProviderError(f"this host is in zone {ident['zone']}; a volume in {location} could not be attached")
        return ident["zone"], ident["project"]

    def _vol_path(self, vol_id=None, zone: str | None = None) -> str:
        zone = zone or self._scope()[0]
        base = f"/block/v1/zones/{zone}/volumes"
        return base if vol_id is None else f"{base}/{_check_id(vol_id)}"

    # -- waiting
    def _wait_volume(self, vol_id: str, ready, what: str, *, quiet_error: bool = False) -> dict:
        deadline = self.monotonic() + self.wait_timeout
        while True:
            doc = self._get_doc(vol_id)
            if doc.get("status") == "error" and not quiet_error:
                raise ProviderError(f"Scaleway volume {vol_id} went to the error state while {what}")
            if any(r.get("status") == "error" for r in doc.get("references") or []) and not quiet_error:
                raise ProviderError(f"a reference of Scaleway volume {vol_id} went to the error state while {what}")
            if ready(doc):
                return doc
            if self.monotonic() > deadline:
                raise ProviderError(f"Scaleway volume {vol_id} did not finish {what} in time", indeterminate=True)
            self.sleep(2.0)

    def _get_doc(self, vol_id) -> dict:
        doc = self.request("GET", self._vol_path(vol_id))
        doc = doc.get("volume", doc)
        if doc.get("status") == "deleted":
            raise NotFound(f"volume {vol_id} is deleted")
        return doc

    # -- reads
    def visible(self) -> Inventory:
        zone, project = self._scope()
        servers = self._pages(f"/instance/v1/zones/{zone}/servers", "servers", query={"project": project}, size_param="per_page")
        volumes = self._pages(self._vol_path(), "volumes", query={"project_id": project}, size_param="page_size")
        return Inventory(volumes=tuple(parse_volume(v, zone) for v in volumes if v.get("status") != "deleted"),
                         servers=tuple(str(s["id"]).lower() for s in servers), complete=True)

    def price_gb_month(self, location: str) -> Decimal:
        """Euro per GB-month from Scaleway's public product catalogue (``Volume Low Latency 5K`` or ``15K``).

        The catalogue prices a GB-hour; a month is taken as 730 hours. A GB is 10^9 bytes, as motel counts it here.
        """
        zone = self._scope(location)[0]
        wanted = f"/storage/block/volume-low-latency-{self.perf_iops // 1000}k/{zone}"
        try:
            doc = self.request("GET", "/product-catalog/v2alpha1/public-catalog/products",
                               query={"product_types": "block_storage", "zone": zone, "page_size": 100}, auth=False)
            item = next(p for p in doc.get("products") or [] if p.get("sku") == wanted)
            money = item["price"]["retail_price"]
            hourly = Decimal(int(money["units"])) + Decimal(int(money["nanos"])) / Decimal(10**9)
            if money.get("currency_code") != "EUR" or hourly <= 0:
                raise ValueError("not a positive euro price")
        except (StopIteration, KeyError, TypeError, ValueError, InvalidOperation, ProviderError) as exc:
            raise ProviderError("cannot read the Block Storage price from the Scaleway product catalogue",
                                "set rooms.price_gb_month_eur in /etc/motel/motel.toml") from exc
        return (hourly * HOURS_PER_MONTH).quantize(Decimal("0.000001"))

    def get_volume(self, vol_id) -> Volume:
        return parse_volume(self._get_doc(vol_id), self._scope()[0])

    def list_owned(self, labels: dict) -> list:
        zone, project = self._scope()
        found = self._pages(self._vol_path(), "volumes", query={"project_id": project}, size_param="page_size")
        out = [parse_volume(v, zone) for v in found if v.get("status") != "deleted"]
        return [v for v in out if all(v.labels.get(k) == str(val) for k, val in labels.items())]

    # -- writes
    def create_volume(self, name, size_gb, location, labels, server_id=None) -> Volume:
        zone, project = self._scope(location)
        size_gb = int(size_gb)
        if not MIN_GB <= size_gb <= MAX_GB:
            raise ProviderError(f"Scaleway Block Storage volumes are {MIN_GB} GB to {MAX_GB} GB, not {size_gb} GB")
        if server_id is not None:
            _check_id(server_id, "server")
        body = {"name": str(name), "project_id": project, "perf_iops": self.perf_iops,
                "from_empty": {"size": size_gb * GB}, "tags": labels_to_tags(labels)}
        doc = self.request("POST", self._vol_path(zone=zone), body)
        doc = doc.get("volume", doc)
        vol_id = _check_id(doc["id"])
        try:
            self._wait_volume(vol_id, lambda d: d.get("status") == "available", "being created")
            if server_id is not None:
                self.attach(vol_id, server_id)
        except ProviderError as exc:
            # The volume exists. Report it as indeterminate so the desk finds it by its labels and carries on.
            raise ProviderError(f"volume {vol_id} was created but is not ready: {exc.message}", indeterminate=True) from exc
        return self.get_volume(vol_id)

    def attach(self, vol_id, server_id) -> None:
        zone = self._scope()[0]
        vol_id, server_id = _check_id(vol_id), _check_id(server_id, "server")
        current = parse_volume(self._get_doc(vol_id), zone).server_id
        if current not in (None, server_id):
            raise ProviderError(f"volume {vol_id} is already held by {current}")
        if current is None:
            self.request("POST", f"/instance/v1/zones/{zone}/servers/{server_id}/attach-volume",
                         {"volume_id": vol_id, "volume_type": "sbs_volume"})
        self._wait_volume(vol_id, lambda d: d.get("status") == "in_use" and _is_attached(d, server_id), "attaching")

    def detach(self, vol_id) -> None:
        zone = self._scope()[0]
        vol_id = _check_id(vol_id)
        holder = parse_volume(self._get_doc(vol_id), zone).server_id
        if holder is not None:
            if not UUID_RE.fullmatch(holder):
                raise ProviderError(f"volume {vol_id} is held by {holder}, which motel cannot detach")
            self.request("POST", f"/instance/v1/zones/{zone}/servers/{holder}/detach-volume", {"volume_id": vol_id})
        self._wait_volume(vol_id, lambda d: d.get("status") == "available" and _attachment(d) is None, "detaching")

    def resize(self, vol_id, size_gb) -> None:
        zone = self._scope()[0]
        vol_id, size_gb = _check_id(vol_id), int(size_gb)
        current = int(self._get_doc(vol_id)["size"]) // GB
        if size_gb <= current:
            raise ProviderError(f"size {size_gb} GB is not larger than {current} GB (Scaleway volumes only grow)")
        if size_gb > MAX_GB:
            raise ProviderError(f"Scaleway Block Storage volumes are at most {MAX_GB} GB")
        self.request("PATCH", self._vol_path(vol_id, zone), {"size": size_gb * GB})
        self._wait_volume(vol_id, lambda d: d.get("status") in ("available", "in_use") and int(d["size"]) >= size_gb * GB,
                          "resizing")

    def delete(self, vol_id) -> None:
        self.request("DELETE", self._vol_path(_check_id(vol_id)))

    # -- guest
    def device_path(self, vol_id) -> str:
        return f"/dev/disk/by-id/scsi-0SCW_sbs_volume-{_check_id(vol_id)}"

    def expected_serial(self, vol_id) -> str:
        return f"volume-{_check_id(vol_id)}"


def _is_attached(doc: dict, server_id: str) -> bool:
    refs = [r for r in doc.get("references") or [] if r.get("status") != "detached"]
    return bool(refs) and all(r.get("status") == "attached" and r.get("product_resource_type") == "instance_server"
                              and str(r.get("product_resource_id", "")).lower() == server_id for r in refs)
