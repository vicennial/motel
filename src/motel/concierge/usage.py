"""Token usage from upstream responses: plain JSON bodies and server-sent-event streams.

Two wire formats are understood, selected per binding (``format``):

- ``anthropic``: ``usage.input_tokens`` / ``output_tokens`` / ``cache_read_input_tokens`` / ``cache_creation_input_tokens``
  (and the ``cache_creation`` 5m/1h split). Streams carry the input side in ``message_start`` and the cumulative output
  in ``message_delta``.
- ``openai``: chat completions ``prompt_tokens`` / ``completion_tokens`` / ``prompt_tokens_details.cached_tokens`` and the
  responses API ``input_tokens`` / ``output_tokens`` / ``input_tokens_details.cached_tokens`` (also as ``response.usage``
  in the final stream event).

Both are normalised to :class:`Usage`, where ``input_tokens`` never includes cached tokens. Everything here treats the
upstream as untrusted input: values are clamped, types are checked, nothing raises on bad data.
"""

from __future__ import annotations

import json
from dataclasses import dataclass

MAX_COUNT = 10**10
MAX_LINE = 1 << 20  # an SSE line longer than this is skipped (and the usage it carried is lost)
WEB_SEARCH_USD = 0.01  # per request, Anthropic's published server-tool price; counted when the response reports it


def _count(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int | float):
        return 0
    if value != value or value < 0:  # NaN or negative
        return 0
    return int(min(value, MAX_COUNT))


@dataclass
class Usage:
    input_tokens: int = 0  # uncached input
    output_tokens: int = 0
    cache_read: int = 0
    cache_write_5m: int = 0
    cache_write_1h: int = 0
    extra_usd: float = 0.0
    model: str | None = None
    speed: str | None = None  # "fast" when the response says it ran in fast mode
    saw_input: bool = False  # the input side is known (anthropic message_start, or any complete usage)
    final: bool = False  # the output total is final

    @property
    def total_input(self) -> int:
        return self.input_tokens + self.cache_read + self.cache_write_5m + self.cache_write_1h

    def merge(self, other: Usage) -> None:
        """Later values replace earlier ones for every field the later object carried."""
        for name in ("input_tokens", "output_tokens", "cache_read", "cache_write_5m", "cache_write_1h"):
            if getattr(other, name) or other.saw_input:
                setattr(self, name, getattr(other, name))
        if other.extra_usd:
            self.extra_usd = other.extra_usd
        self.model = other.model or self.model
        self.speed = other.speed or self.speed
        self.saw_input = self.saw_input or other.saw_input
        self.final = self.final or other.final


def from_usage_dict(fmt: str, usage, model=None) -> Usage | None:
    """Normalise one ``usage`` object; None when it is not a recognisable usage object."""
    if not isinstance(usage, dict):
        return None
    result = Usage(model=model if isinstance(model, str) else None)
    if fmt == "anthropic":
        if "input_tokens" not in usage and "output_tokens" not in usage:
            return None
        result.input_tokens = _count(usage.get("input_tokens"))
        result.output_tokens = _count(usage.get("output_tokens"))
        result.cache_read = _count(usage.get("cache_read_input_tokens"))
        split = usage.get("cache_creation")
        if isinstance(split, dict) and split:
            result.cache_write_5m = _count(split.get("ephemeral_5m_input_tokens"))
            result.cache_write_1h = _count(split.get("ephemeral_1h_input_tokens"))
        else:
            result.cache_write_5m = _count(usage.get("cache_creation_input_tokens"))
        tools = usage.get("server_tool_use")
        if isinstance(tools, dict):
            result.extra_usd = _count(tools.get("web_search_requests")) * WEB_SEARCH_USD
        result.saw_input = "input_tokens" in usage
        result.speed = usage.get("speed") if isinstance(usage.get("speed"), str) else None
    else:
        if "prompt_tokens" in usage:
            prompt, completion, details = usage.get("prompt_tokens"), usage.get("completion_tokens"), usage.get("prompt_tokens_details")
        elif "input_tokens" in usage:
            prompt, completion, details = usage.get("input_tokens"), usage.get("output_tokens"), usage.get("input_tokens_details")
        else:
            return None
        cached = _count(details.get("cached_tokens")) if isinstance(details, dict) else 0
        prompt = _count(prompt)
        cached = min(cached, prompt)
        result.cache_read = cached
        result.input_tokens = prompt - cached
        result.output_tokens = _count(completion)
        result.saw_input = True
    return result


