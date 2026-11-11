"""The provider registry: one row per cloud. To add a provider, add a ``ProviderSpec`` to ``SPECS``.

Everything that has to know "which providers exist" reads this table: ``host.provider`` validation in the config,
``motel-desk setup``, the installer's ``--provider`` choices, the doctor and ``desk.default_provider``. Provider
modules are imported only when one is built, so the desk never loads clouds it does not use.

Two ways to authenticate:

* ``token``: a secret in ``credentials/<name>.token`` (root-owned, 0600), typed once at ``setup``.
* ``instance``: the cloud's own identity for this machine (instance role, service account, managed identity).
  Nothing is typed or stored by motel.
"""

from __future__ import annotations

import importlib
import os
import re
from collections.abc import Callable
from dataclasses import dataclass, field
from decimal import Decimal

LOCAL = "local"  # not a cloud: loop files under [local] dir; handled by the desk itself


@dataclass(frozen=True)
class Option:
    """One key of the optional ``[provider]`` table, passed to the constructor as ``kwarg``."""

    kwarg: str
    choices: tuple = ()
    pattern: str = ""
    convert: Callable | None = None

    def problem(self, name: str, value) -> str | None:
        if not isinstance(value, str) or (self.choices and value not in self.choices) or \
                (self.pattern and not re.fullmatch(self.pattern, value)):
            allowed = " or ".join(f'"{c}"' for c in self.choices) if self.choices else f"text matching {self.pattern}"
            return f"provider.{name} must be {allowed}"
        return None

    def value(self, raw: str):
        return self.convert(raw) if self.convert else raw


@dataclass(frozen=True)
class ProviderSpec:
    name: str
    module: str
    cls: str
    label: str  # for prompts and messages: "DigitalOcean"
    auth: str = "token"  # "token" | "instance"
    token_hint: str = "an API token with read and write access to volumes and servers"
    usd_priced: bool = False  # the constructor takes eur_per_usd (rooms.eur_per_usd)
    options: dict = field(default_factory=dict)  # [provider] keys this cloud understands
    credentials_file: str | None = None  # instance clouds: optional root-owned file read instead of the instance identity
    beta: bool = True

    @property
    def needs_token(self) -> bool:
        return self.auth == "token"

    def token_prompt(self) -> str:
        return f"{self.label} API token ({self.token_hint}; hidden): "

    def load_class(self):
        return getattr(importlib.import_module(self.module), self.cls)


# Values mirror the provider modules (tests/test_registry.py checks it).
VULTR_BLOCK_TYPES = ("high_perf", "storage_opt")

SPECS: dict = {spec.name: spec for spec in (
    ProviderSpec("hetzner", "motel.providers.hetzner", "HetznerProvider", "Hetzner Cloud", beta=False,
                 token_hint="Read & Write"),
    ProviderSpec("digitalocean", "motel.providers.digitalocean", "DigitalOceanProvider", "DigitalOcean", usd_priced=True,
                 token_hint="custom scopes, see docs/providers/digitalocean.md"),
    ProviderSpec("linode", "motel.providers.linode", "LinodeProvider", "Linode", usd_priced=True,
                 token_hint="scopes volumes:read_write and linodes:read_write"),
    ProviderSpec("vultr", "motel.providers.vultr", "VultrProvider", "Vultr", usd_priced=True,
                 token_hint="API key limited to this host's address",
                 options={"block_type": Option("block_type", choices=VULTR_BLOCK_TYPES)}),
    ProviderSpec("scaleway", "motel.providers.scaleway", "ScalewayProvider", "Scaleway", token_hint="IAM secret key"),
    ProviderSpec("aws", "motel.providers.aws", "AwsProvider", "AWS", auth="instance",
                 credentials_file="aws.credentials"),
    ProviderSpec("gcp", "motel.providers.gcp", "GcpProvider", "Google Cloud", auth="instance"),
    ProviderSpec("azure", "motel.providers.azure", "AzureProvider", "Azure", auth="instance",
                 options={"sku": Option("sku", pattern=r"[A-Za-z0-9_]{1,40}"),
                          "client_id": Option("identity", pattern=r"[A-Za-z0-9-]{8,64}",
                                              convert=lambda v: {"client_id": v})}),
)}


def names() -> tuple:
    return tuple(SPECS)


def provider_names() -> tuple:
    """Every value ``host.provider`` accepts."""
    return names() + (LOCAL,)


def get(name: str) -> ProviderSpec:
    return SPECS[name]


def build(cfg, paths, token: str | None):
    """Construct the provider named by ``cfg.provider``. ``token`` is None for instance-identity clouds."""
    spec = SPECS[cfg.provider]
    kwargs: dict = {}
    if spec.usd_priced:
        kwargs["eur_per_usd"] = Decimal(str(cfg.eur_per_usd))
    for key, raw in cfg.provider_options.items():
        option = spec.options[key]
        kwargs[option.kwarg] = option.value(raw)
    cls = spec.load_class()
    if spec.needs_token:
        return cls(token or "", **kwargs)
    if spec.credentials_file:
        path = os.path.join(paths.credentials_dir, spec.credentials_file)
        if os.path.exists(path):
            loader = importlib.import_module(spec.module).load_credentials_file
            kwargs["credentials"] = loader(path)
    return cls(**kwargs)
