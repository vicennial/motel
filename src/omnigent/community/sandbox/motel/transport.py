"""Talking to a cabin through the ``motel`` CLI, and the shell scripts the launcher sends. Standard library only.

Everything reaches a cabin through ``motel cabin ssh ID -- CMD...`` (the cabin's admission gate runs CMD as a
managed job in ``~/work``). Commands are never put in argv: the launcher sends a script on the child's stdin
(``sh -s``), so tokens and credential values stay out of every process list, on both machines.
"""

from __future__ import annotations

import hashlib
import ipaddress
import logging
import os
import shlex
import signal
import subprocess
import threading
import time
from collections import deque
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

from . import relay

log = logging.getLogger(__name__)

PRELUDE = 'PATH="$HOME/.local/bin:$PATH"; export PATH'
VENV = '"$HOME/.local/share/omnigent-motel/venv"'
SSH = "/usr/bin/ssh"


class TransportError(Exception):
    """A cabin command could not be run (not: it ran and failed)."""


@dataclass(frozen=True)
class Result:
    returncode: int
    stdout: str
    stderr: str


# -- scripts ------------------------------------------------------------------------------------------------


def wrap(script: str) -> str:
    """Put a script in one ``{ ...; }`` group. The shell reads a group completely before it runs any of it, so a script
    cut short on the pipe (a dropped ssh session) is a syntax error and runs nothing."""
    return "{\n" + script.rstrip("\n") + "\n}\n"


def export_lines(env: Mapping[str, str]) -> str:
    """``export NAME='value'`` lines. This text only ever travels on a pipe's stdin."""
    return "".join(f"export {name}={shlex.quote(value)}\n" for name, value in env.items())


def base_setup() -> str:
    """Python check, apt packages, the omnigent venv. The cabin user has passwordless sudo."""
    packages = "git tmux curl ca-certificates python3-venv python3-pip python3-yaml"
    return (
        "set -eu\n"
        f"{PRELUDE}\n"
        "DEBIAN_FRONTEND=noninteractive; export DEBIAN_FRONTEND\n"
        "python3 -c 'import sys; sys.exit(0 if sys.version_info >= (3, 12) else 1)' || "
        "{ echo 'motel: omnigent needs Python 3.12 or newer on the cabin; use the ubuntu-24.04 image' >&2; exit 1; }\n"
        "sudo -n apt-get update -q\n"
        f"sudo -n apt-get install -y -q {packages}\n"
        f"[ -x {VENV}/bin/pip ] || python3 -m venv {VENV}\n"
    )


HARNESS_LOG = "/tmp/omnigent-harness-install.log"
HARNESS_FAILED = "/tmp/omnigent-harness-install.failed"
"""Written when the background harness install fails, so a later failure can say why a harness CLI is missing."""


def link_omnigent() -> str:
    return f'mkdir -p "$HOME/.local/bin"\nln -sf {VENV}/bin/omnigent "$HOME/.local/bin/omnigent"\n'


def harness_install(harness_packages: Sequence[str], wait: bool) -> str:
    """Node and the coding-harness CLIs. They are not needed to start the host, so by default they install in the
    background (log: ``HARNESS_LOG``) while the host registers; ``wait`` installs them first."""
    if not harness_packages:
        return ""
    specs = " ".join(shlex.quote(p) for p in harness_packages)
    lines = ["set -e", PRELUDE, "DEBIAN_FRONTEND=noninteractive; export DEBIAN_FRONTEND",
             "sudo -n apt-get install -y -q nodejs npm",
             f'npm install -g --prefix "$HOME/.local" --no-audit --no-fund --loglevel=error {specs}']
    if wait:
        return "\n".join(lines[1:]) + "\n"
    inner = (f"if sh -c {shlex.quote(chr(10).join(lines))}; then rm -f {HARNESS_FAILED}; "
             f"else echo failed > {HARNESS_FAILED}; fi")
    return f"rm -f {HARNESS_FAILED}\nsetsid nohup sh -c {shlex.quote(inner)} > {HARNESS_LOG} 2>&1 < /dev/null &\n"


