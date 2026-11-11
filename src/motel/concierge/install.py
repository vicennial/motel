"""What the installer must create for the concierge: the systemd units, the system user, the directories and their modes.

The engine part wires these into ``installer.py``. Nothing here touches the system: it only returns text and data, so the
installer (and its ``--prefix`` mode) stays the single place that writes files.

Admin socket ownership (the decision, and why):

- The admin socket must be usable by root only, although the service runs as the unprivileged ``motel-concierge``.
- A socket the service creates itself is owned by the service user. That user could replace or re-mode it, and any
  bug that makes the service chmod it would open it to everyone.
- So the socket is created by systemd, not by the service: ``motel-concierge.socket`` listens on
  ``/run/motel/concierge.sock`` with ``SocketUser=root``, ``SocketGroup=root``, ``SocketMode=0600`` in a ``0700`` root
  directory, and hands the open socket to the service (``LISTEN_FDS``, fd 3). The service user never owns the file or its
  directory and cannot change either.
- The service also reads ``SO_PEERCRED`` on each connection and serves uid 0 only, so a wrong mode would still not be
  enough to use the socket.
- For that reason the service unit has no ``RuntimeDirectory=``: it would chown ``/run/motel`` to the service user and
  delete the socket when the service stops.

Key files live in ``/etc/motel/llm-keys`` (``root:motel-concierge 0750``) as ``root:motel-concierge 0640``. The service
needs no access to ``/etc/motel/credentials`` (root 0700: provider tokens and the own-login token, read by root only). The service reads ``/etc/motel/motel.toml``, so that file must
be readable by it (it holds no secrets; credentials live in separate files).
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass

from motel.concierge.fdstore import MARKER_ENV, SIGNAL_ENV
from motel.concierge.settings import MAX_PORT_RANGE

USER = "motel-concierge"
GROUP = "motel-concierge"
STATE_DIR = "/var/lib/motel-concierge"
SOCKET_DIR = "/run/motel"
SOCKET_PATH = "/run/motel/concierge.sock"
ENTRY_PATH = "/usr/local/sbin/motel-concierge"
LIB = "/usr/local/lib/motel"
SERVICE_NAME = "motel-concierge.service"
SOCKET_NAME = "motel-concierge.socket"
UNIT_DIR = "/etc/systemd/system"
KEY_DIR = "/etc/motel/llm-keys"  # the concierge's keys only; /etc/motel/credentials stays root 0700 and unreadable to it


@dataclass(frozen=True)
class UserSpec:
    name: str
    group: str
    home: str = "/nonexistent"
    shell: str = "/usr/sbin/nologin"

    def useradd_args(self) -> list[str]:
        """Arguments for ``useradd`` (the installer runs it only when the user does not exist)."""
        return ["useradd", "--system", "--user-group", "--no-create-home", "--home-dir", self.home, "--shell", self.shell, self.name]


@dataclass(frozen=True)
class DirSpec:
    path: str
    owner: str
    group: str
    mode: int
    create: bool = True  # False: the directory belongs to something else; only fix owner/group/mode if it exists
    note: str = ""


@dataclass(frozen=True)
class FileSpec:
    path: str
    owner: str
    group: str
    mode: int
    kind: str  # "service" | "socket" | "entry"


@dataclass(frozen=True)
class Layout:
    user: UserSpec
    dirs: tuple[DirSpec, ...]
    files: tuple[FileSpec, ...]
    socket_path: str
    service: str
    socket_unit: str
    enable: tuple[str, ...]  # units to `systemctl enable --now`, in order


def layout() -> Layout:
    return Layout(
        user=UserSpec(USER, GROUP),
        dirs=(
            DirSpec(STATE_DIR, USER, GROUP, 0o700, note="ledger; the only place the service writes"),
            DirSpec(KEY_DIR, "root", GROUP, 0o750, note="LLM API key files, one per binding: root:motel-concierge 0640, written by the operator"),
        ),
        files=(
            FileSpec(f"{UNIT_DIR}/{SERVICE_NAME}", "root", "root", 0o644, "service"),
            FileSpec(f"{UNIT_DIR}/{SOCKET_NAME}", "root", "root", 0o644, "socket"),
            FileSpec(ENTRY_PATH, "root", "root", 0o755, "entry"),
        ),
        socket_path=SOCKET_PATH, service=SERVICE_NAME, socket_unit=SOCKET_NAME, enable=(SOCKET_NAME, SERVICE_NAME))


def entry_text(lib: str = LIB) -> str:
    """The root-owned launcher (same pattern as the desk entry: system interpreter, ``-I``, no user-writable path)."""
    return ("#!/usr/bin/python3 -I\n# Installed by motel's installer. -I ignores PYTHON* variables and the current directory.\n"
            "import sys\nif sys.version_info < (3, 11):\n    sys.exit('motel: Python 3.11 or newer is required. Try: sudo apt install python3')\n"
            f"sys.path.insert(0, {lib!r})\nfrom motel.concierge.service import main\nraise SystemExit(main())\n")


PRESERVE_MIN_SYSTEMD = 254  # FileDescriptorStorePreserve= exists from this version


def detect_systemd_version(run=subprocess.run) -> int | None:
    """The major version of the running systemd (``systemctl --version``: "systemd 255 (255.4-1ubuntu8)"), or None if unknown.
    The installer passes the result to :func:`unit_text`."""
    try:
        out = run(["systemctl", "--version"], capture_output=True, text=True, timeout=10, check=False).stdout
    except (OSError, subprocess.SubprocessError):
        return None
    found = re.match(r"\s*systemd\s+([0-9]+)", out or "")
    return int(found.group(1)) if found else None


def unit_text(port: int | None = None, *, systemd_version: int | None = None) -> str:
    """``motel-concierge.service``: hardened, unprivileged, no capabilities, writes only its state directory.

    ``FileDescriptorStoreMax=`` (the systemd key; ``FileDescriptorStore=`` does not exist and is ignored, which leaves the store at
    0) equals the largest port range a binding may configure (one stored socket per live attempt). ``Environment=`` carries a
    marker, ``MOTEL_FDSTORE_MAX``, with the same number: systemd < 254 does not set ``$FDSTORE``, so the service reads the marker
    to know the unit is one this installer wrote, and refuses to start without it.

    On systemd 254 or newer (``systemd_version``, from :func:`detect_systemd_version`) the unit also has
    ``FileDescriptorStorePreserve=yes`` (the store survives an explicit stop too) and a second marker,
    ``MOTEL_FDSTORE_SIGNAL=1``, telling the service that systemd then sets ``$FDSTORE`` and that it must agree with the marker.
    On older or unknown systemd there is no Preserve line: the store (default behaviour, systemd.service(5)) survives crash
    restarts and ``systemctl restart``, but is released by an explicit ``systemctl stop``; a start after that ends the live
    attempts (``not_preserved``).

    ``port`` is accepted for callers written before the listeners became per-attempt and is ignored: the service reads its
    port range from ``[tours] concierge_port_range`` in ``/etc/motel/motel.toml``."""
    if port is not None and (not isinstance(port, int) or isinstance(port, bool) or not 1024 <= port <= 65535):
        raise ValueError(f"concierge port must be an integer from 1024 to 65535, not {port!r}")
    modern = systemd_version is not None and systemd_version >= PRESERVE_MIN_SYSTEMD
    preserve = "FileDescriptorStorePreserve=yes\n" if modern else ""
    signal = f"Environment={SIGNAL_ENV}=1\n" if modern else ""
    return f"""# Managed by motel's installer. Credential proxy for tours: see docs/concierge.md.
