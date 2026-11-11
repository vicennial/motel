"""Paths, ``motel.toml`` loading with strict validation, and the setup receipt."""

from __future__ import annotations

import json
import os
import posixpath
import re
import stat
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta

from motel import billing
from motel.errors import INSTALL_HINT, ConfigError, Guard, NotSetUp
from motel.providers import registry
from motel.units import USER_RE, parse_duration

CONFIG_FILE = "/etc/motel/motel.toml"
DEFAULT_LOCAL_DIR = "/var/lib/motel/local"
BLOCKED_TOP = {"etc", "usr", "bin", "sbin", "lib", "lib32", "lib64", "boot", "proc", "sys", "dev", "root", "home", "run"}


@dataclass(frozen=True)
class Paths:
    """Filesystem layout. ``root`` is a prefix used by tests and by ``install.sh --prefix``.

    The installed desk always uses ``root="/"``; no environment variable changes it.
    """

    root: str = "/"

    @property
    def strict(self) -> bool:
        """Ownership checks apply only on the real filesystem."""
        return self.root == "/"

    def real(self, path: str) -> str:
        return path if self.root == "/" else os.path.join(self.root, path.lstrip("/"))

    @property
    def config_dir(self) -> str:
        return self.real("/etc/motel")

    @property
    def config_file(self) -> str:
        return self.real(CONFIG_FILE)

    @property
    def receipt_file(self) -> str:
        return self.real("/etc/motel/setup-receipt.json")

    @property
    def credentials_dir(self) -> str:
        return self.real("/etc/motel/credentials")

    def token_file(self, provider: str) -> str:
        return os.path.join(self.credentials_dir, f"{provider}.token")

    @property
    def state_dir(self) -> str:
        return self.real("/var/lib/motel")


# Provider ids: numbers (Hetzner), UUIDs (Vultr, DigitalOcean, Scaleway), "i-0abc" (AWS), VM and disk names (Azure).
PROVIDER_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,79}")  # Azure names are up to 80 characters


def _is_provider_id(value) -> bool:
    return isinstance(value, str) and PROVIDER_ID_RE.fullmatch(value) is not None


def same_id(a, b) -> bool:
    """Provider ids compare without case, because Azure resource names do: two names that differ only in case are one
    resource. The checks that ask "is this the same server?" need that. The never-touch check in the guard folds case
    too, where a generous match only protects more volumes. Other clouds use numeric, UUID or lowercase ids, so
    folding changes nothing there."""
    return str(a).casefold() == str(b).casefold()


def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


