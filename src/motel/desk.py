"""The front desk: the root-owned helper (``motel-desk``).

Run through one narrow sudo rule by the unprivileged ``motel`` CLI, by the housekeeping timer and by the
operator. It owns the cloud token, the lock, the state, the provider calls and the mounts.

Nothing here reads the environment: the installed desk's paths, provider and identity checks are fixed.
Tests call :func:`run` with explicit paths, fakes and a caller.
"""

from __future__ import annotations

import argparse
import json
import os
import sys

from motel.config import Config, Paths, Receipt, check_root_owned, same_id
from motel.doctor import run_doctor
from motel.errors import EXIT_OK, Guard, MotelError, NotSetUp, Refused, SystemFault, Usage
from motel.providers import registry
from motel.providers.local import LocalProvider
from motel.render import render
from motel.rooms import Caller, Ctx, Rooms
from motel.setup import setup_cloud, setup_local
from motel.state import SCHEMA, Store
from motel.sysops import RealSysOps
from motel.units import check_room, check_task, parse_duration, parse_size_gb, utc_now

USER_VERBS = ("checkin", "ls", "status", "extend", "checkout", "restore", "doctor")
OPERATOR_VERBS = ("reap", "setup")


class DeskParser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message)

    def exit(self, status=0, message=None):
        if status:
            raise Usage((message or "").strip())
        raise SystemExit(status)


def build_parser() -> argparse.ArgumentParser:
    common = DeskParser(add_help=False, allow_abbrev=False)
    common.add_argument("--json", action="store_true")
    parser = DeskParser(prog="motel-desk", allow_abbrev=False, description="motel front desk (root helper)")
    sub = parser.add_subparsers(dest="verb", required=True, parser_class=DeskParser)
    p = sub.add_parser("checkin", parents=[common], allow_abbrev=False)
    p.add_argument("--size", required=True)
    p.add_argument("--for", dest="ttl", default=None)
    p.add_argument("--task", default="room")
    p.add_argument("--suite", action="store_true")
    p = sub.add_parser("ls", parents=[common], allow_abbrev=False)
    p.add_argument("--all", action="store_true")
    p = sub.add_parser("status", parents=[common], allow_abbrev=False)
    p.add_argument("room")
    p = sub.add_parser("extend", parents=[common], allow_abbrev=False)
    p.add_argument("room")
    p.add_argument("--for", dest="ttl", required=True)
    p = sub.add_parser("checkout", parents=[common], allow_abbrev=False)
    p.add_argument("room")
    p.add_argument("--now", action="store_true")
    p = sub.add_parser("restore", parents=[common], allow_abbrev=False)
    p.add_argument("room")
    p.add_argument("--for", dest="ttl", default=None)
    sub.add_parser("doctor", parents=[common], allow_abbrev=False)
    p = sub.add_parser("reap", parents=[common], allow_abbrev=False)
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("setup", parents=[common], allow_abbrev=False)
    p.add_argument("provider", choices=registry.provider_names())
    p.add_argument("--token-file", default=None,
                   help="clouds that use a token: read it from this root-owned 0600 file (automation)")
    return parser


# --------------------------------------------------------------------------- wiring
def default_provider(cfg: Config, paths: Paths):
    """The provider ``cfg.provider`` names: a row of ``motel.providers.registry`` (``local`` is built here)."""
    if cfg.provider == registry.LOCAL:
        return LocalProvider(paths.real(cfg.local_dir), strict=paths.strict)
    spec = registry.get(cfg.provider)
    token = None
    if spec.needs_token:
        token_path = paths.token_file(spec.name)
        if not os.path.exists(token_path):
            raise NotSetUp(f"no {spec.label} token installed at {token_path}", f"sudo motel-desk setup {spec.name}")
        if paths.strict:
            check_root_owned(token_path, secret=True)
        with open(token_path, encoding="utf-8") as handle:
            token = handle.read().strip()
    return registry.build(cfg, paths, token)


