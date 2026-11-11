"""The operator ceilings of ``[tours]`` as the engine uses them.

The ``[tours]`` table is validated by the concierge part (``parse_tours_section``); the engine reads the result
by attribute with the key names of the contract. A dict with the same keys works as well, which is what the
tests use. Nothing a tour sends can raise any of these values.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass
from datetime import timedelta
from decimal import Decimal, InvalidOperation

from motel.cabins.util import parse_duration
from motel.errors import INSTALL_HINT
from motel.tours import deps
from motel.tours.errors import NotSetUp, Usage
from motel.units import USER_RE

DEFAULT_PORT = 7781


@dataclass(frozen=True)
class Limits:
    enabled: bool = False
    concierge_port: int = DEFAULT_PORT
    max_concurrent_guests: int = 4
    max_guests_per_tour: int = 20
    max_eur_per_tour: Decimal = Decimal("5.0")
    max_usd_model_per_guest: Decimal = Decimal("2.0")
    max_ttl: timedelta = timedelta(hours=4)
    default_image: str = "ubuntu-24.04"
    default_server_type: str = ""
    max_result_mb: int = 64
    egress_allow: tuple = ()
    bindings: tuple = ()
    own_login_enabled: bool = False       # [tours.own_login]: opt-in, off by default
    own_login_token_file: str = "/etc/motel/credentials/own-login/claude.token"
    own_login_max_concurrent: int = 2
    own_login_max_ttl: timedelta = timedelta(hours=2)
    own_login_allow_users: tuple = ()     # empty means nobody: the operator names who may use their login
    max_active_tours_per_user: int = 3    # unfinished tours of one user
    max_staged_mb_per_user: int = 1024    # caller files staged on the host for one user's unfinished tours
    min_free_after_staging_mb: int = 2048 # a start is refused unless this much disk stays free after staging

    @property
    def max_result_bytes(self) -> int:
        return self.max_result_mb * 1024 * 1024

    @classmethod
    def from_table(cls, table) -> Limits:
        """Validate the raw ``[tours]`` table: the concierge part checks its keys, this checks ``[tours.own_login]``."""
        section = deps.parse_tours_section(table, strict=False)
        own = _own_login(table.get("own_login", {}))
        return cls.from_section(section, own_login=own, engine=_engine_keys(table))

    @classmethod
    def for_cleanup(cls, table) -> Limits:
        """What ``cancel``, ``reap`` and ``supervise`` need to finish and clean up tours that exist: the table when it is
        usable, the built-in defaults when it is missing or invalid (a broken config must never keep a cabin alive)."""
        try:
            return cls.from_table(table) if isinstance(table, dict) else cls()
        except Exception:  # noqa: BLE001  the concierge part owns its own error type
            return cls()

    @classmethod
    def from_section(cls, section, own_login=None, engine=None) -> Limits:
        """Build from the object (or dict) that ``parse_tours_section`` returned."""
        def pick(name, default):
            value = section.get(name, default) if isinstance(section, dict) else getattr(section, name, default)
            return default if value is None else value

        def money(name, default):
            try:
                return Decimal(str(pick(name, default)))
            except InvalidOperation:
                raise NotSetUp(f"[tours] {name} is not a number", "edit /etc/motel/motel.toml") from None

        ttl = pick("max_ttl", cls.max_ttl)
        if pick("max_ttl_s", None) is not None:          # TourConfig keeps the ceiling in seconds
            ttl = timedelta(seconds=int(pick("max_ttl_s", 0)))
        if isinstance(ttl, str):
            ttl = parse_duration(ttl, what="tours.max_ttl")
        own = own_login if own_login is not None else pick("own_login", {})

        def own_pick(name, default):
            value = own.get(name, default) if isinstance(own, dict) else getattr(own, name, default)
            return default if value is None else value

        own_ttl = own_pick("max_ttl", cls.own_login_max_ttl)
        if isinstance(own_ttl, str):
            own_ttl = parse_duration(own_ttl, what="tours.own_login.max_ttl")
        engine = engine or {}
        bindings = pick("bindings", ())
        names = tuple(sorted(bindings.keys())) if isinstance(bindings, dict) else tuple(sorted(bindings))
        return cls(
            enabled=bool(pick("enabled", False)), concierge_port=int(pick("concierge_port", DEFAULT_PORT)),
            max_concurrent_guests=int(pick("max_concurrent_guests", 4)), max_guests_per_tour=int(pick("max_guests_per_tour", 20)),
            max_eur_per_tour=money("max_eur_per_tour", "5.0"), max_usd_model_per_guest=money("max_usd_model_per_guest", "2.0"),
            max_ttl=ttl, default_image=str(pick("default_image", "ubuntu-24.04")),
            default_server_type=str(pick("default_server_type", "")), max_result_mb=int(pick("max_result_mb", 64)),
            egress_allow=tuple(str(h).lower() for h in pick("egress_allow", ())), bindings=names,
            own_login_enabled=bool(own_pick("enabled", False)),
            own_login_token_file=str(own_pick("claude_token_file", cls.own_login_token_file)),
            own_login_max_concurrent=int(own_pick("max_concurrent", 2)), own_login_max_ttl=own_ttl,
            own_login_allow_users=tuple(own_pick("allow_users", ())),
            max_active_tours_per_user=int(engine.get("max_active_tours_per_user", 3)),
            max_staged_mb_per_user=int(engine.get("max_staged_mb_per_user", 1024)))


OWN_LOGIN_KEYS = {"enabled", "claude_token_file", "max_concurrent", "max_ttl", "allow_users"}
ENGINE_KEYS = {"max_active_tours_per_user": (1, 1000), "max_staged_mb_per_user": (16, 1_000_000)}


def _engine_keys(table) -> dict:
    """The engine's own ``[tours]`` keys (the concierge part ignores them)."""
    out = {}
    for name, (low, high) in ENGINE_KEYS.items():
        if name in table:
            value = table[name]
            if not isinstance(value, int) or isinstance(value, bool) or not low <= value <= high:
                raise Usage(f"tours.{name} must be a whole number from {low} to {high}", "edit /etc/motel/motel.toml")
            out[name] = value
    return out


