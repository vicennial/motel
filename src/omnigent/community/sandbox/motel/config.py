"""The ``sandbox.motel`` config block, validated at server start (typos stop startup, not the first session)."""

from __future__ import annotations

import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, field_validator

ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,127}$")
# Variables that change how the shell or the dynamic loader behaves. Passing them through would let a config
# typo (or a hostile config) change what runs on the cabin before the host starts.
RESERVED_ENV = frozenset({"PATH", "HOME", "SHELL", "IFS", "ENV", "BASH_ENV", "PWD", "OLDPWD", "USER", "LOGNAME",
                          "PYTHONPATH", "PYTHONHOME", "PYTHONSTARTUP", "NODE_OPTIONS", "SHELLOPTS",
                          "BASHOPTS", "PROMPT_COMMAND", "PS4"})
RESERVED_PREFIXES = ("LD_", "BASH_FUNC_", "OMNIGENT_")


def reserved_env_name(name: str) -> bool:
    """Names that must never be passed through: they change how the shell, the loader, node or Omnigent itself start."""
    return name in RESERVED_ENV or name.startswith(RESERVED_PREFIXES)
VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+)*([a-z]+[0-9]*|\.?(dev|post|rc)[0-9]+)*$")
NPM_SPEC_RE = re.compile(r"^(@[a-z0-9][a-z0-9._-]*/)?[a-z0-9][a-z0-9._-]*(@[A-Za-z0-9][A-Za-z0-9._-]*)?$")
DURATION_RE = re.compile(r"^[1-9][0-9]{0,3}[mhd]$")
TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
IMAGE_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,38}[a-z0-9])?$")
TASK_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")

def release_version(version: str) -> bool:
    """A released version number (no dev or local build), safe to put in ``pip install omnigent==VERSION``."""
    return bool(VERSION_RE.fullmatch(version)) and "dev" not in version


TOKEN_MARGIN_S = 3600
HARD_MAX_LIFETIME_S = 14 * 24 * 3600  # motel never allows a cabin longer than this


def lease_ceiling_s(config_file: str | None = None) -> int:
    """The longest a cabin can live: ``[cabins] max_lifetime`` from motel's config, or motel's own hard ceiling (14d)
    when that file cannot be read or says nothing valid."""
    try:
        import tomllib

        from motel.cabins.config import CONFIG_FILE
        from motel.cabins.util import parse_duration

        with open(config_file or CONFIG_FILE, "rb") as handle:
            text = (tomllib.load(handle).get("cabins") or {}).get("max_lifetime", "7d")
        return min(int(parse_duration(str(text)).total_seconds()), HARD_MAX_LIFETIME_S)
    except Exception:
        return HARD_MAX_LIFETIME_S


def token_ttl_s(config_file: str | None = None) -> int:
    """Launch-token lifetime: the cabin's longest life plus a margin, so a live cabin can always re-authenticate its
    tunnel while a token from a long-deleted cabin cannot (the same rule as Omnigent's own providers)."""
    return lease_ceiling_s(config_file) + TOKEN_MARGIN_S


DEFAULT_HARNESS_PACKAGES = ("@anthropic-ai/claude-code", "@openai/codex")