def harness_retry_script(harness_packages: Sequence[str]) -> str:
    """Install the harness CLIs again (in the background) when an earlier background install left its failure marker."""
    body = harness_install(harness_packages, False)
    return f"if [ -e {HARNESS_FAILED} ]; then\n{body}fi\n" if body else ""


def ensure_omnigent_script(version: str, harness_packages: Sequence[str], wait_harnesses: bool = False) -> str:
    """Install the pinned omnigent from PyPI when no ``omnigent`` is on the cabin's PATH. Safe to repeat."""
    return (
        f"{PRELUDE}\n"
        "if command -v omnigent >/dev/null 2>&1; then\n"
        + harness_retry_script(harness_packages if not wait_harnesses else [])
        + "exit 0\nfi\n"
        + base_setup()
        + f"{VENV}/bin/pip install --quiet --disable-pip-version-check {shlex.quote('omnigent==' + version)}\n"
        + link_omnigent()
        + harness_install(harness_packages, wait_harnesses)
    )


def wheel_install_script(remote_tgz: str, harness_packages: Sequence[str], wait_harnesses: bool = False) -> str:
    """Install the wheels ``omnigent sandbox create`` ships (built from a checkout), dependencies from PyPI."""
    tgz = shlex.quote(remote_tgz)
    return (
        base_setup()
        + "cd /tmp && rm -rf oa-wheels && mkdir oa-wheels\n"
        + f"tar xzf {tgz} -C oa-wheels --warning=no-unknown-keyword\n"
        + f"{VENV}/bin/pip install --quiet --disable-pip-version-check --force-reinstall "
        "--no-warn-script-location oa-wheels/*.whl\n"
        + link_omnigent()
        + harness_install(harness_packages, wait_harnesses)
    )


# -- the server URL a host will dial ------------------------------------------------------------------------


@dataclass(frozen=True)
class ServerTarget:
    scheme: str
    host: str
    port: int
    loopback: bool

    @property
    def url(self) -> str:
        return f"{self.scheme}://{self.host}:{self.port}"


def parse_server_target(command: str) -> ServerTarget | None:
    """The ``--server URL`` of an ``omnigent host`` command, or None when there is none."""
    try:
        words = shlex.split(command)
    except ValueError:
        return None
    for i, word in enumerate(words[:-1]):
        if word == "--server":
            url = words[i + 1]
            break
    else:
        return None
    try:
        parsed = urlparse(url)
        host, port = parsed.hostname, parsed.port
    except ValueError:
        return None
    if not host or parsed.scheme not in ("http", "https", "ws", "wss"):
        return None
    port = port or (443 if parsed.scheme in ("https", "wss") else 80)
    try:
        loopback = ipaddress.ip_address(host).is_loopback
    except ValueError:
        loopback = host.lower() == "localhost"
    return ServerTarget(parsed.scheme, host, port, loopback)


# -- running things on a cabin ------------------------------------------------------------------------------


def _group_gone(pid: int) -> bool:
    try:
        os.killpg(pid, 0)
    except (ProcessLookupError, PermissionError):
        return True
    return False


def _close_quietly(stream) -> None:
    try:
        if stream:
            stream.close()
    except (OSError, ValueError):
        pass


def stop_process(proc, grace: float = 3.0) -> None:
    """Stop a child started with ``start_new_session=True`` and everything it started.

    The real ``motel cabin ssh`` runs ``/usr/bin/ssh`` as a child, so ending only ``motel`` would leave the ssh session
    (and, on the cabin, the relay holding its port) running. Close stdin so a program that reads it can leave by itself,
    then signal the whole process group: TERM, then KILL. The close runs in a helper thread: a pump that is blocked
    writing to a full pipe holds the file's lock, so ``close`` would wait for it, and the signals must not wait. Once
    the process is gone the blocked write fails and the close finishes.
    """
    closer = threading.Thread(target=_close_quietly, args=(proc.stdin,), daemon=True)
    closer.start()
    closer.join(0.3)
    try:
        proc.wait(0.3)
    except subprocess.TimeoutExpired:
        pass
    for sig, wait in ((signal.SIGTERM, grace), (signal.SIGKILL, 2.0)):
        if _group_gone(proc.pid):
            break
        try:
            os.killpg(proc.pid, sig)
        except (ProcessLookupError, PermissionError):
            break
        end = time.monotonic() + wait
        while time.monotonic() < end and not (proc.poll() is not None and _group_gone(proc.pid)):
            time.sleep(0.05)
    for stream in (proc.stdout, proc.stderr):
        _close_quietly(stream)


