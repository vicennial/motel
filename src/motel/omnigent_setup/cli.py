"""``motel omnigent``: make motel an Omnigent sandbox provider with one command (EXPERIMENTAL).

    motel omnigent setup [--config PATH] [--server-url URL] [--server-type TYPE] [--ttl TTL] [--dry-run] [--yes] [--json]
    motel omnigent status [--config PATH] [--json]
    motel omnigent mcp-config

``main(argv)`` takes the arguments AFTER the word ``omnigent``. Omnigent is the orchestrator (sessions, UI); motel only
supplies and deletes servers. Setup never starts or stops the Omnigent server and never creates a cabin.
"""

from __future__ import annotations

import argparse
import json
import os
import stat
import sys
import tempfile

from motel.errors import MotelError, Refused, SystemFault, Usage
from motel.omnigent_setup import config_edit
from motel.omnigent_setup.probe import (
    INSTALL_OMNIGENT_HINT,
    Check,
    Deps,
    OmnigentEnv,
    check_cabins,
    check_provider,
    find_omnigent,
    install_command,
    omnigent_version,
    quote,
)

SCHEMA = "motel/v1"
DEFAULT_PORT = 6767
MAX_CONFIG_BYTES = 1024 * 1024
VERIFY_COMMAND = "omnigent sandbox create --help"
WOULD = {"add": "add the block to", "update": "update the block in", "replace": "replace the block in"}
DID = {"create": "created", "add": "added the block to", "update": "updated the block in", "replace": "replaced the block in"}
STEP_NAMES = ("Cabins work", "Omnigent is found", "The provider is visible to Omnigent", "The sandbox block is written")

HELP_DESCRIPTION = """\
EXPERIMENTAL. Make motel a sandbox provider for Omnigent. Omnigent runs the sessions and the UI; motel only supplies and
deletes servers (cabins) for it. Cabins must already work on this machine (see "First cabin" in docs/cabins.md).

`setup` checks that cabins work, finds Omnigent, installs motel into Omnigent's environment if it is missing there,
and writes the `sandbox:` block into the Omnigent server config. It does not restart the server and does not create a
cabin. The first session on a bare cabin still takes minutes: the cabin installs Omnigent and the agent CLIs."""

HELP_EXAMPLES = """\
examples:
  motel omnigent setup --dry-run     say what setup would do, change nothing
  motel omnigent setup               do it, then print the restart line
  motel omnigent status              check cabins, Omnigent, the provider and the config block
  motel omnigent mcp-config          print the MCP snippet that lets an agent start tours"""


class OmnigentParser(argparse.ArgumentParser):
    def error(self, message):
        raise Usage(message, "motel omnigent --help")

    def exit(self, status=0, message=None):
        if status:
            raise Usage((message or "").strip(), "motel omnigent --help")
        raise SystemExit(status)


def build_parser() -> argparse.ArgumentParser:
    common = OmnigentParser(add_help=False, allow_abbrev=False)
    common.add_argument("--json", action="store_true", help="machine-readable output")
    target = OmnigentParser(add_help=False, allow_abbrev=False)
    target.add_argument("--config", metavar="PATH", help="the Omnigent server config file (default: the file of the running "
                        "server, else ~/.omnigent/server.yaml)")
    target.add_argument("--server-url", metavar="URL", help="the server's own URL (default: http://127.0.0.1:6767, or the "
                        "port of the running server)")
    target.add_argument("--server-type", metavar="TYPE", help="cabin server type for the motel: block (default: not written)")
    target.add_argument("--ttl", metavar="TTL", help="cabin lease length for the motel: block, e.g. 2h (default: not written)")
    parser = OmnigentParser(prog="motel omnigent", allow_abbrev=False, formatter_class=argparse.RawDescriptionHelpFormatter,
                            description=HELP_DESCRIPTION, epilog=HELP_EXAMPLES)
    sub = parser.add_subparsers(dest="verb", required=True, parser_class=OmnigentParser, metavar="VERB")
    p = sub.add_parser("setup", parents=[common, target], allow_abbrev=False, help="make motel a provider for the Omnigent server")
    p.add_argument("--dry-run", action="store_true", help="say what would be done and change nothing")
    p.add_argument("--yes", action="store_true", help="replace a sandbox block that names another provider")
    sub.add_parser("status", parents=[common, target], allow_abbrev=False,
                   help="check cabins, Omnigent, the provider and the config block (read-only)")
    sub.add_parser("mcp-config", allow_abbrev=False, help="print the MCP server snippet that lets an agent start tours")
    return parser


