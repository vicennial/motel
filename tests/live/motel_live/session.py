"""One live run: preflight, provision the throwaway server, install motel, run the scenarios, always tear down."""

from __future__ import annotations

import json
import os
import signal
import sys
from contextlib import contextmanager

from motel_live import cabins, cloud, omnigent_phase, rooms, tours
from motel_live.cloud import Hcloud, HcloudError
from motel_live.core import FAIL, Abort, Clock, Redactor, Report, Runner, parse_json
from motel_live.remote import PREP_TOKEN_SCRIPT, REMOTE_SRC, REMOTE_SRC_PARENT, Remote
from motel_live.rooms import OUTSIDER, USER, rooms_config

EXIT_OK, EXIT_FAIL, EXIT_USAGE, EXIT_REFUSED, EXIT_LEAK, EXIT_INTERRUPTED = 0, 1, 2, 3, 4, 130
REMOTE_TOKEN = "/root/.motel-live-token"
REMOTE_TARBALL = f"{REMOTE_SRC_PARENT}/motel-src.tar.gz"


def archive_argv(repo: str, sha: str, tarball: str) -> list:
    """``git archive`` of the ref. ``tar.umask=0022`` keeps files and directories off group-write: the installer's source
    audit refuses a group-writable tree, and git's default umask (0002) would write 0664 and 0775."""
    return ["git", "-c", "tar.umask=0022", "-C", repo, "archive", "--format=tar.gz", "--prefix=motel-src/", "-o", tarball, sha]


def unpack_argvs(tarball: str = REMOTE_TARBALL, parent: str = REMOTE_SRC_PARENT) -> list:
    """Commands (run as root on the server) that unpack the source into a root-only directory. The installer runs the code
    as root, so the tree and every directory above it must be root-owned and not writable by others: not /var/tmp."""
    src = f"{parent}/motel-src"
    return [["rm", "-rf", src],
            ["tar", "-xzf", tarball, "-C", parent, "--no-same-owner"],
            ["chmod", "-R", "go-w", src],
            ["chmod", "0700", src],
            ["rm", "-f", tarball]]


class Refusal(Abort):
    """The preflight refused to create anything."""


class Interrupted(KeyboardInterrupt):
    pass


@contextmanager
def signal_guard(on_signal=None):
    """SIGTERM and SIGHUP behave like Ctrl-C, so the ``finally`` teardown runs."""
    old = {}

    def handler(signum, _frame):
        raise Interrupted(f"signal {signum}")
    try:
        for sig in (signal.SIGTERM, signal.SIGHUP):
            old[sig] = signal.signal(sig, handler)
    except ValueError:  # not the main thread
        old = {}
    try:
        yield
    finally:
        for sig, prev in old.items():
            signal.signal(sig, prev)


@contextmanager
def signals_ignored():
    old = {}
    try:
        for sig in (signal.SIGINT, signal.SIGTERM, signal.SIGHUP):
            old[sig] = signal.signal(sig, signal.SIG_IGN)
    except ValueError:
        old = {}
    try:
        yield
    finally:
        for sig, prev in old.items():
            signal.signal(sig, prev)


