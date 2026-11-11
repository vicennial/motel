"""Helpers shared by the DigitalOcean, Vultr and Linode providers (standard library only).

* :class:`ApiClient` is the HTTP layer with the same safety rules as the Hetzner provider: the token goes only
  into the Authorization header of requests to the API host, redirects are refused, reads retry on 429/5xx and
  network errors, writes retry only on 429 (the request was rejected), and a write whose response may have been
  lost raises ``ProviderError(indeterminate=True)``.
* The label codecs map motel's key/value labels onto clouds that have no key/value labels: :func:`encode_tags`
  for providers with a list of tag strings, and :func:`encode_label` for a provider with only a free-text label.
  Both are strict: a value that does not fit is refused, never truncated, and anything that is not in the exact
  canonical form is ignored, so a look-alike tag or label can never be mistaken for a motel label.
"""

from __future__ import annotations

import json
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from decimal import Decimal

from motel import __version__
from motel.errors import NotFound, ProviderError

MAX_PAGES = 200


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):  # noqa: D401
        return None


def default_open(req, timeout):
    return urllib.request.build_opener(NoRedirect).open(req, timeout=timeout)


def fetch_text(url: str, *, method: str = "GET", headers: dict | None = None, timeout: float = 5.0) -> str:
    """Read a small text document from a link-local metadata service (no redirects, no proxy)."""
    req = urllib.request.Request(url, method=method, headers=headers or {})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}), NoRedirect)
    with opener.open(req, timeout=timeout) as resp:
        return resp.read(1 << 20).decode().strip()


class ApiClient:
    """JSON over HTTPS with bearer authentication. ``describe(raw_body) -> str`` turns an error body into text."""

    def __init__(self, provider: str, token: str, base: str, describe, *, timeout: float = 30.0, sleep=time.sleep,
                 opener=default_open):
        self.provider, self._token, self.base, self._describe = provider, token, base.rstrip("/"), describe
        self.timeout, self.sleep, self._open = timeout, sleep, opener

    def request(self, method: str, path: str, body=None, *, headers: dict | None = None) -> dict:
        """GET retries on 429/5xx/network errors. Writes retry only on 429; a lost write is indeterminate."""
        data = None if body is None else json.dumps(body).encode()
        is_read = method == "GET"
        delay = 2.0
        for attempt in range(5):
            req = urllib.request.Request(self.base + path, data=data, method=method, headers={
                "Authorization": f"Bearer {self._token}", "Content-Type": "application/json",
                "Accept": "application/json", "User-Agent": f"motel/{__version__}", **(headers or {})})
            try:
                with self._open(req, self.timeout) as resp:
                    raw = resp.read()
                    doc = json.loads(raw) if raw else {}
                    if not isinstance(doc, dict):
                        raise ValueError("the response is not a JSON object")
                    return doc
            except urllib.error.HTTPError as exc:
                raw = exc.read()
                retry_after = exc.headers.get("Retry-After") if exc.headers else None
                exc.close()
                transient = exc.code in (429, 500, 502, 503, 504)
                if transient and attempt < 4 and (is_read or exc.code == 429):
                    self.sleep(max(delay, min(_seconds(retry_after), 60.0)))
                    delay *= 2
                    continue
                if exc.code == 404:
                    raise NotFound(f"{method} {path}: not found") from exc
                message = f"{self.provider} API {method} {path}: HTTP {exc.code} {self._describe(raw)}"
                raise ProviderError(message.strip(), indeterminate=(not is_read and exc.code >= 500)) from exc
            except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
                if is_read and attempt < 4:
                    self.sleep(delay)
                    delay *= 2
                    continue
                raise ProviderError(f"{self.provider} API {method} {path}: {exc}", indeterminate=not is_read) from exc
        raise ProviderError(f"{self.provider} API {method} {path}: retries exhausted", indeterminate=not is_read)


def _seconds(text) -> float:
    try:
        return max(0.0, float(text))
    except (TypeError, ValueError):
        return 0.0


def short(text, limit: int = 200) -> str:
    """Error text from an API body, bounded and on one line."""
    return " ".join(str(text).split())[:limit]


def to_eur(usd: Decimal, eur_per_usd: Decimal) -> Decimal:
    return (Decimal(usd) * Decimal(eur_per_usd)).quantize(Decimal("0.0001"))


# --------------------------------------------------------------------------------------------- tag codec
TAG_PREFIX = "m_"
KEY_RE = re.compile(r"^[a-z0-9]+(?:-[a-z0-9]+)*$")
_TAG_SAFE = re.compile(r"[a-z0-9-]")
_ESCAPE = re.compile(r"_([0-9a-f]{2})")


def _escape(value: str) -> str:
    return "".join(c if _TAG_SAFE.fullmatch(c) else "".join(f"_{b:02x}" for b in c.encode()) for c in value)


