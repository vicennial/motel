"""What the controller accepts back from a guest, and what it refuses.

Everything a guest leaves in its ``out/`` directory is untrusted. The controller lists that directory (types and
sizes only), reads ``run.json``, and then plans exactly which files it will fetch: fixed names for the bundle and
the logs, and the artifacts the guest declared *and* the spec asked for. Links, devices, sockets, pipes,
traversal, odd names, oversized files and too many files are refused here, before any byte is fetched. Each
fetched file is hashed and compared again by the caller. Nothing guest-supplied is ever executed or applied.
"""

from __future__ import annotations

import fnmatch
import json
import re
from dataclasses import dataclass, field

from motel.tours.guest import LOG_CAP_BYTES

RUN_SCHEMA = "motel/guest-run/v1"
RUN_JSON = "run.json"
BUNDLE = "result.bundle"
CLAIMS = "RESULT.json"
STDOUT = "stdout.log"
STDERR = "stderr.log"
ARTIFACT_DIR = "artifacts"
MAX_RUN_JSON = 64 * 1024
MAX_CLAIMS = 64 * 1024
MAX_LOG = LOG_CAP_BYTES                # one constant with the runner: what it keeps is the most we fetch
MAX_FILES = 256
MAX_LISTING = 20000
COMPONENT_RE = re.compile(r"^[A-Za-z0-9._@+=,-]{1,255}$")
HEX64 = re.compile(r"^[0-9a-f]{64}$")
SHA1 = re.compile(r"^[0-9a-f]{40}$")
# Reasons after which the runner leaves no bundle, result or artifacts: out/ could not be made safe to read.
UNCLEAN_REASONS = ("stragglers_left", "out_not_clean")
TERMINATIONS = ("exited", "timeout", "killed", "failed")
REASON_RE = re.compile(r"^[a-z][a-z0-9_]{0,39}$")
REFUSAL_RE = re.compile(r"^motel_[a-z0-9_]{1,60}$")
KINDS = {"f": "file", "d": "directory", "l": "symbolic link", "p": "pipe", "s": "socket", "b": "block device",
         "c": "character device"}


@dataclass(frozen=True)
class Entry:
    """One thing in the guest's ``out/`` directory, as the controller saw it listed. ``kind`` is a ``find %y`` letter."""

    path: str
    kind: str
    size: int = 0


@dataclass(frozen=True)
class Item:
    role: str                     # bundle | claims | stdout | stderr | artifact
    remote: str                   # path below out/
    local: str                    # path below the guest's results directory
    max_bytes: int
    sha256: str | None = None     # the digest the guest declared; checked after the fetch
    truncate: bool = False        # a log: more than max_bytes is cut off, not an error


@dataclass
class Plan:
    items: list = field(default_factory=list)
    violations: list = field(default_factory=list)
    notes: list = field(default_factory=list)


class BadRunDoc(ValueError):
    pass


MAX_JSON_DEPTH = 32


def loads_limited(data: bytes, max_depth: int = MAX_JSON_DEPTH):
    """``json.loads`` for text a guest wrote: refuses nesting deeper than ``max_depth`` before parsing (a deeply
    nested document would otherwise raise RecursionError). Raises ValueError."""
    depth, in_string, escaped = 0, False, False
    for byte in data:
        if in_string:
            if escaped:
                escaped = False
            elif byte == 0x5C:
                escaped = True
            elif byte == 0x22:
                in_string = False
        elif byte == 0x22:
            in_string = True
        elif byte in (0x5B, 0x7B):
            depth += 1
            if depth > max_depth:
                raise ValueError("the document is nested too deeply")
        elif byte in (0x5D, 0x7D):
            depth -= 1
    return json.loads(data)


def contains_text(value, needle: str, _depth: int = 0) -> bool:
    """True when ``needle`` is inside any string, key or number of a parsed JSON value (so an escaped spelling in
    the raw bytes does not hide it)."""
    if _depth > MAX_JSON_DEPTH + 2:
        return True              # cannot happen after loads_limited; refuse rather than recurse
    if isinstance(value, str):
        return needle in value
    if isinstance(value, dict):
        return any(contains_text(k, needle, _depth + 1) or contains_text(v, needle, _depth + 1) for k, v in value.items())
    if isinstance(value, list):
        return any(contains_text(v, needle, _depth + 1) for v in value)
    return False


