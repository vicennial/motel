"""Installer: copies the desk to root-owned locations and renders the sudoers rule and housekeeping units.

``install.sh`` (from a checkout) and ``sudo motel install`` (from a wheel) are thin wrappers around :func:`main`.
``--prefix DIR`` installs below DIR without touching ownership, systemd, users or the real sudoers (for packaging and
tests); ``--dry-run`` only prints the plan; ``--upgrade`` replaces an existing installation and keeps its config
and state.

The desk always runs from a root-owned copy in ``/usr/local/lib/motel`` with the system interpreter
(``/usr/bin/python3 -I``), never from the venv or uvx cache that happens to hold the wheel: a user can write there.
"""

from __future__ import annotations

import argparse
import os
import shutil
import stat
import subprocess
import sys
import tomllib

from motel.errors import INSTALL_CMD, INSTALL_HINT, MotelError, SystemFault, Usage, install_hint
from motel.providers import registry
from motel.tours.install import TOURS_SERVICE, TOURS_TIMER, install_tours, install_tours_housekeeping
from motel.units import USER_RE

LIB = "/usr/local/lib/motel"
DESK = "/usr/local/sbin/motel-desk"
CLI = "/usr/local/bin/motel"
USER_VERBS = ("checkin", "ls", "status", "extend", "checkout", "restore", "doctor")
# Cabin verbs an ordinary user may run, pinned one per sudoers line (tests check this equals motel.cabins.desk.USER_VERBS).
# `setup`, `reap` and `resolve` are operator verbs: root only.
CABIN_USER_VERBS = ("checkin", "ls", "status", "extend", "checkout", "doctor", "images")
# Tour verbs an ordinary user may run (tests check this equals motel.tours.desk.USER_VERBS). `supervise`, `reap`, `bake`,
# `image` and `own-login` are operator verbs: root only.
TOUR_USER_VERBS = ("start", "ls", "status", "wait", "result", "fetch", "logs", "cancel")
# What the installer does about tours (see tours_mode), like cabins: OFF installs nothing, CLEANUP keeps the concierge, the
# reaper and the verbs that only read or end a tour (no `start`), ON also lets users start tours.
TOURS_OFF, TOURS_CLEANUP, TOURS_ON = False, "cleanup", True
TOUR_CLEANUP_VERBS = tuple(v for v in TOUR_USER_VERBS if v != "start")
# What the installer does about cabins (see cabin_mode): OFF installs nothing, CLEANUP keeps the reaper and the
# user verbs that only look at or end a cabin, ON also lets users check cabins in and extend them.
CABINS_OFF, CABINS_CLEANUP, CABINS_ON = False, "cleanup", True
UNIT_DIR = "/etc/systemd/system"
CABIN_SERVICE = "motel-cabins-housekeeping.service"
CABIN_TIMER = "motel-cabins-housekeeping.timer"
CABIN_CLEANUP_VERBS = ("ls", "status", "checkout")
CABIN_USER = "motel-cabin"
CABIN_HOME = "/var/lib/motel-cabin"
INTERVAL_MINUTES = 10


def validate_users(users: list) -> list:
    """Users and ``@groups`` must be plain lowercase account names: never ALL, never a pattern."""
    if not users:
        raise Usage("allow_users is empty", install_hint("--users alice"))
    for entry in users:
        name = entry[1:] if entry.startswith("@") else entry
        if not USER_RE.fullmatch(name):
            raise Usage(f"{entry!r} is not a plain user name or @group", install_hint("--users alice,@agents"))
    return list(users)


def render_sudoers(users: list, desk: str = DESK, cabins=CABINS_OFF, tours=TOURS_OFF) -> str:
    users = validate_users(users)
    who = ",".join("%" + u[1:] if u.startswith("@") else u for u in users)
    commands = []
    for verb in USER_VERBS:
        if verb != "checkin":
            commands.append(f"{desk} {verb}")
        commands.append(f"{desk} {verb} *")
    if cabins:  # one line per verb, never `cabin *`: setup, reap and resolve stay root-only
        for verb in (CABIN_USER_VERBS if cabins is CABINS_ON else CABIN_CLEANUP_VERBS):
            if verb not in ("checkin", "status", "extend", "checkout"):
                commands.append(f"{desk} cabin {verb}")
            commands.append(f"{desk} cabin {verb} *")
    if tours:  # one line per verb, never `tour *`: supervise, reap, bake, image and own-login stay root-only
        for verb in (TOUR_USER_VERBS if tours is TOURS_ON else TOUR_CLEANUP_VERBS):
            if verb not in ("start", "status", "wait", "result", "fetch", "logs", "cancel"):
                commands.append(f"{desk} tour {verb}")
            commands.append(f"{desk} tour {verb} *")
    return ("# Managed by motel's installer: users in allow_users may run these front-desk verbs as root.\n"
            "# `setup` and `reap`" + (" (and `cabin setup|reap|resolve`)" if cabins else "")
            + (" (and `tour supervise|reap|bake|image|own-login`)" if tours else "")
            + " are deliberately absent: they are operator commands.\n"
            f"Defaults!{desk} env_reset, !setenv\n"
            f"{who} ALL=(root) NOPASSWD: " + ", \\\n    ".join(commands) + "\n")


