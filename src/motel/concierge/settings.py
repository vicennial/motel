"""The ``[tours]`` section of ``/etc/motel/motel.toml`` as the concierge reads it.

Public seam for the engine part (``config.py`` integration)::

    from motel.concierge.settings import parse_tours_section
    tours = parse_tours_section(data.get("tours", {}))      # data = tomllib.load(...)

``parse_tours_section(table: dict, *, strict: bool = False) -> TourConfig``
    ``table`` is the already-parsed ``[tours]`` table (an empty dict means "all defaults, disabled"). The function
    validates every value, collects every problem and raises :class:`motel.concierge.errors.ConfigError` once, listing
    them all. Unknown keys directly under ``[tours]`` are ignored unless ``strict`` (they belong to the engine); unknown
    keys inside a binding are always an error, because a mistyped security key must never be silently dropped.

Keys read (contract defaults in ``docs/design/tours-contract.md``):

- ``enabled``, ``concierge_port`` (the fixed port on the guest; the controller no longer listens on it), ``concierge_port_range``
  (``"17800-17999"``: the controller loopback ports for per-attempt listeners), ``max_concurrent_guests``, ``max_guests_per_tour``, ``max_eur_per_tour``,
  ``max_usd_model_per_guest``, ``max_ttl``, ``default_image``, ``default_server_type``, ``max_result_mb``,
  ``egress_allow``.
- Concierge-only extras, optional: ``concierge_max_request_mb`` (default 8) and ``concierge_upstream_timeout_s``
  (idle read timeout towards the upstream, default 300), ``concierge_max_buffered_mb`` (memory charged for request bodies
  across all guests, default 192; charged by the real size of the decoded text, at most 14x the body plus 200 bytes per
  JSON node, and it must hold one such request at the caps: 14x the request cap plus 200 x ``concierge_max_json_nodes``) and
  ``concierge_max_json_nodes`` (JSON keys plus values one request body may hold, default 200000).
- ``[tours.bindings.NAME]``: ``upstream``, ``auth``, ``key_file``, ``paths`` (as in the contract) and the optional
  ``methods`` (default ``["POST"]``), ``format`` (``"anthropic"`` or ``"openai"``; default from ``auth``),
  ``max_concurrent`` (requests in flight to this upstream host, default 16), ``default_max_output_tokens`` (reserved for
  a request that states no output cap, ``openai`` format only; default 32768), ``betas`` (``anthropic-beta`` /
  ``openai-beta`` values a guest may send; default none), ``allow`` (request features the binding permits; default none,
  see ``motel.concierge.policy``), ``profile`` (``"claude-code"`` or ``"codex"``: the betas and features that CLI needs
  for a plain print-mode run, added to ``betas`` and ``allow``; see ``motel.concierge.profiles``), ``default_price`` and ``pricing."model-prefix"`` (``input``, ``output``, ``cache_read``, ``cache_write``, ``cache_write_1h``; USD per
  million tokens).
"""

from __future__ import annotations

import ipaddress
import re
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from motel.concierge.errors import ConfigError
from motel.concierge.policy import FEATURES
from motel.concierge.pricing import DEFAULT_PRICE, Price
from motel.concierge.profiles import PROFILES, merged
from motel.units import parse_duration

BINDING_RE = re.compile(r"^[a-z][a-z0-9_-]{0,31}$")
PATH_RE = re.compile(r"^/[A-Za-z0-9._~/-]*$")
AUTH_STYLES = ("x-api-key", "bearer")
FORMATS = ("anthropic", "openai")
METHODS = ("GET", "POST")
TOP_KEYS = {"enabled", "concierge_port", "max_concurrent_guests", "max_guests_per_tour", "max_eur_per_tour",
            "max_usd_model_per_guest", "max_ttl", "default_image", "default_server_type", "max_result_mb", "egress_allow",
            "concierge_port_range", "concierge_max_request_mb", "concierge_upstream_timeout_s", "concierge_max_buffered_mb", "concierge_max_json_nodes",
            "bindings"}
BINDING_KEYS = {"upstream", "auth", "key_file", "paths", "methods", "format", "max_concurrent", "default_max_output_tokens",
                "default_price", "pricing", "betas", "allow", "profile"}
TOOL_ALLOW_RE = re.compile(r"^tool:[A-Za-z0-9_.-]{1,64}$")
# What a request body costs in memory, charged in steps (see ``proxy.ProxyHandler._plan``): the raw bytes and the re-encoded copy
# (RAW_COPIES), the decoded text, the strings of the parsed tree and the re-serialised text (TEXT_COPIES), each at the size its
# Python string kind needs (1 byte per character for ASCII and Latin-1, 2 up to U+FFFF, 4 beyond: one emoji makes the whole
# text 4 bytes per character), and NODE_BYTES for each parsed key or value.
RAW_COPIES = 2
TEXT_COPIES = 3
WIDEST_CHAR_BYTES = 4
NODE_BYTES = 200
BODY_CHARGE_FACTOR = RAW_COPIES + TEXT_COPIES * WIDEST_CHAR_BYTES  # 14: the most a body of n bytes is charged, before nodes


