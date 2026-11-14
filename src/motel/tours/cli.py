"""The unprivileged ``motel tour`` command.

``main(argv)`` takes the arguments AFTER the word ``tour``. It reads and validates the spec, makes the git bundles
as the invoking user, then calls the root-owned front desk through ``sudo -n``. Results are read back through
the desk (the state directory is root-only). It never sees a credential.

    motel tour start FILE|- [--json]
    motel tour ls [--all] | status ID | wait ID [--any|--all] [--timeout S] | cancel ID
    motel tour result ID GUEST | logs ID GUEST [--stream S] | fetch ID GUEST [--artifacts DIR]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile

from motel.cabins.errors import EXIT_ERROR, EXIT_OK, Fail
from motel.errors import desk_refusal, not_installed
from motel.receipts import render_tour_receipt, tour_receipt
from motel.tours import artifacts as art
from motel.tours import bundle, ownlogin, spec
from motel.tours.errors import Usage
from motel.tours.state import ATTEMPT_ID_RE, TOUR_ID_RE  # noqa: F401

DESK = "/usr/local/sbin/motel-desk"
SUDO_CANDIDATES = ("/usr/bin/sudo", "/bin/sudo", "/usr/local/bin/sudo")
SAFE_ENV = {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
SPEC_MAX = 2 * 1024 * 1024
SCHEMA = "motel/v1"


class CliParser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message)

    def exit(self, status=0, message=None):
        if status:
            raise Usage((message or "").strip())
        raise SystemExit(status)


def build_parser() -> argparse.ArgumentParser:
    common = CliParser(add_help=False, allow_abbrev=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    parser = CliParser(prog="motel tour", allow_abbrev=False, formatter_class=argparse.RawDescriptionHelpFormatter,
                       description="EXPERIMENTAL. Run a swarm of bounded, non-interactive agent jobs on disposable servers: one\n"
                                   "fresh cabin per guest, deleted when the guest finishes. `wait` and `status` end a finished tour\n"
                                   "with a receipt on stderr (per-guest server cost, model spend, servers left); `--json` has it\n"
                                   "under `tour.receipt`.",
                       epilog="examples:\n  motel tour start review.toml     start the tour in the spec; prints the tour id\n"
                              "  motel tour wait TOUR             wait until every guest has finished, then show the receipt\n"
                              "  motel tour fetch TOUR GUEST      import the guest's commits as a branch (nothing is merged)\n\n"
                              "No cloud yet? `motel try --cabin` shows a small tour, offline, with its receipt.")
    sub = parser.add_subparsers(dest="verb", required=True, parser_class=CliParser, metavar="VERB")
    p = sub.add_parser("start", parents=[common], allow_abbrev=False, help="start a tour from a TOML or JSON spec")
    p.add_argument("spec", metavar="FILE|-", help="the spec file, or - for standard input")
    p = sub.add_parser("ls", parents=[common], allow_abbrev=False, help="list your tours")
    p.add_argument("--all", action="store_true", help="everyone's tours (root only)")
    for name, text in (("status", "show one tour and its guests"), ("cancel", "stop a tour: revoke, stop, collect, delete")):
        p = sub.add_parser(name, parents=[common], allow_abbrev=False, help=text)
        p.add_argument("tour")
    p = sub.add_parser("wait", parents=[common], allow_abbrev=False, help="wait for guests to finish")
    p.add_argument("tour")
    group = p.add_mutually_exclusive_group()
    group.add_argument("--any", dest="mode", action="store_const", const="any", help="return when any guest has finished")
    group.add_argument("--all", dest="mode", action="store_const", const="all", help="return when all have finished (default)")
    p.add_argument("--timeout", type=float, default=3600.0, help="seconds to wait (default 3600); exit 9 on timeout")
    p.set_defaults(mode="all")
    p = sub.add_parser("result", parents=[common], allow_abbrev=False, help="the result record of one guest")
    p.add_argument("tour")
    p.add_argument("guest")
    p = sub.add_parser("fetch", parents=[common], allow_abbrev=False,
                       help="import the guest's commits as branch motel/<tour>/<guest> in the current repository")
    p.add_argument("tour")
    p.add_argument("guest")
    p.add_argument("--artifacts", metavar="DIR", default=None, help="also save the guest's artifacts into DIR")
    p = sub.add_parser("logs", parents=[common], allow_abbrev=False, help="the tail of a guest's output (untrusted)")
    p.add_argument("tour")
    p.add_argument("guest")
    p.add_argument("--stream", default="stdout", choices=("stdout", "stderr", "runner", "boot"))
    p.add_argument("--tail", type=int, default=64 * 1024, help="bytes from the end (default 65536)")
    return parser


def default_runner(args: list, stdin: bytes | None = None) -> tuple:
    sudo = next((s for s in SUDO_CANDIDATES if os.path.exists(s)), None)
    if sudo is None or not os.path.exists(DESK):
        raise not_installed()
    extra = {"input": stdin} if stdin is not None else {"stdin": subprocess.DEVNULL}
    proc = subprocess.run([sudo, "-n", DESK, "tour", *args], capture_output=True, env=SAFE_ENV, shell=False, check=False, **extra)
    return proc.returncode, proc.stdout, proc.stderr


class Cli:
    def __init__(self, *, runner=default_runner, out=None, err=None, cwd=None, git=bundle.run_git, stdin=None):
        self.runner, self.out, self.err = runner, out or sys.stdout, err or sys.stderr
        self.cwd, self.git = cwd or os.getcwd(), git
        self.stdin = stdin

    def say(self, text: str) -> None:
        print(f"motel: {text}", file=self.err)

    # -- the desk
    def desk(self, args: list, stdin: bytes | None = None) -> tuple:
        """(exit code, parsed JSON document or None, raw stdout bytes)."""
        code, stdout, stderr = self.runner([*args, "--json"], stdin)
        try:
            doc = json.loads(stdout or b"{}")
            doc = doc if isinstance(doc, dict) else None
        except ValueError:
            doc = None
        if not doc and code != 0 and b"sudo" in (stderr or b""):     # no answer at all, and sudo's own words: the desk never ran
            raise desk_refusal((stderr or b"").decode("utf-8", "replace"))
        if doc is None and code != 0:
            return code, None, stdout
        return code, doc, stdout

    def call(self, args: list, stdin: bytes | None = None) -> tuple:
        code, doc, _raw = self.desk(args, stdin)
        if doc is None:
            raise Fail("the front desk returned no usable answer", "motel doctor")
        if doc.get("ok") is False or (code not in (0, 9)):
            raise _DeskError(code or EXIT_ERROR, doc)
        return code, doc

    # -- output
    def emit(self, doc: dict, text: str, want_json: bool) -> None:
        print(json.dumps(doc, indent=1, sort_keys=True) if want_json else text, file=self.out)

    # -- verbs
    def start(self, args, want_json: bool) -> int:
        if args.spec == "-":
            raw = (self.stdin or sys.stdin).read(SPEC_MAX + 1)
            base_dir = self.cwd
        else:
            try:
                with open(args.spec, encoding="utf-8") as handle:
                    raw = handle.read(SPEC_MAX + 1)
            except OSError as exc:
                raise Usage(f"cannot read {args.spec}: {exc.strerror}", "motel tour start --help") from None
            base_dir = os.path.dirname(os.path.abspath(args.spec))
        doc = spec.load_text(raw if isinstance(raw, str) else raw.decode(), source=args.spec)
        parsed = spec.parse(doc)
        if any(g.auth == "own-login" for g in parsed.guests):
            self.say(ownlogin.WARNING)
        work = tempfile.mkdtemp(prefix="motel-tour-")
        try:
            os.chmod(work, 0o700)
            inputs = self.prepare(parsed, base_dir, work)
            payload = json.dumps({"spec": parsed.to_wire(), "inputs": inputs}).encode()
            code, reply = self.call(["start"], payload)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        for line in reply.get("warnings", []):
            if line != ownlogin.WARNING:
                self.say(line)
        tour = reply["tour"]
        if want_json:
            self.emit(reply, "", True)
        else:
            self.say(f"tour {tour['id']} started · {len(tour['guests'])} guest(s). Try: motel tour wait {tour['id']}")
            print(tour["id"], file=self.out)
        return code

    def prepare(self, parsed, base_dir: str, work: str) -> dict:
        """Make the bundles and copy the overlays as this user; fill each wire guest's ``base``/``bundle``/``overlay_input``."""
        inputs, bundles = {}, {}
        for guest in parsed.guests:
            repo = os.path.realpath(os.path.join(base_dir, guest.repo or "."))
            sha = bundle.resolve(repo, guest.base, self.git)
            if (repo, sha) not in bundles:
                path = os.path.join(work, f"b{len(bundles)}.bundle")
                digest, _size = bundle.make_bundle(repo, sha, path, self.git)
                bundles[(repo, sha)] = digest[:16]
                inputs[digest[:16]] = {"kind": "bundle", "path": path, "sha256": digest}
            guest.base, guest.bundle = sha, bundles[(repo, sha)]
            if guest.overlay:
                source = os.path.join(base_dir, guest.overlay)
                path = os.path.join(work, f"o{len(inputs)}.patch")
                try:
                    with open(source, "rb") as src, open(path, "xb") as dst:
                        dst.write(src.read(8 * 1024 * 1024 + 1))
                except OSError as exc:
                    raise Usage(f"cannot read the overlay {guest.overlay}: {exc.strerror}") from None
                digest, _size = bundle.digest_of(path)
                inputs[digest[:16]] = {"kind": "overlay", "path": path, "sha256": digest}
                guest.overlay_input = digest[:16]
        return inputs

    def ls(self, args, want_json: bool) -> int:
        code, doc = self.call(["ls"] + (["--all"] if args.all else []))
        rows = [f"{t['id']}  {t['status']:<10} {t['cleanup']:<16} {t['name']}  {_counts(t['counts'])}" for t in doc["tours"]]
        self.emit(doc, "\n".join(rows) or "no tours", want_json)
        return code

    def show_receipt(self, tour: dict) -> None:
        """A finished tour ends with its receipt on stderr (stdout keeps only the tour text or JSON)."""
        receipt = tour.get("receipt") or tour_receipt(tour)
        if receipt.get("final"):
            for line in render_tour_receipt(receipt, tour.get("id")):
                print(line, file=self.err)

    def status(self, args, want_json: bool) -> int:
        code, doc = self.call(["status", _tour(args.tour)])
        self.emit(doc, render_tour(doc["tour"]), want_json)
        self.show_receipt(doc["tour"])
        return code

    def wait(self, args, want_json: bool) -> int:
        code, doc = self.call(["wait", _tour(args.tour), f"--{args.mode}", "--timeout", repr(float(args.timeout))])
        self.emit(doc, render_tour(doc["tour"]), want_json)
        if not doc.get("satisfied", True):
            self.say(f"still running after {args.timeout:g}s. Try: motel tour wait {args.tour}")
        self.show_receipt(doc["tour"])
        return code

    def cancel(self, args, want_json: bool) -> int:
        code, doc = self.call(["cancel", _tour(args.tour)])
        self.emit(doc, render_tour(doc["tour"]), want_json)
        self.show_receipt(doc["tour"])
        return code

    def result(self, args, want_json: bool) -> int:
        code, doc = self.call(["result", _tour(args.tour), _guest(args.guest)])
        self.emit(doc, render_result(doc["result"]), want_json)
        return code

    def logs(self, args, want_json: bool) -> int:
        code, doc = self.call(["logs", _tour(args.tour), _guest(args.guest), "--stream", args.stream, "--tail", str(args.tail)])
        if want_json:
            self.emit(doc, "", True)
        else:
            self.say("the text below is untrusted guest output")
            print(doc["text"], file=self.out, end="" if doc["text"].endswith("\n") else "\n")
        return code

    def fetch(self, args, want_json: bool) -> int:
        tour, guest = _tour(args.tour), _guest(args.guest)
        _c, reply = self.call(["result", tour, guest])
        record = reply["result"]
        verified = record["verified"]
        out = {"schema": SCHEMA, "ok": True, "tour": tour, "guest": guest, "branch": None, "tip": None, "artifacts": []}
        work = tempfile.mkdtemp(prefix="motel-fetch-")
        try:
            os.chmod(work, 0o700)
            if verified.get("bundle"):
                path = os.path.join(work, "result.bundle")
                self.pull(["fetch", tour, guest], path, verified["bundle"]["sha256"], verified["bundle"]["bytes"])
                out["tip"] = bundle.import_bundle(path, record["branch"], self.cwd, self.git)
                out["branch"] = record["branch"]
            if args.artifacts:
                out["artifacts"] = self.save_artifacts(tour, guest, verified["artifacts"], args.artifacts)
        finally:
            shutil.rmtree(work, ignore_errors=True)
        if not verified.get("bundle") and not out["artifacts"]:
            self.say(f"guest {guest} left no commits" + ("" if args.artifacts else " (artifacts: add --artifacts DIR)"))
        text = "\n".join([out["branch"]] if out["branch"] else []) + "\n".join(out["artifacts"])
        if out["branch"]:
            self.say(f"imported {out['tip'][:12]} as branch {out['branch']}; nothing was checked out or merged. "
                     f"Try: git log {out['branch']}")
        self.emit(out, text.strip(), want_json)
        return EXIT_OK

    def pull(self, args: list, dest: str, sha256: str, size: int) -> None:
        """Get a file from the desk into ``dest`` and check it against the digest in the result record."""
        code, raw, stderr = self.runner([*args], None)
        if code != 0:
            try:
                doc = json.loads(raw or b"{}")
            except ValueError:
                doc = {}
            raise _DeskError(code, doc if isinstance(doc, dict) and doc else {"error": "the front desk could not send the file", "hint": "motel doctor"})
        import hashlib
        if hashlib.sha256(raw).hexdigest() != sha256 or len(raw) != size:
            raise Fail("the file the front desk sent does not match the recorded digest; refused", "motel tour result " + args[1] + " " + args[2])
        fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        with os.fdopen(fd, "wb") as handle:
            handle.write(raw)

    def save_artifacts(self, tour: str, guest: str, items: list, directory: str) -> list:
        saved = []
        root = os.path.abspath(directory)
        os.makedirs(root, mode=0o700, exist_ok=True)
        if os.path.islink(root):
            raise Usage("the artifacts directory must not be a link")
        for item in items:
            rel = item["path"]
            if not art.safe_relpath(rel):
                raise Fail("the result lists an unsafe artifact path; refused", "motel tour result " + tour + " " + guest)
            target = os.path.join(root, rel)
            os.makedirs(os.path.dirname(target), mode=0o700, exist_ok=True)
            real = os.path.realpath(os.path.dirname(target))
            if real != root and not real.startswith(root + os.sep):
                raise Usage("an artifact would be written outside the artifacts directory")
            self.pull(["fetch", tour, guest, "--artifact", rel], target, item["sha256"], item["bytes"])
            saved.append(target)
        return saved


