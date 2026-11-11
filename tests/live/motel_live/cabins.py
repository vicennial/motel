"""Phase 2: cabins (disposable servers). Skippable (`--cabins` to enable, `--cabins-skip` to drop parts).

Written against the cabin CLI in the m3 branch: `motel cabin run|checkin|ls|status|extend|checkout` (checkin --json prints the cabin
view with `lease_id`, `server_id`, `firewall_id`, `volume_id`), `motel-desk cabin setup|doctor|reap`, and the [cabins] table.
Everything is parsed tolerantly; a missing piece skips the phase instead of failing it.

Two cloud projects are in play. The rooms project holds the test server (the motel "controller host"); cabin setup refuses that
project on purpose. Cabins live in a second, dedicated project reached through ``ctx.hcc`` with its own token, and every cabin
command here runs on the test server over ssh (the cabin firewall admits only that server's public address). The one thing that
runs on the operator's machine is the negative probe: the operator's address is not allowlisted, so port 22 must be closed to it.
"""

from __future__ import annotations

import re
import socket

from motel_live.core import REAP_INTERVAL_S, RELEASE_MARGIN_S, parse_json, release_bounds, wait_until
from motel_live.remote import CD_SCRIPT, CLI, GIT_PREP_SCRIPT, REACH_SCRIPT, REMOTE_CABINS_TOKEN, REMOTE_SRC
from motel_live.rooms import USER, RoomsScenario, rooms_config

CABIN_CONFIG = """
[cabins]
enabled = true
token_file = "/etc/motel/credentials/cabins.token"
access = "ssh-allowlist"
types = ["{cabin_type}"]
default_ttl = "2h"
idle_timeout = "{idle}"
max_concurrent = 3
max_eur_per_hour = 1.0
"""
ROOMS_TOKEN = "/etc/motel/credentials/hetzner.token"
CABINS_TOKEN = "/etc/motel/credentials/cabins.token"
WORKDIR = "/tmp/motel-live-work"
POLL_S = 60   # the harness runs the reaper every minute while it waits (the real timer runs every 10 minutes)
HOST_ROUTE = re.compile(r"^(?:\d{1,3}(?:\.\d{1,3}){3}/32|[0-9a-fA-F:]+/128)$")


def parse_minutes(text: str) -> int:
    """'30m' or '2h' -> minutes (the harness's own --cabin-idle values)."""
    match = re.fullmatch(r"(\d+)([mh])", (text or "").strip())
    if not match:
        raise AssertionError(f"--cabin-idle {text!r}: use a number of minutes or hours, e.g. 30m")
    return int(match.group(1)) * (60 if match.group(2) == "h" else 1)


def idle_release_bounds(idle_s: float, margin_s: int = RELEASE_MARGIN_S, interval_s: int = REAP_INTERVAL_S) -> tuple:
    """(earliest, latest) seconds after the server's creation at which an idle cabin may be deleted: it is sealed once idle
    for ``idle_s`` and then held for its release window (see ``core.release_bounds``)."""
    return release_bounds(idle_s, margin_s, interval_s)


def tcp_open(host: str, port: int, timeout: float = 10) -> bool:
    """A TCP connect from THIS machine (the operator's), used only for the negative probe. Tests replace it."""
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


def cabin_id(res, doc) -> str | None:
    if isinstance(doc, dict):
        cab = doc.get("cabin") if isinstance(doc.get("cabin"), dict) else doc
        for key in ("lease_id", "id", "name"):
            if cab.get(key):
                return str(cab[key])
    m = re.search(r"\b([a-z0-9]+(?:-[a-z0-9]+)+)\b", res.out.strip().splitlines()[0]) if res.out.strip() else None
    return m.group(1) if m else None


