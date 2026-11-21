"""Tour specs: parsing (TOML or JSON) and validation against the operator ceilings.

Two shapes share one validator. The *user* shape is what a person or agent writes (``repo``, ``base``,
``overlay`` as names on their machine). The *wire* shape is what the unprivileged CLI hands to the desk after it
made the git bundles: every guest names an input by key and a full commit. The desk validates the wire shape
again, against ``/etc/motel/motel.toml``, because nothing the caller sends is trusted. A tour may ask for less
than a ceiling, never more.
"""

from __future__ import annotations

import json
import re
import tomllib
from dataclasses import dataclass, field
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from motel.cabins.util import parse_duration as _parse_duration
from motel.tours.config import Limits
from motel.tours.errors import Fail, Refused, Usage
from motel.tours.guest import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    MAX_EGRESS_BYTES,
    MAX_EGRESS_HOSTS,
    MAX_OUTPUT_TOKENS_LIMIT,
    MIN_OUTPUT_TOKENS,
    MODEL_MAX,
    MODEL_RE,
    RESERVED_ENV_NAMES,  # noqa: F401  (re-exported: the guest part owns the list)
    RESERVED_ENV_PREFIXES,  # noqa: F401
    egress_cost,
    env_name_reserved,
)

SCHEMA = "motel/tour-spec/v1"
WIRE_SCHEMA = "motel/tour-start/v1"
GUEST_ID_RE = re.compile(r"^[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")
NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 ._-]{0,63}$")
BINDING_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")
OWN_LOGIN_HOST = "api.anthropic.com"       # an own-login guest's profile always allows it, and it counts in the budget
HOST_RE = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")
ENV_NAME_RE = re.compile(r"^[A-Z_][A-Z0-9_]{0,63}$")
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
IMAGE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,62}$")
TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,39}$")
INPUT_KEY_RE = re.compile(r"^[0-9a-f]{16}$")
GLOB_RE = re.compile(r"^[A-Za-z0-9._*?\[\]/-]{1,200}$")
ADAPTERS = ("claude", "codex", "command")
AUTHS = ("concierge", "own-login")

MAX_PROMPT = 64 * 1024
MAX_ARGV = 64
MAX_ARG_LEN = 4096
MAX_ENV = 32
MAX_ENV_VALUE = 4096
MAX_ARTIFACT_GLOBS = 32
MAX_TURNS = 500
MIN_TTL = timedelta(minutes=1)
DEFAULT_TTL = timedelta(minutes=30)
DEFAULT_MODEL_BUDGET = Decimal("1.0")
# Environment names a spec may never set (the guest part owns the list, so the runner and the spec cannot disagree).
SECRET_WORDS = ("KEY", "TOKEN", "SECRET", "PASSWORD", "PASSWD", "CREDENTIAL", "AUTH")

TOP_KEYS = {"tour", "defaults", "guests", "schema"}
TOUR_KEYS = {"name"}
SHARED_KEYS = {"server_type", "image", "ttl", "adapter", "env", "secrets", "egress", "budget_usd", "max_turns", "artifacts",
               "auth", "max_output_tokens", "model"}
GUEST_ONLY_KEYS = {"id", "prompt", "argv", "repo", "base", "overlay"}
WIRE_GUEST_KEYS = {"id", "prompt", "argv", "adapter", "env", "secrets", "egress", "server_type", "image", "ttl_s",
                   "budget_usd", "max_turns", "artifacts", "base", "bundle", "overlay_input", "auth", "max_output_tokens", "model"}