class _DeskError(Exception):
    def __init__(self, code: int, doc: dict):
        super().__init__(doc.get("error", "failed"))
        self.code, self.doc = code, doc


def _tour(value: str) -> str:
    if not TOUR_ID_RE.fullmatch(value or ""):
        raise Usage(f"bad tour id {value!r}", "motel tour ls")
    return value


def _guest(value: str) -> str:
    if not spec.GUEST_ID_RE.fullmatch(value or ""):
        raise Usage(f"bad guest id {value!r}", "motel tour status TOUR")
    return value


def _counts(counts: dict) -> str:
    return ", ".join(f"{n} {s}" for s, n in sorted(counts.items()))


def render_tour(tour: dict) -> str:
    head = (f"{tour['id']} · {tour['name']} · {tour['status']} · cleanup {tour['cleanup']} · "
            f"EUR {tour['budget']['spent_eur']} spent of {tour['budget']['max_eur']}")
    rows = []
    for g in tour["guests"]:
        res = g.get("result") or {}
        extra = f"  branch {res['branch']}" if res.get("bundle") else ""
        rows.append(f"  {g['id']:<24} {g['status']:<10} cleanup {g['cleanup']:<16} attempts {g['attempts']}{extra}")
        for a in reversed(g.get("attempt_list") or []):        # a guest still booking (or that never ran): the last thing seen
            why = (a.get("boot") or {}).get("summary") if g["status"] in ("queued", "booking") else None
            why = why or a.get("error")
            if why and not (g.get("result") or {}).get("bundle"):
                rows.append(f"    {a['id']} · {a.get('phase')} · {why}")
                break
    return "\n".join([head, *rows])