def build_ctx(paths: Paths, make_provider, sysops, clock) -> Ctx:
    if paths.strict and os.path.exists(paths.config_file):
        check_root_owned(paths.config_file)
    cfg = Config.load(paths.config_file)
    receipt = Receipt.load(paths.receipt_file)
    if paths.strict:
        check_root_owned(paths.receipt_file)
    if receipt.provider != cfg.provider:
        raise Guard(f"the config says provider {cfg.provider!r} but setup was done for {receipt.provider!r}", "motel doctor")
    if cfg.server_id != "auto" and not same_id(cfg.server_id, receipt.server_id):
        raise Guard(f"the config names server {cfg.server_id} but setup was done for {receipt.server_id}", "motel doctor")
    provider = make_provider(cfg, paths)
    return Ctx(cfg=cfg, receipt=receipt, provider=provider, sysops=sysops, store=Store(paths.state_dir, clock, receipt.server_id),
               paths=paths, clock=clock)


def groups_of(caller: Caller) -> set:
    import grp
    names = set()
    try:
        for gid in os.getgrouplist(caller.name, caller.gid):
            try:
                names.add(grp.getgrgid(gid).gr_name)
            except KeyError:
                pass
    except OSError:
        pass
    return names


def is_allowed(cfg: Config, caller: Caller, groups_fn=groups_of) -> bool:
    if caller.uid == 0:
        return True
    users = {e for e in cfg.allow_users if not e.startswith("@")}
    wanted_groups = {e[1:] for e in cfg.allow_users if e.startswith("@")}
    return caller.name in users or bool(wanted_groups & groups_fn(caller))


def real_caller() -> Caller:
    import pwd
    if os.geteuid() != 0:
        raise Guard("motel-desk must run as root (use `motel`, which calls it through sudo)", "motel doctor")
    raw = os.environ.get("SUDO_UID", "")
    uid = int(raw) if raw.isdigit() else 0
    try:
        entry = pwd.getpwuid(uid)
        return Caller(uid=uid, gid=entry.pw_gid, name=entry.pw_name, operator=True)
    except KeyError:
        raise Guard(f"uid {uid} has no account on this host", "motel doctor") from None


def rooms_in_use(paths: Paths) -> bool:
    """True when this host was set up or still holds a room or floor record (then a reap must run, or fail loudly)."""
    try:
        os.lstat(paths.receipt_file)
        return True
    except FileNotFoundError:
        pass
    except OSError:
        return True  # a receipt that cannot be checked is not proof of a fresh host
    for sub in ("rooms", "floors"):
        try:
            if any(not name.startswith(".") for name in os.listdir(os.path.join(paths.state_dir, sub))):
                return True
        except FileNotFoundError:
            continue
        except OSError:
            return True  # unreadable state: let the real reap report it
    return False