@dataclass
class Guest:
    id: str
    adapter: str
    prompt: str = ""
    argv: list = field(default_factory=list)
    env: dict = field(default_factory=dict)
    secrets: list = field(default_factory=list)
    egress: list = field(default_factory=list)
    server_type: str = ""
    image: str = ""
    ttl: timedelta | None = None          # None until the desk applies the default (it knows the ceilings)
    budget_usd: Decimal | None = None
    max_turns: int = 30
    max_output_tokens: int | None = None   # per model reply (claude adapter); None leaves the guest's own default
    model: str | None = None   # claude and codex adapters: the CLI's --model value; None leaves the CLI's own default
    artifacts: list = field(default_factory=list)
    auth: str = "concierge"      # "concierge" (API keys, the default) or "own-login" (opt-in, claude adapter only)
    base: str = "HEAD"           # user shape: a revision; wire shape: a full commit
    repo: str | None = None      # user shape only
    overlay: str | None = None   # user shape only
    bundle: str | None = None    # wire shape only: key of the staged input bundle
    overlay_input: str | None = None  # wire shape only

    def to_wire(self) -> dict:
        doc = {"id": self.id, "adapter": self.adapter, "env": dict(self.env), "secrets": list(self.secrets),
               "egress": list(self.egress), "server_type": self.server_type, "image": self.image,
               "max_turns": self.max_turns, "artifacts": list(self.artifacts), "base": self.base, "bundle": self.bundle,
               "auth": self.auth}
        if self.max_output_tokens is not None:
            doc["max_output_tokens"] = self.max_output_tokens
        if self.model is not None:
            doc["model"] = self.model
        if self.ttl is not None:
            doc["ttl_s"] = int(self.ttl.total_seconds())
        if self.budget_usd is not None:
            doc["budget_usd"] = str(self.budget_usd)
        if self.prompt:
            doc["prompt"] = self.prompt
        if self.argv:
            doc["argv"] = list(self.argv)
        if self.overlay_input:
            doc["overlay_input"] = self.overlay_input
        return doc


@dataclass
class TourSpec:
    name: str
    guests: list

    def to_wire(self) -> dict:
        return {"schema": WIRE_SCHEMA, "name": self.name, "guests": [g.to_wire() for g in self.guests]}


# --------------------------------------------------------------------------- reading
def load_text(text: str, *, source: str = "the spec") -> dict:
    """TOML or JSON text to a dict. JSON when it starts with ``{``."""
    if not isinstance(text, str) or len(text.encode("utf-8", "replace")) > 2 * 1024 * 1024:
        raise Usage(f"{source} is missing or larger than 2 MiB", "motel tour start --help")
    try:
        doc = json.loads(text) if text.lstrip().startswith("{") else tomllib.loads(text)
    except (ValueError, RecursionError) as exc:  # JSONDecodeError and TOMLDecodeError are ValueErrors
        raise Usage(f"{source} is not valid TOML or JSON: {exc}", "motel tour start --help") from None
    if not isinstance(doc, dict):
        raise Usage(f"{source} must be a table or object", "motel tour start --help")
    return doc


# --------------------------------------------------------------------------- field validators
def _is_int(value) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _str(value, what: str, *, pattern=None, maximum: int = 256, example: str = "") -> str:
    if not isinstance(value, str) or not value or len(value) > maximum or "\x00" in value:
        raise Usage(f"{what} must be a non-empty string of at most {maximum} characters")
    if pattern is not None and not pattern.fullmatch(value):
        raise Usage(f"{what} {value!r} is not valid" + (f" (for example {example})" if example else ""))
    return value


def _str_list(value, what: str, *, pattern, maximum: int, example: str) -> list:
    if not isinstance(value, list) or len(value) > maximum:
        raise Usage(f"{what} must be a list of at most {maximum} strings")
    out = [_str(v, f"{what} entry", pattern=pattern, maximum=253, example=example) for v in value]
    if len(set(out)) != len(out):
        raise Usage(f"{what} lists the same entry twice")
    return out


def _duration(value, what: str) -> timedelta:
    if _is_int(value):          # wire shape: seconds
        return timedelta(seconds=value)
    if not isinstance(value, str):
        raise Usage(f"{what} must be a duration such as 45m or 2h")
    try:
        return _parse_duration(value, what=what)
    except Fail as exc:
        raise Usage(str(exc)) from None


def _money(value, what: str) -> Decimal:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise Usage(f"{what} must be a number of US dollars")
    try:
        amount = Decimal(str(value))
    except InvalidOperation:
        raise Usage(f"{what} must be a number of US dollars") from None
    if not amount.is_finite() or amount < 0 or amount > Decimal("10000"):
        raise Usage(f"{what} must be between 0 and 10000")
    return amount


def check_env(env, what: str = "env") -> dict:
    if not isinstance(env, dict) or len(env) > MAX_ENV:
        raise Usage(f"{what} must be a table of at most {MAX_ENV} variables")
    out = {}
    for name, value in env.items():
        if not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name):
            raise Usage(f"{what} name {name!r} must be upper case letters, digits and underscores")
        if env_name_reserved(name) or any(w in name for w in SECRET_WORDS):
            raise Usage(f"{what} name {name} is reserved or looks like a secret: a guest never receives a credential "
                        "(name a concierge binding in `secrets` instead)")
        if not isinstance(value, str) or len(value) > MAX_ENV_VALUE or "\x00" in value:
            raise Usage(f"{what} {name} must be a string of at most {MAX_ENV_VALUE} characters")
        out[name] = value
    return out