def safe_relpath(path) -> bool:
    """A relative POSIX path of plain components: no ``..``, no ``.``, no empty part, nothing exotic."""
    if not isinstance(path, str) or not path or len(path) > 512 or path.startswith("/"):
        return False
    return all(part not in ("", ".", "..") and COMPONENT_RE.fullmatch(part) for part in path.split("/"))


def clean_text(text, limit: int = 512) -> str:
    return "".join(c for c in str(text) if c.isprintable())[:limit]


def _clamp(value, low, high):
    return min(max(value, low), high) if isinstance(value, int) and not isinstance(value, bool) else 0


def _int(value, low=0, high=1 << 40):
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise BadRunDoc("a number in run.json is out of range")
    return value


def validate_run_doc(doc, attempt_id: str) -> dict:
    """A strictly typed copy of the guest's ``run.json`` (or BadRunDoc). Every value is a claim, not a fact."""
    if not isinstance(doc, dict) or doc.get("schema") != RUN_SCHEMA:
        raise BadRunDoc("run.json has the wrong schema")
    if doc.get("attempt") != attempt_id:
        raise BadRunDoc("run.json names another attempt")
    termination = doc.get("termination")
    if termination not in TERMINATIONS:
        raise BadRunDoc("run.json has an unknown termination")
    exit_code = doc.get("exit_code")
    if exit_code is not None:
        exit_code = _int(exit_code, -255, 255)
    head = doc.get("head")
    if head not in (None, "") and not (isinstance(head, str) and SHA1.fullmatch(head)):
        raise BadRunDoc("run.json head is not a commit id")
    sha = doc.get("bundle_sha256")
    if sha is not None and not (isinstance(sha, str) and HEX64.fullmatch(sha)):
        raise BadRunDoc("run.json bundle digest is malformed")
    changed = doc.get("changed_files", [])
    if not isinstance(changed, list) or len(changed) > 2000:
        raise BadRunDoc("run.json changed_files is malformed")
    declared = doc.get("artifacts", [])
    if not isinstance(declared, list) or len(declared) > 1000:
        raise BadRunDoc("run.json artifacts is malformed")
    arts = []
    for item in declared:
        if not isinstance(item, dict) or not isinstance(item.get("path"), str) or not isinstance(item.get("sha256"), str) \
                or not HEX64.fullmatch(item["sha256"]):
            raise BadRunDoc("run.json lists a malformed artifact")
        arts.append({"path": item["path"], "bytes": _int(item.get("bytes")), "sha256": item["sha256"]})
    reason = doc.get("reason")
    reason = reason if isinstance(reason, str) and REASON_RE.fullmatch(reason) else None
    started = doc.get("command_started")
    result_json = doc.get("result_json")
    refusals = []
    raw_refusals = doc.get("concierge_refusals", [])
    for item in raw_refusals[:20] if isinstance(raw_refusals, list) else []:
        if isinstance(item, dict) and isinstance(item.get("type"), str) and REFUSAL_RE.fullmatch(item["type"]):
            status = item.get("status")
            status = status if isinstance(status, int) and not isinstance(status, bool) and 100 <= status <= 599 else None
            refusals.append({"type": item["type"], "status": status, "count": _clamp(item.get("count", 0), 0, 10**9)})
    rejected = []
    raw_rejected = doc.get("rejected", [])
    for item in raw_rejected[:100] if isinstance(raw_rejected, list) else []:
        if isinstance(item, dict):
            entry = {"path": clean_text(item.get("path", ""), 120), "reason": clean_text(item.get("reason", ""), 40)}
            if isinstance(item.get("detail"), str):                 # git_broken carries what git said
                entry["detail"] = clean_text(item["detail"], 200)
            rejected.append(entry)
    return {"reason": reason, "command_started": started if isinstance(started, bool) else None,
            "concierge_refusals": refusals, "rejected": rejected,
            "result_json": result_json if isinstance(result_json, bool) else False,
            "detail": clean_text(doc.get("detail", ""), 200) if isinstance(doc.get("detail"), str) else None,
            "stragglers_killed": _clamp(doc.get("stragglers_killed", 0), 0, 10**6),
            "stragglers_left": _clamp(doc.get("stragglers_left", 0), 0, 10**6),
            "pruned_out": _clamp(doc.get("pruned_out", 0), 0, 10**6),
            "exit_code": exit_code, "termination": termination, "started": clean_text(doc.get("started", ""), 40),
            "finished": clean_text(doc.get("finished", ""), 40), "head": head or None, "bundle_sha256": sha,
            "changed_files": [clean_text(c) for c in changed if isinstance(c, str)][:2000], "artifacts": arts,
            "stdout_bytes": _int(doc.get("stdout_bytes", 0)), "stderr_bytes": _int(doc.get("stderr_bytes", 0))}


