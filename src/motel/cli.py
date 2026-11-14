"""The unprivileged ``motel`` command.

It validates every argument, then calls the root-owned front desk through ``sudo -n``. It never sees the
cloud token and never passes raw paths or provider ids: only sizes, durations, task names and room ids.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys

from motel import __version__
from motel.errors import EXIT_ERROR, MotelError, NotSetUp, SystemFault, Usage, desk_refusal, not_installed  # noqa: F401
from motel.render import render, warnings_for
from motel.units import canonical_minutes, check_room, check_task, parse_duration, parse_size_gb

DESK = "/usr/local/sbin/motel-desk"
SUDO_CANDIDATES = ("/usr/bin/sudo", "/bin/sudo", "/usr/local/bin/sudo")
SAFE_ENV = {"PATH": "/usr/bin:/bin"}


class CliParser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message)

    def exit(self, status=0, message=None):
        if status:
            raise Usage((message or "").strip())
        raise SystemExit(status)


# `motel --help`: every top-level command, grouped. The room verbs come from the parser itself; the commands that are
# routed to their own modules (ROUTES) are described here. A test checks that both sets are listed in full.
ROOM_VERBS = ("checkin", "ls", "extend", "checkout", "restore")
HELP_GROUPS = (
    ("cabins (experimental: disposable servers in your own cloud account)", (
        ("cabin", "run a command on a fresh server and delete it: motel cabin --help"),)),
    ("tours (experimental: swarms of bounded agent jobs, one fresh cabin per guest)", (
        ("tour", "start a tour, wait for it, fetch its results: motel tour --help"),)),
    ("setup", (
        ("try", "a 30-second offline demo (try --cabin: a cabin run and a tour, with receipts)"),
        ("install", "install the front desk on this host (run as root)"),
        ("doctor", "check the installation"),
        ("mcp", "run the MCP server for your agent"),
        ("skill", "print or install the agent skill (SKILL.md)"))),
)


class TopParser(CliParser):
    def format_help(self) -> str:
        aliases = {"checkin": "acquire", "ls": "status", "checkout": "release"}
        width = max(len(n) + (len(aliases[n]) + 3 if n in aliases else 0) for n in ROOM_VERBS)
        width = max([width] + [len(n) for _, items in HELP_GROUPS for n, _ in items])
        lines = ["usage: motel [-h] [--version] COMMAND ...", "", self.description, "", "rooms (temporary private folders on cloud volumes)"]
        for verb in ROOM_VERBS:
            label = verb + (f" ({aliases[verb]})" if verb in aliases else "")
            lines.append(f"  {label.ljust(width)}  {self.verb_help[verb]}")
        for title, items in HELP_GROUPS:
            lines += ["", title]
            lines += [f"  {name.ljust(width)}  {text}" for name, text in items]
        lines += ["", "options:", "  -h, --help  show this help message and exit", "  --version   print the version", "",
                  "Every command takes --help. Start here: motel try"]
        return "\n".join(lines) + "\n"


def build_parser() -> argparse.ArgumentParser:
    common = CliParser(add_help=False, allow_abbrev=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    parser = TopParser(prog="motel", allow_abbrev=False,
                       description="Disk and servers by the hour, for agents. Rooms: temporary private directories on temporary volumes.")
    parser.add_argument("--version", action="store_true", help="print the version")
    sub = parser.add_subparsers(dest="verb", parser_class=CliParser, metavar="VERB")
    p = sub.add_parser("checkin", aliases=["acquire"], parents=[common], allow_abbrev=False, help="take a room; prints its path")
    p.add_argument("size", help="e.g. 200G, 1T or a plain number of GB")
    p.add_argument("--for", dest="ttl", default=None, help="checkout time from now, e.g. 90m, 6h, 3d (default 6h)")
    p.add_argument("--task", default="room", help="short name for the room id (default: room)")
    p.add_argument("--suite", action="store_true", help="give the room a volume of its own")
    p = sub.add_parser("ls", aliases=["status"], parents=[common], allow_abbrev=False,
                       help="list rooms (with ROOM: show one room)")
    p.add_argument("room", nargs="?", help="a room id; omit to list")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--mine", action="store_true", help="only your rooms (default)")
    group.add_argument("--all", action="store_true", help="everyone's rooms")
    p = sub.add_parser("extend", parents=[common], allow_abbrev=False, help="late checkout")
    p.add_argument("room")
    p.add_argument("--for", dest="ttl", required=True, help="e.g. 6h")
    p = sub.add_parser("checkout", aliases=["release"], parents=[common], allow_abbrev=False,
                       help="give the room back (lost & found, or --now to delete)")
    p.add_argument("room")
    p.add_argument("--now", action="store_true", help="delete the data immediately")
    p = sub.add_parser("restore", parents=[common], allow_abbrev=False, help="bring a room back from lost & found")
    p.add_argument("room")
    p.add_argument("--for", dest="ttl", default=None, help="new checkout time (default 6h)")
    sub.add_parser("doctor", parents=[common], allow_abbrev=False, help="check the installation")
    parser.verb_help = {a.dest: a.help for a in sub._choices_actions}
    return parser


def desk_argv(args) -> list:
    """Translate validated CLI arguments into front-desk arguments (canonical units only)."""
    verb = {"acquire": "checkin", "release": "checkout"}.get(args.verb, args.verb)
    if verb == "checkin":
        argv = ["checkin", "--size", f"{parse_size_gb(args.size)}G", "--task", check_task(args.task)]
        if args.ttl:
            argv += ["--for", canonical_minutes(parse_duration(args.ttl, clamp=True))]
        if args.suite:
            argv.append("--suite")
    elif verb in ("ls", "status") and not getattr(args, "room", None):
        argv = ["ls"] + (["--all"] if args.all else [])
    elif verb in ("ls", "status"):
        argv = ["status", check_room(args.room)]
    elif verb == "extend":
        argv = ["extend", check_room(args.room), "--for", canonical_minutes(parse_duration(args.ttl, clamp=True))]
    elif verb == "checkout":
        argv = ["checkout", check_room(args.room)] + (["--now"] if args.now else [])
    elif verb == "restore":
        argv = ["restore", check_room(args.room)] + (["--for", canonical_minutes(parse_duration(args.ttl, clamp=True))] if args.ttl else [])
    else:
        argv = ["doctor"]
    return argv + ["--json"]


ROUTES = {
    "cabin": ("motel.cabins.cli", ("motel.cabins", "motel.cabins.cli"), "cabins are not installed in this version of motel"),
    "tour": ("motel.tours.cli", ("motel.tours", "motel.tours.cli"), "tours are not installed in this version of motel"),
    "mcp": ("motel.mcp_server", ("motel.mcp_server",), "MCP server not installed in this version of motel"),
    "install": ("motel.installer", ("motel.installer",), "the installer is not part of this version of motel"),
    "skill": ("motel.skill", ("motel.skill",), "the agent skill is not part of this version of motel"),
    "try": ("motel.onboard.demo", ("motel.onboard", "motel.onboard.demo"), "the guided demo is not installed in this version of motel"),
}


def route(word: str, rest: list, out, err) -> int:
    """`motel cabin ...` and `motel mcp ...` are handled by optional modules: ``module.main(rest)``.

    ``rest`` is the argument list after the word. A missing module is exit 2; a module that is present but
    broken raises its own error.
    """
    import importlib
    module_name, missing_names, message = ROUTES[word]
    try:
        module = importlib.import_module(module_name)
    except ModuleNotFoundError as exc:
        if exc.name not in missing_names:
            raise
        error = Usage(message, "motel --help")
        print(json.dumps(error.as_json()) if "--json" in rest else error.line(), file=out if "--json" in rest else err)
        return error.code
    return int(module.main(rest) or 0)


def default_runner(argv: list) -> tuple:
    sudo = next((s for s in SUDO_CANDIDATES if os.path.exists(s)), None)
    if sudo is None or not os.path.exists(DESK):
        raise not_installed()
    proc = subprocess.run([sudo, "-n", DESK, *argv], capture_output=True, text=True, stdin=subprocess.DEVNULL,
                          env=SAFE_ENV, shell=False)
    return proc.returncode, proc.stdout, proc.stderr


def main(argv=None, *, runner=default_runner, out=None, err=None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    want_json = "--json" in argv
    if argv and argv[0] in ROUTES:
        return route(argv[0], argv[1:], out, err)
    if not argv:  # bare `motel` on a terminal: banner and "start here" (still exit 2, as before)
        from motel.onboard.banner import first_run
        if first_run(out):
            return Usage.code
    try:
        args = build_parser().parse_args(argv)
        if args.version:
            print(f"motel {__version__}", file=out)
            return 0
        if not args.verb:
            raise Usage("no verb given")
        code, stdout, stderr = runner(desk_argv(args))
        try:
            doc = json.loads(stdout)
            if not isinstance(doc, dict):
                raise ValueError
        except ValueError:
            if code == 1 and "sudo" in stderr:
                raise desk_refusal(stderr) from None
            raise SystemFault("the front desk returned no usable answer", "motel doctor") from None
    except MotelError as exc:
        print(json.dumps(exc.as_json()) if want_json else exc.line(), file=out if want_json else err)
        return exc.code
    if not doc.get("ok", False) and "error" in doc:
        if want_json:
            print(json.dumps(doc, indent=1, sort_keys=True), file=out)
        else:
            print(f"motel: {doc['error']}. Try: {doc.get('hint', 'motel doctor')}", file=err)
        return int(doc.get("code", code or EXIT_ERROR))
    verb = {"acquire": "checkin", "release": "checkout", "status": "ls" if not getattr(args, "room", None) else "status"}.get(args.verb, args.verb)
    for line in warnings_for(doc):
        print(line, file=err)
    if want_json:
        print(json.dumps(doc, indent=1, sort_keys=True), file=out)
    elif verb == "checkin":
        room = doc["room"]
        print(f"motel: room {room['id']} · {room['size_gb']} GB · checkout {room['expires']}. Disposable: copy results out "
              f"before checkout (extend: motel extend {room['id']} --for 6h)", file=err)
        print(room["path"], file=out)
    else:
        print(render(verb, doc), file=out)
    if verb == "doctor" and not doc.get("ok", True):
        return code or EXIT_ERROR
    return code


if __name__ == "__main__":
    raise SystemExit(main())
