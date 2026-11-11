"""AWS EBS provider (standard library only; beta, not verified against the live API).

* EC2 Query API (``Version=2016-11-15``), requests signed with SigV4 implemented here with ``hmac``/``hashlib``.
* This instance's id, availability zone and region come from IMDSv2 (token PUT first; there is no IMDSv1 fallback).
* Credentials come from the instance role through IMDS (nothing long-lived on disk) or from a root-owned
  credentials file. They go only into the signature and the ``x-amz-security-token`` header of requests to the EC2
  endpoint; redirects are refused.
* Volumes are created in this instance's availability zone. Labels are EBS tags; the volume name is the ``Name`` tag.
* Reads retry on throttling, 5xx and network errors. Writes retry only on throttling error codes (the request was
  rejected). A write whose outcome is unknown raises ``ProviderError(indeterminate=True)``; ``CreateVolume`` also
  carries a ``ClientToken`` so a later identical request would not create a second volume.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import stat
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET  # noqa: S405  responses come only from the signed TLS endpoint we chose
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from motel import __version__
from motel.errors import InventoryIncomplete, MotelError, NotFound, ProviderError, Usage
from motel.providers.base import HostIdentity, Inventory, Volume

EC2_VERSION = "2016-11-15"
IMDS_BASE = "http://169.254.169.254"
VOLUME_RE = re.compile(r"^vol-[0-9a-f]{8}(?:[0-9a-f]{9})?$")
INSTANCE_RE = re.compile(r"^i-[0-9a-f]{8}(?:[0-9a-f]{9})?$")
ZONE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{2,30}[a-z0-9]$")
MAX_PAGES = 200
THROTTLE_CODES = frozenset({"RequestLimitExceeded", "Throttling", "ThrottlingException", "ThrottledException",
                            "TooManyRequestsException", "RequestThrottled", "RequestThrottledException",
                            "ServiceUnavailable", "Unavailable", "SlowDown", "PriorRequestNotComplete"})
VOLUME_TYPES = frozenset({"gp3", "gp2", "io1", "io2", "st1", "sc1", "standard"})
DEVICE_NAMES = tuple(f"/dev/sd{c}" for c in "fghijklmnop") + tuple(f"/dev/sd{c}" for c in "qrstuvwxyz")

# USD per GB-month, on-demand list price, EXCLUDING tax. Only prices that were not in doubt when this table was
# written are listed; any other region raises an error that tells the operator to set rooms.price_gb_month_eur
# (the number is read as USD for this provider). Source: https://aws.amazon.com/ebs/pricing/ , checked 2026-11-09
# (the page is rendered by script, so the figures were taken from the published price list, not scraped).
PRICES_GB_MONTH_USD = {
    "gp3": {"us-east-1": Decimal("0.08"), "us-east-2": Decimal("0.08"), "us-west-2": Decimal("0.08")},
}
PRICE_SOURCE = "https://aws.amazon.com/ebs/pricing/"
PRICE_DATE = "2026-11-09"


class _NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def _default_open(req, timeout):
    return urllib.request.build_opener(_NoRedirect).open(req, timeout=timeout)


def _check_volume_id(vol_id) -> str:
    text = str(vol_id)
    if not VOLUME_RE.fullmatch(text):
        raise ProviderError(f"refusing odd volume id {text!r}")
    return text


def _check_instance_id(server_id) -> str:
    text = str(server_id)
    if not INSTANCE_RE.fullmatch(text):
        raise ProviderError(f"refusing odd instance id {text!r}")
    return text


# ---------------------------------------------------------------------------------------------- SigV4
def _hmac(key: bytes, msg: str) -> bytes:
    return hmac.new(key, msg.encode("utf-8"), hashlib.sha256).digest()


def signing_key(secret_key: str, date: str, region: str, service: str) -> bytes:
    key = _hmac(("AWS4" + secret_key).encode("utf-8"), date)
    for part in (region, service, "aws4_request"):
        key = _hmac(key, part)
    return key


def _enc(text: str) -> str:
    return urllib.parse.quote(text, safe="-_.~")


def canonical_request(method: str, path: str, query: str, headers: dict, payload: bytes) -> tuple:
    """Returns (canonical request text, signed header names). ``headers`` maps names to values; every header is signed."""
    pairs = sorted((_enc(k), _enc(v)) for k, v in urllib.parse.parse_qsl(query, keep_blank_values=True))
    canon_query = "&".join(f"{k}={v}" for k, v in pairs)
    canon_path = "/".join(_enc(_enc(seg)) for seg in (path or "/").split("/")) or "/"
    lowered = {k.lower(): " ".join(str(v).split()) for k, v in headers.items()}
    names = sorted(lowered)
    block = "".join(f"{n}:{lowered[n]}\n" for n in names)
    signed = ";".join(names)
    text = "\n".join([method, canon_path, canon_query, block, signed, hashlib.sha256(payload).hexdigest()])
    return text, signed


def sigv4(*, method: str, path: str, query: str, headers: dict, payload: bytes, access_key: str, secret_key: str,
          region: str, service: str, amz_date: str) -> tuple:
    """Returns (Authorization header value, signature). ``amz_date`` is ``YYYYMMDDTHHMMSSZ``."""
    day = amz_date[:8]
    request, signed = canonical_request(method, path, query, headers, payload)
    scope = f"{day}/{region}/{service}/aws4_request"
    to_sign = "\n".join(["AWS4-HMAC-SHA256", amz_date, scope, hashlib.sha256(request.encode("utf-8")).hexdigest()])
    signature = hmac.new(signing_key(secret_key, day, region, service), to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    auth = f"AWS4-HMAC-SHA256 Credential={access_key}/{scope}, SignedHeaders={signed}, Signature={signature}"
    return auth, signature


# ---------------------------------------------------------------------------------------------- credentials
@dataclass(frozen=True)
class Credentials:
    access_key: str
    secret_key: str
    token: str | None = None
    expires: datetime | None = None

    def __repr__(self) -> str:  # never print secrets
        return f"Credentials(access_key={self.access_key[:4]}..., expires={self.expires})"


def load_credentials_file(path: str, *, owner_uid: int = 0) -> Credentials:
    """Read ``aws_access_key_id`` / ``aws_secret_access_key`` / optional ``aws_session_token`` (``key = value`` lines;
    ``[section]`` lines and comments are ignored) from a root-owned, non-symlink, regular file of mode 0600 or stricter."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise Usage(f"cannot open the credentials file {path!r} (a symlink or a missing file is refused): {exc.strerror}",
                    "sudo motel-desk setup aws") from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Usage(f"{path} is not a regular file", "sudo motel-desk setup aws")
        if st.st_uid != owner_uid:
            raise Usage(f"{path} must be owned by root", f"sudo chown root:root {path}")
        if stat.S_IMODE(st.st_mode) & ~0o600:
            raise Usage(f"{path} must be mode 0600 or stricter", f"sudo chmod 600 {path}")
        if st.st_size > 8192:
            raise Usage(f"{path} is too large to be a credentials file", "sudo motel-desk setup aws")
        with os.fdopen(os.dup(fd), encoding="utf-8", errors="strict") as handle:
            text = handle.read()
    except UnicodeDecodeError as exc:
        raise Usage(f"{path} does not hold credentials", "sudo motel-desk setup aws") from exc
    finally:
        os.close(fd)
    values = {}
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith(("#", ";", "[")) or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip().lower()] = value.strip()
    access, secret, token = values.get("aws_access_key_id"), values.get("aws_secret_access_key"), values.get("aws_session_token")
    if not access or not secret or not re.fullmatch(r"[A-Za-z0-9/+=_.-]{8,200}", access + "") \
            or not re.fullmatch(r"[A-Za-z0-9/+=_.-]{16,200}", secret):
        raise Usage(f"{path} needs aws_access_key_id and aws_secret_access_key lines", "sudo motel-desk setup aws")
    return Credentials(access, secret, token or None)


