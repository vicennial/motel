"""Git work done as the invoking user, never as root: make the input bundles for a tour, import a result bundle.

Making: for each guest, resolve ``base`` to a full commit and bundle that commit's history. The bundle is a file
the user owns in a private temp directory; the desk copies it with ``O_NOFOLLOW``, a size cap and a digest.
Importing: ``git fetch`` of the guest's bundle into ``refs/heads/motel/<tour>/<guest>``. It never checks out,
merges or rebases anything, and it checks every object (``transfer.fsckObjects``) because the bundle came from
an untrusted guest.
"""

from __future__ import annotations

import contextlib
import hashlib
import os
import re
import secrets
import selectors
import shutil
import signal
import stat
import subprocess
import tempfile
import time
from dataclasses import dataclass

from motel.errors import install_hint
from motel.tours.errors import Broken, NotFound, Usage

SAFE_ENV = {"PATH": "/usr/bin:/bin:/usr/local/bin", "LANG": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0",
            "GIT_CONFIG_NOSYSTEM": "1"}
BRANCH_RE = re.compile(r"^motel/t-[0-9a-f]{10}/[a-z0-9](?:[a-z0-9-]{0,30}[a-z0-9])?$")


def run_git(args, cwd: str | None = None, input_bytes: bytes | None = None) -> tuple:
    env = dict(SAFE_ENV)
    env["HOME"] = os.environ.get("HOME", "/nonexistent")
    try:
        proc = subprocess.run(["git", *args], cwd=cwd, input=input_bytes, capture_output=True, env=env, check=False,
                              stdin=None if input_bytes is not None else subprocess.DEVNULL)
    except FileNotFoundError:
        raise Usage("git is not installed", "install git") from None
    return proc.returncode, proc.stdout, proc.stderr


def resolve(repo: str, revision: str, git=run_git) -> str:
    rc, _o, _e = git(["rev-parse", "--git-dir"], cwd=repo)
    if rc != 0:
        raise Usage(f"{repo} is not a git repository", "run it inside a git checkout or set `repo` in the guest")
    rc, out, _e = git(["rev-parse", "--verify", "--quiet", "--end-of-options", revision + "^{commit}"], cwd=repo)
    sha = out.decode().strip()
    if rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", sha):
        raise Usage(f"{revision!r} is not a commit in {repo}", "git -C REPO log --oneline")
    return sha


def make_bundle(repo: str, sha: str, dest: str, git=run_git) -> tuple:
    """Bundle the history of ``sha`` into ``dest`` (created exclusively). Returns (sha256, bytes)."""
    ref = f"refs/heads/motel-base-{secrets.token_hex(4)}"
    rc, _o, err = git(["update-ref", ref, sha], cwd=repo)
    if rc != 0:
        raise Usage("could not prepare the repository for bundling: " + err.decode(errors="replace").strip()[:200])
    try:
        rc, _o, err = git(["bundle", "create", dest, ref], cwd=repo)
    finally:
        git(["update-ref", "-d", ref], cwd=repo)
    if rc != 0:
        raise Usage("git could not make the bundle: " + err.decode(errors="replace").strip()[:200])
    return digest_of(dest)