def _own_login(table) -> dict:
    """Check ``[tours.own_login]`` (an engine table: the concierge part does not read it)."""
    if not isinstance(table, dict):
        raise Usage("[tours.own_login] must be a table", "edit /etc/motel/motel.toml")
    unknown = sorted(set(table) - OWN_LOGIN_KEYS)
    if unknown:
        raise Usage(f"[tours.own_login] has an unknown key {unknown[0]!r}", "edit /etc/motel/motel.toml")
    if not isinstance(table.get("enabled", False), bool):
        raise Usage("tours.own_login.enabled must be true or false", "edit /etc/motel/motel.toml")
    path = table.get("claude_token_file", Limits.own_login_token_file)
    if not isinstance(path, str) or not path.startswith("/") or "\0" in path or "\n" in path:
        raise Usage("tours.own_login.claude_token_file must be an absolute path", "edit /etc/motel/motel.toml")
    count = table.get("max_concurrent", 2)
    if not isinstance(count, int) or isinstance(count, bool) or not 1 <= count <= 100:
        raise Usage("tours.own_login.max_concurrent must be a whole number from 1 to 100", "edit /etc/motel/motel.toml")
    allow = table.get("allow_users", [])
    if not isinstance(allow, list) or not all(isinstance(u, str) and USER_RE.fullmatch(u[1:] if u.startswith("@") else u)
                                              for u in allow):
        raise Usage("tours.own_login.allow_users must be a list of user names or @groups (empty means nobody)",
                    "edit /etc/motel/motel.toml")
    if not isinstance(table.get("max_ttl", "2h"), str):
        raise Usage("tours.own_login.max_ttl must be a duration such as 2h", "edit /etc/motel/motel.toml")
    parse_duration(table.get("max_ttl", "2h"), what="tours.own_login.max_ttl")
    return dict(table)


def load_cleanup_limits(path: str) -> Limits:
    """For verbs that must work whatever state the config is in (cancel, reap, supervise, and the reads)."""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except (OSError, tomllib.TOMLDecodeError):
        return Limits()
    return Limits.for_cleanup(data.get("tours"))


def new_attempts_block(path: str) -> str | None:
    """Why a supervisor may not start a new attempt (book a cabin) now, or None when it may. The raw flags decide, as in the
    installer: ``tours_disabled`` when there is no ``motel.toml``, no ``[tours]`` table or ``enabled`` is not ``true``;
    ``cabins_disabled`` when tours are enabled but ``[cabins] enabled`` is not ``true`` (tours run on cabins). Tour users
    keep their verbs and attempts that already run finish. A config that cannot be parsed or read for another reason right
    now answers None: a typo or a transient error must not cancel queued work, and the doctor reports a broken table."""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        return "tours_disabled"
    except (OSError, tomllib.TOMLDecodeError):
        return None
    table = data.get("tours")
    if not (isinstance(table, dict) and table.get("enabled") is True):
        return "tours_disabled"
    cabins = data.get("cabins")
    if not (isinstance(cabins, dict) and cabins.get("enabled") is True):
        return "cabins_disabled"
    return None


def new_attempts_allowed(path: str) -> bool:
    return new_attempts_block(path) is None


def load_limits(path: str, *, require_enabled: bool = True) -> Limits:
    """Read ``[tours]`` from ``motel.toml``. A missing or disabled table means tours are off."""
    try:
        with open(path, "rb") as handle:
            data = tomllib.load(handle)
    except FileNotFoundError:
        raise NotSetUp(f"{path} does not exist", INSTALL_HINT) from None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise Usage(f"cannot read {path}: {exc}", "motel doctor") from None
    table = data.get("tours")
    if table is None:
        raise NotSetUp(f"{path} has no [tours] section: tours are off", "add [tours] with enabled = true")
    limits = Limits.from_table(table)
    if require_enabled and not limits.enabled:
        raise NotSetUp("tours are disabled in the config", "set enabled = true in [tours]")
    return limits
