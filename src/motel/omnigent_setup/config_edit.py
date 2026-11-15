"""Text-level edit of an Omnigent server config for ``motel omnigent setup``.

motel has no YAML library (the core is standard library only), so the ``sandbox:`` block is edited as text. The edit
touches only the lines of that block (or appends one at the end of the file). Every other byte of the file stays as it
was, so other keys and every comment survive. Anything this module cannot edit safely becomes a ``manual`` plan: the
caller prints the exact block to paste and changes nothing.

Omnigent reads the block in two shapes (see ``omnigent/server/managed_hosts.py``)::

    sandbox:                          sandbox:
      provider: motel                   server_url: http://127.0.0.1:6767
      server_url: http://...            providers:
      motel: {server_type: cpx32}         - provider: motel
                                          - provider: modal

``plan_config`` is a pure function of the file text. ``motel omnigent status`` uses it too, to say whether the block
matches what ``setup`` would write.
"""

from __future__ import annotations

import re
from dataclasses import dataclass

PROVIDER = "motel"
KEY_RE = re.compile(r"^(?P<indent> *)(?P<dash>-[ ]+)?(?P<key>[A-Za-z_][A-Za-z0-9_.-]*)[ ]*:(?:[ ]+(?P<val>\S.*?))?[ ]*$")
DASH_RE = re.compile(r"^(?P<indent> *)-(?:[ ]|$)")
TOP_SANDBOX_RE = re.compile(r"""^(?P<q>["']?)sandbox(?P=q)[ ]*:(?P<rest>.*)$""")
URL_RE = re.compile(r"^https?://[A-Za-z0-9._~:/?&=%+@\[\]-]+$")
TYPE_RE = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
TTL_RE = re.compile(r"^[1-9][0-9]{0,3}[mhd]$")
NOTE = "# Added by `motel omnigent setup` (see docs/omnigent.md). Restart the Omnigent server to load it.\n"


@dataclass(frozen=True)
class Want:
    """What the user asked for. ``server_url_explicit`` is true when ``--server-url`` was given."""

    server_url: str
    server_url_explicit: bool = False
    server_type: str | None = None
    ttl: str | None = None
    replace_other: bool = False


@dataclass
class Plan:
    """The outcome of planning an edit.

    ``action``: ``unchanged``, ``create`` (no file yet), ``add`` (a new block or list entry), ``update`` (lines of an
    existing motel block changed), ``replace`` (a block for another provider, replaced because of ``--yes``) or
    ``manual`` (not edited: ``reason`` says why and ``block`` is what to paste). ``present`` is true when the file
    already names motel as a sandbox provider.
    """

    action: str
    block: str
    text: str | None = None
    reason: str = ""
    present: bool = False

    @property
    def changed(self) -> bool:
        return self.action in ("create", "add", "update", "replace")


@dataclass
class Entry:
    idx: int
    indent: int
    dash: bool
    key: str | None
    val: str | None          # the value without its comment; None when the line has no inline value
    raw_val: str | None      # the value as written, comment included


class _Manual(Exception):
    """Raised inside planning when the file cannot be edited safely."""


def check_want(want: Want) -> None:
    """Raise ValueError for a value that must not be written into YAML."""
    if not URL_RE.fullmatch(want.server_url):
        raise ValueError(f"bad server URL {want.server_url!r}: use http://127.0.0.1:6767 or an https:// address")
    if want.server_type is not None and not TYPE_RE.fullmatch(want.server_type):
        raise ValueError(f"bad server type {want.server_type!r}: use a cabin type such as cpx32")
    if want.ttl is not None and not TTL_RE.fullmatch(want.ttl):
        raise ValueError(f"bad ttl {want.ttl!r}: use e.g. 90m, 2h or 1d")


def _flags(want: Want) -> list[tuple[str, str]]:
    return [(k, v) for k, v in (("server_type", want.server_type), ("ttl", want.ttl)) if v is not None]


def render_block(want: Want) -> str:
    """The block ``setup`` writes: provider and server_url, plus a ``motel:`` sub-block only for flags that were given."""
    lines = ["sandbox:", f"  provider: {PROVIDER}", f"  server_url: {want.server_url}"]
    flags = _flags(want)
    if flags:
        lines.append("  motel:")
        lines += [f"    {k}: {v}" for k, v in flags]
    return "\n".join(lines) + "\n"


