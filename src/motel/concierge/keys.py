"""Real upstream keys, read from ``key_file`` at use time.

A key is cached with the file's identity (device, inode, size, mtime) so rotating a key file takes effect without a
restart and without reading the file on every request. The key never leaves this module except as the string handed to
the proxy for header injection; it is not logged, not put in an exception message and not written to the ledger.
"""

from __future__ import annotations

import os
import re
import stat
import threading

KEY_RE = re.compile(r"^[\x21-\x7e]{8,1024}$")  # printable ASCII, no space or control characters: safe in a header
MAX_KEY_FILE = 8192


class KeyUnavailable(Exception):
    """The key file is missing, unreadable or does not hold a plausible key. The message never contains key material."""


class KeyStore:
    def __init__(self, bindings: dict):
        self._files = {name: b.key_file for name, b in bindings.items()}
        self._cache: dict = {}
        self._lock = threading.Lock()

    def get(self, binding: str) -> str:
        path = self._files[binding]
        try:
            fd = os.open(path, os.O_RDONLY | getattr(os, "O_CLOEXEC", 0))
        except OSError as exc:
            raise KeyUnavailable(f"cannot open the key file for binding {binding!r} ({exc.strerror})") from None
        try:
            st = os.fstat(fd)
            ident = (st.st_dev, st.st_ino, st.st_size, st.st_mtime_ns)
            with self._lock:
                cached = self._cache.get(binding)
                if cached and cached[0] == ident:
                    return cached[1]
            if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_KEY_FILE:
                raise KeyUnavailable(f"the key file for binding {binding!r} is not a regular file of plausible size")
            raw = os.read(fd, MAX_KEY_FILE + 1)
        except OSError as exc:
            raise KeyUnavailable(f"cannot read the key file for binding {binding!r} ({exc.strerror})") from None
        finally:
            os.close(fd)
        key = raw.decode("ascii", "replace").strip()
        if not KEY_RE.fullmatch(key):
            raise KeyUnavailable(f"the key file for binding {binding!r} does not hold a usable key (one printable token, 8-1024 characters)")
        with self._lock:
            self._cache[binding] = (ident, key)
        return key

    def available(self, binding: str) -> bool:
        try:
            self.get(binding)
        except KeyUnavailable:
            return False
        return True
