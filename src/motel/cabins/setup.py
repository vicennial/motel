"""``motel-desk cabin setup``: identify this host, check the cabin credentials, write a nonsecret receipt.

Credentials are read only from an interactive terminal with echo off. They are never taken from an argument,
an environment variable, a file or a pipe, and never printed. Nothing is stored until every check passes.
"""

from __future__ import annotations

import getpass
import hashlib
import ipaddress
import json
import os
import re
import stat
import sys
import urllib.request
import warnings

from motel.cabins.config import CabinConfig, CabinPaths
from motel.cabins.errors import ApiError, Guarded, GuardRefused, Prerequisite, Usage
from motel.cabins.provider import GuardedApi, Transport, list_all, selector
from motel.cabins.store import Store, atomic_write
from motel.cabins.tailnet import TailnetApi, TailnetTransport, verify_policy
from motel.cabins.util import SCHEMA, host_cidr, iso, utcnow

METADATA = "http://169.254.169.254/hetzner/v1/metadata/"
# A host's public address is never in these ranges (documentation ranges are allowed so tests can use them).
NOT_PUBLIC_V4 = tuple(ipaddress.ip_network(n) for n in (
    "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8", "169.254.0.0/16", "172.16.0.0/12",
    "192.168.0.0/16", "198.18.0.0/15", "224.0.0.0/4", "240.0.0.0/4"))
IPV6_RE = re.compile(r"address:\s*['\"]?([0-9a-fA-F:]+:[0-9a-fA-F:]*)(?:/\d{1,3})?['\"]?")


def fetch_metadata(key: str) -> str:
    """Read-only link-local HTTP to this host's own Hetzner metadata service."""
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(METADATA + key, timeout=10) as response:
        return response.read(8192).decode("utf-8", "replace").strip()


def ipv6_from_network_config(text: str):
    """Best effort: the host's global IPv6 address from the metadata ``network-config`` document, or None."""
    for match in IPV6_RE.finditer(text or ""):
        try:
            addr = ipaddress.IPv6Address(match.group(1))
        except ValueError:
            continue
        if addr.is_global and not addr.is_link_local:
            return str(addr)
    return None


def discover_host(fetch=fetch_metadata, clock=utcnow) -> dict:
    """This host's identity from its metadata service. Raises a refusal rather than guessing."""
    try:
        instance = fetch("instance-id")
        public = fetch("public-ipv4")
    except (OSError, ValueError) as exc:
        raise Prerequisite(f"cannot read this host's metadata ({type(exc).__name__}); cabins need a Hetzner Cloud "
                           "host", "run setup on the host that will launch cabins") from None
    if not instance.isdigit():
        raise Prerequisite(f"unexpected instance id {instance!r} from the metadata service")
    if (host_cidr(public) is None or ipaddress.ip_address(public).version != 4
            or any(ipaddress.ip_address(public) in net for net in NOT_PUBLIC_V4)):
        raise Prerequisite("the metadata service did not report a usable public IPv4 address; cabins in "
                           "ssh-allowlist mode need one")
    zone = ""
    try:
        zone = fetch("availability-zone")
    except (OSError, ValueError):
        pass
    location = zone.split("-dc")[0] if re.fullmatch(r"[a-z0-9]+-dc\d+", zone) else None
    v6 = None
    try:
        v6 = ipv6_from_network_config(fetch("network-config"))
    except (OSError, ValueError):
        pass
    return {"server_id": int(instance), "public_ipv4": public, "public_ipv6": v6, "location": location,
            "verified_at": iso(clock()), "source": "hetzner metadata service"}


# --- hidden prompts ---------------------------------------------------------------------------

def read_secret(prompt: str, stream=None) -> str:
    stream = stream or sys.stdin
    if not stream.isatty():
        raise Usage("setup reads credentials only from an interactive terminal (never a pipe, file or argument)",
                    "sudo motel-desk cabin setup hetzner")
    try:
        with warnings.catch_warnings():
            warnings.simplefilter("error", getpass.GetPassWarning)
            value = getpass.getpass(prompt)
    except (getpass.GetPassWarning, EOFError, OSError):
        raise Usage("this terminal cannot hide typed input, so the prompt is refused",
                    "sudo motel-desk cabin setup hetzner") from None
    value = value.strip()
    if not value or len(value) > 200 or any(c.isspace() or not c.isprintable() for c in value):
        raise Usage("empty or malformed input; nothing changed", "sudo motel-desk cabin setup hetzner")
    return value


def read_token_file(path: str, *, owner_uid: int = 0) -> str:
    """Read a token for automation from a root-owned, non-symlink, regular file of mode 0600 or stricter.

    The descriptor is opened with O_NOFOLLOW and checked with fstat, so the file cannot be swapped between the
    check and the read. The content is never printed or put into an error message.
    """
    hint = "sudo motel-desk cabin setup hetzner"
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise Usage(f"cannot open the token file {path!r} (a symlink or a missing file is refused): {exc.strerror}",
                    hint) from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Usage(f"{path} is not a regular file", hint)
        if st.st_uid != owner_uid:
            raise Usage(f"{path} must be owned by root", f"sudo chown root:root {path}")
        if stat.S_IMODE(st.st_mode) & ~0o600:
            raise Usage(f"{path} must be mode 0600 or stricter", f"sudo chmod 600 {path}")
        if st.st_size > 4096:
            raise Usage(f"{path} is too large to be a token file", hint)
        with os.fdopen(os.dup(fd), encoding="utf-8", errors="strict") as handle:
            value = handle.read().strip()
    except UnicodeDecodeError:
        raise Usage(f"{path} does not hold a token", hint) from None
    finally:
        os.close(fd)
    if not 16 <= len(value) <= 200 or any(c.isspace() or not c.isprintable() for c in value):
        raise Usage("that does not look like an API token", hint)
    return value


