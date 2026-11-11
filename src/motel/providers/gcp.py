"""Google Cloud Persistent Disk / Hyperdisk provider (standard library only; beta, not verified against the live API).

* Compute Engine REST API v1. The access token comes from the metadata server for this VM's service account
  (``Metadata-Flavor: Google``); there is no key file. The token goes only into the Authorization header of
  requests to the API host. Redirects are refused.
* Project, zone and instance name come from the metadata server. The volume id is the DISK NAME, and the server id
  is the INSTANCE NAME (disks list the instances using them by name).
* Disks are zonal, created in this VM's zone with ``labels`` set at creation, attached with
  ``deviceName`` = disk name, so ``/dev/disk/by-id/google-<name>`` is stable.
* Reads retry on 429/5xx/network errors; writes retry only on 429 and rate-limit 403 (the request was rejected).
  Create carries a ``requestId`` (a UUID derived from the disk name) so an identical retry cannot create two disks.
  A write whose outcome is unknown raises ``ProviderError(indeterminate=True)``.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime
from decimal import Decimal

from motel import __version__
from motel.errors import InventoryIncomplete, MotelError, NotFound, ProviderError
from motel.providers.base import HostIdentity, Inventory, Volume

COMPUTE_BASE = "https://compute.googleapis.com/compute/v1"
METADATA_BASE = "http://169.254.169.254/computeMetadata/v1"
NAME_RE = re.compile(r"^[a-z]([-a-z0-9]{0,61}[a-z0-9])?$")
LABEL_KEY_RE = re.compile(r"^[a-z][a-z0-9_-]{0,62}$")
LABEL_VALUE_RE = re.compile(r"^[a-z0-9_-]{0,63}$")
MAX_PAGES = 200
DISK_TYPES = ("pd-balanced", "pd-ssd", "pd-standard", "hyperdisk-balanced", "hyperdisk-throughput", "hyperdisk-extreme")
RATE_REASONS = frozenset({"rateLimitExceeded", "userRateLimitExceeded", "backendError"})

# USD per GB-month (GB here is GiB, as in the API's ``sizeGb``), on-demand list price, before tax. Only prices that
# were not in doubt when this table was written are listed; any other disk type or region raises an error that tells
# the operator to set rooms.price_gb_month_eur (read as USD for this provider). Source:
# https://cloud.google.com/compute/disks-image-pricing , checked 2026-11-09 (the page is rendered by script, so the
# figures come from the published price list, not a scrape; treat them as unverified).
PRICES_GB_MONTH_USD = {
    "pd-balanced": {"us-central1": Decimal("0.10"), "us-east1": Decimal("0.10"), "us-west1": Decimal("0.10")},
}
PRICE_SOURCE = "https://cloud.google.com/compute/disks-image-pricing"
PRICE_DATE = "2026-11-09"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def _default_open(req, timeout):
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


def _check_name(name) -> str:
    text = str(name)
    if not NAME_RE.fullmatch(text):
        raise ProviderError(f"refusing odd disk or instance name {text!r}")
    return text


def _check_labels(labels: dict) -> dict:
    out = {}
    for key, value in labels.items():
        key, value = str(key), str(value)
        if not LABEL_KEY_RE.fullmatch(key) or not LABEL_VALUE_RE.fullmatch(value):
            raise ProviderError(f"GCP label {key!r}={value!r} is not allowed (lowercase letters, digits, - and _ only; "
                                "up to 63 characters)")
        out[key] = value
    return out


def _last(url: str | None) -> str | None:
    return url.rsplit("/", 1)[-1] if url else None


def _parse_time(text):
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def parse_volume(doc: dict) -> Volume:
    users = sorted(_last(u) or "" for u in doc.get("users") or [])
    return Volume(id=str(doc["name"]), name=str(doc["name"]), size_gb=int(doc["sizeGb"]),
                  server_id=None if not users else ",".join(users), labels=dict(doc.get("labels") or {}),
                  delete_protected=False, created=_parse_time(doc.get("creationTimestamp")),
                  location=_last(doc.get("zone")) or _last(doc.get("region")))


class GcpProvider:
    name = "gcp"
    currency = "USD"
    formats_on_create = False  # persistent disks are blank: the desk makes the filesystem on a proven-blank device
    gb_bytes = 2 ** 30  # sizes are GiB

    def __init__(self, *, disk_type: str = "pd-balanced", provisioned_iops: int | None = None,
                 provisioned_throughput: int | None = None, base: str = COMPUTE_BASE, metadata_base: str = METADATA_BASE,
                 timeout: float = 30.0, sleep=time.sleep, monotonic=time.monotonic, opener=_default_open,
                 prices: dict | None = None):
        if disk_type not in DISK_TYPES:
            raise ProviderError(f"unsupported disk type {disk_type!r} (use one of {', '.join(DISK_TYPES)})")
        self.disk_type, self.iops, self.throughput = disk_type, provisioned_iops, provisioned_throughput
        self.base, self.metadata_base = base.rstrip("/"), metadata_base.rstrip("/")
        self.timeout, self.sleep, self.monotonic, self._open = timeout, sleep, monotonic, opener
        self._ident: tuple | None = None
        self._token: str | None = None
        self._token_until = 0.0
        self.prices = prices if prices is not None else PRICES_GB_MONTH_USD

    # -- metadata
    def _meta(self, path: str) -> str:
        req = urllib.request.Request(f"{self.metadata_base}/{path}", headers={"Metadata-Flavor": "Google"})
        with self._open(req, 5.0) as resp:
            return resp.read().decode("utf-8", "replace").strip()

    def _identity(self) -> tuple:
        if self._ident is None:
            try:
                project = self._meta("project/project-id")
                zone = _last(self._meta("instance/zone"))
                name = self._meta("instance/name")
            except Exception as exc:  # noqa: BLE001
                raise ProviderError("cannot read this VM's project, zone and name from the GCE metadata server "
                                    f"(is this a Compute Engine VM?): {exc}") from exc
            if not re.fullmatch(r"[a-z][-a-z0-9:.]{4,61}[a-z0-9]", project) or not zone or not NAME_RE.fullmatch(zone) \
                    or not NAME_RE.fullmatch(name):
                raise ProviderError(f"unexpected project {project!r}, zone {zone!r} or instance {name!r} from metadata")
            self._ident = (project, zone, name)
        return self._ident

    def whoami(self) -> HostIdentity:
        project, zone, name = self._identity()
        return HostIdentity(name, zone, "gcp")

    def _access_token(self) -> str:
        if self._token is None or self.monotonic() >= self._token_until:
            try:
                doc = json.loads(self._meta("instance/service-accounts/default/token"))
                self._token = str(doc["access_token"])
                self._token_until = self.monotonic() + max(30.0, float(doc.get("expires_in", 300)) - 60.0)
            except Exception as exc:  # noqa: BLE001
                raise ProviderError("cannot get an access token for this VM's service account from the metadata server "
                                    f"(is a service account attached, with the compute access scope?): {exc.__class__.__name__}") from exc
        return self._token

    # -- http
    def request(self, method: str, path: str, body=None, *, query: dict | None = None) -> dict:
        """GET retries on 429/5xx/network errors. Writes retry only when the request was rejected as rate limited."""
        data = None if body is None else json.dumps(body).encode()
        is_read = method == "GET"
        url = self.base + path + ("?" + urllib.parse.urlencode(query, quote_via=urllib.parse.quote) if query else "")
        delay = 1.0
        for attempt in range(6):
            req = urllib.request.Request(url, data=data, method=method, headers={
                "Authorization": f"Bearer {self._access_token()}", "Content-Type": "application/json",
                "User-Agent": f"motel/{__version__}"})
            try:
                with self._open(req, self.timeout) as resp:
                    raw = resp.read()
                    return json.loads(raw) if raw else {}
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                exc.close()
                try:
                    err = (json.loads(raw) or {}).get("error") or {}
                except ValueError:
                    err = {}
                reasons = {e.get("reason") for e in err.get("errors") or [] if isinstance(e, dict)}
                rejected = exc.code == 429 or (exc.code == 403 and bool(reasons & RATE_REASONS))
                transient = rejected or exc.code in (500, 502, 503, 504)
                if attempt < 5 and transient and (is_read or rejected):
                    self.sleep(min(delay, 20.0))
                    delay *= 2
                    continue
                if exc.code == 404:
                    raise NotFound(f"{method} {path}: not found") from exc
                message = f"GCE API {method} {path}: HTTP {exc.code} {err.get('status', '')} {err.get('message', '')}"
                raise ProviderError(message.strip(), indeterminate=(not is_read and exc.code >= 500 and not rejected)) from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if is_read and attempt < 5:
                    self.sleep(min(delay, 20.0))
                    delay *= 2
                    continue
                raise ProviderError(f"GCE API {method} {path}: {exc}", indeterminate=not is_read) from exc
        raise ProviderError(f"GCE API {method} {path}: retries exhausted", indeterminate=not is_read)

    def pages(self, path: str, query: dict | None = None) -> list:
        """All items of a paginated listing; raises InventoryIncomplete if the listing cannot be completed."""
        out, token = [], None
        for _ in range(MAX_PAGES):
            q = {"maxResults": "500", **(query or {})}
            if token:
                q["pageToken"] = token
            try:
                doc = self.request("GET", path, query=q)
            except ProviderError as exc:
                raise InventoryIncomplete(f"cannot read the full disk list: {exc.message}", "motel doctor") from exc
            items = doc.get("items") or []
            if isinstance(items, dict):  # aggregatedList: {"zones/x": {"disks": [...], "warning": {...}}}
                if doc.get("unreachables"):
                    raise InventoryIncomplete(f"some locations were unreachable: {', '.join(map(str, doc['unreachables'][:3]))}",
                                              "motel doctor")
                flat = []
                for scope, part in items.items():
                    if (part.get("warning") or {}).get("code") == "UNREACHABLE":
                        raise InventoryIncomplete(f"{scope} was unreachable", "motel doctor")
                    flat += part.get("disks") or []
                items = flat
            out += items
            token = doc.get("nextPageToken")
            if not token:
                return out
        raise InventoryIncomplete("the disk list has too many pages", "motel doctor")

    def _wait_op(self, op: dict, timeout: float = 300.0) -> None:
        """Poll a zone operation until DONE; an error in it raises, a timeout is indeterminate."""
        project, zone, _ = self._identity()
        deadline = self.monotonic() + timeout
        while op.get("status") != "DONE":
            if self.monotonic() > deadline:
                raise ProviderError(f"GCE operation {op.get('name')} timed out", indeterminate=True)
            self.sleep(2.0)
            where = _last(op.get("zone")) or zone
            op = self.request("GET", f"/projects/{project}/zones/{where}/operations/{_check_name(op['name'])}")
        errors = (op.get("error") or {}).get("errors") or []
        if errors:
            first = errors[0]
            raise ProviderError(f"GCE operation {op.get('operationType', '')} failed: {first.get('code', '')} "
                                f"{first.get('message', '')}".strip())

    # -- paths
    def _disks(self) -> str:
        project, zone, _ = self._identity()
        return f"/projects/{project}/zones/{zone}/disks"

    # -- reads
    def visible(self) -> Inventory:
        project, zone, name = self._identity()
        try:
            self.request("GET", f"/projects/{project}/zones/{zone}/instances/{name}")
        except NotFound:
            servers = ()
        else:
            servers = (name,)
        disks = self.pages(f"/projects/{project}/aggregated/disks")
        return Inventory(volumes=tuple(parse_volume(d) for d in disks), servers=servers, complete=True)

    def price_gb_month(self, location: str) -> Decimal:
        region = re.sub(r"-[a-z]$", "", location or self._identity()[1])
        try:
            return self.prices[self.disk_type][region]
        except KeyError:
            raise ProviderError(f"no built-in price for {self.disk_type} in {region} (list prices: {PRICE_SOURCE})",
                                "set rooms.price_gb_month_eur in /etc/motel/motel.toml (read as USD), then run: motel doctor") from None

    def get_volume(self, vol_id) -> Volume:
        return parse_volume(self.request("GET", f"{self._disks()}/{_check_name(vol_id)}"))

    def list_owned(self, labels: dict) -> list:
        want = {str(k): str(v) for k, v in labels.items()}
        return [v for v in (parse_volume(d) for d in self.pages(self._disks()))
                if all(v.labels.get(k) == val for k, val in want.items())]

    # -- writes
    def create_volume(self, name, size_gb, location, labels, server_id=None) -> Volume:
        project, zone, instance = self._identity()
        name = _check_name(name)
        if location and location != zone:
            raise ProviderError(f"disks must be created in this VM's zone ({zone}), not {location}")
        if server_id is not None and _check_name(server_id) != instance:
            raise ProviderError("this provider can only attach disks to the VM it runs on")
        body = {"name": name, "sizeGb": str(int(size_gb)), "type": f"projects/{project}/zones/{zone}/diskTypes/{self.disk_type}",
                "labels": _check_labels(labels)}
        if self.iops:
            body["provisionedIops"] = str(self.iops)
        if self.throughput:
            body["provisionedThroughput"] = str(self.throughput)
        request_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"motel/{project}/{zone}/{name}"))
        op = self.request("POST", self._disks(), body, query={"requestId": request_id})
        try:
            self._wait_op(op)
            if server_id is not None:
                self.attach(name, server_id)
            return self.get_volume(name)
        except MotelError as exc:
            # The disk may exist. Report "may or may not have worked" so the desk finds it by label and name.
            raise ProviderError(f"disk {name} create did not finish cleanly: {exc.message}", exc.hint, indeterminate=True) from exc

    def attach(self, vol_id, server_id) -> None:
        project, zone, _ = self._identity()
        name, instance = _check_name(vol_id), _check_name(server_id)
        body = {"source": f"projects/{project}/zones/{zone}/disks/{name}", "deviceName": name, "mode": "READ_WRITE",
                "type": "PERSISTENT", "autoDelete": False}
        self._wait_op(self.request("POST", f"/projects/{project}/zones/{zone}/instances/{instance}/attachDisk", body))

    def detach(self, vol_id) -> None:
        project, zone, _ = self._identity()
        vol = self.get_volume(vol_id)
        if vol.server_id is None:
            return
        if "," in vol.server_id:
            raise ProviderError(f"disk {vol.id} is attached to several instances ({vol.server_id}); refusing to detach")
        inst = self.request("GET", f"/projects/{project}/zones/{zone}/instances/{_check_name(vol.server_id)}")
        device = next((d.get("deviceName") for d in inst.get("disks") or [] if _last(d.get("source")) == vol.id), None)
        if not device:
            raise ProviderError(f"instance {vol.server_id} does not list disk {vol.id}")
        self._wait_op(self.request("POST", f"/projects/{project}/zones/{zone}/instances/{vol.server_id}/detachDisk",
                                   query={"deviceName": device}))

    def resize(self, vol_id, size_gb) -> None:
        name = _check_name(vol_id)
        current = self.get_volume(name).size_gb
        if int(size_gb) <= current:
            raise ProviderError(f"size {size_gb} is not larger than {current}")
        self._wait_op(self.request("POST", f"{self._disks()}/{name}/resize", {"sizeGb": str(int(size_gb))}))

    def delete(self, vol_id) -> None:
        self._wait_op(self.request("DELETE", f"{self._disks()}/{_check_name(vol_id)}"))

    def device_path(self, vol_id) -> str:
        return "/dev/disk/by-id/google-" + _check_name(vol_id)

    def expected_serial(self, vol_id) -> str:
        return _check_name(vol_id)