def plan_collection(entries, run: dict, patterns, max_total_bytes: int, max_files: int = MAX_FILES) -> Plan:
    """Decide what to fetch from the listing and the (validated) ``run.json``. Violations are reported, not fetched."""
    plan = Plan()
    if len(entries) > MAX_LISTING:
        plan.violations.append(f"the guest left more than {MAX_LISTING} entries; the listing was cut off")
    by_path = {}
    for entry in entries[:MAX_LISTING]:
        if not safe_relpath(entry.path):
            plan.violations.append(f"unsafe path refused: {clean_text(entry.path, 80)!r}")
            continue
        by_path[entry.path] = entry
        if entry.kind not in ("f", "d"):
            plan.violations.append(f"{entry.path}: a {KINDS.get(entry.kind, 'special file')} is not accepted")
    budget = max_total_bytes

    def take(role, remote, local, declared_bytes, sha, cap) -> None:
        nonlocal budget
        entry = by_path.get(remote)
        if entry is None or entry.kind != "f":
            plan.violations.append(f"{remote}: declared by the guest but missing or not a regular file")
            return
        size = entry.size if declared_bytes is None else min(entry.size, declared_bytes)
        if entry.size > cap or size > budget:
            plan.violations.append(f"{remote}: over the size limit")
            return
        budget -= size
        plan.items.append(Item(role, remote, local, size if declared_bytes is not None else entry.size, sha))

    if run["bundle_sha256"] is not None:
        take("bundle", BUNDLE, BUNDLE, None, run["bundle_sha256"], max_total_bytes)
    elif BUNDLE in by_path:
        plan.notes.append("result.bundle present but run.json declares none: ignored")
    if CLAIMS in by_path and by_path[CLAIMS].kind == "f":
        if run.get("result_json"):      # only a RESULT.json that the runner says it validated is fetched at all
            take("claims", CLAIMS, CLAIMS, None, None, MAX_CLAIMS)
        else:
            plan.notes.append("RESULT.json is present but run.json does not report it as validated: ignored")
    for name in (STDOUT, STDERR):
        if name in by_path and by_path[name].kind == "f":
            entry = by_path[name]
            plan.items.append(Item("stdout" if name == STDOUT else "stderr", name, name, min(entry.size, MAX_LOG), None,
                                   truncate=True))
    count = 0
    taken = set()
    for art in run["artifacts"]:
        rel = art["path"]
        if rel in taken:
            plan.violations.append(f"{clean_text(rel, 80)}: declared more than once; the first one is used")
            continue
        clash = next((t for t in taken if rel.startswith(t + "/") or t.startswith(rel + "/")), None) if safe_relpath(rel) else None
        if clash is not None:
            plan.violations.append(f"{rel}: clashes with {clash} (a file and a directory of the same name); refused")
            continue
        if not safe_relpath(rel):
            plan.violations.append(f"unsafe artifact path refused: {clean_text(rel, 80)!r}")
            continue
        if not any(fnmatch.fnmatchcase(rel, p) for p in patterns):
            plan.violations.append(f"{rel}: not one of the requested artifact patterns")
            continue
        if count >= max_files:
            plan.violations.append(f"{rel}: more than {max_files} artifacts")
            continue
        count += 1
        taken.add(rel)
        take("artifact", f"{ARTIFACT_DIR}/{rel}", f"{ARTIFACT_DIR}/{rel}", art["bytes"], art["sha256"], max_total_bytes)
    declared_paths = {f"{ARTIFACT_DIR}/{a['path']}" for a in run["artifacts"]}
    ignored = [p for p, e in by_path.items() if p.startswith(ARTIFACT_DIR + "/") and e.kind == "f" and p not in declared_paths]
    if ignored:
        plan.notes.append(f"{len(ignored)} undeclared file(s) under artifacts/ ignored")
    return plan
