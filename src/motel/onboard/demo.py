"""``motel try``: a guided, offline, unprivileged tour of one stay.

It runs the real rooms engine (``motel.desk`` and ``motel.rooms``) and the real ``motel`` CLI in this process,
against the in-memory fake provider, fake system operations (nothing is mounted) and a simulated clock. All
state lives in a temporary folder that is removed before the command returns, also after an error or Ctrl-C.
It needs no root, no cloud account, no network and no configuration, and it never reads or writes motel's
real directories.
"""

from __future__ import annotations

import argparse
import io
import json
import os
import shutil
import sys
import tempfile
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime

from motel import __version__, desk
from motel import cli as motel_cli
from motel.config import Config, Paths
from motel.errors import Usage
from motel.onboard.banner import AMBER, BOLD, DIM, PINK, TEAL, banner, expiry_line, paint, want_color
from motel.providers.fake import FakeProvider
from motel.rooms import Caller
from motel.state import atomic_write
from motel.testing import FakeClock, FakeSysOps

SCHEMA = "motel/try/v1"
SERVER = "1000001"
USER = "alice"
TASK = "video-render"
DEMO_CONFIG = f"""\
[host]
provider = "hetzner"
allow_users = ["{USER}"]

[rooms]
mount_root = "/mnt/motel"
price_gb_month_eur = 0.05

[rooms.caps]
max_floors = 1
max_floor_gb = 100
max_total_gb = 100
monthly_eur = 30
"""

NEXT_STEPS = (
    ("sudo uvx --from motel==X.Y.Z motel install --users $USER", "install the front desk and the narrow sudo rule (X.Y.Z: the release)"),
    ("sudo motel-desk setup hetzner", "give it a Read & Write token for a dedicated project (hidden prompt)"),
    ("motel doctor", "check the installation"),
    ("motel checkin 50G --for 6h --task my-job", "take your first real room"),
    ("npx skills add vicennial/motel", "teach your agent when to use it (MCP: motel mcp)"),
)

LEARNED = (
    "A room is a private folder on a temporary volume. You ask for a size and a checkout time.",
    "Late checkout (extend) moves the time out. Past the time, a room waits in grace with its data intact.",
    "After grace it moves to lost & found for 24 hours, where restore brings it back, files and all.",
    "At the cap, motel says no vacancy (exit 3) before it asks the cloud for anything.",
    "Checkout --now deletes at once, and housekeeping then deletes the empty volume so billing stops.",
)

PAUSE_SHORT, PAUSE_LONG = 0.7, 1.3


class TryParser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message, "motel try --help")

    def exit(self, status=0, message=None):
        if status:
            raise Usage((message or "").strip(), "motel try --help")
        raise SystemExit(status)


def build_parser() -> argparse.ArgumentParser:
    parser = TryParser(prog="motel try", allow_abbrev=False,
                       description="A 30-second guided demo of one stay. Offline, no root, no cloud, no config: it runs the real "
                                   "rooms engine against a fake provider and a simulated clock, in a temp folder it deletes.")
    parser.add_argument("--fast", action="store_true", help="no pauses between steps")
    parser.add_argument("--json", action="store_true", help="print one machine-readable transcript instead of the narration")
    parser.add_argument("--no-color", action="store_true", help="plain text (NO_COLOR works too)")
    return parser


@dataclass
class Result:
    """What a run leaves behind, for tests and for ``--json``."""

    transcript: dict
    sandbox: str
    removed: bool
    sysops_calls: list = field(default_factory=list)
    provider_calls: list = field(default_factory=list)
    ok: bool = True