def render_service(desk: str = DESK) -> str:
    return ("[Unit]\nDescription=motel housekeeping (expire rooms, empty lost & found, retire empty floors)\n\n"
            f"[Service]\nType=oneshot\nExecStart={desk} reap --apply --json\nNice=10\nIOSchedulingClass=idle\n"
            "TimeoutStartSec=15min\n")


def render_cabin_service(desk: str = DESK) -> str:
    """Its own unit: a hung rooms pass must not delay the cabin reaper, and cabins bill by the hour."""
    return ("[Unit]\nDescription=motel cabin housekeeping (delete expired and idle cabins, retry unfinished cleanups)\n\n"
            f"[Service]\nType=oneshot\nExecStart={desk} cabin reap --apply --json\nNice=10\nIOSchedulingClass=idle\n"
            "TimeoutStartSec=15min\n")


def render_timer(what: str = "housekeeping", unit: str | None = None) -> str:
    text = ("[Unit]\nDescription=run motel " + what + " every "
            f"{INTERVAL_MINUTES} minutes\n\n[Timer]\nOnBootSec=2min\nOnUnitActiveSec={INTERVAL_MINUTES}min\nAccuracySec=30s\n")
    if unit:
        text += f"Unit={unit}\n"
    return text + "Persistent=true\n\n[Install]\nWantedBy=timers.target\n"


def render_cron(desk: str = DESK, cabins=CABINS_OFF, tours=TOURS_OFF) -> str:
    text = (f"# motel housekeeping (alternative to the systemd timer)\n"
            f"*/{INTERVAL_MINUTES} * * * * root {desk} reap --apply --json >/dev/null\n")
    if cabins:  # a separate cron job: one pass never waits for the other
        text += f"*/{INTERVAL_MINUTES} * * * * root {desk} cabin reap --apply --json >/dev/null\n"
    if tours:  # and another: a hung tour pass never delays the cabin reaper
        text += f"*/{INTERVAL_MINUTES} * * * * root {desk} tour reap --apply --json >/dev/null\n"
    return text


def render_entry(module: str, lib: str = LIB) -> str:
    return (f"#!/usr/bin/python3 -I\n# Installed by motel's install.sh. -I ignores PYTHON* variables and the current directory.\n"
            f"import sys\nif sys.version_info < (3, 11):\n    sys.exit('motel: Python 3.11 or newer is required. Try: sudo apt install python3')\n"
            f"sys.path.insert(0, {lib!r})\nfrom {module} import main\nraise SystemExit(main())\n")


def render_config(users: list, provider: str, cabins: bool = False, tours: bool = False) -> str:
    users = validate_users(users)
    if provider not in registry.provider_names():
        raise Usage(f"unknown provider {provider!r}", "sudo motel install --provider " + "|".join(registry.provider_names()))
    listed = ", ".join(f'"{u}"' for u in users)
    text = (f'# /etc/motel/motel.toml: see docs/configuration.md\n[host]\nprovider = "{provider}"   # or any of: '
            + ", ".join(registry.provider_names()) + '\n'
            f'server_id = "auto"       # filled in by `motel-desk setup`\nlocation = "auto"\n'
            f'allow_users = [{listed}]   # users and @groups allowed to run the front desk\n\n'
            '[rooms]\nmount_root = "/mnt/motel"\nfilesystem = "ext4"\ndefault_ttl = "6h"\nmax_ttl_call = "7d"\n'
            'max_lifetime = "14d"\ngrace = "6h"\nlost_and_found = "24h"\nmin_floor_gb = 50\n\n'
            '[rooms.caps]\nmax_floors = 3\nmax_floor_gb = 500\nmax_total_gb = 1000\nmonthly_eur = 30\n')
    if cabins:
        text += ('\n# Cabins: disposable servers. Use a NEW, EMPTY cloud project for them; see docs/cabins.md.\n'
                 '[cabins]\nenabled = true\ntypes = ["cpx32"]\nmax_eur_per_hour = 0.20\n')
    if tours:
        text += ('\n# Tours: swarms of bounded agent jobs on cabins. API keys stay with the concierge; see docs/tours.md.\n'
                 '[tours]\nenabled = true\nconcierge_port = 7781\nmax_concurrent_guests = 4\nmax_guests_per_tour = 20\n'
                 'max_eur_per_tour = 5.0\nmax_usd_model_per_guest = 2.0\nmax_ttl = "4h"\n\n'
                 '[tours.own_login]   # opt-in: use your own Claude login instead of API keys (see docs/tours.md)\nenabled = false\n')
    return text


