"""Errors and exit codes. Every error line starts with ``motel:`` and ends with a next command."""

from __future__ import annotations

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_REFUSED = 3
EXIT_BUSY = 4
EXIT_ERROR = 5
EXIT_GUARD = 6
EXIT_NOT_FOUND = 7
EXIT_NOT_SET_UP = 8
EXIT_INVENTORY = 9

# The one install hint every "not installed" message uses. The installer refuses a source that anybody else can change,
# so the routes are root's own uv cache or a root-owned clone; running install.sh from a user's own checkout fails.
INSTALL_CMD = "sudo uvx --from motel motel install"
INSTALL_ALT = "(or install.sh from a root-owned clone; see docs/install.md)"


def install_hint(args: str = "") -> str:
    """``sudo uvx --from motel motel install [ARGS] (or install.sh from a root-owned clone; see docs/install.md)``."""
    return f"{INSTALL_CMD}{' ' + args if args else ''} {INSTALL_ALT}"


INSTALL_HINT = install_hint()


class MotelError(Exception):
    """Base class. ``code`` is the process exit code, ``hint`` the suggested next command."""

    code = EXIT_ERROR
    default_hint = "motel doctor"

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message)
        self.message = str(message).rstrip(". ")
        self.hint = hint or self.default_hint

    def line(self) -> str:
        return f"motel: {self.message}. Try: {self.hint}"

    def as_json(self) -> dict:
        return {"schema": "motel/v1", "ok": False, "error": self.message, "hint": self.hint, "code": self.code}


class Usage(MotelError):
    code = EXIT_USAGE
    default_hint = "motel --help"


class Refused(MotelError):
    """A policy or cap refusal ("no vacancy")."""

    code = EXIT_REFUSED
    default_hint = "motel ls --mine"


class Busy(MotelError):
    code = EXIT_BUSY
    default_hint = "motel ls --mine"


class ProviderError(MotelError):
    """The cloud provider (or a system command) failed.

    ``indeterminate`` is true when a write may or may not have happened (lost response).
    """

    code = EXIT_ERROR

    def __init__(self, message: str, hint: str | None = None, *, indeterminate: bool = False,
                 volume_id: str | None = None):
        super().__init__(message, hint)
        self.indeterminate = indeterminate
        self.volume_id = volume_id  # set when a create got far enough for the cloud to hand out an id


class SystemFault(MotelError):
    code = EXIT_ERROR


class ConfigError(MotelError):
    code = EXIT_ERROR


class Guard(MotelError):
    """A safety guard refused to act."""

    code = EXIT_GUARD


class NotFound(MotelError):
    code = EXIT_NOT_FOUND
    default_hint = "motel ls --mine"


class NotSetUp(MotelError):
    code = EXIT_NOT_SET_UP
    default_hint = "sudo motel-desk setup hetzner|local"


class InventoryIncomplete(MotelError):
    code = EXIT_INVENTORY