def render_result(record: dict) -> str:
    v = record["verified"]
    r = record.get("runner_reported") or {}
    lines = [f"{record['tour']} / {record['guest']} · {record['status']} · cleanup {record['cleanup']} · auth {record.get('auth')}",
             f"  verified by motel: {v['duration_s']}s · cost EUR {v['cost_eur']} · "
             f"{v['changed_files_total']} file(s) changed · bundle {'yes' if v['bundle'] else 'no'} · {len(v['artifacts'])} artifact(s)",
             f"  reported by the runner on the guest (not observed by motel): exit {r.get('exit_code')} · {r.get('termination')}"]
    usage = v.get("usage") or {}
    lines.append("  model use: " + ("not metered" if usage.get("metered") is False else f"USD {usage.get('usd')}"))
    for text in record.get("violations", []):
        lines.append(f"  refused: {text}")
    claims = (record.get("guest_claims") or {}).get("result")
    if claims is not None:
        lines.append("  guest claims (untrusted): " + json.dumps(claims)[:400])
    return "\n".join(lines)


def main(argv=None, **kw) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    cli = Cli(**kw)
    want_json = "--json" in argv
    try:
        args = build_parser().parse_args(argv)
        return getattr(cli, args.verb)(args, want_json)
    except _DeskError as exc:
        doc = exc.doc
        if want_json:
            print(json.dumps(doc, indent=1, sort_keys=True), file=cli.out)
        else:
            print(f"motel: {doc.get('error', 'failed')}. Try: {doc.get('hint', 'motel tour ls')}", file=cli.err)
        return exc.code
    except Fail as exc:
        print(json.dumps(exc.as_json()) if want_json else exc.line(), file=cli.out if want_json else cli.err)
        return exc.code
    except Exception as exc:  # noqa: BLE001
        from motel.errors import MotelError
        if isinstance(exc, MotelError):
            print(json.dumps(exc.as_json()) if want_json else exc.line(), file=cli.out if want_json else cli.err)
            return exc.code
        raise



if __name__ == "__main__":
    raise SystemExit(main())