def load_existing_config(path: str) -> dict | None:
    """The parsed config, None when there is none. A config that exists but cannot be read is an error: the
    installer must never guess users or cabin settings around it."""
    try:
        with open(path, "rb") as handle:
            return tomllib.load(handle)
    except FileNotFoundError:
        return None
    except (OSError, tomllib.TOMLDecodeError) as exc:
        raise Usage(f"{path} exists but cannot be read ({exc}); the installer will not guess around it",
                    f"fix {path} (sudo motel-desk doctor), then run this again") from exc


def cabin_state_exists(state_dir: str, receipt: str) -> bool:
    """Leases or a setup receipt of cabins: servers may still be running and billing.

    Only "does not exist" (ENOENT) means there is no state. Any other error (permissions, I/O) means the state may be
    there and unreadable, so it counts as in use: a reaper that is kept by mistake costs nothing, one that is removed
    by mistake can strand a server.
    """
    try:
        os.lstat(receipt)
        return True
    except FileNotFoundError:
        pass
    except OSError:
        return True
    try:
        with os.scandir(state_dir) as entries:
            return any(True for _ in entries)
    except FileNotFoundError:
        return False
    except OSError:
        return True


def cabin_mode(config: dict | None, *, state_exists: bool = False, wanted: bool = False):
    """CABINS_ON when the config enables cabins; CABINS_CLEANUP when it only mentions them (``enabled = false``, which
    means "no new checkins") or cabin state exists (also when there is no config at all), because the reaper and the
    checkout verbs must outlive the switch; otherwise CABINS_OFF. ``wanted`` (``--cabins``) matters only for a new config."""
    if config is None:  # no config yet: live cabin state still gets its reaper, whatever --cabins says about the new config
        return CABINS_ON if wanted else (CABINS_CLEANUP if state_exists else CABINS_OFF)
    table = config.get("cabins")
    if isinstance(table, dict) and table.get("enabled") is True:
        return CABINS_ON
    return CABINS_CLEANUP if (table is not None or state_exists) else CABINS_OFF


def tours_from_data(config: dict | None) -> bool | None:
    """True when the config enables tours, False when it has no (or a disabled) [tours] table, None without a config."""
    if config is None:
        return None
    table = config.get("tours")
    return isinstance(table, dict) and table.get("enabled") is True


def tours_mode(config: dict | None, *, state_exists: bool = False, wanted: bool = False, cabins=CABINS_ON):
    """TOURS_ON when the config enables tours (the raw ``enabled = true``, so an invalid table does not change the answer) and
    cabins are ON; TOURS_CLEANUP when it only mentions them (``enabled = false``: no new tours), when tour state exists, or when
    tours are enabled but cabins are not ON (tours run on cabins, so no new tour can start; what exists can still be read and
    ended); otherwise TOURS_OFF. ``wanted`` (``--tours``) matters only for a new config. An upgrade is never refused for this."""
    if config is None:
        return TOURS_ON if wanted else (TOURS_CLEANUP if state_exists else TOURS_OFF)
    table = config.get("tours")
    if isinstance(table, dict) and table.get("enabled") is True:
        return TOURS_ON if cabins is CABINS_ON else TOURS_CLEANUP
    return TOURS_CLEANUP if (table is not None or state_exists) else TOURS_OFF


def users_from_data(config: dict | None) -> list | None:
    users = (config or {}).get("host", {}).get("allow_users") if isinstance((config or {}).get("host"), dict) else None
    return list(users) if isinstance(users, list) and users else None


def provider_from_data(config: dict | None) -> str | None:
    host = (config or {}).get("host")
    value = host.get("provider") if isinstance(host, dict) else None
    return value if isinstance(value, str) else None


def read_version(package_dir: str) -> str:
    """``__version__`` of a package directory without importing it."""
    import re
    try:
        with open(os.path.join(package_dir, "__init__.py"), encoding="utf-8") as handle:
            match = re.search(r'^__version__\s*=\s*"([^"]+)"', handle.read(), re.M)
        return match.group(1) if match else "unknown"
    except OSError:
        return "unknown"


def audit_chain(path: str, *, owner_uid: int = 0) -> list:
    """Problems with a directory and every parent above it: each must be a directory owned by root (``owner_uid``)
    and not writable by group or others, or whoever can write there can replace root-run code."""
    problems, current = [], "/"
    real = os.path.realpath(path)
    for part in [""] + [p for p in real.split("/") if p]:
        current = os.path.join(current, part) if part else "/"
        try:
            st = os.lstat(current)
        except OSError as exc:
            problems.append(f"{current}: {exc.strerror}")
            continue
        if not stat.S_ISDIR(st.st_mode):
            problems.append(f"{current}: not a directory")
        if st.st_uid != owner_uid:
            problems.append(f"{current}: owned by uid {st.st_uid}, not root")
        if st.st_mode & 0o022:
            problems.append(f"{current}: writable by group or others (mode {stat.S_IMODE(st.st_mode):o})")
    return problems


