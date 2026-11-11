"""Controller-side channel to the guest's restricted ``cabinctl`` account (forced command), and activity rules."""

from __future__ import annotations

import ipaddress
import json
import math
import os
import re
import select
import signal
import socket
import subprocess
import time

from motel.cabins.errors import GuestUnknown
from motel.cabins.tailnet import TAILNET_V4

SAMPLE_MAX_AGE_S = 180
MAX_REPLY = 65536          # hard cap on guest output read by the root helper
SYSTEM_USER = "motel-cabin"
DROP_TOOLS = ("/usr/bin/setpriv", "/bin/setpriv", "/usr/sbin/runuser", "/sbin/runuser", "/usr/bin/runuser")
WORD_RE = re.compile(r"^[a-z0-9_.:-]{1,48}$")


STDERR_CAP = 2048          # bytes of the ssh client's stderr kept (only one line is ever shown)
DETAIL_MAX = 120
# What looks like a secret: a run of 32 or more key-alphabet characters that mixes letters and digits (a key, a base64 blob, a
# token), any run of 64 or more, or a token with a well-known key prefix. Ordinary words, unit names and ssh error names
# ("cabin-guest-firewall", "kex_exchange_identification") are shorter or have no digits and stay readable.
_LONG_TOKEN = re.compile(r"[A-Za-z0-9+/=_-]{32,}")
_KEY_PREFIX = re.compile(r"\b(?:sk-[A-Za-z0-9_-]{8,}|gh[pousr]_[A-Za-z0-9]{8,}|github_pat_[A-Za-z0-9_]{8,}|AKIA[0-9A-Z]{12,}"
                         r"|xox[a-z]-[A-Za-z0-9-]{8,}|AAAA[A-Za-z0-9+/=]{12,}|mt_[A-Za-z0-9_-]{8,}|tskey-[A-Za-z0-9-]{8,})")
_PATH = re.compile(r"/[^\s:'\"]*/[^\s:'\"]*")
_NOISE = re.compile(r"^(@|Warning: Permanently added)")       # host-key warning boxes and the known-hosts notice


def _redact_token(match) -> str:
    token = match.group(0)
    secret = len(token) >= 64 or (any(c.isdigit() for c in token) and any(c.isalpha() for c in token))
    return "<redacted>" if secret else token


def safe_detail(raw, limit: int = DETAIL_MAX) -> str:
    """The last informative line of a stderr text (ssh prints its warnings first and the reason last), fit to be stored and
    shown: printable ASCII only, paths and secret-looking strings replaced, at most ``limit`` characters. Lines that are only
    a warning box or the known-hosts notice are skipped unless nothing else is left. ``""`` when there is nothing to show."""
    if isinstance(raw, (bytes, bytearray)):
        raw = bytes(raw).decode("utf-8", "replace")
    lines = ["".join(c if " " <= c <= "~" else "?" for c in line).strip() for line in str(raw or "").splitlines()]
    lines = [line for line in lines if line]
    chosen = [line for line in lines if not _NOISE.match(line)] or lines
    if not chosen:
        return ""
    line = _PATH.sub("<path>", chosen[-1])
    line = _KEY_PREFIX.sub("<redacted>", line)
    line = _LONG_TOKEN.sub(_redact_token, line)
    return line[:limit]


def capture_runner(argv, timeout, cap=MAX_REPLY, err_cap=STDERR_CAP):
    """Run a guest query with a hard byte cap on stdout (kill on overflow) and a small bounded copy of stderr.

    Never reads unbounded guest output: the guest is untrusted and this process may be root. Returns ``(rc, stdout, stderr)``.
    """
    proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                            env={"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}, close_fds=True,
                            start_new_session=True)

    def kill():
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except (ProcessLookupError, PermissionError):
            proc.kill()
    deadline = time.monotonic() + timeout
    chunks, errs, total, err_total = [], [], 0, 0
    try:
        out_fd, err_fd = proc.stdout.fileno(), proc.stderr.fileno()
        open_fds = {out_fd, err_fd}
        while out_fd in open_fds:
            left = deadline - time.monotonic()
            if left <= 0:
                kill()
                raise subprocess.TimeoutExpired(argv, timeout)
            ready, _w, _x = select.select(sorted(open_fds), [], [], min(left, 1.0))
            for fd in ready:
                data = os.read(fd, 8192)
                if not data:
                    open_fds.discard(fd)
                    continue
                if fd == err_fd:                       # keep the first bytes, drain and drop the rest
                    if err_total < err_cap:
                        errs.append(data[:err_cap - err_total])
                    err_total += len(data)
                    continue
                total += len(data)
                if total > cap:
                    kill()
                    raise GuestUnknown("guest reply too large")
                chunks.append(data)
        rc = proc.wait(timeout=max(0.1, deadline - time.monotonic()))
    finally:
        if proc.poll() is None:
            kill()
            proc.wait()
        proc.stdout.close()
        proc.stderr.close()
    return rc, b"".join(chunks), b"".join(errs)