def _split_comment(text: str) -> tuple[str, str]:
    """Split ``value  # comment`` into ``("value", "  # comment")``. A ``#`` inside quotes is not a comment."""
    quote = None
    for i, ch in enumerate(text):
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch == "#" and (i == 0 or text[i - 1] in " \t"):
            value = text[:i].rstrip()
            return value, text[len(value):]
    return text.rstrip(), ""


def scalar(value: str | None) -> str | None:
    """A plain or simply quoted scalar as a string; None for anything structured (flow, block, anchor, tag)."""
    if value is None:
        return None
    text = value.strip()
    if not text or text[0] in "{[|>&*!%@`" or text in ("~", "null"):
        return None
    if len(text) >= 2 and text[0] == text[-1] and text[0] in "\"'":
        inner = text[1:-1]
        return None if "\\" in inner or text[0] in inner else inner
    if text[0] in "\"'":
        return None
    return text


def _is_content(line: str) -> bool:
    stripped = line.strip()
    return bool(stripped) and not stripped.startswith("#")


def _indent_of(line: str) -> int:
    return len(line) - len(line.lstrip(" "))


def _balanced(value: str) -> bool:
    depth, quote = 0, None
    for ch in value:
        if quote:
            if ch == quote:
                quote = None
        elif ch in "\"'":
            quote = ch
        elif ch in "{[":
            depth += 1
        elif ch in "}]":
            depth -= 1
    return depth == 0 and quote is None


def _parse_entries(lines: list[str], start: int, end: int) -> list[Entry]:
    """The content lines of lines[start:end] as entries. Block-scalar bodies are skipped: they are opaque text."""
    entries: list[Entry] = []
    skip_above: int | None = None
    for idx in range(start, end):
        line = lines[idx]
        if not _is_content(line):
            continue
        indent = _indent_of(line)
        if "\t" in line[:indent + 1]:
            raise _Manual("the block is indented with tabs")
        if skip_above is not None:
            if indent > skip_above:
                continue
            skip_above = None
        match = KEY_RE.match(line)
        if not match:
            dash_only = DASH_RE.match(line)
            text = line.strip()[1:].strip() if dash_only else None
            if text and not _balanced(text):
                raise _Manual("the block has a multi-line flow value")
            entries.append(Entry(idx, indent, bool(dash_only), None, text, None))
            continue
        raw = match.group("val")
        val = _split_comment(raw)[0] if raw is not None else None
        if val and val[0] in "{[" and not _balanced(val):
            raise _Manual("the block has a multi-line flow value")
        dash = match.group("dash")
        entries.append(Entry(idx, indent, bool(dash), match.group("key"), val, raw))
        if val and val[0] in "|>":
            skip_above = indent + (len(dash) if dash else 0)
    return entries


def _append_text(text: str, block_text: str) -> str:
    """Append a block to a file, keeping its text as it was and separating the block with a blank line."""
    out = text
    if out and not out.endswith("\n"):
        out += "\n"
    if out.strip() and not out.endswith("\n\n"):
        out += "\n"
    return out + NOTE + block_text


def plan_config(text: str | None, want: Want) -> Plan:
    """Plan the edit of a server config file. ``text`` is None when the file does not exist."""
    check_want(want)
    block = render_block(want)
    if text is None:
        return Plan("create", block, text=_append_text("", block))
    try:
        return _plan(text, want, block)
    except _Manual as exc:
        return Plan("manual", block, reason=str(exc))


def _plan(text: str, want: Want, block: str) -> Plan:
    if "\r" in text:
        raise _Manual("the file uses Windows line endings")
    lines = text.split("\n")
    content = [ln for ln in lines if _is_content(ln)]
    if any("\t" in ln[:len(ln) - len(ln.lstrip())] for ln in content):
        raise _Manual("the file is indented with tabs")
    if content and (content[0].lstrip().startswith("{") or any(ln.rstrip() == "..." for ln in content)
                    or any(ln.startswith("---") for ln in content[1:])):
        raise _Manual("the file is a flow mapping or has several YAML documents")
    tops = [i for i, ln in enumerate(lines) if TOP_SANDBOX_RE.match(ln)]
    if len(tops) > 1:
        raise _Manual("the file has more than one top-level sandbox key")
    if not tops:
        return Plan("add", block, text=_append_text(text, block))
    start = tops[0]
    head = TOP_SANDBOX_RE.match(lines[start])
    if head.group("q"):
        raise _Manual("the sandbox key is quoted")
    if _split_comment(head.group("rest").strip())[0]:
        raise _Manual("the sandbox block is written inline (a flow mapping or a value)")
    last = start
    for i in range(start + 1, len(lines)):
        if _is_content(lines[i]):
            if _indent_of(lines[i]) == 0:
                break
            last = i
    end = last + 1
    return _Editor(lines, start, end, _parse_entries(lines, start + 1, end), want, block).run()