class Tour:
    """One simulated stay. All paths live under ``self.root``, a fresh temp folder."""

    def __init__(self, root: str, start: datetime):
        self.root = root
        self.paths = Paths(root=root)
        os.makedirs(self.paths.config_dir)
        with open(self.paths.config_file, "w", encoding="utf-8") as handle:
            handle.write(DEMO_CONFIG)
        atomic_write(self.paths.receipt_file, json.dumps({
            "schema": "motel/v1", "provider": "hetzner", "server_id": SERVER, "location": "fsn1",
            "setup_at": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "never_touch": []}), 0o644)
        Config.load(self.paths.config_file)  # fail early if the demo config is ever invalid
        self.clock = FakeClock(start)
        self.provider = FakeProvider(server_id=SERVER, clock=self.clock)
        self.sysops = FakeSysOps()
        self.caller = Caller(uid=os.getuid(), gid=os.getgid(), name=USER)
        self.operator = Caller(uid=os.getuid(), gid=os.getgid(), name=USER, operator=True)
        self.last_doc: dict = {}

    def scrub(self, text: str) -> str:
        """Show paths as they would look on a real host (without the temp prefix)."""
        return text.replace(self.root, "")

    def _desk(self, argv: list, caller: Caller) -> tuple:
        out, err = io.StringIO(), io.StringIO()
        code = desk.run(argv, paths=self.paths, caller=caller, make_provider=lambda cfg, paths: self.provider,
                        sysops=self.sysops, clock=self.clock, out=out, err=err, groups_fn=lambda c: set())
        return code, out.getvalue(), err.getvalue()

    def cli(self, argv: list) -> tuple:
        """Run the real ``motel`` CLI with the front desk call swapped for the in-process engine."""
        def runner(desk_argv):
            code, out, err = self._desk(desk_argv, self.caller)
            try:
                self.last_doc = json.loads(out)
            except ValueError:
                self.last_doc = {}
            return code, out, err

        out, err = io.StringIO(), io.StringIO()
        code = motel_cli.main(argv, runner=runner, out=out, err=err)
        return code, out.getvalue(), err.getvalue()

    def housekeeping(self) -> tuple:
        return self._desk(["reap", "--apply"], self.operator)

    def advance(self, **kwargs) -> None:
        self.clock.advance(**kwargs)

    def stamp(self) -> str:
        return self.clock().strftime("%Y-%m-%dT%H:%M:%SZ")


class Narrator:
    """Prints the story and records the transcript. In ``--json`` mode it prints nothing and never pauses."""

    def __init__(self, out, *, color: bool, fast: bool, quiet: bool, sleep, tour: Tour):
        self.out, self.color, self.fast, self.quiet, self.sleep, self.tour = out, color, fast, quiet, sleep, tour
        self.steps: list = []
        self.current: dict | None = None

    def emit(self, text: str = "") -> None:
        if self.quiet:
            return
        try:
            print(text, file=self.out)
        except UnicodeEncodeError:
            print(text.encode("ascii", "replace").decode(), file=self.out)
        self.out.flush()

    def pause(self, seconds: float) -> None:
        if not (self.fast or self.quiet):
            self.sleep(seconds)

    def step(self, title: str, *story: str) -> None:
        number = len(self.steps) + 1
        self.current = {"step": number, "title": title, "clock": self.tour.stamp(), "commands": []}
        self.steps.append(self.current)
        self.emit()
        self.emit(paint(f"{number}. {title}", PINK, BOLD, color=self.color))
        for line in story:
            self.emit("   " + line)
        self.pause(PAUSE_SHORT)

    def aside(self, text: str) -> None:
        self.emit("   " + paint(text, DIM, color=self.color))

    def command(self, shown: str, code: int, out: str = "", err: str = "", *, note: str | None = None,
                exit_visible: bool = False) -> None:
        out, err = self.tour.scrub(out).rstrip("\n"), self.tour.scrub(err).rstrip("\n")
        room = (self.tour.last_doc or {}).get("room") or {}
        self.tour.last_doc = {}
        record = {"command": shown, "exit_code": code, "stdout": out, "stderr": err}
        if room:
            record["room"] = {k: room.get(k) for k in ("id", "state", "size_gb", "expires", "time_left_h", "path")
                              if k in room}
            record["room"]["path"] = self.tour.scrub(record["room"].get("path") or "") or None
        if note:
            record["note"] = note
        self.current["commands"].append(record)
        self.emit(paint(f"$ {shown}", BOLD, color=self.color))
        for line in err.splitlines():
            self.emit(paint(line, DIM, color=self.color))
        for line in out.splitlines():
            self.emit(line)
        if exit_visible or code:
            self.emit(paint(f"[exit {code}]", AMBER if code else DIM, color=self.color))
        self.pause(PAUSE_LONG)

    def clock_jump(self, text: str) -> None:
        self.current["clock_after"] = self.tour.stamp()
        self.emit("   " + paint(f"~ {text} (simulated clock: {self.tour.stamp()})", TEAL, color=self.color))
        self.pause(PAUSE_SHORT)