def digest_of(path: str) -> tuple:
    h, size = hashlib.sha256(), 0
    with open(path, "rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            h.update(chunk)
            size += len(chunk)
    return h.hexdigest(), size


def import_bundle(path: str, branch: str, cwd: str, git=run_git) -> str:
    """Fetch the guest's branch from a verified bundle file into the repository at ``cwd``. Returns the tip."""
    if not BRANCH_RE.fullmatch(branch):
        raise Broken("the result names an unexpected branch")
    rc, _o, _e = git(["rev-parse", "--git-dir"], cwd=cwd)
    if rc != 0:
        raise Usage("this directory is not a git repository", "cd into the repository the tour was started from")
    rc, _o, err = git(["bundle", "verify", path], cwd=cwd)
    if rc != 0:
        lines = err.decode(errors="replace").strip().splitlines()
        raise NotFound("this repository does not have the commit the guest started from" + (f" ({lines[-1][:160]})" if lines else ""),
                       "git fetch the base commit first, then run this again")
    rc, out, _e = git(["bundle", "list-heads", path], cwd=cwd)
    wanted = f"refs/heads/{branch}"
    tips = {line.split(" ", 1)[1]: line.split(" ", 1)[0] for line in out.decode().splitlines() if " " in line}
    if rc != 0 or wanted not in tips:
        raise Broken("the bundle does not contain the guest's branch")
    rc, _o, err = git(["-c", "transfer.fsckObjects=true", "fetch", "--quiet", "--no-tags", path, f"{wanted}:{wanted}"], cwd=cwd)
    if rc != 0:
        raise Broken("git could not import the bundle: " + err.decode(errors="replace").strip()[:200]
                     + " (an existing branch with that name that has other history blocks the import)")
    return tips[wanted]


INSPECT_USER = "motel-inspect"
MAX_CHANGED = 2000


@dataclass(frozen=True)
class Sandbox:
    """How the git that reads a guest's bundle is run. Everything in the bundle came from an untrusted guest, and git
    parses it, so this git never runs as root: as ``user`` (a system account with no home, no shell and no files of its
    own), with no supplementary groups, in a per-attempt directory that user owns, and with hard limits."""

    user: str | None = INSPECT_USER      # used when the desk is root; an unprivileged desk (tests) runs git as itself
    address_space: int = 1 << 30
    cpu_s: int = 60                      # per git process
    file_bytes: int = 1 << 30            # the largest file one git process may write
    open_files: int = 256
    processes: int = 256                 # RLIMIT_NPROC is per user, and every supervisor shares this user
    total_s: float = 240.0               # all the git work for one attempt, copying included
    step_s: float = 120.0                # one git command
    output_bytes: int = 8 << 20          # what a command may print (not counting the streamed object scan)
    scratch_bytes: int = 4 << 30         # the whole scratch directory
    scratch_files: int = 50_000
    copy_bytes: int = 2 << 30            # the largest bundle that is copied in
    listing_bytes: int = 64 << 20        # the list of new objects (about a million of them)
    scan_bytes: int = 512 << 20          # new objects scanned for the own-login token, uncompressed


class _Cut(Exception):
    """A bound was hit while a command ran; the command was killed."""


INSPECT_ROOT = "/var/lib/motel-inspect"      # root:root 0711, outside the root-only tour state so the inspection user can enter


def setup_problems(work_root: str, sandbox: Sandbox | None = None, *, euid: int | None = None, which=shutil.which) -> list:
    """What is wrong with the setup that bundle inspection needs (empty when fine): the unprivileged user, the scratch
    root's owner and mode, and the ``prlimit`` and ``git`` programs. When the desk does not run as root, git runs as the
    desk user itself and there is no user or directory to check. Each message names the fix."""
    sb = sandbox or Sandbox()
    problems = []
    fix = f"run: {install_hint('--upgrade')} (with [tours] enabled)"
    search = "/usr/sbin:/usr/bin:/sbin:/bin:/usr/local/bin"
    for program in ("git", "prlimit"):
        if which(program, path=search) is None:
            problems.append(f"{program} is not installed, so result bundles cannot be inspected")
    if (os.geteuid() if euid is None else euid) != 0:
        return problems
    import pwd
    try:
        entry = pwd.getpwnam(sb.user or "")
        if entry.pw_uid == 0 or entry.pw_gid == 0:
            problems.append(f"the inspection user {sb.user} is root; it must be an unprivileged account")
    except KeyError:
        problems.append(f"the inspection user {sb.user!r} does not exist; {fix}")
    try:
        st = os.lstat(work_root)
        if not stat.S_ISDIR(st.st_mode) or st.st_uid != 0 or stat.S_IMODE(st.st_mode) != 0o711:
            problems.append(f"{work_root} must be a directory owned by root with mode 0711 (it is {st.st_uid}:{stat.S_IMODE(st.st_mode):o}); {fix}")
    except OSError:
        problems.append(f"{work_root} does not exist; {fix}")
    return problems


SCRATCH_RE = re.compile(r"^(a-[0-9a-f]{10})-[a-z0-9_]{8}$")      # what tempfile.mkdtemp makes from an attempt id
SCRATCH_MIN_AGE_S = 15 * 60


def _remove_scratch(work_root: str, name: str) -> bool:
    """Remove one scratch directory that is a direct child of ``work_root`` and a real directory (never a link):
    ``shutil.rmtree`` works through directory descriptors and does not follow links inside either."""
    path = os.path.join(work_root, name)
    try:
        if not stat.S_ISDIR(os.lstat(path).st_mode):
            return False
    except OSError:
        return False
    shutil.rmtree(path, ignore_errors=True)
    return not os.path.lexists(path)


def reap_scratch(work_root: str, attempt_status, *, now: float, apply: bool, min_age_s: float = SCRATCH_MIN_AGE_S) -> list:
    """Scratch directories an inspection left behind (a supervisor killed hard, a crashed desk). Only direct children of
    ``work_root`` named like an attempt's scratch qualify, and one is removed only when it is older than ``min_age_s``
    AND its attempt is finished or unknown to every tour: the age alone never decides. ``attempt_status(id)`` gives the
    attempt's status or None. Returns the actions (``{"action": "remove_inspect_scratch", ...}``)."""
    actions = []
    try:
        names = sorted(os.listdir(work_root))
    except OSError:
        return actions
    for name in names:
        match = SCRATCH_RE.fullmatch(name)
        if not match:
            continue
        try:
            st = os.lstat(os.path.join(work_root, name))
        except OSError:
            continue
        if not stat.S_ISDIR(st.st_mode) or now - st.st_mtime < min_age_s:
            continue
        status = attempt_status(match.group(1))
        if status is not None and status not in ("done", "failed", "timed_out", "over_budget", "cancelled"):
            continue            # its attempt is still running: it may be inspecting right now
        actions.append({"action": "remove_inspect_scratch", "dir": name, "attempt": match.group(1), "attempt_status": status})
        if apply:
            _remove_scratch(work_root, name)
    return actions


def remove_scratch_of(work_root: str, attempt_ids) -> int:
    """Remove the scratch directories of these attempts (a restarted supervisor cleaning up its own tour: nothing of it
    is running). Returns how many were removed."""
    wanted, removed = set(attempt_ids), 0
    try:
        names = os.listdir(work_root)
    except OSError:
        return 0
    for name in names:
        match = SCRATCH_RE.fullmatch(name)
        if match and match.group(1) in wanted and _remove_scratch(work_root, name):
            removed += 1
    return removed


def _identity(sandbox: Sandbox):
    """``(uid, gid)`` to drop to, or None when the desk is not root. Refuses to run git as root: a missing user is a
    failure to inspect (the bundle is then withheld), never a reason to fall back."""
    if os.geteuid() != 0:
        return None
    import pwd
    try:
        entry = pwd.getpwnam(sandbox.user or "")
    except KeyError:
        raise Broken(f"the inspection user {sandbox.user!r} does not exist; the bundle cannot be inspected "
                     "(run the installer with [tours] enabled)") from None
    if entry.pw_uid == 0 or entry.pw_gid == 0:
        raise Broken("the inspection user must not be root")
    return entry.pw_uid, entry.pw_gid


def _scratch_use(path: str, max_files: int | None = None) -> tuple:
    """``(bytes, files)`` under ``path``; with ``max_files`` the walk stops as soon as it has seen more than that."""
    size = files = 0
    stack = [path]
    while stack:
        if max_files is not None and files > max_files:
            break
        try:
            with os.scandir(stack.pop()) as it:
                for entry in it:
                    files += 1
                    if entry.is_dir(follow_symlinks=False):
                        stack.append(entry.path)
                    else:
                        with contextlib.suppress(OSError):
                            size += entry.stat(follow_symlinks=False).st_size
        except OSError:
            continue
    return size, files


class _Git:
    """Runs git for one attempt under the sandbox's limits and a single deadline."""

    def __init__(self, sandbox: Sandbox, scratch: str, ident):
        self.sb, self.scratch, self.ident = sandbox, scratch, ident
        self.deadline = time.monotonic() + sandbox.total_s

    def argv(self, args) -> list:
        sb = self.sb
        limits = [f"--as={sb.address_space}", f"--cpu={sb.cpu_s}", f"--fsize={sb.file_bytes}", f"--nofile={sb.open_files}",
                  "--core=0"]
        if self.ident is not None:       # RLIMIT_NPROC counts every process of the user: only meaningful for the dedicated one
            limits.append(f"--nproc={sb.processes}")
        config = ["core.hooksPath=/dev/null", "protocol.allow=never", "protocol.file.allow=always", "core.fsmonitor=false",
                  "pack.threads=1", "index.threads=1", "core.bigFileThreshold=1m", "gc.auto=0", "core.symlinks=false",
                  "fetch.unpackLimit=1", "transfer.unpackLimit=1"]
        return ["prlimit", *limits, "git", *[x for c in config for x in ("-c", c)], *args]

    def __call__(self, args, *, cwd=None, stdin_path=None, sink=None, cap=None, timeout=None):
        """``(returncode, stdout, stderr)``; with ``sink`` the stdout goes to it in chunks (and is not returned)."""
        sb = self.sb
        cap = sb.output_bytes if cap is None else cap
        env = {"PATH": "/usr/bin:/bin:/usr/local/bin", "LANG": "C.UTF-8", "GIT_TERMINAL_PROMPT": "0", "GIT_CONFIG_NOSYSTEM": "1",
               "GIT_CONFIG_GLOBAL": "/dev/null", "GIT_NO_REPLACE_OBJECTS": "1", "HOME": self.scratch, "GIT_ATTR_NOSYSTEM": "1"}
        until = min(self.deadline, time.monotonic() + (sb.step_s if timeout is None else timeout))
        extra = {} if self.ident is None else {"user": self.ident[0], "group": self.ident[1], "extra_groups": []}
        stdin = open(stdin_path, "rb") if stdin_path else subprocess.DEVNULL     # noqa: SIM115  closed below
        try:
            try:
                proc = subprocess.Popen(self.argv(args), cwd=cwd or self.scratch, env=env, stdin=stdin, stdout=subprocess.PIPE,
                                        stderr=subprocess.PIPE, start_new_session=True, **extra)
            except FileNotFoundError:
                raise Broken("git or prlimit is not installed, so the bundle cannot be inspected") from None
            except PermissionError:
                raise Broken("the inspection user cannot run git here") from None
        finally:
            if stdin_path:
                stdin.close()
        out, err, total = bytearray(), bytearray(), 0
        sel = selectors.DefaultSelector()
        sel.register(proc.stdout, selectors.EVENT_READ, "out")
        sel.register(proc.stderr, selectors.EVENT_READ, "err")
        next_check = 0.0
        try:
            while sel.get_map():
                now = time.monotonic()
                if now >= until:
                    raise _Cut("it took too long")
                for key, _mask in sel.select(timeout=min(0.2, until - now)):
                    data = os.read(key.fileobj.fileno(), 65536)
                    if not data:
                        sel.unregister(key.fileobj)
                        continue
                    if key.data == "err":
                        err += data[: max(0, 4096 - len(err))]
                        continue
                    total += len(data)
                    if total > cap:
                        raise _Cut("it printed too much")
                    if sink is not None:
                        sink(data)
                    else:
                        out += data
                if time.monotonic() >= next_check:
                    next_check = time.monotonic() + 0.25
                    used, files = _scratch_use(self.scratch, sb.scratch_files)
                    if used > sb.scratch_bytes or files > sb.scratch_files:
                        raise _Cut("the scratch repository grew too large")
            while proc.poll() is None:
                if time.monotonic() >= until:
                    raise _Cut("it took too long")
                time.sleep(0.01)
        except _Cut as exc:
            self._kill(proc)
            raise Broken(f"git was stopped while inspecting the result bundle ({exc})") from None
        except BaseException:
            self._kill(proc)
            raise
        finally:
            # Always, also after a clean exit: git runs in its own session, so a child it forked and left running (with its
            # output closed, so the loop above saw end of file) is still in this group and must not outlive the command.
            self._killpg(proc.pid)
            sel.close()
            proc.stdout.close()
            proc.stderr.close()
        return proc.returncode, bytes(out), bytes(err)

    @staticmethod
    def _killpg(pgid: int) -> None:
        with contextlib.suppress(ProcessLookupError, PermissionError):
            os.killpg(pgid, signal.SIGKILL)

    @classmethod
    def _kill(cls, proc) -> None:
        cls._killpg(proc.pid)
        with contextlib.suppress(Exception):
            proc.wait(timeout=5)

    def copy_in(self, src: str, name: str) -> str:
        """Copy a bundle into the scratch directory, owned by the inspection user (it cannot read the tour's own files)."""
        dest = os.path.join(self.scratch, name)
        fd_in = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
        try:
            fd_out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            try:
                copied = 0
                while True:
                    if time.monotonic() >= self.deadline:
                        raise Broken("copying the bundles for inspection took too long")
                    chunk = os.read(fd_in, 1 << 20)
                    if not chunk:
                        break
                    copied += len(chunk)
                    if copied > self.sb.copy_bytes:
                        raise Broken("a bundle is too large to inspect")
                    os.write(fd_out, chunk)
                if self.ident is not None:
                    os.fchown(fd_out, *self.ident)
            finally:
                os.close(fd_out)
        finally:
            os.close(fd_in)
        return dest

    def copy_out(self, src: str, dest: str) -> dict:
        """Copy the rebuilt bundle from the scratch directory to ``dest`` (new, root-owned): the file there belongs to the
        inspection user, so it is opened without following links and checked to be a regular file first."""
        fd_in = os.open(src, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC | os.O_NONBLOCK)
        try:
            st = os.fstat(fd_in)
            if not stat.S_ISREG(st.st_mode):
                raise Broken("the rebuilt bundle is not a regular file")
            if st.st_nlink != 1:       # a hard link could name a file of someone else that the inspection user can reach
                raise Broken("the rebuilt bundle has more than one hard link")
            if st.st_uid != (os.geteuid() if self.ident is None else self.ident[0]):
                raise Broken("the rebuilt bundle is not owned by the inspection user")
            fd_out = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
            digest, size = hashlib.sha256(), 0
            try:
                while True:
                    chunk = os.read(fd_in, 1 << 20)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self.sb.copy_bytes:
                        raise Broken("the rebuilt bundle is too large")
                    digest.update(chunk)
                    os.write(fd_out, chunk)
            except BaseException:
                os.close(fd_out)
                with contextlib.suppress(OSError):
                    os.unlink(dest)
                raise
            os.close(fd_out)
        finally:
            os.close(fd_in)
        return {"bytes": size, "sha256": digest.hexdigest()}


def inspect_result(base_bundle: str, result_bundle: str, base_sha: str, branch: str, *, work_root: str, attempt: str = "a",
                   token: str | None = None, sandbox: Sandbox | None = None, deliver_to: str | None = None) -> dict:
    """What the guest's bundle really contains, computed here and not taken from the guest's ``run.json``.

    Both bundles are copied into a per-attempt scratch directory under ``work_root`` that belongs to the inspection
    user, and git runs there as that user under the sandbox's limits (never as root). A scratch bare repository gets
    the base history, then the result bundle is verified against it and its branch fetched with every object checked.
    With ``token`` (an own-login token) every object reachable from the result and not from the base is also scanned
    for it. The guest's file is never what the user receives: with ``deliver_to`` a new bundle is built from the
    scratch repository (``refs/heads/<branch>`` and ``^<base>``, so it holds exactly the objects that were scanned and
    names exactly one ref) and written there; ``"delivered"`` in the result gives its size and digest. The guest's
    bundle may name ``HEAD`` (the runner's ``base..HEAD``) or the branch, and nothing else. Returns ``{"head",
    "changed_files", "changed_total", "ahead", "token_found", "delivered"}``. Raises :class:`Broken` on anything it
    cannot establish: the caller then withholds the bundle."""
    sb = sandbox or Sandbox()
    if not BRANCH_RE.fullmatch(branch) or not re.fullmatch(r"[0-9a-f]{40}", base_sha or ""):
        raise Broken("the result names an unexpected branch or base")
    ident = _identity(sb)
    os.makedirs(work_root, mode=0o711, exist_ok=True)
    os.chmod(work_root, 0o711)               # others may enter (to reach their own directory), not list
    scratch = tempfile.mkdtemp(prefix=re.sub(r"[^a-z0-9-]", "", attempt.lower())[:24] + "-", dir=work_root)
    try:
        if ident is not None:
            os.chown(scratch, *ident)
        os.chmod(scratch, 0o700)
        git = _Git(sb, scratch, ident)
        base_copy = git.copy_in(base_bundle, "base.bundle")
        result_copy = git.copy_in(result_bundle, "result.bundle")
        repo = os.path.join(scratch, "r")
        if git(["init", "-q", "--bare", "--template=", repo])[0] != 0:
            raise Broken("git could not make a scratch repository")
        rr = lambda *a, **kw: git(list(a), cwd=repo, **kw)  # noqa: E731  (cwd = the scratch repository)
        rc, _o, _e = rr("-c", "transfer.fsckObjects=true", "fetch", "--quiet", "--no-tags", base_copy, "refs/heads/*:refs/base/*")
        if rc != 0:
            raise Broken("the caller's base history could not be loaded for inspection")
        if rr("bundle", "verify", result_copy)[0] != 0:
            raise Broken("the result bundle does not verify against the base commit")
        wanted = f"refs/heads/{branch}"
        rc, out, _e = rr("bundle", "list-heads", result_copy)
        heads = {}
        for line in out.decode().splitlines():
            if " " in line:
                heads[line.split(" ", 1)[1]] = line.split(" ", 1)[0]
        if rc != 0 or not heads:
            raise Broken("the result bundle has no ref")
        if not set(heads) <= {"HEAD", wanted} or len(set(heads.values())) != 1:
            raise Broken("the result bundle names refs other than the guest's branch")
        source = wanted if wanted in heads else "HEAD"
        rc, _o, err = rr("-c", "transfer.fsckObjects=true", "fetch", "--quiet", "--no-tags", result_copy, f"{source}:refs/result")
        if rc != 0:
            raise Broken("git rejected the result bundle: " + err.decode(errors="replace").strip()[:160])
        rc, out, _e = rr("rev-parse", "--verify", "--quiet", "refs/result^{commit}")
        head = out.decode().strip()
        if rc != 0 or not re.fullmatch(r"[0-9a-f]{40}", head):
            raise Broken("the result branch is not a commit")
        ahead = rr("merge-base", "--is-ancestor", base_sha, head)[0] == 0
        rc, out, _e = rr("diff", "--name-only", "-z", "--no-textconv", "--no-ext-diff", "--no-renames", base_sha, head)
        if rc != 0:
            raise Broken("git could not list the changed files")
        names = [n.decode("utf-8", "replace") for n in out.split(b"\0") if n]
        found = _scan_new_objects(git, rr, scratch, ident, base_sha, token, sb) if token else False
        delivered = None
        if deliver_to and not found:
            # What the user receives is built here from the scanned objects, not the guest's file: extra objects the
            # guest packed (no ref reaches them) and extra refs never arrive.
            if rr("update-ref", wanted, "refs/result")[0] != 0:
                raise Broken("git could not name the result branch")
            built = os.path.join(scratch, "deliver.bundle")
            if rr("bundle", "create", built, wanted, f"^{base_sha}")[0] != 0:
                raise Broken("git could not rebuild the result bundle")
            if rr("bundle", "verify", built)[0] != 0:
                raise Broken("the rebuilt result bundle does not verify")
            delivered = git.copy_out(built, deliver_to)
        return {"head": head, "changed_files": [_printable(n) for n in names[:MAX_CHANGED]], "changed_total": len(names),
                "ahead": ahead, "token_found": found, "delivered": delivered}
    finally:
        shutil.rmtree(scratch, ignore_errors=True)


class _Found(Exception):
    pass


def _scan_new_objects(git, rr, scratch: str, ident, base_sha: str, token: str, sb: Sandbox) -> bool:
    """True when an object reachable from ``refs/result`` and not from the base (commits, trees, file names and file
    contents) holds the token. Those are exactly the objects of the bundle motel delivers (rebuilt from this repository
    with the same revision range), and nothing else reaches the user: objects the guest packed that no ref reaches,
    and other refs, are not in the delivered file. The objects are read by git, so a token inside a compressed blob is
    found where a byte scan of the guest's bundle could not see it."""
    rc, out, _e = rr("rev-list", "--objects", "refs/result", "--not", base_sha, cap=sb.listing_bytes)
    if rc != 0:
        raise Broken("git could not list the objects of the result")
    ids = b"".join(line[:40] + b"\n" for line in out.splitlines() if re.fullmatch(rb"[0-9a-f]{40}( .*)?", line))
    listing = os.path.join(scratch, "objects.txt")
    fd = os.open(listing, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        os.write(fd, ids)
        if ident is not None:
            os.fchown(fd, *ident)
    finally:
        os.close(fd)
    raw, state = token.encode(), {"keep": b"", "bytes": 0}

    def sink(chunk: bytes) -> None:
        window = state["keep"] + chunk
        if raw in window:
            raise _Found
        state["keep"] = window[-(len(raw) - 1):] if len(raw) > 1 else b""

    try:
        rc, _o, _e = rr("cat-file", "--batch", stdin_path=listing, sink=sink, cap=sb.scan_bytes)
    except _Found:
        return True
    except Broken as exc:
        if "printed too much" in str(exc):
            raise Broken("the result holds too much new data to scan for the own-login token") from None
        raise
    if rc != 0:
        raise Broken("git could not read the objects of the result")
    return False


def _printable(text: str) -> str:
    return "".join(c for c in text if c.isprintable())[:512]


def safe_remove(path: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(path)
