#!/usr/bin/env python3
"""Live test harness for motel. Runs on the controller machine and drives a throwaway Hetzner test server.

    python3 tests/live/run_live.py --hcloud-cmd 'WRAPPER --' --token-file PATH [--ref REF] [--cabins] [--tours] [--omnigent]
    python3 tests/live/run_live.py --dry-run ...          # print the plan and every command, execute nothing
    python3 tests/live/run_live.py --cleanup RUN_ID ...   # delete everything labelled for that run (both projects)

The cabins phase (--cabins) runs in a second, dedicated cloud project and needs both --cabins-hcloud-cmd and --cabins-token-file.
The tours phase (--tours) implies --cabins (tours run on cabins): it sets cabins up, installs tours as real root and runs two tours of two
command guests each (no model, no real key). Alone, --tours skips the cabin checks themselves; add --cabins to run both.

The omnigent phase (--omnigent) implies --cabins too: it installs Omnigent (--omnigent-version) and motel into a venv as the cabin user, starts a
disposable Omnigent server on the test server (loopback, throwaway HOME, no key) with sandbox.provider: motel, has a managed session
provision ONE cabin, checks the host registers over the relay, a command returns its output, keep_alive extends the cabin and terminate
checks it out. No model is called. Add --tours to run both phases.

Exit codes: 0 all checks passed (skips allowed), 1 a check failed, 2 usage, 3 preflight refused (project not empty),
4 teardown left resources behind, 130 interrupted (teardown still ran).
Stdlib only, Python 3.11+. See tests/live/README.md.
"""

from __future__ import annotations

import argparse
import json
import os
import secrets
import shlex
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

from motel_live.cloud import Hcloud, HcloudError  # noqa: E402
from motel_live.core import RUN_ID_RE, Clock, Redactor, Report, Runner  # noqa: E402
from motel_live.omnigent_phase import valid_version  # noqa: E402
from motel_live.session import EXIT_LEAK, EXIT_OK, EXIT_USAGE, Session  # noqa: E402


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="run_live.py", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--hcloud-cmd", default="hcloud", help="hcloud command or wrapper, split with shlex (default: hcloud)")
    p.add_argument("--ssh-cmd", default="ssh")
    p.add_argument("--scp-cmd", default="scp")
    p.add_argument("--token-file", help="controller-side file holding the Read & Write project token; copied to the server as root 0600")
    p.add_argument("--ref", default="HEAD", help="git ref of motel to install on the server (default HEAD)")
    p.add_argument("--repo", default=None, help="git checkout that holds the ref (default: the checkout this script lives in)")
    p.add_argument("--run-id", default=None, help="label value for this run (default: generated)")
    p.add_argument("--out-dir", default=None, help="report/log/state directory (default ./motel-live-<run-id>)")
    p.add_argument("--location", default="nbg1")
    p.add_argument("--server-type", default="cx23")
    p.add_argument("--image", default="ubuntu-24.04")
    p.add_argument("--bystander-gb", type=int, default=10)
    p.add_argument("--min-floor-gb", type=int, default=20)
    p.add_argument("--room-gb", type=int, default=8)
    p.add_argument("--suite-gb", type=int, default=10)
    p.add_argument("--skip-rooms", action="store_true")
    p.add_argument("--cabins", action="store_true", help="also run phase 2 (cabins); needs --cabins-hcloud-cmd and --cabins-token-file, "
                                                         "else the phase is skipped")
    p.add_argument("--cabins-hcloud-cmd", default=None, help="hcloud wrapper for the second, dedicated CABINS project, split with shlex "
                                                              "(the project must be empty; it must not be the --hcloud-cmd project)")
    p.add_argument("--cabins-token-file", default=None, help="controller-side file holding the Read & Write token of the cabins project; "
                                                              "copied to the server for `motel-desk cabin setup hetzner --token-file`")
    p.add_argument("--cabins-skip", default="", help="comma list of cabin parts to skip: run,firewall,disk,idle,ttl")
    p.add_argument("--tours", action="store_true", help="also run phase 3 (tours); implies --cabins and needs the same two options. "
                                                         "Without an explicit --cabins the cabin checks themselves are skipped")
    p.add_argument("--tours-skip", default="", help="comma list of tour parts to skip: isolation,restart,failure")
    p.add_argument("--tours-egress", default="pypi.org", help="the one host the tour guests may reach (an operator egress_allow entry)")
    p.add_argument("--omnigent", action="store_true", help="also run phase 4 (the Omnigent sandbox provider on a real cabin); implies --cabins "
                                                            "and needs the same two options. Without an explicit --cabins the cabin checks themselves are skipped")
    p.add_argument("--omnigent-version", default="0.21.0", help="the Omnigent release to install (and to pin on the cabin); default 0.21.0")
    p.add_argument("--omnigent-skip", default="", help="comma list of omnigent parts to skip: shell (the session shell endpoint), keepalive")
    p.add_argument("--omnigent-agent", default=None, help="name of the built-in agent the managed session binds (default: polly, else the first listed)")
    p.add_argument("--cabin-type", default="cx23")
    p.add_argument("--cabin-idle", default="30m", help="idle_timeout (cabins enforce a 30m minimum; the idle check waits about this long)")
    p.add_argument("--cabins-config", default=None, help="controller-side TOML fragment replacing the default [cabins] table "
                                                         "({cabin_type}, {idle}, {ttl} are substituted)")
    p.add_argument("--no-probe", action="store_true", help="skip the public port-22 probe from the controller")
    p.add_argument("--no-sweep", action="store_true", help="report leaked resources at teardown but do not delete them")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--cleanup", metavar="RUN_ID", help="delete everything labelled motel-live=RUN_ID (and only that), then exit")
    return p


