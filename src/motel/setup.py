"""``motel-desk setup``: discover this host, check the credentials, freeze the never-touch set.

A cloud API token is read only from an interactive terminal with echo off (or, for automation, from a root-owned
0600 file named with ``--token-file``). It is never taken from an argument, an environment variable or a pipe, and
never printed. Clouds that identify the machine themselves (instance role, service account, managed identity) need
no token at all.
"""

from __future__ import annotations

import getpass
import json
import os
import stat
import sys
import warnings

from motel import __version__
from motel.config import Config, Paths, Receipt, same_id
from motel.errors import Guard, MotelError, Refused, Usage
from motel.providers import registry
from motel.providers.base import VolumeProvider
from motel.state import SCHEMA, Store, atomic_write
from motel.units import iso, utc_now


def _hint(provider: str) -> str:
    return f"sudo motel-desk setup {provider}"


def read_token_tty(stdin=None, prompt: str | None = None, *, provider: str = "hetzner") -> str:
    stdin = stdin or sys.stdin
    prompt = prompt or registry.get(provider).token_prompt()
    if not stdin.isatty():
        raise Usage("setup reads the token only from an interactive terminal (never from a pipe, file or argument)",
                    _hint(provider))
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            token = getpass.getpass(prompt)
        except getpass.GetPassWarning as exc:
            raise Usage("this terminal cannot hide typed input, so the token prompt is refused",
                        _hint(provider)) from exc
    return _check_token_shape(token.strip(), provider)


def read_token_file(path: str, *, owner_uid: int = 0, provider: str = "hetzner") -> str:
    """Read a token for automation from a root-owned, non-symlink, regular file of mode 0600 or stricter.

    The descriptor is opened with O_NOFOLLOW and checked with fstat, so the file cannot be swapped between
    the check and the read. The content is never printed or put into an error message.
    """
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except OSError as exc:
        raise Usage(f"cannot open the token file {path!r} (a symlink or a missing file is refused): {exc.strerror}",
                    _hint(provider)) from exc
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise Usage(f"{path} is not a regular file", _hint(provider))
        if st.st_uid != owner_uid:
            raise Usage(f"{path} must be owned by root", f"sudo chown root:root {path}")
        if stat.S_IMODE(st.st_mode) & ~0o600:
            raise Usage(f"{path} must be mode 0600 or stricter", f"sudo chmod 600 {path}")
        if st.st_size > 4096:
            raise Usage(f"{path} is too large to be a token file", _hint(provider))
        with os.fdopen(os.dup(fd), encoding="utf-8", errors="strict") as handle:
            token = handle.read().strip()
    except UnicodeDecodeError as exc:
        raise Usage(f"{path} does not hold a token", _hint(provider)) from exc
    finally:
        os.close(fd)
    return _check_token_shape(token, provider)


def _check_token_shape(token: str, provider: str = "hetzner") -> str:
    if not 16 <= len(token) <= 200 or any(c.isspace() or not c.isprintable() for c in token):
        raise Usage("that does not look like an API token", _hint(provider))
    return token


def _write_secret(path: str, value: str) -> None:
    directory = os.path.dirname(path)
    os.makedirs(directory, mode=0o700, exist_ok=True)
    os.chmod(directory, 0o700)
    atomic_write(path, value + "\n", 0o600)


def _own_volume_ids(store: Store, inventory) -> set:
    """Volumes that belong to motel and must not be frozen as never-touch on a re-run.

    A volume counts when a floor record in ANY non-terminal state names it, or (for a create whose response
    was lost) when its unique floor label and name match a record that has no volume id yet.
    """
    own = set(store.live_volume_ids())
    pending = {f["id"]: f for f in store.floors() if not f.get("volume_id") and f.get("state") not in Store.TERMINAL_FLOOR_STATES}
    for vol in inventory.volumes:
        record = pending.get((vol.labels or {}).get("floor"))
        if record is not None and vol.name == record.get("name"):
            own.add(vol.id)
    return own


def _finish(paths: Paths, cfg: Config, provider_name: str, identity, inventory, previous: Receipt | None, clock,
            store: Store) -> dict:
    keep = _own_volume_ids(store, inventory)
    entries = {v.id: {"id": v.id, "name": v.name, "size_gb": v.size_gb} for v in inventory.volumes if v.id not in keep}
    if previous is not None:
        for vid in previous.never_touch:  # frozen: a re-run can add, never remove
            entries.setdefault(vid, next((e for e in previous.raw.get("never_touch", []) if str(e.get("id")) == vid), {"id": vid}))
    receipt = {"schema": SCHEMA, "motel_version": __version__, "provider": provider_name, "server_id": identity.server_id,
               "location": identity.location, "setup_at": iso(clock()),
               "never_touch": sorted(entries.values(), key=lambda e: str(e["id"])),
               "note": "Nonsecret. Volumes listed here existed before motel was set up and are never touched."}
    atomic_write(paths.receipt_file, json.dumps(receipt, indent=1, sort_keys=True) + "\n", 0o644)
    return receipt


