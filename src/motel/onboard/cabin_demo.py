"""``motel try --cabin``: a simulated cabin run and a 3-guest tour, ending with their cost receipts.

Like ``motel try``, it is offline: no network call, no root, no cloud account, no configuration. The REAL unprivileged
``motel cabin`` and ``motel tour`` command code runs in this process (argument parsing, the ssh and rsync steps it
would take, the status lines, the receipt block). What is simulated is everything behind the narrow sudo rule and the
cloud: ``World`` answers as the front desk would, keeps the provider's servers in memory and runs a simulated clock.
Started-hour billing is the real ``motel.billing`` arithmetic and the receipts are built by the real
``motel.receipts``. Prices and command output here are illustrative. The temporary folder is removed afterwards.
"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import os
import types
from datetime import timedelta
from decimal import Decimal

from motel import __version__, billing
from motel.cabins import cli as cabin_cli
from motel.cabins.util import iso, money, parse_iso
from motel.onboard.banner import BOLD, DIM, PINK, banner, paint
from motel.receipts import cabin_receipt, tour_receipt
from motel.testing import FakeClock
from motel.tours import cli as tour_cli
from motel.tours import spec as tour_spec

PIN = f"motel=={__version__}"
OFFER = {"server_type": "cpx32", "location": "fsn1", "vcpus": 4, "memory_gb": 8.0, "total_hourly_net_eur": "0.0318",
         "currency": "EUR"}
PUBLIC_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + "A" * 43
HOST_KEY = "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + "B" * 43
ADDRESS = "192.0.2.10"        # a documentation address (RFC 5737): it never reaches a real host
FILES = b"Makefile\0src/app.py\0tests/test_app.py\0"
TEST_OUTPUT = ("pytest tests/", "============ 42 passed in 3.41s ============")

TOUR_SPEC = """\
[tour]
name = "fix-flaky-tests"

[defaults]
adapter = "claude"
ttl = "90m"                   # wall clock per guest
secrets = ["anthropic"]       # the model key stays with the concierge
budget_usd = 1.50             # model spend per guest

[[guests]]
id = "fix-auth"
prompt = "Fix the failing tests in tests/auth."

[[guests]]
id = "fix-billing"
prompt = "Fix the failing tests in tests/billing."