def make_run_id(clock: Clock) -> str:
    import time
    return time.strftime("%Y%m%d-%H%M%S", time.gmtime(clock.time())) + "-" + secrets.token_hex(2)


def split(name: str, text: str) -> list:
    try:
        argv = shlex.split(text)
    except ValueError as exc:
        raise SystemExit(f"run_live: bad {name}: {exc}") from exc
    if not argv:
        raise SystemExit(f"run_live: {name} is empty")
    return argv


def cleanup_mode(args, clock: Clock) -> int:
    run_id = args.cleanup
    if not RUN_ID_RE.fullmatch(run_id):
        print(f"run_live: {run_id!r} is not a valid run id", file=sys.stderr)
        return EXIT_USAGE
    redact = Redactor()
    runner = Runner(dry_run=args.dry_run, redact=redact, echo=lambda line: print(line, file=sys.stderr))
    hc = Hcloud(runner, args.hcloud_argv, dry=args.dry_run)
    hcc = Hcloud(runner, args.cabins_hcloud_argv, dry=args.dry_run) if args.cabins_hcloud_argv else None
    state = {}
    state_path = os.path.join(args.out_dir or f"motel-live-{run_id}", "state.json")
    if os.path.exists(state_path):
        with open(state_path, encoding="utf-8") as handle:
            state = json.load(handle)
        if state.get("run_id") != run_id:
            print(f"run_live: {state_path} belongs to run {state.get('run_id')!r}, ignoring it", file=sys.stderr)
            state = {}
    try:
        before = hc.owned(run_id, state)  # the controller server ids, read before the servers are deleted
        outcome = hc.destroy_run(run_id, state)
        left = hc.owned(run_id, state) if not args.dry_run else {"servers": [], "volumes": [], "ssh_keys": []}
    except HcloudError as exc:
        print(f"run_live: cleanup failed: {exc}", file=sys.stderr)
        return EXIT_LEAK
    remaining = [f"{k} {i['id']}" for k in ("servers", "volumes", "ssh_keys") for i in left[k]]
    result = {"run_id": run_id, "deleted": outcome["deleted"], "errors": outcome["errors"],
              "left_alone_not_provably_this_run": outcome["unattributed"], "remaining": remaining}
    ok = not outcome["errors"] and not remaining
    # The cabins project: cabins that motel created for this run's test server (by its controller label), found through the server ids
    # in state.json or still labelled in the rooms project.
    if hcc is not None:
        server_ids = set(state.get("server_ids", [])) | set(before["server_ids"])
        try:
            cab = hcc.destroy_cabins(run_id, server_ids)
            cab_left, cab_unattributed = hcc.owned_cabins(run_id, server_ids) if not args.dry_run else ({}, [])
        except HcloudError as exc:
            print(f"run_live: cabins-project cleanup failed: {exc}", file=sys.stderr)
            return EXIT_LEAK
        cab_remaining = [f"{kind} {ident}" for kind, items in cab_left.items() for ident in items]
        result["cabins_project"] = {"deleted": cab["deleted"], "errors": cab["errors"], "left_alone_not_provably_this_run": cab["unattributed"],
                                    "remaining": cab_remaining}
        ok = ok and not cab["errors"] and not cab_remaining
    elif state.get("cabins_project"):
        print(f"run_live: run {run_id} used a cabins project; rerun with --cabins-hcloud-cmd to sweep it (cabin servers may still bill)",
              file=sys.stderr)
        result["cabins_project"] = "not swept: --cabins-hcloud-cmd missing"
        ok = False
    print(json.dumps(result, indent=1))
    return EXIT_OK if ok else EXIT_LEAK