class _Editor:
    """Edits the lines of one ``sandbox:`` block (``lines[start]`` is the ``sandbox:`` line, ``end`` is exclusive)."""

    def __init__(self, lines, start, end, entries, want, block):
        self.lines, self.start, self.end, self.entries, self.want, self.block = lines, start, end, entries, want, block
        self.replacements: dict[int, str] = {}
        self.insertions: dict[int, list[str]] = {}
        self.base = 2
        self.keys: dict[str, Entry] = {}

    # -- helpers
    def insert(self, after: int, new: list[str]) -> None:
        self.insertions.setdefault(after, []).extend(new)

    def finish(self, present: bool, action: str = "update") -> Plan:
        if not self.replacements and not self.insertions:
            return Plan("unchanged", self.block, present=present)
        out: list[str] = []
        for idx, line in enumerate(self.lines):
            out.append(self.replacements.get(idx, line))
            out.extend(self.insertions.get(idx, ()))
        return Plan(action, self.block, text="\n".join(out), present=present)

    def set_scalar(self, entry: Entry, new: str) -> None:
        """Replace the value of a ``key: value  # comment`` line, keeping the comment."""
        if scalar(entry.val) is None:
            raise _Manual(f"the value of {entry.key} is not a plain scalar")
        line = self.lines[entry.idx]
        start = KEY_RE.match(line).start("val")
        self.replacements[entry.idx] = f"{line[:start]}{new}{_split_comment(entry.raw_val)[1]}"

    def end_of(self, entry: Entry) -> int:
        """Index of the last content line under ``entry`` (deeper indent), or its own line."""
        last = entry.idx
        for e in self.entries:
            if e.idx > entry.idx:
                if e.indent <= entry.indent:
                    break
                last = e.idx
        return last

    # -- planning
    def run(self) -> Plan:
        entries = self.entries
        if not entries:
            body = render_block(self.want).split("\n")[1:-1]
            self.insert(self.start, body)
            return self.finish(False, "add")
        base = min(e.indent for e in entries)
        if any(e.indent == base and e.dash for e in entries):
            raise _Manual("the sandbox block holds a list, not a mapping")
        for e in entries:
            if e.indent == base and e.key:
                if e.key in self.keys:
                    raise _Manual(f"the sandbox block sets {e.key} twice")
                self.keys[e.key] = e
        self.base = base
        if "provider" in self.keys and "providers" in self.keys:
            raise _Manual("the sandbox block has both provider and providers")
        return self.plan_list() if "providers" in self.keys else self.plan_single()

    def plan_single(self) -> Plan:
        keys, want, pad = self.keys, self.want, " " * self.base
        provider = keys.get("provider")
        if provider is not None:
            name = scalar(provider.val)
            if name is None:
                raise _Manual("the provider value is not a plain name")
            if name != PROVIDER:
                if not want.replace_other:
                    raise _Manual(f"the sandbox block already uses provider {name}, so motel was not added. "
                                  "Run again with --yes to replace it (the old block is kept as comments and in the backup)")
                return self.replace_block()
            after, new = provider.idx, []
        else:
            after, new = self.start, [f"{pad}provider: {PROVIDER}"]
        url = keys.get("server_url")
        if url is None:
            new.append(f"{pad}server_url: {want.server_url}")
        elif want.server_url_explicit and scalar(url.val) != want.server_url:
            self.set_scalar(url, want.server_url)
        if new:
            self.insert(after, new)
        self.plan_flags(keys.get("motel"), self.entries, self.base, max(e.idx for e in self.entries))
        return self.finish(provider is not None, "update" if provider is not None else "add")

    def plan_flags(self, motel: Entry | None, entries: list[Entry], parent_indent: int, tail: int) -> None:
        """Make ``motel: {server_type, ttl}`` carry the flags that were given. ``tail`` is where a new sub-block goes."""
        flags = _flags(self.want)
        if not flags:
            return
        pad = " " * parent_indent
        if motel is None:
            self.insert(tail, [f"{pad}motel:", *[f"{pad}  {k}: {v}" for k, v in flags]])
            return
        if motel.val:
            raise _Manual("the motel: sub-block is written inline")
        nested = [e for e in entries if motel.idx < e.idx <= self.end_of(motel)]
        sub = min((e.indent for e in nested), default=parent_indent + 2)
        have = {e.key: e for e in nested if e.indent == sub and e.key}
        for key, value in flags:
            if key not in have:
                self.insert(motel.idx, [f"{' ' * sub}{key}: {value}"])
            elif scalar(have[key].val) != value:
                self.set_scalar(have[key], value)

    def replace_block(self) -> Plan:
        old = [("# " + ln if ln.strip() else "#") for ln in self.lines[self.start:self.end]]
        new = ["# Replaced by `motel omnigent setup --yes`. The previous block:", *old, *self.block.rstrip("\n").split("\n")]
        return Plan("replace", self.block, text="\n".join(self.lines[:self.start] + new + self.lines[self.end:]))

    # -- the providers: list shape
    def plan_list(self) -> Plan:
        providers, want = self.keys["providers"], self.want
        if providers.val:
            raise _Manual("the providers list is written inline")
        span_end = self.end_of(providers)
        inside = [e for e in self.entries if providers.idx < e.idx <= span_end]
        dashes = [e for e in inside if e.dash]
        if not dashes:
            raise _Manual("the providers list has no entries")
        item_indent = min(e.indent for e in dashes)
        items = [e for e in dashes if e.indent == item_indent]
        stops = [it.idx for it in items[1:]] + [span_end + 1]
        found = None
        for item, stop in zip(items, stops, strict=True):
            if self.item_provider(item, stop) == PROVIDER:
                found = (item, stop)
        shared_url = self.keys.get("server_url")
        sub = " " * (item_indent + 2)
        flags = _flags(want)
        if found is None:
            new = [f"{' ' * item_indent}- provider: {PROVIDER}"]
            if shared_url is None or (want.server_url_explicit and scalar(shared_url.val) != want.server_url):
                new.append(f"{sub}server_url: {want.server_url}")
            if flags:
                new += [f"{sub}motel:", *[f"{sub}  {k}: {v}" for k, v in flags]]
            self.insert(span_end, new)
            return self.finish(False, "add")
        item, stop = found
        inner = [e for e in self.entries if item.idx <= e.idx < stop]
        own = {e.key: e for e in inner if e.key and e.indent + (2 if e.dash else 0) == item_indent + 2}
        if item.key is None:                  # a flow entry such as `- {provider: motel}`: only a clean match is left alone
            if flags or shared_url is None or (want.server_url_explicit and scalar(shared_url.val) != want.server_url):
                raise _Manual("the motel entry is written inline")
            return self.finish(True)
        url = own.get("server_url") or shared_url
        if url is None:
            raise _Manual("the motel entry has no server_url; add sandbox.server_url")
        if want.server_url_explicit and scalar(url.val) != want.server_url:
            if "server_url" not in own:
                raise _Manual("the shared sandbox.server_url differs from --server-url; edit it by hand")
            self.set_scalar(url, want.server_url)
        tail = max(e.idx for e in inner)
        if flags and "motel" not in own:
            self.insert(tail, [f"{sub}motel:", *[f"{sub}  {k}: {v}" for k, v in flags]])
        else:
            self.plan_flags(own.get("motel"), inner, item_indent + 2, tail)
        return self.finish(True)

    def item_provider(self, item: Entry, stop: int) -> str | None:
        if item.key == "provider":
            return scalar(item.val)
        if item.key is None and item.val:
            match = re.search(r"provider\s*:\s*([^,}\s]+)", item.val)
            return match.group(1).strip("\"'") if match else None
        for e in self.entries:
            if item.idx < e.idx < stop and e.key == "provider" and e.indent == item.indent + 2:
                return scalar(e.val)
        return None