[[guests]]
id = "lint"
adapter = "command"
argv = ["make", "lint"]
secrets = []
budget_usd = 0
"""
# (guest id, seconds the guest's cabin exists, model spend in USD). One guest runs past the first hour: two started hours.
GUESTS = (("fix-auth", 14 * 60 + 22, "0.8421"), ("fix-billing", 67 * 60 + 8, "1.1307"), ("lint", 3 * 60 + 10, "0"))
TOUR_ID = "t-3fa91c0b72"

READ_RECEIPT = 4.0     # seconds to read a receipt before the story goes on

LEARNED = (
    "A cabin is one fresh server. `motel cabin run` checks one in, runs your command there, and deletes it.",
    "The provider bills every STARTED hour in full: 4 minutes of work and 59 minutes of work both cost 1 hour.",
    "Every run ends with a receipt: server type, wall time, started hours, price per hour, estimated total.",
    "A tour is a swarm of bounded jobs, one fresh cabin per guest. Its receipt adds model spend and servers left.",
    "Estimates use the provider's list price, net of VAT. `--json` carries the same numbers under `receipt`.",
)
NEXT_STEPS = (
    (f"sudo uvx --from {PIN} motel install --users $USER --cabins --tours", "install the front desk with cabins and tours on"),
    ("sudo motel-desk cabin setup hetzner", "give it a token for a NEW, empty project: cabins only (hidden prompt)"),
    ("motel cabin run -- uname -a", "your first real cabin run, with its receipt (docs/cabins.md has the config)"),
    ("motel tour start tour.toml", "your first tour (docs/tours.md)"),
)


class Proc(types.SimpleNamespace):
    """What ``subprocess.run`` returns, as far as the cabin and tour code reads it."""

    def __init__(self, returncode=0, stdout=b""):
        super().__init__(returncode=returncode, stdout=stdout, stderr=b"")


class World:
    """The simulated front desk and cloud. All paths live under ``self.root``, a fresh temp folder."""

    def __init__(self, root: str, start):
        self.root = root
        self.clock = FakeClock(start)
        self.cabins: dict = {}
        self.out = io.StringIO()          # what a command run on a cabin prints
        self.last_doc: dict = {}          # the Narrator's hook for the rooms demo; unused here
        self.receipts: dict = {}
        self.tour_start = None
        os.makedirs(os.path.join(root, "state"))

    # -- the Narrator's view of the world
    def scrub(self, text: str) -> str:
        return text.replace(self.root, "")

    def stamp(self) -> str:
        return self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")

    def advance(self, **kwargs) -> None:
        self.clock.advance(**kwargs)

    def servers_left(self) -> int:
        return sum(1 for rec in self.cabins.values() if not rec["released_at"])

    # -- cabins
    def new_cabin(self, task: str, *, created=None) -> dict:
        lease = f"{task}-{hashlib.sha256(f'{task}{len(self.cabins)}'.encode()).hexdigest()[:12]}"
        now = created or self.clock()
        rec = {"lease_id": lease, "name": f"motel-{lease}", "state": "active", "task": task, "created_at": iso(now),
               "expires_at": iso(now + timedelta(hours=2)), "released_at": None, "offer": dict(OFFER), "address": ADDRESS,
               "host_key": HOST_KEY, "disk": None}
        self.cabins[lease] = rec
        return rec

    def view(self, rec: dict) -> dict:
        """The desk's cabin record: the billing numbers come from ``motel.billing``, the receipt from ``motel.receipts``."""
        now = self.clock()
        end = parse_iso(rec["released_at"]) or now
        hours = billing.billed_hours(parse_iso(rec["created_at"]), end)
        out = dict(rec, schema="motel/v1")
        out["ssh"] = {"user": "cabin", "host": ADDRESS, "port": 22, "host_key_alias": rec["name"], "host_key": HOST_KEY}
        out["billing"] = {"granularity": "started_hour", "billed_hours": hours, "paid_until": None, "release_window": None,
                          "source": "record"}
        out["cost_estimate_net_eur"] = money(Decimal(OFFER["total_hourly_net_eur"]) * hours)
        out["receipt"] = cabin_receipt(out, now)
        return out

    def release(self, rec: dict, when=None) -> dict:
        rec["state"], rec["released_at"] = "released", iso(when or self.clock())
        return self.view(rec)

    def desk(self, args: list) -> tuple:
        """The cabin verbs a ``motel cabin run`` calls: ``checkin`` and ``checkout``."""
        verb = args[0]
        if verb == "checkin":
            self.advance(seconds=38)                       # the server boots
            task = args[args.index("--task") + 1]
            return 0, {"ok": True, "cabin": self.view(self.new_cabin(task))}
        if verb == "checkout" and args[1] in self.cabins:
            self.advance(seconds=6)
            return 0, {"ok": True, "cabin": self.release(self.cabins[args[1]])}
        return 7, {"ok": False, "error": "no such cabin", "hint": "motel cabin ls", "code": 7}

    def run(self, argv, **_kw) -> Proc:
        """Stands in for ``subprocess.run`` inside the cabin client: git, the desk (through sudo), rsync and ssh."""
        prog = os.path.basename(argv[0])
        if prog == "git":
            return Proc(0, FILES)
        if prog == "sudo":
            code, doc = self.desk(argv[4:-1])              # sudo -n DESK cabin VERB ... --json
            return Proc(code, json.dumps(doc).encode())
        if prog == "rsync":
            self.advance(seconds=2)
            return Proc(0)
        if prog == "ssh":                                  # the command runs on the (simulated) cabin
            self.advance(minutes=4, seconds=10)
            job = json.loads(base64.b64decode(argv[-1].rsplit(" ", 1)[1]))
            self.out.write("\n".join(TEST_OUTPUT if job["argv"][:1] == ["make"] else (f"$ {' '.join(job['argv'])}",)) + "\n")
            return Proc(0)
        raise AssertionError(f"the cabin demo made an unexpected call: {argv[0]}")

    # -- tours
    def start_tour(self) -> None:
        self.tour_start = self.clock()
        for gid, _seconds, _usd in GUESTS:
            self.new_cabin(gid, created=self.tour_start + timedelta(seconds=20))

    def tour_view(self) -> dict:
        """The desk's tour record at the simulated time now: each guest is running until its cabin has lived its seconds."""
        now = self.clock()
        guests, spent = [], Decimal(0)
        for gid, seconds, usd in GUESTS:
            rec = next(r for r in self.cabins.values() if r["task"] == gid)
            done = now >= parse_iso(rec["created_at"]) + timedelta(seconds=seconds)
            row = {"id": gid, "status": "done" if done else "running", "cleanup": "deleted" if done else "live", "attempts": 1,
                   "result": None}
            if done:
                cab = self.release(rec, when=parse_iso(rec["created_at"]) + timedelta(seconds=seconds))
                spent += Decimal(cab["cost_estimate_net_eur"])
                row["result"] = {"attempt": f"a-{hashlib.sha256(gid.encode()).hexdigest()[:10]}", "branch": f"motel/{TOUR_ID}/{gid}",
                                 "exit_code": 0, "bundle": gid != "lint", "changed_files": 0 if gid == "lint" else 3,
                                 "artifacts": 0, "cost_eur": cab["cost_estimate_net_eur"], "usd": usd, "metered": True,
                                 "auth": "api-key", "partial": False, "violations": 0,
                                 "cabin": cabin_receipt(cab, now)}
            guests.append(row)
        counts: dict = {}
        for g in guests:
            counts[g["status"]] = counts.get(g["status"], 0) + 1
        finished = all(g["status"] == "done" for g in guests)
        tour = {"id": TOUR_ID, "name": "fix-flaky-tests", "status": "done" if finished else "running",
                "cleanup": "deleted" if finished else "live", "owner": "alice", "created_at": iso(self.tour_start),
                "finished_at": iso(now) if finished else None, "cancel_requested": False, "counts": counts, "guests": guests,
                "budget": {"max_eur": "5.0", "reserved_eur": "0", "spent_eur": money(spent)}}
        tour["receipt"] = tour_receipt(tour)
        return tour

    def tour_desk(self, args: list, stdin=None) -> tuple:
        verb = args[0]
        if verb in ("status", "wait") and args[1] == TOUR_ID:
            doc = {"schema": "motel/v1", "ok": True, "tour": self.tour_view()}
            if verb == "wait":
                doc["satisfied"] = doc["tour"]["status"] == "done"
            return 0, json.dumps(doc).encode(), b""
        return 7, json.dumps({"ok": False, "error": "no such tour", "hint": "motel tour ls", "code": 7}).encode(), b""