def audit_tree(path: str, *, owner_uid: int = 0) -> list:
    """Problems with files below ``path``: not root-owned, or writable by group or others."""
    problems = []
    for base, dirs, files in os.walk(path):
        for name in dirs + files:
            full = os.path.join(base, name)
            st = os.lstat(full)
            if st.st_uid != owner_uid or (st.st_mode & 0o022 and not stat.S_ISLNK(st.st_mode)):
                problems.append(f"{full}: owner {st.st_uid}, mode {stat.S_IMODE(st.st_mode):o}")
    return problems


def _oserror_text(exc: BaseException) -> str:
    """A short reason from an OSError or a ``shutil.Error`` (whose argument is a list of failures)."""
    if isinstance(exc, OSError) and exc.strerror:
        return f"{exc.strerror}: {exc.filename}" if exc.filename else exc.strerror
    return str(exc)[:200]


def audit_links(path: str) -> list:
    """Symlinks and non-regular files below ``path`` (always refused, whoever owns them)."""
    out = []
    for base, dirs, files in os.walk(path):
        for name in dirs + files:
            full = os.path.join(base, name)
            try:
                mode = os.lstat(full).st_mode
            except OSError as exc:
                out.append(f"{full}: {exc.strerror}")
                continue
            if stat.S_ISLNK(mode):
                out.append(f"{full}: is a symbolic link")
            elif not (stat.S_ISREG(mode) or stat.S_ISDIR(mode)):
                out.append(f"{full}: is not a regular file or directory")
    return out


def audit_source(package_dir: str, *, owner_uid: int = 0, chain: bool = True) -> list:
    """Problems that make a source tree unfit to be copied into root's code directory.

    Root copies this code and later runs it, so whoever can write it (or any directory above it) can run code as
    root: every directory above must be root-owned and not writable by others (``chain``), and every entry below must
    be a root-owned regular file or directory that group and others cannot write. Symlinks, devices, sockets and
    pipes are refused outright (a link could pull a secret into the installed, world-readable copy).
    """
    problems = audit_chain(os.path.dirname(os.path.realpath(package_dir)), owner_uid=owner_uid) if chain else []
    try:
        top = os.lstat(package_dir)
    except OSError as exc:
        return problems + [f"{package_dir}: {exc.strerror}"]
    entries = [(package_dir, top)]
    for base, dirs, files in os.walk(package_dir):
        for name in dirs + files:
            full = os.path.join(base, name)
            try:
                entries.append((full, os.lstat(full)))
            except OSError as exc:
                problems.append(f"{full}: {exc.strerror}")
    for full, st in entries:
        if stat.S_ISLNK(st.st_mode):
            problems.append(f"{full}: is a symbolic link")
            continue
        if not (stat.S_ISREG(st.st_mode) or stat.S_ISDIR(st.st_mode)):
            problems.append(f"{full}: is not a regular file or directory")
        if st.st_uid != owner_uid:
            problems.append(f"{full}: owned by uid {st.st_uid}, not root")
        if st.st_mode & 0o022:
            problems.append(f"{full}: writable by group or others (mode {stat.S_IMODE(st.st_mode):o})")
    return problems


def interpreter_problems(executable: str | None = None, *, owner_uid: int = 0) -> list:
    """Problems with where the running Python lives (it and the tools that started it run as root during an install)."""
    return audit_chain(os.path.dirname(os.path.realpath(executable or sys.executable)), owner_uid=owner_uid)


def audit_entry(path: str, module: str, *, lib: str = LIB, owner_uid: int = 0) -> list:
    """Problems with an installed entry file (``motel-desk`` or ``motel``): it must be a regular file (not a link)
    owned by root, not writable by group or others, and contain exactly what the installer writes for ``module``."""
    try:
        st = os.lstat(path)
    except OSError as exc:
        return [f"{path}: {exc.strerror}"]
    problems = []
    if not stat.S_ISREG(st.st_mode):
        return [f"{path}: not a regular file"]
    if st.st_uid != owner_uid:
        problems.append(f"{path}: owned by uid {st.st_uid}, not root")
    if st.st_mode & 0o022:
        problems.append(f"{path}: writable by group or others (mode {stat.S_IMODE(st.st_mode):o})")
    try:
        with open(path, encoding="utf-8") as handle:
            if handle.read() != render_entry(module, lib):
                problems.append(f"{path}: content differs from what install.sh writes")
    except (OSError, UnicodeDecodeError) as exc:
        problems.append(f"{path}: unreadable ({exc})")
    return problems


