#!/usr/bin/python3
"""motel tour guest runner: one bounded, non-interactive job on a fresh cabin.

Standard library only; written for Python 3.10+ because it runs on the guest, not on the controller. It is one
file plus ``adapters.py`` next to it (both are package data of ``motel.tours.guest``; copy them together).

The controller runs it as the unprivileged user ``guest``::

    python3 runner.py [--in DIR] [--home DIR] [--work DIR] [--out DIR]

Inputs (``--in``, default ``/var/lib/motel-guest/in``, root-owned and readable by ``guest``):
``card.json`` (schema ``motel/guest-card/v1``), ``input.bundle`` (git bundle holding the base commit) and an
optional ``overlay.patch``.

What it does, in order: validate the card; clone the bundle to ``<home>/work``, check out ``base`` on ``branch``,
apply the overlay as its own commit; write the agent's config files into the fixed HOME; run the adapter in its own
session and process group under a wall-clock limit (TERM, then KILL after a grace period) with stdout and stderr
streamed to capped files; commit everything the agent left behind as a fixed bot identity; write
``out/result.bundle`` (``base..HEAD``), copy declared artifacts and the agent's ``RESULT.json``; write
``out/run.json`` last and atomically.

Outputs (``--out``, default ``<home>/out``)::

    run.json          schema motel/guest-run/v1 (see below); present exactly when the runner finished
    result.bundle     git bundle of base..HEAD (absent when the agent changed nothing, or it was rejected)
    artifacts/        copies of the declared artifacts: regular files only, relative paths, size capped
    RESULT.json       the agent's own result file, if it left one (64 KB cap, must be a JSON object)
    stdout.log        the adapter's stdout, capped (head and tail kept), placeholder redacted
    stderr.log        the same for stderr

The runner's exit status is 0 whenever ``run.json`` was written, whatever happened to the agent: read ``run.json``.
It is 70 only when ``run.json`` itself could not be written.

``run.json`` always has: schema, attempt, exit_code, termination, started, finished, head, changed_files,
bundle_sha256, artifacts, stdout_bytes, stderr_bytes. ``termination`` is ``exited``, ``timeout``, ``killed`` or
``failed``. ``failed`` carries a ``reason`` (``adapter_missing``, ``invalid_card``, ``input_missing``,
``base_missing``, ``overlay_failed``, ``config_failed``, ``git_missing``, ``runner_error``, ...) and
``command_started: false`` when the agent command never ran (a controller may retry only those, never a started
attempt). Extra, additive fields: tour, guest, adapter, base, branch, overlay_commit, command_started, signal,
duration_s, bundle_bytes, changed_files_truncated, stdout_total_bytes, stderr_total_bytes, stdout_truncated,
stderr_truncated, result_json, rejected (items the runner refused to hand over, with a reason), redactions and
``concierge_refusals``: a list of ``{"type": "motel_<reason>", "status": 401|403|429|502|null, "count": n}``, one
entry per refusal type and status the agent printed (the concierge answers over budget with 403 and over concurrency
with 429; its Retry-After header is not visible to the runner). Present, possibly empty, whenever the command ran.

Safety properties, each covered by tests:

* the adapter gets only the card's ``env``, the binding variables and a small fixed base (clean PATH, fixed HOME);
  the runner's own environment is never passed on;
* output is untrusted: only regular files, no symlinks, no ``..``, no absolute paths, no ``.git``, size capped;
* the placeholder token is redacted from logs and from working-tree files; if it is found in a commit the agent
  made, the bundle is withheld; an artifact or RESULT.json containing it is withheld and the attempt fails with
  reason ``secret_leak``. It is never in run.json;
* own-login mode (``"auth": "own-login"``, claude adapter only): one line of at most 4 KB is read from stdin and
  passed to the adapter process only, as ``CLAUDE_CODE_OAUTH_TOKEN``. No ``ANTHROPIC_BASE_URL`` or
  ``ANTHROPIC_API_KEY`` is set. The token is held in memory, redacted from the captured output, and a copy in the
  bundle (working tree or history), an artifact or RESULT.json withholds that item and fails the attempt with
  ``secret_leak``. An empty stdin fails with ``own_login_missing_token``.
"""

from __future__ import annotations

import argparse
import glob
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import stat
import subprocess
import sys
import tempfile
import threading
import time

SCHEMA_CARD = "motel/guest-card/v1"
SCHEMA_RUN = "motel/guest-run/v1"
MIB = 1 << 20
DEFAULT_IN = "/var/lib/motel-guest/in"
KILL_GRACE_S = 10.0
RESULT_JSON_CAP = 64 * 1024
DEFAULT_RESULT_MB = 64
MAX_RESULT_MB = 1024
MAX_PROMPT = 100_000
MAX_ENV_ITEMS, MAX_ENV_VALUE, MAX_ENV_TOTAL = 256, 32 * 1024, 256 * 1024
MAX_ARTIFACT_PATTERNS, MAX_ARTIFACT_FILES = 100, 500
MAX_CHANGED_FILES = 10_000
MAX_REJECTED = 100
GIT_TIMEOUT_S = 900
BOT_NAME = "motel guest"
BOT_EMAIL = "guest@motel.invalid"
REDACTED = b"[motel:redacted]"
PATH_DEFAULT = "/usr/local/bin:/usr/bin:/bin"
ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")
SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
BRANCH_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]{0,200}$")
ENV_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]{0,63}$")
PLACEHOLDER_RE = re.compile(r"^[A-Za-z0-9._~+/=:-]{16,512}$")
HERE = os.path.dirname(os.path.abspath(__file__))


class Stop(Exception):
    """A step failed in a way that has a stable reason code. ``started`` says whether the agent command ran."""

    def __init__(self, reason, detail="", started=False):
        super().__init__(reason)
        self.reason, self.detail, self.started = reason, detail, started


# --------------------------------------------------------------------------------------------- helpers