def check_globs(values, what: str = "artifacts") -> list:
    if not isinstance(values, list) or len(values) > MAX_ARTIFACT_GLOBS:
        raise Usage(f"{what} must be a list of at most {MAX_ARTIFACT_GLOBS} patterns")
    out = []
    for pattern in values:
        _str(pattern, f"{what} pattern", pattern=GLOB_RE, maximum=200)
        parts = pattern.split("/")
        if pattern.startswith("/") or any(p in ("", ".", "..") for p in parts):
            raise Usage(f"{what} pattern {pattern!r} must be a relative path without . or .. parts")
        out.append(pattern)
    return out


def host_allowed(host: str, allow) -> bool:
    for entry in allow:
        if entry == host or (entry.startswith("*.") and host.endswith(entry[1:])):
            return True
    return False


# --------------------------------------------------------------------------- the validator
def _unknown(doc: dict, allowed: set, where: str) -> None:
    extra = sorted(set(doc) - allowed)
    if extra:
        raise Usage(f"unknown key(s) in {where}: {', '.join(map(str, extra))}")


def parse(doc: dict, *, limits: Limits | None = None, cabin_types: tuple | None = None, wire: bool = False) -> TourSpec:
    """Validate a spec document. With ``limits`` it is also checked against the operator ceilings."""
    if not isinstance(doc, dict):
        raise Usage("the spec must be a table or object")
    _unknown(doc, TOP_KEYS if not wire else {"schema", "name", "guests"}, "the spec")
    if wire:
        if doc.get("schema") != WIRE_SCHEMA:
            raise Usage("the front desk got a spec it does not understand", "motel tour start --help")
        name, defaults, raw_guests = doc.get("name"), {}, doc.get("guests")
    else:
        tour = doc.get("tour", {})
        if not isinstance(tour, dict):
            raise Usage("[tour] must be a table")
        _unknown(tour, TOUR_KEYS, "[tour]")
        defaults = doc.get("defaults", {})
        if not isinstance(defaults, dict):
            raise Usage("[defaults] must be a table")
        _unknown(defaults, SHARED_KEYS, "[defaults]")
        name, raw_guests = tour.get("name", "tour"), doc.get("guests")
    name = _str(name, "tour name", pattern=NAME_RE, maximum=64, example="fix-flaky-tests")
    if not isinstance(raw_guests, list) or not raw_guests:
        raise Usage("the spec needs at least one [[guests]] entry")
    if limits is not None and len(raw_guests) > limits.max_guests_per_tour:
        raise Refused(f"{len(raw_guests)} guests is above the limit of {limits.max_guests_per_tour} per tour "
                      "(tours.max_guests_per_tour)")
    guests, seen = [], set()
    for index, raw in enumerate(raw_guests, 1):
        if not isinstance(raw, dict):
            raise Usage(f"guest {index} must be a table")
        guest = _guest(raw, defaults, index, limits, cabin_types, wire)
        if guest.id in seen:
            raise Usage(f"guest id {guest.id!r} is used twice")
        seen.add(guest.id)
        guests.append(guest)
    return TourSpec(name=name, guests=guests)


def _pick(raw: dict, defaults: dict, key: str, default=None):
    if key in raw:
        return raw[key]
    return defaults.get(key, default)


