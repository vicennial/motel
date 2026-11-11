"""The channel to a tour guest: unprivileged ssh, with fixed remote commands and hard byte caps.

The desk is root, so every ssh runs as the unprivileged ``motel-cabin`` system user (``setpriv``/``runuser``),
like the cabin guest channel. The per-attempt private key is a root-written, group-readable file in the cabin
key tree. The remote commands are constants; the only variable parts are an input name from a short allowlist
and a relative path that ``artifacts.safe_relpath`` already accepted. Output read back from a guest is capped,
and the process is killed on overflow or on timeout. The guest is untrusted: the controller never reads
unbounded guest output.
"""

from __future__ import annotations

import json
import os
import select
import shlex
import signal
import subprocess
import time
from collections.abc import Iterator
from dataclasses import dataclass

from motel.cabins.errors import GuestUnknown
from motel.cabins.guestchan import drop_prefix, ssh_options
from motel.tours.artifacts import Entry, safe_relpath
from motel.tours.guest import GUEST_USER, INPUT_DIR, LOG_CAP_BYTES, RUNNER_DIR, TOUR_READY

IN_DIR = INPUT_DIR                          # created by cloud-init: root:guest 0750
GUEST_USER = GUEST_USER
GUEST_HOME = "~guest"                        # expanded by the shell of the cabin account
OUT_DIR = GUEST_HOME + "/out"                # the runner writes run.json, result.bundle, artifacts/ here
# The guest output, one NUL-ended "<type> <size> <path>" record each. -P (never follow links) goes before the path, and
# find starts from /: it must return to its starting directory, and the ssh account's home is closed to the guest.
LIST_SCRIPT = f"cd / && exec /usr/bin/find -P {OUT_DIR} -mindepth 1 -printf '%y %s %P\\0'"
INPUT_NAMES = ("card.json", "input.bundle", "overlay.patch")     # -> IN_DIR, root:guest 0640
RUNNER_NAMES = ("runner.py", "adapters.py")                      # -> RUNNER_DIR, root 0644
PUT_NAMES = RUNNER_NAMES + INPUT_NAMES
SSH_USER = "cabin"
MAX_LISTING_BYTES = 4 * 1024 * 1024
LOG_CAP = LOG_CAP_BYTES
POLL_S = 0.5


class TransportError(Exception):
    """A guest operation failed (message is safe to record)."""


class TooLarge(TransportError):
    pass


@dataclass(frozen=True)
class Target:
    """Where a tour guest is and which files authenticate us to it."""

    lease_id: str
    alias: str
    address: str
    key_path: str
    known_hosts: str


@dataclass
class RunResult:
    exit_status: int | None
    timed_out: bool = False
    cancelled: bool = False
    output_bytes: int = 0


def kill_group(proc) -> None:
    try:
        os.killpg(proc.pid, signal.SIGKILL)
    except (ProcessLookupError, PermissionError, OSError):
        try:
            proc.kill()
        except OSError:
            pass