class CabinTransport:
    """``motel cabin ssh`` as a function. ``popen`` and ``run`` are injectable for tests."""

    def __init__(self, binary: str = "motel", env: Mapping[str, str] | None = None, *,
                 run=subprocess.run, popen=subprocess.Popen):
        self.binary = binary
        self.env = dict(env) if env is not None else None
        self._run, self._popen = run, popen

    def argv(self, cabin: str, remote: Sequence[str]) -> list[str]:
        return [self.binary, "cabin", "ssh", cabin, "--", *remote]

    def run_script(self, cabin: str, script: str, timeout: float) -> Result:
        try:
            proc = self._run(self.argv(cabin, ["sh", "-s"]), input=wrap(script).encode(), capture_output=True,
                             timeout=timeout, check=False, env=self.env)
        except FileNotFoundError:
            raise TransportError(f"cannot find the motel executable {self.binary!r}") from None
        except subprocess.TimeoutExpired:
            raise TransportError(f"the command on cabin {cabin} did not finish within {timeout:g}s") from None
        return Result(proc.returncode, proc.stdout.decode("utf-8", "replace"), proc.stderr.decode("utf-8", "replace"))

    def popen_script(self, cabin: str, script: str, *, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=False,
                     remote: Sequence[str] = ("sh", "-s"), new_session: bool = False):
        """Start a command whose script is written to stdin at once. The caller reads and stops the process.

        ``new_session`` puts it in a process group of its own so :func:`stop_process` can end the whole tree."""
        try:
            proc = self._popen(self.argv(cabin, remote), stdin=subprocess.PIPE, stdout=stdout, stderr=stderr, text=text,
                               env=self.env, bufsize=1 if text else -1, start_new_session=new_session)
        except FileNotFoundError:
            raise TransportError(f"cannot find the motel executable {self.binary!r}") from None
        try:
            data = wrap(script)
            proc.stdin.write(data if text else data.encode())
            proc.stdin.close()
        except (OSError, ValueError):
            pass  # the process is gone; the caller sees its exit status
        return proc

    def put(self, cabin: str, local: Path, remote: str, timeout: float) -> None:
        """Copy a file to ``remote`` on the cabin and check its SHA-256 on both ends."""
        expected = hashlib.sha256()
        with open(local, "rb") as src:
            for chunk in iter(lambda: src.read(1 << 20), b""):
                expected.update(chunk)
        script = ["sh", "-c", 'umask 077 && mkdir -p "$(dirname "$1")" && cat > "$1"', "sh", remote]
        with open(local, "rb") as src:
            try:
                proc = self._run(self.argv(cabin, script), stdin=src, capture_output=True, timeout=timeout, check=False,
                                 env=self.env)
            except FileNotFoundError:
                raise TransportError(f"cannot find the motel executable {self.binary!r}") from None
            except subprocess.TimeoutExpired:
                raise TransportError(f"copying {Path(local).name} to cabin {cabin} timed out after {timeout:g}s") from None
        if proc.returncode != 0:
            raise TransportError(f"copying {Path(local).name} to cabin {cabin} failed: "
                                 f"{proc.stderr.decode('utf-8', 'replace').strip()[-300:]}")
        check = self.run_script(cabin, f"sha256sum -- {shlex.quote(remote)}\n", 120)
        if check.returncode != 0 or check.stdout.split()[:1] != [expected.hexdigest()]:
            raise TransportError(f"{Path(local).name} arrived on cabin {cabin} damaged (checksum mismatch)")