def _previous(paths: Paths, server_id: str) -> Receipt | None:
    if not os.path.exists(paths.receipt_file):
        return None
    previous = Receipt.load(paths.receipt_file)
    if not same_id(previous.server_id, server_id):
        raise Guard(f"this host was set up for server {previous.server_id}, not {server_id}",
                    f"sudo rm {paths.receipt_file}  # only if you really moved this config to another server")
    return previous


def _require_root(paths: Paths, provider: str = "hetzner") -> None:
    if paths.strict and os.geteuid() != 0:
        raise Usage("setup must run as root", _hint(provider))


def setup_cloud(paths: Paths, provider_name: str, *, provider_factory, token_reader=None, clock=utc_now) -> dict:
    """Set this host up for the cloud ``provider_name`` (a row of ``motel.providers.registry``).

    ``provider_factory(token)`` builds the provider: ``""`` for the metadata-only identity probe, the real token
    afterwards, and ``None`` for a cloud that needs no token. ``token_reader()`` returns the token.
    """
    spec = registry.get(provider_name)
    _require_root(paths, provider_name)
    cfg = Config.load(paths.config_file)
    if cfg.provider != provider_name:
        raise Refused(f"{cfg.source} says provider = {cfg.provider!r}", "sudo motel-desk setup " + cfg.provider)
    if spec.needs_token:
        identity = provider_factory("").whoami()  # metadata only; no token involved yet
    else:
        provider = provider_factory(None)
        identity = provider.whoami()
    if cfg.server_id != "auto" and not same_id(cfg.server_id, identity.server_id):
        raise Guard(f"config names server {cfg.server_id} but this server is {identity.server_id}", "motel doctor")
    previous = _previous(paths, identity.server_id)
    token = None
    if spec.needs_token:
        token = (token_reader or (lambda: read_token_tty(provider=provider_name)))()
        provider: VolumeProvider = provider_factory(token)
    store = Store(paths.state_dir, clock, "setup")
    with store.locked():  # no checkin can create a volume between reading the inventory and freezing it
        inventory = provider.visible()  # raises InventoryIncomplete rather than freezing a partial list
        if identity.server_id not in inventory.servers:
            who = "this token" if spec.needs_token else "the identity of this machine"
            raise Refused(f"{who} cannot see server {identity.server_id}: it belongs to a different project or account; "
                          f"rooms need access to the {spec.label} project this server lives in", _hint(provider_name))
        if cfg.location not in ("auto", identity.location):
            raise Guard(f"config says location {cfg.location} but this server is in {identity.location}", "motel doctor")
        if token is not None:
            _write_secret(paths.token_file(provider_name), token)
        receipt = _finish(paths, cfg, provider_name, identity, inventory, previous, clock, store)
    return {"schema": SCHEMA, "ok": True, "provider": provider_name, "server_id": identity.server_id,
            "location": identity.location, "never_touch": len(receipt["never_touch"]),
            "other_servers_visible": len([s for s in inventory.servers if s != identity.server_id]),
            "receipt": paths.receipt_file}


def setup_hetzner(paths: Paths, *, provider_factory, token_reader=read_token_tty, clock=utc_now) -> dict:
    return setup_cloud(paths, "hetzner", provider_factory=provider_factory, token_reader=token_reader, clock=clock)


def setup_local(paths: Paths, *, provider_factory, clock=utc_now) -> dict:
    _require_root(paths)
    cfg = Config.load(paths.config_file)
    if cfg.provider != "local":
        raise Refused(f"{cfg.source} says provider = {cfg.provider!r}", "sudo motel-desk setup " + cfg.provider)
    provider = provider_factory()
    identity = provider.whoami()
    previous = _previous(paths, identity.server_id)
    store = Store(paths.state_dir, clock, "setup")
    with store.locked():
        receipt = _finish(paths, cfg, "local", identity, provider.visible(), previous, clock, store)
    return {"schema": SCHEMA, "ok": True, "provider": "local", "server_id": identity.server_id,
            "location": identity.location, "never_touch": len(receipt["never_touch"]), "receipt": paths.receipt_file}


__all__ = ["setup_cloud", "setup_hetzner", "setup_local", "read_token_tty", "MotelError"]