def _error(message: str, hint: str, code: int) -> MotelError:
    exc = MotelError(message, hint)
    exc.code = code
    return exc


class Omnigent:
    def __init__(self, deps: Deps | None = None, out=None, err=None):
        self.deps = deps or Deps()
        self.out, self.err = out or sys.stdout, err or sys.stderr
        self.steps: list[dict] = []
        self.json = False

    # -- output
    def say(self, text: str) -> None:
        if not self.json:
            print(text, file=self.out)

    def step(self, number: int, status: str, detail: str, ok: bool = True) -> None:
        name = STEP_NAMES[number - 1]
        self.steps.append({"step": name, "ok": ok, "status": status, "detail": detail})
        self.say(f"[{number}/4] {name}: {status}. {detail}")

    # -- shared pieces
    def want(self, args) -> tuple[config_edit.Want, list[dict]]:
        servers = self.deps.servers()
        url = args.server_url
        if not url:
            port = next((s["port"] for s in servers if s.get("port")), DEFAULT_PORT)
            url = f"http://127.0.0.1:{port}"
        want = config_edit.Want(url, bool(args.server_url), args.server_type, args.ttl, bool(getattr(args, "yes", False)))
        try:
            config_edit.check_want(want)
        except ValueError as exc:
            raise Usage(str(exc), "motel omnigent setup --help") from None
        return want, servers

    def config_path(self, args, servers: list[dict]) -> tuple[str, str]:
        if args.config:
            return os.path.abspath(os.path.expanduser(args.config)), "--config"
        for server in servers:
            if server.get("config"):
                return server["config"], f"the running server (pid {server['pid']})"
        base = self.deps.environ.get("OMNIGENT_CONFIG_HOME") or os.path.join(self.deps.home, ".omnigent")
        return os.path.join(base, "server.yaml"), "default"

    @staticmethod
    def read_config(path: str) -> str | None:
        try:
            info = os.stat(path)
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise SystemFault(f"cannot read {path}: {exc.strerror}", "check the file's permissions") from None
        if not stat.S_ISREG(info.st_mode):
            raise SystemFault(f"{path} is not a regular file", "pass the server config file with --config")
        if info.st_size > MAX_CONFIG_BYTES:
            raise SystemFault(f"{path} is larger than 1 MiB, so it is not an Omnigent server config", "pass --config PATH")
        try:
            with open(path, encoding="utf-8", newline="") as handle:
                return handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            raise SystemFault(f"cannot read {path} as UTF-8 text ({exc.__class__.__name__})", "check the file") from None

    def verify_yaml(self, env: OmnigentEnv, old: str | None, new: str, parse: bool) -> str | None:
        """Parse the new file with the YAML library of Omnigent's own environment. Returns an error text, or None when fine."""
        rc, out, err = self.deps.run([env.python, "-I", "-c", VERIFY_SCRIPT], input=json.dumps(
            {"old": old, "new": new, "parse": parse}), timeout=60)
        try:
            doc = json.loads(out.strip().splitlines()[-1])
        except (ValueError, IndexError):
            return f"could not check the edited file with Omnigent's Python ({(err or out).strip()[:160] or f'exit {rc}'})"
        if not doc.get("available"):
            return None                       # no PyYAML there: the text-level checks stand alone
        return None if doc.get("ok") else str(doc.get("error") or "the edited file did not pass the check")

    def write_config(self, path: str, old: str | None, new: str) -> str | None:
        """Back the file up, then write it atomically with its original mode. Returns the backup path (None for a new file)."""
        real = os.path.realpath(path)
        backup = None
        if old is not None:
            mode = stat.S_IMODE(os.stat(real).st_mode)
            backup = self.backup(real, mode)
        else:
            mode = 0o600
            os.makedirs(os.path.dirname(real), mode=0o700, exist_ok=True)
        if old is not None and self.read_config(real) != old:
            raise SystemFault(f"{path} changed while setup was running, so nothing was written", "run the command again")
        fd, tmp = tempfile.mkstemp(dir=os.path.dirname(real), prefix=".motel-omnigent-")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="") as handle:
                handle.write(new)
                handle.flush()
                os.fchmod(handle.fileno(), mode)
                os.fsync(handle.fileno())
            os.replace(tmp, real)
        except BaseException:
            try:
                os.unlink(tmp)
            except OSError:
                pass
            raise
        return backup

    def backup(self, real: str, mode: int) -> str:
        stamp = self.deps.now().strftime("%Y%m%dT%H%M%S")
        with open(real, "rb") as handle:
            data = handle.read()
        for n in range(10):
            target = f"{real}.bak-{stamp}" + (f"-{n}" if n else "")
            try:
                fd = os.open(target, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(fd, "wb") as out:
                out.write(data)
                os.fchmod(out.fileno(), mode)
            return target
        raise SystemFault(f"cannot make a backup of {real}: too many files with the same name", "move the old backups away")

    # -- setup
    def setup(self, args) -> int:
        self.json = args.json
        dry = args.dry_run
        want, servers = self.want(args)
        path, source = self.config_path(args, servers)
        result = {"schema": SCHEMA, "ok": False, "dry_run": dry, "changed": False, "steps": self.steps,
                  "config": {"path": path, "source": source, "action": None, "backup": None}}
        try:
            self.run_steps(args, want, servers, path, result, dry)
        except MotelError as exc:
            result.update(ok=False, error=exc.message, hint=exc.hint, code=exc.code)
            if args.json:
                print(json.dumps(result, indent=1, sort_keys=True), file=self.out)
            else:
                print(exc.line(), file=self.err)
            return exc.code
        result["ok"] = True
        if args.json:
            print(json.dumps(result, indent=1, sort_keys=True), file=self.out)
        return 0

    def fail_step(self, number: int, check: Check, fallback_hint: str) -> MotelError:
        self.step(number, "FAILED", check.detail, ok=False)
        return _error(check.detail, check.hint or fallback_hint, check.code or 5)

    def run_steps(self, args, want, servers, path, result, dry) -> None:
        deps = self.deps
        cabins = check_cabins(deps)
        if not cabins.ok:
            raise self.fail_step(1, cabins, "motel cabin doctor")
        self.step(1, "ok", cabins.detail)

        env = find_omnigent(deps)
        if env is None:
            raise self.fail_step(2, Check(False, "no omnigent command was found on PATH", INSTALL_OMNIGENT_HINT, 8), INSTALL_OMNIGENT_HINT)
        if env.version is None:
            raise self.fail_step(2, Check(False, f"{env.executable} runs, but {env.python} cannot read the omnigent version",
                                          INSTALL_OMNIGENT_HINT, 8), INSTALL_OMNIGENT_HINT)
        self.step(2, "ok", f"omnigent {env.version} at {env.executable} ({env.kind}, python {env.python})")

        provider = check_provider(deps, env)
        if provider.ok:
            self.step(3, "ok", provider.detail)
        else:
            cmd = install_command(deps, env)
            if cmd is None:
                manual = f"{env.python} -m pip install {deps.motel_spec()}"
                raise self.fail_step(3, Check(False, f"{provider.detail}, and {env.python} is not a virtual environment, "
                                              "so setup does not install into it", manual, 8), manual)
            if dry:
                self.step(3, "would install", f"{provider.detail}. Would run: {quote(cmd)}")
            else:
                self.say(f"      running: {quote(cmd)}")
                rc, out, err = deps.run(cmd, timeout=900)
                if rc != 0:
                    tail = (err or out).strip().splitlines()[-1:] or [f"exit {rc}"]
                    raise self.fail_step(3, Check(False, f"installing motel into the Omnigent environment failed: {tail[0][:200]}",
                                                  quote(cmd), 5), quote(cmd))
                provider = check_provider(deps, env)
                if not provider.ok:
                    raise self.fail_step(3, Check(False, f"motel was installed, but {provider.detail}", quote(cmd), 5), quote(cmd))
                if omnigent_version(deps, env) != env.version:
                    raise self.fail_step(3, Check(False, "the omnigent version changed during the install; check it with: "
                                                  "omnigent --version", "omnigent --version", 5), "omnigent --version")
                self.step(3, "installed", provider.detail)

        old = self.read_config(path)
        plan = config_edit.plan_config(old, want)
        result["config"]["action"] = plan.action
        if plan.action == "manual":
            self.step(4, "not written", f"{path}: {plan.reason}", ok=False)
            result["config"]["block"] = plan.block
            self.say(f"Add this block to {path} by hand:\n\n{plan.block}")
            raise _error(f"{plan.reason}. {path} was not changed", f"paste the block above into {path}, then restart the Omnigent server", Refused.code)
        if plan.action == "unchanged":
            self.step(4, "unchanged", f"{path} already has the motel sandbox block (nothing to change)")
            self.report_next(path, servers, result, changed=False)
            return
        problem = self.verify_yaml(env, old, plan.text, parse=provider.ok)
        if problem:
            self.step(4, "FAILED", f"the edited {path} did not pass the check ({problem}); nothing was written", ok=False)
            result["config"]["block"] = plan.block
            self.say(f"Add this block to {path} by hand:\n\n{plan.block}")
            raise _error(f"the edit of {path} did not pass the check: {problem}", f"paste the block above into {path}", Refused.code)
        if dry:
            what = f"would create {path}" if old is None else f"would {WOULD[plan.action]} {path} (a backup is made first)"
            self.step(4, "would change", what)
        else:
            backup = self.write_config(path, old, plan.text)
            result["config"]["backup"] = backup
            result["changed"] = True
            self.step(4, "written", f"{DID[plan.action]} {path}" + (f" (backup: {backup})" if backup else ""))
        self.report_next(path, servers, result, changed=not dry, dry=dry)

    def report_next(self, path: str, servers: list[dict], result: dict, *, changed: bool, dry: bool = False) -> None:
        running = list(servers)
        using = [s for s in running if s.get("config") and os.path.realpath(s["config"]) == os.path.realpath(path)]
        restart = (f"Restart the Omnigent server so that it loads {path}. Setup does not restart it." if changed or dry else
                   f"Restart the Omnigent server only if it was started before {path} had this block. Setup does not restart it.")
        start = f"omnigent server -c {path}"
        lines = [restart, f"  Start it with this config: {start}   (keep your usual --host, --port and other options)"]
        if running and not using:
            lines.append(f"  A server is running (pid {running[0]['pid']}) and it was not started with this file: add -c {path}.")
        elif using:
            lines.append(f"  The running server (pid {using[0]['pid']}) uses this file: stop it and start it again.")
        lines.append(f"Then check that Omnigent lists the provider: {VERIFY_COMMAND}   (motel appears under --provider)")
        result["next"] = {"restart": start, "verify": VERIFY_COMMAND, "server_running": bool(running), "server_uses_config": bool(using)}
        if dry:
            self.say("Dry run: nothing was changed. After a real run:")
        elif not changed:
            self.say("Nothing changed.")
        for line in lines:
            self.say(line)

    # -- status
    def status(self, args) -> int:
        self.json = args.json
        want, servers = self.want(args)
        path, source = self.config_path(args, servers)
        rows: list[dict] = []

        def row(name, ok, detail, level=None):
            rows.append({"check": name, "ok": ok, "level": level or ("ok" if ok else "fail"), "detail": detail})

        cabins = check_cabins(self.deps)
        row("cabins", cabins.ok, cabins.detail + ("" if cabins.ok else f" (next: {cabins.hint})"))
        env = find_omnigent(self.deps)
        if env is None or env.version is None:
            row("omnigent", False, f"not found (next: {INSTALL_OMNIGENT_HINT})")
            row("provider visible", False, "not checked: Omnigent was not found")
        else:
            row("omnigent", True, f"omnigent {env.version} at {env.executable} ({env.kind})")
            provider = check_provider(self.deps, env)
            row("provider visible", provider.ok, provider.detail + ("" if provider.ok else " (next: motel omnigent setup)"))
        config = {"path": path, "source": source, "exists": False, "present": False, "matches": False, "action": None}
        try:
            text = self.read_config(path)
            plan = config_edit.plan_config(text, want)
            config.update(exists=text is not None, present=plan.present or plan.action == "unchanged", action=plan.action,
                          matches=plan.action == "unchanged")
            if text is None:
                row("config block", False, f"{path} does not exist (next: motel omnigent setup)")
            else:
                row("config block", config["present"], f"{path} " + ("has a motel sandbox block" if config["present"]
                                                                    else "has no motel sandbox block (next: motel omnigent setup)"))
            if config["matches"]:
                row("block matches setup", True, "the block is what setup would write")
            elif plan.action == "manual":
                row("block matches setup", False, f"setup would not edit it: {plan.reason}")
            else:
                row("block matches setup", False, "setup would change the file (next: motel omnigent setup --dry-run)")
        except MotelError as exc:
            row("config block", False, exc.message)
            row("block matches setup", False, "not checked")
        using = [s for s in servers if s.get("config") and os.path.realpath(s["config"]) == os.path.realpath(path)]
        if not servers:
            row("server", True, "no running omnigent server was found", "info")
        elif using:
            row("server", True, f"the running server (pid {using[0]['pid']}) uses {path}", "info")
        else:
            row("server", False, f"a server is running (pid {servers[0]['pid']}) without -c {path}; the block is not loaded "
                                 "until the server is restarted with it", "warn")
        ready = all(r["ok"] for r in rows if r["level"] != "warn" and r["check"] != "server")
        doc = {"schema": SCHEMA, "ok": ready, "checks": rows, "config": config,
               "omnigent": {"found": env is not None, "version": env.version if env else None,
                            "executable": env.executable if env else None, "python": env.python if env else None,
                            "kind": env.kind if env else None}}
        if args.json:
            print(json.dumps(doc, indent=1, sort_keys=True), file=self.out)
        else:
            width = max(len(r["check"]) for r in rows)
            for r in rows:
                print(f"{r['check'].ljust(width)}  {r['level'].ljust(4)}  {r['detail']}", file=self.out)
            print("ready" if ready else "not ready: run motel omnigent setup", file=self.out)
        return 0 if ready else 8


# Runs inside Omnigent's own Python, which has PyYAML and the real parser for the sandbox block.
VERIFY_SCRIPT = r"""
import importlib, json, sys
try:
    import yaml
except ImportError:
    print(json.dumps({"available": False})); raise SystemExit(0)
req = json.load(sys.stdin)
out = {"available": True, "ok": False}
try:
    old = yaml.safe_load(req["old"]) if req["old"] is not None else {}
    new = yaml.safe_load(req["new"])
    old = old or {}
    if not isinstance(old, dict) or not isinstance(new, dict):
        raise ValueError("the file is not a YAML mapping")
    rest = lambda d: {k: v for k, v in d.items() if k != "sandbox"}
    if rest(old) != rest(new):
        raise ValueError("a key other than sandbox would change")
    block = new.get("sandbox")
    entries = block.get("providers") if isinstance(block, dict) and "providers" in block else [block]
    if not any(isinstance(e, dict) and e.get("provider") == "motel" for e in entries or []):
        raise ValueError("the edited sandbox block does not name motel")
    if req.get("parse"):
        importlib.import_module("omnigent.server.managed_hosts").parse_sandbox_config(block)
    out["ok"] = True
except Exception as exc:
    out["error"] = (type(exc).__name__ + ": " + str(exc))[:300]
print(json.dumps(out))
"""


def mcp_snippet() -> dict:
    """The MCP server entry for ``motel mcp --with-tours`` (the same invocation as ``docs/agents.md``)."""
    return {"mcpServers": {"motel": {"command": "motel", "args": ["mcp", "--with-tours"]}}}


MCP_NOTE = (
    "motel: add this to the MCP settings of an agent that runs on a machine where `motel cabin doctor` passes (for example "
    "`claude mcp add-json motel '{\"command\":\"motel\",\"args\":[\"mcp\",\"--with-tours\"]}'`, or a project .mcp.json). "
    "A cabin has no motel on it, so a session that runs inside a cabin cannot start tours.")


def main(argv=None, *, deps: Deps | None = None, out=None, err=None) -> int:
    out, err = out or sys.stdout, err or sys.stderr
    argv = list(sys.argv[1:] if argv is None else argv)
    want_json = "--json" in argv
    try:
        if not argv or argv[0] in ("-h", "--help"):
            print(build_parser().format_help().rstrip(), file=out)
            return 0 if argv else Usage.code
        args = build_parser().parse_args(argv)
        tool = Omnigent(deps, out, err)
        if args.verb == "setup":
            return tool.setup(args)
        if args.verb == "status":
            return tool.status(args)
        print(json.dumps(mcp_snippet(), indent=2), file=out)
        print(MCP_NOTE, file=err)
        return 0
    except MotelError as exc:
        print(json.dumps(exc.as_json()) if want_json else exc.line(), file=out if want_json else err)
        return exc.code


if __name__ == "__main__":
    raise SystemExit(main())