def ls_listing(path: str, shown: str) -> tuple:
    """What `ls` prints for a directory (hidden entries skipped), or its error when the directory is gone."""
    try:
        names = sorted(n for n in os.listdir(path) if not n.startswith("."))
    except OSError:
        return 2, "", f"ls: cannot access '{shown}': No such file or directory"
    return 0, "  ".join(names), ""


def story(tour: Tour, say: Narrator) -> None:
    """The whole stay. Every motel command here is the real CLI; the engine behind it is the real one."""
    say.emit(banner(say.color))
    say.emit()
    say.emit(paint("A guided stay, simulated in about 30 seconds.", BOLD, color=say.color))
    say.emit("Real rooms engine, fake cloud, simulated clock. Nothing is installed, mounted or sent anywhere.")
    say.emit("A temporary folder plays the cloud volume. It is deleted when the tour ends.")

    say.step("Check in",
             "Your render job needs 40 GB of scratch space for 6 hours. checkin prints one thing on stdout: the path.")
    code, out, err = tour.cli(["checkin", "40G", "--for", "6h", "--task", TASK])
    room = tour.last_doc["room"]
    room_id, path = room["id"], room["path"]
    say.command(f"D=$(motel checkin 40G --for 6h --task {TASK})", code, "", err)
    shown_path = tour.scrub(path)
    say.command('echo "$D"', 0, out)

    say.step("Use it", "The room is a private folder (mode 0700, yours). Write results into it like any directory.")
    with open(os.path.join(path, "notes.txt"), "w", encoding="utf-8") as handle:
        handle.write("frame 4812 rendered\n")
    say.command('echo "frame 4812 rendered" > "$D/notes.txt"', 0)
    code, listing, lerr = ls_listing(path, shown_path)
    say.command('ls "$D"', code, listing, lerr)

    say.step("See the room", "ls shows every room you hold: its state, size and time left.")
    code, out, err = tour.cli(["ls"])
    say.command("motel ls", code, out, err)

    say.step("Late checkout", "The job runs long. extend pushes the checkout time out by 2 hours.")
    code, out, err = tour.cli(["extend", room_id, "--for", "2h"])
    say.command(f"motel extend {room_id} --for 2h", code, out, err)

    say.step("Nobody checks out", "Now you forget about the room. Fast-forward 9 hours: the checkout time (8 hours in) passes.")
    tour.advance(hours=9)
    say.clock_jump("9 hours pass")
    say.emit("   " + expiry_line(room_id, say.color))
    code, out, err = tour.cli(["status", room_id])
    say.command(f"motel status {room_id}", code, out, err)
    say.aside("Grace: the files are still there and extend would revive the room.")

    say.step("Grace runs out", "Housekeeping runs every 10 minutes on a real host. Another 6 hours pass, then it runs once.")
    tour.advance(hours=6)
    say.clock_jump("6 hours pass")
    code, out, err = tour.housekeeping()
    say.command("sudo motel-desk reap --apply", code, out, err)
    code, listing, lerr = ls_listing(path, shown_path)
    say.command('ls "$D"', code, listing, lerr)
    code, out, err = tour.cli(["ls"])
    say.command("motel ls", code, out, err)
    say.aside("Lost & found: moved aside, kept for 24 hours, unreachable for now.")

    say.step("Change your mind", "10 hours later you remember the render. Lost & found hands the room back with its files.")
    tour.advance(hours=10)
    say.clock_jump("10 hours pass")
    code, out, err = tour.cli(["restore", room_id, "--for", "4h"])
    say.command(f"motel restore {room_id} --for 4h", code, out, err)
    code, listing, lerr = ls_listing(path, shown_path)
    say.command('ls "$D"', code, listing, lerr)

    say.step("No vacancy", "This demo host is small: one floor, at most 100 GB. Ask for more than fits.")
    code, out, err = tour.cli(["checkin", "90G", "--for", "6h", "--task", "big-job"])
    say.command("motel checkin 90G --for 6h --task big-job", code, out, err, exit_visible=True)
    say.aside("Exit 3 is a policy refusal. It happens before any cloud call. Agents must not retry in a loop.")

    say.step("Check out", "Done. checkout --now deletes at once. Without --now the room waits in lost & found for a day.")
    code, out, err = tour.cli(["checkout", room_id, "--now"])
    say.command(f"motel checkout {room_id} --now", code, out, err)
    say.aside("Purged: the files are gone for good.")
    tour.advance(minutes=16)
    say.clock_jump("16 minutes pass")
    code, out, err = tour.housekeeping()
    say.command("sudo motel-desk reap --apply", code, out, err)
    say.emit("   " + paint(f"Cloud volumes left in the (fake) project: {len(tour.provider.volumes)}. Nothing keeps billing.",
                           DIM, color=say.color))