class Installer:
    def __init__(self, prefix: str, src_dir: str, *, dry_run: bool, log=print):
        self.prefix, self.src, self.dry, self.log = prefix.rstrip("/") or "/", src_dir, dry_run, log
        self.real = self.prefix == "/"

    def at(self, path: str) -> str:
        return path if self.real else os.path.join(self.prefix, path.lstrip("/"))

    def _own(self, path: str) -> None:
        if self.real:
            os.chown(path, 0, 0)

    def mkdir(self, path: str, mode: int) -> None:
        self.log(f"mkdir {path} {mode:o}")
        if not self.dry:
            os.makedirs(self.at(path), mode=mode, exist_ok=True)
            os.chmod(self.at(path), mode)
            self._own(self.at(path))

    def mkdir_group(self, path: str, mode: int, group: str) -> None:
        """A directory owned by root with the group ``group`` (the unprivileged user reads it but cannot write)."""
        self.log(f"mkdir {path} {mode:o} root:{group}")
        if self.dry:
            return
        target = self.at(path)
        os.makedirs(target, mode=mode & 0o777, exist_ok=True)
        os.chmod(target, mode)  # includes the setgid bit, which makedirs would drop
        if self.real:
            import grp
            os.chown(target, 0, grp.getgrnam(group).gr_gid)

    def ensure_cabin_user(self) -> None:
        """The system user and group the guest ssh client runs as. It owns nothing: its home is root's."""
        self.log(f"ensure system user and group {CABIN_USER} (no login shell, home {CABIN_HOME} owned by root)")
        if self.dry or not self.real:
            return
        import grp
        import pwd
        try:
            entry = pwd.getpwnam(CABIN_USER)
        except KeyError:
            entry = None
        if entry is not None:  # the ssh client runs with --clear-groups, so the tree's group must be its primary group
            try:
                primary = grp.getgrgid(entry.pw_gid).gr_name
            except KeyError:
                primary = None
            if primary != CABIN_USER:
                raise Usage(f"the {CABIN_USER} user exists but its primary group is {primary!r}, not {CABIN_USER!r}",
                            f"sudo usermod --gid {CABIN_USER} {CABIN_USER}")
            return
        useradd = shutil.which("useradd", path="/usr/sbin:/usr/bin:/sbin:/bin")
        if useradd is None:
            raise Usage("useradd is not installed, so the motel-cabin user cannot be created", "sudo apt install passwd")
        nologin = next((p for p in ("/usr/sbin/nologin", "/sbin/nologin", "/usr/bin/false") if os.path.exists(p)), "/usr/bin/false")
        proc = subprocess.run([useradd, "--system", "--user-group", "--no-create-home", "--home-dir", CABIN_HOME,
                               "--shell", nologin, CABIN_USER], capture_output=True, text=True, stdin=subprocess.DEVNULL)
        if proc.returncode != 0:
            raise Usage(f"could not create the {CABIN_USER} user: {proc.stderr.strip()[:200]}",
                        f"sudo useradd --system --user-group --no-create-home --home-dir {CABIN_HOME} {CABIN_USER}")

    def remove(self, path: str) -> bool:
        """Delete an installed file if it is there (a unit that no longer applies). Returns True when it existed."""
        target = self.at(path)
        existed = os.path.lexists(target)
        if existed:
            self.log(f"remove {path}")
            if not self.dry:
                os.unlink(target)
        return existed

    def write(self, path: str, text: str, mode: int, replace: bool = True) -> None:
        self.log(f"write {path} {mode:o}")
        if self.dry:
            return
        target = self.at(path)
        if not replace and os.path.exists(target):
            return
        os.makedirs(os.path.dirname(target), exist_ok=True)
        tmp = target + ".new"
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, mode)
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.chmod(tmp, mode)
        self._own(tmp)
        os.replace(tmp, target)

    def copy_package(self) -> None:
        """Copy the package into root's code directory: stage it privately, then swap with two renames."""
        src = os.path.join(self.src, "motel")
        dst = self.at(f"{LIB}/motel")
        self.log(f"copy {src} -> {dst} (root-owned, read-only)")
        refused = [p for p in audit_links(src)]
        if refused:
            raise Usage("refusing to install a source tree with links or special files: " + "; ".join(refused[:4]),
                        "install from a clean checkout or wheel")
        if self.dry:
            return
        staging, old = dst + ".new", dst + ".old"
        unchanged = "the install did not finish"
        try:
            # A run that died between the two renames below left the working copy in ".old", the finished new copy in
            # ".new" and nothing in place: that pair is the signature of the death, so put the working copy back before
            # anything is cleaned up. ".old" alone is not that signature and is discarded like any other leftover.
            if os.path.isdir(old) and os.path.isdir(staging) and not os.path.lexists(dst):
                os.rename(old, dst)
            unchanged = ("the installed copy was not changed" if os.path.lexists(dst)
                         else "nothing was installed")
            for leftover in (staging, old):
                shutil.rmtree(leftover, ignore_errors=True)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.mkdir(staging, 0o700)  # nobody else can look into it while it is being filled
            os.chmod(staging, 0o700)
            shutil.copytree(src, staging, symlinks=True, copy_function=shutil.copyfile, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns("__pycache__", "*.pyc", "*.orig", "*.rej"))
            refused = audit_links(staging)  # what was copied, not what was scanned a moment ago
            if refused:
                shutil.rmtree(staging, ignore_errors=True)
                raise Usage("the source tree changed while it was copied: " + "; ".join(refused[:4]), "run the install again")
            for base, _dirs, files in os.walk(staging):
                os.chmod(base, 0o755)
                self._own(base)
                for name in files:
                    os.chmod(os.path.join(base, name), 0o644)
                    self._own(os.path.join(base, name))
        except (OSError, shutil.Error) as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise SystemFault(f"could not copy the package into {dst}: {_oserror_text(exc)} ({unchanged})",
                              "free disk space or fix the permissions of /usr/local/lib, then run the same command again") from exc
        # swap: the old copy is renamed aside, never deleted before the new one is in place
        had_old = os.path.lexists(dst)
        unchanged = "the installed copy was not changed" if had_old else "nothing was installed"
        try:
            if had_old:
                os.rename(dst, old)
        except OSError as exc:
            shutil.rmtree(staging, ignore_errors=True)
            raise SystemFault(f"could not move the installed copy aside: {_oserror_text(exc)} ({unchanged})",
                              "fix the permissions of /usr/local/lib, then run the same command again") from exc
        try:
            os.rename(staging, dst)
        except OSError as exc:
            if had_old:
                try:
                    os.rename(old, dst)
                except OSError as back:
                    raise SystemFault(f"could not put the new copy in place ({_oserror_text(exc)}) and could not restore the "
                                      f"old one ({_oserror_text(back)}); the working copy is {old}",
                                      f"sudo mv {old} {dst}") from exc
            shutil.rmtree(staging, ignore_errors=True)
            raise SystemFault(f"could not put the new copy in place: {_oserror_text(exc)} ({unchanged})",
                              "fix the permissions of /usr/local/lib, then run the same command again") from exc
        shutil.rmtree(old, ignore_errors=True)

    def check_sudoers(self, text: str, check: bool) -> None:
        visudo = shutil.which("visudo", path="/usr/sbin:/usr/bin:/sbin:/bin")
        if not check:
            return
        if visudo is None:
            if self.real:
                raise Usage("visudo is not installed, so the sudoers rule cannot be checked", "sudo apt install sudo")
            self.log("visudo not found; skipping the sudoers syntax check")
            return
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".sudoers", delete=False, encoding="utf-8") as handle:
            handle.write(text)
            probe = handle.name
        try:
            proc = subprocess.run([visudo, "-cqf", probe], capture_output=True, text=True)
        finally:
            os.unlink(probe)
        if proc.returncode != 0:
            raise Usage(f"visudo rejected the generated rule: {proc.stderr.strip()[:200]}", install_hint("--users alice"))
        self.log("visudo: rule is valid")


