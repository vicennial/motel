"""What ``motel install`` does for tours (only when ``[tours] enabled = true``).

The concierge part defines the user, directories and units (``motel.concierge.install``); this module applies them
through the installer's helpers, so ``--prefix`` and ``--dry-run`` keep working. It also creates the engine's own
root-only directories.

The concierge reads ``/etc/motel/motel.toml`` as its own user, so that file stays world-readable; it holds no
secrets. The LLM keys live in ``/etc/motel/llm-keys`` (root:motel-concierge 0750, files 0640). ``/etc/motel/credentials``
stays root 0700: only the own-login token (if the operator opts in) is placed below it.
"""

from __future__ import annotations

import os
import shutil
import subprocess

from motel.errors import Usage, install_hint
from motel.tours import deps

OWN_LOGIN_DIR = "/etc/motel/credentials/own-login"
STATE_DIR = "/var/lib/motel/tours"
ENTRY_LIB = "/usr/local/lib/motel"
UNIT_DIR = "/etc/systemd/system"
TOURS_SERVICE = "motel-tours-housekeeping.service"
TOURS_TIMER = "motel-tours-housekeeping.timer"
INTERVAL_MINUTES = 10


def render_tours_service(desk: str = "/usr/local/sbin/motel-desk") -> str:
    """Its own unit, like the cabin reaper: a hung rooms or cabins pass must not delay the tour reaper (tours bill by
    the hour through their cabins), and a failure of one pass must not hide another."""
    return ("[Unit]\nDescription=motel tour housekeeping (restart dead supervisors, finish cancelled tours, retry unfinished "
            f"cleanups)\n\n[Service]\nType=oneshot\nExecStart={desk} tour reap --apply --json\nNice=10\nIOSchedulingClass=idle\n"
            "TimeoutStartSec=15min\n")


def render_tours_timer() -> str:
    return ("[Unit]\nDescription=run motel tour housekeeping every "
            f"{INTERVAL_MINUTES} minutes\n\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec={INTERVAL_MINUTES}min\nAccuracySec=30s\n"
            f"Unit={TOURS_SERVICE}\nPersistent=true\n\n[Install]\nWantedBy=timers.target\n")


def install_tours_housekeeping(inst) -> None:
    """Write the tour reaper's unit and timer (the caller reloads systemd and enables the timer)."""
    inst.write(f"{UNIT_DIR}/{TOURS_SERVICE}", render_tours_service(), 0o644)
    inst.write(f"{UNIT_DIR}/{TOURS_TIMER}", render_tours_timer(), 0o644)


def inspect_user():
    """The unprivileged account that reads guest bundles (``bundle.INSPECT_USER``): system, no home, no login shell."""
    from motel.tours.bundle import INSPECT_USER
    return deps.concierge_install().UserSpec(name=INSPECT_USER, group=INSPECT_USER)


def install_tours(inst, *, systemd: bool) -> None:
    """Create the engine directories, the concierge user, its directories and units; enable the units (real root install)."""
    part = deps.concierge_install()
    layout = part.layout()
    inst.mkdir("/etc/motel/credentials", 0o700)
    inst.mkdir(OWN_LOGIN_DIR, 0o700)       # root only; the operator may put a token here (docs/tours.md)
    inst.mkdir(STATE_DIR, 0o700)
    inst.mkdir("/var/lib/motel-inspect", 0o711)      # per-attempt scratch for the inspection user (bundle.INSPECT_ROOT)
    _ensure_user(inst, layout.user)
    _ensure_user(inst, inspect_user())       # git reads guest bundles as this user, never as root
    for entry in layout.dirs:
        _ensure_dir(inst, entry)
    inst.write(part.ENTRY_PATH, part.entry_text(ENTRY_LIB), 0o755)
    # ports come from [tours] in motel.toml; the unit keeps the listener sockets across a restart where systemd can
    install_units(inst, part, systemd=systemd)


def _read(inst, path):
    try:
        with open(inst.at(path), encoding="utf-8") as handle:
            return handle.read()
    except (OSError, UnicodeDecodeError):
        return None


