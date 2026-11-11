"""Hetzner Cloud provider (standard library only).

The token goes only into the Authorization header of requests to the API host. Redirects are refused so the
header can never follow a redirect elsewhere. Writes that may have happened but whose response was lost
raise ``ProviderError(indeterminate=True)`` and are never retried blindly.
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
from motel.errors import InventoryIncomplete, MotelError, NotFound, ProviderError
from motel.providers.base import HostIdentity, Inventory, Volume

API_BASE = "https://api.hetzner.cloud/v1"
METADATA_BASE = "http://169.254.169.254/hetzner/v1/metadata"
ID_RE = re.compile(r"^[0-9]{1,18}$")
MAX_PAGES = 200


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def _default_open(req, timeout):
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


def _check_id(vol_id) -> str:
    text = str(vol_id)
    if not ID_RE.fullmatch(text):
        raise ProviderError(f"refusing odd volume id {text!r}")
    return text


def _parse_time(text):
    try:
        return datetime.fromisoformat(text) if text else None
    except ValueError:
        return None


def parse_volume(doc: dict) -> Volume:
    server = doc.get("server")
    return Volume(id=str(doc["id"]), name=str(doc.get("name", "")), size_gb=int(doc["size"]),
                  server_id=None if server is None else str(server), labels=dict(doc.get("labels") or {}),
                  delete_protected=bool((doc.get("protection") or {}).get("delete")),
                  created=_parse_time(doc.get("created")),
                  location=((doc.get("location") or {}).get("name")))


class HetznerProvider:
    name = "hetzner"
    currency = "EUR"
    formats_on_create = True  # created with format=ext4

    def __init__(self, token: str, *, base: str = API_BASE, timeout: float = 30.0, sleep=time.sleep,
                 monotonic=time.monotonic, opener=_default_open, metadata_fetch=None):
        self._token = token
        self.base, self.timeout, self.sleep, self.monotonic, self._open = base.rstrip("/"), timeout, sleep, monotonic, opener
        self._metadata = metadata_fetch or self._fetch_metadata

    # -- http
    def request(self, method: str, path: str, body=None, *, retry_unsafe: bool = False) -> dict:
        """GET retries on 429/5xx/network errors. Writes retry only on 429 (the request was rejected)."""
        data = None if body is None else json.dumps(body).encode()
        is_read = method == "GET"
        delay = 2.0
        for attempt in range(5):
            req = urllib.request.Request(self.base + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self._token}", "Content-Type": "application/json",
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
                transient = exc.code in (429, 500, 502, 503, 504)
                if transient and attempt < 4 and (is_read or exc.code == 429):
                    self.sleep(delay)
                    delay *= 2
                    continue
                if exc.code == 404:
                    raise NotFound(f"{method} {path}: not found") from exc
                message = f"Hetzner API {method} {path}: HTTP {exc.code} {err.get('code', '')} {err.get('message', '')}"
                raise ProviderError(message.strip(), indeterminate=(not is_read and exc.code >= 500)) from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if is_read and attempt < 4:
                    self.sleep(delay)
                    delay *= 2
                    continue
                raise ProviderError(f"Hetzner API {method} {path}: {exc}", indeterminate=not is_read) from exc
        raise ProviderError(f"Hetzner API {method} {path}: retries exhausted", indeterminate=not is_read)

    def pages(self, path: str, key: str) -> list:
        """All items of a paginated listing; raises InventoryIncomplete if the listing cannot be completed."""
        out, page = [], 1
        sep = "&" if "?" in path else "?"
        for _ in range(MAX_PAGES):
            try:
                doc = self.request("GET", f"{path}{sep}per_page=50&page={page}")
            except ProviderError as exc:
                raise InventoryIncomplete(f"cannot read the full {key} list: {exc.message}", "motel doctor") from exc
            out += doc.get(key) or []
            pagination = (doc.get("meta") or {}).get("pagination") or {}
            nxt = pagination.get("next_page")
            if not nxt:
                total = pagination.get("total_entries")
                if isinstance(total, int) and len(out) < total:
                    raise InventoryIncomplete(f"the {key} list ended early ({len(out)} of {total})", "motel doctor")
                return out
            page = nxt
        raise InventoryIncomplete(f"the {key} list has too many pages", "motel doctor")

    def wait(self, actions: list, timeout: float = 240.0) -> None:
        deadline = self.monotonic() + timeout
        for action in actions:
            current = action
            while current.get("status") == "running":
                if self.monotonic() > deadline:
                    raise ProviderError(f"Hetzner action {current.get('command')} {current.get('id')} timed out",
                                        indeterminate=True)
                self.sleep(2.0)
                current = self.request("GET", f"/actions/{int(current['id'])}")["action"]
            if current.get("status") != "success":
                err = current.get("error") or {}
                raise ProviderError(f"Hetzner action {current.get('command')} failed: {err.get('code')} {err.get('message')}")

    # -- metadata
    @staticmethod
    def _fetch_metadata(path: str) -> str:
        with urllib.request.urlopen(f"{METADATA_BASE}/{path}", timeout=5) as resp:
            return resp.read().decode().strip()

    def whoami(self) -> HostIdentity:
        try:
            server_id = self._metadata("instance-id")
            zone = self._metadata("availability-zone")
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("cannot read this server's id from the Hetzner metadata service "
                                f"(is this a Hetzner Cloud server?): {exc}") from exc
        if not ID_RE.fullmatch(server_id):
            raise ProviderError(f"unexpected instance id {server_id!r} from the metadata service")
        return HostIdentity(server_id, zone.split("-")[0] if zone else "", "hetzner")

    # -- reads
    def server(self, server_id: str) -> dict:
        return self.request("GET", f"/servers/{_check_id(server_id)}")["server"]

    def visible(self) -> Inventory:
        servers = self.pages("/servers", "servers")
        volumes = self.pages("/volumes", "volumes")
        return Inventory(volumes=tuple(parse_volume(v) for v in volumes),
                         servers=tuple(str(s["id"]) for s in servers), complete=True)

    def price_gb_month(self, location: str) -> Decimal:
        doc = self.request("GET", "/pricing")
        try:
            return Decimal(str(doc["pricing"]["volume"]["price_per_gb_month"]["gross"]))
        except (KeyError, TypeError, InvalidOperation) as exc:
            raise ProviderError("cannot read the volume price from the Hetzner pricing API",
                                "motel doctor") from exc

    def get_volume(self, vol_id) -> Volume:
        return parse_volume(self.request("GET", f"/volumes/{_check_id(vol_id)}")["volume"])

    def list_owned(self, labels: dict) -> list:
        selector = ",".join(f"{k}={v}" for k, v in sorted(labels.items()))
        path = "/volumes?label_selector=" + urllib.parse.quote(selector, safe="")
        return [parse_volume(v) for v in self.pages(path, "volumes")]

    # -- writes
    def create_volume(self, name, size_gb, location, labels, server_id=None) -> Volume:
        body = {"name": name, "size": int(size_gb), "automount": False, "format": "ext4", "labels": dict(labels)}
        if server_id is not None:
            body["server"] = int(_check_id(server_id))  # Hetzner takes either a server or a location
        else:
            body["location"] = location
        doc = self.request("POST", "/volumes", body)
        vol = parse_volume(doc["volume"])  # the volume exists (and bills) from here on, whatever happens next
        try:
            self.wait([a for a in [doc.get("action")] + (doc.get("next_actions") or []) if a])
        except MotelError as exc:
            raise ProviderError(f"volume {vol.id} was created but did not become ready: {exc.message}", exc.hint,
                                indeterminate=True, volume_id=vol.id) from exc
        return vol

    def attach(self, vol_id, server_id) -> None:
        doc = self.request("POST", f"/volumes/{_check_id(vol_id)}/actions/attach",
                           {"server": int(_check_id(server_id)), "automount": False})
        self.wait([doc["action"]])

    def detach(self, vol_id) -> None:
        self.wait([self.request("POST", f"/volumes/{_check_id(vol_id)}/actions/detach", {})["action"]])

    def resize(self, vol_id, size_gb) -> None:
        self.wait([self.request("POST", f"/volumes/{_check_id(vol_id)}/actions/resize", {"size": int(size_gb)})["action"]])

    def delete(self, vol_id) -> None:
        self.request("DELETE", f"/volumes/{_check_id(vol_id)}")

    def device_path(self, vol_id) -> str:
        return f"/dev/disk/by-id/scsi-0HC_Volume_{_check_id(vol_id)}"

    def expected_serial(self, vol_id) -> str:
        return _check_id(vol_id)