class Channel(io.StringIO):
    """A stdout or stderr that also keeps its lines, in order, in a log shared with the other stream (as a terminal shows them)."""

    def __init__(self, log: list, is_err: bool):
        super().__init__()
        self.log, self.is_err, self.pending = log, is_err, ""

    def write(self, text):
        self.pending += text
        while "\n" in self.pending:
            line, self.pending = self.pending.split("\n", 1)
            self.log.append((self.is_err, line))
        return super().write(text)


def run_cabin_cli(world: World, argv: list) -> tuple:
    """The real ``motel cabin`` entry point against the simulated desk. Returns (exit code, stdout, stderr, log)."""
    log: list = []
    out, err = Channel(log, False), Channel(log, True)
    world.out = out                                  # what a command run on the cabin prints goes to stdout
    code = cabin_cli.main(argv, runner=world.run, root=os.path.join(world.root, "state"), make_key=lambda directory: PUBLIC_KEY,
                          out=out, err=err, cwd=world.root, monotonic=lambda: world.clock().timestamp(),
                          desk_exists=lambda: True, sleep=lambda seconds: None)
    return code, out.getvalue(), err.getvalue(), log


def run_tour_cli(world: World, argv: list) -> tuple:
    """The real ``motel tour`` entry point against the simulated desk. Returns (exit code, stdout, stderr, log)."""
    log: list = []
    out, err = Channel(log, False), Channel(log, True)
    code = tour_cli.main(argv, runner=world.tour_desk, out=out, err=err)
    return code, out.getvalue(), err.getvalue(), log