def _guest(raw: dict, defaults: dict, index: int, limits, cabin_types, wire: bool) -> Guest:
    if wire:
        _unknown(raw, WIRE_GUEST_KEYS, f"guest {index}")
    else:
        _unknown(raw, SHARED_KEYS | GUEST_ONLY_KEYS, f"guest {index}")
    gid = _str(raw.get("id"), f"guest {index} id", pattern=GUEST_ID_RE, maximum=32, example="fix-auth")
    where = f"guest {gid}"
    adapter = _pick(raw, defaults, "adapter", "claude")
    if adapter not in ADAPTERS:
        raise Usage(f"{where}: adapter must be one of {', '.join(ADAPTERS)}")
    prompt, argv = raw.get("prompt", ""), raw.get("argv", [])
    if adapter == "command":
        if not isinstance(argv, list) or not 1 <= len(argv) <= MAX_ARGV or not all(
                isinstance(a, str) and a and len(a) <= MAX_ARG_LEN and "\x00" not in a for a in argv):
            raise Usage(f"{where}: the command adapter needs `argv`, a list of 1-{MAX_ARGV} non-empty strings (no shell)")
        if prompt:
            raise Usage(f"{where}: the command adapter takes `argv`, not `prompt`")
    else:
        if not isinstance(prompt, str) or not prompt.strip() or len(prompt.encode()) > MAX_PROMPT or "\x00" in prompt:
            raise Usage(f"{where}: {adapter} needs a `prompt` of at most {MAX_PROMPT // 1024} KiB")
        if argv:
            raise Usage(f"{where}: `argv` belongs to the command adapter")
    env = check_env({**defaults.get("env", {}), **raw.get("env", {})} if not wire else raw.get("env", {}), f"{where} env")
    auth = _pick(raw, defaults, "auth", "concierge")
    if auth not in AUTHS:
        raise Usage(f"{where}: auth must be one of {', '.join(AUTHS)}")
    secrets = _str_list(_pick(raw, defaults, "secrets", []), f"{where} secrets", pattern=BINDING_RE, maximum=8, example="anthropic")
    egress = [h.lower() for h in _str_list(_pick(raw, defaults, "egress", []), f"{where} egress", pattern=HOST_RE,
                                           maximum=MAX_EGRESS_HOSTS, example="github.com")]
    on_profile = sorted(set(egress) | ({OWN_LOGIN_HOST} if auth == "own-login" else set()))
    if len(on_profile) > MAX_EGRESS_HOSTS or egress_cost(on_profile) > MAX_EGRESS_BYTES:
        raise Refused(f"{where}: egress lists too many or too long host names for a cabin's firewall profile "
                      f"(at most {MAX_EGRESS_HOSTS} hosts and {MAX_EGRESS_BYTES} bytes of names"
                      + (", with api.anthropic.com counted for own-login" if auth == "own-login" else "") + ")",
                      "list fewer or shorter hosts, or run the work in two guests")
    server_type = _pick(raw, defaults, "server_type", "")
    if server_type:
        _str(server_type, f"{where} server_type", pattern=TYPE_RE, maximum=40, example="cpx32")
    image = _pick(raw, defaults, "image", "")
    if image:
        _str(image, f"{where} image", pattern=IMAGE_RE, maximum=63, example="ubuntu-24.04")
    ttl_raw = raw.get("ttl_s") if wire else _pick(raw, defaults, "ttl", None)
    ttl = _duration(ttl_raw, f"{where} ttl") if ttl_raw is not None else None
    if ttl is not None and ttl < MIN_TTL:
        raise Usage(f"{where}: ttl must be at least 1m")
    budget_raw = _pick(raw, defaults, "budget_usd", None)
    budget = _money(budget_raw, f"{where} budget_usd") if budget_raw is not None else None
    turns = _pick(raw, defaults, "max_turns", 30)
    if not _is_int(turns) or not 1 <= turns <= MAX_TURNS:
        raise Usage(f"{where}: max_turns must be a whole number from 1 to {MAX_TURNS}")
    out_tokens = _pick(raw, defaults, "max_output_tokens", None)
    if out_tokens is not None and (not _is_int(out_tokens) or not MIN_OUTPUT_TOKENS <= out_tokens <= MAX_OUTPUT_TOKENS_LIMIT):
        raise Usage(f"{where}: max_output_tokens must be a whole number from {MIN_OUTPUT_TOKENS} to {MAX_OUTPUT_TOKENS_LIMIT} "
                    f"(default {DEFAULT_MAX_OUTPUT_TOKENS})")
    model = _pick(raw, defaults, "model", None)
    if model is not None:
        if adapter == "command":
            raise Usage(f"{where}: `model` belongs to the claude and codex adapters; the command adapter runs your argv as given")
        _str(model, f"{where} model", pattern=MODEL_RE, maximum=MODEL_MAX, example="claude-sonnet-5-5")
    artifacts = check_globs(_pick(raw, defaults, "artifacts", []), f"{where} artifacts")
    if auth == "own-login":
        if adapter != "claude":
            raise Usage(f"{where}: auth = \"own-login\" works only with the claude adapter")
        if secrets or budget:
            raise Usage(f"{where}: auth = \"own-login\" uses your own Claude login: it has no `secrets` and no budget_usd "
                        "(model use is not metered); set secrets = [] and drop budget_usd for this guest")
        budget = Decimal(0)
    if budget is not None and secrets and budget <= 0:
        raise Usage(f"{where}: `secrets` needs a budget_usd above zero")
    if budget and not secrets:
        raise Usage(f"{where}: budget_usd without `secrets` has nothing to spend on; name a binding such as [\"anthropic\"]")
    guest = Guest(id=gid, adapter=adapter, prompt=prompt, argv=list(argv), env=env, secrets=secrets, egress=egress,
                  server_type=server_type, image=image, ttl=ttl, budget_usd=budget, max_turns=turns, max_output_tokens=out_tokens,
                  model=model, artifacts=artifacts, auth=auth)
    _inputs(guest, raw, wire, where)
    if limits is not None:
        guest.ttl = guest.ttl if guest.ttl is not None else min(DEFAULT_TTL, limits.max_ttl)
        if guest.budget_usd is None:
            guest.budget_usd = min(DEFAULT_MODEL_BUDGET, limits.max_usd_model_per_guest) if secrets else Decimal(0)
        if guest.auth == "own-login" and guest.ttl == min(DEFAULT_TTL, limits.max_ttl):
            guest.ttl = min(guest.ttl, limits.own_login_max_ttl)
        _ceilings(guest, limits, cabin_types, where)
    return guest


