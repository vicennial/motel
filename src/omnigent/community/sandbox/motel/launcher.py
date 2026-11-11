"""Run Omnigent hosts on motel cabins: ``omnigent sandbox create --provider motel`` and ``sandbox.provider: motel``.

A cabin is a disposable server on your own cloud account, with a TTL, a dead-man timer and money caps (see
docs/omnigent.md). This launcher is an exec-model provider: it checks a cabin in (``provision``), runs commands
over ``motel cabin ssh`` and checks the cabin out (``terminate``). motel's TTL and dead-man timer stay the
backstop for every path that does not reach ``terminate``.

Rules this module keeps:

* No secret in argv, on this machine or on the cabin's ssh command line. Scripts travel on a pipe's stdin.
* Credentials are named in config (``env``) and read from this process's environment when the host starts.
  They are never written to a file here.
* ``terminate`` raises, naming ``cleanup_pending``, unless the cabin is confirmed released.
"""

from __future__ import annotations

import importlib.metadata
import json
import logging
import os
import re
import secrets
import shlex
import shutil
import time
from collections.abc import Callable, Mapping
from pathlib import Path

import click
from omnigent.onboarding.sandboxes.base import (
    ExecModelHostLauncher,
    RemoteCommandResult,
    RemoteProcess,
    SandboxGoneError,
    foreground_kill_command,
    foreground_pidfile,
    foreground_record_prefix,
    supervise_host_command,
)
from omnigent.onboarding.sandboxes.types import SandboxCapabilities, SandboxError

from motel.sdk import BadReply, Motel, MotelError, NotFound, NotInstalled, NotSetUp, NoVacancy, Timeout

from . import transport as tp
from .config import ENV_NAME_RE, TASK_RE, MotelSandboxConfig, release_version, reserved_env_name

log = logging.getLogger(__name__)

ENV_NAMES_VAR = "OMNIGENT_MOTEL_SANDBOX_ENV"
"""Comma-separated variable NAMES to pass to the host when ``sandbox.motel.env`` is not configured."""

HOST_LOG = "/tmp/omnigent-host.log"
HOST_PID = "$HOME/.local/state/motel-omnigent/host.pid"
"""The host supervisor's pid, written by the shell that becomes the session leader. Its process group is the host.
Not under /tmp: tmpfiles aging (10 days on Ubuntu) would remove it while a cabin lives for up to 14."""
HOST_PATTERN = "^([^ ]*/python[0-9.]* )?([^ ]*/)?omnigent host( |$)"
"""The host's command line on a cabin: ``.../venv/bin/python3 .../omnigent host ...`` (a pip console script runs through
its shebang). Anchored to the start, so a shell or editor that merely mentions 'omnigent host' is not matched."""
HARNESS_WARN_EVERY_S = 600.0
_harness_seen: dict[str, float] = {}
_harness_warned: set[str] = set()


class MotelSandboxError(click.ClickException, SandboxError):
    """A cabin operation failed. The message says what to try."""


def secret_forms(value: str) -> set[str]:
    """Every way a value can appear in a shell command or its error output: raw, shell-quoted, JSON-escaped."""
    forms = {value, shlex.quote(value), "'" + value.replace("'", "'\"'\"'") + "'", "'" + value.replace("'", "'\\''") + "'"}
    forms.add(json.dumps(value)[1:-1])
    return {f for f in forms if f}


STDOUT_MIN_SECRET = 8


def redact(text: str, secrets_, *, min_len: int = 1) -> str:
    """Replace secret values (any spelling) with ``[redacted]``. Values of any length count by default: the caller
    knows they are secrets. ``min_len`` skips shorter ones, for output that something parses."""
    forms = set()
    for secret in secrets_:
        if secret and len(secret) >= min_len:
            forms |= secret_forms(secret)
    for form in sorted(forms, key=len, reverse=True):
        text = text.replace(form, "[redacted]")
    return text