# -- tunnels ------------------------------------------------------------------------------------------------


def relay_source() -> str:
    return Path(relay.__file__).read_text(encoding="utf-8")


class RelayTunnel:
    """The stdio relay: ``python3 -c RELAY cabin PORT`` on the cabin, the other end in this process."""

    kind = "relay"

    def __init__(self, transport: CabinTransport, cabin: str, port: int, host: str, ready_timeout: float = 60.0,
                 max_streams: int = relay.DEFAULT_MAX_STREAMS):
        self.transport, self.cabin, self.port, self.host = transport, cabin, port, host
        self.ready_timeout, self.max_streams = ready_timeout, max_streams
        self.proc = None
        self.relay: relay.Relay | None = None
        self.err_tail: deque[str] = deque(maxlen=20)

    def start(self) -> None:
        argv = ["python3", "-I", "-c", relay_source(), "cabin", str(self.port), str(self.max_streams)]
        proc = self.transport._popen(self.transport.argv(self.cabin, argv), stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                                     stderr=subprocess.PIPE, env=self.transport.env, start_new_session=True)
        self.proc = proc
        end = relay.controller_relay(proc.stdout, proc.stdin, self.host, self.port, max_streams=self.max_streams)
        self.relay = end
        threading.Thread(target=end.serve, daemon=True).start()
        threading.Thread(target=self._drain, args=(proc.stderr,), daemon=True).start()
        end.ready.wait(self.ready_timeout)
        if not end.listening:
            detail = end.error or " | ".join(self.err_tail) or "no answer from the cabin"
            self.close()
            raise TransportError(f"the tunnel to cabin {self.cabin} did not start: {detail}")
        if end.stray:
            log.warning("cabin %s printed %d bytes before the relay started (a login script?); ignored: %r", self.cabin,
                        len(end.stray), end.stray[:120])

    def _drain(self, stream) -> None:
        try:
            for line in iter(stream.readline, b""):
                self.err_tail.append(line.decode("utf-8", "replace").strip()[:200])
        except (OSError, ValueError):
            pass

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None and not (self.relay and self.relay.stop.is_set())

    def close(self) -> None:
        if self.relay is not None:
            self.relay.close_all()
        if self.proc is not None:
            stop_process(self.proc)


def reverse_forward_argv(ssh_config: str, alias: str, port: int, host: str, target_port: int, ssh: str = SSH) -> list[str]:
    """``ssh -R`` for a cabin whose sshd allows remote forwarding. The stock cabin does not (see docs/omnigent.md)."""
    return [ssh, "-F", ssh_config, "-N", "-T", "-o", "ExitOnForwardFailure=yes", "-o", "ClearAllForwardings=no",
            "-o", "BatchMode=yes", "-R", f"127.0.0.1:{port}:{host}:{target_port}", "--", alias]


class ForwardTunnel:
    """``ssh -R`` with the cabin's access files. Fails at once (``ExitOnForwardFailure``) when the cabin refuses."""

    kind = "reverse-forward"

    def __init__(self, ssh_config: str, alias: str, port: int, host: str, popen=subprocess.Popen, settle: float = 4.0,
                 ssh: str = SSH):
        self.argv = reverse_forward_argv(ssh_config, alias, port, host, port, ssh)
        self.alias, self._popen, self.settle = alias, popen, settle
        self.proc = None

    def start(self) -> None:
        self.proc = self._popen(self.argv, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE,
                                start_new_session=True)
        try:
            code = self.proc.wait(self.settle)
        except subprocess.TimeoutExpired:
            return  # still running after the settle time: the forward is up
        detail = self.proc.stderr.read().decode("utf-8", "replace").strip()[-300:] if self.proc.stderr else ""
        raise TransportError(f"ssh -R to {self.alias} exited ({code}): {detail or 'the cabin refused the forward'}")

    def alive(self) -> bool:
        return self.proc is not None and self.proc.poll() is None

    def close(self) -> None:
        if self.proc is not None:
            stop_process(self.proc)


