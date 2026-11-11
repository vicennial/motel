"""Keep each attempt's listening socket across a restart: systemd's file-descriptor store, with the standard library only.

The service hands every listener it opens to systemd (``FDSTORE=1`` with the descriptor attached to a datagram on
``NOTIFY_SOCKET``, named ``att-<attempt>``), and takes the listeners back on the next start (they arrive in
``LISTEN_FDS`` with ``LISTEN_FDNAMES``). While the service is down systemd keeps the port bound, so no other local
process can take it, and connections queue in the backlog until the service is back. The unit sets
``FileDescriptorStoreMax=`` (at least the largest port range) and, on systemd 254 or newer, ``FileDescriptorStorePreserve=yes``
(see ``install.unit_text``, and ``FdStore.check_unit`` for what the service checks at start).

The store is the only thing that keeps an attempt live across a restart. A socket the service closes and binds again leaves a
window in which another local process can hold the port, read the placeholder token from the cabin's reconnecting forward, and
let go before the service is back, so a start never binds a live attempt's port again: an attempt whose socket did not come back
is revoked (``lost_listeners``: ``not_preserved``). Consequently ``add`` failing (no notify socket, the store full) means the
attempt must not be issued at all. The one exception is the explicit ``--dev-rebind`` flag, which is unsafe.

The store is used only when systemd really started this process, which is when ``LISTEN_PID`` equals this process's pid (the
unit is socket-activated for the admin socket, so systemd always sets it). ``INVOCATION_ID`` is not accepted instead: it is an
ordinary environment variable that a child of a service inherits. ``NOTIFY_SOCKET`` is removed from the environment once read, so nothing this process starts inherits it.
"""

from __future__ import annotations

import array
import logging
import os
import socket

log = logging.getLogger("motel.concierge")

PREFIX = "att-"
MARKER_ENV = "MOTEL_FDSTORE_MAX"  # written by the installer next to FileDescriptorStoreMax=: its value, for systemd < 254
SIGNAL_ENV = "MOTEL_FDSTORE_SIGNAL"  # "1" when the unit was written for systemd >= 254, which then sets $FDSTORE
MAX_STORED = 512  # the default limit; the unit and the largest port range use settings.MAX_PORT_RANGE


def fd_name(attempt: str) -> str:
    return PREFIX + attempt


def listen_fd_names(environ=None, pid: int | None = None) -> list[tuple[int, str]]:
    """``(fd, name)`` for every descriptor systemd passed (``LISTEN_FDS``); the name is ``""`` when none was given."""
    from motel.concierge.admin import listen_fds
    env = os.environ if environ is None else environ
    fds = listen_fds(env, pid)
    names = env.get("LISTEN_FDNAMES", "").split(":") if env.get("LISTEN_FDNAMES") else []
    return [(fd, names[i] if i < len(names) else "") for i, fd in enumerate(fds)]


def manager_limit(environ=None) -> int | None:
    """``$FDSTORE``: the number of descriptors systemd will store for this service (``FileDescriptorStoreMax=``). Only systemd 254
    or newer sets it, and only when the store is enabled for the unit; None when missing or not a number."""
    env = os.environ if environ is None else environ
    raw = env.get("FDSTORE")
    return int(raw) if isinstance(raw, str) and raw.isascii() and raw.isdigit() else None