def story(world: World, say) -> None:
    """The whole demo. The motel commands are the real client code; the desk and the cloud behind them are simulated."""
    spec_doc = tour_spec.load_text(TOUR_SPEC, source="the demo spec")
    tour_spec.parse(spec_doc)                        # the spec shown below is a valid one, checked by the real parser
    say.emit(banner(say.color))
    say.emit()
    say.para("Cabins and tours, simulated in about 20 seconds.", BOLD)
    say.para("The real motel cabin and tour commands, with a simulated front desk, cloud and clock. No network, no root.")
    say.para("Server prices and the command's output are illustrative. Real ones come from your provider at checkin.")

    say.step("Run a job on a fresh server",
             "cabin run checks in a server, syncs this git checkout to it, runs your command, and deletes the server.")
    code, out, err, log = run_cabin_cli(world, ["run", "--task", "unit-tests", "--", "make", "test"])
    say.command("motel cabin run --task unit-tests -- make test", code, out, err, log=log)
    [cabin] = world.cabins.values()
    world.receipts["cabin"] = cabin_receipt(world.view(cabin), world.clock())
    say.pause(READ_RECEIPT)
    say.aside("The command ran about 4 minutes. The provider still bills one whole started hour.")
    say.aside("With --json the last line of stdout is an object with the same numbers under `receipt`.")

    say.step("Start a tour of 3 guests",
             "A tour runs bounded agent jobs, one fresh cabin per guest. This spec has two model guests and one lint guest.")
    for line in TOUR_SPEC.splitlines():
        say.emit("   " + paint(line, DIM, color=say.color))
    world.start_tour()
    say.command("motel tour start tour.toml", 0, TOUR_ID,
                f"motel: tour {TOUR_ID} started · 3 guest(s). Try: motel tour wait {TOUR_ID}")
    say.aside("The demo skips bundling a git repository here, so this one answer is simulated, not run.")

    say.step("Look in while it runs", "status shows each guest. A guest that is done has already had its server deleted.")
    world.advance(minutes=20)
    say.clock_jump("20 minutes pass")
    code, out, err, log = run_tour_cli(world, ["status", TOUR_ID])
    say.command(f"motel tour status {TOUR_ID}", code, out, err, log=log)

    say.step("Wait for the end", "wait blocks until every guest has finished, then prints the receipt for the whole tour.")
    world.advance(minutes=50)
    say.clock_jump("50 minutes pass")
    code, out, err, log = run_tour_cli(world, ["wait", TOUR_ID])
    say.command(f"motel tour wait {TOUR_ID}", code, out, err, log=log)
    world.receipts["tour"] = tour_receipt(world.tour_view())
    say.pause(READ_RECEIPT)
    say.aside("fix-billing ran 1 hour 7 minutes, so it is billed 2 started hours. The others each cost 1.")
    say.para(f"   Servers left in the (simulated) project: {world.servers_left()}. Nothing keeps billing.", DIM)
    say.emit()
    say.para("Cost is the provider's list price, net of VAT. Model spend is metered by the concierge, in USD.", PINK)