def worst_case_charge(max_request_bytes: int, max_json_nodes: int) -> int:
    """The most memory one request can be charged: the body at its widest string kind, plus every parsed node."""
    return BODY_CHARGE_FACTOR * max_request_bytes + NODE_BYTES * max_json_nodes


PORT_RANGE_RE = re.compile(r"^\s*([0-9]{1,5})\s*-\s*([0-9]{1,5})\s*$")
DEFAULT_PORT_RANGE = (17800, 17999)
MAX_PORT_RANGE = 512  # ports one range may span: the unit's FileDescriptorStoreMax= holds one socket per attempt
PRICE_KEYS = {"input", "output", "cache_read", "cache_write", "cache_write_1h"}


@dataclass(frozen=True)
class BindingConfig:
    name: str
    upstream: str  # normalised origin, no trailing slash
    scheme: str
    host: str
    port: int
    auth: str
    key_file: str
    paths: tuple[str, ...]
    methods: tuple[str, ...] = ("POST",)
    format: str = "anthropic"
    max_concurrent: int = 16
    default_max_output_tokens: int = 32768
    pricing: dict = field(default_factory=dict)  # model prefix -> Price
    default_price: Price = DEFAULT_PRICE
    betas: tuple[str, ...] = ()  # anthropic-beta / openai-beta values a guest may send
    allow: frozenset = frozenset()  # gated features this binding permits (motel.concierge.policy.FEATURES)
    profile: str = ""  # a named profile (motel.concierge.profiles); ``betas`` and ``allow`` already include its lists

    @property
    def host_key(self) -> str:
        """Concurrency is limited per upstream host, however many bindings point at it."""
        return f"{self.host}:{self.port}"

    def path_allowed(self, path: str) -> bool:
        for entry in self.paths:
            if entry.endswith("/*"):
                prefix = entry[:-1]  # keeps the trailing slash: "/v1/models/*" allows "/v1/models/x", not "/v1/modelsx"
                if path.startswith(prefix) and len(path) > len(prefix):
                    return True
            elif path == entry:
                return True
        return False


@dataclass(frozen=True)
class TourConfig:
    enabled: bool = False
    concierge_port: int = 7781  # the fixed port on the guest; the engine forwards it to the attempt's own port (see endpoints)
    concierge_port_range: tuple[int, int] = DEFAULT_PORT_RANGE  # controller loopback ports the per-attempt listeners use
    max_concurrent_guests: int = 4
    max_guests_per_tour: int = 20
    max_eur_per_tour: float = 5.0
    max_usd_model_per_guest: float = 2.0
    max_ttl_s: int = 4 * 3600
    default_image: str = "ubuntu-24.04"
    default_server_type: str = ""
    max_result_mb: int = 64
    egress_allow: tuple[str, ...] = ()
    concierge_max_request_mb: float = 8.0
    concierge_upstream_timeout_s: float = 300.0
    concierge_max_buffered_mb: float = 192.0
    concierge_max_json_nodes: int = 200_000
    bindings: dict = field(default_factory=dict)  # name -> BindingConfig

    @property
    def max_request_bytes(self) -> int:
        return int(self.concierge_max_request_mb * 1024 * 1024)

    @property
    def max_buffered_bytes(self) -> int:
        return int(self.concierge_max_buffered_mb * 1024 * 1024)