[Unit]
Description=motel concierge (credential proxy for tours)
Requires={SOCKET_NAME}
After={SOCKET_NAME} network-online.target
Wants=network-online.target
ConditionPathExists=/etc/motel/motel.toml

[Service]
Type=simple
User={USER}
Group={GROUP}
ExecStart={ENTRY_PATH}
Restart=on-failure
RestartSec=2s
# Each attempt's listening socket is handed to systemd and kept across restarts, so no other local process can take its port.
# The marker lets the service tell this unit from an old or hand-written one (systemd < 254 does not set $FDSTORE).
FileDescriptorStoreMax={MAX_PORT_RANGE}
Environment={MARKER_ENV}={MAX_PORT_RANGE}
{signal}{preserve}NotifyAccess=main
TimeoutStopSec=30s
# The admin socket is created by {SOCKET_NAME} (root, 0600) and passed in; no RuntimeDirectory here on purpose.
StateDirectory=motel-concierge
StateDirectoryMode=0700
ReadWritePaths={STATE_DIR}
UMask=0077
NoNewPrivileges=yes
CapabilityBoundingSet=
AmbientCapabilities=
ProtectSystem=strict
ProtectHome=yes
PrivateTmp=yes
PrivateDevices=yes
ProtectKernelTunables=yes
ProtectKernelModules=yes
ProtectKernelLogs=yes
ProtectControlGroups=yes
ProtectClock=yes
ProtectHostname=yes
ProtectProc=invisible
ProcSubset=pid
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6
RestrictNamespaces=yes
RestrictRealtime=yes
RestrictSUIDSGID=yes
LockPersonality=yes
MemoryDenyWriteExecute=yes
RemoveIPC=yes
SystemCallArchitectures=native
SystemCallFilter=@system-service
SystemCallFilter=~@privileged

