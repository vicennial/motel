"""Remove a credential from data that is on its way to a guest, including across chunk boundaries."""

from __future__ import annotations

import json

REPLACEMENT = b"[redacted]"


def needles(secret: str) -> list[bytes]:
    """The byte forms an echo of ``secret`` could take: raw and JSON-escaped."""
    forms = {secret.encode("utf-8", "replace"), json.dumps(secret)[1:-1].encode("utf-8", "replace")}
    return sorted((f for f in forms if f), key=len, reverse=True)


class StreamRedactor:
    """Replace every occurrence of the secret in a byte stream.

    A tail of the data that could still turn out to be the start of the secret is held back until the next chunk
    decides it. Output is therefore delayed only while the end of a chunk really looks like the beginning of the key.
    """

    def __init__(self, secret: str):
        self._needles = needles(secret)
        self._pending = b""
        self.hits = 0

    def feed(self, data: bytes) -> bytes:
        buf = self._pending + data
        for needle in self._needles:
            if needle in buf:
                self.hits += buf.count(needle)
                buf = buf.replace(needle, REPLACEMENT)
        hold = 0
        for needle in self._needles:
            for k in range(min(len(needle) - 1, len(buf)), hold, -1):
                if needle.startswith(buf[-k:]):
                    hold = k
                    break
        if hold:
            self._pending, buf = buf[-hold:], buf[:-hold]
        else:
            self._pending = b""
        return buf

    def finish(self) -> bytes:
        rest, self._pending = self._pending, b""
        return rest

    def scrub_text(self, text: str) -> str:
        """Redact a header value (a short string, no streaming concerns)."""
        data = text.encode("utf-8", "replace")
        out = StreamRedactor.__new__(StreamRedactor)
        out._needles, out._pending, out.hits = self._needles, b"", 0
        cleaned = out.feed(data) + out.finish()
        self.hits += out.hits
        return cleaned.decode("utf-8", "replace")