def default_runner(argv, timeout, cap=MAX_REPLY):
    """Like :func:`capture_runner` without the stderr: ``(rc, stdout)``."""
    rc, out, _err = capture_runner(argv, timeout, cap)
    return rc, out


def probe_tcp(ip: str, port: int = 22, timeout: float = 4.0, connect=socket.create_connection) -> str:
    """What a plain TCP connect from this host to the cabin's ssh port sees: ``open (SSH-2.0-...)``, ``open (no ssh banner)``,
    ``refused``, ``timeout`` or ``error <errno>``. Tells a firewall that drops packets (timeout) from an sshd that is down
    (refused) from one that answers but will not talk to us (open)."""
    try:
        with connect((ip, port), timeout=timeout) as sock:
            sock.settimeout(timeout)
            try:
                banner = sock.recv(64)
            except OSError:
                banner = b""
    except ConnectionRefusedError:
        return "refused"
    except TimeoutError:
        return "timeout"
    except OSError as exc:
        return f"error {exc.errno}" if exc.errno else "error"
    text = safe_detail(banner.split(b"\n", 1)[0], 60)
    return f"open ({text})" if text.startswith("SSH-") else "open (no ssh banner)"


def drop_prefix(run_as, exists=os.path.exists) -> list:
    """The command prefix that runs the guest ssh client as the unprivileged system user.

    ``run_as`` is ``(name, uid, gid)``. Prefers setpriv (also sets no-new-privs); falls back to runuser. With
    neither installed the guest channel is refused rather than run as root.
    """
    name, uid, gid = run_as
    if uid is None or gid is None:
        raise GuestUnknown(f"the {name} system user does not exist; the guest channel is refused")
    for tool in DROP_TOOLS:
        if not exists(tool):
            continue
        if tool.endswith("setpriv"):
            return [tool, "--reuid", str(uid), "--regid", str(gid), "--clear-groups", "--no-new-privs", "--"]
        return [tool, "-u", name, "--"]
    raise GuestUnknown("neither setpriv nor runuser is installed; the guest channel is refused")