[Install]
WantedBy=multi-user.target
"""


def socket_unit_text() -> str:
    """``motel-concierge.socket``: the root-only admin socket (see the module docstring)."""
    return f"""# Managed by motel's installer. The admin socket of the concierge: root only.
[Unit]
Description=motel concierge admin socket (root only)

[Socket]
ListenStream={SOCKET_PATH}
SocketUser=root
SocketGroup=root
SocketMode=0600
DirectoryMode=0700
Accept=no
RemoveOnStop=yes

[Install]
WantedBy=sockets.target
"""


@dataclass(frozen=True)
class UnitChange:
    path: str
    text: str
    mode: int
    action: str  # "create" (not there) | "update" (there, with other text)
    unit: str  # SERVICE_NAME or SOCKET_NAME


def unit_changes(read, systemd_version: int | None = None) -> tuple[UnitChange, ...]:
    """The units the installer must write: every one that is missing or whose text differs from what is installed.

    This is what makes an upgrade of an installed host rewrite the units too (a unit written by an earlier version lacks
    ``FileDescriptorStoreMax=`` or the marker, and the service refuses to start without them), not only a first install. ``read(path)`` returns the
    installed text or None. Both units are always compared, so a changed socket unit is rewritten as well."""
    changes = []
    for name, text in ((SERVICE_NAME, unit_text(systemd_version=systemd_version)), (SOCKET_NAME, socket_unit_text())):
        path = f"{UNIT_DIR}/{name}"
        have = read(path)
        if have != text:
            changes.append(UnitChange(path, text, 0o644, "create" if have is None else "update", name))
    return tuple(changes)


def after_unit_changes(changes) -> tuple[tuple[str, ...], ...]:
    """The systemctl calls that follow writing ``changes``: a reload whenever a unit was written, and a restart of the
    service when its own unit changed on a host where it exists (the store keeps each attempt's socket across it)."""
    if not changes:
        return ()
    steps = [("systemctl", "daemon-reload")]
    if any(c.unit == SERVICE_NAME and c.action == "update" for c in changes):
        steps.append(("systemctl", "restart", SERVICE_NAME))
    return tuple(steps)


def unit_problems(text: str) -> list[str]:
    """Problems with an installed service unit, for the installer's verify step: ``FileDescriptorStoreMax=`` and the
    ``MOTEL_FDSTORE_MAX`` marker must both be there and at least the largest port range (attempts would not survive a restart,
    and the service refuses to start without the marker)."""
    problems = []
    for label, pattern in (("FileDescriptorStoreMax=", r"^FileDescriptorStoreMax=([0-9]+)\s*$"),
                           (f"Environment={MARKER_ENV}=", rf"^Environment={MARKER_ENV}=([0-9]+)\s*$")):
        found = re.search(pattern, text, re.MULTILINE)
        if not found:
            problems.append(f"motel-concierge.service has no {label}; rewrite it (sudo motel install)")
        elif int(found.group(1)) < MAX_PORT_RANGE:
            problems.append(f"{label}{found.group(1)} is below {MAX_PORT_RANGE}; rewrite it (sudo motel install)")
    return problems