class Supervised:
    """Keeps one tunnel up. A thread restarts it when its process dies.

    ``gone()`` says whether the cabin is definitively gone (released or not found): the watcher then stops for good.
    After ``max_failures`` restarts in a row that all fail it also stops, logging one ERROR line (not a warning every
    interval). ``on_stop`` runs once when the supervisor ends for either reason, or on :meth:`close`.
    """

    def __init__(self, make: Callable[[], object], interval: float = 5.0, *, gone: Callable[[], bool] | None = None,
                 max_failures: int = 6, on_stop: Callable[[], None] | None = None, name: str = "tunnel"):
        self.make, self.interval, self.gone = make, interval, gone
        self.max_failures, self.on_stop, self.name = max_failures, on_stop, name
        self.current = None
        self.stop = threading.Event()
        self.lock = threading.Lock()
        self.failures = 0
        self._stopped = False

    def start(self) -> None:
        self.current = self.make()
        self.current.start()
        threading.Thread(target=self._watch, daemon=True).start()

    def ensure(self) -> None:
        with self.lock:
            if self.stop.is_set() or (self.current is not None and self.current.alive()):
                return
            if self.current is not None:
                self.current.close()
            fresh = self.make()
            try:
                fresh.start()
            except BaseException:
                fresh.close()
                raise
            if self.stop.is_set():  # closed while this one was starting
                fresh.close()
                return
            self.current = fresh
            self.failures = 0

    def _watch(self) -> None:
        while not self.stop.wait(self.interval):
            if self.alive():
                continue
            try:
                if self.gone is not None and self.gone():
                    log.info("%s: the cabin is gone; the tunnel supervisor stops", self.name)
                    self.close()
                    return
                self.ensure()
            except Exception as exc:  # the cabin may be starting or briefly unreachable
                self.failures += 1
                if self.failures >= self.max_failures:
                    log.error("%s: giving up after %d failed restarts (last: %s); the tunnel stays down", self.name,
                              self.failures, exc)
                    self.close()
                    return
                log.debug("%s: restart failed (%d/%d): %s", self.name, self.failures, self.max_failures, exc)

    def alive(self) -> bool:
        return self.current is not None and self.current.alive()

    def close(self) -> None:
        self.stop.set()
        with self.lock:
            if self.current is not None:
                self.current.close()
            first, self._stopped = not self._stopped, True
        if first and self.on_stop is not None:
            self.on_stop()


class TunnelRegistry:
    """Tunnels by sandbox id, for the whole process.

    Omnigent builds a fresh launcher for every launch, keep-alive and teardown, so a tunnel cannot live on a launcher
    instance: whichever instance runs ``terminate`` has to find and close the tunnel that another one opened.
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._tunnels: dict[str, Supervised] = {}
        self._locks: dict[str, threading.Lock] = {}

    def lock_for(self, sandbox_id: str) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(sandbox_id, threading.Lock())

    def get(self, sandbox_id: str) -> Supervised | None:
        with self._lock:
            return self._tunnels.get(sandbox_id)

    def put(self, sandbox_id: str, tunnel: Supervised) -> None:
        with self._lock:
            self._tunnels[sandbox_id] = tunnel

    def discard(self, sandbox_id: str, tunnel: Supervised | None = None) -> None:
        with self._lock:
            if tunnel is None or self._tunnels.get(sandbox_id) is tunnel:
                self._tunnels.pop(sandbox_id, None)

    def close(self, sandbox_id: str) -> bool:
        with self._lock:
            tunnel = self._tunnels.pop(sandbox_id, None)
        if tunnel is not None:
            tunnel.close()
        return tunnel is not None

    def close_all(self) -> None:
        with self._lock:
            tunnels, self._tunnels = list(self._tunnels.values()), {}
        for tunnel in tunnels:
            tunnel.close()

    def ids(self) -> list[str]:
        with self._lock:
            return list(self._tunnels)


REGISTRY = TunnelRegistry()
