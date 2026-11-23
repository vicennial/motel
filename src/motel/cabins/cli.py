"""The unprivileged ``motel cabin`` command (EXPERIMENTAL).

``main(argv)`` takes the arguments AFTER the word ``cabin``. It validates every argument, makes a cabin-specific
SSH key, calls the root-owned front desk through ``sudo -n`` and then talks to the cabin over SSH. It never sees
the cloud token and never passes raw paths or provider IDs to the desk.

    motel cabin run [--type T] [--for D] [--keep] [--disk SIZE] [--image NAME] [--json] -- CMD...
    motel cabin run --pool NAME [--type T] [--for D] [--disk SIZE] [--image NAME] [--json] -- CMD...
    motel cabin run --cabin ID [--json] -- CMD...      (sync and run in a cabin you hold; no checkin, no checkout)
    motel cabin pool ls [NAME] [--json]
    motel cabin pool drain [NAME] [--json]
    motel cabin checkin [--type T] [--for D] [--task NAME] [--disk SIZE] [--image NAME] [--json]
    motel cabin images [--json]
    motel cabin ls | status ID | extend ID --for D | checkout ID | doctor | ssh ID [-- CMD...]
"""

from __future__ import annotations

import argparse
import base64
import ipaddress
import json
import os
import re
import secrets
import shlex
import shutil
import subprocess
import sys
import tempfile
import time

from motel.cabins.config import CONFIG_FILE, DESK
from motel.cabins.errors import (
    EXIT_BUSY,
    EXIT_ERROR,
    EXIT_INCOMPLETE,
    EXIT_NOT_SET_UP,
    EXIT_OK,
    Fail,
    NotFound,
    Prerequisite,
    Usage,
)
from motel.cabins.guestchan import ssh_options
from motel.cabins.pool import pool_drain, pool_ls, pools_root, run_pooled
from motel.cabins.render import render
from motel.cabins.util import LEASE_ID_RE, SCHEMA, TASK_RE, parse_duration, parse_size_gb, utcnow
from motel.errors import NOT_ALLOWED_HINT, NOT_INSTALLED_HINT
from motel.receipts import cabin_receipt, render_cabin_receipt

SUDO = "/usr/bin/sudo"
SAFE_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
REMOTE_DIR = "work"
ADJECTIVES = ("swift", "quiet", "brave", "amber", "lucky", "misty", "sunny", "rusty", "gentle", "plucky")
ANIMALS = ("gull", "heron", "otter", "finch", "marten", "newt", "lynx", "crane", "wren", "badger")


HELP_DESCRIPTION = """\
EXPERIMENTAL. Disposable servers by the hour, in your own cloud account. A cabin is one fresh server: you check it in,
run commands on it, and check it out. Checkout deletes it, and deletion is final: copy results out first.

The provider bills every started hour in full. `run` and `checkout` end with a receipt on stderr (server type, wall time,
started hours billed, price per hour, estimated total, servers left) and `--json` carries it under `receipt`."""

HELP_EXAMPLES = """\
examples:
  motel cabin run -- make test            run a command on a fresh server, then delete the server
  motel cabin run --pool ci -- make test  opt-in warm pool: reuse a matching idle cabin of yours
  motel cabin run --keep -- ./build.sh    keep the cabin afterwards; open it with: motel cabin ssh ID
  motel cabin checkin --for 2h            take a cabin and print its id (it expires after 2h)
  motel cabin images                      list the images your operator baked; start one with --image NAME
  motel cabin ls                          list your cabins
  motel cabin pool ls                     list your warm cabin pools
  motel cabin pool drain                  check out idle cabins from your pools
  motel cabin checkout ID                 delete a cabin now and print its receipt

No cloud yet? `motel try --cabin` shows a cabin run and a small tour, offline, with their receipts."""


class CabinParser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message)

    def exit(self, status=0, message=None):
        if status:
            raise Usage((message or "").strip())
        raise SystemExit(status)


