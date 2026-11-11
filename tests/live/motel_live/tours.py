"""Phase 3: tours (agent jobs on disposable cabins). Enabled with ``--tours``, which implies ``--cabins``.

Everything the fakes could not prove, on a real Ubuntu server as real root: the installer's units and users, the concierge under real
systemd (descriptor store, restart), real cabins that run guests, the real egress filter, the real bundle inspection. No model is
called and no real key exists: the guests use the `command` adapter (no model, no secrets), and the one guest that needs a concierge
listener holds a *fake* key that is only ever compared against, never sent anywhere (every request the guest makes is refused by the
concierge before it would reach an upstream).

Two tours, four cabins in all (Hetzner bills each started hour, so that is the cost floor of these checks):

  tour 1 "main": ``alpha`` (commit; probes the filter and the concierge; stays up across a concierge restart) and ``bravo`` (commit).
  tour 2 "failures": ``fails`` (exits 3) and ``hangs`` (sleeps); ``tour cancel`` while ``hangs`` runs.

Like the cabins phase, every motel command runs on the test server over ssh. The harness only reads the cabins project.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import UTC, datetime

from motel_live import cloud
from motel_live.cabins import CabinsScenario
from motel_live.core import parse_json, wait_until
from motel_live.remote import (
    ATTEMPTS_SCRIPT,
    CD_SCRIPT,
    CLI,
    ENV_SCRIPT,
    REACH_BANNER_SCRIPT,
    REMOTE_SRC,
    SAMPLER_SCRIPT,
    SHA_SCRIPT,
    TOUR_REPO_SCRIPT,
)
from motel_live.rooms import USER

SPEC_DIR = "/tmp/motel-live-tours"
REPO = f"{SPEC_DIR}/repo"          # a git repository: the specs live beside it, so the repository stays clean
KEY_FILE = "/etc/motel/llm-keys/anthropic.key"
UNITS = ("/etc/systemd/system/motel-concierge.service", "/etc/systemd/system/motel-concierge.socket")
SERVICE = "motel-concierge.service"
SAMPLES = "/tmp/motel-live-inspect-samples"
SAMPLER_UNIT = "motel-live-sampler"
INSPECT_USER = "motel-inspect"
INSPECT_DIR = "/var/lib/motel-inspect"
TOURS_ROOT = "/var/lib/motel/tours"
FAKE_KEY_MARK = "LIVEFAKE"                       # the fake key is sk-ant-LIVEFAKE-<hex>; no real key ever exists in this phase
LOOP_ROUNDS, LOOP_SECONDS = 24, 10               # alpha asks the concierge every 10 s for four minutes
RESTART_AFTER = 40                               # seconds after alpha runs: well inside that window
GUEST_TTL = "15m"
SKEW = 3                                         # seconds around the restart that are not judged (clock skew, a probe in flight)
DIAG_DIR = "tours-diagnostics"                   # under the run's out dir: one numbered folder per failed check, full and unclipped
LOG_STREAMS = ("stdout", "stderr", "runner", "boot")
BAD_ENDS = {"failed", "cancelled", "timed_out", "over_budget"}   # a guest that ends like this will never run its command
SETTLE_S = 300                                   # how long a cancelled tour may take to end with its cabins deleted

TOURS_CONFIG = """
[tours]
enabled = true
concierge_port = 7781
max_concurrent_guests = 2
max_guests_per_tour = 2
max_eur_per_tour = 1.0
max_usd_model_per_guest = 0.05
max_ttl = "20m"
egress_allow = ["{egress}"]

[tours.bindings.anthropic]
upstream = "https://api.anthropic.com"
auth = "x-api-key"
key_file = "/etc/motel/llm-keys/anthropic.key"
paths = ["/v1/messages"]
"""

# -- the guest scripts (run by `sh -c` as the unprivileged guest user; stdout comes back through `tour logs`) ----------------

HELPERS = r"""code() { t=$1; shift; curl -sS -m "$t" -o /tmp/mr -w '%{http_code}' "$@" 2>/dev/null || true; }
kind() { sed -n 's/.*"type": *"\(motel_[a-z_]*\)".*/\1/p' /tmp/mr | tail -n 1; }
"""
LISTEN_LOOP = r"""i=0
while [ $i -lt %(rounds)d ]; do
  c=$(code 5 -X POST -H "x-api-key: $ANTHROPIC_API_KEY" "$ANTHROPIC_BASE_URL/v2/unknown")
  echo "LISTEN $(date +%%s) $c $(kind)"
  i=$((i+1)); sleep %(seconds)d