def install_units(inst, part, *, systemd: bool, version="detect", run=None) -> None:
    """Write the concierge's units that are missing or differ, reload systemd, restart the service when its own unit
    changed, then enable both units (socket first). ``--dry-run`` only logs the plan; ``--no-systemd`` (``systemd`` false)
    writes the files and starts nothing.

    A unit left by an older installer (the aab7907 one spelled the store key ``FileDescriptorStore=``, which systemd ignores)
    is rewritten here, not only on a first install: the service refuses to start without the right key and marker."""
    run = subprocess.run if run is None else run
    if version == "detect":
        version = part.detect_systemd_version()
    if version is None:
        inst.log("warning: the systemd version is unknown, so motel-concierge.service is written in the shape for systemd older "
                 "than 254 (no FileDescriptorStorePreserve=): `systemctl stop` then releases the stored listener sockets")
    changes = part.unit_changes(lambda path: _read(inst, path), version)
    service = f"{part.UNIT_DIR}/{part.SERVICE_NAME}"
    before = _read(inst, service)
    for change in changes:
        inst.write(change.path, change.text, change.mode)
    if not changes:
        inst.log("the concierge units are up to date")
    installed = next((c.text for c in changes if c.path == service), None) if inst.dry else _read(inst, service)
    problems = part.unit_problems(installed if installed is not None else before or "")
    if problems:
        raise Usage("the concierge service unit is not usable: " + "; ".join(problems),
                    install_hint("--upgrade") + ", then motel doctor")
    if before is not None and any(c.path == service for c in changes) and part.unit_problems(before):
        inst.log("note: the unit being replaced has no working descriptor store, so the live attempts of running tours end "
                 "once, as not_preserved, when the service restarts: upgrade between tours")
    steps = tuple(part.after_unit_changes(changes)) + tuple(("systemctl", "enable", "--now", unit) for unit in part.layout().enable)
    if not systemd:
        inst.log("note: --no-systemd or --cron: nothing is reloaded or started; run: " + "; ".join(" ".join(s) for s in steps))
    elif inst.dry:
        for step in steps:
            inst.log("would run: " + " ".join(step))
    elif inst.real:
        for step in steps:
            _systemctl(inst, run, step)


def _systemctl(inst, run, step) -> None:
    done = run(list(step), check=False, capture_output=True, text=True)
    if getattr(done, "returncode", 0) != 0:
        inst.log(f"warning: `{' '.join(step)}` failed: {(getattr(done, 'stderr', '') or '').strip()[:200]}")


def _ensure_user(inst, user) -> None:
    inst.log(f"ensure system user and group {user.name} (no login shell)")
    if inst.dry or not inst.real:
        return
    import grp
    import pwd
    try:
        entry = pwd.getpwnam(user.name)
    except KeyError:
        entry = None
    if entry is not None:
        try:
            primary = grp.getgrgid(entry.pw_gid).gr_name
        except KeyError:
            primary = None
        if primary != user.group:
            raise Usage(f"the {user.name} user exists but its primary group is {primary!r}, not {user.group!r}",
                        f"sudo usermod --gid {user.group} {user.name}")
        return
    args = user.useradd_args()
    useradd = shutil.which("useradd", path="/usr/sbin:/usr/bin:/sbin:/bin")
    if useradd is None:
        raise Usage(f"useradd is not installed, so the {user.name} user cannot be created", "sudo apt install passwd")
    nologin = next((p for p in ("/usr/sbin/nologin", "/sbin/nologin", "/usr/bin/false") if os.path.exists(p)), user.shell)
    args = [useradd, *args[1:]]
    args[args.index("--shell") + 1] = nologin
    proc = subprocess.run(args, capture_output=True, text=True, stdin=subprocess.DEVNULL)
    if proc.returncode != 0:
        raise Usage(f"could not create the {user.name} user: {proc.stderr.strip()[:200]}", "sudo " + " ".join(user.useradd_args()))


def _ensure_dir(inst, entry) -> None:
    inst.log(f"mkdir {entry.path} {entry.mode:o} {entry.owner}:{entry.group}")
    if inst.dry:
        return
    target = inst.at(entry.path)
    if not entry.create and not os.path.isdir(target):
        return
    os.makedirs(target, mode=entry.mode & 0o777, exist_ok=True)
    os.chmod(target, entry.mode)
    if inst.real:
        import grp
        import pwd
        os.chown(target, pwd.getpwnam(entry.owner).pw_uid, grp.getgrnam(entry.group).gr_gid)
