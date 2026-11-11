"""The ``[cabins]`` section of ``/etc/motel/motel.toml`` and the cabin file layout.

The core config loader should call :func:`parse_section` for the ``cabins`` table instead of rejecting it, so
that there is one validation path. Cabins also read ``[host] allow_users`` (users and ``@groups``).
"""

from __future__ import annotations

import ipaddress
import os
import posixpath
import re
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta

from motel import billing
from motel.cabins.errors import ConfigError, Prerequisite
from motel.cabins.util import PACKAGE_RE, TYPE_RE, USER_RE, parse_duration
from motel.errors import INSTALL_HINT

CONFIG_FILE = "/etc/motel/motel.toml"
DESK = "/usr/local/sbin/motel-desk"
ACCESS_MODES = ("ssh-allowlist", "tailscale")
TAG_RE = re.compile(r"^tag:[a-z0-9][a-z0-9-]{0,60}$")
TS_ID_RE = re.compile(r"^[A-Za-z0-9]{4,64}$")
DEFAULT_DNS = ("185.12.64.1", "185.12.64.2")
MAX_TTL_PER_CALL = timedelta(hours=24)
MIN_IDLE = timedelta(minutes=30)          # idle may be made longer, never shorter
MAX_LIFETIME_LIMIT = timedelta(days=14)
CABINS_KEYS = {"enabled", "token_file", "access", "types", "default_ttl", "idle_timeout", "max_concurrent",
               "max_eur_per_hour", "location", "max_lifetime", "max_disk_gb", "image", "dns_resolvers",
               "guest_packages", "tailscale", "release_margin"}
TAILSCALE_KEYS = {"tag", "tailnet", "oauth_file", "controller_addresses", "controller_device_id",
                  "allowed_tag_owners", "protected_test_destinations", "protected_device_ids", "probe_ports"}


@dataclass(frozen=True)
class CabinPaths:
    """File layout. ``root`` is a prefix used by tests; the installed desk always uses ``/``."""

    root: str = "/"

    @property
    def strict(self) -> bool:
        return self.root == "/"

    def real(self, path: str) -> str:
        return path if self.root == "/" else os.path.join(self.root, path.lstrip("/"))

    @property
    def config_file(self) -> str:
        return self.real(CONFIG_FILE)

    @property
    def receipt_file(self) -> str:
        return self.real("/etc/motel/cabins-receipt.json")

    @property
    def state_dir(self) -> str:
        return self.real("/var/lib/motel/cabins")

    @property
    def keys_dir(self) -> str:
        """Controller keys live under the home of the unprivileged ``motel-cabin`` system user."""
        return self.real("/var/lib/motel-cabin/keys")

    @property
    def lib_dir(self) -> str:
        return os.path.dirname(os.path.abspath(__file__))


@dataclass(frozen=True)
class TailscaleConfig:
    tag: str
    tailnet: str
    oauth_file: str
    controller_addresses: tuple
    controller_device_id: str | None
    allowed_tag_owners: tuple
    protected_test_destinations: tuple
    protected_device_ids: tuple
    probe_ports: tuple