@dataclass(frozen=True)
class Config:
    provider: str
    server_id: str  # "auto" or a provider id
    location: str  # "auto" or a location name
    allow_users: tuple
    never_touch: frozenset
    mount_root: str
    filesystem: str
    default_ttl: timedelta
    max_ttl_call: timedelta
    max_lifetime: timedelta
    grace: timedelta
    lost_and_found: timedelta
    warn_before: timedelta
    retire_after: timedelta
    min_floor_gb: int
    max_floors: int
    max_floor_gb: int
    max_total_gb: int
    monthly_eur: float
    price_gb_month_eur: float | None
    local_dir: str = DEFAULT_LOCAL_DIR
    source: str = ""
    eur_per_usd: float = 1.0  # exchange rate for clouds that price in dollars
    provider_options: dict = field(default_factory=dict)  # the optional [provider] table, validated per cloud
    release_margin: timedelta = timedelta(minutes=6)  # an empty shared floor is released this long before a billed hour starts
    cabins: object = None  # motel.cabins.config.CabinConfig when a [cabins] table is present and valid
    cabins_problem: str | None = None  # why it is not: rooms still work; doctor and the cabin verbs report it
    tours: object = None  # motel.tours.config.Limits when a valid [tours] table is present
    tours_problem: str | None = None  # why [tours] is unusable (rooms and cleanup keep working; the doctor reports it)

    @classmethod
    def load(cls, path: str) -> "Config":
        try:
            with open(path, "rb") as handle:
                data = tomllib.load(handle)
        except FileNotFoundError:
            raise NotSetUp(f"{path} does not exist", INSTALL_HINT) from None
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read {path}: {exc}") from exc
        return cls.from_dict(data, source=path)

    @classmethod
    def from_dict(cls, data: dict, source: str = "<dict>") -> "Config":
        problems: list = []

        def table(parent: dict, key: str, allowed: set, where: str) -> dict:
            value = parent.get(key, {})
            if not isinstance(value, dict):
                problems.append(f"{where} must be a table")
                return {}
            for unknown in sorted(set(value) - allowed):
                problems.append(f"unknown key {where}.{unknown}")
            return value

        def get(tbl: dict, key: str, kind, default, where: str):
            if key not in tbl:
                return default
            value = tbl[key]
            if kind == "number":
                ok = _is_int(value) or isinstance(value, float)
            elif kind is int:
                ok = _is_int(value)
            else:
                ok = isinstance(value, kind) and not isinstance(value, bool)
            if not ok:
                problems.append(f"{where}.{key} must be {kind if isinstance(kind, str) else kind.__name__}")
                return default
            return value

        def duration(tbl: dict, key: str, default: str, where: str, zero: bool = False) -> timedelta:
            text = get(tbl, key, str, default, where)
            try:
                return parse_duration(text, allow_zero=zero, what=f"{where}.{key}")
            except Exception as exc:  # noqa: BLE001
                problems.append(getattr(exc, "message", str(exc)))
                return parse_duration(default, allow_zero=True)

        for unknown in sorted(set(data) - {"host", "rooms", "local", "cabins", "provider", "tours"}):
            problems.append(f"unknown section [{unknown}]")
        host = table(data, "host", {"provider", "server_id", "location", "allow_users", "never_touch"}, "host")
        rooms = table(data, "rooms", {"mount_root", "filesystem", "default_ttl", "max_ttl_call", "max_lifetime", "grace",
                                      "lost_and_found", "min_floor_gb", "warn_before", "retire_after",
                                      "release_margin", "price_gb_month_eur", "eur_per_usd", "caps"}, "rooms")
        caps = table(rooms, "caps", {"max_floors", "max_floor_gb", "max_total_gb", "monthly_eur"}, "rooms.caps")
        local = table(data, "local", {"dir"}, "local")

        provider = get(host, "provider", str, "", "host")
        if provider not in registry.provider_names():
            problems.append("host.provider must be one of " + ", ".join(f'"{n}"' for n in registry.provider_names()))
        spec = registry.SPECS.get(provider)
        options_in = table(data, "provider", set().union(*(set(sp.options) for sp in registry.SPECS.values())), "provider")
        provider_options = {}
        for key, value in options_in.items():
            if spec is None or key not in spec.options:
                problems.append(f"provider.{key} does not apply to host.provider {provider!r}")
                continue
            problem = spec.options[key].problem(key, value)
            if problem:
                problems.append(problem)
            else:
                provider_options[key] = value
        server_id = host.get("server_id", "auto")
        if _is_int(server_id) and server_id > 0:
            server_id = str(server_id)
        elif not (server_id == "auto" or _is_provider_id(server_id)):
            problems.append("host.server_id must be \"auto\" or a provider server id")
            server_id = "auto"
        location = get(host, "location", str, "auto", "host")
        if location != "auto" and not (location.replace("-", "").isalnum() and location.islower()):
            problems.append("host.location must be \"auto\" or a lowercase location name")
        users = get(host, "allow_users", list, [], "host")
        if not users:
            problems.append("host.allow_users must list at least one user or @group")
        for entry in users:
            name = entry[1:] if isinstance(entry, str) and entry.startswith("@") else entry
            if not (isinstance(name, str) and USER_RE.fullmatch(name)):
                problems.append(f"host.allow_users entry {entry!r} is not a user name or @group")
        never = get(host, "never_touch", list, [], "host")
        never_ids = set()
        for entry in never:
            if (_is_int(entry) and entry > 0) or _is_provider_id(entry):
                never_ids.add(str(entry))
            else:
                problems.append(f"host.never_touch entry {entry!r} is not a volume id")

        mount_root = get(rooms, "mount_root", str, "/mnt/motel", "rooms")
        norm = posixpath.normpath(mount_root) if mount_root else ""
        parts = [p for p in norm.split("/") if p]
        if (not mount_root.startswith("/") or norm != mount_root.rstrip("/") or len(parts) < 2
                or parts[0] in BLOCKED_TOP or norm.startswith("/var/lib/motel")):
            problems.append("rooms.mount_root must be a dedicated, normalised absolute directory such as /mnt/motel")
        filesystem = get(rooms, "filesystem", str, "ext4", "rooms")
        if filesystem != "ext4":
            problems.append("rooms.filesystem must be \"ext4\" (xfs with quotas arrives in a later release)")
        default_ttl = duration(rooms, "default_ttl", "6h", "rooms")
        max_ttl_call = duration(rooms, "max_ttl_call", "7d", "rooms")
        max_lifetime = duration(rooms, "max_lifetime", "14d", "rooms")
        grace = duration(rooms, "grace", "6h", "rooms", zero=True)
        lost = duration(rooms, "lost_and_found", "24h", "rooms", zero=True)
        warn = duration(rooms, "warn_before", "2h", "rooms", zero=True)
        retire = duration(rooms, "retire_after", "15m", "rooms", zero=True)
        margin = duration(rooms, "release_margin", "6m", "rooms")
        if not billing.MIN_MARGIN <= margin <= billing.MAX_MARGIN:
            problems.append("rooms.release_margin must be between 4m and 30m")
        if default_ttl > max_ttl_call:
            problems.append("rooms.default_ttl must not exceed rooms.max_ttl_call")
        if max_ttl_call > max_lifetime:
            problems.append("rooms.max_ttl_call must not exceed rooms.max_lifetime")
        min_floor = get(rooms, "min_floor_gb", int, 50, "rooms")
        max_floors = get(caps, "max_floors", int, 3, "rooms.caps")
        max_floor = get(caps, "max_floor_gb", int, 500, "rooms.caps")
        max_total = get(caps, "max_total_gb", int, 1000, "rooms.caps")
        monthly = get(caps, "monthly_eur", "number", 30, "rooms.caps")
        price = get(rooms, "price_gb_month_eur", "number", None, "rooms")
        eur_per_usd = get(rooms, "eur_per_usd", "number", 1.0, "rooms")
        if eur_per_usd <= 0:
            problems.append("rooms.eur_per_usd must be greater than 0")
        for name, value in (("rooms.min_floor_gb", min_floor), ("rooms.caps.max_floors", max_floors),
                            ("rooms.caps.max_floor_gb", max_floor), ("rooms.caps.max_total_gb", max_total)):
            if value < 1:
                problems.append(f"{name} must be at least 1")
        if min_floor > max_floor:
            problems.append("rooms.min_floor_gb must not exceed rooms.caps.max_floor_gb")
        if max_floor > max_total:
            problems.append("rooms.caps.max_floor_gb must not exceed rooms.caps.max_total_gb")
        if monthly < 0 or (price is not None and price < 0):
            problems.append("euro amounts must not be negative")
        local_dir = get(local, "dir", str, DEFAULT_LOCAL_DIR, "local")
        if not local_dir.startswith("/") or posixpath.normpath(local_dir) != local_dir.rstrip("/"):
            problems.append("local.dir must be a normalised absolute path")

        cabins, cabins_problem = None, None
        if "cabins" in data:  # leniently: a typo in [cabins] must never stop rooms housekeeping
            cabins, cabins_problem = _parse_cabins(data, users)

        tours, tours_problem = None, None
        if "tours" in data:
            tours, tours_problem = _parse_tours(data)      # lenient: a bad [tours] must not stop rooms or cleanup

        if problems:
            raise ConfigError(f"{source}: " + "; ".join(problems[:8]) + ("; ..." if len(problems) > 8 else ""),
                              "motel doctor")
        return cls(provider=provider, server_id=server_id, location=location, allow_users=tuple(users),
                   never_touch=frozenset(never_ids), mount_root=mount_root.rstrip("/"), filesystem=filesystem,
                   default_ttl=default_ttl, max_ttl_call=max_ttl_call, max_lifetime=max_lifetime, grace=grace,
                   lost_and_found=lost, warn_before=warn, retire_after=retire, min_floor_gb=min_floor,
                   max_floors=max_floors, max_floor_gb=max_floor, max_total_gb=max_total, monthly_eur=float(monthly),
                   price_gb_month_eur=None if price is None else float(price), local_dir=local_dir.rstrip("/"),
                   source=source, eur_per_usd=float(eur_per_usd), provider_options=provider_options, cabins=cabins,
                   release_margin=margin,
                   cabins_problem=cabins_problem, tours=tours, tours_problem=tours_problem)