class SshTransport:
    def __init__(self, *, run_as, runner_argv, ssh="/usr/bin/ssh", exists=os.path.exists, popen=subprocess.Popen,
                 monotonic=time.monotonic):
        self.run_as, self.ssh, self.exists, self.popen, self.monotonic = run_as, ssh, exists, popen, monotonic
        self.runner_argv = list(runner_argv)

    # -- command lines
    def argv(self, target: Target, remote: str, *, forward_port: int | None = None, forward_to: int | None = None,
             timeout_connect: int = 20) -> list:
        options = ssh_options(target.known_hosts, target.alias)
        if forward_port is not None:     # a tour guest needs the reverse forward to the concierge
            options = ["ClearAllForwardings=no" if o == "ClearAllForwardings=yes" else o for o in options]
        argv = [self.ssh, "-F", "/dev/null", "-i", target.key_path, *options, "-o", f"ConnectTimeout={timeout_connect}",
                "-o", "ServerAliveInterval=15", "-o", "ServerAliveCountMax=4", "-T"]
        if forward_port is not None:
            argv += ["-o", "ExitOnForwardFailure=yes", "-R", f"127.0.0.1:{forward_port}:127.0.0.1:{forward_to or forward_port}"]
        argv += ["-p", "22", "-l", SSH_USER, "--", target.address, remote]
        try:
            return drop_prefix(self.run_as, self.exists) + argv
        except GuestUnknown as exc:
            raise TransportError(str(exc)) from None

    @staticmethod
    def put_command(name: str) -> str:
        """Write one file on the guest. Inputs go to the group-readable input directory, which cloud-init made
        (root:guest 0750); the runner goes to the root-owned runner directory. Neither directory is created or
        re-moded here."""
        if name in INPUT_NAMES:
            script = f"umask 027 && cat > {IN_DIR}/{name} && chgrp {GUEST_USER} {IN_DIR}/{name}"
        elif name in RUNNER_NAMES:
            script = f"umask 022 && cat > {RUNNER_DIR}/{name}"
        else:
            raise TransportError("unknown input name")
        return f"sudo -n /bin/sh -c {shlex.quote(script)}"

    def run_command(self) -> str:
        return shlex.join(self.runner_argv)

    # -- process helpers
    def _spawn(self, argv, stdin):
        return self.popen(argv, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, close_fds=True,
                          env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, start_new_session=True)

    def _drain(self, proc, deadline: float, cap: int, cancel=None) -> Iterator[bytes]:
        """Yield stdout in chunks; kill the process on timeout, cancel or when more than ``cap`` bytes arrive."""
        fd, total = proc.stdout.fileno(), 0
        try:
            while True:
                if cancel is not None and cancel():
                    kill_group(proc)
                    raise TransportError("cancelled")
                left = deadline - self.monotonic()
                if left <= 0:
                    kill_group(proc)
                    raise TransportError("timed out")
                ready, _w, _x = select.select([fd], [], [], min(left, POLL_S))
                if not ready:
                    if proc.poll() is not None:
                        break
                    continue
                data = os.read(fd, 65536)
                if not data:
                    break
                total += len(data)
                if total > cap:
                    kill_group(proc)
                    raise TooLarge("the guest sent more than the allowed number of bytes")
                yield data
        finally:
            if proc.poll() is None:
                kill_group(proc)
            try:
                proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                pass
            proc.stdout.close()

    # -- operations
    def put(self, target: Target, name: str, source_path: str, *, timeout: float = 300) -> None:
        with open(source_path, "rb") as source:
            proc = self._spawn(self.argv(target, self.put_command(name)), source)
            for _ in self._drain(proc, self.monotonic() + timeout, 64 * 1024):
                pass
        if proc.returncode != 0:
            raise TransportError(f"copying {name} to the guest failed (exit {proc.returncode})")

    def preflight(self, target: Target, forward_port: int | None, *, forward_to: int | None = None, timeout: float = 60) -> None:
        """Open the exact channel the runner will use (with the reverse forward, when it has one) and check that the
        guest finished its tour setup (egress filter loaded, ``guest`` user present)."""
        proc = self._spawn(self.argv(target, f"/bin/cat {TOUR_READY}", forward_port=forward_port, forward_to=forward_to), subprocess.DEVNULL)
        data = b"".join(self._drain(proc, self.monotonic() + timeout, 4096))
        if proc.returncode != 0:
            raise TransportError("the guest channel" + (" with its reverse forward" if forward_port else "")
                                 + f" could not be opened (exit {proc.returncode})")
        try:
            ready = json.loads(data)
        except ValueError:
            raise TransportError("the guest did not report its tour setup") from None
        if not isinstance(ready, dict) or ready.get("tour_nft") != "loaded" or ready.get("guest_user") is not True:
            raise TransportError("the guest's tour egress filter is not loaded")

    def run(self, target: Target, *, forward_port: int | None, forward_to: int | None = None, timeout: float, cancel, log_path: str,
            stdin_line: bytes | None = None) -> RunResult:
        """Run the guest runner. Its console output is kept (capped) in ``log_path``. Once this is called the
        command counts as started: nothing here is retried.

        ``stdin_line`` is the own-login token as one line ``token\n``: it goes to the runner on ssh stdin and
        nowhere else (not argv, not the environment, not a file)."""
        proc = self._spawn(self.argv(target, self.run_command(), forward_port=forward_port, forward_to=forward_to),
                           subprocess.PIPE if stdin_line is not None else subprocess.DEVNULL)
        if stdin_line is not None:
            try:
                proc.stdin.write(stdin_line)
                proc.stdin.flush()
            except OSError:
                pass            # the ssh died early; the exit status tells
            finally:
                try:
                    proc.stdin.close()
                except OSError:
                    pass
        written = 0
        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC
        fd = os.open(log_path, flags, 0o600)
        timed_out = cancelled = False
        try:
            with os.fdopen(fd, "wb") as log:
                try:
                    for chunk in self._drain(proc, self.monotonic() + timeout, 1 << 40, cancel):
                        room = LOG_CAP - written
                        if room > 0:
                            log.write(chunk[:room])
                            written += min(len(chunk), room)
                except TransportError as exc:
                    timed_out, cancelled = str(exc) == "timed out", str(exc) == "cancelled"
        finally:
            if proc.poll() is None:
                kill_group(proc)
        return RunResult(None if (timed_out or cancelled) else proc.returncode, timed_out, cancelled, written)

    def stop(self, target: Target, *, timeout: float = 30) -> None:
        """Best effort: kill every process of the guest user."""
        proc = self._spawn(self.argv(target, f"sudo -n /usr/bin/pkill -KILL -u {GUEST_USER}"), subprocess.DEVNULL)
        try:
            for _ in self._drain(proc, self.monotonic() + timeout, 4096):
                pass
        except TransportError:
            pass

    def list_out(self, target: Target, *, timeout: float = 60) -> list:
        remote = f"sudo -n -u {GUEST_USER} /bin/sh -c {shlex.quote(LIST_SCRIPT)}"
        proc = self._spawn(self.argv(target, remote), subprocess.DEVNULL)
        data = b"".join(self._drain(proc, self.monotonic() + timeout, MAX_LISTING_BYTES))
        if proc.returncode != 0:
            raise TransportError(f"listing the guest output failed (exit {proc.returncode})")
        entries = []
        for record in data.split(b"\0"):
            if not record:
                continue
            kind, _, rest = record.decode("utf-8", "replace").partition(" ")
            size, _, path = rest.partition(" ")
            entries.append(Entry(path=path, kind=kind[:1] or "?", size=int(size) if size.isdigit() else 0))
        return entries

    def fetch(self, target: Target, rel: str, *, max_bytes: int, timeout: float = 600, truncate: bool = False) -> Iterator[bytes]:
        """Stream one regular file below ``out/`` (as the guest user), refusing more than ``max_bytes``; with
        ``truncate`` the first ``max_bytes`` are returned and the rest is dropped (for logs)."""
        if not safe_relpath(rel):
            raise TransportError("unsafe path refused")
        remote = f"sudo -n -u {GUEST_USER} /bin/cat -- {OUT_DIR}/{shlex.quote(rel)}"
        proc = self._spawn(self.argv(target, remote), subprocess.DEVNULL)
        if truncate:
            sent = 0
            for chunk in self._drain(proc, self.monotonic() + timeout, 1 << 40):
                room = max_bytes - sent
                if room > 0:
                    yield chunk[:room]
                    sent += min(len(chunk), room)
                if sent >= max_bytes:
                    kill_group(proc)          # enough: the rest of a long log is not wanted
                    return
            if proc.returncode != 0:
                raise TransportError(f"reading {rel} failed (exit {proc.returncode})")
            return
        yield from self._drain(proc, self.monotonic() + timeout, max_bytes)
        if proc.returncode != 0:
            raise TransportError(f"reading {rel} failed (exit {proc.returncode})")