@dataclass(frozen=True)
class CabinConfig:
    enabled: bool
    token_file: str
    access: str
    types: tuple
    default_ttl: timedelta
    idle_timeout: timedelta
    max_concurrent: int
    max_eur_per_hour: float
    location: str                      # "auto" = this host's location, taken from the setup receipt
    max_lifetime: timedelta
    max_disk_gb: int
    image: str
    dns_resolvers: tuple
    guest_packages: tuple
    tailscale: TailscaleConfig | None
    allow_users: tuple = ()
    release_margin: timedelta = billing.DEFAULT_MARGIN
    source: str = ""
    raw: dict = field(default_factory=dict)


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def parse_section(data: dict, *, allow_users=(), source: str = "<dict>") -> CabinConfig:
    """Validate the ``[cabins]`` table (``data`` is the table itself). Raises ConfigError listing problems."""
    problems: list = []

    def get(tbl, key, kind, default, where):
        if key not in tbl:
            return default
        value = tbl[key]
        if kind == "number":
            ok = _is_int(value) or isinstance(value, float)
            ok = ok and not isinstance(value, bool)
        elif kind is int:
            ok = _is_int(value)
        else:
            ok = isinstance(value, kind) and (kind is bool or not isinstance(value, bool))
        if not ok:
            problems.append(f"{where}.{key} must be {kind if isinstance(kind, str) else kind.__name__}")
            return default
        return value

    def duration(tbl, key, default, where):
        text = get(tbl, key, str, default, where)
        try:
            return parse_duration(text, what=f"{where}.{key}")
        except Exception as exc:  # noqa: BLE001
            problems.append(getattr(exc, "message", str(exc)))
            return parse_duration(default)

    if not isinstance(data, dict):
        raise ConfigError(f"{source}: [cabins] must be a table")
    for unknown in sorted(set(data) - CABINS_KEYS):
        problems.append(f"unknown key cabins.{unknown}")
    enabled = get(data, "enabled", bool, False, "cabins")
    token_file = get(data, "token_file", str, "/etc/motel/credentials/cabins.token", "cabins")
    if not token_file.startswith("/") or posixpath.normpath(token_file) != token_file:
        problems.append("cabins.token_file must be a normalised absolute path")
    access = get(data, "access", str, "ssh-allowlist", "cabins")
    if access not in ACCESS_MODES:
        problems.append('cabins.access must be "ssh-allowlist" or "tailscale"')
    types = get(data, "types", list, [], "cabins")
    if enabled and not types:
        problems.append("cabins.types must list at least one server type (for example [\"cpx32\"])")
    for entry in types:
        if not (isinstance(entry, str) and TYPE_RE.fullmatch(entry)):
            problems.append(f"cabins.types entry {entry!r} is not a server type name")
    default_ttl = duration(data, "default_ttl", "2h", "cabins")
    idle = duration(data, "idle_timeout", "30m", "cabins")
    max_life = duration(data, "max_lifetime", "7d", "cabins")
    if default_ttl > MAX_TTL_PER_CALL:
        problems.append("cabins.default_ttl must not exceed 24h")
    if idle < MIN_IDLE:
        problems.append("cabins.idle_timeout must be at least 30m (a cabin is never reaped as idle sooner)")
    if max_life > MAX_LIFETIME_LIMIT:
        problems.append("cabins.max_lifetime must not exceed 14d")
    if default_ttl > max_life:
        problems.append("cabins.default_ttl must not exceed cabins.max_lifetime")
    margin = duration(data, "release_margin", "6m", "cabins")
    if not billing.MIN_MARGIN <= margin <= billing.MAX_MARGIN:
        problems.append("cabins.release_margin must be between 4m and 30m (an idle cabin is deleted that long before "
                        "the next billed hour starts)")
    max_conc = get(data, "max_concurrent", int, 4, "cabins")
    if not 1 <= max_conc <= 64:
        problems.append("cabins.max_concurrent must be between 1 and 64")
    cap = get(data, "max_eur_per_hour", "number", 1.0, "cabins")
    if cap <= 0:
        problems.append("cabins.max_eur_per_hour must be positive")
    location = get(data, "location", str, "auto", "cabins")
    if location != "auto" and not (location.replace("-", "").isalnum() and location.islower()):
        problems.append('cabins.location must be "auto" or a lowercase location name')
    max_disk = get(data, "max_disk_gb", int, 500, "cabins")
    if not 10 <= max_disk <= 10240:
        problems.append("cabins.max_disk_gb must be between 10 and 10240")
    image = get(data, "image", str, "ubuntu-24.04", "cabins")
    if not TYPE_RE.fullmatch(image.replace(".", "-")):
        problems.append("cabins.image must be an image name such as ubuntu-24.04")
    dns = get(data, "dns_resolvers", list, list(DEFAULT_DNS), "cabins")
    for ip in dns:
        try:
            if ipaddress.ip_address(ip).version != 4:
                raise ValueError
        except (ValueError, TypeError):
            problems.append("cabins.dns_resolvers must be IPv4 addresses")
            break
    if not dns:
        problems.append("cabins.dns_resolvers must not be empty")
    packages = get(data, "guest_packages", list, ["git", "rsync", "ca-certificates", "curl"], "cabins")
    if not all(isinstance(p, str) and PACKAGE_RE.fullmatch(p) for p in packages):
        problems.append("cabins.guest_packages must be package names")

    ts = None
    ts_table = data.get("tailscale")
    if access == "tailscale" or ts_table is not None:
        ts = _parse_tailscale(ts_table, problems, required=access == "tailscale")

    if problems:
        raise ConfigError(f"{source}: " + "; ".join(problems[:8]) + ("; ..." if len(problems) > 8 else ""),
                          "sudo motel-desk cabin doctor")
    return CabinConfig(
        enabled=enabled, token_file=token_file, access=access, types=tuple(types), default_ttl=default_ttl,
        idle_timeout=idle, max_concurrent=max_conc, max_eur_per_hour=float(cap), location=location,
        max_lifetime=max_life, max_disk_gb=max_disk, image=image, dns_resolvers=tuple(dns),
        guest_packages=tuple(packages), tailscale=ts, allow_users=tuple(allow_users), release_margin=margin,
        source=source, raw=dict(data))


