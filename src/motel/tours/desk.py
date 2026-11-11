"""The tour side of the front desk: ``motel-desk tour ...`` (root).

``main(argv)`` takes the arguments AFTER the word ``tour``. User verbs are pinned one per line in the sudoers rule;
operator verbs (``supervise``, ``reap``, ``bake``, ``image``, ``own-login``) need root. The desk validates every
field of every request. Nothing here reads the environment: paths, endpoints and credentials are fixed. Tests call
:func:`run` with explicit paths, fakes and a caller.
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import time

from motel.cabins.config import CabinPaths, load_cleanup_config, load_config
from motel.cabins.desk import Caller as CabinCaller
from motel.cabins.desk import build_controller, cabin_user, groups_of, is_allowed, real_caller
from motel.cabins.errors import EXIT_INCOMPLETE, EXIT_OK, Fail, Guarded
from motel.cabins.guestchan import SYSTEM_USER
from motel.cabins.userdata import ssh_keygen
from motel.cabins.util import SCHEMA, utcnow
from motel.tours import bundle, deps, ownlogin
from motel.tours.config import load_cleanup_limits, load_limits, new_attempts_allowed, new_attempts_block
from motel.tours.errors import Refused, Unavailable, Usage
from motel.tours.pipeline import Env
from motel.tours.service import RawReply, Service
from motel.tours.slots import SlotPool
from motel.tours.state import TourStore
from motel.tours.transport import SshTransport

USER_VERBS = ("start", "ls", "status", "wait", "result", "fetch", "logs", "cancel")
OPERATOR_VERBS = ("supervise", "reap", "bake", "image", "own-login")
DESK = "/usr/local/sbin/motel-desk"
SAFE_ENV = {"PATH": "/usr/sbin:/usr/bin:/sbin:/bin", "LANG": "C.UTF-8"}
MAX_REQUEST = 1024 * 1024


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
    parser = DeskParser(prog="motel-desk tour", allow_abbrev=False, description="motel front desk: tours")
    sub = parser.add_subparsers(dest="verb", required=True, parser_class=DeskParser)
    sub.add_parser("start", parents=[common], allow_abbrev=False)
    p = sub.add_parser("ls", parents=[common], allow_abbrev=False)
    p.add_argument("--all", action="store_true")
    for name in ("status", "cancel", "supervise"):
        p = sub.add_parser(name, parents=[common], allow_abbrev=False)
        p.add_argument("tour")
    p = sub.add_parser("wait", parents=[common], allow_abbrev=False)
    p.add_argument("tour")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--any", dest="mode", action="store_const", const="any")
    group.add_argument("--all", dest="mode", action="store_const", const="all")
    p.add_argument("--timeout", type=float, default=3600.0)
    p.set_defaults(mode="all")
    p = sub.add_parser("result", parents=[common], allow_abbrev=False)
    p.add_argument("tour")
    p.add_argument("guest")
    p = sub.add_parser("fetch", parents=[common], allow_abbrev=False)
    p.add_argument("tour")
    p.add_argument("guest")
    p.add_argument("--artifact", default=None)
    p = sub.add_parser("logs", parents=[common], allow_abbrev=False)
    p.add_argument("tour")
    p.add_argument("guest")
    p.add_argument("--stream", default="stdout", choices=("stdout", "stderr", "runner", "boot"))
    p.add_argument("--tail", type=int, default=64 * 1024)
    p = sub.add_parser("reap", parents=[common], allow_abbrev=False)
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("bake", parents=[common], allow_abbrev=False)
    p.add_argument("name")
    p.add_argument("--script", required=True, help="a root-owned setup script on this host")
    p.add_argument("--type", dest="server_type", default=None)
    p = sub.add_parser("image", parents=[common], allow_abbrev=False)
    isub = p.add_subparsers(dest="action", required=True, parser_class=DeskParser)
    isub.add_parser("ls", parents=[common], allow_abbrev=False)
    q = isub.add_parser("rm", parents=[common], allow_abbrev=False)
    q.add_argument("image")
    p = sub.add_parser("own-login", parents=[common], allow_abbrev=False)
    osub = p.add_subparsers(dest="action", required=True, parser_class=DeskParser)
    q = osub.add_parser("set", parents=[common], allow_abbrev=False)
    q.add_argument("--stdin", action="store_true", required=True, help="read the token from standard input")
    osub.add_parser("status", parents=[common], allow_abbrev=False)
    osub.add_parser("clear", parents=[common], allow_abbrev=False)
    return parser


# --------------------------------------------------------------------------- real collaborators
def systemd_launcher(desk: str = DESK, runner=subprocess.run):
    def launch(tour_id: str) -> str:
        unit = f"motel-tour-{tour_id}"
        proc = runner(["systemd-run", "--unit", unit, "--description", f"motel tour {tour_id}", "--collect",
                       "--property=TimeoutStopSec=300", desk, "tour", "supervise", tour_id],
                      capture_output=True, stdin=subprocess.DEVNULL, env=SAFE_ENV, check=False, shell=False)
        if proc.returncode != 0:
            raise Unavailable(f"systemd-run could not start {unit}")
        return unit
    return launch


def systemd_unit_active(runner=subprocess.run):
    def active(tour_id: str) -> bool:
        try:
            proc = runner(["systemctl", "is-active", "--quiet", f"motel-tour-{tour_id}"], stdin=subprocess.DEVNULL,
                          capture_output=True, env=SAFE_ENV, check=False, shell=False)
        except OSError:
            return False
        return proc.returncode == 0
    return active


INSPECT_ROOT = bundle.INSPECT_ROOT


def _inspector(paths: CabinPaths):
    """What reads a guest's bundle with git: never as root (user ``motel-inspect``), in per-attempt directories under
    ``bundle.INSPECT_ROOT``, under hard limits (see ``bundle.Sandbox``)."""
    work_root = paths.real(bundle.INSPECT_ROOT)

    def inspect(base_bundle, result_bundle, base_sha, branch, attempt, token, deliver_to=None):
        return bundle.inspect_result(base_bundle, result_bundle, base_sha, branch, work_root=work_root, attempt=attempt, token=token,
                                     deliver_to=deliver_to)
    return inspect


def make_env_factory(paths: CabinPaths, store: TourStore, limits, cabin_cfg, clock, controller_hooks: dict, transport=None,
                     concierge_factory=None):
    def factory() -> Env:
        def controller(owner: dict, cleanup_only: bool = False):
            caller = CabinCaller(uid=owner["uid"], gid=0, name=owner["name"], operator=True)
            return build_controller(paths, caller, require_enabled=not cleanup_only, cleanup_only=cleanup_only,
                                    clock=clock, **controller_hooks)
        return Env(
            store=store, limits=limits, cabin_cfg=cabin_cfg, controller_factory=controller,
            concierge=(concierge_factory or deps.concierge_client)(),
            transport=transport or SshTransport(run_as=cabin_user() or (SYSTEM_USER, None, None),
                                                runner_argv=deps.runner_argv()),
            keygen=ssh_keygen, extras=lambda **kw: deps.userdata_extras(**kw), runner_files=lambda: deps.runner_files(),
            token_reader=lambda: ownlogin.read_token(limits.own_login_token_file),
            inspect_bundle=_inspector(paths), inspect_root=paths.real(bundle.INSPECT_ROOT),
            accepting=lambda: new_attempts_allowed(paths.config_file), block_reason=lambda: new_attempts_block(paths.config_file),
            slots=SlotPool(os.path.join(store.root, ".slots"), limits.max_concurrent_guests),
            own_slots=SlotPool(os.path.join(store.root, ".own-login-slots"), limits.own_login_max_concurrent), clock=clock)
    return factory


# --------------------------------------------------------------------------- dispatch
def _read_stdin(stream, cap: int) -> bytes:
    data = stream.read(cap + 1)
    if isinstance(data, str):
        data = data.encode()
    if len(data) > cap:
        raise Usage("the request on standard input is too large")
    return data


# User verbs an operator (root, or an administrator who is not a tour user) may also run, on every user's tours.
# `start` and `fetch` stay with tour users: they act in the caller's own name and repository.
OPERATOR_MAY_USE = frozenset({"ls", "status", "wait", "result", "logs", "cancel"})


def is_operator(caller: CabinCaller, cabin_cfg, groups_fn=groups_of) -> bool:
    """Who may run the operator verbs and see everyone's tours.

    The sudoers rule is the gate: it pins each user verb and offers the operator verbs to nobody but administrators.
    This is the second check. ``real_caller`` marks every process that runs as root through sudo as ``operator``,
    including a user of the allow list, so that flag alone says nothing about the person. Here root is an operator,
    and so is an administrator who is NOT a tour user (not in ``allow_users``); a tour user is never one."""
    if caller.uid == 0:
        return True
    return bool(caller.operator) and not is_allowed(cabin_cfg, caller, groups_fn)


def execute(args, *, paths: CabinPaths, caller: CabinCaller, stdin=None, groups_fn=groups_of, clock=utcnow, service=None,
            controller_hooks=None, **hooks):
    verb = args.verb
    config = paths.config_file
    # `start` needs a valid [tours]. Everything else (reads, cancel, reap, supervise) must keep working, and keep
    # cleaning up, whatever state the table is in: a broken config never keeps a cabin alive.
    limits = hooks.get("limits") or (load_limits(config, require_enabled=False) if verb == "start" else load_cleanup_limits(config))
    if service is None:
        cabin_cfg = hooks.get("cabin_cfg") or (load_config(config, require_enabled=True) if verb == "start"
                                               else load_cleanup_config(config))
        store = hooks.get("store") or TourStore(paths.real("/var/lib/motel/tours"), clock)
        service = Service(
            store=store, limits=limits, cabin_cfg=cabin_cfg, clock=clock,
            launcher=hooks.get("launcher") or systemd_launcher(), unit_active=hooks.get("unit_active") or systemd_unit_active(),
            env_factory=hooks.get("env_factory") or make_env_factory(paths, store, limits, cabin_cfg, clock, controller_hooks or {},
                                                                    hooks.get("transport"), hooks.get("concierge_factory")),
            concierge_factory=hooks.get("concierge_factory") or deps.concierge_client, sleep=hooks.get("sleep") or time.sleep,
            groups_fn=lambda c: groups_fn(c), operator_fn=lambda c: is_operator(c, service.cabin_cfg, groups_fn),
            inspect_root=paths.real(bundle.INSPECT_ROOT),
            setup_check=hooks.get("setup_check") or (lambda: bundle.setup_problems(paths.real(bundle.INSPECT_ROOT))))
    operator = is_operator(caller, service.cabin_cfg, groups_fn)
    if verb in OPERATOR_VERBS and not operator:
        raise Refused(f"`{verb}` is an operator command", "sudo motel-desk tour " + verb)
    if verb not in OPERATOR_VERBS and not (verb in OPERATOR_MAY_USE and operator) and not is_allowed(service.cabin_cfg, caller, groups_fn):
        raise Refused(f"{caller.name} is not in allow_users", "sudo motel-desk cabin doctor")
    if verb == "start":
        if not limits.enabled:
            raise Refused("tours are disabled on this host", "ask the operator to set enabled = true in [tours]")
        try:
            payload = json.loads(_read_stdin(stdin or sys.stdin.buffer, MAX_REQUEST))
        except ValueError:
            raise Usage("the front desk got an unreadable tour request", "motel tour start --help") from None
        return service.start(caller, payload)
    if verb == "ls":
        return service.ls(caller, args.all)
    if verb == "status":
        return service.status(caller, args.tour)
    if verb == "wait":
        return service.wait(caller, args.tour, args.mode, max(0.0, min(args.timeout, 6 * 3600.0)))
    if verb == "result":
        return service.result(caller, args.tour, args.guest)
    if verb == "fetch":
        return service.fetch(caller, args.tour, args.guest, args.artifact)
    if verb == "logs":
        return service.logs(caller, args.tour, args.guest, args.stream, args.tail)
    if verb == "cancel":
        return service.cancel(caller, args.tour)
    if verb == "supervise":
        return service.supervise(args.tour)
    if verb == "reap":
        return service.reap(args.apply)
    if verb == "own-login":
        data = _read_stdin(stdin or sys.stdin.buffer, ownlogin.MAX_BYTES) if args.action == "set" else None
        return service.own_login(args.action, data)
    if verb in ("bake", "image"):
        return _images(args, service, paths, caller, hooks, controller_hooks or {}, clock)
    raise Usage(f"unknown verb {verb}")  # unreachable: argparse restricts verbs


def _images(args, service, paths, caller, hooks, controller_hooks, clock) -> dict:
    """``bake`` and ``image``: the image bake is the guest part (``motel.tours.bake``); the engine passes a controller."""
    bake = hooks.get("bake") or deps.bake_module()
    ctl = build_controller(paths, caller, require_enabled=False, clock=clock, **controller_hooks)
    if args.verb == "bake":
        return {"schema": SCHEMA, "ok": True, "image": bake.bake(ctl, args.script, args.name, server_type=args.server_type)}
    if args.action == "ls":
        return {"schema": SCHEMA, "ok": True, "images": bake.images(ctl)}
    if not args.image.isdigit():
        raise Usage("an image id is a positive integer", "sudo motel-desk tour image ls")
    try:
        removed = bake.delete(ctl, int(args.image))
    except Guarded as exc:
        if "no creation record" not in exc.message:
            raise
        # bake.delete only removes images this host baked (it needs the creation record `image ls` shows as recorded).
        raise Guarded(f"image {args.image} was not baked on this host (recorded: false in `tour image ls`), so it is not removed here",
                      "sudo motel-desk tour image ls  (delete other images in the provider console)") from None
    return {"schema": SCHEMA, "ok": True, "removed": removed}


def _exit_code(verb, doc) -> int:
    if isinstance(doc, RawReply):
        return EXIT_OK
    if verb == "wait" and not doc.get("satisfied", True):
        return EXIT_INCOMPLETE
    if verb == "reap" and not doc["ok"]:
        return EXIT_INCOMPLETE
    return EXIT_OK


def run(argv, *, paths: CabinPaths, caller: CabinCaller, out=None, err=None, **hooks) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    argv = list(argv)
    want_json = "--json" in argv
    try:
        args = build_parser().parse_args(argv)
        doc = execute(args, paths=paths, caller=caller, **hooks)
    except Fail as exc:
        print(json.dumps(exc.as_json()) if want_json else exc.line(), file=out if want_json else err)
        return exc.code
    except Exception as exc:  # noqa: BLE001  never leak a traceback to the caller
        e = Fail(f"internal error: {exc.__class__.__name__}")
        print(json.dumps(e.as_json()) if want_json else e.line(), file=out if want_json else err)
        return e.code
    if isinstance(doc, RawReply):
        stream = getattr(out, "buffer", out)
        stream.write(doc.data)
        stream.flush()
        return EXIT_OK
    print(json.dumps(doc, indent=1, sort_keys=True), file=out)
    return _exit_code(args.verb, doc)


def main(argv=None) -> int:
    """Entry point for ``motel-desk tour ...``; ``argv`` is what follows the word ``tour``."""
    argv = list(sys.argv[1:] if argv is None else argv)
    os.umask(0o077)
    try:
        caller = real_caller()
    except Fail as exc:
        print(json.dumps(exc.as_json()) if "--json" in argv else exc.line(), file=sys.stdout if "--json" in argv else sys.stderr)
        return exc.code
    return run(argv, paths=CabinPaths(), caller=caller)


if __name__ == "__main__":
    raise SystemExit(main())
