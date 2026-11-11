"""Shared plumbing: command runner (dry-run, redaction, logging), clock, and the check report."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
from dataclasses import dataclass, field

REDACTED = "[REDACTED]"
# Secrets of a known shape that the harness never registered (an API key echoed by a diagnostic command, a private key block).
SECRET_RES = (
    re.compile(r"sk-ant-[A-Za-z0-9_-]+"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----.*?-----END [A-Z ]*PRIVATE KEY-----", re.S),
)
# A secret-looking variable assignment in a log line (``OMNIGENT_HOST_LAUNCH_TOKEN=abc``): the value goes, the name stays.
ASSIGN_RE = re.compile(r"(?i)\b([A-Za-z_]*(?:TOKEN|SECRET|PASSWORD|API_?KEY)[A-Za-z_]*)=\S+")
RUN_ID_RE = re.compile(r"^[a-z0-9][a-z0-9-]{3,40}$")

PASS, FAIL, SKIP, PLAN = "PASS", "FAIL", "SKIP", "PLAN"


class Redactor:
    """Replaces registered secret values (and bearer-style headers) in any text that is logged or reported."""

    def __init__(self):
        self._secrets: set = set()

    def add(self, value) -> None:
        value = (value or "").strip()
        if len(value) >= 8:
            self._secrets.add(value)

    def __call__(self, text) -> str:
        text = text if isinstance(text, str) else str(text)
        for secret in sorted(self._secrets, key=len, reverse=True):
            text = text.replace(secret, REDACTED)
        for pattern in SECRET_RES:
            text = pattern.sub(REDACTED, text)
        text = ASSIGN_RE.sub(lambda m: f"{m.group(1)}={REDACTED}", text)
        return re.sub(r"(?i)(bearer\s+)\S+", r"\1" + REDACTED, text)


class Clock:
    """Real time. Tests replace it with a fake that advances without waiting."""

    def time(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class CmdResult:
    argv: list
    rc: int
    out: str = ""
    err: str = ""
    dry: bool = False

    @property
    def ok(self) -> bool:
        return self.rc == 0


class Runner:
    """Runs argv lists (never shell strings). Every command is logged, redacted, and skipped in dry-run mode."""

    def __init__(self, *, dry_run: bool, redact: Redactor, log_path: str | None = None, echo=None):
        self.dry_run, self.redact, self.log_path = dry_run, redact, log_path
        self.echo = echo if echo is not None else (lambda line: None)
        self.history: list = []

    def log(self, line: str) -> None:
        line = self.redact(line)
        self.echo(line)
        if self.log_path:
            with open(self.log_path, "a", encoding="utf-8") as handle:
                handle.write(line + "\n")

    def run(self, argv: list, *, input: str | None = None, timeout: float | None = 300, mutating: bool = True) -> CmdResult:
        argv = [str(a) for a in argv]
        self.history.append(argv)
        self.log("$ " + shlex.join(argv))
        if self.dry_run:
            return CmdResult(argv, 0, dry=True)
        try:
            proc = subprocess.run(argv, input=input, capture_output=True, text=True, timeout=timeout, shell=False,
                                  stdin=None if input is not None else subprocess.DEVNULL)
            res = CmdResult(argv, proc.returncode, proc.stdout, proc.stderr)
        except subprocess.TimeoutExpired as exc:
            res = CmdResult(argv, 124, _text(exc.stdout), _text(exc.stderr) + f"\n[timeout after {timeout}s]")
        except OSError as exc:
            res = CmdResult(argv, 127, "", f"{argv[0]}: {exc}")
        self.log(f"  -> rc={res.rc}" + (f" out={_clip(res.out)}" if res.out.strip() else "") +
                 (f" err={_clip(res.err)}" if res.err.strip() else ""))
        return res

    def popen(self, argv: list) -> subprocess.Popen | None:
        argv = [str(a) for a in argv]
        self.history.append(argv)
        self.log("$ " + shlex.join(argv) + "   (background)")
        if self.dry_run:
            return None
        return subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)


def _text(value) -> str:
    return value.decode("utf-8", "replace") if isinstance(value, bytes) else (value or "")


def _clip(text: str, limit: int = 400) -> str:
    text = text.strip().replace("\n", "\\n")
    return text if len(text) <= limit else text[:limit] + "...[clipped]"


class DryDoc(str):
    """Stands in for a JSON document in --dry-run mode: any lookup yields another placeholder."""

    def __new__(cls, name="x"):
        return super().__new__(cls, f"<{name}>")

    def __getitem__(self, key):
        return DryDoc(f"{str(self)[1:-1]}.{key}") if isinstance(key, str) else DryDoc(f"{str(self)[1:-1]}[{key}]")

    def get(self, key, default=None):
        return self[key]

    def __iter__(self):
        return iter(())

    def __bool__(self):
        return True


class Skip(Exception):
    """Raised inside a check to mark it SKIP with a reason."""


class Abort(Exception):
    """Raised to stop the scenarios (the teardown still runs)."""


class CheckCtx:
    def __init__(self, name: str, dry: bool):
        self.name, self.dry, self.evidence_lines, self.status, self.detail = name, dry, [], PASS, ""

    def evidence(self, text) -> None:
        self.evidence_lines.append(str(text))

    def require(self, cond, message: str) -> None:
        if not cond and not self.dry:
            raise AssertionError(message)

    def skip(self, reason: str) -> None:
        raise Skip(reason)


@dataclass
class Report:
    run_id: str
    redact: Redactor
    dry: bool = False
    path: str | None = None
    checks: list = field(default_factory=list)
    meta: dict = field(default_factory=dict)
    started: float = field(default_factory=time.time)
    fail_hooks: list = field(default_factory=list)   # called with the check's name right after a FAIL is recorded (diagnostics)

    def check(self, name: str, clock: Clock | None = None):
        return _CheckBlock(self, name, clock or Clock())

    def add(self, name: str, status: str, detail: str = "", evidence=None, seconds: float = 0.0, *, diagnose: bool = True) -> None:
        """Record a check. ``diagnose=False`` keeps the FAIL hooks quiet: an interrupt or an Abort ends the run, it is not a finding
        that evidence should be collected for (collecting it would only delay the teardown)."""
        self.checks.append({"name": name, "status": status, "detail": self.redact(detail), "seconds": round(seconds, 1),
                            "evidence": [self.redact(e) for e in (evidence or [])]})
        self.save()
        if status == FAIL and diagnose and not self.dry:
            for hook in list(self.fail_hooks):
                try:
                    hook(name)
                except Exception as exc:  # noqa: BLE001  a diagnostic never changes the verdict
                    self.meta.setdefault("fail_hook_errors", []).append(f"{type(exc).__name__}: {exc}"[:200])

    def last_failed(self) -> bool:
        return bool(self.checks) and self.checks[-1]["status"] == FAIL and not self.dry

    def failed(self) -> list:
        return [c for c in self.checks if c["status"] == FAIL]

    def to_json(self) -> dict:
        counts = {s: sum(1 for c in self.checks if c["status"] == s) for s in (PASS, FAIL, SKIP, PLAN)}
        return {"run_id": self.run_id, "dry_run": self.dry, "counts": counts, "checks": self.checks, **self.meta}

    def save(self) -> None:
        if not self.path:
            return
        tmp = self.path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as handle:
            handle.write(self.redact(json.dumps(self.to_json(), indent=1, sort_keys=True)) + "\n")
        os.replace(tmp, self.path)

    def summary(self) -> str:
        lines = [f"motel live run {self.run_id}" + ("  (dry run: nothing executed)" if self.dry else "")]
        for check in self.checks:
            lines.append(f"  {check['status']:<4} {check['name']}" + (f"  -- {check['detail']}" if check["detail"] else ""))
        counts = self.to_json()["counts"]
        lines.append("totals: " + ", ".join(f"{k} {v}" for k, v in counts.items() if v))
        return "\n".join(lines)


class _CheckBlock:
    """``with report.check("name") as c:`` records PASS, FAIL (assertion or error) or SKIP. Never raises (except interrupts)."""

    def __init__(self, report: Report, name: str, clock: Clock):
        self.report, self.name, self.clock = report, name, clock

    def __enter__(self) -> CheckCtx:
        self.t0 = self.clock.time()
        self.c = CheckCtx(self.name, self.report.dry)
        return self.c

    def __exit__(self, etype, exc, tb):
        seconds = self.clock.time() - self.t0
        if etype is None:
            self.report.add(self.name, PLAN if self.report.dry else PASS, self.c.detail, self.c.evidence_lines, seconds)
            return False
        if issubclass(etype, (KeyboardInterrupt, SystemExit, Abort)):
            why = str(exc) if issubclass(etype, Abort) else f"interrupted: {etype.__name__}"
            self.report.add(self.name, FAIL, why, self.c.evidence_lines, seconds, diagnose=False)
            return False
        if self.report.dry:
            self.report.add(self.name, PLAN, "", self.c.evidence_lines, seconds)
            return True
        if issubclass(etype, Skip):
            self.report.add(self.name, SKIP, str(exc), self.c.evidence_lines, seconds)
            return True
        kind = "" if issubclass(etype, AssertionError) else f"{etype.__name__}: "
        self.report.add(self.name, FAIL, f"{kind}{exc}", self.c.evidence_lines, seconds)
        return True


def parse_json(res: CmdResult):
    """JSON from a command's stdout (the last JSON object if other lines precede it). ``{}`` when there is none."""
    if res.dry:
        return DryDoc("json")
    text = res.out.strip()
    for candidate in (text, text[text.find("{"):] if "{" in text else "", text[text.find("["):] if "[" in text else ""):
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except ValueError:
            continue
    return {}