def _parse_iso(text: str) -> datetime | None:
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except (ValueError, AttributeError):
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=UTC)


class _Imds:
    """IMDSv2 client. A token is fetched with PUT first and sent on every GET; there is no IMDSv1 fallback."""

    def __init__(self, base: str, opener, timeout: float, monotonic):
        self.base, self._open, self.timeout, self._mono = base.rstrip("/"), opener, timeout, monotonic
        self._token: str | None = None
        self._token_until = 0.0

    def _request(self, method: str, path: str, headers: dict) -> str:
        req = urllib.request.Request(self.base + path, method=method, headers=headers)
        try:
            with self._open(req, self.timeout) as resp:
                return resp.read().decode("utf-8", "replace").strip()
        except urllib.error.HTTPError as exc:
            exc.close()
            raise OSError(f"IMDS {method} {path}: HTTP {exc.code}") from exc

    def _token_value(self) -> str:
        if self._token is None or self._mono() >= self._token_until:
            self._token = self._request("PUT", "/latest/api/token", {"X-aws-ec2-metadata-token-ttl-seconds": "600"})
            self._token_until = self._mono() + 300
        return self._token

    def get(self, path: str) -> str:
        try:
            return self._request("GET", path, {"X-aws-ec2-metadata-token": self._token_value()})
        except OSError as exc:
            if "HTTP 401" not in str(exc):
                raise
            self._token = None  # expired token: fetch one new one and try once more
            return self._request("GET", path, {"X-aws-ec2-metadata-token": self._token_value()})


