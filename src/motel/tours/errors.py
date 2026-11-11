"""Errors for tours. Same convention and exit codes as cabins; only the default hints differ."""

from __future__ import annotations

from motel.cabins import errors as _base
from motel.cabins.errors import (  # noqa: F401  (re-exported)
    EXIT_BUSY,
    EXIT_ERROR,
    EXIT_GUARD,
    EXIT_INCOMPLETE,
    EXIT_NOT_FOUND,
    EXIT_NOT_SET_UP,
    EXIT_OK,
    EXIT_REFUSED,
    EXIT_USAGE,
    Fail,
)

HINT = "motel tour ls"


class Usage(_base.Usage):
    default_hint = "motel tour --help"


class Refused(_base.PolicyRefused):
    """A ceiling or policy refusal."""

    default_hint = HINT


class NotFound(_base.NotFound):
    default_hint = HINT


class NotSetUp(_base.Prerequisite):
    default_hint = "sudo motel-desk cabin doctor"


class Guarded(_base.Guarded):
    default_hint = HINT


class Unavailable(_base.Unavailable):
    default_hint = HINT


class Incomplete(_base.Incomplete):
    default_hint = HINT


class Broken(Fail):
    """An internal fault in a tour (state unreadable, impossible transition)."""

    default_hint = HINT