def _write_secret(path: str, text: str) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    atomic_write(path, text, 0o600)


def load_receipt(paths: CabinPaths) -> dict:
    try:
        with open(paths.receipt_file, encoding="utf-8") as handle:
            doc = json.load(handle)
        return doc if isinstance(doc, dict) else {}
    except (OSError, ValueError):
        return {}


def save_receipt(paths: CabinPaths, doc: dict) -> None:
    doc["schema"] = "motel-cabins-setup/v1"
    doc["note"] = "Nonsecret. Written by motel-desk cabin setup."
    os.makedirs(os.path.dirname(paths.receipt_file), exist_ok=True)
    atomic_write(paths.receipt_file, json.dumps(doc, indent=1, sort_keys=True) + "\n", 0o644)


# --- hetzner ----------------------------------------------------------------------------------

def verify_hetzner(token: str, host: dict, store: Store, *, transport_factory=Transport) -> dict:
    """Read-only. The token must not see this host, and every server or volume in its project must carry
    motel cabin labels AND have a root-owned intent record here. Anything else means: not a dedicated project."""
    api = GuardedApi(transport_factory(token), host["server_id"])
    try:
        api.isolated_project()
        labels = selector(**{"managed-by": "motel", "role": "cabin", "controller": str(host["server_id"])})
        known = {r.get("lease_id") for r in store.all()}
        found = {kind: list_all(api, kind, labels) for kind in ("servers", "volumes")}
    except GuardRefused as exc:
        raise Guarded(f"refused, nothing changed: {exc}", "create a dedicated Hetzner project for cabins, "
                      "then: sudo motel-desk cabin setup hetzner") from None
    except ApiError as exc:
        raise Usage(f"the provider check failed (HTTP {exc.status}); nothing changed",
                    "sudo motel-desk cabin setup hetzner") from None
    unknown = [o for kind in found.values() for o in kind if (o.get("labels") or {}).get("lease") not in known]
    if unknown:
        raise Guarded(f"the project holds {len(unknown)} resource(s) with motel labels but no intent record on "
                      "this host; a dedicated, unshared cabin project is required; nothing changed",
                      "use a new Hetzner project for cabins")
    return {"verified_at": iso(utcnow()), "token_sha256": hashlib.sha256(token.encode()).hexdigest(),
            "protected_not_found": [host["server_id"]], "project_servers": len(found["servers"]),
            "project_volumes": len(found["volumes"])}


def setup_hetzner(paths: CabinPaths, cfg: CabinConfig, *, token_reader=read_secret, fetch=fetch_metadata,
                  transport_factory=Transport, clock=utcnow) -> dict:
    if paths.strict and os.geteuid() != 0:
        raise Usage("setup must run as root", "sudo motel-desk cabin setup hetzner")
    host = discover_host(fetch, clock)
    receipt = load_receipt(paths)
    old = receipt.get("host") or {}
    if old.get("server_id") not in (None, host["server_id"]):
        raise Guarded(f"this config was set up for server {old['server_id']}, not {host['server_id']}",
                      f"sudo rm {paths.receipt_file}  # only if you really moved this config to another server")
    token = token_reader("Hetzner token of the dedicated CABIN project (hidden): ")
    store = Store(paths.state_dir, clock, host["server_id"])
    result = verify_hetzner(token, host, store, transport_factory=transport_factory)
    _write_secret(paths.real(cfg.token_file), token + "\n")
    receipt.update(host=host, hetzner=result)
    save_receipt(paths, receipt)
    return {"schema": SCHEMA, "ok": True, "host_server_id": host["server_id"], "public_ipv4": host["public_ipv4"],
            "public_ipv6": host["public_ipv6"], "location": host["location"],
            "project_servers": result["project_servers"], "receipt": paths.receipt_file}


# --- tailscale --------------------------------------------------------------------------------

def setup_tailscale(paths: CabinPaths, cfg: CabinConfig, *, secret_reader=read_secret,
                    transport_factory=TailnetTransport, clock=utcnow) -> dict:
    if paths.strict and os.geteuid() != 0:
        raise Usage("setup must run as root", "sudo motel-desk cabin setup tailscale")
    if cfg.access != "tailscale" or cfg.tailscale is None:
        raise Prerequisite('cabins.access is not "tailscale"', 'set access = "tailscale" and [cabins.tailscale]')
    receipt = load_receipt(paths)
    if not (receipt.get("hetzner") or {}).get("verified_at"):
        raise Prerequisite("set up the cabin project first", "sudo motel-desk cabin setup hetzner")
    ts = cfg.tailscale
    client_id = secret_reader("Tailscale OAuth client ID (hidden): ")
    secret = secret_reader("Tailscale OAuth client secret (hidden): ")
    api = TailnetApi(transport_factory(client_id, secret), tag=ts.tag, tailnet=ts.tailnet,
                     protected_devices=ts.protected_device_ids)
    devices = verify_policy(api, ts)       # raises with a reason; stores nothing
    raw = json.dumps({"client_id": client_id, "client_secret": secret}, sort_keys=True)
    _write_secret(paths.real(ts.oauth_file), raw)
    receipt["tailscale"] = {"verified_at": iso(clock()), "credential_sha256": hashlib.sha256(raw.encode()).hexdigest(),
                            "tailnet": ts.tailnet, "tag": ts.tag, "devices_visible": len(devices)}
    save_receipt(paths, receipt)
    return {"schema": SCHEMA, "ok": True, "tailnet": ts.tailnet, "tag": ts.tag, "devices_visible": len(devices),
            "receipt": paths.receipt_file}