def _num(value, low=0.0, high=1e12):
    """A finite non-bool number in range, else None."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return value if low <= value <= high else None


def _words(value, limit=16):
    """A list of short lowercase tokens, else None."""
    if not isinstance(value, list) or len(value) > limit:
        return None
    if not all(isinstance(x, str) and WORD_RE.fullmatch(x) for x in value):
        return None
    return value


def ssh_options(known_hosts: str, alias: str) -> list:
    """Options shared by every ssh the desk and the caller run against a cabin: pinned host key, no agent."""
    return ["-o", "IdentitiesOnly=yes", "-o", "IdentityAgent=none",
            "-o", f"UserKnownHostsFile={known_hosts}", "-o", "GlobalKnownHostsFile=/dev/null",
            "-o", "StrictHostKeyChecking=yes", "-o", f"HostKeyAlias={alias}", "-o", "UpdateHostKeys=no",
            "-o", "BatchMode=yes", "-o", "ForwardAgent=no", "-o", "ForwardX11=no",
            "-o", "ClearAllForwardings=yes", "-o", "PasswordAuthentication=no",
            "-o", "KbdInteractiveAuthentication=no", "-o", "AddressFamily=inet"]


class GuestChannel:
    def __init__(self, keys_dir, runner=capture_runner, run_as=None, exists=os.path.exists, layout_check=None):
        """``layout_check`` returns None or a reason when the key tree is unusable; the channel is then
        refused (guest reads as unknown, so cabins fall back to TTL-only cleanup)."""
        self.keys_dir, self.runner, self.run_as, self.exists = keys_dir, runner, run_as, exists
        self.layout_check = layout_check

    def unavailable_reason(self):
        """Why the channel cannot run (None when it can). Checked as a checkin prerequisite."""
        if self.layout_check is not None:
            problem = self.layout_check()
            if problem:
                return problem
        if self.run_as is None:
            return None
        try:
            drop_prefix(self.run_as, self.exists)
        except GuestUnknown as exc:
            return str(exc)
        return None

    def paths(self, lease_id):
        base = os.path.join(self.keys_dir, lease_id)
        return os.path.join(base, "controller_ed25519"), os.path.join(base, "known_hosts")

    @staticmethod
    def address(rec) -> str:
        access = rec.get("access") or {}
        ip = access.get("address")
        try:
            parsed = ipaddress.ip_address(ip)
        except ValueError:
            raise GuestUnknown("invalid cabin address") from None
        except TypeError:
            raise GuestUnknown("no cabin address recorded") from None
        if parsed.version != 4:
            raise GuestUnknown("cabin address is not IPv4")
        if access.get("mode") == "tailscale":
            if parsed not in TAILNET_V4:
                raise GuestUnknown("invalid tailnet address")
        elif parsed.is_unspecified or parsed.is_loopback or parsed.is_multicast or parsed.is_link_local:
            raise GuestUnknown("unusable cabin address")
        return ip

    def call(self, rec, *words, timeout=60) -> dict:
        if self.layout_check is not None and (problem := self.layout_check()):
            raise GuestUnknown(f"key layout unusable: {problem}")
        ip = self.address(rec)
        key, known = self.paths(rec["lease_id"])
        argv = (["/usr/bin/ssh", "-F", "/dev/null", "-i", key, *ssh_options(known, rec["name"]),
                 "-o", "ConnectTimeout=20", "-o", "ServerAliveInterval=10", "-o", "ServerAliveCountMax=3",
                 "-p", "22", "-l", "cabinctl", "--", ip, " ".join(words)])
        if self.run_as is not None:
            argv = drop_prefix(self.run_as, self.exists) + argv
        try:
            result = self.runner(argv, timeout)
            rc, out, err = result if len(result) > 2 else (*result, b"")
        except subprocess.TimeoutExpired:
            raise GuestUnknown(f"guest query timed out after {int(timeout)}s", detail="timeout") from None
        except (OSError, subprocess.SubprocessError):
            raise GuestUnknown("guest query failed") from None
        if rc != 0 or not isinstance(out, (bytes, bytearray)) or not out or len(out) > MAX_REPLY:
            detail = safe_detail(err)
            raise GuestUnknown(f"guest query exit {rc}" + (f": {detail}" if detail else ""), rc=rc, detail=detail)
        try:
            doc = json.loads(bytes(out).decode("utf-8"))
        except (ValueError, RecursionError, MemoryError):      # includes decode errors and deep nesting
            raise GuestUnknown("guest reply unreadable") from None
        if not isinstance(doc, dict) or doc.get("schema") != "motel-guest/v1":
            raise GuestUnknown("guest reply has wrong schema")
        return doc


def validate_activity(doc):
    """Strictly typed copy of a guest activity reply, or None when any field has the wrong type."""
    if not isinstance(doc, dict):
        return None
    age = _num(doc.get("sample_age_s"))
    if age is None:
        return None
    active = doc.get("active")
    reasons, unknown = _words(doc.get("reasons", [])), _words(doc.get("unknown", []))
    if not isinstance(active, bool) or reasons is None or unknown is None:
        return None
    idle = _num(doc.get("idle_seconds"))
    if idle is None and doc.get("idle_seconds") is not None:
        return None
    out = {"sample_age_s": age, "active": active, "reasons": list(reasons), "unknown": list(unknown),
           "idle_seconds": idle}
    if "sealed_at" in doc:
        sealed = _num(doc["sealed_at"])
        if sealed is None:
            return None
        out["sealed_at"] = sealed
    return out


def classify_activity(doc, idle_after_s: float = 1800):
    """('active'|'idle'|'unknown', detail). Missing, stale or malformed evidence is unknown, never idle."""
    doc = validate_activity(doc)
    if doc is None:
        return "unknown", "guest activity reply malformed"
    age, idle = doc["sample_age_s"], doc["idle_seconds"]
    if age > SAMPLE_MAX_AGE_S:
        return "unknown", "guest activity sample missing or stale"
    if doc["active"] is True:
        return "active", ",".join(doc["reasons"]) or "active"
    if doc["unknown"]:
        return "unknown", "guest could not verify all activity sources"
    if idle is None:
        return "unknown", "guest idle time missing"
    if idle >= idle_after_s:
        return "idle", f"idle {int(idle // 60)} min"
    return "active", f"recent activity {int(idle // 60)} min ago"


READY_DISK = ("none", "mounted", "failed")


def _valid_boot(doc):
    """The guest's progress report (stage, cloud-init verdict, last line printed, up to three error lines), cleaned; ``{}`` when absent or hostile.
    Guest-supplied text: words only for the first two, printable ASCII and at most 120 characters for the last."""
    if not isinstance(doc, dict):
        return {}
    out = {}
    for key in ("stage", "cloud_init"):
        value = doc.get(key)
        if isinstance(value, str) and WORD_RE.fullmatch(value):
            out[key] = value
    if isinstance(doc.get("last"), str):
        out["last"] = safe_detail(doc["last"])
    errors = doc.get("errors")
    if isinstance(errors, list) and errors and all(isinstance(x, str) for x in errors[-3:]):
        out["errors"] = [safe_detail(x, 160) for x in errors[-3:]]
    return out


def validate_ready(doc):
    """Strictly typed copy of a readiness reply. Raises GuestUnknown on any malformed field."""
    bad = GuestUnknown("guest readiness reply malformed")
    if not isinstance(doc, dict):
        raise bad
    boot = doc.get("bootstrap")
    if not (isinstance(boot, str) and WORD_RE.fullmatch(boot)):
        raise bad
    if boot != "done":
        return {"bootstrap": boot, "boot": _valid_boot(doc.get("boot"))}
    tail = doc.get("tailnet_ip")
    if tail is not None and not (isinstance(tail, str) and len(tail) <= 45):
        raise bad
    listeners = doc.get("tcp_listeners")
    if not isinstance(listeners, list) or len(listeners) > 256:
        raise bad
    clean = []
    for item in listeners:
        if not isinstance(item, dict) or not isinstance(item.get("addr"), str) or len(item["addr"]) > 45 \
                or _num(item.get("port"), 0, 65535) is None or isinstance(item["port"], float):
            raise bad
        clean.append({"addr": item["addr"], "port": item["port"]})
    for key in ("nft", "gate"):
        if not (isinstance(doc.get(key), str) and WORD_RE.fullmatch(doc[key])):
            raise bad
    if not isinstance(doc.get("forwarding_disabled"), bool):
        raise bad
    disk = doc.get("disk", "none")
    if disk not in READY_DISK:
        raise bad
    forward_port = doc.get("forward_port")
    if forward_port is not None and (isinstance(forward_port, bool) or not isinstance(forward_port, int)
                                     or not 1024 <= forward_port <= 65535):
        raise bad
    return {"bootstrap": "done", "tailnet_ip": tail, "tcp_listeners": clean, "nft": doc["nft"],
            "gate": doc["gate"], "forwarding_disabled": doc["forwarding_disabled"], "disk": disk,
            "forward_port": forward_port}


def validate_seal(doc):
    """('sealed' bool, verified idle seconds or None, reason str). Malformed replies are 'not sealed'."""
    if not isinstance(doc, dict):
        return False, None, "malformed reply"
    verified = _num(doc.get("verified_idle_seconds"))
    reason = doc.get("reason")
    reason = re.sub(r"[^A-Za-z0-9 ,.:;_-]", "", reason)[:200] if isinstance(reason, str) else "refused"
    return doc.get("sealed") is True, verified, reason or "refused"
