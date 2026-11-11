"""The opt-in "own-login" auth mode: the operator's own Claude token, supplied by the operator.

motel never reads ``~/.claude``, a credential store or a keychain, and never runs ``claude setup-token``. The
operator creates a token on their own machine and hands it to ``sudo motel-desk tour own-login set --stdin``,
which writes it ``root:root 0600``. The supervisor reads that file only to put the token on the ssh stdin of one
runner. The token is never in ``card.json``, argv, the ssh environment, a file on the guest, state, logs or
results. It cannot be revoked per job, so own-login ttls are short.
"""

from __future__ import annotations

import contextlib
import os
import re
import secrets
import stat

from motel.cabins.util import iso
from motel.tours.errors import NotSetUp, Refused, Usage

TOKEN_RE = re.compile(r"^[A-Za-z0-9._~+/=-]{20,2048}$")
MAX_BYTES = 4096
WARNING = ("own-login: your Claude token is placed on the guest, has no per-job budget, is account-wide, and its use is "
           "your responsibility under your plan's terms (docs/tours.md)")


def parse_token(data: bytes) -> str:
    """The token from file or stdin contents: one value, one optional trailing newline, plain characters."""
    if len(data) > MAX_BYTES:
        raise Usage("that is not a token: it is larger than 4 KiB", "claude setup-token (on your own machine)")
    text = data.decode("utf-8", "replace")
    if text.endswith("\n"):
        text = text[:-1]
    if not TOKEN_RE.fullmatch(text):
        raise Usage("that does not look like a token (20-2048 letters, digits and . _ ~ + / = -, on one line)",
                    "claude setup-token (on your own machine)")
    return text


def read_token(path: str) -> str:
    """Read the stored token. The file must be a regular file owned by the desk's user, private, and small."""
    try:
        fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_CLOEXEC)
    except FileNotFoundError:
        raise NotSetUp("no own-login token is stored", "sudo motel-desk tour own-login set --stdin") from None
    except OSError:
        raise NotSetUp("the own-login token file cannot be opened", "sudo motel-desk tour own-login status") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_uid != os.geteuid() or st.st_mode & 0o077 or st.st_size > MAX_BYTES:
            raise NotSetUp("the own-login token file must be a private regular file owned by root",
                           "sudo motel-desk tour own-login set --stdin")
        with os.fdopen(fd, "rb", closefd=False) as handle:
            return parse_token(handle.read(MAX_BYTES + 1))
    finally:
        os.close(fd)


def set_token(path: str, data: bytes) -> dict:
    token = parse_token(data)
    folder = os.path.dirname(path)
    os.makedirs(folder, mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    tmp = os.path.join(folder, f".{os.path.basename(path)}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW | os.O_CLOEXEC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="ascii") as handle:
            handle.write(token + "\n")
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp, path)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise
    return status(path)


def status(path: str) -> dict:
    """Presence and modification time only. Never the token, its length or any part of it."""
    try:
        st = os.lstat(path)
    except OSError:
        return {"present": False, "modified": None}
    if not stat.S_ISREG(st.st_mode):
        return {"present": False, "modified": None, "problem": "not a regular file"}
    from datetime import UTC, datetime
    return {"present": True, "modified": iso(datetime.fromtimestamp(st.st_mtime, UTC))}


def clear(path: str) -> dict:
    try:
        os.unlink(path)
    except FileNotFoundError:
        pass
    except OSError:
        raise Refused("the own-login token file could not be removed", "sudo motel-desk tour own-login status") from None
    return status(path)


MIN_PARTIAL = 6


def redact(data: bytes, token: str | None, *, tail: bool = False) -> bytes:
    """Replace the token with ``[redacted]``. With ``tail`` the data may have been cut off at a size limit, so a
    leading part of the token at the very end is replaced too."""
    if not token:
        return data
    raw = token.encode()
    data = data.replace(raw, b"[redacted]")
    if tail:
        for size in range(min(len(raw) - 1, len(data)), MIN_PARTIAL - 1, -1):
            if data.endswith(raw[:size]):
                return data[:-size] + b"[redacted]"
    return data


def scan(chunks, token: str, found: list):
    """Pass chunks through unchanged and append True to ``found`` if the token appears anywhere, also across chunks."""
    raw, keep = token.encode(), b""
    for chunk in chunks:
        window = keep + chunk
        if raw in window:
            found.append(True)
        keep = window[-(len(raw) - 1):] if len(raw) > 1 else b""
        yield chunk