class CabinsScenario:
    def __init__(self, ctx):
        self.ctx, self.rep, self.rem, self.clock, self.dry = ctx, ctx.report, ctx.remote, ctx.clock, ctx.dry
        self.hc, self.hcc = ctx.hc, ctx.hcc  # rooms project (test server), cabins project (cabins only)
        self.args = ctx.args
        self.ip = ""
        self.skip = set(filter(None, (self.args.cabins_skip or "").split(",")))
        self.rooms = RoomsScenario(ctx)

    # ------------------------------------------------------------ helpers
    def config_text(self) -> str:
        """The whole /etc/motel/motel.toml of the run: the rooms tables and the [cabins] table."""
        if self.args.cabins_config:
            with open(self.args.cabins_config, encoding="utf-8") as handle:
                frag = handle.read()
        else:
            frag = CABIN_CONFIG
        frag = frag.replace("{cabin_type}", self.args.cabin_type).replace("{idle}", self.args.cabin_idle)
        if re.search(r'^\s*token_file\s*=\s*"[^"]*hetzner\.token"', frag, re.M):
            raise AssertionError("the [cabins] fragment points token_file at the rooms token; cabin setup would overwrite it")
        return rooms_config() + frag

    def configure(self) -> None:
        self.rooms.set_config(self.config_text())

    def cabin(self, *args, timeout=1800):
        """`motel cabin ...` as the user, from inside a git worktree (cabin run syncs the current checkout)."""
        res = self.rem.run(["sh", "-c", CD_SCRIPT, "sh", WORKDIR, CLI, "cabin", *args], as_user=USER, timeout=timeout)
        return res, parse_json(res)

    def new(self, kind: str, inv: dict | None = None) -> dict:
        """Resources of ``kind`` in the CABINS project that were not there at the start (the harness creates nothing in it)."""
        now = (inv if inv is not None else self.hcc.inventory((kind,))).get(kind, {})
        init = (self.ctx.cabins_initial or {}).get(kind, {})
        return {i: v for i, v in now.items() if i not in init}

    def rooms_new(self, kind: str, inv: dict | None = None) -> dict:
        """Resources of ``kind`` in the ROOMS project that are neither initial nor the harness's own nor a rooms floor.

        Cabins belong in the other project, so anything here is a cabin (or its firewall, address, disk) in the wrong place.
        The harness's own test server has primary IPs that Hetzner created with it. They are unlabelled and named by Hetzner, so
        they are recognised by the IDs recorded at creation and by being assigned to the harness server."""
        now = (inv if inv is not None else self.hc.inventory((kind,))).get(kind, {})
        init = self.ctx.initial.get(kind, {})
        server = str(self.ctx.state.get("server_id"))
        own = {server, str(self.ctx.state.get("key_id")), str(self.ctx.state["bystander"]["id"])}
        own |= {str(i) for i in self.ctx.state.get("server_ip_ids") or []}
        return {i: v for i, v in now.items() if i not in init and i not in own and (v.get("labels") or {}).get("role") != "floor"
                and not (kind == "primary-ip" and v.get("assignee") == server)}

    def live_cabins(self) -> list:
        res, doc = self.cabin("ls", "--json")
        rows = doc.get("cabins") if isinstance(doc, dict) else None
        if not isinstance(rows, list):
            return []
        # `cabin ls` also lists released cabins (history); only the ones motel still counts as live or pending matter.
        pending = doc.get("active_or_pending")
        if isinstance(pending, list):
            return [r for r in rows if r.get("lease_id") in set(pending)]
        return [r for r in rows if r.get("state") not in ("released",)]

    def checkin(self, *extra) -> tuple:
        res, doc = self.cabin("checkin", "--type", self.args.cabin_type, *extra, "--json")
        return res, doc, cabin_id(res, doc)

    def reap(self) -> None:
        self.rooms.reap_apply()
        self.rem.desk("cabin", "reap", "--apply", "--json")

    KINDS = ("server", "firewall", "primary-ip", "volume")
    CLEAN_TIMEOUT = 300  # seconds to wait for cabin resources to disappear (tests shorten it for the failure cases)

    def residue(self) -> list:
        """Cabin-looking resources that exist in either project. Both projects must be back to their initial state."""
        cabins_inv, rooms_inv = self.hcc.inventory(self.KINDS), self.hc.inventory(self.KINDS)  # one listing per kind and project
        out = [f"cabins project: {k} {i} ({v['name']})" for k in self.KINDS for i, v in self.new(k, cabins_inv).items()]
        out += [f"ROOMS project (wrong place): {k} {i} ({v['name']})" for k in self.KINDS for i, v in self.rooms_new(k, rooms_inv).items()]
        return out

    def nothing_left(self) -> bool:
        return not self.residue()

    def wait_clean(self, timeout=None) -> None:
        try:
            wait_until(self.clock, self.nothing_left, timeout=timeout or self.CLEAN_TIMEOUT, interval=15, what="cabin resources to disappear", dry=self.dry)
        except AssertionError as exc:
            raise AssertionError(f"{exc}; still there: {self.residue()[:6]}") from None

    # ------------------------------------------------------------ run
    def run(self) -> None:
        with self.rep.check("cabins: config accepted, cabin setup with the cabins token file in the cabins project, doctor ok", self.clock) as c:
            self.rem.run(["sh", "-c", "DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git rsync"], timeout=600)
            res = self.rem.run(["sh", "-c", GIT_PREP_SCRIPT, "sh", WORKDIR], as_user=USER)
            c.require(res.rc == 0, f"could not create a git worktree for `cabin run`: {res.err.strip()[:200]}")
            seen = self.hcc.list("server", f"motel-live={self.ctx.run_id}")
            c.require(not seen, "the cabins wrapper can see this run's test server: it reaches the rooms project, not a second one")
            self.configure()
            # Documented order (docs/cabins.md): enable [cabins] in the config, then run the installer again. It adds the
            # motel-cabin user, its keys tree, the cabin sudoers verbs and the cabin reaper.
            res = self.rem.run(["sh", f"{REMOTE_SRC}/install.sh", "--users", USER, "--provider", "hetzner", "--cabins", "--upgrade"],
                               timeout=600)
            c.require(res.rc == 0, f"install.sh --cabins --upgrade rc={res.rc}: {(res.err.strip() or res.out.strip())[-300:]}")
            # The harness drives every reap itself, so both housekeeping timers stay off.
            self.rem.run(["systemctl", "disable", "--now", "motel-housekeeping.timer", "motel-cabins-housekeeping.timer"])
            res = self.ctx.run_setup("cabin", "setup", "hetzner", token_file=self.args.cabins_token_file, remote_token=REMOTE_CABINS_TOKEN)
            c.evidence(f"cabin setup rc={res.rc}: {(res.out + res.err).strip()[:200]}")
            if res.rc == 2 and not self.dry:
                c.skip("cabin setup does not take --token-file in this ref (it exits 2); cabins need an interactive setup")
            c.require(res.rc == 0, f"cabin setup rc={res.rc}: {(res.err.strip() or res.out.strip())[:300]}")
            c.require(self.rem.run(["test", "-e", REMOTE_CABINS_TOKEN]).rc != 0, "the cabins token file was left on the server")
            res = self.rem.run(["stat", "-c", "%U:%G %a", CABINS_TOKEN])
            c.require(res.out.strip() == "root:root 600", f"cabins token file ownership/mode: {res.out.strip()!r}")
            res = self.rem.run(["cmp", "-s", ROOMS_TOKEN, CABINS_TOKEN])
            c.require(res.rc == 1, "the rooms token and the cabins token are the same file or content (cabin setup overwrote the rooms token?)")
            res = self.rem.desk("cabin", "doctor", "--json")
            doc = parse_json(res)
            failed = "; ".join(f"{k.get('check')}: {k.get('detail', '')}"[:300] for k in (doc.get("checks") or [])
                               if not k.get("ok"))
            c.require(res.rc == 0 and doc.get("ok") is not False,
                      f"cabin doctor rc={res.rc}: failed checks: {failed or (res.err.strip() or res.out.strip())[:300]}")
            res, doc = self.rooms.motel("doctor")
            c.require(res.rc == 0 and doc.get("ok") is True, "motel doctor with [cabins]: non-ok checks: " + "; ".join(
                f"{k.get('level')} {k.get('check')}: {k.get('detail', '')}"[:300] for k in (doc.get("checks") or [])
                if k.get("level") != "ok"))
        if self.rep.last_failed() or self.rep.checks[-1]["status"] == "SKIP":
            return
        try:
            if "run" not in self.skip:
                self.cabin_run()
            if {"firewall", "disk"} - self.skip:
                self.shared_cabin()
            if {"idle", "ttl"} - self.skip:
                self.idle_and_ttl()
        finally:
            self.drain()

    def cabin_run(self) -> None:
        with self.rep.check("cabins: `cabin run -- uname -a` passes the exit code and output through", self.clock) as c:
            res, _ = self.cabin("run", "--type", self.args.cabin_type, "--", "uname", "-a")
            c.require(res.rc == 0, f"cabin run rc={res.rc}: {res.err.strip()[:300]}")
            c.require("Linux" in res.out, f"no uname output: {res.out[:200]}")
        with self.rep.check("cabins: `cabin run -- sh -c 'exit 7'` exits 7", self.clock) as c:
            res, _ = self.cabin("run", "--type", self.args.cabin_type, "--", "sh", "-c", "exit 7")
            c.require(res.rc == 7, f"exit code {res.rc}, expected 7: {res.err.strip()[:200]}")
        with self.rep.check("cabins: an ephemeral `cabin run` leaves no server, firewall, address or volume behind", self.clock) as c:
            self.wait_clean()

    def take_shared(self, c, holder: dict) -> dict:
        """The one cabin the firewall, SSH and disk checks share (one server, one firewall, one address, one disk). The disk
        is only added when the disk check runs; it changes nothing about the firewall or the reachability of SSH."""
        extra = ["--disk", "10G"] if "disk" not in self.skip else []
        res, doc, cid = self.checkin("--for", "20m", *extra)
        c.require(res.rc == 0 and cid, f"cabin checkin rc={res.rc}: {res.out[:300]} {res.err[:200]}")
        holder["id"] = cid
        holder["view"] = doc if isinstance(doc, dict) else {}
        return holder["view"]

    def shared_cabin(self) -> None:
        holder = {}
        if "firewall" not in self.skip:
            self.firewall_check(holder)
            if "id" in holder:
                self.ssh_checks(holder)
        if "disk" not in self.skip and ("firewall" in self.skip or "id" in holder):
            with self.rep.check("cabins: --disk attaches a volume to the cabin (the checkout below removes it)", self.clock) as c:
                view = holder["view"] if "id" in holder else self.take_shared(c, holder)
                vols = self.new("volume")
                c.evidence(f"new volumes {[(i, v.get('size'), v.get('server')) for i, v in vols.items()]}")
                c.require(len(vols) == 1, f"expected one new volume, found {len(vols)}")
                vol = next(iter(vols.values()))
                sid = str(view.get("server_id") or "") or next(iter(self.new("server")), "")
                c.require(sid and str(vol.get("server")) == sid, "the disk is not attached to the cabin server")
        if "id" not in holder:
            return
        what = "firewall, its address and its disk" if "disk" not in self.skip else "firewall and its address"
        with self.rep.check(f"cabins: checkout deletes the cabin, its {what}", self.clock) as c:
            res, _ = self.cabin("checkout", holder["id"])
            c.require(res.rc in (0, 9), f"cabin checkout rc={res.rc}: {res.err.strip()[:300]}")
            self.wait_clean()

    def firewall_check(self, holder: dict) -> None:
        with self.rep.check("cabins: the cabin firewall admits only the test server's own address (/32) to SSH", self.clock) as c:
            view = self.take_shared(c, holder)
            sid = str(view.get("server_id") or "") or next(iter(self.new("server")), "")
            fw_id = str(view.get("firewall_id") or "") or next(iter(self.new("firewall")), "")
            c.require(sid and fw_id, "the cabin view names no server or firewall, and none appeared")
            full = self.hcc.describe("server", sid)  # the cabins project, not the rooms project
            self.ip = str(((full.get("public_net") or {}).get("ipv4") or {}).get("ip", ""))
            fw = self.hcc.describe("firewall", fw_id)
            allowed = f"{self.ctx.state['server_ip']}/32"
            sources, ports = [], set()
            for rule in fw.get("rules") or []:
                if rule.get("direction") == "in":
                    sources += list(rule.get("source_ips") or [])
                    ports.add(str(rule.get("port")))
            c.evidence(f"inbound sources {sorted(set(sources))} ports {sorted(ports)}; test server is {allowed}")
            c.require(sources, "the firewall has no inbound rule at all (the cabin would be unreachable)")
            c.require(all(HOST_ROUTE.fullmatch(s) for s in sources), f"a source is wider than a single host: {sorted(set(sources))}")
            v4 = {s for s in sources if s.endswith("/32")}
            c.require(v4 == {allowed}, f"IPv4 sources are {sorted(v4)}, expected exactly {allowed}")
            c.require("22" in ports, f"no port 22 rule (ports {sorted(ports)})")
            if isinstance(view.get("access"), dict) and view["access"].get("sources"):
                c.require(set(view["access"]["sources"]) == set(sources), "the cabin view and the provider firewall disagree")
            applied = fw.get("applied_to") or []
            c.require(any(str((a.get("server") or {}).get("id")) == sid for a in applied), "the firewall is not applied to the cabin server")

    def ssh_checks(self, holder: dict) -> None:
        with self.rep.check("cabins: SSH on the cabin's public address is reachable from the test server (the allowlisted address)", self.clock) as c:
            c.require(self.ip or self.dry, "cabin has no public IPv4")

            def reachable():
                return self.rem.run(["sh", "-c", REACH_SCRIPT, "sh", self.ip or "<cabin-ip>"], timeout=30).rc == 0
            wait_until(self.clock, reachable, timeout=240, interval=10, what="port 22 of the cabin to answer the test server", dry=self.dry)
            c.evidence(f"TCP connect to {self.ip}:22 over ssh from the test server: open")
        with self.rep.check("cabins: SSH on the cabin's public address is closed to the operator's machine (not allowlisted)", self.clock) as c:
            if self.args.no_probe:
                c.skip("--no-probe given")
            if not self.ip and not self.dry:
                c.skip("cabin has no public IPv4")
            ok = False if self.dry else tcp_open(self.ip, 22)
            c.evidence(f"connect from this machine (not the test server) to {self.ip}:22: {'OPEN' if ok else 'closed or filtered'}")
            c.require(not ok, "port 22 on the cabin answered from the operator's machine, which is not allowlisted")

    def idle_and_ttl(self) -> None:
        """Two cabins at once: one with a short TTL (hard delete) and one left idle (idle teardown). The idle timeout is
        at least 30m by design, and the idle cabin is then held until its release window (see ``idle_release_bounds``), so
        this is the slow step; running both together keeps it to one wait. The TTL cabin is deleted at its TTL plus grace,
        never held for the paid hour: a TTL is a promise."""
        launched, t0, started = {}, [0.0], {}
        with self.rep.check("cabins: launch one cabin with a 5m TTL and one idle cabin", self.clock) as c:
            if "ttl" not in self.skip:
                started["ttl"] = self.clock.time()
                res, doc, cid = self.checkin("--for", "5m", "--task", "ttl-test")
                c.require(res.rc == 0 and cid, f"ttl cabin checkin rc={res.rc}: {res.out[:300]} {res.err[:200]}")
                launched["ttl"] = cid
            if "idle" not in self.skip:
                started["idle"] = self.clock.time()
                res, doc, cid = self.checkin("--for", "2h", "--task", "idle-test")
                c.require(res.rc == 0 and cid, f"idle cabin checkin rc={res.rc}: {res.out[:300]} {res.err[:200]}")
                launched["idle"] = cid
            t0[0] = self.clock.time()
        if self.rep.last_failed():
            return
        gone = {}
        idle_s = parse_minutes(self.args.cabin_idle) * 60
        earliest, latest = idle_release_bounds(idle_s)

        def poll():
            self.reap()
            alive = {r.get("lease_id") for r in self.live_cabins()}
            for kind, cid in launched.items():
                if cid not in alive and kind not in gone:
                    gone[kind] = int(self.clock.time() - t0[0])
            return len(gone) == len(launched)
        try:
            wait_until(self.clock, poll, timeout=int(latest) + 30 * 60, interval=POLL_S,
                       what="the TTL and idle cabins to be deleted", dry=self.dry)
        except AssertionError:
            pass
        if "ttl" in launched:
            with self.rep.check("cabins: a cabin past its 5m TTL is hard-deleted by reap", self.clock) as c:
                c.evidence(f"deleted after {gone.get('ttl')}s")
                c.require("ttl" in gone, "the expired cabin was not deleted in time")
                # the TTL (plus the 10 minute grace) is not stretched to the end of the paid hour
                c.require(gone.get("ttl", 0) < 30 * 60, f"the TTL cabin lived {gone.get('ttl')}s: a TTL must not be held for billing")
        if "idle" in launched:
            name = (f"cabins: an idle cabin is sealed after idle_timeout ({self.args.cabin_idle}) and deleted in its release window "
                    "before the next billed hour")
            with self.rep.check(name, self.clock) as c:
                # `gone` counts from t0, after both checkins; the cabin was created somewhere in [started, t0].
                lag = t0[0] - started["idle"]
                lo, hi = gone.get("idle", 0) - POLL_S, gone.get("idle", 0) + lag
                c.evidence(f"deleted {gone.get('idle')}s after the checkins ({int(lag)}s of provisioning); expected {int(earliest)}s to "
                           f"{int(latest)}s after the server was created")
                c.require("idle" in gone, "the idle cabin was not deleted in time")
                c.require(hi >= earliest, f"the idle cabin was deleted at most {int(hi)}s after creation, before its release window "
                                          f"({int(earliest)}s): an early delete inside a paid hour saves nothing")
                c.require(lo <= latest, f"the idle cabin was deleted at least {int(lo)}s after creation, past the end of the paid hour "
                                        f"({int(latest)}s)")
                c.require("ttl" not in launched or gone.get("idle", 0) > gone.get("ttl", 0), "idle teardown fired before the TTL cabin went")
            self.wait_clean()

    def drain(self) -> None:
        """Best effort: check out whatever cabin is left, then verify nothing remains."""
        if self.dry:
            return
        for row in self.live_cabins():
            ident = row.get("lease_id")
            if ident:
                self.cabin("checkout", str(ident))
        self.reap()
        with self.rep.check("cabins: after the phase no cabin server, firewall, address or volume remains", self.clock) as c:
            self.wait_clean()
            c.evidence("the cabins project holds nothing new, and the rooms project gained no cabin resource")