def _inputs(guest: Guest, raw: dict, wire: bool, where: str) -> None:
    if wire:
        if not SHA_RE.fullmatch(str(raw.get("base", ""))):
            raise Usage(f"{where}: base must be a full 40-character commit id")
        guest.base = raw["base"]
        guest.bundle = _str(raw.get("bundle"), f"{where} bundle", pattern=INPUT_KEY_RE, maximum=16)
        if raw.get("overlay_input") is not None:
            guest.overlay_input = _str(raw["overlay_input"], f"{where} overlay_input", pattern=INPUT_KEY_RE, maximum=16)
        return
    guest.base = _str(raw.get("base", "HEAD"), f"{where} base", maximum=200)
    if guest.base.startswith("-"):
        raise Usage(f"{where}: base must be a commit, branch or tag, not an option")
    for key in ("repo", "overlay"):
        if raw.get(key) is not None:
            setattr(guest, key, _str(raw[key], f"{where} {key}", maximum=1024))


def _ceilings(guest: Guest, limits: Limits, cabin_types, where: str) -> None:
    if guest.auth == "own-login":
        if not limits.own_login_enabled:
            raise Refused(f"{where}: own-login is not enabled on this host; the operator must set "
                          "[tours.own_login] enabled = true (the API-key path needs nothing)")
        if guest.ttl > limits.own_login_max_ttl:
            raise Refused(f"{where}: ttl is above the own-login ceiling of {_fmt(limits.own_login_max_ttl)} "
                          "(tours.own_login.max_ttl)")
    if guest.ttl > limits.max_ttl:
        raise Refused(f"{where}: ttl is above the ceiling of {_fmt(limits.max_ttl)} (tours.max_ttl)")
    if guest.budget_usd > limits.max_usd_model_per_guest:
        raise Refused(f"{where}: budget_usd {guest.budget_usd} is above the ceiling of "
                      f"{limits.max_usd_model_per_guest} (tours.max_usd_model_per_guest)")
    unknown = [s for s in guest.secrets if s not in limits.bindings]
    if unknown:
        raise Refused(f"{where}: no such binding: {', '.join(unknown)}; configured: {', '.join(limits.bindings) or 'none'}")
    blocked = [h for h in guest.egress if not host_allowed(h, limits.egress_allow)]
    if blocked:
        raise Refused(f"{where}: egress to {', '.join(blocked)} is not allowed; the operator allows: "
                      f"{', '.join(limits.egress_allow) or 'nothing'} (tours.egress_allow)")
    if cabin_types is not None:
        chosen = guest.server_type or limits.default_server_type or (cabin_types[0] if cabin_types else "")
        if chosen not in cabin_types:
            raise Refused(f"{where}: server type {chosen!r} is not allowed; choices: {', '.join(sorted(cabin_types))}")


def _fmt(delta: timedelta) -> str:
    minutes = int(delta.total_seconds() // 60)
    return f"{minutes // 60}h" if minutes % 60 == 0 and minutes else f"{minutes}m"


def effective_server_type(guest: Guest, limits: Limits, cabin_types) -> str:
    return guest.server_type or limits.default_server_type or (cabin_types[0] if cabin_types else "")


def effective_image(guest: Guest, limits: Limits) -> str:
    return guest.image or limits.default_image
