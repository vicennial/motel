"""The cabin side of the front desk: ``motel-desk cabin ...`` (root).

Called through the core's narrow sudo rule, by the housekeeping timer and by the operator. ``main(argv)`` takes
the arguments AFTER the word ``cabin``. Nothing here reads the environment: paths, endpoints and credentials
are fixed. Tests call :func:`run` with explicit paths, fakes and a caller.

Verbs the sudoers rule may allow without a password: ``USER_VERBS``. ``OPERATOR_VERBS`` need root.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
from dataclasses import dataclass

from motel.cabins import setup as setup_mod
from motel.cabins.config import (
    CabinConfig,
    CabinPaths,
    load_cleanup_config,
    load_config,
)
from motel.cabins.controller import Controller
from motel.cabins.errors import (
    EXIT_OK,
    ApiError,
    Fail,
    Guarded,
    GuardRefused,
    NotFound,
    PolicyRefused,
    Prerequisite,
    Unavailable,
    Usage,
)
from motel.cabins.guestchan import SYSTEM_USER, GuestChannel, probe_tcp
from motel.cabins.provider import GuardedApi, Transport
from motel.cabins.render import render
from motel.cabins.store import Store, check_key_layout
from motel.cabins.tailnet import TailnetApi, TailnetTransport
from motel.cabins.util import (
    OWNER_RE,
    SCHEMA,
    parse_duration,
    parse_size_gb,
    sha256_text,
    utcnow,
)
from motel.errors import install_hint

USER_VERBS = ("checkin", "ls", "status", "extend", "checkout", "doctor", "images")
OPERATOR_VERBS = ("reap", "setup", "resolve")


@dataclass(frozen=True)
class Caller:
    uid: int
    gid: int
    name: str
    operator: bool = False      # True only for a verified euid-0 desk process (see real_caller)


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
    parser = DeskParser(prog="motel-desk cabin", allow_abbrev=False, description="motel front desk: cabins")
    sub = parser.add_subparsers(dest="verb", required=True, parser_class=DeskParser)
    p = sub.add_parser("checkin", parents=[common], allow_abbrev=False)
    p.add_argument("--type", dest="server_type", default=None)
    p.add_argument("--for", dest="ttl", default=None)
    p.add_argument("--task", required=True)
    p.add_argument("--client-key", required=True)
    p.add_argument("--disk", default=None)
    p.add_argument("--image", default=None, help="a baked image of this host, by name or numeric id")
    sub.add_parser("ls", parents=[common], allow_abbrev=False)
    sub.add_parser("images", parents=[common], allow_abbrev=False)
    p = sub.add_parser("status", parents=[common], allow_abbrev=False)
    p.add_argument("cabin")
    p = sub.add_parser("extend", parents=[common], allow_abbrev=False)
    p.add_argument("cabin")
    p.add_argument("--for", dest="ttl", required=True)
    p = sub.add_parser("checkout", parents=[common], allow_abbrev=False)
    p.add_argument("cabin")
    sub.add_parser("doctor", parents=[common], allow_abbrev=False)
    p = sub.add_parser("reap", parents=[common], allow_abbrev=False)
    p.add_argument("--apply", action="store_true")
    p = sub.add_parser("resolve", parents=[common], allow_abbrev=False)
    p.add_argument("cabin")
    p.add_argument("--forget-node", action="store_true", required=True,
                   help="give up on a tailnet node that cannot be proven absent, then finish cleanup")
    p = sub.add_parser("setup", parents=[common], allow_abbrev=False)
    p.add_argument("what", choices=("hetzner", "tailscale"))
    p.add_argument("--token-file", default=None,
                   help="hetzner only: read the token from this root-owned 0600 file (automation)")
    return parser


# --------------------------------------------------------------------------- wiring
def check_root_file(path: str, *, private: bool) -> None:
    """Root-owned regular file, not writable by others; private files also not readable by others."""
    st = os.lstat(path)
    if not stat.S_ISREG(st.st_mode) or st.st_uid != 0:
        raise Guarded(f"{path} must be a root-owned regular file", f"sudo chown root:root {path}")
    if stat.S_IMODE(st.st_mode) & (0o077 if private else 0o022):
        raise Guarded(f"{path} has unsafe permissions", f"sudo chmod {'600' if private else '644'} {path}")


def read_private(path: str, paths: CabinPaths) -> str:
    if paths.strict:
        check_root_file(path, private=True)
    with open(path, encoding="utf-8") as handle:
        return handle.read().strip()


def build_controller(paths: CabinPaths, caller: Caller, *, clock=utcnow, hetzner_factory=None, tailnet_factory=None,
                     guest=None, fetch=setup_mod.fetch_metadata, sleep=None, keygen=None, require_enabled=True,
                     cleanup_only=False, system_user=None, **extra) -> Controller:
    if paths.strict and os.path.exists(paths.config_file):
        check_root_file(paths.config_file, private=False)
    # Cleaning up leases that already exist must not depend on the config still being valid or enabled.
    cfg = (load_cleanup_config(paths.config_file) if cleanup_only
           else load_config(paths.config_file, require_enabled=require_enabled))
    receipt = setup_mod.load_receipt(paths)
    if paths.strict and os.path.exists(paths.receipt_file):
        check_root_file(paths.receipt_file, private=False)
    host_id = (receipt.get("host") or {}).get("server_id")
    hashes, hetzner, tailnet = {}, None, None
    token_path = paths.real(cfg.token_file)
    if host_id and os.path.exists(token_path):
        token = read_private(token_path, paths)
        hashes["hetzner"] = sha256_text(token)
        hetzner = (hetzner_factory or (lambda t, h: GuardedApi(Transport(t), h)))(token, host_id)
        del token
    if cfg.access == "tailscale" and cfg.tailscale and os.path.exists(paths.real(cfg.tailscale.oauth_file)):
        raw = read_private(paths.real(cfg.tailscale.oauth_file), paths)
        hashes["tailscale"] = sha256_text(raw)
        cred = json.loads(raw)
        del raw
        ts = cfg.tailscale
        protected = set(ts.protected_device_ids) | ({ts.controller_device_id} if ts.controller_device_id else set())
        tailnet = (tailnet_factory or (lambda c, t: TailnetApi(
            TailnetTransport(c.get("client_id"), c.get("client_secret")), tag=t.tag, tailnet=t.tailnet,
            protected_devices=protected)))(cred, ts)
        del cred
    template, agent = bootstrap_files()
    owner = system_user if system_user is not None else (cabin_user() if paths.strict else None)
    store = Store(paths.state_dir, clock, host_id, keys_dir=paths.keys_dir)
    # In the installed (strict) layout the guest ssh client must never run as root: without the system user the
    # channel refuses every call instead of falling back.
    run_as = owner if owner or not paths.strict else (SYSTEM_USER, None, None)
    gid = owner[2] if owner else None
    channel = guest or GuestChannel(store.keys, run_as=run_as,
                                    layout_check=lambda: check_key_layout(paths.keys_dir, gid))
    kwargs = {"receipt": receipt, "hetzner": hetzner, "tailnet": tailnet, "guest": channel,
                  "credential_hashes": hashes, "caller_uid": caller.uid, "clock": clock, "template": template,
                  "guest_agent": agent, "fetch_metadata": fetch, "tcp_probe": probe_tcp}
    if sleep is not None:
        kwargs["sleep"] = sleep
    if keygen is not None:
        kwargs["keygen"] = keygen
    kwargs.update(extra)
    return Controller(cfg, store, **kwargs)


def bootstrap_files() -> tuple:
    """(cloud-init template text, guest agent bytes) from the package data of ``motel.cabins``, or (None, None).

    ``importlib.resources`` finds them in a wheel, in the installed root-owned copy and in a source checkout alike.
    """
    from importlib import resources
    try:
        root = resources.files("motel.cabins")
        return (root.joinpath("cloud-init.yaml").read_text(encoding="utf-8"), root.joinpath("guest_agent.py").read_bytes())
    except (OSError, ModuleNotFoundError):
        return None, None


def cabin_user():
    """(name, uid, gid) of the unprivileged system user that runs the guest ssh client, or None when the
    installer has not created it (checkin then reports a missing prerequisite; cleanup needs no guest)."""
    import pwd
    try:
        entry = pwd.getpwnam(SYSTEM_USER)
    except KeyError:
        return None
    return (SYSTEM_USER, entry.pw_uid, entry.pw_gid)


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


def is_allowed(cfg: CabinConfig, caller: Caller, groups_fn=groups_of) -> bool:
    """root, or a user listed in [host] allow_users (names or @groups). An empty list allows nobody else."""
    if caller.uid == 0:
        return True
    users = {e for e in cfg.allow_users if not e.startswith("@")}
    wanted = {e[1:] for e in cfg.allow_users if e.startswith("@")}
    return caller.name in users or bool(wanted & groups_fn(caller))


def real_caller() -> Caller:
    import pwd
    if os.geteuid() != 0:
        raise Guarded("motel-desk must run as root (use `motel cabin`, which calls it through sudo)",
                      "motel cabin --help")
    raw = os.environ.get("SUDO_UID", "")
    uid = int(raw) if raw.isdigit() else 0
    try:
        entry = pwd.getpwuid(uid)
    except KeyError:
        raise Guarded(f"uid {uid} has no account on this host", "motel cabin --help") from None
    return Caller(uid=uid, gid=entry.pw_gid, name=entry.pw_name, operator=True)


def owner_label(caller: Caller) -> str:
    return caller.name if OWNER_RE.fullmatch(caller.name) else f"uid-{caller.uid}"


def local_checks(paths: CabinPaths, cfg: CabinConfig) -> list:
    checks = []

    def check(name, ok, detail=""):
        checks.append({"check": name, "ok": bool(ok), "detail": detail})
    for path, private in ((paths.config_file, False), (paths.receipt_file, False), (paths.real(cfg.token_file), True)):
        if not os.path.exists(path):
            check(f"{path} present", False, "missing")
            continue
        if not paths.strict:
            check(f"{path} present", True)
            continue
        try:
            check_root_file(path, private=private)
            check(f"{path} root-owned", True)
        except (OSError, Fail) as exc:
            check(f"{path} root-owned", False, getattr(exc, "message", type(exc).__name__))
    if paths.strict:
        user = cabin_user()
        problem = check_key_layout(paths.keys_dir, user[2] if user else None) if user else (
            "motel-cabin user missing; with [cabins] enabled, run the installer again: " + install_hint("--cabins --upgrade"))
        check(f"{paths.keys_dir}: root:motel-cabin 2750, home traversable by the group", problem is None,
              problem or "")
    from importlib import resources
    for name in ("cloud-init.yaml", "guest_agent.py"):
        check(f"cabin bootstrap file {name}", resources.files("motel.cabins").joinpath(name).is_file())
    return checks


# --------------------------------------------------------------------------- dispatch
def execute(args, *, paths: CabinPaths, caller: Caller, groups_fn=groups_of, token_reader=None, secret_reader=None,
            token_file_uid: int = 0, **wiring) -> dict:
    verb = args.verb
    if verb == "checkin" and args.image is not None:
        from motel.tours import bake
        bake.parse_image_spec(args.image)       # the syntax is checked before anything else is read or asked of the cloud
    # Operator verbs need a desk running as root (euid 0, which real_caller verifies), including `sudo motel-desk
    # cabin setup`, where SUDO_UID names the admin. The sudoers rule pins the first argument to the user verbs,
    # so a user in allow_users cannot reach these through sudo.
    if verb in OPERATOR_VERBS and not (caller.uid == 0 or caller.operator):
        raise PolicyRefused(f"`{verb}` is an operator command", "sudo motel-desk cabin " + verb)
    if verb == "setup":
        if args.token_file and args.what != "hetzner":
            raise Usage("--token-file applies to `setup hetzner` only", "sudo motel-desk cabin setup hetzner --token-file PATH")
        cfg = load_config(paths.config_file, require_enabled=False)
        if args.what == "hetzner":
            if args.token_file:
                token_reader = (lambda _prompt, path=args.token_file: setup_mod.read_token_file(path, owner_uid=token_file_uid))
            return setup_mod.setup_hetzner(paths, cfg, token_reader=token_reader or setup_mod.read_secret,
                                           fetch=wiring.get("fetch", setup_mod.fetch_metadata),
                                           transport_factory=wiring.get("setup_transport", Transport))
        return setup_mod.setup_tailscale(paths, cfg, secret_reader=secret_reader or setup_mod.read_secret,
                                         transport_factory=wiring.get("setup_ts_transport", TailnetTransport))
    if verb == "reap" and not cabins_in_use(paths):
        # A timer or cron line can outlive the cabin setup (or run before it): no receipt and no lease means nothing to do.
        return {"schema": SCHEMA, "ok": True, "apply": bool(args.apply), "set_up": False,
                "note": "cabins are not set up on this host and no cabin lease exists: nothing to reap",
                "provider": {"inventory": "not_applicable", "error": None},
                "actions": [], "planned": [], "warnings": [], "errors": [], "orphans": [], "busy": []}
    # The verbs that only look at or end a cabin read the lenient, read-only cleanup config, so they keep working
    # while `enabled = false` or while an unrelated [cabins] key is broken: a running server must stay reachable.
    cleanup_verbs = ("reap", "checkout", "resolve", "ls", "status")
    ctl = build_controller(paths, caller, require_enabled=verb not in ("doctor", *cleanup_verbs),
                           cleanup_only=verb in cleanup_verbs, **wiring)
    if verb not in OPERATOR_VERBS and not is_allowed(ctl.cfg, caller, groups_fn):
        raise PolicyRefused(f"{caller.name} is not in allow_users", "sudo motel-desk cabin doctor")
    if verb == "doctor":
        return ctl.doctor(local_checks(paths, ctl.cfg))
    if verb == "reap":
        return ctl.reap(args.apply)
    if verb == "resolve":
        return {"schema": SCHEMA, "ok": True, "cabin": ctl.resolve(args.cabin, args.forget_node)}
    if verb == "ls":
        return ctl.status()
    if verb == "images":
        from motel.tours import bake
        _need_provider(ctl)
        return {"schema": SCHEMA, "ok": True, "images": bake.user_images(ctl)}
    if verb == "checkin":
        ttl = parse_duration(args.ttl, what="--for") if args.ttl else None
        disk = parse_size_gb(args.disk) if args.disk else None
        image = None
        if args.image is not None:
            # The user's text never reaches the cloud query: it selects one row of this host's recorded, labelled
            # snapshots, and the controller is handed that row's numeric id.
            from motel.tours import bake
            _need_provider(ctl)
            image = bake.resolve_image(ctl, args.image)["id"]
        view = ctl.checkin(args.server_type, ttl, args.task, owner_label(caller), args.client_key, disk, image=image)
        return {"schema": SCHEMA, "ok": True, "cabin": view}
    if verb == "status":
        doc = ctl.status(args.cabin)
        if not doc["cabins"]:
            raise NotFound(f"no cabin {args.cabin}")
        return {"schema": SCHEMA, "ok": True, "cabin": doc["cabins"][0], "provider": doc["provider"]}
    if verb == "extend":
        return {"schema": SCHEMA, "ok": True, "cabin": ctl.extend(args.cabin, parse_duration(args.ttl, what="--for"))}
    if verb == "checkout":
        return {"schema": SCHEMA, "ok": True, "cabin": ctl.checkout(args.cabin)}
    raise Usage(f"unknown verb {verb}")  # unreachable: argparse restricts verbs


def _need_provider(ctl) -> None:
    if ctl.hz is None:
        raise Prerequisite("the cabin project token is not installed", "sudo motel-desk cabin doctor")


def cabins_in_use(paths: CabinPaths) -> bool:
    """A setup receipt or any lease record exists: a cabin server may still be running and billing."""
    try:
        os.lstat(paths.receipt_file)
        return True
    except FileNotFoundError:
        pass
    except OSError:
        return True  # only "does not exist" means no state: an unreadable receipt may well be there
    try:
        with os.scandir(os.path.join(paths.state_dir, "leases")) as entries:
            return any(e.name.endswith(".json") and not e.name.startswith(".") for e in entries)
    except FileNotFoundError:
        return False
    except OSError:
        return True


def _exit_code(verb, doc) -> int:
    if verb == "reap" and doc.get("set_up") is False:
        return EXIT_OK
    if verb == "doctor" and not doc["ok"]:
        return 8
    if verb in ("checkout", "resolve") and doc["cabin"]["state"] != "released":
        return 9
    if verb == "reap" and (doc["errors"] or doc["provider"]["inventory"] != "complete"):
        return 9
    if verb == "ls" and doc["provider"]["inventory"] != "complete":
        return 9
    return EXIT_OK


def run(argv, *, paths: CabinPaths, caller: Caller, out=None, err=None, **hooks) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    argv = list(argv)
    want_json = "--json" in argv
    try:
        args = build_parser().parse_args(argv)
        doc = execute(args, paths=paths, caller=caller, **hooks)
    except Fail as exc:
        print(json.dumps(exc.as_json()) if want_json else exc.line(), file=out if want_json else err)
        return exc.code
    except GuardRefused as exc:
        e = Guarded(f"guard refused: {exc}")
        print(json.dumps(e.as_json()) if want_json else e.line(), file=out if want_json else err)
        return e.code
    except ApiError as exc:
        e = Unavailable(f"provider unavailable (provider_unavailable:{exc.status}:{exc.code})")
        print(json.dumps(e.as_json()) if want_json else e.line(), file=out if want_json else err)
        return e.code
    except Exception as exc:  # noqa: BLE001  never leak a traceback to the caller
        e = Fail(f"internal error: {exc.__class__.__name__}")
        print(json.dumps(e.as_json()) if want_json else e.line(), file=out if want_json else err)
        return e.code
    if want_json:
        print(json.dumps(doc, indent=1, sort_keys=True), file=out)
    else:
        print(render(args.verb, doc), file=out)
    return _exit_code(args.verb, doc)


def main(argv=None) -> int:
    """Entry point for ``motel-desk cabin ...``; ``argv`` is what follows the word ``cabin``."""
    argv = list(sys.argv[1:] if argv is None else argv)
    os.umask(0o022)
    try:
        caller = real_caller()
    except Fail as exc:
        print(json.dumps(exc.as_json()) if "--json" in argv else exc.line(),
              file=sys.stdout if "--json" in argv else sys.stderr)
        return exc.code
    return run(argv, paths=CabinPaths(), caller=caller)


if __name__ == "__main__":
    raise SystemExit(main())