def from_json_body(fmt: str, body: bytes) -> Usage | None:
    """Usage from a complete (non-streamed) JSON response, or None."""
    try:
        obj = json.loads(body)
    except (ValueError, RecursionError):
        return None
    if not isinstance(obj, dict):
        return None
    raw = obj.get("usage")
    if raw is None and isinstance(obj.get("response"), dict):
        raw = obj["response"].get("usage")
    usage = from_usage_dict(fmt, raw, obj.get("model"))
    if usage is not None:
        usage.final = True
    return usage


class SseUsage:
    """Incremental parser for a server-sent-event stream; feed it the raw bytes as they pass through."""

    def __init__(self, fmt: str):
        self.fmt = fmt
        self.usage = Usage()
        self._buf = bytearray()
        self._skip = False
        self.events = 0
        self.dropped_lines = 0

    def feed(self, chunk: bytes) -> None:
        self._buf += chunk
        while True:
            end = self._buf.find(b"\n")
            if end < 0:
                break
            line = bytes(self._buf[:end])
            del self._buf[:end + 1]
            if self._skip:  # the tail of an overlong line
                self._skip = False
                continue
            self._line(line.rstrip(b"\r"))
        if len(self._buf) > MAX_LINE:
            self._buf.clear()
            self._skip = True
            self.dropped_lines += 1

    def _line(self, line: bytes) -> None:
        if not line.startswith(b"data:"):
            return
        payload = line[5:].strip()
        if not payload.startswith(b"{"):
            return
        try:
            obj = json.loads(payload)
        except (ValueError, RecursionError):
            return
        if isinstance(obj, dict):
            self.events += 1
            self._event(obj)

    def _event(self, obj: dict) -> None:
        if self.fmt == "anthropic":
            kind = obj.get("type")
            if kind == "message_start" and isinstance(obj.get("message"), dict):
                got = from_usage_dict("anthropic", obj["message"].get("usage"), obj["message"].get("model"))
                if got is not None:
                    got.saw_input = True
                    self.usage.merge(got)
            elif kind == "message_delta":
                got = from_usage_dict("anthropic", obj.get("usage"))
                if got is not None:
                    got.final = True
                    got.saw_input = False  # message_delta may omit the input side: keep what message_start said
                    self._merge_delta(got, obj.get("usage"))
            return
        resp = obj.get("response")
        raw = obj.get("usage") if isinstance(obj.get("usage"), dict) else resp.get("usage") if isinstance(resp, dict) else None
        got = from_usage_dict("openai", raw, obj.get("model") or (resp.get("model") if isinstance(resp, dict) else None))
        if got is not None:
            got.final = True
            self.usage.merge(got)

    def _merge_delta(self, got: Usage, raw: dict) -> None:
        # Merge only the fields the delta really carried: a missing field must not zero a value from message_start.
        for name, key in (("input_tokens", "input_tokens"), ("cache_read", "cache_read_input_tokens"),
                          ("output_tokens", "output_tokens")):
            if key in raw:
                setattr(self.usage, name, getattr(got, name))
        if "cache_creation" in raw or "cache_creation_input_tokens" in raw:
            self.usage.cache_write_5m, self.usage.cache_write_1h = got.cache_write_5m, got.cache_write_1h
        if got.extra_usd:
            self.usage.extra_usd = got.extra_usd
        self.usage.speed = got.speed or self.usage.speed
        self.usage.final = True