class AwsProvider:
    name = "aws"
    currency = "USD"
    formats_on_create = False  # EBS volumes are blank: the desk makes the filesystem on a proven-blank device
    gb_bytes = 2 ** 30  # sizes are GiB

    def __init__(self, credentials=None, *, region: str | None = None, base: str | None = None, imds_base: str = IMDS_BASE,
                 volume_type: str = "gp3", iops: int | None = None, throughput: int | None = None,
                 encrypted: bool | None = None, kms_key_id: str | None = None, timeout: float = 30.0, sleep=time.sleep,
                 monotonic=time.monotonic, clock=lambda: datetime.now(UTC), opener=_default_open, prices: dict | None = None):
        """``credentials`` is a ``Credentials``, a callable returning one, or None for the instance role (IMDS)."""
        if volume_type not in VOLUME_TYPES:
            raise ProviderError(f"unsupported EBS volume type {volume_type!r} (use one of {', '.join(sorted(VOLUME_TYPES))})")
        if volume_type in ("io1", "io2") and not iops:
            raise ProviderError(f"EBS volume type {volume_type} needs iops")
        self.volume_type, self.iops, self.throughput = volume_type, iops, throughput
        self.encrypted, self.kms_key_id = encrypted, kms_key_id
        self.timeout, self.sleep, self.monotonic, self.clock, self._open = timeout, sleep, monotonic, clock, opener
        self._region, self._base = region, base.rstrip("/") if base else None
        self._imds = _Imds(imds_base, opener, 5.0, monotonic)
        self._creds_source = credentials
        self._creds: Credentials | None = credentials if isinstance(credentials, Credentials) else None
        self._ident: tuple | None = None
        self.prices = prices if prices is not None else PRICES_GB_MONTH_USD

    # -- identity
    def _identity(self) -> tuple:
        if self._ident is None:
            try:
                instance_id = self._imds.get("/latest/meta-data/instance-id")
                zone = self._imds.get("/latest/meta-data/placement/availability-zone")
                region = self._imds.get("/latest/meta-data/placement/region")
            except Exception as exc:  # noqa: BLE001
                raise ProviderError("cannot read this instance's id from IMDSv2 (is this an EC2 instance, and is IMDS "
                                    f"enabled with tokens allowed?): {exc}") from exc
            if not INSTANCE_RE.fullmatch(instance_id):
                raise ProviderError(f"unexpected instance id {instance_id!r} from IMDS")
            if not ZONE_RE.fullmatch(zone) or not ZONE_RE.fullmatch(region):
                raise ProviderError(f"unexpected zone {zone!r} or region {region!r} from IMDS")
            self._ident = (instance_id, zone, self._region or region)
        return self._ident

    def whoami(self) -> HostIdentity:
        instance_id, zone, _ = self._identity()
        return HostIdentity(instance_id, zone, "aws")

    def _endpoint(self) -> str:
        if self._base:
            return self._base
        region = self._identity()[2]
        return f"https://ec2.{region}.amazonaws.com" + (".cn" if region.startswith("cn-") else "")

    def _credentials(self) -> Credentials:
        creds = self._creds
        now = self.clock()
        if creds is not None and (creds.expires is None or creds.expires - now > timedelta(minutes=5)):
            return creds
        if callable(self._creds_source):
            self._creds = self._creds_source()
            return self._creds
        try:
            role = self._imds.get("/latest/meta-data/iam/security-credentials/").splitlines()[0].strip()
            doc = json.loads(self._imds.get(f"/latest/meta-data/iam/security-credentials/{urllib.parse.quote(role, safe='')}"))
            if doc.get("Code", "Success") != "Success":
                raise ValueError(f"IMDS credentials code {doc.get('Code')}")
            self._creds = Credentials(doc["AccessKeyId"], doc["SecretAccessKey"], doc.get("Token"),
                                      _parse_iso(doc.get("Expiration", "")))
        except Exception as exc:  # noqa: BLE001
            raise ProviderError("cannot get credentials from the instance role (attach an instance profile, or install a "
                                f"root-owned credentials file): {exc.__class__.__name__}") from exc
        return self._creds

    # -- http
    def _call(self, action: str, params: dict | None = None, *, write: bool = False) -> ET.Element:
        """One EC2 call. Reads retry on throttling/5xx/network; writes only on throttling error codes."""
        params = {"Action": action, "Version": EC2_VERSION, **(params or {})}
        body = urllib.parse.urlencode(sorted(params.items()), quote_via=urllib.parse.quote, safe="-_.~").encode()
        base = self._endpoint()
        parsed = urllib.parse.urlsplit(base)
        region = self._identity()[2]
        delay = 1.0
        for attempt in range(6):
            creds = self._credentials()
            stamp = self.clock().astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")
            headers = {"content-type": "application/x-www-form-urlencoded; charset=utf-8", "host": parsed.netloc,
                       "x-amz-date": stamp}
            if creds.token:
                headers["x-amz-security-token"] = creds.token
            auth, _ = sigv4(method="POST", path="/", query="", headers=headers, payload=body, access_key=creds.access_key,
                            secret_key=creds.secret_key, region=region, service="ec2", amz_date=stamp)
            req = urllib.request.Request(base + "/", data=body, method="POST", headers={
                "Content-Type": headers["content-type"], "X-Amz-Date": stamp, "Authorization": auth,
                "User-Agent": f"motel/{__version__}", **({"X-Amz-Security-Token": creds.token} if creds.token else {})})
            try:
                with self._open(req, self.timeout) as resp:
                    return _parse_xml(resp.read())
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                exc.close()
                code, message = _xml_error(raw)
                throttled = code in THROTTLE_CODES or exc.code == 429
                transient = throttled or exc.code in (500, 502, 503, 504)
                if attempt < 5 and transient and (not write or throttled):
                    self.sleep(min(delay, 20.0))
                    delay *= 2
                    continue
                if code in ("InvalidVolume.NotFound", "InvalidInstanceID.NotFound"):
                    raise NotFound(f"{action}: {message or code}") from exc
                text = f"AWS EC2 {action}: HTTP {exc.code} {code} {message}".strip()
                raise ProviderError(text, indeterminate=write and exc.code >= 500 and not throttled) from exc
            except (urllib.error.URLError, TimeoutError, OSError, ET.ParseError) as exc:
                if not write and attempt < 5:
                    self.sleep(min(delay, 20.0))
                    delay *= 2
                    continue
                raise ProviderError(f"AWS EC2 {action}: {exc}", indeterminate=write) from exc
        raise ProviderError(f"AWS EC2 {action}: retries exhausted", indeterminate=write)

    # -- reads
    def _describe(self, params: dict, key: str = "volumeSet") -> list:
        """All ``DescribeVolumes`` pages. Raises InventoryIncomplete if the listing cannot be completed."""
        out, token = [], None
        for _ in range(MAX_PAGES):
            page = dict(params)
            if "VolumeId.1" not in page:  # EC2 refuses MaxResults together with explicit volume ids
                page["MaxResults"] = "500"
            if token:
                page["NextToken"] = token
            try:
                root = self._call("DescribeVolumes", page)
            except ProviderError as exc:
                raise InventoryIncomplete(f"cannot read the full volume list: {exc.message}", "motel doctor") from exc
            out += [parse_volume(item) for item in _items(root, key)]
            token = _text(root, "nextToken")
            if not token:
                return out
        raise InventoryIncomplete("the volume list has too many pages", "motel doctor")

    def visible(self) -> Inventory:
        instance_id = self._identity()[0]
        root = self._call("DescribeInstances", {"InstanceId.1": instance_id})
        seen = [_text(i, "instanceId") for r in _items(root, "reservationSet") for i in _items(r, "instancesSet")]
        volumes = [v for v in self._describe({}) if v.size_gb is not None and v.state not in ("deleting", "deleted")]
        return Inventory(volumes=tuple(volumes), servers=tuple(s for s in seen if s), complete=True)

    def price_gb_month(self, location: str) -> Decimal:
        region = self._identity()[2] if not location or location == self._identity()[1] else _region_of(location)
        try:
            return self.prices[self.volume_type][region]
        except KeyError:
            raise ProviderError(f"no built-in price for {self.volume_type} in {region} (list prices: {PRICE_SOURCE})",
                                "set rooms.price_gb_month_eur in /etc/motel/motel.toml (read as USD), then run: motel doctor") from None

    def get_volume(self, vol_id) -> Volume:
        vols = self._describe({"VolumeId.1": _check_volume_id(vol_id)})
        if not vols or vols[0].state in ("deleted",):
            raise NotFound(f"no volume {vol_id}")
        return vols[0]

    def list_owned(self, labels: dict) -> list:
        params, n = {}, 0
        for key, value in sorted(labels.items()):
            n += 1
            params[f"Filter.{n}.Name"] = f"tag:{key}"
            params[f"Filter.{n}.Value.1"] = str(value)
        found = self._describe(params)
        return [v for v in found if v.state not in ("deleting", "deleted")
                and all(v.labels.get(k) == str(val) for k, val in labels.items())]

    # -- writes
    def _wait_volume(self, vol_id: str, done, what: str, timeout: float = 300.0, missing_ok: float = 30.0) -> Volume:
        start = self.monotonic()
        deadline = start + timeout
        while True:
            try:
                vol = self.get_volume(vol_id)
            except NotFound:
                # EC2 is eventually consistent: a volume that was just created may not be listed yet.
                if self.monotonic() - start > missing_ok:
                    raise
                vol = None
            except InventoryIncomplete as exc:
                raise ProviderError(f"while waiting for volume {vol_id} to {what}: {exc.message}", indeterminate=True) from exc
            if vol is not None:
                if vol.state == "error":
                    raise ProviderError(f"volume {vol_id} went into the error state while waiting to {what}", indeterminate=True)
                if done(vol):
                    return vol
            if self.monotonic() > deadline:
                raise ProviderError(f"timed out waiting for volume {vol_id} to {what}", indeterminate=True)
            self.sleep(3.0)

    def create_volume(self, name, size_gb, location, labels, server_id=None) -> Volume:
        instance_id, zone, _ = self._identity()
        zone = location or zone
        if server_id is not None and (_check_instance_id(server_id) != instance_id or zone != self._identity()[1]):
            raise ProviderError("EBS volumes can only be created in the availability zone of the instance they attach to "
                                f"({self._identity()[1]}), and only for this instance")
        tags = _check_tags({**labels, "Name": name})
        params = {"AvailabilityZone": zone, "Size": str(int(size_gb)), "VolumeType": self.volume_type, "ClientToken": name,
                  "TagSpecification.1.ResourceType": "volume"}
        for i, (key, value) in enumerate(sorted(tags.items()), 1):
            params[f"TagSpecification.1.Tag.{i}.Key"] = key
            params[f"TagSpecification.1.Tag.{i}.Value"] = value
        if self.iops:
            params["Iops"] = str(self.iops)
        if self.throughput:
            params["Throughput"] = str(self.throughput)
        if self.encrypted is not None:
            params["Encrypted"] = "true" if self.encrypted else "false"
        if self.kms_key_id:
            params["KmsKeyId"] = self.kms_key_id
        root = self._call("CreateVolume", params, write=True)
        vol_id = _text(root, "volumeId")
        if not vol_id or not VOLUME_RE.fullmatch(vol_id):
            raise ProviderError("EC2 CreateVolume returned no usable volume id", indeterminate=True)
        try:
            self._wait_volume(vol_id, lambda v: v.state == "available", "become available")
            if server_id is not None:
                self.attach(vol_id, server_id)
            return self.get_volume(vol_id)
        except MotelError as exc:
            # The volume exists. Report "may or may not have worked" so the desk finds it by its labels and name and
            # carries on (attach included) instead of forgetting a billed volume.
            raise ProviderError(f"volume {vol_id} was created but is not ready: {exc.message}", exc.hint,
                                indeterminate=True) from exc

    def _free_device(self, instance_id: str, skip=()) -> str:
        used = {a for v in self._describe({"Filter.1.Name": "attachment.instance-id", "Filter.1.Value.1": instance_id})
                for a in v.devices}
        for dev in DEVICE_NAMES:
            if dev not in used and dev not in skip:
                return dev
        raise ProviderError("no free /dev/sd[f-z] device name on this instance")

    def attach(self, vol_id, server_id) -> None:
        vol_id, instance_id = _check_volume_id(vol_id), _check_instance_id(server_id)
        tried: list = []
        for _ in range(3):
            device = self._free_device(instance_id, tried)
            try:
                self._call("AttachVolume", {"VolumeId": vol_id, "InstanceId": instance_id, "Device": device}, write=True)
                break
            except ProviderError as exc:
                if exc.indeterminate or "already in use" not in exc.message:
                    raise
                tried.append(device)
        else:
            raise ProviderError(f"could not find a free device name for {vol_id}")
        self._wait_volume(vol_id, lambda v: v.server_id == instance_id and v.attach_state == "attached", "attach")

    def detach(self, vol_id) -> None:
        vol = self.get_volume(vol_id)
        if vol.server_id is None:
            return
        self._call("DetachVolume", {"VolumeId": vol.id, "InstanceId": vol.server_id}, write=True)
        self._wait_volume(vol.id, lambda v: v.server_id is None and v.state == "available", "detach")

    def resize(self, vol_id, size_gb) -> None:
        vol_id = _check_volume_id(vol_id)
        current = self.get_volume(vol_id).size_gb
        if int(size_gb) <= current:
            raise ProviderError(f"size {size_gb} is not larger than {current}")
        try:
            self._call("ModifyVolume", {"VolumeId": vol_id, "Size": str(int(size_gb))}, write=True)
        except ProviderError as exc:
            if "VolumeModificationRateExceeded" in exc.message:
                raise ProviderError("EBS allows one modification per volume every 6 hours; this volume was modified too "
                                    "recently", "retry after the 6 hour cool-down") from exc
            raise
        start = self.monotonic()
        while True:
            root = self._call("DescribeVolumesModifications", {"VolumeId.1": vol_id})
            state = _text(root, "volumeModificationSet/item/modificationState")
            if state in ("optimizing", "completed"):
                return
            if state == "failed":
                raise ProviderError(f"EBS modification of {vol_id} failed: {_text(root, 'volumeModificationSet/item/statusMessage')}")
            if self.monotonic() - start > 600:
                raise ProviderError(f"timed out waiting for the EBS modification of {vol_id}", indeterminate=True)
            self.sleep(5.0)

    def delete(self, vol_id) -> None:
        self._call("DeleteVolume", {"VolumeId": _check_volume_id(vol_id)}, write=True)

    def device_path(self, vol_id) -> str:
        return "/dev/disk/by-id/nvme-Amazon_Elastic_Block_Store_" + _check_volume_id(vol_id).replace("-", "")

    def expected_serial(self, vol_id) -> str:
        return _check_volume_id(vol_id).replace("-", "")