def wait_until(clock: Clock, fn, *, timeout: float, interval: float = 10, what: str = "condition", dry: bool = False):
    """Poll ``fn()`` until it returns something truthy; returns it. Raises AssertionError on timeout."""
    if dry:
        return fn()
    deadline = clock.time() + timeout
    while True:
        value = fn()
        if value:
            return value
        if clock.time() >= deadline:
            raise AssertionError(f"timed out after {int(timeout)}s waiting for {what}")
        clock.sleep(interval)


# -- hourly billing: when may a resource that is ready to go be deleted? (the same arithmetic as motel.billing, written out)
HOUR_S = 3600
RELEASE_MARGIN_S = 360       # the default `release_margin` (6m)
REAP_INTERVAL_S = 600        # the housekeeping timer
REAP_SLACK_S = 60            # the product's allowance for a pass that starts a little late
HARNESS_PASS_S = 60          # how often the harness runs a reap pass


def release_bounds(eligible_s: float, margin_s: int = RELEASE_MARGIN_S, interval_s: int = REAP_INTERVAL_S,
                   slack_s: int = REAP_SLACK_S, pass_s: int = HARNESS_PASS_S) -> tuple:
    """(earliest, latest) seconds after a resource's creation at which the product deletes something that is ready to go
    ``eligible_s`` after creation (idle long enough, or an empty floor past ``retire_after``).

    It is held for the release window of the billed hour it is ready in: the last ``interval_s`` before ``margin_s`` ahead
    of the next billed hour. It is deleted by the first pass that finds the window open, or earlier when the next pass
    (``interval_s`` plus ``slack_s`` away) would come after the window closed, so from ``opens - slack_s``. Never before it is
    eligible, and always before the billed hour ends. A resource ready inside the window, or in the margin after it, goes at
    once. One that turns ready so close to the end of the hour that the first pass to see it (``pass_s`` away at most) can
    fall after the hour turned is carried by that pass into the next billed hour's window, so its latest bound is the end of that hour."""
    hour = int(eligible_s // HOUR_S) + 1
    closes = hour * HOUR_S - margin_s
    opens = closes - interval_s
    if eligible_s + pass_s > hour * HOUR_S:
        hour += 1
    return max(float(eligible_s), float(opens - slack_s)), float(hour * HOUR_S)


def want_stderr() -> bool:
    return sys.stderr.isatty()