class MotelSandboxConfig(BaseModel):
    """Everything is optional: ``omnigent sandbox create --provider motel`` has no config block at all."""

    model_config = ConfigDict(extra="forbid")

    motel_binary: str = "motel"
    """The ``motel`` CLI to run (a name on PATH or a path)."""
    server_type: str | None = None
    """Cabin server type; ``None`` uses the first type the operator allowed."""
    ttl: str = "2h"
    """Lease length at checkin, and the sliding window ``keep_alive`` pushes forward (``90m``, ``2h``, ``1d``)."""
    task: str = "omnigent"
    """Task label for cabins whose Omnigent name cannot be used as one."""
    disk_gb: int | None = None
    """Extra disk with the cabin's lifecycle, in GB."""
    env: list[str] | None = None
    """NAMES of environment variables to pass to the host. Values are read from the launching process's
    environment when the host starts. ``None`` falls back to ``OMNIGENT_MOTEL_SANDBOX_ENV`` (comma separated)."""
    tunnel: Literal["auto", "relay", "reverse-forward", "none"] = "auto"
    """How a cabin reaches a ``server_url`` on the controller's loopback. ``auto`` and ``relay`` carry the
    connection over the ssh session's stdio. ``reverse-forward`` uses ``ssh -R`` and needs a cabin whose sshd
    allows it (the stock cabin does not). ``none`` never tunnels (use a public ``server_url``)."""
    image: str | None = None
    """Name of an image the operator baked on this host (``motel cabin images`` lists them). Cabins start from it
    instead of the stock image. It does not choose ``bootstrap``: say ``bootstrap: none`` when the image already has
    omnigent."""
    bootstrap: Literal["pip", "none"] = "pip"
    """``pip`` installs a pinned omnigent into a venv on the cabin when it is missing. ``none`` expects omnigent
    already on the cabin's PATH."""
    omnigent_version: str = "auto"
    """Version to install on the cabin. ``auto`` is the version running here (host and server must match)."""
    harness_packages: list[str] = list(DEFAULT_HARNESS_PACKAGES)
    """npm packages for the coding-harness CLIs, installed with ``npm -g`` into the cabin user's prefix. Pin
    them (``@openai/codex@1.2.3``) for a reproducible cabin. An empty list installs none."""
    wait_for_harnesses: bool = False
    """``False`` installs node and the harness CLIs in the background after omnigent, so the host registers sooner
    (log on the cabin: /tmp/omnigent-harness-install.log). ``True`` waits for them first."""
    max_streams: int = 64
    """Most connections the tunnel carries at once. More are refused (the client sees an immediate close)."""
    command_timeout_s: float = 1800.0
    """Time limit for one remote command, including the omnigent install."""
    checkin_timeout_s: float = 1800.0
    """Time limit for ``motel cabin checkin``."""

    @field_validator("env")
    @classmethod
    def _env_names(cls, names):
        for name in names or []:
            if not ENV_NAME_RE.fullmatch(name) or reserved_env_name(name):
                raise ValueError(f"env entry {name!r} is not an allowed variable name (names only, never values)")
        return names

    @field_validator("ttl")
    @classmethod
    def _ttl(cls, value):
        if not DURATION_RE.fullmatch(value):
            raise ValueError(f"ttl {value!r}: use minutes, hours or days, such as 90m, 2h or 1d")
        return value

    @field_validator("server_type")
    @classmethod
    def _type(cls, value):
        if value is not None and not TYPE_RE.fullmatch(value):
            raise ValueError(f"server_type {value!r} is not a valid cabin type name")
        return value

    @field_validator("image")
    @classmethod
    def _image(cls, value):
        if value is not None and (not IMAGE_RE.fullmatch(value) or value.isdigit()):
            raise ValueError(f"image {value!r} is not a baked image name: use 1-40 lowercase letters, digits and hyphens "
                             "(see `motel cabin images`)")
        return value

    @field_validator("task")
    @classmethod
    def _task(cls, value):
        if not TASK_RE.fullmatch(value):
            raise ValueError("task must be 1-32 lowercase letters, digits or hyphens")
        return value

    @field_validator("omnigent_version")
    @classmethod
    def _version(cls, value):
        if value != "auto" and not VERSION_RE.fullmatch(value):
            raise ValueError(f"omnigent_version {value!r}: use 'auto' or a version such as 0.21.0")
        return value

    @field_validator("harness_packages")
    @classmethod
    def _packages(cls, values):
        for spec in values:
            if not NPM_SPEC_RE.fullmatch(spec):
                raise ValueError(f"harness_packages entry {spec!r} is not a plain npm package spec")
        return values

    @field_validator("max_streams")
    @classmethod
    def _streams(cls, value):
        if not 1 <= value <= 1024:
            raise ValueError("max_streams must be between 1 and 1024")
        return value

    @field_validator("disk_gb")
    @classmethod
    def _disk(cls, value):
        if value is not None and value <= 0:
            raise ValueError("disk_gb must be positive")
        return value
