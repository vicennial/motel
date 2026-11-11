"""Errors and exit codes for cabins.

Same convention as the core: every message line starts with ``motel:`` and ends with ``Try: <next command>``,
and the exit codes are the same (0 ok, 2 usage, 3 refused, 4 busy, 5 error, 6 guard, 7 not found,
8 not set up, 9 incomplete).
"""

from __future__ import annotations

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_BUSY = 4
EXIT_ERROR = 5
EXIT_GUARD = 6
EXIT_NOT_FOUND = 7
EXIT_NOT_SET_UP = 8
EXIT_INCOMPLETE = 9


class Fail(Exception):
    """Base class. ``code`` is the exit code, ``hint`` the suggested next command."""

    code = EXIT_ERROR
    default_hint = "motel cabin ls"

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = str(message).rstrip(". ")
        self.hint = hint or self.default_hint

    def line(self) -> str:
        return f"motel: {self.message}. Try: {self.hint}"

    def as_json(self) -> dict:
        return {"schema": "motel/v1", "ok": False, "error": self.message, "hint": self.hint, "code": self.code}


class Usage(Fail):
    code = EXIT_USAGE
    default_hint = "motel cabin --help"


class PolicyRefused(Fail):
    """A policy or cap refusal ("no vacancy")."""

    code = EXIT_REFUSED


class Busy(Fail):
    code = EXIT_BUSY


class Unavailable(Fail):
    code = EXIT_ERROR
    default_hint = "motel cabin ls"


class CheckinAborted(Fail):
    """The caller asked a checkin that was still waiting for the guest to give up (a cancelled tour, a stopping supervisor).
    The cabin was cleaned up like any failed checkin; the caller decides what the abort means."""


class Guarded(Fail):
    code = EXIT_GUARD
    default_hint = "sudo motel-desk cabin doctor"


class NotFound(Fail):
    code = EXIT_NOT_FOUND


class Prerequisite(Fail):
    code = EXIT_NOT_SET_UP
    default_hint = "sudo motel-desk cabin doctor"


class Incomplete(Fail):
    code = EXIT_INCOMPLETE


class ConfigError(Fail):
    code = EXIT_ERROR
    default_hint = "sudo motel-desk cabin doctor"


class GuestUnknown(Exception):
    """The guest could not be queried. Never treated as idle.

    ``rc`` is the exit status of the ssh client when it ran, ``detail`` the first line of its stderr (cleaned and redacted
    by ``guestchan.safe_detail``) or ``"timeout"``: the two things an operator needs to tell a closed port from a refused key."""

    def __init__(self, message="", *, rc=None, detail=None):
        super().__init__(message)
        self.rc, self.detail = rc, detail


class GuardRefused(Exception):
    """The provider write boundary refused. Nothing was written."""


class Pending(GuardRefused):
    """A transient provider state that time fixes, as opposed to a refusal that never will.

    Bootstrap: the firewall is not yet applied, or the cabin's own volume is not yet attached. Cleanup: the server
    still reads as present, or the volume as attached, a moment after the delete was requested. The bootstrap loop
    and the bounded cleanup retry (``Controller._retry_write``) treat it as "try again"; everywhere else it is an
    ordinary guard refusal."""


class ApiError(Exception):
    def __init__(self, status, code="api_error"):
        self.status, self.code = status, code
        super().__init__(f"provider HTTP {status}: {code}")
