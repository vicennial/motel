"""Guest side of motel tours: the runner and adapters that execute on the guest, and the egress profile and image
bake that prepare it. See ``docs/tours-guest.md`` and ``docs/design/tours-contract.md``.

Engine entry points:

* :func:`userdata_extras` ``(egress_hosts, resolvers, *, concierge_port=None, own_login=False, ports=(443,),
  refresh_s=300, refresh_window_s=21600) -> motel.cabins.userdata.GuestExtras``: pass the result to
  ``render_user_data(..., extras=...)`` to make a cabin a tour guest.
* :func:`runner_files` ``() -> dict[str, bytes]``: ``runner.py`` and ``adapters.py``, to be copied together into
  :data:`RUNNER_DIR` on the guest (root-owned, mode 0644).
* :func:`runner_argv` ``() -> list[str]``: the command that runs the runner as the unprivileged ``guest`` user.

Constants the engine shares with the runner, so the two cannot drift: ``LOG_CAP_BYTES`` (bytes kept per output stream
= the most the engine fetches), ``RESERVED_ENV_NAMES`` / ``RESERVED_ENV_PREFIXES`` / :func:`env_name_reserved` (card
env names nobody may set), ``DEFAULT_MAX_OUTPUT_TOKENS`` / ``MIN_OUTPUT_TOKENS`` / ``MAX_OUTPUT_TOKENS_LIMIT`` (card field
``max_output_tokens``), and ``MAX_EGRESS_HOSTS`` / ``MAX_EGRESS_BYTES`` / ``EGRESS_HOST_OVERHEAD`` / :func:`egress_cost` (the
egress host budget: ``sum(len(h) + EGRESS_HOST_OVERHEAD) <= MAX_EGRESS_BYTES``, own-login's api.anthropic.com included).
"""

from __future__ import annotations

from importlib import resources

from motel.tours.guest.adapters import (
    DEFAULT_MAX_OUTPUT_TOKENS,
    LOG_CAP_BYTES,
    MAX_OUTPUT_TOKENS_LIMIT,
    MIN_OUTPUT_TOKENS,
    RESERVED_ENV_NAMES,
    RESERVED_ENV_PREFIXES,
    env_name_reserved,
)
from motel.tours.guest.egress import (
    EGRESS_HOST_OVERHEAD,
    GUEST_USER,
    INPUT_DIR,
    MAX_EGRESS_BYTES,
    MAX_EGRESS_HOSTS,
    OWN_LOGIN_HOSTS,
    RUNNER_DIR,
    TOUR_READY,
    USER_DATA_TARGET,
    egress_cost,
    userdata_extras,
)

RUNNER_FILES = ("runner.py", "adapters.py")


def runner_files() -> dict:
    """The guest-side files, read from the package data."""
    base = resources.files("motel.tours.guest")
    return {name: (base / name).read_bytes() for name in RUNNER_FILES}


def runner_argv(*, python="/usr/bin/python3") -> list:
    """argv that runs the runner as ``guest``: ``sudo`` from the cabin account (which has passwordless sudo)."""
    return ["sudo", "-n", "-u", GUEST_USER, "-H", "--", python, "-I", f"{RUNNER_DIR}/runner.py"]


__all__ = ["DEFAULT_MAX_OUTPUT_TOKENS", "EGRESS_HOST_OVERHEAD", "GUEST_USER", "INPUT_DIR", "LOG_CAP_BYTES",
           "MAX_EGRESS_BYTES", "MAX_EGRESS_HOSTS", "MAX_OUTPUT_TOKENS_LIMIT", "MIN_OUTPUT_TOKENS", "OWN_LOGIN_HOSTS",
           "RESERVED_ENV_NAMES", "RESERVED_ENV_PREFIXES", "RUNNER_DIR", "RUNNER_FILES", "TOUR_READY", "USER_DATA_TARGET", "egress_cost",
           "env_name_reserved", "runner_argv", "runner_files", "userdata_extras"]