def epilogue(say: Narrator, removed: bool, root: str) -> dict:
    say.emit()
    say.emit(paint("What you just saw", PINK, BOLD, color=say.color))
    for line in LEARNED:
        say.emit(f"  - {line}")
    say.emit()
    say.emit(paint("Do it for real", PINK, BOLD, color=say.color))
    width = max(len(cmd) for cmd, _ in NEXT_STEPS)
    for cmd, why in NEXT_STEPS:
        say.emit(f"  {paint(cmd.ljust(width), TEAL, color=say.color)}  {paint(why, DIM, color=say.color)}")
    say.emit()
    say.emit(paint("Cleaned up: the demo's temp folder is gone. No real mount, volume or file was touched.",
                   DIM, color=say.color))
    return {"sandbox_removed": removed, "touched_real_mounts": False}


def run_demo(*, out=None, color: bool = False, fast: bool = True, quiet: bool = False, sleep=time.sleep,
             start: datetime | None = None) -> Result:
    """Run the tour in a fresh temp folder and remove it afterwards, whatever happens."""
    out = out or sys.stdout
    root = tempfile.mkdtemp(prefix="motel-try-")
    removed = False
    try:
        start = start or datetime.now(UTC).replace(minute=0, second=0, microsecond=0)
        tour = Tour(root, start)
        say = Narrator(out, color=color, fast=fast, quiet=quiet, sleep=sleep, tour=tour)
        story(tour, say)
        calls = [list(c) for c in tour.sysops.calls]
        provider_calls = [list(c[:1]) for c in tour.provider.calls]
        transcript = {"schema": SCHEMA, "ok": True, "version": __version__, "simulated": True,
                      "engine": "real rooms engine, fake provider, fake system operations, simulated clock",
                      "started": start.strftime("%Y-%m-%dT%H:%M:%SZ"), "ended": tour.stamp(), "steps": say.steps}
    finally:
        shutil.rmtree(root, ignore_errors=True)
        removed = not os.path.exists(root)
    cleanup = epilogue(say, removed, root)
    transcript.update(cleanup=cleanup, learned=list(LEARNED), next=[{"command": c, "why": w} for c, w in NEXT_STEPS])
    return Result(transcript=transcript, sandbox=root, removed=removed, sysops_calls=calls, provider_calls=provider_calls)


def main(rest: list | None = None, *, out=None, err=None, sleep=time.sleep) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    rest = list(sys.argv[2:] if rest is None else rest)
    try:
        args = build_parser().parse_args(rest)
    except Usage as exc:
        print(json.dumps(exc.as_json()) if "--json" in rest else exc.line(), file=out if "--json" in rest else err)
        return exc.code
    color = (not args.json) and want_color(out, args.no_color)
    try:
        result = run_demo(out=out, color=color, fast=args.fast or args.json, quiet=args.json, sleep=sleep)
    except KeyboardInterrupt:
        print("\nmotel: tour interrupted; the temp folder was removed. Try: motel try", file=err)
        return 130
    if args.json:
        print(json.dumps(result.transcript, indent=1, sort_keys=True), file=out)
    return 0
