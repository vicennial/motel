"""Errors of the concierge package. They follow the project style: ``motel: <what>. Try: <command>``."""

from __future__ import annotations

from motel.errors import EXIT_ERROR, EXIT_NOT_FOUND, EXIT_REFUSED, EXIT_USAGE, MotelError


class ConciergeError(MotelError):
    """Base class: the concierge is unreachable, refused an admin request or is misconfigured."""

    code = EXIT_ERROR
    default_hint = "systemctl status motel-concierge"


class ConfigError(ConciergeError):
    """The ``[tours]`` section is invalid."""

    code = EXIT_USAGE
    default_hint = "edit /etc/motel/motel.toml ([tours] section; see docs/concierge.md)"


class AdminRefused(ConciergeError):
    """The admin socket understood the request and said no (``kind`` is a short machine-readable reason)."""

    code = EXIT_REFUSED

    def __init__(self, message: str, kind: str = "refused", hint: str | None = None):
        super().__init__(message, hint)
        self.kind = kind


class AttemptNotFound(AdminRefused):
    code = EXIT_NOT_FOUND

    def __init__(self, message: str, hint: str | None = None):
        super().__init__(message, "not_found", hint)