def _parse_tailscale(table, problems, *, required) -> TailscaleConfig | None:
    if table is None:
        problems.append('cabins.access = "tailscale" needs a [cabins.tailscale] table')
        return None
    if not isinstance(table, dict):
        problems.append("cabins.tailscale must be a table")
        return None
    for unknown in sorted(set(table) - TAILSCALE_KEYS):
        problems.append(f"unknown key cabins.tailscale.{unknown}")
    tag = table.get("tag", "tag:motel-cabin")
    if not (isinstance(tag, str) and TAG_RE.fullmatch(tag)):
        problems.append("cabins.tailscale.tag must look like tag:motel-cabin")
    tailnet = table.get("tailnet", "-")
    if not (isinstance(tailnet, str) and re.fullmatch(r"[A-Za-z0-9.@_-]{1,100}", tailnet)):
        problems.append("cabins.tailscale.tailnet must be \"-\" or a tailnet name")
    oauth = table.get("oauth_file", "/etc/motel/credentials/cabins-tailscale.json")
    if not (isinstance(oauth, str) and oauth.startswith("/") and posixpath.normpath(oauth) == oauth):
        problems.append("cabins.tailscale.oauth_file must be a normalised absolute path")
    addrs = table.get("controller_addresses", [])
    from motel.cabins.tailnet import exact_tailnet_address
    if not (isinstance(addrs, list) and addrs and all(exact_tailnet_address(a) for a in addrs)):
        problems.append("cabins.tailscale.controller_addresses must list this host's exact tailnet addresses "
                        "(every address of its tailnet device)")
        addrs = []
    cid = table.get("controller_device_id")
    if cid is not None and not (isinstance(cid, str) and TS_ID_RE.fullmatch(cid)):
        problems.append("cabins.tailscale.controller_device_id must be a tailnet node ID")
    owners = table.get("allowed_tag_owners", [])
    if not (isinstance(owners, list) and owners and all(isinstance(o, str) and o for o in owners)):
        problems.append("cabins.tailscale.allowed_tag_owners must list who may own the cabin tag "
                        "(for example [\"autogroup:admin\"])")
        owners = []
    dests = table.get("protected_test_destinations", [])
    if not (isinstance(dests, list) and all(isinstance(d, str) and d for d in dests)):
        problems.append("cabins.tailscale.protected_test_destinations must be a list of strings")
        dests = []
    protected = table.get("protected_device_ids", [])
    if not (isinstance(protected, list) and all(isinstance(p, str) and TS_ID_RE.fullmatch(p) for p in protected)):
        problems.append("cabins.tailscale.protected_device_ids must be a list of node IDs")
        protected = []
    ports = table.get("probe_ports", [22, 80, 443, 3000, 5432, 8080])
    if not (isinstance(ports, list) and all(_is_int(x) and 0 < x < 65536 for x in ports) and 22 in ports):
        problems.append("cabins.tailscale.probe_ports must be ports including 22")
        ports = [22]
    return TailscaleConfig(tag=tag, tailnet=tailnet, oauth_file=oauth, controller_addresses=tuple(addrs),
                           controller_device_id=cid, allowed_tag_owners=tuple(owners),
                           protected_test_destinations=tuple(dests), protected_device_ids=tuple(protected),
                           probe_ports=tuple(sorted(set(ports))))


def parse_allow_users(host: dict) -> tuple:
    users = host.get("allow_users", []) if isinstance(host, dict) else []
    out = []
    for entry in users if isinstance(users, list) else []:
        name = entry[1:] if isinstance(entry, str) and entry.startswith("@") else entry
        if isinstance(name, str) and USER_RE.fullmatch(name):
            out.append(entry)
    return tuple(out)


def load_config(path: str, *, require_enabled: bool = True) -> CabinConfig:
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise Prerequisite(f"{path} does not exist", INSTALL_HINT) from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise ConfigError(f"cannot read {path}: {exc}") from exc
    section = data.get("cabins")
    if section is None:
        raise Prerequisite(f"{path} has no [cabins] section: cabins are off",
                           "add [cabins] with enabled = true, then: sudo motel-desk cabin setup hetzner")
    cfg = parse_section(section, allow_users=parse_allow_users(data.get("host", {})), source=path)
    if require_enabled and not cfg.enabled:
        raise Prerequisite("cabins are disabled in the config", "set enabled = true in [cabins]")
    return cfg


CLEANUP_SKIP = {"enabled", "types", "tailscale", "access"}


def load_cleanup_config(path: str) -> CabinConfig:
    """Config for cleaning up existing leases (reap, checkout), which must work even when new checkins are
    refused: a missing ``[cabins]`` table, ``enabled = false`` or an invalid unrelated key must not strand a
    running server. Every key is validated on its own; one that fails falls back to its default. The result
    is never used to create anything (the controller's checkin needs ``load_config`` to have succeeded).
    """
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        data = {}
    table = data.get("cabins") if isinstance(data.get("cabins"), dict) else {}
    users = parse_allow_users(data.get("host", {}))
    try:
        return parse_section(table, allow_users=users, source=path) if table else \
            parse_section({"enabled": False}, allow_users=users, source=path)
    except ConfigError:
        pass
    good: dict = {"enabled": False}
    for key, value in table.items():
        if key in CLEANUP_SKIP or key not in CABINS_KEYS:
            continue
        try:
            parse_section({"enabled": False, key: value}, allow_users=users)
        except ConfigError:
            continue
        good[key] = value
    cfg = parse_section(good, allow_users=users, source=path)
    if table.get("access") == "tailscale" and isinstance(table.get("tailscale"), dict):
        try:
            return parse_section({**good, "access": "tailscale", "tailscale": table["tailscale"]},
                                 allow_users=users, source=path)
        except ConfigError:
            pass
    return cfg
