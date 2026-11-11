"""Parsing and formatting helpers: durations, sizes, names, timestamps, money."""

from __future__ import annotations

import decimal
import hashlib
import ipaddress
import re
from datetime import UTC, datetime, timedelta

from motel.cabins.errors import Usage

SCHEMA = "motel/v1"
LEASE_SCHEMA = "motel-cabin/v1"

TASK_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
# Provider labels must start AND end with an alphanumeric character.
OWNER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9_.-]{0,38}[A-Za-z0-9])?$")
LEASE_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?-[0-9a-f]{12}$")
DURATION_RE = re.compile(r"^([0-9]{1,4})([mhd])$")
SIZE_RE = re.compile(r"^([0-9]{1,6})(?:([gGtT])[bB]?)?$")
TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
ED25519_PUB_RE = re.compile(r"^ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI[A-Za-z0-9+/]{43}$")
PRIVATE_KEY_RE = re.compile(r"^-----BEGIN OPENSSH PRIVATE KEY-----\n(?:[A-Za-z0-9+/=]{1,76}\n){1,40}"  # gitleaks:allow (a pattern, not a key)
                            r"-----END OPENSSH PRIVATE KEY-----\n$")
USER_RE = re.compile(r"^[a-z_][a-z0-9_-]{0,31}$")
PACKAGE_RE = re.compile(r"^[a-z0-9][a-z0-9.+-]{0,62}$")
EXPIRY_LABEL_RE = re.compile(r"^[0-9]{8}T[0-9]{6}Z$")


def utcnow() -> datetime:
    return datetime.now(UTC).replace(microsecond=0)


def iso(dt):
    return None if dt is None else dt.astimezone(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text):
    return None if not text else datetime.fromisoformat(text)


def expiry_label(dt) -> str:
    return dt.astimezone(UTC).strftime("%Y%m%dT%H%M%SZ")


def parse_duration(text, *, what="duration", maximum=None) -> timedelta:
    """'90m', '6h' or '3d' -> timedelta. ``maximum`` raises a usage error when exceeded."""
    match = DURATION_RE.fullmatch(text or "")
    if not match:
        raise Usage(f"bad {what} {text!r}: use e.g. 90m, 2h or 1d", "motel cabin run --for 2h -- make test")
    value, unit = int(match.group(1)), match.group(2)
    delta = {"m": timedelta(minutes=value), "h": timedelta(hours=value), "d": timedelta(days=value)}[unit]
    if delta <= timedelta(0):
        raise Usage(f"{what} must be positive")
    if maximum is not None and delta > maximum:
        from motel.cabins.errors import PolicyRefused
        raise PolicyRefused(f"{what} is at most {fmt_td(maximum)} per call")
    return delta


def parse_size_gb(text: str) -> int:
    """'200G', '200GB', '1T' (= 1024 GB) or a plain number of GB."""
    match = SIZE_RE.fullmatch(text or "")
    if not match:
        raise Usage(f"bad size {text!r}: use e.g. 200G, 1T or a plain number of GB", "motel cabin checkin --disk 200G")
    value = int(match.group(1)) * (1024 if (match.group(2) or "").lower() == "t" else 1)
    if value <= 0:
        raise Usage("size must be positive")
    return value


def fmt_td(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    if minutes and minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes and minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode()).hexdigest()


def money(value) -> str:
    return str(decimal.Decimal(value).quantize(decimal.Decimal("0.0001")))


def is_pos_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def host_cidr(value):
    """Canonical single-host CIDR ('203.0.113.10/32', '2001:db8::1/128') or None.

    Anything wider than one host, unspecified, loopback, link-local, multicast or reserved is refused.
    """
    if not isinstance(value, str):
        return None
    try:
        net = ipaddress.ip_network(value if "/" in value else value + ("/32" if ":" not in value else "/128"),
                                   strict=True)
    except ValueError:
        return None
    if net.prefixlen != net.max_prefixlen:
        return None
    addr = net.network_address
    if addr.is_unspecified or addr.is_loopback or addr.is_link_local or addr.is_multicast or addr.is_reserved:
        return None
    return str(net)