# --------------------------------------------------------------------------- dispatch
def execute(args, *, paths: Paths, caller: Caller, make_provider, sysops, clock, groups_fn=groups_of,
            hetzner_factory=None, provider_factory=None, local_factory=None, token_reader=None, token_file_uid: int = 0,
            concierge_health=None) -> dict:
    verb = args.verb
    # Operator verbs need a desk running as root (euid 0, which real_caller verifies) -- including `sudo motel-desk
    # setup`, where SUDO_UID names the admin. The sudoers rule pins the first argument to the user verbs, so a
    # user in allow_users cannot reach these through sudo.
    if verb in OPERATOR_VERBS and not (caller.uid == 0 or caller.operator):
        raise Refused(f"`{verb}` is an operator command", "motel doctor")
    if verb == "setup":
        if args.provider == registry.LOCAL:
            if args.token_file:
                raise Usage("--token-file applies to clouds that use a token", "sudo motel-desk setup local")
            cfg = Config.load(paths.config_file)
            return setup_local(paths, provider_factory=local_factory or (lambda: LocalProvider(paths.real(cfg.local_dir), strict=paths.strict)),
                               clock=clock)
        spec = registry.get(args.provider)
        if args.token_file and not spec.needs_token:
            raise Usage(f"{spec.label} needs no token: it uses the identity of this machine",
                        f"sudo motel-desk setup {spec.name}")
        if args.token_file:
            from motel.setup import read_token_file
            token_reader = (lambda path=args.token_file: read_token_file(path, owner_uid=token_file_uid, provider=spec.name))
        elif token_reader is None and spec.needs_token:
            from motel.setup import read_token_tty
            token_reader = lambda: read_token_tty(provider=spec.name)  # noqa: E731
        factory = provider_factory or (hetzner_factory if spec.name == "hetzner" else None)
        if factory is None:
            cfg = Config.load(paths.config_file)
            factory = lambda token: registry.build(cfg, paths, token)  # noqa: E731
        return setup_cloud(paths, spec.name, provider_factory=factory, token_reader=token_reader, clock=clock)
    if verb == "doctor":
        return run_doctor(paths, lambda: build_ctx(paths, make_provider, sysops, clock), concierge_health=concierge_health)
    if verb == "reap" and not rooms_in_use(paths):
        # The housekeeping timer is installed before `setup` runs: no receipt and no room or floor record means
        # there is nothing to reap, which is not a failure (a failed unit right after install only alarms people).
        return {"schema": SCHEMA, "ok": True, "apply": bool(args.apply), "set_up": False, "host": None,
                "run_at": clock().strftime("%Y-%m-%dT%H:%M:%SZ"),
                "note": "motel is not set up on this host and holds no rooms: nothing to reap",
                "actions": [], "warnings": [], "refused": [], "orphans": [], "errors": [], "floors": [], "rooms": [],
                "held": []}
    ctx = build_ctx(paths, make_provider, sysops, clock)
    if verb not in OPERATOR_VERBS and not is_allowed(ctx.cfg, caller, groups_fn):
        raise Refused(f"{caller.name} is not in allow_users", "motel doctor")
    rooms = Rooms(ctx)
    if verb == "reap":
        return rooms.reap(args.apply)
    if verb == "checkin":
        ttl = parse_duration(args.ttl) if args.ttl else ctx.cfg.default_ttl
        room = rooms.checkin(caller, parse_size_gb(args.size), ttl, check_task(args.task), args.suite)
        return {"schema": SCHEMA, "ok": True, "room": room}
    if verb == "ls":
        return {"schema": SCHEMA, "ok": True, **rooms.ls(caller, args.all)}
    room_id = check_room(args.room)
    if verb == "status":
        return {"schema": SCHEMA, "ok": True, "room": rooms.status(caller, room_id)}
    if verb == "extend":
        return {"schema": SCHEMA, "ok": True, "room": rooms.extend(caller, room_id, parse_duration(args.ttl))}
    if verb == "checkout":
        return {"schema": SCHEMA, "ok": True, "room": rooms.checkout(caller, room_id, args.now)}
    if verb == "restore":
        ttl = parse_duration(args.ttl) if args.ttl else None
        return {"schema": SCHEMA, "ok": True, "room": rooms.restore(caller, room_id, ttl)}
    raise Usage(f"unknown verb {verb}")  # unreachable: argparse restricts verbs


def run(argv, *, paths: Paths, caller: Caller, make_provider=default_provider, sysops=None, clock=utc_now,
        out=None, err=None, **hooks) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    want_json = "--json" in argv
    try:
        args = build_parser().parse_args(argv)
        doc = execute(args, paths=paths, caller=caller, make_provider=lambda cfg, p: make_provider(cfg, p),
                      sysops=sysops or RealSysOps(scan_marker=os.path.join(paths.state_dir, ".abandoned-scan")), clock=clock, **hooks)
    except MotelError as exc:
        if want_json:
            print(json.dumps(exc.as_json()), file=out)
        else:
            print(exc.line(), file=err)
        return exc.code
    except Exception as exc:  # noqa: BLE001  never leak a traceback to the caller
        fault = SystemFault(f"internal error: {exc.__class__.__name__}: {exc}")
        print(json.dumps(fault.as_json()) if want_json else fault.line(), file=out if want_json else err)
        return fault.code
    if want_json:
        print(json.dumps(doc, indent=1, sort_keys=True), file=out)
    else:
        print(render(args.verb, doc), file=out)
    if args.verb == "doctor" and not doc["ok"]:
        return SystemFault.code
    if args.verb == "reap" and not doc["ok"]:
        return SystemFault.code
    return EXIT_OK


def main(argv=None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    os.umask(0o022)
    if argv and argv[0] == "cabin":  # `motel-desk cabin <verb>`: the cabin desk validates its own verbs and allowlist
        from motel.cabins.desk import main as cabin_main
        return int(cabin_main(argv[1:]) or 0)
    if argv and argv[0] == "tour":  # `motel-desk tour <verb>`: the tour desk validates its own verbs
        from motel.tours.desk import main as tour_main
        return int(tour_main(argv[1:]) or 0)
    try:
        caller = real_caller()
    except MotelError as exc:
        print(json.dumps(exc.as_json()) if "--json" in argv else exc.line(), file=sys.stderr if "--json" not in argv else sys.stdout)
        return exc.code
    return run(argv, paths=Paths(), caller=caller)


if __name__ == "__main__":
    raise SystemExit(main())