# ---------------------------------------------------------------------------------------------- XML helpers
def _strip_ns(root: ET.Element) -> ET.Element:
    for el in root.iter():
        el.tag = el.tag.rsplit("}", 1)[-1]
    return root


def _parse_xml(raw: bytes) -> ET.Element:
    return _strip_ns(ET.fromstring(raw))


def _text(el: ET.Element, path: str) -> str | None:
    node = el.find(path)
    return None if node is None or node.text is None else node.text.strip()


def _items(el: ET.Element, path: str) -> list:
    node = el.find(path)
    return [] if node is None else node.findall("item")


def _xml_error(raw: bytes) -> tuple:
    try:
        root = _parse_xml(raw)
        err = root.find(".//Error")
        if err is None:
            err = root
        return (_text(err, "Code") or _text(err, "code") or "", _text(err, "Message") or _text(err, "message") or "")
    except ET.ParseError:
        return "", ""


def _region_of(zone: str) -> str:
    return re.sub(r"[a-z]$", "", zone)


def _check_tags(tags: dict) -> dict:
    out = {}
    for key, value in tags.items():
        key, value = str(key), str(value)
        if not 1 <= len(key) <= 128 or key.lower().startswith("aws:") or len(value) > 256:
            raise ProviderError(f"EBS tag {key!r} is not allowed (keys 1-128 chars, no aws: prefix; values up to 256 chars)")
        out[key] = value
    return out


@dataclass(frozen=True)
class AwsVolume(Volume):
    """A ``Volume`` plus the EC2 status fields the waiters need."""

    state: str = ""
    attach_state: str | None = None
    devices: tuple = ()


def parse_volume(item: ET.Element) -> Volume:
    tags = {_text(t, "key") or "": _text(t, "value") or "" for t in _items(item, "tagSet")}
    attachments = [a for a in _items(item, "attachmentSet") if _text(a, "status") != "detached"]
    first = attachments[0] if attachments else None
    created = _parse_iso(_text(item, "createTime") or "")
    size = _text(item, "size")
    return AwsVolume(id=_text(item, "volumeId") or "", name=tags.get("Name", ""), size_gb=int(size) if size else None,
                 server_id=None if first is None else _text(first, "instanceId"),
                 labels={k: v for k, v in tags.items() if k != "Name" and not k.lower().startswith("aws:")},
                 delete_protected=False, created=created, location=_text(item, "availabilityZone"),
                 state=_text(item, "status") or "", attach_state=None if first is None else _text(first, "status"),
                 devices=tuple(d for d in (_text(a, "device") for a in attachments) if d))