def _unescape(text: str) -> str:
    probe = _ESCAPE.sub("", text)
    if "_" in probe:
        raise ValueError("stray underscore")
    return re.sub(r"(?:_[0-9a-f]{2})+", lambda m: bytes.fromhex(m.group().replace("_", "")).decode(), text)


def encode_tags(labels: dict, max_len: int) -> list:
    """Encode labels as tag strings ``m_<key>_<value>`` using only ``a-z 0-9 - _``.

    The key is ``[a-z0-9-]`` and so never holds ``_``; the value keeps ``a-z 0-9 -`` and writes every other byte
    (including ``_`` and upper case) as ``_xx`` in lower-case hex. The first ``_`` after the prefix is therefore
    always the separator, and decoding is unambiguous. A tag longer than ``max_len`` is an error, never truncated.
    """
    out = []
    for key, value in labels.items():
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise ProviderError(f"label key {key!r} cannot be stored as a tag (use lower-case letters, digits and -)")
        tag = f"{TAG_PREFIX}{key}_{_escape(str(value))}"
        if len(tag) > max_len:
            raise ProviderError(f"label {key!r} does not fit in a {max_len}-character tag ({len(tag)} needed)")
        out.append(tag)
    return out


def decode_tags(tags) -> dict:
    """Labels from a list of tags. Foreign or non-canonical tags are ignored; a key given two values is dropped."""
    found: dict = {}
    conflicted: set = set()
    for tag in tags or []:
        if not isinstance(tag, str) or not tag.startswith(TAG_PREFIX):
            continue
        key, sep, rest = tag[len(TAG_PREFIX):].partition("_")
        if not sep or not KEY_RE.fullmatch(key):
            continue
        try:
            value = _unescape(rest)
        except (ValueError, UnicodeDecodeError):
            continue
        if f"{TAG_PREFIX}{key}_{_escape(value)}" != tag:
            continue  # not the canonical spelling of this label
        if key in found and found[key] != value:
            conflicted.add(key)
        found[key] = value
    return {k: v for k, v in found.items() if k not in conflicted}


# ------------------------------------------------------------------------------------------- label codec
NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
LABEL_SEP = "~"
_LABEL_SAFE = re.compile(r"[A-Za-z0-9._-]")
_PERCENT = re.compile(r"%([0-9a-f]{2})")


def _pct_escape(value: str) -> str:
    return "".join(c if _LABEL_SAFE.fullmatch(c) else "".join(f"%{b:02x}" for b in c.encode()) for c in value)


def _pct_unescape(text: str) -> str:
    if "%" in _PERCENT.sub("", text):
        raise ValueError("stray percent sign")
    return re.sub(r"(?:%[0-9a-f]{2})+", lambda m: bytes.fromhex(m.group().replace("%", "")).decode(), text)


def _render_label(name: str, labels: dict) -> str:
    return LABEL_SEP.join([name] + [f"{key}={_pct_escape(str(value))}" for key, value in labels.items()])


def encode_label(name: str, labels: dict, max_len: int) -> str:
    """One free-text label ``<name>~<key>=<value>~...`` (Vultr has no tags). The name stays first and readable.

    Values are percent-encoded outside ``A-Za-z0-9._-`` (so ``~``, ``=`` and ``%`` never appear raw), keys are
    ``[a-z0-9-]``. A label longer than ``max_len`` is an error, never truncated.
    """
    if not NAME_RE.fullmatch(name):
        raise ProviderError(f"volume name {name!r} cannot be stored in a label (use letters, digits . _ -)")
    for key in labels:
        if not isinstance(key, str) or not KEY_RE.fullmatch(key):
            raise ProviderError(f"label key {key!r} cannot be stored in a label (use lower-case letters, digits and -)")
    text = _render_label(name, labels)
    if len(text) > max_len:
        raise ProviderError(f"the labels do not fit in a {max_len}-character label ({len(text)} needed)")
    return text


def decode_label(text: str) -> tuple:
    """``(name, labels)`` from a label. Anything not in the exact canonical form yields no labels."""
    text = str(text or "")
    name, _, rest = text.partition(LABEL_SEP)
    if not rest:
        return text, {}
    labels: dict = {}
    conflicted: set = set()
    try:
        for part in rest.split(LABEL_SEP):
            key, sep, raw = part.partition("=")
            if not sep or not KEY_RE.fullmatch(key):
                return name, {}
            value = _pct_unescape(raw)
            if key in labels and labels[key] != value:
                conflicted.add(key)
            labels[key] = value
    except (ValueError, UnicodeDecodeError):
        return name, {}
    if not NAME_RE.fullmatch(name) or _render_label(name, labels) != text:
        return name, {}
    return name, {k: v for k, v in labels.items() if k not in conflicted}


def matches(volume_labels: dict, wanted: dict) -> bool:
    return all(volume_labels.get(k) == str(v) for k, v in wanted.items())