class FdStore:
    """Hand listening sockets to systemd and take them back; every call is refused or a no-op without a trustworthy notify socket."""

    def __init__(self, environ=None, *, limit: int = MAX_STORED, pid: int | None = None):
        real = environ is None
        env = os.environ if real else environ
        path = env.get("NOTIFY_SOCKET") or ""
        if real:
            os.environ.pop("NOTIFY_SOCKET", None)  # nothing we start may inherit it
        started_by_systemd = env.get("LISTEN_PID") == str(os.getpid() if pid is None else pid)
        if path.startswith("@"):  # abstract namespace
            path = "\0" + path[1:]
        self.path = path if started_by_systemd else ""
        self.limit = limit
        self._names: set[str] = set()  # attempts whose socket we believe the store holds

    @property
    def enabled(self) -> bool:
        return bool(self.path)

    def check_unit(self, needed: int, environ=None) -> str | None:
        """A refusal message when this is not a unit the installer wrote for a store large enough, else None.

        ``needed`` is the most attempts that can be live at once (the port range size). The unit's ``MOTEL_FDSTORE_MAX`` marker
        must be there (an old or hand-written unit has none) and at least ``needed``. ``$FDSTORE`` (systemd >= 254) must agree
        with it when set, and must be set when the unit says it was written for such a systemd (``MOTEL_FDSTORE_SIGNAL=1``): that
        catches a ``FileDescriptorStoreMax=`` systemd ignored. The store's own limit becomes the marker, so a full store is
        refused by ``add`` instead of dropped silently by systemd."""
        env = os.environ if environ is None else environ
        fix = "reinstall the unit: sudo motel install, then systemctl daemon-reload and restart motel-concierge"
        raw = env.get(MARKER_ENV)
        marker = int(raw) if isinstance(raw, str) and raw.isascii() and raw.isdigit() else None
        if marker is None:
            return f"the service unit has no {MARKER_ENV} marker, so it is an old or hand-written unit; {fix}"
        if marker < needed:
            return f"the unit's store is sized {marker} but the port range allows {needed} live attempts; {fix}"
        seen = manager_limit(env)
        if seen is not None and seen != marker:
            return f"systemd's $FDSTORE is {seen} but the unit's marker says {marker}; the unit and the manager disagree; {fix}"
        if seen is None and env.get(SIGNAL_ENV) == "1":
            return f"the unit was written for systemd 254 or newer but $FDSTORE is not set, so systemd ignored FileDescriptorStoreMax=; {fix}"
        self.limit = min(self.limit, marker)
        if marker < MAX_STORED:
            log.warning("the unit's store is sized %d, below the code's limit of %d; this service will refuse attempts beyond %d",
                        marker, MAX_STORED, marker)
        return None

    def _send(self, message: str, fds: list[int] | None = None) -> bool:
        if not self.path:
            return False
        try:
            with socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM | socket.SOCK_CLOEXEC) as notify:
                if fds:
                    rights = (socket.SOL_SOCKET, socket.SCM_RIGHTS, array.array("i", fds))  # send_fds() takes no address
                    notify.sendmsg([message.encode()], [rights], 0, self.path)
                else:
                    notify.sendto(message.encode(), self.path)
            return True
        except OSError as exc:
            log.warning("could not tell systemd about %s (%s)", message.split("\n")[0], exc)
            return False

    def add(self, attempt: str, sock: socket.socket) -> bool:
        """Store the socket. False when it cannot be kept: no notify socket, the store (``limit``) is full, or the send failed."""
        if attempt not in self._names and len(self._names) >= self.limit:
            log.error("the file-descriptor store is full (%d); not storing the socket of attempt %s", self.limit, attempt)
            return False
        if not self._send(f"FDSTORE=1\nFDNAME={fd_name(attempt)}\n", [sock.fileno()]):
            return False
        self._names.add(attempt)
        return True

    def adopt(self, attempt: str) -> None:
        """Note that the store already holds this attempt's socket (it came back at start)."""
        self._names.add(attempt)

    def remove(self, attempt: str) -> bool:
        """Drop every stored descriptor with this attempt's name."""
        self._names.discard(attempt)
        return self._send(f"FDSTOREREMOVE=1\nFDNAME={fd_name(attempt)}\n")


def inherited_listeners(environ=None, pid: int | None = None) -> tuple[dict[str, list[socket.socket]], list[int]]:
    """Split the descriptors systemd passed into ``({attempt: [listening sockets]}, other fds)``.

    The store does not keep names unique, so one attempt can come back with several sockets; the caller decides which is
    right and closes and removes the others (``Endpoints.restore``). The other fd is the admin socket."""
    stored: dict[str, list[socket.socket]] = {}
    other: list[int] = []
    for fd, name in listen_fd_names(environ, pid):
        if name.startswith(PREFIX):
            try:
                stored.setdefault(name[len(PREFIX):], []).append(socket.socket(fileno=fd))
            except OSError:
                log.warning("stored descriptor %d (%s) is not a socket; ignored", fd, name)
        else:
            other.append(fd)
    return stored, other