def check_system_python(log=print) -> None:
    """The entry files run under ``/usr/bin/python3 -I``, not under the interpreter that is running the installer."""
    python = "/usr/bin/python3"
    try:
        proc = subprocess.run([python, "-I", "-c", "import sys; print(int(sys.version_info >= (3, 11)))"],
                              capture_output=True, text=True, stdin=subprocess.DEVNULL, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise Usage(f"{python} cannot run: {exc}", "sudo apt install python3") from exc
    if proc.stdout.strip() != "1":
        raise Usage(f"{python} is older than Python 3.11, which the front desk needs", "sudo apt install python3 (3.11 or newer)")
    log(f"{python}: Python 3.11 or newer")


def install(prefix: str, src_dir: str, *, users: list | None, provider: str | None, systemd: bool, cron: bool, dry_run: bool,
            visudo: bool = True, log=print, audit: bool | None = None, owner_uid: int = 0, cabins: bool | None = None,
            upgrade: bool = False, audit_src: bool | None = None, cmd: str = "sudo motel install",
            tours: bool | None = None) -> str:
    """Install (or, with ``upgrade``, replace) the desk. Returns the provider name in the config.

    ``audit`` checks the directories root-run code is installed into; ``audit_src`` checks the tree it is copied
    from (both default to on for the real filesystem).
    """
    inst = Installer(prefix, src_dir, dry_run=dry_run, log=log)
    audit = inst.real if audit is None else audit
    audit_src = inst.real if audit_src is None else audit_src
    config_path = inst.at("/etc/motel/motel.toml")
    config = load_existing_config(config_path)  # raises if it exists but does not parse: never guess around it
    installed_pkg = inst.at(f"{LIB}/motel")
    source_pkg = os.path.join(src_dir, "motel")
    if upgrade and (not os.path.isdir(installed_pkg) or config is None):
        raise Usage("nothing to upgrade: motel is not installed here", f"{cmd} --provider hetzner")
    if os.path.isdir(installed_pkg) and os.path.realpath(source_pkg) == os.path.realpath(installed_pkg):
        raise Usage(f"the source is the installed copy itself ({installed_pkg}, version {read_version(installed_pkg)}): "
                    "that would copy it onto itself and upgrade nothing",
                    install_hint("--upgrade"))
    provider = (provider_from_data(config) if upgrade else None) or provider or provider_from_data(config) or "hetzner"
    if provider not in registry.provider_names():
        raise Usage(f"unknown provider {provider!r}", f"{cmd} --provider " + "|".join(registry.provider_names()))
    users = users_from_data(config) or users
    if not users and config is None and os.environ.get("SUDO_USER"):
        users = [os.environ["SUDO_USER"]]  # only for a brand-new config; an existing one is never guessed around
    users = validate_users(users or [])
    # The config is the one source of truth for cabins; --cabins only chooses what a NEW config says. Disabling
    # cabins stops new checkins; the reaper and the verbs that end a cabin stay while any cabin state exists.
    mode = cabin_mode(config, wanted=bool(cabins),
                      state_exists=cabin_state_exists(inst.at("/var/lib/motel/cabins"), inst.at("/etc/motel/cabins-receipt.json")))
    if cabins and config is not None and mode is not CABINS_ON:
        log("note: the existing config does not enable [cabins]; add it (see docs/cabins.md) and run this again to install cabins")
    # Tours, like cabins, are on only when the config says so; --tours only chooses what a NEW config says. They run on
    # cabins. Disabling tours stops new tours; the concierge, the reaper and the verbs that read or end a tour stay while
    # tours are mentioned in the config or tour state exists (unfinished tours bill by the hour).
    tours_configured = tours_from_data(config)
    if config is None and tours and mode is not CABINS_ON:
        raise Usage("tours run on cabins, and cabins are not enabled", f"add --cabins: {cmd} --cabins --tours")
    tmode = tours_mode(config, wanted=bool(tours), cabins=mode,
                       state_exists=cabin_state_exists(inst.at("/var/lib/motel/tours"), inst.at("/etc/motel/tours-receipt.json")))
    if tours and tours_configured is False:
        log("note: the existing config does not enable [tours]; add it (see docs/tours.md) and run this again to install tours")
    if tours_configured and mode is not CABINS_ON:
        log("note: [tours] is enabled but [cabins] is not, so tours are installed in cleanup mode: no new tour can start "
            f"and users keep the verbs that read or end a tour. Enable [cabins] (docs/cabins.md) and run {cmd} again")
    if upgrade or os.path.isdir(installed_pkg):
        log(f"motel {read_version(installed_pkg)} -> {read_version(source_pkg)}")
    if audit_src:
        problems = audit_source(source_pkg, owner_uid=owner_uid, chain=inst.real)
        if problems:
            raise Usage("refusing to install code that someone else can change from " + source_pkg + ": " + "; ".join(problems[:4]),
                        f"install from a root-owned source: {INSTALL_HINT}")
    if inst.real:
        odd = interpreter_problems(owner_uid=owner_uid)
        if odd:  # the code this installer is made of comes from there: someone who can change it can change what root runs
            log("warning: the Python running this installer is where others can change it ("
                + "; ".join(odd[:2]) + "); install from root's own uv (a root-owned uv in /usr/local/bin) or a root-owned clone")
    if inst.real and not dry_run:
        check_system_python(log)
    sudoers = render_sudoers(users, cabins=mode, tours=tmode)
    inst.check_sudoers(sudoers, visudo)
    # Root runs this code through sudo, so nobody else may be able to write it or any directory above it.
    inst.mkdir(LIB, 0o755)  # root:root 0755, set explicitly rather than inherited from a umask
    for directory in (os.path.dirname(DESK), os.path.dirname(CLI)):
        if not dry_run and not os.path.isdir(inst.at(directory)):
            inst.mkdir(directory, 0o755)
    if audit and not dry_run:
        for directory in (LIB, os.path.dirname(DESK), os.path.dirname(CLI)):
            problems = audit_chain(inst.at(directory), owner_uid=owner_uid)
            if problems:
                raise Usage("refusing to install root-run code where others can replace it: " + "; ".join(problems[:4]),
                            f"sudo chmod go-w on those directories, then run: {cmd}")
    inst.copy_package()
    if mode:
        inst.ensure_cabin_user()
        inst.mkdir_group(CABIN_HOME, 0o750, CABIN_USER)
        inst.mkdir_group(f"{CABIN_HOME}/keys", 0o2750, CABIN_USER)
        for tool in ("setpriv", "runuser"):
            if inst.real and shutil.which(tool, path="/usr/sbin:/usr/bin:/sbin:/bin"):
                break
        else:
            if inst.real:
                log("warning: neither setpriv nor runuser was found; cabin checkins will report a missing prerequisite")
    inst.write(DESK, render_entry("motel.desk"), 0o755)
    inst.write(CLI, render_entry("motel.cli"), 0o755)
    inst.mkdir("/etc/motel", 0o755)
    inst.mkdir("/etc/motel/credentials", 0o700)
    inst.mkdir("/var/lib/motel", 0o700)
    inst.write("/etc/motel/motel.toml", render_config(users, provider, mode is CABINS_ON, bool(tours) and config is None), 0o644,
               replace=False)
    if tmode:
        install_tours(inst, systemd=systemd and not cron)
    inst.write("/etc/sudoers.d/motel", sudoers, 0o440)
    if cron:
        inst.write("/etc/cron.d/motel", render_cron(cabins=mode, tours=tmode), 0o644)
    elif systemd:
        inst.write("/etc/systemd/system/motel-housekeeping.service", render_service(), 0o644)
        inst.write("/etc/systemd/system/motel-housekeeping.timer", render_timer(), 0o644)
        timers = ["motel-housekeeping.timer"]
        if mode:
            inst.write(f"{UNIT_DIR}/{CABIN_SERVICE}", render_cabin_service(), 0o644)
            inst.write(f"{UNIT_DIR}/{CABIN_TIMER}", render_timer("cabin housekeeping"), 0o644)
            timers.append(CABIN_TIMER)
        else:  # cabins are off and nothing is left to clean up: an earlier install's cabin reaper has no job any more
            if inst.real and not dry_run and os.path.lexists(inst.at(f"{UNIT_DIR}/{CABIN_TIMER}")):
                subprocess.run(["systemctl", "disable", "--now", CABIN_TIMER], check=False)
            for name in (CABIN_TIMER, CABIN_SERVICE):
                inst.remove(f"{UNIT_DIR}/{name}")
        if tmode:
            install_tours_housekeeping(inst)      # its own unit and timer: a hung pass never delays another one
            timers.append(TOURS_TIMER)
        else:
            if inst.real and not dry_run and os.path.lexists(inst.at(f"{UNIT_DIR}/{TOURS_TIMER}")):
                subprocess.run(["systemctl", "disable", "--now", TOURS_TIMER], check=False)
            for name in (TOURS_TIMER, TOURS_SERVICE):
                inst.remove(f"{UNIT_DIR}/{name}")
        if inst.real and not dry_run:
            subprocess.run(["systemctl", "daemon-reload"], check=False)
            for timer in timers:
                subprocess.run(["systemctl", "enable", "--now", timer], check=False)
    return provider


def main(argv=None, *, prog: str = "motel install") -> int:
    """``prog`` names the command the user typed: ``motel install`` (a wheel) or ``install.sh`` (a checkout)."""
    again = INSTALL_CMD
    parser = argparse.ArgumentParser(prog=prog, description="Install the motel front desk (run as root).")
    parser.add_argument("--prefix", default="/", help="install below this directory (packaging and tests)")
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--upgrade", action="store_true",
                        help="replace the installed code, sudoers rule and units; keep the config, credentials and state")
    parser.add_argument("--users", default=None, help="comma-separated users and @groups (default: allow_users in an existing config, else $SUDO_USER)")
    parser.add_argument("--provider", default=None, choices=registry.provider_names(),
                        help="cloud for a new config (default hetzner; an upgrade keeps the configured one)")
    parser.add_argument("--cabins", action="store_true", help="a new config enables cabins (an existing config decides otherwise)")
    parser.add_argument("--tours", action="store_true", help="a new config enables tours (needs cabins; an existing config decides otherwise)")
    parser.add_argument("--cron", action="store_true", help="write /etc/cron.d/motel instead of the systemd timer")
    parser.add_argument("--no-systemd", action="store_true", help="install neither timer nor cron (schedule `motel-desk reap --apply` yourself, and with cabins also "
                        "`motel-desk cabin reap --apply`: cabins bill by the hour)")
    parser.add_argument("--no-visudo", action="store_true", help="skip the visudo syntax check")
    parser.add_argument("--src", default=None, help="directory that contains the `motel` package (default: where this module is installed)")
    args = parser.parse_args(argv)
    src = args.src or os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    if args.prefix.rstrip("/") in ("", "/") and not args.dry_run and os.geteuid() != 0:
        print(f"{prog}: run as root, because it writes /usr/local, /etc and the sudoers rule. Try: {again}", file=sys.stderr)
        return 2
    try:
        provider = install(args.prefix, src, users=[u for u in (args.users or "").split(",") if u] or None,
                           provider=args.provider, systemd=not args.no_systemd, cron=args.cron, dry_run=args.dry_run,
                           visudo=not args.no_visudo, cabins=args.cabins or None, upgrade=args.upgrade,
                           cmd=again, tours=args.tours or None)
    except MotelError as exc:
        print(f"{prog}: {exc.message}. Try: {exc.hint}", file=sys.stderr)
        return exc.code
    except OSError as exc:  # any other file operation: a message, never a traceback
        print(f"{prog}: {_oserror_text(exc)}. Try: check the disk space and the permissions of /usr/local, /etc and /var/lib, "
              f"then run {again} again", file=sys.stderr)
        return SystemFault.code
    if not args.dry_run:
        if args.upgrade:
            print("upgraded; config, credentials and state were kept. Next: motel doctor")
        else:
            print(f"installed. Next: sudo motel-desk setup {provider}   (then: motel doctor)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