def main(argv=None, *, clock: Clock | None = None) -> int:
    clock = clock or Clock()
    args = build_parser().parse_args(argv)
    if (args.tours or args.omnigent) and not args.cabins:   # tours and omnigent run on cabins: they are set up, but only --cabins asks for the cabin checks
        args.cabins = True
        args.cabins_skip = ",".join(filter(None, [args.cabins_skip, "run,firewall,disk,idle,ttl"]))
    args.hcloud_argv, args.ssh_argv, args.scp_argv = (split("--hcloud-cmd", args.hcloud_cmd), split("--ssh-cmd", args.ssh_cmd),
                                                      split("--scp-cmd", args.scp_cmd))
    args.cabins_hcloud_argv = split("--cabins-hcloud-cmd", args.cabins_hcloud_cmd) if args.cabins_hcloud_cmd else None
    if args.cabins_hcloud_argv == args.hcloud_argv:
        print("run_live: --cabins-hcloud-cmd is the same command as --hcloud-cmd; the cabins phase needs a different, dedicated project",
              file=sys.stderr)
        return EXIT_USAGE
    if args.cleanup:
        return cleanup_mode(args, clock)
    if args.omnigent and not valid_version(args.omnigent_version):
        print(f"run_live: --omnigent-version {args.omnigent_version!r} is not a release version such as 0.21.0", file=sys.stderr)
        return EXIT_USAGE
    run_id = args.run_id or make_run_id(clock)
    if not RUN_ID_RE.fullmatch(run_id):
        print(f"run_live: run id {run_id!r} must match {RUN_ID_RE.pattern}", file=sys.stderr)
        return EXIT_USAGE
    if not args.token_file and not args.dry_run:
        print("run_live: --token-file is required (a controller-side file holding the project token)", file=sys.stderr)
        return EXIT_USAGE
    args.repo = args.repo or subprocess.run(["git", "-C", HERE, "rev-parse", "--show-toplevel"], capture_output=True, text=True).stdout.strip() or HERE
    redact = Redactor()
    tokens = {}
    for flag, path in (("--token-file", args.token_file), ("--cabins-token-file", args.cabins_token_file)):
        if path and not args.dry_run:
            try:
                with open(path, encoding="utf-8") as handle:
                    tokens[flag] = handle.read().strip()
            except OSError as exc:
                print(f"run_live: cannot read {flag}: {exc.strerror}", file=sys.stderr)
                return EXIT_USAGE
            redact.add(tokens[flag])
    if len(tokens) == 2 and tokens["--token-file"] == tokens["--cabins-token-file"]:
        print("run_live: --cabins-token-file holds the same token as --token-file; the cabins phase needs the token of a second project",
              file=sys.stderr)
        return EXIT_USAGE
    run_dir = None
    if not args.dry_run:
        run_dir = os.path.abspath(args.out_dir or f"motel-live-{run_id}")
        os.makedirs(run_dir, mode=0o700, exist_ok=True)
        os.chmod(run_dir, 0o700)
    runner = Runner(dry_run=args.dry_run, redact=redact, log_path=os.path.join(run_dir, "log.txt") if run_dir else None,
                    echo=lambda line: print(line, file=sys.stderr))
    report = Report(run_id, redact, dry=args.dry_run, path=os.path.join(run_dir, "report.json") if run_dir else None)
    report.meta["args"] = {k: v for k, v in vars(args).items() if k not in ("token_file", "cabins_token_file")}
    session = Session(args, run_id, clock=clock, redact=redact, runner=runner, report=report, run_dir=run_dir)
    rc = session.run()
    report.meta["exit_code"] = rc
    report.save()
    print(report.summary())
    if run_dir:
        print(f"report: {report.path}\nlog:    {runner.log_path}")
    if rc == EXIT_LEAK:
        more = f" --cabins-hcloud-cmd {shlex.quote(args.cabins_hcloud_cmd)}" if args.cabins_hcloud_cmd else ""
        print(f"\n!! RESOURCES MAY STILL EXIST AND BILL. Run: run_live.py --cleanup {run_id} --hcloud-cmd {shlex.quote(args.hcloud_cmd)}{more}",
              file=sys.stderr)
    return rc


if __name__ == "__main__":
    raise SystemExit(main())