def stop_host_script(pidfile: str = HOST_PID, pattern: str = HOST_PATTERN) -> str:
    """Stop the host this provider started: its recorded process group, nothing else. ``pidfile`` is a shell word
    (``$HOME`` is expanded). The record is trusted only if it is a number and that process still has the pidfile's name
    in its command line (so a reused pid is left alone). Without a record (a host started by an older version) fall back
    to ``HOST_PATTERN``. A TERM to the host ends the supervisor's loop too (the host exits 143)."""
    q = f'"{pidfile}"'
    marker = "/".join(pidfile.rsplit("/", 2)[-2:])
    return (f"pid=$(cat {q} 2>/dev/null)\n"
            f"case \"$pid\" in ''|*[!0-9]*) pid= ;; esac\n"
            f"if [ -n \"$pid\" ]; then\n"
            f"  if grep -aqF {shlex.quote(marker)} /proc/$pid/cmdline 2>/dev/null; then\n"
            f"    kill -TERM -$pid 2>/dev/null; sleep 1; kill -KILL -$pid 2>/dev/null\n"
            f"  fi\n"
            f"  rm -f {q}\n"
            f"else\n"
            f"  pkill -f {shlex.quote(pattern)} || true\n"
            f"fi\n"
            f"true\n")


def task_name(name: str, default: str) -> str:
    """A unique cabin task label (a-z, 0-9, '-', at most 32 chars) from an Omnigent sandbox name: a readable prefix and
    8 random hex digits, so a cabin can be found again by its label after a checkin that timed out."""
    label = re.sub(r"[^a-z0-9]+", "-", (name or "").lower()).strip("-")[:22].strip("-")
    if not TASK_RE.fullmatch(label or "x"):
        label = ""
    return f"{label or default[:22].strip('-') or 'omnigent'}-{secrets.token_hex(4)}"


def command_secrets(command: str) -> list[str]:
    """Values of ``SOMETHING_TOKEN=value`` assignments in a host command (the launch token), for redaction."""
    try:
        words = shlex.split(command)
    except ValueError:
        return []
    return [w.split("=", 1)[1] for w in words if re.match(r"^[A-Za-z_]*(TOKEN|SECRET|KEY)[A-Za-z_]*=", w)]


class _Stream(RemoteProcess):
    """A streaming remote process: combined output as lines."""

    def __init__(self, proc):
        self._proc = proc

    @property
    def lines(self):
        return iter(self._proc.stdout.readline, "")

    def wait(self) -> int:
        return self._proc.wait()

    def close(self) -> None:
        tp.stop_process(self._proc)