def _parse_cabins(data: dict, users: list) -> tuple:
    """(CabinConfig or None, problem text or None). Validated through the cabins module (one validation path), but a
    bad table is reported rather than raised: the rooms must keep working. Cabin verbs have their own strict
    and cleanup loaders."""
    from motel.cabins.config import parse_section
    from motel.cabins.errors import Fail
    try:
        return parse_section(data["cabins"], allow_users=tuple(u for u in users if isinstance(u, str)), source="[cabins]"), None
    except Fail as exc:
        return None, exc.message


def _parse_tours(data: dict):
    """Validate the ``[tours]`` table through the concierge part (one validation path). Returns (limits, problem): an
    invalid table is NOT a config error here, because it must not take rooms (or the cleanup of running tours) down
    with it. Starting a tour reads the table strictly, and ``motel doctor`` reports the problem."""
    from motel.tours.config import Limits
    try:
        return Limits.from_table(data["tours"]), None
    except Exception as exc:  # noqa: BLE001  the concierge part owns its own error type
        return None, "[tours] " + (getattr(exc, "message", None) or f"is invalid ({type(exc).__name__})")


@dataclass(frozen=True)
class Receipt:
    """The nonsecret record of what setup discovered and froze."""

    provider: str
    server_id: str
    location: str
    never_touch: frozenset = field(default_factory=frozenset)
    setup_at: str = ""
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str) -> "Receipt":
        try:
            with open(path, encoding="utf-8") as handle:
                doc = json.load(handle)
            return cls(provider=str(doc["provider"]), server_id=str(doc["server_id"]), location=str(doc["location"]),
                       never_touch=frozenset(str(v["id"]) for v in doc.get("never_touch", [])),
                       setup_at=str(doc.get("setup_at", "")), raw=doc)
        except FileNotFoundError:
            raise NotSetUp(f"{path} does not exist: this host is not set up") from None
        except (OSError, ValueError, KeyError, TypeError) as exc:
            raise ConfigError(f"setup receipt {path} is unreadable: {exc}") from exc


def check_root_owned(path: str, *, secret: bool = False) -> None:
    """Refuse a config or credential file that a non-root user could have written."""
    st = os.stat(path)
    bad_bits = 0o077 if secret else 0o022
    if st.st_uid != 0 or stat.S_IMODE(st.st_mode) & bad_bits:
        want = "root-owned and 0600" if secret else "root-owned and not group/world writable"
        raise Guard(f"{path} must be {want}", "sudo chown root:root " + path)