def _is_loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def parse_tours_section(table: dict, *, strict: bool = False) -> TourConfig:
    """Validate the ``[tours]`` table. See the module docstring."""
    problems: list[str] = []
    if table is None:
        table = {}
    if not isinstance(table, dict):
        raise ConfigError("[tours] must be a table")

    def number(tbl, key, default, where, *, integer=False, low=None, high=None):
        if key not in tbl:
            return default
        value = tbl[key]
        ok = isinstance(value, int | float) and not isinstance(value, bool) and value == value
        if ok and integer:
            ok = isinstance(value, int)
        if ok and ((low is not None and value < low) or (high is not None and value > high)):
            ok = False
        if not ok:
            kind = "an integer" if integer else "a number"
            rng = f" between {low} and {high}" if low is not None and high is not None else (f" >= {low}" if low is not None else "")
            problems.append(f"{where}.{key} must be {kind}{rng}")
            return default
        return value

    def text(tbl, key, default, where):
        if key not in tbl:
            return default
        if not isinstance(tbl[key], str):
            problems.append(f"{where}.{key} must be a string")
            return default
        return tbl[key]

    if strict:
        for key in sorted(set(table) - TOP_KEYS):
            problems.append(f"[tours] has an unknown key {key!r}")
    enabled = table.get("enabled", False)
    if not isinstance(enabled, bool):
        problems.append("tours.enabled must be true or false")
        enabled = False
    ttl_text = text(table, "max_ttl", "4h", "tours")
    try:
        max_ttl_s = int(parse_duration(ttl_text, what="tours.max_ttl").total_seconds())
    except Exception as exc:  # noqa: BLE001
        problems.append(getattr(exc, "message", str(exc)))
        max_ttl_s = 4 * 3600
    egress = table.get("egress_allow", [])
    if not isinstance(egress, list) or not all(isinstance(h, str) and h for h in egress):
        problems.append("tours.egress_allow must be a list of host names")
        egress = []

    bindings: dict = {}
    raw_bindings = table.get("bindings", {})
    if not isinstance(raw_bindings, dict):
        problems.append("tours.bindings must be a table of [tours.bindings.NAME] tables")
        raw_bindings = {}
    for name, raw in raw_bindings.items():
        where = f"tours.bindings.{name}"
        if not BINDING_RE.fullmatch(str(name)):
            problems.append(f"{where}: the binding name must match {BINDING_RE.pattern}")
            continue
        if not isinstance(raw, dict):
            problems.append(f"{where} must be a table")
            continue
        for key in sorted(set(raw) - BINDING_KEYS):
            problems.append(f"{where} has an unknown key {key!r}")
        before = len(problems)
        upstream = text(raw, "upstream", "", where)
        parts = urlsplit(upstream)
        host, port = None, None
        try:
            host, port = parts.hostname, parts.port
        except ValueError:
            problems.append(f"{where}.upstream has a bad port")
        if not upstream:
            problems.append(f"{where}.upstream is required (for example https://api.example.com)")
        elif (parts.scheme not in ("https", "http") or not host or parts.username is not None or parts.password is not None
              or parts.path not in ("", "/") or parts.query or parts.fragment):
            problems.append(f"{where}.upstream must be an origin: https://host[:port], with no path, query or credentials")
        elif parts.scheme == "http" and not _is_loopback(host):
            problems.append(f"{where}.upstream may use plain http only for a loopback address; use https")
        auth = text(raw, "auth", "", where)
        if auth not in AUTH_STYLES:
            problems.append(f"{where}.auth must be one of: {', '.join(AUTH_STYLES)}")
        key_file = text(raw, "key_file", "", where)
        if not key_file.startswith("/") or "\0" in key_file or "\n" in key_file:
            problems.append(f"{where}.key_file must be an absolute path")
        paths = raw.get("paths", [])
        if not isinstance(paths, list) or not paths or not all(isinstance(p, str) for p in paths):
            problems.append(f"{where}.paths must be a non-empty list of upstream paths")
            paths = []
        for entry in paths:
            body = entry[:-2] + "/" if entry.endswith("/*") else entry
            if not PATH_RE.fullmatch(body) or "//" in body or any(seg == ".." for seg in body.split("/")) or len(entry) > 512:
                problems.append(f"{where}.paths entry {entry!r} must be an absolute path (optionally ending /*) of plain characters")
        methods = raw.get("methods", ["POST"])
        if not isinstance(methods, list) or not methods or any(m not in METHODS for m in methods):
            problems.append(f"{where}.methods must be a non-empty list drawn from: {', '.join(METHODS)}")
            methods = ["POST"]
        fmt = text(raw, "format", "anthropic" if auth == "x-api-key" else "openai", where)
        if fmt not in FORMATS:
            problems.append(f"{where}.format must be one of: {', '.join(FORMATS)}")
        max_concurrent = number(raw, "max_concurrent", 16, where, integer=True, low=1, high=1024)
        default_out = number(raw, "default_max_output_tokens", 32768, where, integer=True, low=1, high=10_000_000)
        pricing: dict = {}
        default_price = DEFAULT_PRICE
        betas = raw.get("betas", [])
        if not isinstance(betas, list) or not all(isinstance(b, str) and b and len(b) <= 128 and "," not in b for b in betas):
            problems.append(f"{where}.betas must be a list of beta header values")
            betas = []
        allow = raw.get("allow", [])
        if not isinstance(allow, list) or any(not (a in FEATURES or (isinstance(a, str) and TOOL_ALLOW_RE.fullmatch(a))) for a in allow):
            problems.append(f"{where}.allow must be a list drawn from: {', '.join(FEATURES)}, or tool:<exact tool type>")
            allow = []
        profile = raw.get("profile", "")
        if not isinstance(profile, str) or (profile and profile not in PROFILES):
            problems.append(f"{where}.profile must be one of: {', '.join(sorted(PROFILES))}")
            profile = ""
        betas, allow = merged(profile, betas, allow)

        def price(tbl, label):
            if not isinstance(tbl, dict):
                problems.append(f"{label} must be a table with input and output (USD per million tokens)")
                return None
            for key in sorted(set(tbl) - PRICE_KEYS):
                problems.append(f"{label} has an unknown key {key!r}")
            if "input" not in tbl or "output" not in tbl:
                problems.append(f"{label} needs input and output")
                return None
            values = {k: number(tbl, k, None, label, low=0, high=100_000) for k in PRICE_KEYS if k in tbl}
            if any(v is None for v in values.values()):
                return None
            return Price.make(values["input"], values["output"], values.get("cache_read"), values.get("cache_write"),
                              values.get("cache_write_1h"))

        if "default_price" in raw:
            default_price = price(raw["default_price"], f"{where}.default_price") or DEFAULT_PRICE
        raw_pricing = raw.get("pricing", {})
        if not isinstance(raw_pricing, dict):
            problems.append(f"{where}.pricing must be a table of model-prefix tables")
            raw_pricing = {}
        for model, entry in raw_pricing.items():
            parsed = price(entry, f'{where}.pricing."{model}"')
            if parsed is not None and model:
                pricing[str(model)] = parsed
        if len(problems) == before and host:
            scheme = parts.scheme
            bindings[name] = BindingConfig(
                name=name, upstream=f"{scheme}://{parts.netloc}", scheme=scheme, host=host, port=port or (443 if scheme == "https" else 80),
                auth=auth, key_file=key_file, paths=tuple(paths), methods=tuple(methods), format=fmt,
                max_concurrent=max_concurrent, default_max_output_tokens=default_out, pricing=pricing, default_price=default_price,
                betas=betas, allow=frozenset(allow), profile=profile)

    port = number(table, "concierge_port", 7781, "tours", integer=True, low=1024, high=65535)
    port_range = DEFAULT_PORT_RANGE
    if "concierge_port_range" in table:
        match = PORT_RANGE_RE.fullmatch(table["concierge_port_range"]) if isinstance(table["concierge_port_range"], str) else None
        low_port, high_port = (int(match.group(1)), int(match.group(2))) if match else (0, 0)
        if not match or not 1024 <= low_port <= high_port <= 65535:
            problems.append('tours.concierge_port_range must be a string such as "17800-17999" (ports 1024 to 65535, low to high)')
        elif high_port - low_port + 1 > MAX_PORT_RANGE:
            problems.append(f"tours.concierge_port_range spans {high_port - low_port + 1} ports; at most {MAX_PORT_RANGE} are allowed "
                            "(each live attempt keeps one listening socket in systemd's file-descriptor store)")
        else:
            port_range = (low_port, high_port)
    result = TourConfig(
        enabled=enabled, concierge_port=port, concierge_port_range=port_range,
        max_concurrent_guests=number(table, "max_concurrent_guests", 4, "tours", integer=True, low=1, high=1000),
        max_guests_per_tour=number(table, "max_guests_per_tour", 20, "tours", integer=True, low=1, high=1000),
        max_eur_per_tour=float(number(table, "max_eur_per_tour", 5.0, "tours", low=0)),
        max_usd_model_per_guest=float(number(table, "max_usd_model_per_guest", 2.0, "tours", low=0.000001, high=1_000_000)),
        max_ttl_s=max_ttl_s, default_image=text(table, "default_image", "ubuntu-24.04", "tours"),
        default_server_type=text(table, "default_server_type", "", "tours"),
        max_result_mb=number(table, "max_result_mb", 64, "tours", integer=True, low=1, high=100_000),
        egress_allow=tuple(egress),
        concierge_max_request_mb=float(number(table, "concierge_max_request_mb", 8.0, "tours", low=0.001, high=256)),
        concierge_upstream_timeout_s=float(number(table, "concierge_upstream_timeout_s", 300.0, "tours", low=1, high=3600)),
        concierge_max_buffered_mb=float(number(table, "concierge_max_buffered_mb", 192.0, "tours", low=0.002, high=65536)),
        concierge_max_json_nodes=number(table, "concierge_max_json_nodes", 200_000, "tours", integer=True, low=100, high=50_000_000),
        bindings=bindings)
    floor = worst_case_charge(result.max_request_bytes, result.concierge_max_json_nodes)
    if result.max_buffered_bytes < floor:
        problems.append(f"tours.concierge_max_buffered_mb must be at least {floor / (1024 * 1024):.1f} MiB here: {BODY_CHARGE_FACTOR} times "
                        f"concierge_max_request_mb plus {NODE_BYTES} bytes for each of concierge_max_json_nodes "
                        "(one request, at its worst, is charged that much and must always fit)")
    if problems:
        raise ConfigError("invalid [tours] configuration: " + "; ".join(problems))
    return result