class Session:
    def __init__(self, args, run_id: str, *, clock: Clock, redact: Redactor, runner: Runner, report: Report, run_dir: str | None):
        self.args, self.run_id, self.clock, self.redact, self.r, self.report, self.run_dir = args, run_id, clock, redact, runner, report, run_dir
        self.dry = args.dry_run
        self.hc = Hcloud(runner, args.hcloud_argv, dry=self.dry)
        # The cabins phase runs in a second, dedicated project reached through its own wrapper and token.
        self.missing_cabins_options = [flag for flag, value in (("--cabins-hcloud-cmd", args.cabins_hcloud_argv),
                                                                ("--cabins-token-file", args.cabins_token_file)) if not value]
        self.cabins_ready = bool(args.cabins) and not self.missing_cabins_options
        self.hcc = Hcloud(runner, args.cabins_hcloud_argv, dry=self.dry) if self.cabins_ready else None
        self.state: dict = {"run_id": run_id, "server_ids": []}
        self.initial: dict = {}
        self.cabins_initial: dict | None = None  # set by the cabins-project preflight; None when that project is not in play
        self.remote: Remote | None = None
        self.created_anything = False
        self.t_created = None
        self.t_destroyed = None
        self.name = f"motel-live-{run_id}"

    # ------------------------------------------------------------- state file
    def save_state(self) -> None:
        if not self.run_dir or self.dry:
            return
        tmp = os.path.join(self.run_dir, "state.json.tmp")
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(self.state, handle, indent=1, sort_keys=True)
        os.replace(tmp, os.path.join(self.run_dir, "state.json"))

    def path(self, name: str) -> str:
        return os.path.join(self.run_dir or "<run-dir>", name)

    # ------------------------------------------------------------- the run
    def run(self) -> int:
        rc = EXIT_OK
        try:
            with signal_guard():
                self.preflight()
                self.provision()
                self.install_and_setup()
                if not self.args.skip_rooms:
                    rooms.RoomsScenario(self).run()
                if self.cabins_ready:
                    cabins.CabinsScenario(self).run()
                    if self.args.tours:
                        tours.ToursScenario(self).run()
                    if self.args.omnigent:
                        omnigent_phase.OmnigentScenario(self).run()
        except Refusal:
            rc = EXIT_REFUSED
        except KeyboardInterrupt:
            self.report.add("run interrupted", FAIL, "KeyboardInterrupt or termination signal; tearing down")
            rc = EXIT_INTERRUPTED
        except Abort as exc:
            self.report.add("run aborted", FAIL, str(exc))
            rc = EXIT_FAIL
        except Exception as exc:  # noqa: BLE001
            self.report.add("harness error", FAIL, f"{type(exc).__name__}: {exc}")
            rc = EXIT_FAIL
        finally:
            leaked = self.teardown()
        if leaked:
            return EXIT_LEAK
        if rc == EXIT_OK and self.report.failed():
            rc = EXIT_FAIL
        return rc

    # ------------------------------------------------------------- 1 preflight
    def preflight(self) -> None:
        with self.report.check("preflight: the project holds no servers or volumes outside this run", self.clock) as c:
            self.initial = self.hc.inventory()
            counts = {k: len(v) for k, v in self.initial.items()}
            c.evidence(f"initial inventory: {counts}")
            foreign = self.hc.foreign(self.initial, self.run_id)
            if foreign:
                raise Refusal("refusing to run: the project already holds " + ", ".join(foreign[:6]) +
                              (" ..." if len(foreign) > 6 else "") +
                              f" (none labelled {cloud.LABEL}={self.run_id}). Use a dedicated, empty test project")
        self.report.meta["initial_inventory"] = {k: sorted(v) for k, v in self.initial.items()}
        self.state["initial_ids"] = self.report.meta["initial_inventory"]
        if self.args.cabins and not self.cabins_ready:
            why = "--cabins also needs " + " and ".join(self.missing_cabins_options) + \
                  " (a second, dedicated cloud project: cabin setup refuses the project that holds the test server)"
            print(f"run_live: cabins phase skipped: {why}", file=sys.stderr)
            self.report.add("cabins: phase skipped", "SKIP", why)
            if self.args.tours:
                self.report.add("tours: phase skipped", "SKIP", "tours run on cabins: " + why)
            if self.args.omnigent:
                self.report.add("omnigent: phase skipped", "SKIP", "the omnigent provider runs on a cabin: " + why)
        if self.cabins_ready:
            self.preflight_cabins()

    def preflight_cabins(self) -> None:
        """Same rule as the rooms project, for the cabins project: nothing in it may exist outside this run. Refuses before anything
        is created anywhere (exit 3)."""
        with self.report.check("preflight: the cabins project holds nothing outside this run", self.clock) as c:
            inv = self.hcc.inventory()
            counts = {k: len(v) for k, v in inv.items()}
            c.evidence(f"initial cabins-project inventory: {counts}")
            shared = [f"{kind} {ident}" for kind in cloud.KINDS for ident in inv.get(kind, {}) if ident in self.initial.get(kind, {})]
            if shared:
                raise Refusal("refusing to run: the cabins wrapper reaches the same project as --hcloud-cmd (both list " +
                              ", ".join(shared[:4]) + "). The cabins phase needs a second, dedicated project")
            foreign = self.hcc.foreign(inv, self.run_id, kinds=cloud.CABINS_PREFLIGHT_KINDS)
            if foreign:
                raise Refusal("refusing to run: the cabins project already holds " + ", ".join(foreign[:6]) +
                              (" ..." if len(foreign) > 6 else "") +
                              f" (none labelled {cloud.LABEL}={self.run_id}). Use a dedicated, empty cabins project")
            self.cabins_initial = inv
        self.report.meta["initial_inventory_cabins"] = {k: sorted(v) for k, v in self.cabins_initial.items()}
        self.state["cabins_initial_ids"] = self.report.meta["initial_inventory_cabins"]
        self.state["cabins_project"] = True

    # ------------------------------------------------------------- 2/3 provision
    def provision(self) -> None:
        a = self.args
        self.created_anything = True
        key, pub = self.path("id_ed25519"), self.path("id_ed25519.pub")
        with self.report.check("provision: per-run ssh key", self.clock) as c:
            res = self.r.run(["ssh-keygen", "-q", "-t", "ed25519", "-N", "", "-C", self.name, "-f", key])
            c.require(res.rc == 0, f"ssh-keygen failed: {res.err.strip()[:200]}")
            self.hc.create_ssh_key(self.name, pub, self.run_id)
            keys = self.hc.list("ssh-key", f"{cloud.LABEL}={self.run_id}")
            self.state["key_id"] = str(keys[0]["id"]) if keys else None
            self.save_state()
        self.gate()
        with self.report.check(f"provision: create throwaway server ({a.server_type}, {a.location}, {a.image})", self.clock) as c:
            self.t_created = self.clock.time()
            self.hc.create_server(self.name, a.server_type, a.image, a.location, self.name, self.run_id)
            doc = self.hc.describe("server", self.name)
            self.state.update(server_id=str(doc.get("id")), server_ip=cloud.server_ip(doc), server_ids=[str(doc.get("id"))],
                              server_ip_ids=cloud.server_ip_ids(doc), created_at=self.t_created)
            self.save_state()
            c.evidence(f"server {self.state['server_id']} at {self.state['server_ip']}")
            c.require(self.state["server_ip"], "the new server has no public IPv4")
        self.gate()
        self.remote = Remote(self.r, self.clock, ssh_cmd=a.ssh_argv, scp_cmd=a.scp_argv, host=self.state["server_ip"], key=key,
                             known_hosts=self.path("known_hosts"))
        with self.report.check("provision: ssh is reachable and cloud-init has finished", self.clock) as c:
            c.require(self.remote.wait_ssh(), "ssh never became reachable")
            self.remote.run(["cloud-init", "status", "--wait"], timeout=600)
        self.gate()
        with self.report.check("provision: pre-create the bystander volume attached to the server (before motel setup)", self.clock) as c:
            self.hc.create_bystander(f"{self.name}-bystander", a.bystander_gb, self.name, self.run_id)
            vols = self.hc.list("volume", f"{cloud.LABEL}={self.run_id},role=bystander")
            c.require(len(vols) == 1, f"expected one bystander volume, found {len(vols)}")
            self.state["bystander"] = cloud.snapshot_volume(vols[0]) if vols else {"id": "<bystander>"}
            c.require(self.dry or self.state["bystander"]["server"] == self.state["server_id"], "bystander is not attached to the server")
            c.evidence(f"bystander {self.state['bystander']}")
            self.save_state()
        self.gate()

    def gate(self) -> None:
        if self.report.last_failed():
            raise Abort(f"{self.report.checks[-1]['name']} failed")

    # ------------------------------------------------------------- 4 ship + install + setup
    def run_setup(self, *desk_args, token_file: str | None = None, remote_token: str = REMOTE_TOKEN):
        """Copy a token (a file path only) to the server as root 0600, run setup with it, delete it. Returns setup's CmdResult.

        ``desk_args`` defaults to ``setup hetzner`` with the rooms token. The cabins phase passes ``cabin setup hetzner`` with the
        cabins token file and its own remote path, so the two tokens never share a file.
        """
        desk_args = desk_args or ("setup", "hetzner")
        rem = self.remote
        rem.run(["sh", "-c", PREP_TOKEN_SCRIPT, "sh", remote_token])
        try:
            copied = rem.scp_to(token_file or self.args.token_file or "<token-file>", remote_token)
            if copied.rc != 0:
                return copied
            rem.run(["chmod", "0600", remote_token])
            return rem.desk(*desk_args, "--token-file", remote_token, "--json")
        finally:
            rem.run(["rm", "-f", remote_token])

    def install_and_setup(self) -> None:
        a, rem = self.args, self.remote
        with self.report.check("install: ship the git ref and run install.sh", self.clock) as c:
            sha = self.r.run(["git", "-C", a.repo, "rev-parse", "--verify", f"{a.ref}^{{commit}}"])
            c.require(sha.rc == 0 or self.dry, f"unknown ref {a.ref!r}: {sha.err.strip()[:200]}")
            sha_text = sha.out.strip() or "<sha>"
            self.report.meta["motel_ref"], self.report.meta["motel_commit"] = a.ref, sha_text
            tarball = self.path("motel-src.tar.gz")
            res = self.r.run(archive_argv(a.repo, sha_text, tarball))
            c.require(res.rc == 0, f"git archive failed: {res.err.strip()[:200]}")
            c.require(rem.scp_to(tarball, REMOTE_TARBALL).rc == 0, "could not copy the source to the server")
            for argv in unpack_argvs():
                res = rem.run(argv)
                c.require(res.rc == 0, f"could not unpack the source ({' '.join(argv[:2])}): {res.err.strip()[:200]}")
            for user in (USER, OUTSIDER):
                res = rem.run(["useradd", "-m", "-s", "/bin/bash", user])
                c.require(res.rc == 0, f"useradd {user}: {res.err.strip()[:200]}")
            res = rem.run(["sh", f"{REMOTE_SRC}/install.sh", "--users", USER, "--provider", "hetzner"], timeout=600)
            c.evidence(f"install.sh rc={res.rc}: {(res.out + res.err).strip()[-300:]}")
            c.require(res.rc == 0, f"install.sh failed (rc {res.rc})")
            res = rem.put_text("/etc/motel/motel.toml", rooms_config(min_floor_gb=a.min_floor_gb))
            c.require(res.rc == 0, f"could not write the test config: {res.err.strip()[:200]}")
        self.gate()
        with self.report.check("setup: motel-desk setup hetzner --token-file discovers the server and records the bystander as never-touch", self.clock) as c:
            res = self.run_setup()
            c.require(res.rc == 0, f"setup rc={res.rc}: {res.err.strip()[:300]} {res.out.strip()[:300]}")
            doc = parse_json(res)
            c.evidence(f"setup: server {doc.get('server_id')}, never_touch {doc.get('never_touch')}")
            c.require(self.dry or str(doc.get("server_id")) == self.state["server_id"], "setup bound to a different server id")
            receipt = parse_json(rem.read_file("/etc/motel/setup-receipt.json"))
            ids = {str(v.get("id")) for v in (receipt.get("never_touch") or [])} if not self.dry else set()
            c.require(self.dry or str(self.state["bystander"]["id"]) in ids, f"bystander missing from never_touch: {sorted(ids)}")
            c.require(rem.run(["test", "-e", REMOTE_TOKEN]).rc != 0, "the token file was left on the server")
        self.gate()
        with self.report.check("install: housekeeping timer is installed, then switched off so the harness controls reaping", self.clock) as c:
            res = rem.run(["systemctl", "is-enabled", "motel-housekeeping.timer"])
            c.require(res.out.strip() == "enabled", f"timer state {res.out.strip()!r}")
            rem.run(["systemctl", "disable", "--now", "motel-housekeeping.timer"])
            res = rem.run(["systemctl", "start", "motel-housekeeping.service"], timeout=300)
            c.require(res.rc == 0, f"motel-housekeeping.service failed: {res.err.strip()[:200]}")
        # the timer is not part of the gate: the rooms scenario can still run

    # ------------------------------------------------------------- 7 teardown
    def teardown(self) -> list:
        """Always runs. Returns the list of resources that could not be removed (empty on success)."""
        leaked: list = []
        if not self.created_anything:
            return leaked
        with signals_ignored():
            print("teardown: deleting everything labelled for this run ...", file=sys.stderr)
            outcome = {"deleted": [], "errors": [], "unattributed": []}
            try:
                outcome = self.hc.destroy_run(self.run_id, self.state)
            except HcloudError as exc:
                outcome["errors"].append(str(exc))
            self.t_destroyed = self.clock.time()
            leaked += outcome["errors"]
            self.report.add("teardown: harness-owned server, volumes and ssh key deleted",
                            "PLAN" if self.dry else FAIL if outcome["errors"] else "PASS",
                            "; ".join(outcome["errors"]) if outcome["errors"] else f"{len(outcome['deleted'])} resource(s) deleted",
                            outcome["deleted"] + [f"left alone (not provably this run's): volume {u}" for u in outcome["unattributed"]])
            if self.cabins_initial is not None:
                leaked += self.destroy_cabin_project()
            if not self.dry:
                leaked += self.final_inventory("teardown: final inventory equals the initial inventory", "rooms project",
                                               self.hc, self.initial)
                if self.cabins_initial is not None:
                    leaked += self.final_inventory("teardown: cabins project final inventory equals its initial inventory",
                                                   "cabins project", self.hcc, self.cabins_initial)
            self.spend()
            if self.run_dir and not self.dry:
                for name in ("id_ed25519",):
                    try:
                        os.unlink(os.path.join(self.run_dir, name))
                    except OSError:
                        pass
            self.report.meta["leaked"] = leaked
            self.report.save()
        return leaked

    def destroy_cabin_project(self) -> list:
        """The harness creates nothing in the cabins project, so anything of this run's found there is a cabin remnant: a motel
        leak. It is deleted (unless --no-sweep) and the check FAILs either way. Returns what could not be removed or was kept."""
        left: list = []
        outcome = {"deleted": [], "errors": [], "unattributed": []}
        name = "teardown: no cabin remnants of this run in the cabins project"
        try:
            if self.args.no_sweep and not self.dry:
                found, outcome["unattributed"] = self.hcc.owned_cabins(self.run_id, self.state.get("server_ids", []))
                left = [f"cabins project: {kind} {ident}" for kind, items in found.items() for ident in items]
            else:
                outcome = self.hcc.destroy_cabins(self.run_id, self.state.get("server_ids", []))
        except HcloudError as exc:
            outcome["errors"].append(str(exc))
        left += [f"cabins project: {e}" for e in outcome["errors"]]
        notes = outcome["deleted"] + [f"left alone (not provably this run's): {u}" for u in outcome["unattributed"]]
        if outcome["errors"] or left:
            self.report.add(name, FAIL, "; ".join(left), notes)
        elif outcome["deleted"]:
            self.report.add(name, FAIL, f"motel left {len(outcome['deleted'])} cabin resource(s) behind in the cabins project (now deleted)", notes)
        else:
            self.report.add(name, "PLAN" if self.dry else "PASS", "the cabins project holds none of this run's resources", notes)
        return left

    def final_inventory(self, name: str, which: str, hc: Hcloud, initial: dict) -> list:
        """The project's inventory must equal its initial one. A leak fails the run even when it could be swept; sweeping only stops it billing.

        Returns what is still there after the sweep (empty when clean or swept)."""
        try:
            final = hc.inventory()
            diff = hc.diff(initial, final)
            leaked_now = list(diff["added"])
            swept = []
            if diff["added"] and not self.args.no_sweep:
                swept = hc.delete_leaks(initial, self.run_id)
                diff_after = hc.diff(initial, hc.inventory())
            else:
                diff_after = diff
            if leaked_now or diff["removed"]:
                detail = f"{which}: added {diff['added']} removed {diff['removed']}" + \
                    (f"; swept, {len(diff_after['added'])} still present" if swept else "")
            else:
                detail = "inventory equals the initial inventory"
            self.report.add(name, FAIL if leaked_now or diff["removed"] else "PASS", detail, [f"swept as leaks: {s}" for s in swept])
            return list(diff_after["added"])
        except HcloudError as exc:
            self.report.add(name, FAIL, f"{which}: {exc}")
            return [f"{which}: {exc}"]

    def spend(self) -> None:
        if not self.t_created:
            return
        seconds = max(0.0, (self.t_destroyed or self.clock.time()) - self.t_created)
        hours = cloud.billed_hours(seconds)
        price = None if self.dry else self.hc.hourly_price(self.args.server_type, self.args.location)
        est = {"server_seconds": int(seconds), "billed_hours": hours, "hourly_eur_gross": price,
               "estimate_eur": None if price is None else round(hours * price, 4),
               "note": "server only; excludes volumes (billed per hour, small) and any cabin servers"}
        self.report.meta["spend_estimate"] = est
        self.report.add("spend estimate", "PLAN" if self.dry else "PASS", f"{hours} billed hour(s)" + (f" x EUR {price} = EUR {est['estimate_eur']}" if price else " (price unknown)"))