def build_parser() -> argparse.ArgumentParser:
    common = CabinParser(add_help=False, allow_abbrev=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    launch = CabinParser(add_help=False, allow_abbrev=False)
    launch.add_argument("--type", dest="server_type", default=None, help="server type (default: the first allowed)")
    launch.add_argument("--for", dest="ttl", default=None, help="checkout time from now, e.g. 90m, 2h (default 2h)")
    launch.add_argument("--task", default=None, help="short name for the cabin id")
    launch.add_argument("--disk", default=None, help="extra volume with the cabin's lifecycle, e.g. 200G")
    launch.add_argument("--image", default=None, metavar="NAME",
                        help="start from an image the operator baked on this host (a name or id from `motel cabin images`)")
    parser = CabinParser(prog="motel cabin", allow_abbrev=False, formatter_class=argparse.RawDescriptionHelpFormatter,
                         description=HELP_DESCRIPTION, epilog=HELP_EXAMPLES)
    sub = parser.add_subparsers(dest="verb", required=True, parser_class=CabinParser, metavar="VERB")
    p = sub.add_parser("run", parents=[launch, common], allow_abbrev=False,
                       help="check in, sync this git worktree, run CMD, check out (unless --keep or --pool)")
    p.add_argument("--keep", action="store_true", help="do not check out afterwards")
    p.add_argument("--cabin", default=None, metavar="ID",
                   help="run in this existing cabin: sync and run only, no checkin and no checkout")
    p.add_argument("--pool", default=None, metavar="NAME",
                   help="opt-in per-user warm pool: reuse a matching idle cabin or check in a new one and keep it warm")
    p_pool = sub.add_parser("pool", allow_abbrev=False, help="manage opt-in per-user warm cabin pools (ls, drain)")
    pool_sub = p_pool.add_subparsers(dest="pool_verb", required=True, parser_class=CabinParser, metavar="ACTION")
    p_pls = pool_sub.add_parser("ls", parents=[common], allow_abbrev=False,
                                help="list pooled cabins with live state and lock status")
    p_pls.add_argument("pool_name", nargs="?", default=None, metavar="NAME", help="pool name (omit for all pools)")
    p_pdrain = pool_sub.add_parser("drain", parents=[common], allow_abbrev=False,
                                   help="check out every idle cabin in a pool (or all pools)")
    p_pdrain.add_argument("pool_name", nargs="?", default=None, metavar="NAME", help="pool name (omit for all pools)")
    sub.add_parser("checkin", parents=[launch, common], allow_abbrev=False, help="take a cabin; prints its id")
    sub.add_parser("ls", parents=[common], allow_abbrev=False, help="list your cabins")
    sub.add_parser("images", parents=[common], allow_abbrev=False, help="list the baked images you can start a cabin from")
    sub.add_parser("doctor", parents=[common], allow_abbrev=False, help="check the cabin setup (your cabins only)")
    p = sub.add_parser("status", parents=[common], allow_abbrev=False, help="show one cabin")
    p.add_argument("cabin")
    p = sub.add_parser("extend", parents=[common], allow_abbrev=False, help="late checkout")
    p.add_argument("cabin")
    p.add_argument("--for", dest="ttl", required=True)
    p = sub.add_parser("checkout", parents=[common], allow_abbrev=False, help="delete the cabin (final)")
    p.add_argument("cabin")
    p = sub.add_parser("ssh", allow_abbrev=False, help="open a shell (or run CMD after --) in a cabin")
    p.add_argument("cabin")
    return parser


def _q(path: str) -> str:
    """A path as one double-quoted ssh_config word. Refuses characters that cannot be quoted safely."""
    if any(c in path for c in '"\\\n\r\0') or not path.startswith("/"):
        raise Usage("the cabin state directory has a character that cannot be used in an ssh config",
                    "set XDG_STATE_HOME to a plain path")
    return '"' + path.replace("%", "%%") + '"'


def ssh_config_text(directory: str, view: dict) -> str:
    ssh = view["ssh"]
    alias, host, user, port = ssh["host_key_alias"], ssh["host"], ssh["user"], ssh["port"]
    try:
        ipaddress.IPv4Address(host)
    except ValueError:
        raise Usage("the front desk returned an unusable cabin address") from None
    if not (re.fullmatch(r"motel-[a-z0-9-]{1,60}", str(alias)) and user == "cabin" and port == 22):
        raise Usage("the front desk returned unusable ssh settings")
    return "\n".join([
        f"Host {alias}", f"  HostName {host}", f"  User {user}", f"  Port {port}",
        f"  IdentityFile {_q(os.path.join(directory, 'id_ed25519'))}", "  IdentitiesOnly yes", "  IdentityAgent none",
        f"  UserKnownHostsFile {_q(os.path.join(directory, 'known_hosts'))}", "  GlobalKnownHostsFile /dev/null",
        f"  HostKeyAlias {alias}", "  StrictHostKeyChecking yes", "  UpdateHostKeys no", "  AddressFamily inet",
        "  ForwardAgent no", "  ForwardX11 no", "  ClearAllForwardings yes", "  PasswordAuthentication no",
        "  KbdInteractiveAuthentication no", "  ServerAliveInterval 30", "  ServerAliveCountMax 6", ""])


def keygen(directory: str) -> str:
    path = os.path.join(directory, "id_ed25519")
    subprocess.run(["/usr/bin/ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", "motel-cabin-client", "-f", path],
                   check=True, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, env=SAFE_ENV)
    with open(path + ".pub", encoding="ascii") as handle:
        return " ".join(handle.read().split()[:2])


def state_root() -> str:
    base = os.environ.get("XDG_STATE_HOME") or os.path.join(os.path.expanduser("~"), ".local", "state")
    return os.path.join(base, "motel", "cabins")


def worktree_files(root: str, runner=subprocess.run) -> list:
    """Tracked files plus untracked files that are not ignored (``.gitignore`` is respected).

    Files that are tracked but deleted in the working tree are skipped.
    """
    proc = runner(["git", "-C", root, "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                  stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    if proc.returncode != 0:
        raise Usage("this directory is not a git worktree, so there is nothing to sync",
                    "run `motel cabin run` inside a git checkout")
    names = [n.decode("utf-8", "surrogateescape") for n in proc.stdout.split(b"\0") if n]
    seen, out = set(), []
    for name in names:
        if name in seen or name.startswith("/") or ".." in name.split("/"):
            continue
        seen.add(name)
        if os.path.lexists(os.path.join(root, name)):
            out.append(name)
    return out


def baked_image(offer) -> str | None:
    """The name of the baked image a cabin was started from, or None for the stock image."""
    offer = offer or {}
    return offer.get("image_name") if offer.get("image_type") == "snapshot" else None


def _escape_rsync_pattern(path: str) -> str:
    return path.replace("\\", "\\\\").replace("*", "\\*").replace("?", "\\?").replace("[", "\\[")


def _rsync_include_rules(files: list[str]) -> list[str]:
    """Build anchored rsync include patterns (ancestor directories + exact files) for ``--delete-excluded``."""
    dirs: set[str] = set()
    rules: list[str] = []
    for name in files:
        parts = name.split("/")
        for idx in range(1, len(parts)):
            parent = "/".join(parts[:idx])
            if parent not in dirs:
                dirs.add(parent)
                rules.append("/" + _escape_rsync_pattern(parent) + "/")
        rules.append("/" + _escape_rsync_pattern(name))
    return rules


def encode_job(command, cwd=REMOTE_DIR) -> str:
    return base64.b64encode(json.dumps({"argv": list(command), "cwd": cwd}).encode()).decode()


class Cli:
    def __init__(self, *, runner=subprocess.run, root=None, make_key=keygen, out=None, err=None, cwd=None,
                 monotonic=time.monotonic, desk=DESK, desk_exists=None, sleep=time.sleep,
                 pools_root_dir=None, clock=utcnow, config_path=None):
        self.runner, self.root, self.make_key = runner, root or state_root(), make_key
        self.out, self.err = out or sys.stdout, err or sys.stderr
        self.cwd = cwd or os.getcwd()
        self.monotonic, self.sleep, self.desk = monotonic, sleep, desk
        self.clock = clock
        self.pools_root = pools_root_dir if pools_root_dir is not None else pools_root(root)
        self.config_path = config_path if config_path is not None else (
            CONFIG_FILE if runner is subprocess.run else None)
        self.desk_exists = desk_exists if desk_exists is not None else (
            lambda: os.path.exists(desk) or runner is not subprocess.run)

    # -- helpers
    BUSY_WAIT_S = 90            # checkout is refused while the reaper holds the cabin's lock (a short hold)

    def say(self, text):
        print(f"motel: {text}", file=self.err)

    @staticmethod
    def receipt_of(view):
        """The receipt of a cabin record: the desk's own, else built from the record (a desk that predates receipts)."""
        if not view:
            return None
        return view.get("receipt") or cabin_receipt(view)

    def show_receipt(self, cabin, view) -> bool:
        """Print the cabin's receipt block on stderr. False when the record has no price to show."""
        lines = render_cabin_receipt(self.receipt_of(view), cabin)
        for line in lines:
            print(line, file=self.err)
        return bool(lines)

    def desk_call(self, args) -> tuple:
        """Run the desk and return (exit code, parsed JSON document or {})."""
        if not self.desk_exists():
            raise Prerequisite(f"the front desk is not installed ({self.desk} is missing)", NOT_INSTALLED_HINT)
        proc = self.runner([SUDO, "-n", self.desk, "cabin", *args, "--json"], stdout=subprocess.PIPE, check=False,
                           env=SAFE_ENV)
        try:
            doc = json.loads(proc.stdout or b"{}")
        except ValueError:
            doc = {"ok": False, "error": "unreadable front desk reply", "hint": "motel doctor"}
        if proc.returncode == 1 and not proc.stdout:   # sudo's own status: the desk never ran, so `motel doctor` cannot help
            doc = {"ok": False, "error": "the front desk gave no answer (sudo may have refused you: you are probably not in "
                                         "allow_users)", "hint": NOT_ALLOWED_HINT}
        return proc.returncode, doc

    def fail(self, code, doc, want_json):
        if want_json:
            print(json.dumps(doc), file=self.out)
        else:
            print(f"motel: {doc.get('error', 'failed')}. Try: {doc.get('hint', 'motel doctor')}", file=self.err)
        return code or EXIT_ERROR

    def cabin_dir(self, cabin: str) -> str:
        if not LEASE_ID_RE.fullmatch(cabin or ""):
            raise Usage(f"bad cabin id {cabin!r}", "motel cabin ls")
        return os.path.join(self.root, cabin)

    def ssh_cfg(self, cabin: str) -> str:
        cfg = os.path.join(self.cabin_dir(cabin), "ssh_config")
        if not os.path.exists(cfg):
            raise NotFound(f"no access files for cabin {cabin} in {self.cabin_dir(cabin)}", "motel cabin ls")
        return cfg

    # -- verbs
    def launch_args(self, args) -> list:
        task = args.task or f"{secrets.choice(ADJECTIVES)}-{secrets.choice(ANIMALS)}"
        if not TASK_RE.fullmatch(task):
            raise Usage("task must be 1-32 lowercase letters, digits or hyphens", "motel cabin checkin --task build")
        out = ["--task", task]
        if args.server_type:
            out += ["--type", args.server_type]
        if args.ttl:
            parse_duration(args.ttl, what="--for")
            out += ["--for", args.ttl]
        if args.disk:
            parse_size_gb(args.disk)
            out += ["--disk", args.disk]
        if args.image is not None:
            from motel.tours.bake import parse_image_spec
            parse_image_spec(args.image)        # the desk checks it again: it never trusts this client
            out += ["--image", args.image]
        return out

    def checkin(self, args, want_json=False):
        """Returns (exit code, view or None). Prints errors; the caller prints success."""
        launch = self.launch_args(args)
        os.makedirs(self.root, mode=0o700, exist_ok=True)
        os.chmod(self.root, 0o700)
        pending = tempfile.mkdtemp(dir=self.root, prefix=".pending-")
        try:
            pub = self.make_key(pending)
            rc, doc = self.desk_call(["checkin", *launch, "--client-key", pub])
            view = doc.get("cabin") if isinstance(doc, dict) else None
            if rc != 0 or not view or not view.get("lease_id"):
                if args.image is not None and "unrecognized arguments" in str(doc.get("error", "")):
                    doc = {"ok": False, "code": rc, "error": "the installed front desk is older than this motel and does not "
                           "know --image, so no cabin was started", "hint": "ask the host admin to run: sudo uvx --from motel motel install --upgrade"}
                return self.fail(rc, doc, want_json), None
            final = self.cabin_dir(view["lease_id"])
            os.rename(pending, final)
            pending = None
            self.write_access(final, view)
            with open(os.path.join(final, "receipt.json"), "w", encoding="utf-8") as handle:
                json.dump(view, handle, indent=1, sort_keys=True)
            return EXIT_OK, view
        finally:
            if pending:
                shutil.rmtree(pending, ignore_errors=True)

    @staticmethod
    def write_access(directory: str, view: dict) -> None:
        if not view.get("ssh") or not view.get("host_key"):
            return
        with open(os.path.join(directory, "known_hosts"), "w", encoding="ascii") as handle:
            handle.write(f"{view['name']} {view['host_key']}\n")
        with open(os.path.join(directory, "ssh_config"), "w", encoding="ascii") as handle:
            handle.write(ssh_config_text(directory, view))

    def cleanup_keys(self, cabin: str) -> None:
        key = os.path.join(self.cabin_dir(cabin), "id_ed25519")
        for path in (key, key + ".pub"):
            if os.path.exists(path):
                os.unlink(path)

    def sync(self, cabin: str, view: dict, *, delete: bool = False) -> int:
        """rsync the current git worktree (tracked + untracked-not-ignored) into ~/work on the cabin.

        With ``delete=True`` (used for pooled warm runs), removes stale files in ``~/work`` that are not in the
        current worktree so artifacts from a previous job do not leak.
        """
        files = worktree_files(self.cwd, self.runner)
        cfg = self.ssh_cfg(cabin)
        if delete:
            rules = _rsync_include_rules(files)
            payload = b"".join(r.encode("utf-8", "surrogateescape") + b"\0" for r in rules)
            argv = ["/usr/bin/rsync", "-a", "--delete", "--delete-excluded", "--from0", "--include-from=-",
                    "--exclude=*", "-e", f"/usr/bin/ssh -F {shlex.quote(cfg)}", "./", f"{view['name']}:{REMOTE_DIR}/"]
        else:
            payload = b"".join(f.encode("utf-8", "surrogateescape") + b"\0" for f in files)
            argv = ["/usr/bin/rsync", "-a", "--from0", "--files-from=-", "-e", f"/usr/bin/ssh -F {shlex.quote(cfg)}",
                    "./", f"{view['name']}:{REMOTE_DIR}/"]
        proc = self.runner(argv, input=payload, cwd=self.cwd, check=False, env=SAFE_ENV)
        return proc.returncode

    def remote_run(self, cabin: str, view: dict, command) -> int:
        cfg = self.ssh_cfg(cabin)
        argv = ["/usr/bin/ssh", "-F", cfg, "-T", "--", view["name"], "motel-guest run " + encode_job(command)]
        return self.runner(argv, check=False).returncode

    def result_line(self, args, cabin, result, *, kept, offer=None, done=None, pool=None, pool_hit=None) -> None:
        """``run --json``: one final JSON object on stdout, on a line of its own after the command's own output."""
        if not getattr(args, "json", False):
            return
        done = done or {}
        doc = {"schema": SCHEMA, "ok": result == EXIT_OK, "cabin": cabin, "exit_code": result, "kept": kept,
               "released": done.get("state") == "released",
               "hourly_net_eur": (offer or {}).get("total_hourly_net_eur"),
               "cost_estimate_net_eur": done.get("cost_estimate_net_eur"), "receipt": self.receipt_of(done)}
        hours = (done.get("billing") or {}).get("billed_hours")
        if hours is not None:
            doc["billed_hours"] = hours
        if baked_image(offer):
            doc["image"] = baked_image(offer)
        if pool is not None:
            doc["pool"] = pool
            doc["pool_hit"] = bool(pool_hit)
        print("", file=self.out)
        print(json.dumps(doc, sort_keys=True), file=self.out)
        self.out.flush()

    def run(self, args) -> int:
        command = args.command
        if not command:
            raise Usage("nothing to run: put the command after --", "motel cabin run -- make test")
        if args.pool is not None and (args.cabin or args.keep):
            raise Usage("--pool cannot be combined with --cabin or --keep", "motel cabin run --pool NAME -- CMD")
        if args.cabin:
            return self.run_in(args)
        if args.pool is not None:
            return run_pooled(self, args)
        started = self.monotonic()
        rc, view = self.checkin(args, getattr(args, "json", False))
        if view is None:
            return rc
        cabin = view["lease_id"]
        offer = view.get("offer") or {}
        image = f" · image {baked_image(offer)}" if baked_image(offer) else ""
        self.say(f"cabin {cabin} · EUR {offer.get('total_hourly_net_eur')}/h net{image} · ready in "
                 f"{int(self.monotonic() - started)}s")
        result = EXIT_ERROR
        done = None
        try:
            sync_rc = self.sync(cabin, view)
            if sync_rc != 0:
                self.say(f"syncing the worktree failed (rsync exit {sync_rc}). Try: motel cabin ssh {cabin}")
                result = sync_rc
            else:
                result = self.remote_run(cabin, view, command)
        finally:
            if args.keep:
                self.say(f"kept {cabin}. Try: motel cabin ssh {cabin}   (motel cabin checkout {cabin} when done)")
            else:
                result, done = self.finish(cabin, result)
        self.result_line(args, cabin, result, kept=bool(args.keep), offer=offer, done=done)
        return result

    def run_in(self, args) -> int:
        """``run --cabin ID -- CMD``: sync this worktree to a cabin you already hold and run there. Nothing is
        checked in and nothing is checked out; the cabin keeps its own checkout time."""
        if any((args.server_type, args.ttl, args.task, args.disk, args.keep, args.image)):
            raise Usage("--cabin runs in an existing cabin: --type, --for, --task, --disk, --image and --keep do not apply",
                        "motel cabin run --cabin ID -- CMD")
        cabin = args.cabin
        self.ssh_cfg(cabin)  # validates the id and that this account holds the access files
        try:
            with open(os.path.join(self.cabin_dir(cabin), "receipt.json"), encoding="utf-8") as handle:
                view = json.load(handle)
            view["name"]
        except (OSError, ValueError, KeyError, TypeError):
            raise NotFound(f"no usable receipt for cabin {cabin}", "motel cabin ls") from None
        sync_rc = self.sync(cabin, view)
        if sync_rc != 0:
            self.say(f"syncing the worktree failed (rsync exit {sync_rc}). Try: motel cabin ssh {cabin}")
            result = sync_rc
        else:
            result = self.remote_run(cabin, view, args.command)
        self.result_line(args, cabin, result, kept=True, offer=view.get("offer"))
        return result

    def images(self, want_json) -> int:
        """``images``: the baked images this host offers. A desk or sudoers rule from before this verb gets a clear answer."""
        rc, doc = self.desk_call(["images"])
        error = str(doc.get("error", ""))
        if rc != 0 and ("invalid choice" in error or "gave no answer" in error):
            doc = {"ok": False, "code": rc, "error": "the installed front desk or its sudoers rule is older than this motel and "
                   "has no `images` verb", "hint": "ask the host admin to run: sudo uvx --from motel motel install --upgrade"}
        if rc != 0 or doc.get("ok") is False or not isinstance(doc.get("images"), list):
            return self.fail(rc, doc, want_json)
        print(json.dumps(doc, indent=1, sort_keys=True) if want_json else render("images", doc), file=self.out)
        return EXIT_OK

    def checkout_with_retry(self, cabin: str) -> tuple:
        """The desk's checkout reply. A busy cabin (the reaper holds its lock for a short activity check) is retried
        with a short backoff for up to ``BUSY_WAIT_S`` seconds; any other reply is final."""
        deadline = self.monotonic() + self.BUSY_WAIT_S
        for step in range(40):
            rc, doc = self.desk_call(["checkout", cabin])
            if rc != EXIT_BUSY or self.monotonic() >= deadline:
                break
            self.sleep(min(10, 2 * 2 ** step))
        return rc, doc

    def finish(self, cabin: str, result: int) -> tuple:
        """Check out. Returns (exit code, the desk's cabin record or None).

        - released: the exit code is the command's.
        - cleanup pending (desk exit 9 and the cabin is ``cleanup_pending``): the cabin is already shut out and
          only the provider is slow, so a command that ran keeps its own exit code. A loud warning on stderr and
          ``released: false`` in ``--json`` say so; the record stays for the reaper.
        - anything else (busy past the wait, error, guard, not found): the cabin may still be active and billing.
          The exit code is the desk's, and the message says how to check it out."""
        rc, doc = self.checkout_with_retry(cabin)
        done = (doc.get("cabin") or {}) if isinstance(doc, dict) else {}
        if rc == EXIT_OK and done.get("state") == "released":
            self.cleanup_keys(cabin)
            if not self.show_receipt(cabin, done):
                self.say(f"released {cabin}")
            return result, done
        if rc == EXIT_INCOMPLETE and done.get("state") == "cleanup_pending":
            self.say(f"WARNING: checkout of {cabin} is NOT complete (cleanup pending; it still bills until the reaper "
                     f"finishes). The exit status is the command's. Try: motel cabin status {cabin}")
            self.show_receipt(cabin, done)
            return result, done
        until = done.get("expires_at")
        self.say(f"checkout of {cabin} FAILED ({(doc or {}).get('error', 'failed')}; checkout exit {rc}). The cabin "
                 f"may still be ACTIVE and billing until it is idle or expires{f' at {until}' if until else ''}. "
                 f"The command exited {result}; the exit status is the checkout's. Try: motel cabin checkout {cabin}")
        return (rc or EXIT_ERROR), done

    def simple(self, verb, args, want_json) -> int:
        argv = {"ls": ["ls"], "doctor": ["doctor"], "status": ["status", getattr(args, "cabin", "")],
                "extend": ["extend", getattr(args, "cabin", ""), "--for", getattr(args, "ttl", "")],
                "checkout": ["checkout", getattr(args, "cabin", "")]}[verb]
        if verb not in ("ls", "doctor"):
            self.cabin_dir(args.cabin)
        if verb == "extend":
            parse_duration(args.ttl, what="--for")
        rc, doc = self.desk_call(argv)
        if verb == "doctor" and isinstance(doc.get("checks"), list):  # a failing check is the answer, not an error
            print(json.dumps(doc, indent=1, sort_keys=True) if want_json else render(verb, doc), file=self.out)
            return rc
        if doc.get("ok") is False or rc not in (0, 9):
            return self.fail(rc, doc, want_json)
        if verb == "checkout" and isinstance(doc.get("cabin"), dict) and not doc["cabin"].get("receipt"):
            doc["cabin"]["receipt"] = self.receipt_of(doc["cabin"])      # a desk that predates receipts
        if want_json:
            print(json.dumps(doc, indent=1, sort_keys=True), file=self.out)
        else:
            print(render(verb, doc), file=self.out)
        if verb == "checkout" and (doc.get("cabin") or {}).get("state") == "released":
            self.cleanup_keys(args.cabin)
            self.show_receipt(args.cabin, doc["cabin"])
        return rc


def split_command(argv):
    """Split ``verb ... -- CMD ...`` into (options, command)."""
    if "--" in argv:
        i = argv.index("--")
        return argv[:i], argv[i + 1:]
    return argv, []


def main(argv=None, **kw) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cli = Cli(**kw)
    want_json = "--json" in split_command(argv)[0]  # a "--json" after the double dash belongs to the command
    try:
        head, command = split_command(argv)
        if not head or head[0] in ("-h", "--help"):
            print(build_parser().format_help().rstrip(), file=cli.out)
            return EXIT_OK if head else 2
        args = build_parser().parse_args(head)
        if command and args.verb not in ("run", "ssh"):
            raise Usage("only `run` and `ssh` take a command after --")
        args.command = command
        if args.verb == "run":
            return cli.run(args)
        if args.verb == "checkin":
            rc, view = cli.checkin(args, want_json)
            if view is None:
                return rc
            if want_json:
                print(json.dumps(view, indent=1, sort_keys=True), file=cli.out)
            else:
                print(view["lease_id"], file=cli.out)
                image = f" · image {baked_image(view.get('offer'))}" if baked_image(view.get("offer")) else ""
                cli.say(f"{view['lease_id']} · {(view.get('offer') or {}).get('server_type')}{image} · expires "
                        f"{view['expires_at']}. Deletion is final: copy results out before checkout. "
                        f"Try: motel cabin ssh {view['lease_id']}")
            return rc
        if args.verb == "ssh":
            cfg = cli.ssh_cfg(args.cabin)
            with open(os.path.join(cli.cabin_dir(args.cabin), "receipt.json"), encoding="utf-8") as handle:
                alias = json.load(handle)["name"]
            tail = ["motel-guest run " + encode_job(command, cwd=REMOTE_DIR)] if command else []
            return cli.runner(["/usr/bin/ssh", "-F", cfg, *(["-T"] if command else []), "--", alias, *tail],
                              check=False).returncode
        if args.verb == "images":
            return cli.images(want_json)
        if args.verb == "pool":
            if args.pool_verb == "ls":
                return pool_ls(cli, args.pool_name, want_json)
            if args.pool_verb == "drain":
                return pool_drain(cli, args.pool_name, want_json)
        return cli.simple(args.verb, args, want_json)
    except Fail as exc:
        print(json.dumps(exc.as_json()) if want_json else exc.line(), file=cli.out if want_json else cli.err)
        return exc.code
    except OSError as exc:
        e = Fail(f"{exc.strerror or exc.__class__.__name__}")
        print(json.dumps(e.as_json()) if want_json else e.line(), file=cli.out if want_json else cli.err)
        return e.code


__all__ = ["EXIT_NOT_SET_UP", "Cli", "main", "ssh_options", "worktree_files"]