def utc_now():
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def load_adapters():
    spec = importlib.util.spec_from_file_location("motel_tour_adapters", os.path.join(HERE, "adapters.py"))
    if spec is None or spec.loader is None:
        raise Stop("runner_error", "adapters.py is missing next to runner.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["motel_tour_adapters"] = module     # dataclasses look the module up by name
    spec.loader.exec_module(module)
    return module


def sha256_file(path):
    digest = hashlib.sha256()
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def read_regular(path, cap):
    """Bytes of a regular file, never following a final symlink; None when it is absent. Raises Stop on junk."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
    except FileNotFoundError:
        return None
    except OSError:
        raise Stop("input_unreadable", os.path.basename(path)) from None
    with os.fdopen(fd, "rb") as handle:
        info = os.fstat(handle.fileno())
        if not stat.S_ISREG(info.st_mode):
            raise Stop("input_unreadable", os.path.basename(path) + " is not a regular file")
        if info.st_size > cap:
            raise Stop("input_too_large", os.path.basename(path))
        return handle.read(cap + 1)


class Secrets:
    """Byte strings that must never reach an output.

    ``scrub`` tokens (the concierge placeholder) are redacted in logs and in working-tree files. ``strict`` tokens (a
    real login token) are never redacted in repository content: finding one there withholds the output instead.
    Both are redacted in logs and removed from run.json, and both withhold an artifact or RESULT.json.
    """

    def __init__(self, scrub=(), strict=()):
        self.scrub = [t for t in scrub if t]
        self.strict = [t for t in strict if t]

    @property
    def tokens(self):
        return self.scrub + self.strict

    def __bool__(self):
        return bool(self.tokens)

    def keep(self):
        return max((len(t) for t in self.tokens), default=1) - 1

    def found(self, data):
        return any(t in data for t in self.tokens)

    def redact(self, data):
        count = 0
        for token in self.tokens:
            count += data.count(token)
            if token in data:
                data = data.replace(token, REDACTED)
        return data, count


def scan_fd(fd, secrets, chunk=1 << 20):
    """(has_scrub_token, has_strict_token) for a readable stream; chunks overlap so a split token is found."""
    keep, tail = secrets.keep(), b""
    scrub = strict = False
    while True:
        block = os.read(fd, chunk)
        if not block:
            return scrub, strict
        data = tail + block
        scrub = scrub or any(t in data for t in secrets.scrub)
        strict = strict or any(t in data for t in secrets.strict)
        if strict:
            return scrub, strict
        tail = data[-keep:] if keep else b""


def safe_atomic_write(path, data, mode=0o644):
    tmp = f"{path}.{os.getpid()}.tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, mode)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def write_log(out, name, data):
    safe_atomic_write(os.path.join(out, name), data)


# --------------------------------------------------------------------------------------------- the card

def validate_card(raw, adapters):
    """Strictly checked copy of the card. Raises Stop('invalid_card')."""
    def bad(why):
        raise Stop("invalid_card", why)
    try:
        card = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError, RecursionError):
        bad("card.json is not valid JSON")
    if not isinstance(card, dict) or card.get("schema") != SCHEMA_CARD:
        bad("unknown card schema")
    for key in ("tour", "guest", "attempt"):
        if not isinstance(card.get(key), str) or not ID_RE.fullmatch(card[key]):
            bad(f"invalid {key}")
    if not isinstance(card.get("base"), str) or not SHA_RE.fullmatch(card["base"]):
        bad("base must be a full lowercase commit id")
    branch = card.get("branch")
    if (not isinstance(branch, str) or not BRANCH_RE.fullmatch(branch) or ".." in branch or "//" in branch
            or branch.endswith(("/", ".", ".lock")) or "/." in branch or "@{" in branch):
        bad("invalid branch")
    adapter = card.get("adapter")
    if adapter not in adapters.ADAPTERS:
        bad("unknown adapter")
    spec = adapters.ADAPTERS[adapter]
    prompt = card.get("prompt")
    if spec.needs_prompt:
        if not isinstance(prompt, str) or not prompt.strip() or "\x00" in prompt \
                or len(prompt.encode("utf-8")) > adapters.MAX_PROMPT_BYTES:
            bad(f"prompt must be a non-empty string under {adapters.MAX_PROMPT_BYTES} bytes")
    auth = card.get("auth", "api-key")
    if auth not in ("api-key", "own-login"):
        bad("unknown auth mode")
    if auth == "own-login" and adapter != "claude":
        bad("own-login is only available for the claude adapter")
    port = card.get("concierge_port")
    if auth == "own-login" and port is None:
        port = 1                        # own-login guests do not use the concierge; the field is not needed
        card["concierge_port"] = port
    if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
        bad("invalid concierge_port")
    bindings = card.get("bindings", [])
    if (not isinstance(bindings, list) or len(bindings) > 16 or len(set(bindings)) != len(bindings)
            or not all(isinstance(b, str) and adapters.BINDING_NAME_RE.fullmatch(b) for b in bindings)):
        bad("invalid bindings")
    if auth == "own-login":
        if "anthropic" in bindings:
            bad("own-login cannot be combined with the anthropic binding")
    else:
        missing = [b for b in spec.requires if b not in bindings]
        if missing:
            bad("adapter {} needs the {} binding".format(adapter, ", ".join(missing)))
    placeholder = card.get("placeholder") or ""
    if not isinstance(placeholder, str) or (placeholder and not PLACEHOLDER_RE.fullmatch(placeholder)) \
            or (bindings and not placeholder):
        bad("invalid placeholder token")
    ttl = card.get("ttl_s")
    if isinstance(ttl, bool) or not isinstance(ttl, int) or not 1 <= ttl <= 86400:
        bad("ttl_s must be an integer from 1 to 86400")
    tokens = card.get("max_output_tokens", adapters.DEFAULT_MAX_OUTPUT_TOKENS)
    if (isinstance(tokens, bool) or not isinstance(tokens, int)
            or not adapters.MIN_OUTPUT_TOKENS <= tokens <= adapters.MAX_OUTPUT_TOKENS_LIMIT):
        bad(f"max_output_tokens must be an integer from {adapters.MIN_OUTPUT_TOKENS} to {adapters.MAX_OUTPUT_TOKENS_LIMIT}")
    if "model" in card:
        if adapter == "command":
            bad("the command adapter takes no model")
        if not adapters.card_model_ok(card["model"]):
            bad(f"model must be a string of 1 to {adapters.MODEL_MAX} characters, as in claude-sonnet-5-5")
    max_mb = card.get("max_result_mb", DEFAULT_RESULT_MB)
    if isinstance(max_mb, bool) or not isinstance(max_mb, int) or not 1 <= max_mb <= MAX_RESULT_MB:
        bad("invalid max_result_mb")
    env = card.get("env", {})
    if not isinstance(env, dict) or len(env) > MAX_ENV_ITEMS:
        bad("invalid env")
    total = 0
    for name, value in env.items():
        if not isinstance(name, str) or not ENV_NAME_RE.fullmatch(name):
            bad("invalid env name")
        if adapters.env_name_reserved(name):
            bad(f"env name {name} is reserved")
        if not isinstance(value, str) or "\x00" in value or len(value) > MAX_ENV_VALUE:
            bad("invalid env value")
        total += len(name) + len(value)
    if total > MAX_ENV_TOTAL:
        bad("env too large")
    patterns = card.get("artifacts", [])
    if not isinstance(patterns, list) or len(patterns) > MAX_ARTIFACT_PATTERNS:
        bad("invalid artifacts")
    for pattern in patterns:
        if (not isinstance(pattern, str) or not pattern or len(pattern) > 256 or pattern.startswith("/")
                or "\x00" in pattern or any(ord(c) < 32 for c in pattern)
                or any(part in ("..", ".git") for part in pattern.split("/"))):
            bad("invalid artifact pattern")
    if adapter == "command":
        argv = card.get("argv")
        if (not isinstance(argv, list) or not argv or len(argv) > 512 or not all(isinstance(a, str) and "\x00" not in a
                                                                                  for a in argv)
                or sum(len(a) for a in argv) > 256 * 1024):
            bad("the command adapter needs a non-empty argv list of strings")
    return card


# --------------------------------------------------------------------------------------------- git

class Git:
    def __init__(self, work, path, home):
        self.work = work
        self.env = {"PATH": path, "HOME": home, "LANG": "C.UTF-8", "LC_ALL": "C.UTF-8",
                    "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_CONFIG_SYSTEM": "/dev/null", "GIT_CONFIG_NOSYSTEM": "1",
                    "GIT_TERMINAL_PROMPT": "0", "GIT_ASKPASS": "/bin/false", "GIT_OPTIONAL_LOCKS": "0",
                    "GIT_AUTHOR_NAME": BOT_NAME, "GIT_AUTHOR_EMAIL": BOT_EMAIL,
                    "GIT_COMMITTER_NAME": BOT_NAME, "GIT_COMMITTER_EMAIL": BOT_EMAIL}
        self.base_args = ["git", "-c", "core.hooksPath=/dev/null", "-c", "commit.gpgsign=false",
                          "-c", "core.fsmonitor=false", "-c", "gc.auto=0", "-c", "core.quotepath=false",
                          "-c", "user.name=" + BOT_NAME, "-c", "user.email=" + BOT_EMAIL]

    def run(self, *args, check=True, stdin=None, binary=False, timeout=GIT_TIMEOUT_S, cwd=None):
        proc = subprocess.run(self.base_args + list(args), cwd=cwd or self.work, env=self.env, input=stdin,
                              capture_output=True, timeout=timeout, check=False)
        if check and proc.returncode != 0:
            raise Stop("git_failed", f"git {args[0]} exited {proc.returncode}")
        return proc if binary else (proc.returncode, proc.stdout.decode("utf-8", "backslashreplace").strip())

    def bound_to(self, git_dir, work_tree):
        """A Git on another repository that uses ``work_tree`` as its working directory, with the settings that make a
        repository the agent never touched trustworthy: no replace refs, no lazy fetching, no attribute files."""
        other = Git(work_tree, self.env["PATH"], self.env["HOME"])
        other.env.update({"GIT_DIR": git_dir, "GIT_WORK_TREE": work_tree, "GIT_NO_REPLACE_OBJECTS": "1",
                          "GIT_NO_LAZY_FETCH": "1", "GIT_ATTR_NOSYSTEM": "1"})
        other.base_args += ["-c", "core.attributesFile=/dev/null", "-c", "core.excludesFile=/dev/null",
                            "-c", "diff.external=", "-c", "core.autocrlf=false"]
        return other


# --------------------------------------------------------------------------------------------- the process

MIN_PARTIAL = 6                     # a shorter start of a token at the very end of a stream is not worth redacting


class StreamRedactor:
    """Replaces secret tokens in a byte stream as it goes by, whatever the read sizes are.

    The last ``longest token - 1`` bytes are held back until more data (or the end of the stream) shows whether a
    token continues there, so a token split across two reads is still found. At the end, a partial token (the start
    of one, at least ``MIN_PARTIAL`` bytes) is redacted as well: that is what a cut stream or a killed agent leaves."""

    def __init__(self, tokens=()):
        self.tokens = [t for t in tokens if t]
        self.keep = max((len(t) for t in self.tokens), default=1) - 1
        self.carry = b""
        self.count = 0

    def push(self, data):
        if not self.tokens:
            return data
        buf = self.carry + data
        for token in self.tokens:
            found = buf.count(token)
            if found:
                self.count += found
                buf = buf.replace(token, REDACTED)
        if len(buf) > self.keep:
            self.carry = buf[len(buf) - self.keep:] if self.keep else b""
            return buf[:len(buf) - self.keep]
        self.carry = buf
        return b""

    def flush(self):
        buf, self.carry = self.carry, b""
        for token in self.tokens:
            for length in range(min(len(token) - 1, len(buf)), MIN_PARTIAL - 1, -1):
                if buf.endswith(token[:length]):
                    buf = buf[:len(buf) - length] + REDACTED
                    self.count += 1
                    break
        return buf


# The "omitted" line is part of the log, so it is paid for out of the cap: the longest it can be (a 20-digit count).
MARKER_RESERVE = len(b"\n[motel:  bytes of output omitted]\n") + 20


def omitted_marker(skipped):
    return f"\n[motel: {skipped} bytes of output omitted]\n".encode() if skipped > 0 else b""


class CappedSink:
    """Collects one output stream with a hard byte cap: first half kept, last half ring-buffered, middle dropped.

    Everything is held in memory until :meth:`finish`: the log file is written by the runner after the agent and all
    its stragglers are gone, so nothing the agent can reach is ever the file that is handed over."""

    def __init__(self, cap, scanner=None, tokens=()):
        self.scanner = scanner
        self.redactor = StreamRedactor(tokens)      # redaction happens BEFORE the cap, so a cut cannot split a token
        self.cap = cap
        room = cap - MARKER_RESERVE if cap > 4 * MARKER_RESERVE else cap       # a tiny test cap keeps its halves
        self.head_cap, self.tail_cap = room // 2, room - room // 2
        self.total = 0                  # raw bytes the agent wrote
        self.head, self.tail = bytearray(), bytearray()
        self.kept = 0                   # bytes that went through the head/tail logic

    def feed(self, data):
        self.total += len(data)
        if self.scanner is not None:
            self.scanner.feed(data)
        self.store(self.redactor.push(data))

    def store(self, data):
        self.kept += len(data)
        if len(self.head) < self.head_cap:
            room = self.head_cap - len(self.head)
            self.head += data[:room]
            data = data[room:]
        if data:
            self.tail += data
            if len(self.tail) > self.tail_cap:
                del self.tail[:len(self.tail) - self.tail_cap]

    def finish(self):
        """(bytes to store, whether output was omitted)."""
        self.store(self.redactor.flush())
        skipped = self.kept - len(self.head) - len(self.tail)
        head, tail = bytes(self.head), bytes(self.tail)
        marker = omitted_marker(skipped)
        while skipped > 0 and len(head) + len(marker) + len(tail) > self.cap and (head or tail):
            if len(tail) >= len(head):          # only a cap too small for its own marker gets here
                tail, skipped = tail[1:], skipped + 1
            else:
                head, skipped = head[:-1], skipped + 1
            marker = omitted_marker(skipped)
        if len(head) + len(marker) + len(tail) > self.cap:
            marker = b""
        return head + marker + tail, self.kept > len(head) + len(tail)


def pump(fd, sink):
    """Drain one pipe into a capped sink until the other end closes (the pipe object stays owned by Popen)."""
    try:
        while True:
            data = os.read(fd, 65536)
            if not data:
                return
            sink.feed(data)
    except OSError:
        return


class Interrupt:
    """A TERM, INT or HUP aimed at the runner (for example the controller's ssh dropped) ends the agent too."""

    def __init__(self):
        self.signum = None
        for name in ("SIGTERM", "SIGINT", "SIGHUP"):
            try:
                signal.signal(getattr(signal, name), self.handle)
            except (ValueError, OSError):
                pass

    def handle(self, signum, _frame):
        self.signum = signum


def kill_group(pgid, sig):
    try:
        os.killpg(pgid, sig)
    except (ProcessLookupError, PermissionError):
        pass


# --- stragglers -----------------------------------------------------------------------------------------------------
# An agent can start a process that leaves the agent's process group (setsid) and outlives it. After the agent ends,
# nothing it started may stay alive: such a process could rewrite out/ while the controller collects it.

PR_SET_CHILD_SUBREAPER = 36
SWEEP_BOUND_S = 10.0


def become_subreaper():
    """Orphaned descendants of this process are re-parented to it (not to init), so they can still be found."""
    try:
        import ctypes
        libc = ctypes.CDLL(None, use_errno=True)
        return libc.prctl(PR_SET_CHILD_SUBREAPER, 1, 0, 0, 0) == 0
    except (ImportError, OSError, AttributeError):
        return False


def proc_table():
    """{pid: (ppid, state, uid)} from /proc; a process that vanishes while being read is skipped."""
    table = {}
    try:
        names = os.listdir("/proc")
    except OSError:
        return table
    for name in names:
        if not name.isdigit():
            continue
        try:
            with open(f"/proc/{name}/stat", "rb") as handle:
                raw = handle.read()
            rest = raw[raw.rindex(b")") + 2:].split()
            table[int(name)] = (int(rest[1]), rest[0].decode("ascii", "replace"), os.stat(f"/proc/{name}").st_uid)
        except (OSError, ValueError, IndexError):
            continue
    return table


def descendants(table, root):
    children = {}
    for pid, (ppid, _state, _uid) in table.items():
        children.setdefault(ppid, []).append(pid)
    found, stack = set(), [root]
    while stack:
        for pid in children.get(stack.pop(), ()):
            if pid not in found:
                found.add(pid)
                stack.append(pid)
    return found


def reap_children():
    """Collect zombies adopted as a subreaper. Only called when no child of ours is still being waited for."""
    while True:
        try:
            pid, _status = os.waitpid(-1, os.WNOHANG)
        except ChildProcessError:
            return
        if pid == 0:
            return


def sweep_uid():
    """The uid whose processes may all be killed: the dedicated ``guest`` account, when we run as it. Otherwise None."""
    try:
        import pwd
        uid = pwd.getpwnam("guest").pw_uid
    except (ImportError, KeyError):
        return None
    return uid if uid != 0 and os.geteuid() == uid else None


def sweep_stragglers(own_pid, uid=None, bound_s=SWEEP_BOUND_S, table=proc_table, kill=os.kill, reap=reap_children):
    """SIGKILL every process the agent left behind, repeating until none is left or ``bound_s`` ran out.

    Always: every descendant of ``own_pid`` (orphans included, thanks to the subreaper flag). With ``uid`` (the
    guest account itself): also ``kill(-1)``, which reaches every process of that user, however it got detached.
    Returns (number of processes killed, number still alive)."""
    killed = set()
    deadline = time.monotonic() + bound_s
    while True:
        reap()
        snapshot = table()
        targets = {pid for pid in descendants(snapshot, own_pid) if snapshot[pid][1] != "Z"}
        if uid is not None:
            targets |= {pid for pid, (_p, state, owner) in snapshot.items()
                        if owner == uid and state != "Z" and pid != own_pid}
        if not targets:
            return len(killed), 0
        if time.monotonic() >= deadline:
            return len(killed), len(targets)
        if uid is not None:
            try:
                kill(-1, signal.SIGKILL)        # every process of the guest user except this one
            except (ProcessLookupError, PermissionError):
                pass
        for pid in targets:
            try:
                kill(pid, signal.SIGKILL)
            except (ProcessLookupError, PermissionError):
                pass
        killed |= targets
        time.sleep(0.05)


def run_adapter(argv, executable, env, cwd, ttl_s, log_cap, grace, interrupt, scanners=(None, None), tokens=()):
    """Run the agent. Returns (termination, exit_code, signal, (stdout_sink, stderr_sink), stragglers).

    Never raises for agent behaviour. ``scanners`` (one adapter refusal scanner per stream, stdout then stderr) are
    fed everything their stream carries, before any cap applies. When this returns, every process the agent started is dead (or reported in
    ``stragglers = (killed, left)``)."""
    stdout_sink = CappedSink(log_cap, scanners[0], tokens)
    stderr_sink = CappedSink(log_cap, scanners[1], tokens)

    def child_setup():
        os.umask(0o022)
        try:
            import resource
            resource.setrlimit(resource.RLIMIT_CORE, (0, 0))
        except (ImportError, ValueError, OSError):
            pass

    become_subreaper()
    proc = subprocess.Popen(argv, executable=executable, cwd=cwd, env=env, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, close_fds=True,
                            start_new_session=True, preexec_fn=child_setup)
    pgid = proc.pid                     # setsid: the child leads its own session and process group
    threads = [threading.Thread(target=pump, args=(proc.stdout.fileno(), stdout_sink), daemon=True),
               threading.Thread(target=pump, args=(proc.stderr.fileno(), stderr_sink), daemon=True)]
    for thread in threads:
        thread.start()
    deadline = time.monotonic() + ttl_s
    termination = None
    while True:
        remaining = deadline - time.monotonic()
        if interrupt.signum is not None:
            termination = "killed"
        elif remaining <= 0:
            termination = "timeout"
        if termination:
            kill_group(pgid, signal.SIGTERM)
            try:
                proc.wait(timeout=grace)
            except subprocess.TimeoutExpired:
                kill_group(pgid, signal.SIGKILL)
                proc.wait()
            break
        try:
            proc.wait(timeout=min(0.2, remaining))
            break
        except subprocess.TimeoutExpired:
            continue
    kill_group(pgid, signal.SIGKILL)    # stragglers that stayed in the group
    stragglers = sweep_stragglers(os.getpid(), sweep_uid())     # and the ones that left it
    for thread in threads:
        thread.join(timeout=5)
    for pipe in (proc.stdout, proc.stderr):
        try:
            pipe.close()
        except OSError:
            pass
    code = proc.returncode
    sig = -code if code is not None and code < 0 else None
    if termination is None:
        termination = "killed" if sig is not None else "exited"
    exit_code = 128 + sig if sig is not None else code
    return termination, exit_code, sig, (stdout_sink, stderr_sink), stragglers


def merge_refusals(lists, limit=16):
    """Add up the per-stream refusal lists ({type, status, count} each) into one sorted list."""
    totals = {}
    for entries in lists:
        for item in entries:
            key = (item["type"], item["status"])
            if key in totals or len(totals) < limit:
                totals[key] = totals.get(key, 0) + item["count"]
    return [{"type": kind, "status": status, "count": count}
            for (kind, status), count in sorted(totals.items(), key=lambda entry: (entry[0][0], entry[0][1] or 0))]


def find_executable(name, path, work):
    if "/" in name:
        full = name if os.path.isabs(name) else os.path.join(work, name)
        return full if os.path.isfile(full) and os.access(full, os.X_OK) else None
    return shutil.which(name, path=path)


# --------------------------------------------------------------------------------------------- out/

def remove_tree(path):
    """Delete a file, link or directory tree without following links; make unreadable directories writable first.

    Iterative, relative to a directory descriptor, and at most two descriptors are open at any time, however deep the
    tree is. Each directory is listed once; its remaining names live in the stack, so the cost is linear in the number
    of entries (a directory with thousands of children is not re-read after every child). Going back up opens ``..``
    and checks that it is the directory the walk came from."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISDIR(info.st_mode):
        os.unlink(path)
        return
    flags = os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW | getattr(os, "O_CLOEXEC", 0)
    os.chmod(path, 0o700)
    fd = os.open(path, flags)
    try:
        frames = [(os.listdir(fd), None, None)]         # (names left, identity of the parent, name in the parent)
        while frames:
            names, parent_id, name = frames[-1]
            descended = False
            while names:
                entry = names.pop()
                try:
                    mode = os.lstat(entry, dir_fd=fd).st_mode
                except FileNotFoundError:
                    continue
                if not stat.S_ISDIR(mode):
                    os.unlink(entry, dir_fd=fd)
                    continue
                os.chmod(entry, 0o700, dir_fd=fd)
                child = os.open(entry, flags, dir_fd=fd)
                here = os.fstat(fd)
                os.close(fd)
                fd = child
                frames.append((os.listdir(fd), (here.st_dev, here.st_ino), entry))
                descended = True
                break
            if descended:
                continue
            frames.pop()
            if parent_id is None:
                break
            up = os.open("..", flags, dir_fd=fd)
            os.close(fd)
            fd = up
            now = os.fstat(fd)
            if (now.st_dev, now.st_ino) != parent_id:
                raise OSError("the directory tree changed while it was being removed")
            os.rmdir(name, dir_fd=fd)
    finally:
        os.close(fd)
    os.rmdir(path)


def reset_out(out):
    """Make ``out`` an empty real directory. The agent shares our uid, so it may have put anything there (or replaced
    the directory by a link): nothing in it is ours until we write it."""
    try:
        info = os.lstat(out)
    except FileNotFoundError:
        info = None
    if info is not None and not stat.S_ISDIR(info.st_mode):
        os.unlink(out)
        info = None
    if info is None:
        os.makedirs(out, mode=0o755)
    else:
        os.chmod(out, 0o755)
        for entry in os.listdir(out):
            remove_tree(os.path.join(out, entry))
    os.makedirs(os.path.join(out, "artifacts"), mode=0o755)


def prune_out(out, keep):
    """Remove every entry of ``out`` that is not in ``keep`` (relative paths of regular files the runner wrote itself)
    or a directory leading to one. A kept path that is not a regular file is removed as well. Returns the removals."""
    wanted_dirs = {"artifacts"}
    for rel in keep:
        parts = rel.split("/")
        wanted_dirs.update("/".join(parts[:i]) for i in range(1, len(parts)))
    removed = []
    stack = [""]
    while stack:
        rel_dir = stack.pop()
        base = os.path.join(out, rel_dir) if rel_dir else out
        try:
            names = os.listdir(base)
        except OSError:
            continue
        for name in names:
            rel = f"{rel_dir}/{name}" if rel_dir else name
            full = os.path.join(out, rel)
            try:
                mode = os.lstat(full).st_mode
            except OSError:
                continue
            if stat.S_ISDIR(mode):
                if rel in wanted_dirs:
                    stack.append(rel)
                    continue
            elif stat.S_ISREG(mode) and rel in keep:
                continue
            remove_tree(full)
            removed.append(rel)
    return removed


# --------------------------------------------------------------------------------------------- results

def redact_file(path, secrets):
    """Redact the tokens in a file in place; returns the number of replacements."""
    with open(path, "rb") as handle:
        data = handle.read()
    data, count = secrets.redact(data)
    if count:
        safe_atomic_write(path, data)
    return count


def scan_worktree(work, git, secrets, redactions, rejected):
    """Redact the token in working-tree files that would be committed. Returns the number of files changed."""
    proc = git.run("ls-files", "-z", "-c", "-o", "--exclude-standard", binary=True, check=False)
    changed = 0
    for raw in proc.stdout.split(b"\x00"):
        if not raw:
            continue
        rel = os.fsdecode(raw)
        full = os.path.join(work, rel)
        try:
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            continue                    # a symlink, a vanished file or a socket: git stores links, never follows
        try:
            if not stat.S_ISREG(os.fstat(fd).st_mode):
                continue
            scrub, strict = scan_fd(fd, secrets)
        finally:
            os.close(fd)
        if strict:
            if len(rejected) < MAX_REJECTED:
                rejected.append({"path": rel, "reason": "secret_leak"})
            return -1
        if scrub:
            try:
                size = os.lstat(full).st_size
                if size > 256 * MIB:
                    raise OSError("too large to rewrite")
                if redact_file(full, secrets):
                    changed += 1
            except OSError:
                if len(rejected) < MAX_REJECTED:
                    rejected.append({"path": rel, "reason": "secret_leak"})
                return -1
    redactions["files"] += changed
    return changed


def purge_special_files(work, rejected):
    """Delete FIFOs, sockets and device nodes from the work tree: git would block reading a FIFO. Never follows links."""
    stack = [work]
    while stack:
        current = stack.pop()
        try:
            entries = list(os.scandir(current))
        except OSError:
            continue
        for entry in entries:
            if entry.name == ".git" and current == work:
                continue
            try:
                mode = entry.stat(follow_symlinks=False).st_mode
            except OSError:
                continue
            if stat.S_ISDIR(mode):
                stack.append(entry.path)
            elif not (stat.S_ISREG(mode) or stat.S_ISLNK(mode)):
                try:
                    os.unlink(entry.path)
                except OSError:
                    pass
                if len(rejected) < MAX_REJECTED:
                    rejected.append({"path": os.path.relpath(entry.path, work)[:200], "reason": "special_file"})


MAX_OID_LIST = 64 * MIB


def objects_have_token(git, base, secrets, scratch, timeout=GIT_TIMEOUT_S):
    """Scan the raw bytes of every object reachable from HEAD but not from ``base`` (commit headers and messages,
    tree entries, blobs): True when a secret is in one, None when the scan could not be completed, else False.

    Raw objects, not diffs: a diff hides a token in a merge, in a committer name, in a file that a later commit
    removed, or behind a textconv driver. ``git`` is bound to the runner's own repository."""
    listing = git.run("rev-list", "--objects", "--no-object-names", f"{base}..HEAD", binary=True, check=False,
                      timeout=timeout)
    if listing.returncode != 0 or len(listing.stdout) > MAX_OID_LIST:
        return None
    oids = os.path.join(scratch, "oids")
    with open(oids, "wb") as handle:
        handle.write(listing.stdout)
    timed_out = []
    with open(oids, "rb") as feed:
        proc = subprocess.Popen(git.base_args + ["cat-file", "--batch"], cwd=git.work, env=git.env, stdin=feed,
                                stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)

        def expire():
            timed_out.append(True)
            proc.kill()
        timer = threading.Timer(timeout, expire)
        timer.start()
        found = False
        try:
            found = any(scan_fd(proc.stdout.fileno(), secrets))
        finally:
            timer.cancel()
            if found:
                proc.kill()
            proc.stdout.close()
            proc.wait()
    if timed_out or (proc.returncode != 0 and not found):
        return None
    return found


def verified_repository(args, card, scratch):
    """A fresh repository made and configured by the runner (no hooks, no info/, no replace refs, known config) holding
    the base commit from the controller's bundle plus the agent's HEAD, fetched from the agent's repository. Every git
    step after the run happens here, with the agent's work tree as its working directory. Returns its Git."""
    plain = Git(args.work, args.path, args.home)
    plain.run("init", "-q", "--template=", "-b", "motel-verify", os.path.join(scratch, "repo"), cwd=scratch)
    git_dir = os.path.join(scratch, "repo", ".git")
    git = plain.bound_to(git_dir, args.work)
    git.run("bundle", "unbundle", os.path.join(args.indir, "input.bundle"))
    if git.run("cat-file", "-e", card["base"] + "^{commit}", check=False)[0] != 0:
        raise Stop("base_missing", "the bundle does not contain the base commit")
    git.run("update-ref", "refs/motel/base", card["base"])
    # the agent's repository is only read here, through upload-pack, by a fetch that checks every object it receives
    fetched = git.run("-c", "fetch.fsckObjects=true", "-c", "transfer.fsckObjects=true", "fetch", "-q", "--no-tags",
                      "--no-write-fetch-head", "--no-recurse-submodules", os.path.join(args.work, ".git"),
                      f"+HEAD:refs/heads/{card['branch']}", check=False)
    if fetched[0] != 0:
        raise Stop("git_broken", "the agent's repository could not be read")
    git.run("symbolic-ref", "HEAD", "refs/heads/" + card["branch"])
    git.run("read-tree", "HEAD")
    return git


def collect_changes(card, args, out, secrets, budget, redactions, rejected, result):
    """Commit the agent's work and produce the bundle. Fills ``result``."""
    work = args.work
    purge_special_files(work, rejected)
    scratch = tempfile.mkdtemp(prefix="motel-verify-", dir=os.path.join(args.home, "tmp"))
    try:
        try:
            git = verified_repository(args, card, scratch)
        except (Stop, subprocess.TimeoutExpired) as exc:
            rejected.append({"path": "result.bundle", "reason": "git_broken", "detail": str(getattr(exc, "detail", ""))[:200]})
            return
        collect_with(git, card, work, out, secrets, budget, redactions, rejected, result, scratch)
    finally:
        remove_tree(scratch)


def collect_with(git, card, work, out, secrets, budget, redactions, rejected, result, scratch):
    base = card["base"]
    rc_tracked, _ = git.run("ls-files", "--error-unmatch", "RESULT.json", check=False)
    pathspec = [".", ":(exclude)RESULT.json"] if rc_tracked != 0 else ["."]
    if secrets:
        if scan_worktree(work, git, secrets, redactions, rejected) < 0:
            rejected.append({"path": "result.bundle", "reason": "secret_leak"})
            return
    if git.run("add", "-A", "--", *pathspec, check=False)[0] != 0:
        rejected.append({"path": "result.bundle", "reason": "git_add_failed"})
        return
    if git.run("diff", "--cached", "--quiet", check=False)[0] == 1:
        if git.run("commit", "-q", "--no-verify", "-m", "motel guest {}: attempt {}".format(card["guest"], card["attempt"]),
                   check=False)[0] != 0:
            rejected.append({"path": "result.bundle", "reason": "git_commit_failed"})
            return
    rc, head = git.run("rev-parse", "--verify", "HEAD", check=False)
    if rc != 0 or not SHA_RE.fullmatch(head):
        rejected.append({"path": "result.bundle", "reason": "git_broken"})
        return
    result["head"] = head
    proc = git.run("diff", "--name-only", "--no-renames", "--no-textconv", "--no-ext-diff", "-z", base, "HEAD",
                   check=False, binary=True)
    if proc.returncode == 0:
        files = [os.fsdecode(n).encode("utf-8", "backslashreplace").decode("utf-8") for n in proc.stdout.split(b"\x00") if n]
        result["changed_files_truncated"] = len(files) > MAX_CHANGED_FILES
        result["changed_files"] = files[:MAX_CHANGED_FILES]
    if head == base:
        return                          # nothing to hand over; git refuses an empty bundle
    if secrets:
        verdict = objects_have_token(git, base, secrets, scratch)
        if verdict is None:
            rejected.append({"path": "result.bundle", "reason": "scan_failed"})
            return
        if verdict:
            rejected.append({"path": "result.bundle", "reason": "secret_leak"})
            return
    bundle = os.path.join(out, "result.bundle")
    if git.run("bundle", "create", bundle, f"{base}..HEAD", check=False)[0] != 0:
        rejected.append({"path": "result.bundle", "reason": "bundle_failed"})
        return
    size = os.lstat(bundle).st_size
    if size > budget[0]:
        os.unlink(bundle)
        rejected.append({"path": "result.bundle", "reason": "too_large"})
        return
    if git.run("bundle", "verify", bundle, check=False)[0] != 0:
        os.unlink(bundle)
        rejected.append({"path": "result.bundle", "reason": "bundle_failed"})
        return
    budget[0] -= size
    result["bundle_bytes"] = size
    result["bundle_sha256"] = sha256_file(bundle)


def clean_relative(rel):
    parts = rel.split("/")
    if (not rel or rel.startswith("/") or any(p in ("", ".", "..", ".git") for p in parts)
            or any(ord(c) < 32 or c == "\x7f" for c in rel)):
        return False
    try:
        rel.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def copy_artifacts(card, work, out, secrets, budget, rejected):
    """Copy declared artifacts: regular files only, reached without following any link, size capped."""
    patterns = card.get("artifacts") or []
    found = []
    seen = set()
    real_work = os.path.realpath(work)
    for pattern in patterns:
        for rel in sorted(glob.glob(pattern, root_dir=work, recursive=True)):
            if rel not in seen:
                seen.add(rel)
                found.append(rel)
    accepted = []
    dest_root = os.path.join(out, "artifacts")

    def reject(rel, reason):
        if len(rejected) < MAX_REJECTED:
            rejected.append({"path": rel[:200], "reason": reason})

    for rel in found:
        if len(accepted) >= MAX_ARTIFACT_FILES:
            reject(rel, "too_many_files")
            continue
        if not clean_relative(rel):
            reject(rel, "bad_path")
            continue
        full = os.path.join(work, rel)
        try:
            info = os.lstat(full)
        except OSError:
            continue
        if stat.S_ISDIR(info.st_mode):
            continue                    # a pattern like reports/* may match directories; only files count
        if stat.S_ISLNK(info.st_mode):
            reject(rel, "symlink")
            continue
        if not stat.S_ISREG(info.st_mode):
            reject(rel, "not_regular")
            continue
        if os.path.realpath(full) != os.path.join(real_work, rel):
            reject(rel, "symlinked_parent")
            continue
        try:
            fd = os.open(full, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK | os.O_CLOEXEC)
        except OSError:
            reject(rel, "unreadable")
            continue
        dest = os.path.join(dest_root, rel)
        try:
            opened = os.fstat(fd)
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                reject(rel, "changed_while_reading")
                continue
            if opened.st_size > budget[0]:
                reject(rel, "too_large")
                continue
            os.makedirs(os.path.dirname(dest), mode=0o755, exist_ok=True)
            digest, total, tail = hashlib.sha256(), 0, b""
            leaked = False
            with open(dest, "wb") as sink:
                while True:
                    block = os.read(fd, 1 << 20)
                    if not block:
                        break
                    total += len(block)
                    if total > budget[0]:
                        break
                    window = tail + block
                    if secrets and secrets.found(window):
                        leaked = True
                        break
                    tail = window[-secrets.keep():] if secrets.keep() else b""
                    digest.update(block)
                    sink.write(block)
            if leaked:
                os.unlink(dest)
                reject(rel, "secret_leak")
            elif total > budget[0]:
                os.unlink(dest)
                reject(rel, "too_large")
            else:
                budget[0] -= total
                accepted.append({"path": rel, "bytes": total, "sha256": digest.hexdigest()})
        finally:
            os.close(fd)
    return accepted


def canonical_json(doc):
    """One fixed spelling of a JSON document: sorted keys, no spaces, ASCII only (non-ASCII as \\uXXXX), no NaN."""
    return json.dumps(doc, ensure_ascii=True, sort_keys=True, separators=(",", ":"), allow_nan=False).encode("ascii")


def copy_result_json(work, home, out, secrets, rejected):
    """The agent's own RESULT.json: $MOTEL_RESULT (under HOME) first, then the repository root.

    The file is parsed and written back in canonical form. Both the raw bytes and the canonical form are scanned for the
    secret tokens: JSON lets the agent spell a token with escapes (``\\u0073k-...``) that a plain byte search misses and
    that a parser undoes. Only the canonical form reaches ``out/RESULT.json``; True is returned only then."""
    for candidate in (os.path.join(home, "RESULT.json"), os.path.join(work, "RESULT.json")):
        try:
            data = read_regular(candidate, RESULT_JSON_CAP)
        except Stop as exc:
            rejected.append({"path": "RESULT.json", "reason": "too_large" if exc.reason == "input_too_large"
                             else "not_regular"})
            return False
        if data is None:
            continue
        try:
            doc = json.loads(data.decode("utf-8"))
            if not isinstance(doc, dict):
                raise ValueError("not an object")
            canonical = canonical_json(doc)
        except (ValueError, UnicodeDecodeError, RecursionError):
            rejected.append({"path": "RESULT.json", "reason": "invalid_json"})
            return False
        if secrets and (secrets.found(data) or secrets.found(canonical)):
            rejected.append({"path": "RESULT.json", "reason": "secret_leak"})
            return False
        safe_atomic_write(os.path.join(out, "RESULT.json"), canonical)
        return True
    return False


# --------------------------------------------------------------------------------------------- main flow

OWN_LOGIN_TOKEN_RE = re.compile(rb"^[\x21-\x7e]{8,4094}$")
OWN_LOGIN_LINE_CAP = 4096


def read_own_login_token(stream):
    """Exactly one line from stdin, at most 4 KB, held in memory only. Raises Stop('own_login_missing_token')."""
    line = stream.readline(OWN_LOGIN_LINE_CAP + 2)
    if not line.strip():
        raise Stop("own_login_missing_token", "no token was supplied on stdin")
    if len(line) > OWN_LOGIN_LINE_CAP + 1 or (len(line) > OWN_LOGIN_LINE_CAP and not line.endswith(b"\n")):
        raise Stop("own_login_invalid_token", "the token line is longer than 4 KB")
    token = line.rstrip(b"\r\n")
    if not OWN_LOGIN_TOKEN_RE.fullmatch(token):
        raise Stop("own_login_invalid_token", "the token line has an unexpected format")
    return token


def build_env(card, adapters, ctx, home, path, oauth_token=b""):
    spec = adapters.ADAPTERS[card["adapter"]]
    tmp = os.path.join(home, "tmp")
    env = {"PATH": path, "HOME": home, "USER": "guest", "LOGNAME": "guest", "SHELL": "/bin/bash",
           "LANG": "C.UTF-8", "TERM": "dumb", "TMPDIR": tmp, "CI": "1", "NO_COLOR": "1",
           "XDG_CONFIG_HOME": os.path.join(home, ".config"), "XDG_CACHE_HOME": os.path.join(home, ".cache"),
           "GIT_AUTHOR_NAME": BOT_NAME, "GIT_AUTHOR_EMAIL": BOT_EMAIL,
           "GIT_COMMITTER_NAME": BOT_NAME, "GIT_COMMITTER_EMAIL": BOT_EMAIL, "GIT_TERMINAL_PROMPT": "0",
           "MOTEL_TOUR": card["tour"], "MOTEL_GUEST": card["guest"], "MOTEL_ATTEMPT": card["attempt"],
           "MOTEL_BASE": card["base"], "MOTEL_BRANCH": card["branch"], "MOTEL_WORK": ctx.work,
           "MOTEL_RESULT": os.path.join(home, "RESULT.json")}
    env.update(card.get("env", {}))
    if oauth_token:
        # The login token reaches the adapter process and nothing else: not a file, not an argument.
        env["CLAUDE_CODE_OAUTH_TOKEN"] = oauth_token.decode("ascii")
    for binding in card.get("bindings", []):
        env.update(adapters.binding_env(binding, card["concierge_port"], card.get("placeholder") or ""))
    env.update(spec.env(ctx))
    return env


def write_config_files(files, home, secrets):
    for rel, text in files.items():
        parts = rel.split("/")
        if not rel or rel.startswith("/") or any(p in ("", ".", "..") for p in parts):
            raise Stop("config_failed", "bad config path")
        data = text.encode("utf-8")
        if secrets and secrets.found(data):
            raise Stop("config_failed", "a config file would hold a secret token")
        dest = os.path.join(home, rel)
        os.makedirs(os.path.dirname(dest), mode=0o700, exist_ok=True)
        safe_atomic_write(dest, data, 0o600)


def prepare_workspace(args, card):
    work, home = args.work, args.home
    real_home = os.path.realpath(home)
    if os.path.commonpath([real_home, os.path.realpath(work)]) != real_home or os.path.realpath(work) == real_home:
        raise Stop("runner_error", "work must be a directory below home")
    os.makedirs(home, mode=0o755, exist_ok=True)
    os.makedirs(os.path.join(home, "tmp"), mode=0o700, exist_ok=True)
    if os.path.lexists(work):
        shutil.rmtree(work)
    os.makedirs(work, mode=0o755)
    git = Git(work, args.path, home)
    if shutil.which("git", path=args.path) is None:
        raise Stop("git_missing", "git is not installed in the image")
    bundle = os.path.join(args.indir, "input.bundle")
    if not os.path.isfile(bundle) or os.path.islink(bundle):
        raise Stop("input_missing", "input.bundle")
    git.run("init", "-q", "-b", "motel-start")
    git.run("bundle", "unbundle", bundle)
    if git.run("cat-file", "-e", card["base"] + "^{commit}", check=False)[0] != 0:
        raise Stop("base_missing", "the bundle does not contain the base commit")
    git.run("checkout", "-q", "-b", card["branch"], card["base"])
    overlay = os.path.join(args.indir, "overlay.patch")
    overlay_commit = None
    patch = read_regular(overlay, 64 * MIB)
    if patch is not None:
        if patch.strip():
            proc = git.run("apply", "--whitespace=nowarn", "-", stdin=patch, binary=True, check=False)
            if proc.returncode != 0:
                raise Stop("overlay_failed", "the overlay patch does not apply to the base commit")
            git.run("add", "-A")
            if git.run("diff", "--cached", "--quiet", check=False)[0] == 1:
                git.run("commit", "-q", "--no-verify", "-m", "motel overlay for {}".format(card["guest"]))
                overlay_commit = git.run("rev-parse", "HEAD")[1]
    return git, overlay_commit


def execute(args, adapters):
    """Everything between 'read the card' and 'write run.json'. Returns the run document."""
    started = utc_now()
    t0 = time.monotonic()
    result = {"schema": SCHEMA_RUN, "attempt": None, "exit_code": None, "termination": "failed", "started": started,
              "finished": None, "head": None, "changed_files": [], "bundle_sha256": None, "artifacts": [],
              "stdout_bytes": 0, "stderr_bytes": 0, "command_started": False,
              "concierge_refusals": [], "result_json": False}
    rejected, redactions = [], {"stdout": 0, "stderr": 0, "files": 0}
    secrets = Secrets()
    oauth = b""
    out = args.out
    try:
        reset_out(out)
        if os.geteuid() == 0 and not args.allow_root:
            raise Stop("runner_is_root", "the runner must run as the unprivileged guest user")
        raw = read_regular(os.path.join(args.indir, "card.json"), 1 << 20)
        if raw is None:
            raise Stop("input_missing", "card.json")
        card = validate_card(raw, adapters)
        result.update(attempt=card["attempt"], tour=card["tour"], guest=card["guest"], adapter=card["adapter"],
                      base=card["base"], branch=card["branch"], head=card["base"], overlay_commit=None)
        result["auth"] = card.get("auth", "api-key")
        if result["auth"] == "own-login":
            oauth = read_own_login_token(args.stdin)
        secrets = Secrets(scrub=[(card.get("placeholder") or "").encode()], strict=[oauth])
        interrupt = Interrupt()
        git, overlay_commit = prepare_workspace(args, card)
        result["overlay_commit"] = overlay_commit
        ctx = adapters.Context(card=card, home=args.home, work=args.work, port=card["concierge_port"])
        spec = adapters.ADAPTERS[card["adapter"]]
        try:
            argv = spec.argv(ctx)
            files = spec.files(ctx)
        except (ValueError, KeyError, TypeError) as exc:
            raise Stop("invalid_card", str(exc)[:200]) from None
        env = build_env(card, adapters, ctx, args.home, args.path, oauth)
        write_config_files(files, args.home, secrets)
        name = spec.binary or argv[0]
        executable = find_executable(name, args.path, args.work)
        if executable is None:
            raise Stop("adapter_missing", f"{os.path.basename(name)[:64]} is not installed in this image")
        if interrupt.signum is not None:
            result.update(termination="killed", exit_code=128 + interrupt.signum)
        else:
            result["command_started"] = True
            scanners = (spec.refusals(), spec.refusals())
            termination, exit_code, sig, sinks, stragglers = run_adapter(
                argv, executable, env, args.work, card["ttl_s"], args.log_cap, args.kill_grace, interrupt, scanners,
                secrets.tokens)
            # Every process of the agent is dead now. Whatever it left in out/ is not ours: start from an empty one.
            reset_out(out)
            result.update(termination=termination, exit_code=exit_code, signal=sig,
                          stdout_total_bytes=sinks[0].total, stderr_total_bytes=sinks[1].total,
                          concierge_refusals=merge_refusals(scanner.result() for scanner in scanners),
                          stragglers_killed=stragglers[0])
            for key, sink in zip(("stdout", "stderr"), sinks, strict=True):
                data, omitted = sink.finish()
                redactions[key] = sink.redactor.count
                if secrets.found(data):         # cannot happen; the last guard before the file is written
                    data, _ = secrets.redact(data)
                result[key + "_truncated"] = omitted
                write_log(out, key + ".log", data)
            if stragglers[1]:
                result.update(termination="failed", reason="stragglers_left", stragglers_left=stragglers[1])
        budget = [card.get("max_result_mb", DEFAULT_RESULT_MB) * MIB]
        if result.get("stragglers_left"):
            pass            # a live process may still write: hand over the logs and the record, nothing it could shape
        elif result["command_started"] or interrupt.signum is not None:
            collect_changes(card, args, out, secrets, budget, redactions, rejected, result)
            result["artifacts"] = copy_artifacts(card, args.work, out, secrets, budget, rejected)
            result["result_json"] = copy_result_json(args.work, args.home, out, secrets, rejected)
            if any(item["reason"] == "secret_leak" for item in rejected):
                # A secret in anything handed to the controller fails the attempt; the item is withheld.
                result.update(termination="failed", reason="secret_leak")
    except Stop as exc:
        result.update(termination="failed", reason=exc.reason, command_started=exc.started or result["command_started"])
        if exc.detail:
            result["detail"] = exc.detail[:200]
        if result["exit_code"] is None:
            result["exit_code"] = 127 if exc.reason == "adapter_missing" else None
    except subprocess.TimeoutExpired:
        result.update(termination="failed", reason="git_timeout")
    except Exception as exc:     # a runner bug must still leave a record
        result.update(termination="failed", reason="runner_error", detail=type(exc).__name__)
    # Only what the runner wrote itself may stay in out/ (the agent is dead, but be exact about it), then log sizes.
    keep = {name for name in ("stdout.log", "stderr.log") if os.path.lexists(os.path.join(out, name))}
    if result.get("bundle_sha256"):
        keep.add("result.bundle")
    if result.get("result_json"):
        keep.add("RESULT.json")
    keep.update("artifacts/" + item["path"] for item in result.get("artifacts") or ())
    try:
        pruned = prune_out(out, keep)
        if pruned:
            result["pruned_out"] = len(pruned)
    except Exception as exc:        # run.json is written whatever happens; say that out/ may hold more than it should
        result.update(termination="failed", reason="out_not_clean", detail=type(exc).__name__)
    for key in ("stdout", "stderr"):
        path = os.path.join(out, key + ".log")
        if os.path.exists(path):
            result[key + "_bytes"] = os.path.getsize(path)
    result.update(rejected=rejected, redactions=redactions, finished=utc_now(),
                  duration_s=round(time.monotonic() - t0, 3))
    return result, secrets


def write_run_json(out, result, secrets):
    text = json.dumps(result, indent=2, sort_keys=True).encode("utf-8") + b"\n"
    if secrets and secrets.found(text):     # the guard of last resort: no token reaches run.json
        text, _ = secrets.redact(text)
    safe_atomic_write(os.path.join(out, "run.json"), text)


def main(argv=None):
    home_default = None                 # the fixed HOME of the guest account, taken from the system, not guessed
    try:
        import pwd
        home_default = pwd.getpwnam("guest").pw_dir
    except (ImportError, KeyError):
        pass
    parser = argparse.ArgumentParser(prog="runner.py", description="motel tour guest runner")
    parser.add_argument("--in", dest="indir", default=DEFAULT_IN)
    parser.add_argument("--home", default=home_default)
    parser.add_argument("--work", default=None)
    parser.add_argument("--out", default=None)
    parser.add_argument("--path", default=PATH_DEFAULT, help="PATH given to the agent (default: a clean fixed one)")
    parser.add_argument("--log-cap", type=int, default=None,
                        help="bytes kept per output stream (default: adapters.LOG_CAP_BYTES, which the engine fetches)")
    parser.add_argument("--kill-grace", type=float, default=KILL_GRACE_S, help="seconds between TERM and KILL")
    parser.add_argument("--allow-root", action="store_true", help="tests only: allow running as root")
    args = parser.parse_args(argv)
    args.stdin = sys.stdin.buffer
    if args.home is None:
        parser.error("there is no 'guest' account on this machine; pass --home")
    args.work = args.work or os.path.join(args.home, "work")
    args.out = args.out or os.path.join(args.home, "out")
    try:
        adapters = load_adapters()
        if args.log_cap is None:
            args.log_cap = adapters.LOG_CAP_BYTES
        result, secrets = execute(args, adapters)
    except Stop as exc:
        result, secrets = {"schema": SCHEMA_RUN, "attempt": None, "exit_code": None, "termination": "failed",
                         "reason": exc.reason, "started": utc_now(), "finished": utc_now(), "head": None,
                         "changed_files": [], "bundle_sha256": None, "artifacts": [], "stdout_bytes": 0,
                         "stderr_bytes": 0, "command_started": False}, Secrets()
    try:
        os.makedirs(args.out, mode=0o755, exist_ok=True)
        write_run_json(args.out, result, secrets)
    except OSError:
        return 70
    return 0


if __name__ == "__main__":
    sys.exit(main())