done
"""
ISOLATION = r"""for h in example.com api.anthropic.com 1.1.1.1; do echo "OUTSIDE $h $(code 8 "https://$h/")"; done
echo "ALLOWED %(egress)s $(code 25 -I "https://%(egress)s/")"
base=${ANTHROPIC_BASE_URL%%/u/anthropic}
echo "PLACEHOLDER foreign_host $(code 10 -X POST -H "x-api-key: $ANTHROPIC_API_KEY" "$base/u/evil.example/v1/messages") $(kind)"
echo "PLACEHOLDER other_binding $(code 10 -X POST -H "x-api-key: $ANTHROPIC_API_KEY" "$base/u/openai/v1/messages") $(kind)"
echo "PLACEHOLDER bad_path $(code 10 -X POST -H "x-api-key: $ANTHROPIC_API_KEY" "$ANTHROPIC_BASE_URL/v2/unknown") $(kind)"
echo "PLACEHOLDER bad_token $(code 10 -X POST -H "x-api-key: not-the-token" "$ANTHROPIC_BASE_URL/v1/messages") $(kind)"
"""
# `LIVEFAK[E]` matches the fake key but not its own text, so the script and the processes that run it never count themselves.
KEYSCAN = r"""e=$(env | grep -c 'LIVEFAK[E]')
p=$(cat /proc/[0-9]*/environ /proc/[0-9]*/cmdline 2>/dev/null | tr '\0' '\n' | grep -c 'LIVEFAK[E]')
f=$(timeout 120 grep -rIl --exclude-dir=proc --exclude-dir=sys --exclude-dir=dev -e 'LIVEFAK[E]' /etc /home /var /opt /srv /root /tmp /usr/local 2>/dev/null | wc -l)
echo "KEYSCAN env=$e procs=$p files=$f"
"""


def alpha_script(egress: str, skip: set) -> str:
    parts = ["echo alpha-change > alpha.txt\n", HELPERS]
    if "restart" not in skip:
        parts.append(LISTEN_LOOP % {"rounds": LOOP_ROUNDS, "seconds": LOOP_SECONDS})
    if "isolation" not in skip:
        parts += [ISOLATION % {"egress": egress}, KEYSCAN]
    parts.append("echo ALPHA-DONE\n")
    return "".join(parts)


def tour_spec(name: str, guests: list, server_type: str) -> str:
    # A guest's `repo` is relative to the spec file's directory (docs/tours.md), and the specs live beside the repository.
    guests = [{"repo": os.path.basename(REPO), **g} for g in guests]
    return json.dumps({"tour": {"name": name},
                       "defaults": {"adapter": "command", "ttl": GUEST_TTL, "secrets": [], "budget_usd": 0, "egress": [],
                                    "server_type": server_type},
                       "guests": guests}, indent=1)


def sh_guest(gid: str, script: str, **extra) -> dict:
    return {"id": gid, "argv": ["sh", "-c", script], **extra}


def parse_lines(text: str, word: str) -> list:
    return [line.split()[1:] for line in text.splitlines() if line.startswith(word + " ")]


def when(text):
    if not text:
        return None
    try:
        return datetime.fromisoformat(str(text).replace("Z", "+00:00")).astimezone(UTC).timestamp()
    except ValueError:
        return None


class CabinMeter:
    """Which cabin servers this phase made and for how long they lived (Hetzner bills every started hour, per server).

    Polled during the waits; the first poll that no longer lists a server is its deletion time (an upper bound by one poll)."""

    def __init__(self, scenario):
        self.cab, self.clock, self.hcc = scenario.cab, scenario.clock, scenario.hcc
        self.seen: dict = {}

    def poll(self) -> None:
        now = self.clock.time()
        current = self.cab.new("server", self.hcc.inventory(("server",)))
        for sid, item in current.items():
            rec = self.seen.setdefault(sid, {"first": now, "gone": None})
            rec.update(lease=(item.get("labels") or {}).get("lease"), type=item.get("server_type") or item.get("type"), created=when(item.get("created")))
        for sid, rec in self.seen.items():
            if sid not in current and rec["gone"] is None:
                rec["gone"] = now

    def rows(self) -> list:
        now = self.clock.time()
        out = []
        for sid, rec in sorted(self.seen.items()):
            start = rec.get("created") or rec["first"]
            seconds = max(0.0, (rec["gone"] if rec["gone"] is not None else now) - start)
            out.append({"server": sid, "lease": rec.get("lease"), "type": rec.get("type"), "seconds": int(seconds),
                        "billed_hours": cloud.billed_hours(seconds), "deleted": rec["gone"] is not None})
        return out


class ToursScenario:
    def __init__(self, ctx):
        self.ctx, self.rep, self.rem, self.clock, self.dry = ctx, ctx.report, ctx.remote, ctx.clock, ctx.dry
        self.hcc, self.args = ctx.hcc, ctx.args
        self.skip = set(filter(None, (self.args.tours_skip or "").split(",")))
        self.cab = CabinsScenario(ctx)
        self.rooms = self.cab.rooms
        self.meter = CabinMeter(self)
        self.fake_key = "sk-ant-" + FAKE_KEY_MARK + "-" + secrets.token_hex(12)
        ctx.redact.add(self.fake_key)
        self.uid = ""
        self.systemd = None
        self.tours: dict = {}
        self.t0 = None                 # when the phase began: the start of the journal the diagnostics read
        self.booted = False            # a guest has run its command: cabins boot, so a second tour can say something new
        self.diag_n = 0
        self.diag_dirs: list = []
        self.rep.fail_hooks.append(self.diagnose_failure)

    # ------------------------------------------------------------ helpers
    def tour(self, *args, timeout=600) -> tuple:
        """``motel tour ... --json`` as the tour user, from inside the test repository."""
        res = self.rem.run(["sh", "-c", CD_SCRIPT, "sh", REPO, CLI, "tour", *args, "--json"], as_user=USER, timeout=timeout)
        return res, parse_json(res)

    @staticmethod
    def doc(value) -> dict:
        return value if isinstance(value, dict) else {}

    def guests(self, tid: str) -> dict:
        res, doc = self.tour("status", tid)
        tour = self.doc(self.doc(doc).get("tour"))
        return {g.get("id"): g for g in tour.get("guests") or [] if isinstance(g, dict)}

    def observe(self) -> None:
        if not self.dry:
            self.meter.poll()

    def wait_guest(self, tid: str, gid: str, states: set, timeout: float, what: str):
        def poll():
            self.observe()
            guest = self.guests(tid).get(gid) or {}
            if guest.get("status") in states:
                return guest
            if guest.get("status") in BAD_ENDS and not states & BAD_ENDS and not self.dry:
                raise AssertionError(f"guest {gid} ended {guest.get('status')!r} instead of reaching {what}; {self.why(tid, gid)}")
            return None
        try:
            return wait_until(self.clock, poll, timeout=timeout, interval=5, what=what, dry=self.dry)
        except AssertionError as exc:
            if "timed out" in str(exc):
                raise AssertionError(f"{exc}; {self.why(tid, gid)}") from None
            raise

    def why(self, tid: str, gid: str) -> str:
        """What motel says about a guest that is not getting anywhere: its status, and for its last attempt the phase, the
        last thing the controller saw while the cabin was booking, and the error."""
        try:
            guest = self.guests(tid).get(gid) or {}
            attempts = guest.get("attempt_list") or []
            last = attempts[-1] if attempts else {}
            boot = self.doc(last.get("boot"))
            bits = [f"guest {gid} is {guest.get('status')!r}", f"{guest.get('attempts')} attempt(s)",
                    f"last attempt {last.get('id')}: phase {last.get('phase')}"]
            if boot.get("summary"):
                bits.append(f"last probe: {boot['summary']}")
            if last.get("error"):
                bits.append(f"error: {last['error']}")
            return "; ".join(str(b)[:300] for b in bits)
        except Exception as exc:  # noqa: BLE001  the message must never replace the failure it explains
            return f"(no explanation: {type(exc).__name__})"

    def wait_done(self, tid: str, timeout: float = 1200) -> tuple:
        """``tour wait`` in slices of 20 s (exit 9 = not yet), with the cabin meter polled in between."""
        res = doc = None
        deadline = self.clock.time() + timeout
        while True:
            self.observe()
            res, doc = self.tour("wait", tid, "--timeout", "20", timeout=120)
            if res.rc == 0 or self.dry:
                return res, doc
            if res.rc != 9 or self.clock.time() >= deadline:
                return res, doc
            self.clock.sleep(5)

    def result(self, tid: str, gid: str) -> dict:
        res, doc = self.tour("result", tid, gid)
        return self.doc(self.doc(doc).get("result"))

    def start(self, c, name: str, spec: str) -> str:
        path = f"{SPEC_DIR}/{name}.json"
        res = self.rem.put_text(path, spec)
        c.require(res.rc == 0, f"could not write the spec: {res.err.strip()[:200]}")
        res, doc = self.tour("start", path)
        c.require(res.rc == 0, f"tour start rc={res.rc}: {(res.err.strip() or res.out.strip())[:400]}")
        tid = str(self.doc(self.doc(doc).get("tour")).get("id") or ("<tour-id>" if self.dry else ""))
        c.require(re.fullmatch(r"t-[0-9a-f]+", tid) is not None, f"tour start printed no tour id: {res.out[:200]}")
        self.tours[name] = tid
        return tid

    def systemctl_show(self, *props) -> dict:
        res = self.rem.run(["systemctl", "show", *[a for p in props for a in ("-p", p)], SERVICE])
        return dict(line.partition("=")[::2] for line in res.out.splitlines() if "=" in line)

    # ------------------------------------------------------------ run
    def run(self) -> None:
        cabins_ok = self.dry or any(c["name"].startswith("cabins: config accepted") and c["status"] == "PASS" for c in self.rep.checks)
        if not cabins_ok:
            self.rep.add("tours: phase skipped", "SKIP", "the cabins setup did not pass, and tours run on cabins")
            return
        self.t0 = self.clock.time()
        try:
            if not self.install():
                return
            self.main_tour()
            self.settle("main")
            if "failure" not in self.skip:
                if self.booted or self.dry:
                    self.failure_and_cancel()
                else:
                    self.rep.add("tours: second tour skipped", "SKIP",
                                 "no guest of the first tour ever ran its command, so a second tour would only book more cabins that "
                                 "cannot boot; the diagnostics of the failed checks say why the first did not")
        finally:
            self.drain()

    # ------------------------------------------------------------ 1. install
    def install(self) -> bool:
        with self.rep.check("tours: [tours] config accepted, install.sh --cabins --tours --upgrade, concierge running", self.clock) as c:
            frag = TOURS_CONFIG.replace("{egress}", self.args.tours_egress)
            self.rooms.set_config(self.cab.config_text() + frag)
            res = self.rem.run(["sh", f"{REMOTE_SRC}/install.sh", "--users", USER, "--provider", "hetzner", "--cabins", "--tours",
                                "--upgrade"], timeout=900)
            c.evidence(f"install.sh rc={res.rc}: {(res.out + res.err).strip()[-300:]}")
            c.require(res.rc == 0, f"install.sh --cabins --tours --upgrade rc={res.rc}: {(res.err.strip() or res.out.strip())[-300:]}")
            # The harness drives every reap itself: all three housekeeping timers stay off.
            self.rem.run(["systemctl", "disable", "--now", "motel-housekeeping.timer", "motel-cabins-housekeeping.timer",
                          "motel-tours-housekeeping.timer"])
            # The fake key: the concierge reads it only when a request would be forwarded, and none ever is.
            res = self.rem.put_text(KEY_FILE, self.fake_key + "\n", "0640")
            c.require(res.rc == 0, f"could not write the fake key file: {res.err.strip()[:200]}")
            self.rem.run(["chown", "root:motel-concierge", KEY_FILE])
            res = self.rem.run(["systemctl", "is-active", "motel-concierge.socket", SERVICE])
            c.evidence(f"is-active: {res.out.split()}")
            c.require(res.out.split() == ["active", "active"], f"the concierge units are not both active: {res.out.strip()!r} {res.err.strip()[:200]}")
            res = self.rem.run(["systemctl", "--version"])
            m = re.match(r"\s*systemd\s+(\d+)", res.out)
            self.systemd = int(m.group(1)) if m else None
            c.evidence(f"systemd {self.systemd}")
        if self.rep.last_failed():
            return False
        with self.rep.check(f"tours: {INSPECT_USER} is an unprivileged system user and {INSPECT_DIR} is root:root 0711", self.clock) as c:
            res = self.rem.run(["id", "-u", INSPECT_USER])
            self.uid = res.out.strip()
            c.require(res.rc == 0 and self.uid.isdigit(), f"no {INSPECT_USER} user: {res.err.strip()[:200]}")
            c.require(self.dry or int(self.uid) not in (0,) and int(self.uid) < 1000, f"{INSPECT_USER} uid {self.uid} is not a system uid (< 1000)")
            res = self.rem.run(["getent", "passwd", INSPECT_USER])
            shell = res.out.strip().rsplit(":", 1)[-1]
            c.evidence(f"passwd: {res.out.strip()}")
            c.require(self.dry or shell in ("/usr/sbin/nologin", "/sbin/nologin", "/usr/bin/false", "/bin/false"), f"{INSPECT_USER} has a login shell: {shell!r}")
            res = self.rem.run(["stat", "-c", "%U:%G %a", INSPECT_DIR])
            c.evidence(f"{INSPECT_DIR}: {res.out.strip()}")
            c.require(res.out.strip() == "root:root 711", f"{INSPECT_DIR} is {res.out.strip()!r}, expected root:root 711")
        with self.rep.check("tours: systemd-analyze verify accepts the concierge units (no unknown key, no missing file)", self.clock) as c:
            res = self.rem.run(["systemd-analyze", "verify", *UNITS], timeout=120)
            text = (res.out + res.err).strip()
            c.evidence(f"systemd-analyze verify rc={res.rc}: {text[:400] or '(no output)'}")
            c.require(res.rc == 0, f"systemd-analyze verify rc={res.rc}: {text[:300]}")
            c.require(not re.search(r"unknown (key|lvalue|section)|ignoring|failed to|not found|no such file", text, re.I),
                      f"systemd-analyze verify reported problems: {text[:300]}")
        with self.rep.check("tours: the running concierge has its descriptor store (FileDescriptorStoreMax, $FDSTORE and the marker)", self.clock) as c:
            props = self.systemctl_show("FileDescriptorStoreMax", "FileDescriptorStorePreserve", "MainPID")
            c.evidence(f"systemctl show: {props}")
            c.require(self.dry or props.get("FileDescriptorStoreMax") == "512",
                      f"systemd's FileDescriptorStoreMax is {props.get('FileDescriptorStoreMax')!r}, expected 512 (a misspelled key leaves it 0)")
            res = self.rem.run(["sh", "-c", ENV_SCRIPT, "sh", SERVICE])
            env = dict(line.partition("=")[::2] for line in res.out.splitlines() if "=" in line)
            c.evidence(f"environment of the service process: {env}")
            c.require(env.get("MOTEL_FDSTORE_MAX") == "512", f"the service does not see MOTEL_FDSTORE_MAX=512: {env}")
            if self.systemd is None or self.systemd >= 254:
                c.require(self.dry or env.get("FDSTORE") == "512", f"systemd {self.systemd} should set $FDSTORE=512 for the service: {env}")
                c.require(self.dry or env.get("MOTEL_FDSTORE_SIGNAL") == "1", f"the unit should carry MOTEL_FDSTORE_SIGNAL=1 on systemd {self.systemd}: {env}")
                c.require(self.dry or props.get("FileDescriptorStorePreserve") == "yes", f"FileDescriptorStorePreserve is {props.get('FileDescriptorStorePreserve')!r}")
            else:
                c.evidence(f"systemd {self.systemd} < 254: no $FDSTORE and no Preserve line is expected")
        with self.rep.check("tours: motel doctor is ok with [tours]", self.clock) as c:
            res, doc = self.rooms.motel("doctor")
            bad = "; ".join(f"{k.get('level')} {k.get('check')}: {k.get('detail', '')}"[:300] for k in (self.doc(doc).get("checks") or [])
                            if k.get("level") != "ok")
            c.require(res.rc == 0 and self.doc(doc).get("ok") is True, f"doctor rc={res.rc}, non-ok checks: {bad or res.err.strip()[:300]}")
        return not self.rep.last_failed()

    # ------------------------------------------------------------ 2-4. the main tour
    def main_tour(self) -> None:
        egress, cab_type = self.args.tours_egress, self.args.cabin_type
        with self.rep.check("tours: a tour of two command guests starts (2 cabins at once, no model, no real key)", self.clock) as c:
            res = self.rem.run(["sh", "-c", TOUR_REPO_SCRIPT, "sh", REPO], as_user=USER)
            c.require(res.rc == 0, f"could not make the test repository: {res.err.strip()[:200]}")
            alpha = sh_guest("alpha", alpha_script(egress, self.skip), secrets=["anthropic"], budget_usd=0.01, egress=[egress])
            bravo = sh_guest("bravo", "echo bravo-change > bravo.txt")
            res = self.rem.run(["systemd-run", f"--unit={SAMPLER_UNIT}", "--quiet", "--collect", "sh", "-c", SAMPLER_SCRIPT, "sh",
                                SAMPLES, self.uid or "<uid>"])
            c.require(res.rc == 0, f"could not start the process sampler: {res.err.strip()[:200]}")
            tid = self.start(c, "tour-main", tour_spec("live-main", [alpha, bravo], cab_type))
            c.evidence(f"tour {tid}")
        if self.rep.last_failed():
            return
        tid = self.tours["tour-main"]
        restart = {}
        if "restart" not in self.skip:
            self.restart_midway(tid, restart)
        with self.rep.check("tours: `tour wait` succeeds and both guests are done with their cabins deleted", self.clock) as c:
            res, doc = self.wait_done(tid)
            c.require(res.rc == 0, f"tour wait rc={res.rc} (9 = timed out): {(res.err.strip() or res.out.strip())[:300]}; "
                                   + "; ".join(self.why(tid, g) for g in ("alpha", "bravo")))
            self.booted = True
            c.require(self.dry or self.doc(doc).get("satisfied") is True, "tour wait did not report satisfied")
            guests = self.guests(tid)
            c.evidence(f"guests: {[(g, v.get('status'), v.get('cleanup')) for g, v in sorted(guests.items())]}")
            for gid in ("alpha", "bravo"):
                g = guests.get(gid) or {}
                if not self.dry and not (g.get("status") == "done" and g.get("cleanup") == "deleted"):
                    rec = self.result(tid, gid)
                    c.require(False, f"guest {gid}: status {g.get('status')!r} cleanup {g.get('cleanup')!r}, motel's reason {rec.get('reason')!r}: "
                                     f"{(rec.get('error') or '')[:300]}")
        if self.rep.last_failed():
            return
        for gid in ("alpha", "bravo"):
            self.verify_result(tid, gid)
        self.fetch_branches(tid)
        self.inspection_evidence()
        if "restart" not in self.skip:
            self.restart_evidence(tid, restart)
        if "isolation" not in self.skip:
            self.isolation_evidence(tid)
        with self.rep.check("tours: after tour 1 no cabin server, firewall, address or volume remains", self.clock) as c:
            self.cab.wait_clean()
            self.observe()

    def verify_result(self, tid: str, gid: str) -> None:
        with self.rep.check(f"tours: `tour result` for {gid}: verified bundle digest, size and changed files, checked on the server", self.clock) as c:
            rec = self.result(tid, gid)
            ver = self.doc(rec.get("verified"))
            bundle = self.doc(ver.get("bundle"))
            c.evidence(f"bundle {bundle}, head {ver.get('head')}, changed {ver.get('changed_files')}, cabin {ver.get('cabin')}")
            c.require(self.dry or (rec.get("status") == "done" and rec.get("cleanup") == "deleted"), f"status {rec.get('status')!r} cleanup {rec.get('cleanup')!r}")
            c.require(self.dry or re.fullmatch(r"[0-9a-f]{64}", str(bundle.get("sha256"))), f"no verified bundle digest: {bundle}")
            c.require(self.dry or int(bundle.get("bytes") or 0) > 0, f"no verified bundle size: {bundle}")
            c.require(self.dry or re.fullmatch(r"[0-9a-f]{40}", str(ver.get("head"))), f"no head commit: {ver.get('head')!r}")
            c.require(self.dry or ver.get("changed_files") == [f"{gid}.txt"], f"changed files are {ver.get('changed_files')}, expected ['{gid}.txt']")
            c.require(self.dry or not rec.get("violations"), f"violations: {rec.get('violations')}")
            ran = self.doc(rec.get("runner_reported"))
            c.require(self.dry or (ran.get("exit_code") == 0 and ran.get("termination") == "exited"), f"runner reported {ran}")
            # What motel measured has to be what is on the disk: hash and size of the stored bundle, read as root.
            res = self.rem.run(["sh", "-c", SHA_SCRIPT, "sh", f"{TOURS_ROOT}/{tid}/results/{gid}"])
            lines = res.out.split()
            c.evidence(f"stored bundle: {lines[:1]} {lines[-1:]}")
            c.require(self.dry or (res.rc == 0 and lines[:1] == [bundle.get("sha256")]), f"the stored bundle's sha256 is not the verified one: {lines[:1]} vs {bundle.get('sha256')}")
            c.require(self.dry or (lines[-1:] == [str(bundle.get("bytes"))]), f"the stored bundle's size is {lines[-1:]}, verified {bundle.get('bytes')}")

    def fetch_branches(self, tid: str) -> None:
        with self.rep.check("tours: `tour fetch` imports branch motel/<tour>/<guest> into the test repository, with the guest's commit", self.clock) as c:
            for gid in ("alpha", "bravo"):
                res, doc = self.tour("fetch", tid, gid)
                d = self.doc(doc)
                branch = f"motel/{tid}/{gid}"
                c.require(res.rc == 0, f"tour fetch {gid} rc={res.rc}: {(res.err.strip() or res.out.strip())[:300]}")
                c.require(self.dry or d.get("branch") == branch, f"fetch named branch {d.get('branch')!r}, expected {branch}")
                tip = self.rem.run(["git", "-C", REPO, "rev-parse", "--verify", branch], as_user=USER).out.strip()
                c.require(self.dry or tip == d.get("tip") == self.result(tid, gid).get("verified", {}).get("head"),
                          f"branch {branch} is at {tip}, fetch said {d.get('tip')}")
                text = self.rem.run(["git", "-C", REPO, "show", f"{branch}:{gid}.txt"], as_user=USER).out.strip()
                c.require(self.dry or text == f"{gid}-change", f"{gid}.txt on the branch is {text!r}")
                names = self.rem.run(["git", "-C", REPO, "diff", "--name-only", f"main..{branch}"], as_user=USER).out.split()
                c.require(self.dry or names == [f"{gid}.txt"], f"the branch changes {names}")
                c.evidence(f"{branch} at {tip[:12]}")
            head = self.rem.run(["git", "-C", REPO, "rev-parse", "--abbrev-ref", "HEAD"], as_user=USER).out.strip()
            c.require(self.dry or head == "main", f"fetch changed the checked-out branch to {head!r}")

    def inspection_evidence(self) -> None:
        self.rem.run(["systemctl", "stop", f"{SAMPLER_UNIT}.service"])
        with self.rep.check(f"tours: bundle inspection ran as {INSPECT_USER} under prlimit (git seen under that user, with finite limits)", self.clock) as c:
            res = self.rem.run(["cat", SAMPLES])
            lines = [line for line in res.out.splitlines() if re.search(r"(^|\s)git\s", line)]
            hardened = [line for line in lines if "core.hooksPath=/dev/null" in line]
            limited = [line for line in hardened if re.search(r"Max address space \d+ \d+ bytes", line)
                       and re.search(r"Max processes \d+ \d+ processes", line)]
            c.evidence(f"{len(lines)} sample(s) of git under {INSPECT_USER}; {len(hardened)} hardened; {len(limited)} with finite limits; "
                       f"first: {(limited[0][:260] if limited else '-')}")
            c.require(self.dry or lines, f"no git process of {INSPECT_USER} was ever seen (sampled every 20 ms): inspection ran as another user, or never ran")
            c.require(self.dry or hardened, "git of the inspection user ran without -c core.hooksPath=/dev/null")
            c.require(self.dry or limited, "git of the inspection user had no finite address-space and process limits: prlimit was not applied")

    # ------------------------------------------------------------ 4. restart of the concierge
    def restart_midway(self, tid: str, out: dict) -> None:
        with self.rep.check("tours: `systemctl restart motel-concierge` while alpha runs (its listener is in the descriptor store)", self.clock) as c:
            self.wait_guest(tid, "alpha", {"running"}, 600, "guest alpha to run its command")
            self.booted = True
            if not self.dry:
                self.clock.sleep(RESTART_AFTER)
            before = self.systemctl_show("MainPID", "NFileDescriptorStore")
            t0 = self.rem.run(["date", "+%s"]).out.strip()
            res = self.rem.run(["systemctl", "restart", SERVICE], timeout=120)
            t1 = self.rem.run(["date", "+%s"]).out.strip()
            after = self.systemctl_show("MainPID", "NFileDescriptorStore")
            out.update(t0=int(t0) if t0.isdigit() else None, t1=int(t1) if t1.isdigit() else None, before=before, after=after)
            c.evidence(f"before {before}, after {after}, restart window {t0}-{t1}")
            c.require(res.rc == 0, f"systemctl restart rc={res.rc}: {res.err.strip()[:200]}")
            c.require(self.dry or before.get("MainPID") != after.get("MainPID"), "the service pid did not change: it was not restarted")
            c.require(self.dry or int(before.get("NFileDescriptorStore") or 0) >= 1,
                      f"systemd held no descriptor for alpha's listener before the restart: {before}")
            # Right after a restart the new process may not have re-stored its descriptors yet: note it, do not judge it. The
            # probes that follow (the next check) are what proves the listener came back.
            held = int(after.get("NFileDescriptorStore") or 0)
            c.evidence(f"descriptors held right after the restart: {held}" + ("" if held else " (noted only: the probes decide)"))
            res = self.rem.run(["systemctl", "is-active", SERVICE])
            c.require(res.out.strip() == "active", f"the concierge is {res.out.strip()!r} after the restart")

    def restart_evidence(self, tid: str, restart: dict) -> None:
        with self.rep.check("tours: alpha survived the restart: its placeholder worked on the same port before and after (fd store)", self.clock) as c:
            res, doc = self.tour("logs", tid, "alpha", "--stream", "stdout")
            text = str(self.doc(doc).get("text") or "")
            rows = [(int(r[0]), r[1], r[2] if len(r) > 2 else "") for r in parse_lines(text, "LISTEN") if r and r[0].isdigit()]
            t0, t1 = restart.get("t0"), restart.get("t1")
            c.evidence(f"{len(rows)} probe(s); restart window {t0}-{t1}")
            if not self.dry:
                c.require(t0 and t1, "the restart time was not recorded")
                before = [r for r in rows if r[0] < t0 - SKEW]
                after = [r for r in rows if r[0] > t1 + SKEW]
                c.require(len(before) >= 3 and len(after) >= 3,
                          f"too few probes around the restart ({len(before)} before, {len(after)} after); lines: {rows[:3]}...")
                stray = [r for r in before + after if not (r[1] == "403" and r[2] == "motel_path_denied")]
                c.require(not stray, f"probes outside the restart window were not refused by the concierge: {stray[:3]} "
                                     "(a code 000 means the listener was lost)")
                guests = self.guests(tid)
                c.require(guests.get("alpha", {}).get("status") == "done", f"alpha is {guests.get('alpha', {}).get('status')!r}")
                res, doc = self.tour("status", tid)
                lost = self.doc(self.doc(doc).get("tour")).get("lost_listeners")
                c.require(lost == {}, f"the concierge lost listeners in the restart: {lost}")
                rec = self.result(tid, "alpha")
                c.require(not rec.get("reason"), f"motel failed alpha itself: reason {rec.get('reason')!r}")

    # ------------------------------------------------------------ 3. isolation, from the inside
    def isolation_evidence(self, tid: str) -> None:
        res, doc = self.tour("logs", tid, "alpha", "--stream", "stdout")
        text = str(self.doc(doc).get("text") or "")

        def line(word: str, what: str) -> list:
            rows = [r for r in parse_lines(text, word) if r and r[0] == what]
            return rows[0] if rows else []
        with self.rep.check("tours: from inside a guest an arbitrary outside host is unreachable (example.com, api.anthropic.com, 1.1.1.1)", self.clock) as c:
            for host in ("example.com", "api.anthropic.com", "1.1.1.1"):
                row = line("OUTSIDE", host)
                c.evidence(f"{host}: {row[1:]}")
                c.require(self.dry or (row and row[1] == "000"), f"guest reached {host}: {row or 'no probe line'}; guest output: {text[-300:]!r}")
        with self.rep.check(f"tours: from inside a guest only the allowlisted egress works ({self.args.tours_egress})", self.clock) as c:
            row = line("ALLOWED", self.args.tours_egress)
            c.evidence(f"{self.args.tours_egress}: {row[1:]}")
            c.require(self.dry or (row and re.fullmatch(r"[23]\d\d", row[1])), f"the allowlisted host did not answer: {row or 'no probe line'}")
        with self.rep.check("tours: the concierge refuses the placeholder token for a binding or host that is not allowed, and for a wrong token", self.clock) as c:
            for name, code, kind in (("foreign_host", "403", "motel_binding_denied"), ("other_binding", "403", "motel_binding_denied"),
                                     ("bad_path", "403", "motel_path_denied"), ("bad_token", "401", "motel_invalid_token")):
                row = line("PLACEHOLDER", name)
                c.evidence(f"{name}: {row[1:]}")
                c.require(self.dry or (row and row[1:3] == [code, kind]), f"{name}: expected {code} {kind}, got {row[1:] if row else 'no probe line'}")
            counted = self.doc(self.result(tid, "alpha").get("concierge_refusals")).get("counted_by_concierge")
            c.evidence(f"refusals counted by the concierge for alpha: {counted}")
            c.require(self.dry or int(counted or 0) >= 3, f"the concierge counted {counted} refusals for alpha")
        with self.rep.check("tours: the real key is never on the guest (fake key searched in its environment, processes and files)", self.clock) as c:
            row = line("KEYSCAN", "")
            scan = dict(p.partition("=")[::2] for p in re.findall(r"\w+=\d+", " ".join(r for r in text.splitlines() if r.startswith("KEYSCAN"))))
            c.evidence(f"keyscan: {scan}")
            c.require(self.dry or (scan and all(v == "0" for v in scan.values())), f"the fake key was found on the guest: {scan or 'no scan line'}")
            del row
            c.require(self.dry or self.fake_key not in text, "the fake key is in the guest's output")

    # ------------------------------------------------------------ 5. failure and cancel
    def failure_and_cancel(self) -> None:
        cab_type = self.args.cabin_type
        with self.rep.check("tours: a second tour starts: one guest that exits 3 and one that sleeps", self.clock) as c:
            fails = sh_guest("fails", "echo before-failing > fails.txt; exit 3")
            hangs = sh_guest("hangs", "echo hanging > hangs.txt; sleep 900")
            tid = self.start(c, "tour-fail", tour_spec("live-failures", [fails, hangs], cab_type))
            c.evidence(f"tour {tid}")
        if self.rep.last_failed():
            return
        tid = self.tours["tour-fail"]
        with self.rep.check("tours: the guest that exits nonzero is reported failed (exit code 3) and its cabin is deleted", self.clock) as c:
            guest = self.wait_guest(tid, "fails", {"failed", "done", "timed_out", "cancelled", "over_budget"}, 900, "guest fails to end")
            rec = self.result(tid, "fails")
            ran = self.doc(rec.get("runner_reported"))
            lease = self.doc(rec.get("verified")).get("cabin")
            c.evidence(f"status {self.doc(guest).get('status')}, runner {ran}, cabin {lease}, cleanup {rec.get('cleanup')}")
            c.require(self.dry or self.doc(guest).get("status") == "failed", f"fails is {self.doc(guest).get('status')!r}, expected failed")
            c.require(self.dry or ran.get("exit_code") == 3, f"runner reported exit code {ran.get('exit_code')!r}, expected 3")
            c.require(self.dry or rec.get("cleanup") == "deleted", f"cleanup is {rec.get('cleanup')!r}")
            if not self.dry:
                left = self.hcc.list("server", f"lease={lease}") if lease else []
                c.require(lease and not left, f"the cabin {lease} of the failed guest still exists: {[s.get('id') for s in left]}")
        with self.rep.check("tours: `tour cancel` while hangs runs revokes, stops and deletes every cabin of the tour", self.clock) as c:
            self.wait_guest(tid, "hangs", {"running"}, 600, "guest hangs to run its command")
            self.observe()
            alive_before = [r for r in self.meter.rows() if not r["deleted"]]
            c.evidence(f"cabins alive before the cancel: {len(alive_before)}")
            res, doc = self.tour("cancel", tid)
            c.require(res.rc == 0, f"tour cancel rc={res.rc}: {(res.err.strip() or res.out.strip())[:300]}")
            guest = self.wait_guest(tid, "hangs", {"cancelled", "failed", "timed_out", "done"}, 600, "guest hangs to be cancelled")
            c.require(self.dry or self.doc(guest).get("status") == "cancelled", f"hangs is {self.doc(guest).get('status')!r}, expected cancelled")
            c.require(self.dry or self.doc(guest).get("cleanup") == "deleted", f"hangs cleanup is {self.doc(guest).get('cleanup')!r}")
            self.cab.wait_clean()
            self.observe()
            c.evidence("no cabin server, firewall, address or volume left in the cabins project")

    # ------------------------------------------------------------ 6. drain and cost
    def tour_over(self, tid: str) -> bool:
        """The tour has ended and every one of its cabins is deleted (what `tour status` says: status and cleanup)."""
        res = self.rem.desk("tour", "status", tid, "--json")
        tour = self.doc(self.doc(parse_json(res)).get("tour"))
        return tour.get("status") in ("done", "failed", "cancelled") and tour.get("cleanup") == "deleted"

    def await_ended(self, tids: list, timeout: float = SETTLE_S) -> list:
        """Wait until every tour of ``tids`` is over (see ``tour_over``); the tours that are not, after ``timeout`` seconds."""
        waiting = list(tids)

        def poll():
            self.observe()
            waiting[:] = [t for t in waiting if not self.tour_over(t)]
            return not waiting
        try:
            wait_until(self.clock, poll, timeout=timeout, interval=5, what="the cancelled tours to end with their cabins deleted", dry=self.dry)
        except AssertionError:
            pass
        return waiting

    def settle(self, name: str) -> None:
        """A tour that is still running after a failed check is cancelled, and the harness waits for it to end with its cabins
        deleted before anything else is started or checked out from under its supervisor (the cancel ends a cabin that is still
        booking at the supervisor's next poll)."""
        tid = self.tours.get("tour-" + name)
        if self.dry or not tid or self.tour_over(tid):
            return
        with self.rep.check(f"tours: the {name} tour, cancelled after a failed check, ends with its cabins deleted", self.clock) as c:
            res = self.rem.desk("tour", "cancel", tid, "--json")
            c.evidence(f"tour cancel rc={res.rc}")
            left = self.await_ended([tid])
            c.require(not left, f"tour {tid} was not over {SETTLE_S}s after its cancel; {'; '.join(self.why(tid, g) for g in self.guests(tid))}")

    def drain(self) -> None:
        """Best effort first (stop the sampler, cancel tours that still run and wait for them, check cabins out), then the final checks."""
        if not self.dry:
            self.rem.run(["systemctl", "stop", f"{SAMPLER_UNIT}.service"])
            res = self.rem.desk("tour", "ls", "--all", "--json")
            running = [str(row.get("id")) for row in self.doc(parse_json(res)).get("tours") or []
                       if row.get("status") not in ("done", "failed", "cancelled")]
            if running and any(c["name"].startswith("tours:") for c in self.rep.failed()):
                self.diagnose("before the drain cancels the running tours")
            for tid in running:
                self.rem.desk("tour", "cancel", tid, "--json")
            if running:
                self.await_ended(running)
            self.rem.desk("tour", "reap", "--apply", "--json")
            for row in self.cab.live_cabins():
                if row.get("lease_id"):
                    self.cab.cabin("checkout", str(row["lease_id"]))
            self.cab.reap()
        with self.rep.check("tours: after the phase no cabin server, firewall, address or volume remains", self.clock) as c:
            self.cab.wait_clean()
            self.observe()
            c.evidence("the cabins project holds nothing new, and the rooms project gained no cabin resource")
        self.cost()

    # ------------------------------------------------------------ diagnostics
    MAX_DIAGNOSES = 8

    def scrub(self, text) -> str:
        return self.ctx.redact(text)

    def diagnose_failure(self, check_name: str) -> None:
        if check_name.startswith("tours:") and not self.dry:
            self.diagnose(check_name)

    def diagnose(self, why: str) -> str | None:
        """Full, unclipped artifacts of the tours phase in ``<out dir>/tours-diagnostics/NN-<why>/`` (the log clips long JSON).

        Everything is read as root on the test server and copied back through the harness: ``tour status --json`` of every
        tour, ``tour logs`` of every guest (all four streams), the attempt records and the event log under the tour state
        directory, the journal of the tour supervisors and the concierge since the phase began, the cabins project's servers
        and whether the test server can open TCP 22 to each of them. Secrets known to the harness, API keys and private keys are
        replaced. Best effort: a step that fails is noted in ``index.json`` and never stops the others."""
        run_dir = getattr(self.ctx, "run_dir", None)
        if self.dry or not run_dir or self.diag_n >= self.MAX_DIAGNOSES:
            return None
        self.diag_n += 1
        slug = re.sub(r"[^a-z0-9]+", "-", why.lower()).strip("-")[:60] or "check"
        base = os.path.join(run_dir, DIAG_DIR, f"{self.diag_n:02d}-{slug}")
        os.makedirs(base, exist_ok=True)
        index: dict = {"why": self.scrub(why), "taken_at": datetime.now(UTC).isoformat(timespec="seconds"), "files": [], "errors": []}

        def save(name: str, text: str) -> None:
            path = os.path.join(base, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.scrub(text))
            index["files"].append(name)

        def step(label: str, fn) -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                index["errors"].append(f"{label}: {type(exc).__name__}: {str(exc)[:200]}")

        def keep(name: str, res) -> None:
            save(name, res.out + (f"\n[stderr]\n{res.err}" if res.err.strip() else "") + (f"\n[rc={res.rc}]" if res.rc else ""))

        ids: list = []

        def tours() -> None:
            res = self.rem.desk("tour", "ls", "--all", "--json")
            keep("tour-ls.json", res)
            for row in self.doc(parse_json(res)).get("tours") or []:
                if isinstance(row, dict) and row.get("id") and str(row["id"]) not in ids:
                    ids.append(str(row["id"]))
            ids.extend(t for t in self.tours.values() if t not in ids)
        step("tour ls", tours)
        for tid in ids:
            guests: list = []

            def status(tid=tid, guests=guests) -> None:
                res = self.rem.desk("tour", "status", tid, "--json")
                keep(f"{tid}/status.json", res)
                guests.extend(g.get("id") for g in self.doc(self.doc(parse_json(res)).get("tour")).get("guests") or [] if isinstance(g, dict))
            step(f"tour status {tid}", status)
            for gid in guests:
                for stream in LOG_STREAMS:
                    step(f"tour logs {tid} {gid} {stream}", lambda tid=tid, gid=gid, stream=stream: keep(
                        f"{tid}/logs-{gid}-{stream}.json", self.rem.desk("tour", "logs", tid, str(gid), "--stream", stream, "--tail", "262144", "--json")))

            def state(tid=tid) -> None:
                res = self.rem.run(["sh", "-c", ATTEMPTS_SCRIPT, "sh", f"{TOURS_ROOT}/{tid}"])
                keep(f"{tid}/state.txt", res)
            step(f"state of {tid}", state)

        def journal() -> None:
            since = int((self.t0 or self.clock.time()) - 60)
            keep("journal.txt", self.rem.run(["journalctl", "-u", "motel-tour-*", "-u", SERVICE, "--since", f"@{since}", "--no-pager",
                                              "-o", "short-iso"], timeout=180))
        step("journal", journal)
        step("cabin ls", lambda: keep("cabin-ls.json", self.rem.desk("cabin", "ls", "--json")))

        def cabins() -> None:
            initial = set((self.ctx.cabins_initial or {}).get("server", {}))
            servers = [sv for sv in self.hcc.list("server") if str(sv.get("id")) not in initial]
            save("cabins-servers.json", json.dumps(servers, indent=1, sort_keys=True))
            lines = []
            for sv in servers:
                ip = ((sv.get("public_net") or {}).get("ipv4") or {}).get("ip")
                label = f"server {sv.get('id')} {sv.get('name')} status={sv.get('status')} created={sv.get('created')} ip={ip}"
                if not ip:
                    lines.append(f"{label}: no public address")
                    continue
                res = self.rem.run(["sh", "-c", REACH_BANNER_SCRIPT, "sh", ip], timeout=40)
                verdict = {0: "connected", 1: "refused or closed", 124: "no answer (packets dropped)"}.get(res.rc, f"rc {res.rc}")
                lines.append(f"{label}: TCP 22 from the test server: {verdict}; banner: {res.out.strip()[:120] or '(none)'}")
            save("tcp22-from-the-test-server.txt", "\n".join(lines) + "\n")
        step("cabins and tcp/22", cabins)
        save("index.json", json.dumps(index, indent=1, sort_keys=True))
        self.diag_dirs.append(base)
        self.rep.meta["tours_diagnostics"] = list(self.diag_dirs)
        return base

    def expected_cabins(self) -> int:
        return 2 + (0 if "failure" in self.skip else 2)

    def cost(self) -> None:
        rows = self.meter.rows()
        hours = sum(r["billed_hours"] for r in rows)
        price = None if self.dry else self.hcc.hourly_price(self.args.cabin_type, self.args.location)
        est = None if price is None else round(hours * price, 4)
        self.rep.meta["tours_cabins"] = {"count": len(rows), "expected": self.expected_cabins(), "billed_hours": hours,
                                         "hourly_eur_gross": price, "estimate_eur": est, "cabins": rows}
        with self.rep.check("tours: cost: the cabins this phase created, in billed hours", self.clock) as c:
            for r in rows:
                c.evidence(f"cabin {r['lease']} (server {r['server']}, {r['type']}): {r['seconds']} s, {r['billed_hours']} billed hour(s)"
                           f"{'' if r['deleted'] else ', STILL LISTED'}")
            c.evidence(f"total {len(rows)} cabin(s), {hours} billed hour(s)" + (f" x EUR {price} = EUR {est}" if price else " (price unknown)"))
            c.require(len(rows) == self.expected_cabins(),
                      f"{len(rows)} cabin(s) were created, expected {self.expected_cabins()}: a retry or a stray cabin is billed too")
            c.require(all(r["deleted"] for r in rows), "a cabin of this phase is still listed")