class MotelSandboxLauncher(ExecModelHostLauncher):
    """Omnigent sandbox provider ``motel``: a host on a disposable motel cabin.

    Omnigent builds a new launcher object for every launch, keep-alive and teardown, so nothing here may live only
    on the instance: tunnels are kept in ``transport.REGISTRY``, which is process wide, keyed by sandbox id.
    """

    provider = "motel"

    @property
    def capabilities(self) -> SandboxCapabilities:
        return SandboxCapabilities(
            cli_bootstrap=True,
            managed_launch=True,
            local_port_forward=False,
            # resume() re-opens the tunnel and restarts the host on the same cabin (the wake path: Omnigent calls it
            # when a session's host is offline, for instance after the server restarted).
            resume_stopped=True,
            programmatic_terminate=True,
            file_copy=True,
            streaming_exec=True,
            foreground_exec=True,
            git_clone_options=super().capabilities.git_clone_options,
        )

    def __init__(self, config: MotelSandboxConfig | None = None, *, motel: Motel | None = None,
                 transport: tp.CabinTransport | None = None, environ: Mapping[str, str] | None = None,
                 ssh_binary: str = tp.SSH, sleep: Callable[[float], None] = time.sleep,
                 tunnel_interval: float = 5.0, tunnel_failures: int = 6) -> None:
        self.config = config or MotelSandboxConfig()
        self._motel = motel or Motel(self.config.motel_binary, cabin_timeout=self.config.checkin_timeout_s)
        self._transport = transport or tp.CabinTransport(self.config.motel_binary)
        self._environ = os.environ if environ is None else environ
        self._ssh = ssh_binary
        self._sleep = sleep
        self._tunnel_interval, self._tunnel_failures = tunnel_interval, tunnel_failures
        self._ready: set[str] = set()

    # -- preflight and lifecycle ----------------------------------------------------------------------------
    def prepare(self) -> None:
        """Check the motel CLI works, and that every named credential variable is set, before any money is spent."""
        if shutil.which(self.config.motel_binary) is None and not os.path.isfile(self.config.motel_binary):
            raise MotelSandboxError(f"cannot find the motel executable {self.config.motel_binary!r}. "
                                    "Try: pipx install motel   (then: sudo motel install)")
        try:
            self._motel.cabins()
        except (NotInstalled, NotSetUp) as exc:
            raise MotelSandboxError(f"motel is not ready for cabins: {exc}") from exc
        except MotelError as exc:
            raise MotelSandboxError(f"motel could not list cabins: {exc}") from exc
        self._resolve_env()
        if self.config.bootstrap == "pip":
            self._omnigent_version()

    def provision(self, name: str) -> str:
        """Check a cabin in and wait until it accepts commands. The cabin id is the sandbox id."""
        self._resolve_env()  # fail before a cabin exists (and bills) if a named variable is unset
        task = task_name(name, self.config.task)
        click.echo(f"▸ Checking in a motel cabin for '{name}' (ttl {self.config.ttl})")
        try:
            cabin = self._motel.cabin_checkin(self.config.server_type, self.config.ttl, task, self.config.disk_gb)
        except (Timeout, BadReply) as exc:
            # The cabin may exist although the answer was lost. The task label is unique to this call.
            self._reclaim(task)
            raise MotelSandboxError(f"motel did not confirm the cabin check-in ({exc}); any cabin it created was "
                                    "looked up and checked out") from exc
        except MotelError as exc:
            raise MotelSandboxError(f"motel could not check in a cabin: {exc}") from exc
        price = f", EUR {cabin.hourly_eur}/h net" if cabin.hourly_eur is not None else ""
        click.echo(f"  → cabin {cabin.id} ({cabin.server_type or 'default type'}{price}), expires {cabin.expires}")
        try:
            self._wait_for_ssh(cabin.id)
        except BaseException:
            self._checkout_quietly(cabin.id)
            raise
        return cabin.id

    def attach(self, sandbox_id: str) -> None:
        try:
            cabin = self._motel.cabin_status(sandbox_id)
        except NotFound:
            raise MotelSandboxError(f"cabin '{sandbox_id}' does not exist (it may have been checked out). "
                                    "Try: motel cabin ls") from None
        except MotelError as exc:
            raise MotelSandboxError(f"cannot read cabin '{sandbox_id}': {exc}") from exc
        if cabin.state != "active":
            raise MotelSandboxError(f"cabin '{sandbox_id}' is {cabin.state}, not active. Try: motel cabin status {sandbox_id}")

    def keep_alive(self, sandbox_id: str) -> bool | None:
        """Push the checkout time to now + ``ttl`` (``motel cabin extend``) and make sure the tunnel is up.

        A live tunnel in the registry is left alone: no second relay is ever started for a cabin. Soft-fails: a refusal
        is logged and returns False, and the cabin then ends at its current checkout time.
        """
        extended = True
        try:
            self._motel.cabin_extend(sandbox_id, self.config.ttl)
        except NoVacancy as exc:
            # Extending right after a checkin or a keep-alive moves the time by under the 5-minute minimum:
            # the cabin already runs long enough, which is success.
            if "already runs until" not in exc.message:
                log.warning("motel cabin %s was not extended: %s", sandbox_id, exc)
                extended = False
        except MotelError as exc:
            log.warning("motel cabin %s was not extended: %s", sandbox_id, exc)
            extended = False
        tunnel = tp.REGISTRY.get(sandbox_id)
        if tunnel is not None:
            try:
                tunnel.ensure()
            except Exception as exc:
                log.warning("tunnel for cabin %s is down: %s", sandbox_id, exc)
        self._warn_if_harness_failed(sandbox_id, rate_limited=True)
        return extended

    def is_running(self, sandbox_id: str) -> bool | None:
        try:
            state = self._motel.cabin_status(sandbox_id).state
        except NotFound:
            return False
        except MotelError:
            return None
        if state == "active":
            return True
        if state in ("released", "cleanup_pending"):
            return False
        return None  # creating or bootstrapping: not an answer yet

    def resume(self, sandbox_id: str) -> None:
        """Bring a cabin's host back after the server lost it (a restart, for instance). Omnigent calls this from its
        wake path, then ``start_host``, which re-opens the tunnel and starts a new host.

        The cabin must still be active. The checkout time is pushed forward and a host left running from before is
        stopped (its launch token has been replaced).
        """
        try:
            state = self._motel.cabin_status(sandbox_id).state
        except NotFound:
            raise SandboxGoneError(f"cabin '{sandbox_id}' no longer exists") from None
        except MotelError as exc:
            raise MotelSandboxError(f"cannot read cabin '{sandbox_id}': {exc}") from exc
        if state in ("released", "cleanup_pending"):
            raise SandboxGoneError(f"cabin '{sandbox_id}' is {state}")
        if state != "active":
            raise MotelSandboxError(f"cabin '{sandbox_id}' is {state}, not active. Try: motel cabin status {sandbox_id}")
        self._warn_if_harness_failed(sandbox_id)  # before keep_alive, whose own check then stays quiet
        self.keep_alive(sandbox_id)
        retry = tp.harness_retry_script(self.config.harness_packages) if self._background_harnesses() else ""
        self._exec(sandbox_id, stop_host_script() + retry, 60, quiet=True)

    def terminate(self, sandbox_id: str) -> None:
        """Check the cabin out. Raises, naming ``cleanup_pending``, unless it is confirmed released.

        An absent cabin counts as success, so a retry after a crash is safe. The tunnel (opened by another launcher
        instance) is closed first.
        """
        tp.REGISTRY.close(sandbox_id)
        self._ready.discard(sandbox_id)
        try:
            cabin = self._motel.cabin_checkout(sandbox_id, strict=False)
        except NotFound:
            return
        except Timeout as exc:
            raise MotelSandboxError(f"cleanup_pending: checkout of cabin {sandbox_id} did not finish ({exc})") from exc
        except MotelError as exc:
            raise MotelSandboxError(f"cleanup_pending: cabin {sandbox_id} could not be checked out ({exc})") from exc
        if not cabin.released:
            raise MotelSandboxError(
                f"cleanup_pending: cabin {sandbox_id} is {cabin.state}, not released, so it may still bill. "
                f"The reaper retries; check it with: motel cabin status {sandbox_id}")

    # -- running commands -----------------------------------------------------------------------------------
    def run(self, sandbox_id: str, command: str, *, check: bool = True) -> RemoteCommandResult:
        secrets_ = self._secrets(command)
        result = self._exec(sandbox_id, f"{tp.PRELUDE}\n{command}\n")
        stderr = redact(result.stderr, secrets_)
        if check and result.returncode != 0:  # never the command's text: it can carry a token
            raise MotelSandboxError(f"command failed on cabin {sandbox_id} (exit {result.returncode})"
                                    + (f": {stderr.strip()[-600:]}" if stderr.strip() else "")
                                    + self._harness_hint(sandbox_id))
        # stdout is parsed by Omnigent (the $HOME probe, paths), so a short value such as REGION=a must not rewrite it.
        # Errors and stderr keep full redaction. A real key or token is far longer than this.
        return RemoteCommandResult(result.returncode, redact(result.stdout, secrets_, min_len=STDOUT_MIN_SECRET), stderr)

    def run_background(self, sandbox_id: str, command: str, *, log_path: str = HOST_LOG) -> RemoteCommandResult:
        """Start the supervised host detached. The command (it carries the launch token) goes on stdin only."""
        env = self._prepare_host(sandbox_id, command)
        # The shell records its own pid first. setsid makes it the session and group leader, so that pid names the
        # whole group (the supervisor loop and the host) for ``resume``.
        script = f'echo $$ > "{HOST_PID}"; {supervise_host_command(command)}'
        wrapped = (f'mkdir -p "$(dirname "{HOST_PID}")"\n'
                   f"setsid nohup sh -c {shlex.quote(script)} "
                   f">> {shlex.quote(log_path)} 2>&1 < /dev/null & echo launched")
        secrets_ = self._secrets(command)
        result = self._exec(sandbox_id, f"{tp.PRELUDE}\n{tp.export_lines(env)}{wrapped}\n")
        if result.returncode != 0:
            raise MotelSandboxError(f"could not start the host on cabin {sandbox_id} (exit {result.returncode}): "
                                    f"{redact(result.stderr.strip()[-600:], secrets_)}")
        return RemoteCommandResult(0, result.stdout, redact(result.stderr, secrets_))

    def put(self, sandbox_id: str, local_path: Path, remote_path: str) -> None:
        try:
            self._transport.put(sandbox_id, Path(local_path), remote_path, self.config.command_timeout_s)
        except tp.TransportError as exc:
            raise MotelSandboxError(str(exc)) from exc

    def stream_exec(self, sandbox_id: str, command: str, *, pty: bool = False) -> RemoteProcess:
        body = f"script -qefc {shlex.quote(command)} /dev/null" if pty else command
        try:
            proc = self._transport.popen_script(sandbox_id, f"{tp.PRELUDE}\n{body}\n", text=True, new_session=True)
        except tp.TransportError as exc:
            raise MotelSandboxError(str(exc)) from exc
        return _Stream(proc)

    def exec_foreground(self, sandbox_id: str, command: str) -> int:
        """Run ``command`` here in the foreground (stdout and stderr inherited) until it ends or Ctrl-C."""
        env = self._prepare_host(sandbox_id, command)
        run_dir, pidfile = foreground_pidfile()
        script = (f"{tp.PRELUDE}\n{tp.export_lines(env)}{foreground_record_prefix(pidfile)}"
                  f"exec sh -c {shlex.quote(command)} < /dev/null\n")
        proc = self._transport.popen_script(sandbox_id, script, stdout=None, stderr=None)
        try:
            return proc.wait()
        except KeyboardInterrupt:
            self._exec(sandbox_id, foreground_kill_command(pidfile), 60, quiet=True)
            proc.terminate()
            return 130
        finally:
            tp.REGISTRY.close(sandbox_id)

    def wheel_install_command(self, remote_tgz_path: str) -> str:
        return tp.wheel_install_script(remote_tgz_path, self.config.harness_packages, self.config.wait_for_harnesses)

    # -- helpers --------------------------------------------------------------------------------------------
    def _background_harnesses(self) -> bool:
        return self.config.bootstrap == "pip" and bool(self.config.harness_packages) and not self.config.wait_for_harnesses

    def _harness_failed(self, sandbox_id: str) -> bool:
        """True when the background harness install left its failure marker. One call to the cabin."""
        if not self._background_harnesses():
            return False
        probe = self._exec(sandbox_id, f"[ -e {shlex.quote(tp.HARNESS_FAILED)} ] && echo failed\ntrue\n", 30, quiet=True)
        return probe.returncode == 0 and probe.stdout.strip() == "failed"

    def _harness_hint(self, sandbox_id: str) -> str:
        """When a command fails and the background harness install failed too, say so: the likely cause is a missing
        harness CLI. One extra call, made only on a failure."""
        if self._harness_failed(sandbox_id):
            return f". The harness CLI install failed, see {tp.HARNESS_LOG} on the cabin"
        return ""

    def _warn_if_harness_failed(self, sandbox_id: str, *, rate_limited: bool = False) -> None:
        """Log one WARNING that harness CLIs are missing. From keep_alive it asks the cabin at most every ten minutes and
        warns once per cabin (the server's keep-alive loop is where an operator would see it)."""
        if rate_limited:
            now = time.monotonic()
            if sandbox_id in _harness_warned or now - _harness_seen.get(sandbox_id, -1e9) < HARNESS_WARN_EVERY_S:
                return
            _harness_seen[sandbox_id] = now
        if self._harness_failed(sandbox_id):
            _harness_warned.add(sandbox_id)
            log.warning("motel cabin %s: the harness CLI install failed, so coding harnesses may be missing; see %s on the "
                        "cabin. It is retried on the next launch or resume.", sandbox_id, tp.HARNESS_LOG)

    def _exec(self, sandbox_id: str, script: str, timeout: float | None = None, quiet: bool = False) -> tp.Result:
        try:
            return self._transport.run_script(sandbox_id, script, timeout or self.config.command_timeout_s)
        except tp.TransportError as exc:
            if quiet:
                return tp.Result(255, "", str(exc))
            raise MotelSandboxError(str(exc)) from exc

    def _wait_for_ssh(self, cabin: str, attempts: int = 24, delay: float = 5.0) -> None:
        last = ""
        for _ in range(attempts):
            result = self._exec(cabin, "true\n", 60, quiet=True)
            if result.returncode == 0:
                return
            last = result.stderr.strip()[-200:]
            self._sleep(delay)
        raise MotelSandboxError(f"cabin {cabin} did not accept commands: {last or 'no answer'}")

    def _checkout_quietly(self, cabin: str) -> None:
        try:
            self.terminate(cabin)
        except Exception as exc:
            click.echo(f"  ! cabin {cabin} could not be checked out: {exc}", err=True)

    def _reclaim(self, task: str) -> None:
        """After a check-in whose answer was lost: find the cabin by its unique task label and check it out."""
        try:
            found = [c for c in self._motel.cabins() if c.task == task and not c.released]
        except MotelError as exc:
            click.echo(f"  ! could not look for a cabin labelled '{task}': {exc}. Try: motel cabin ls", err=True)
            return
        for cabin in found:
            click.echo(f"  → checking out cabin {cabin.id}, which was created for this launch")
            self._checkout_quietly(cabin.id)

    def _resolve_env(self) -> dict[str, str]:
        names = self.config.env
        if names is None:
            names = [n.strip() for n in self._environ.get(ENV_NAMES_VAR, "").split(",") if n.strip()]
        values: dict[str, str] = {}
        for name in names:
            if not ENV_NAME_RE.fullmatch(name) or reserved_env_name(name):
                raise MotelSandboxError(f"sandbox env passthrough: {name!r} is not an allowed variable name")
            value = self._environ.get(name)
            if value is None:
                raise MotelSandboxError(
                    f"sandbox env passthrough names '{name}' but it is not set in this process's environment. "
                    f"Set it, or remove it from sandbox.motel.env / {ENV_NAMES_VAR}.")
            values[name] = value
        return values

    def _secrets(self, command: str) -> list[str]:
        """Values to keep out of output: the named credentials (any length) and the launch token."""
        try:
            named = list(self._resolve_env().values())
        except MotelSandboxError:
            named = []
        return [*named, *command_secrets(command)]

    def _omnigent_version(self) -> str:
        if self.config.omnigent_version != "auto":
            return self.config.omnigent_version
        try:
            version = importlib.metadata.version("omnigent")
        except importlib.metadata.PackageNotFoundError:
            raise MotelSandboxError("cannot tell which omnigent version to install on the cabin. "
                                    "Set sandbox.motel.omnigent_version.") from None
        if not release_version(version):
            raise MotelSandboxError(f"this omnigent ({version}) is not a release version; set "
                                    "sandbox.motel.omnigent_version to the version to install on the cabin.")
        return version

    def _prepare_host(self, sandbox_id: str, command: str) -> dict[str, str]:
        """Before a host starts: omnigent on the cabin, the tunnel if the server is on loopback, the credentials."""
        env = self._resolve_env()
        if self.config.bootstrap == "pip" and sandbox_id not in self._ready:
            script = tp.ensure_omnigent_script(self._omnigent_version(), self.config.harness_packages,
                                               self.config.wait_for_harnesses)
            click.echo("  → making sure omnigent is installed on the cabin")
            result = self._exec(sandbox_id, script, self.config.command_timeout_s)
            if result.returncode != 0:
                raise MotelSandboxError(f"installing omnigent on cabin {sandbox_id} failed (exit {result.returncode}): "
                                        f"{redact(result.stderr.strip()[-600:], self._secrets(command))}")
            self._ready.add(sandbox_id)
        target = tp.parse_server_target(command)
        if target is not None:
            self._open_tunnel(sandbox_id, target)
        return env

    # -- tunnels ----------------------------------------------------------------------------------------------
    def _open_tunnel(self, sandbox_id: str, target: tp.ServerTarget) -> None:
        mode = self.config.tunnel
        if mode == "none":
            return
        if not target.loopback:
            if mode == "auto":
                return  # a public URL: the cabin dials it directly
            raise MotelSandboxError(f"tunnel '{mode}' carries a server on this machine's loopback; the server URL is "
                                    f"{target.url}. Use tunnel: auto, or a loopback URL such as http://127.0.0.1:6767.")
        if target.host not in ("127.0.0.1", "localhost"):
            raise MotelSandboxError(f"use http://127.0.0.1:{target.port} as the server URL: the cabin has no IPv6 loopback.")
        if target.port < 1024:
            raise MotelSandboxError(f"the cabin user cannot listen on port {target.port}; run the server on a port above 1023.")
        with tp.REGISTRY.lock_for(sandbox_id):
            existing = tp.REGISTRY.get(sandbox_id)
            if existing is not None and not existing.stop.is_set():
                existing.ensure()  # restarts a dead one; never starts a second beside a live one
                return
            click.echo(f"  → tunnel: cabin 127.0.0.1:{target.port} → this machine's {target.host}:{target.port}")
            tunnel = tp.Supervised(self._tunnel_factory(sandbox_id, target.host, target.port, mode), self._tunnel_interval,
                                   gone=lambda: self._cabin_gone(sandbox_id), max_failures=self._tunnel_failures,
                                   name=f"motel cabin {sandbox_id}")
            tunnel.on_stop = lambda: tp.REGISTRY.discard(sandbox_id, tunnel)
            try:
                tunnel.start()
            except tp.TransportError as exc:
                tunnel.stop.set()
                raise MotelSandboxError(str(exc)) from exc
            tp.REGISTRY.put(sandbox_id, tunnel)

    def _cabin_gone(self, sandbox_id: str) -> bool:
        """True when the cabin is definitively gone (released, pending cleanup or not found)."""
        try:
            return self._motel.cabin_status(sandbox_id).state in ("released", "cleanup_pending")
        except NotFound:
            return True
        except MotelError:
            return False

    def _tunnel_factory(self, sandbox_id: str, host: str, port: int, mode: str):
        if mode == "reverse-forward":
            def forward():
                from motel.cabins.cli import state_root
                directory = Path(state_root()) / sandbox_id
                alias = json.loads((directory / "receipt.json").read_text(encoding="utf-8"))["name"]
                return tp.ForwardTunnel(str(directory / "ssh_config"), alias, port, host, ssh=self._ssh)
            return forward
        return lambda: tp.RelayTunnel(self._transport, sandbox_id, port, host, max_streams=self.config.max_streams)
