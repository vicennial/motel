"""Phase 4: the Omnigent sandbox provider on a real cabin. Enabled with ``--omnigent``, which implies ``--cabins``.

Everything the fakes could not prove about docs/omnigent.md, on the throwaway test server and one real cabin: Omnigent and motel install
into a venv as the unprivileged cabin user, a disposable ``omnigent server`` (a free loopback port, a throwaway HOME, a throwaway
OMNIGENT_HARNESS_TMP_PARENT, no key of any kind) is configured with ``sandbox.provider: motel``, and a *managed* session makes the
server itself provision a cabin through the provider, start ``omnigent host`` on it and wait for it to register over the provider's
relay. No model is called and no real key exists.

How the server is made to provision (Omnigent 0.21.0; ``omnigent sandbox create`` needs an Omnigent source checkout, so it is not used):

  POST /v1/sessions {"agent_id": <a built-in agent>, "host_type": "managed", "sandbox_provider": "motel"}

The create returns at once; the server provisions the sandbox in the background (``MotelSandboxLauncher.provision``), starts the host
through ``run_background``, and the host registers over the relay (``GET /v1/hosts`` lists it, ``sandbox_provider: motel``).
``DELETE /v1/sessions/{id}`` is how the server ends a managed sandbox: it calls the launcher's ``terminate``.

One cabin in all (Hetzner bills each started hour, so that is the cost floor). Like the other phases every motel command runs on the
test server over ssh, as the cabin user, and the harness only reads the cabins project.
"""

from __future__ import annotations

import json
import os
import re
import secrets
from datetime import UTC, datetime

from motel_live.cabins import CabinsScenario
from motel_live.core import DryDoc, parse_json, wait_until
from motel_live.remote import (
    CLI,
    OMNI_API_PY,
    OMNI_CABIN_LOGS_SCRIPT,
    OMNI_COUNT_SCRIPT,
    OMNI_ENV_NAMES_SCRIPT,
    OMNI_HELP_SCRIPT,
    OMNI_INSTALL_SCRIPT,
    OMNI_LOGS_SCRIPT,
    OMNI_PORT_PY,
    OMNI_PROVIDER_PY,
    OMNI_PUT_SCRIPT,
    OMNI_START_SCRIPT,
    OMNI_STOP_SCRIPT,
    REMOTE_SRC,
)
from motel_live.rooms import USER, parse_epoch
from motel_live.tours import CabinMeter

DIAG_DIR = "omnigent-diagnostics"          # under the run's out dir: one numbered folder per failed check, full and unclipped
ROOT = "/srv/omnigent-live"                # everything of the phase lives here on the test server (a throwaway tree, owned by the cabin user)
MOTEL_SRC_COPY = f"{ROOT}/motel-src"       # the shipped ref, readable by the cabin user (the unpacked ref is root-only)
SERVER_TTL = "20m"                         # the provider's ttl: the cabin's lease at checkin
KEEP_ALIVE_TTL = "1h"                      # what the keep-alive probe asks for: well beyond the lease, so the extension is visible
MIN_EXTENSION_S = 600                      # keep_alive must have moved the checkout time forward by at least this much
PREFERRED_AGENTS = ("polly",)              # tried first when --omnigent-agent is not given; else the first agent the server lists
SERVER_PROVIDER = "motel"


VERSION_RE = re.compile(r"^[0-9]+(\.[0-9]+)*([a-z]+[0-9]*|\.?(dev|post|rc)[0-9]+)*$")   # the provider's own rule for omnigent_version


def valid_version(text: str) -> bool:
    """A released Omnigent version, safe to put in ``pip install omnigent==VERSION`` and in the server config (no dev build)."""
    return bool(VERSION_RE.fullmatch(text or "")) and "dev" not in text


def config_yaml(url: str, motel_binary: str, version: str, cabin_type: str) -> str:
    """The Omnigent server config: the motel provider with a small ttl, no npm installs, the loopback URL carried by the relay."""
    return (f"sandbox:\n  provider: motel\n  server_url: {url}\n  motel:\n    motel_binary: {motel_binary}\n"
            f"    server_type: {cabin_type}\n    ttl: {SERVER_TTL}\n    tunnel: auto\n    omnigent_version: \"{version}\"\n"
            f"    harness_packages: []\n    max_streams: 16\n    task: omnigent-live\n")


def cabin_object(doc) -> dict:
    """The cabin of a ``cabin status --json`` document (wrapped as {"cabin": {...}} or bare)."""
    if not isinstance(doc, dict):
        return {}
    inner = doc.get("cabin")
    return inner if isinstance(inner, dict) else doc


class OmnigentScenario:
    REGISTER_TIMEOUT = 1200        # the cabin installs omnigent with apt and pip before its host starts: minutes
    PROVISION_TIMEOUT = 600        # a cabin that accepts commands
    SHELL_TIMEOUT = 240            # the session's runner launches after the host registers
    RELEASE_TIMEOUT = 300
    HEALTH_TIMEOUT = 180
    POLL_S = 5
    OBSERVE_EVERY_S = 30           # how often the cabin meter asks the provider (one API call each time)
    MAX_DIAGNOSES = 6

    def __init__(self, ctx):
        self.ctx, self.rep, self.rem, self.clock, self.dry = ctx, ctx.report, ctx.remote, ctx.clock, ctx.dry
        self.hcc, self.args = ctx.hcc, ctx.args
        self.skip = set(filter(None, (self.args.omnigent_skip or "").split(",")))
        self.cab = CabinsScenario(ctx)
        self.meter = CabinMeter(self)
        self.version = self.args.omnigent_version
        self.root = ROOT
        self.home = f"{ROOT}/home"
        self.motel = f"{ROOT}/venv/bin/motel"
        self.port: int | None = None
        self.url = ""
        self.server_started = False
        self.stopped = False
        self.session_id: str | None = None
        self.session_deleted = False
        self.cabin_id: str | None = None
        self.host_id: str | None = None
        self.token = "omnigent-live-" + secrets.token_hex(4)
        self.timeline: list = []
        self._stage: str | None = None
        self._shell_last = ""
        self.t0 = None
        self._last_observe = -1e9
        self.diag_n = 0
        self.diag_dirs: list = []
        self.rep.fail_hooks.append(self.diagnose_failure)

    # ------------------------------------------------------------ helpers
    @staticmethod
    def doc(value) -> dict:
        return value if isinstance(value, dict) else {}

    def user_run(self, argv: list, *, input: str | None = None, timeout: float = 300):
        return self.rem.run(argv, as_user=USER, input=input, timeout=timeout)

    def api(self, method: str, path: str, body=None, timeout: float = 120) -> tuple:
        """(status, document or text). Status 0: nothing answered. In a dry run the command is only printed."""
        argv = ["python3", "-c", OMNI_API_PY, method, f"{self.url or 'http://127.0.0.1:<port>'}{path}"]
        res = self.user_run(argv, input=json.dumps(body) if body is not None else None, timeout=timeout)
        if res.dry:
            return 200, DryDoc("json")
        head, _, text = res.out.partition("\n")
        try:
            status = int(head.strip())
        except ValueError:
            status, text = 0, (res.err or res.out)
        try:
            return status, json.loads(text)
        except ValueError:
            return status, text.strip()

    def provider(self, *args, timeout: float = 900) -> tuple:
        """The provider's own code in a second process of the venv, with the throwaway HOME (the cabin's ssh state lives there).
        ``-P``: the working directory (inherited from the ssh session, closed to this user) must not be on sys.path, or
        Omnigent's community package loader fails to stat it."""
        argv = ["env", "-i", f"HOME={self.home}", f"PATH={self.root}/venv/bin:/usr/local/bin:/usr/bin:/bin", "LANG=C.UTF-8",
                f"{self.root}/venv/bin/python3", "-P", "-c", OMNI_PROVIDER_PY, args[0], self.motel, KEEP_ALIVE_TTL if args[0] == "keep_alive" else SERVER_TTL,
                *args[1:]]
        res = self.user_run(argv, timeout=timeout)
        if res.dry:
            return res, DryDoc("json")
        return res, parse_json(res)

    def cabin_view(self, cabin_id: str) -> tuple:
        """(exit code, cabin object) of ``motel cabin status``."""
        res, doc = self.cab.cabin("status", cabin_id, "--json", timeout=120)
        return res.rc, cabin_object(doc)

    def observe(self) -> None:
        if self.dry:
            return
        now = self.clock.time()
        if now - self._last_observe >= self.OBSERVE_EVERY_S:
            self._last_observe = now
            self.meter.poll()

    def mark(self, text: str) -> None:
        """One line of the run's timeline (what the session and the host reported, with seconds since the phase began)."""
        if not self.dry:
            self.timeline.append(f"t+{int(self.clock.time() - (self.t0 or self.clock.time()))}s {text}")

    def sandbox_stage(self) -> str:
        """The managed launch stage the session snapshot reports, with the launch error if it failed; "" when none is reported."""
        if not self.session_id:
            return ""
        status, doc = self.api("GET", f"/v1/sessions/{self.session_id}")
        sb = self.doc(self.doc(doc).get("sandbox_status")) if status == 200 else {}
        stage = str(sb.get("stage") or ("ready" if status == 200 and not sb else f"http {status}"))
        if stage != self._stage:
            self._stage = stage
            self.mark(f"session sandbox stage: {stage}" + (f" ({sb.get('error')})" if sb.get("error") else ""))
        if stage == "failed":
            raise AssertionError(f"the managed launch failed: {sb.get('error') or 'no reason given'}")
        return stage

    def hosts(self) -> list:
        status, doc = self.api("GET", "/v1/hosts")
        rows = self.doc(doc).get("hosts") if status == 200 else None
        return [h for h in rows or [] if isinstance(h, dict)]

    def motel_hosts(self) -> list:
        return [h for h in self.hosts() if h.get("sandbox_provider") == SERVER_PROVIDER]

    def logs_text(self) -> str:
        res = self.user_run(["sh", "-c", OMNI_LOGS_SCRIPT, "sh", self.root], timeout=120)
        return res.out

    # ------------------------------------------------------------ run
    def run(self) -> None:
        cabins_ok = self.dry or any(c["name"].startswith("cabins: config accepted") and c["status"] == "PASS" for c in self.rep.checks)
        if not cabins_ok:
            self.rep.add("omnigent: phase skipped", "SKIP", "the cabins setup did not pass, and the provider runs on a cabin")
            return
        self.t0 = self.clock.time()
        self.ctx.state["omnigent"] = {"dir": self.root}
        self.ctx.save_state()
        try:
            if not self.install() or not self.start_server() or not self.provision() or not self.register():
                return
            # What follows does not depend on the one before: a failed shell check must not hide keep_alive or terminate.
            self.command_checks()
            if "keepalive" not in self.skip:
                self.keep_alive()
            self.terminate()
        finally:
            self.drain()

    # ------------------------------------------------------------ 1. install
    def install(self) -> bool:
        with self.rep.check(f"omnigent: omnigent=={self.version} and motel (the shipped ref) install into a venv as {USER}; "
                            "`omnigent sandbox create --help` lists motel", self.clock) as c:
            res = self.rem.run(["sh", "-c", "export DEBIAN_FRONTEND=noninteractive; apt-get update -qq && apt-get install -y -qq -o DPkg::Lock::Timeout=300 python3-venv python3-pip"], timeout=900)
            c.require(res.rc == 0, f"apt-get install python3-venv rc={res.rc}: {res.err.strip()[-300:]}")
            # The unpacked ref is root-only (the installer runs it as root); the cabin user builds its own copy of the same tree.
            for argv in (["rm", "-rf", self.root], ["mkdir", "-p", self.root], ["cp", "-a", REMOTE_SRC, MOTEL_SRC_COPY],
                         ["chown", "-R", f"{USER}:{USER}", self.root]):
                res = self.rem.run(argv)
                c.require(res.rc == 0, f"{argv[0]} {argv[1]}: {res.err.strip()[:200]}")
            res = self.user_run(["sh", "-c", OMNI_INSTALL_SCRIPT, "sh", self.root, self.version, MOTEL_SRC_COPY], timeout=1800)
            c.require(res.rc == 0, f"pip install omnigent=={self.version} and motel rc={res.rc}: {(res.err.strip() or res.out.strip())[-400:]}")
            res = self.user_run([f"{self.root}/venv/bin/pip", "show", "omnigent", "motel"])
            versions = dict(re.findall(r"^Name: (\S+)\nVersion: (\S+)$", res.out, re.M))
            c.evidence("installed: " + ", ".join(f"{k} {v}" for k, v in sorted(versions.items())))
            res = self.user_run(["sh", "-c", OMNI_HELP_SCRIPT, "sh", self.root], timeout=180)
            lines = [ln.strip() for ln in res.out.splitlines() if SERVER_PROVIDER in ln]
            c.evidence(f"`omnigent sandbox create --help` rc={res.rc}; lines naming motel: {lines[:3]}")
            c.require(res.rc == 0 and lines, f"`omnigent sandbox create --help` does not list motel (rc {res.rc}): {res.out.strip()[-300:]}")
            c.require(self.dry or versions.get("omnigent") == self.version,
                      f"the venv has omnigent {versions.get('omnigent')}, expected {self.version}")
        return not self.rep.last_failed()

    # ------------------------------------------------------------ 2. the disposable server
    def start_server(self) -> bool:
        with self.rep.check("omnigent: a disposable server starts on a free loopback port (not 6767) with a throwaway HOME and "
                            "OMNIGENT_HARNESS_TMP_PARENT, no key, and reports the motel provider", self.clock) as c:
            res = self.user_run(["python3", "-c", OMNI_PORT_PY])
            text = res.out.strip()
            c.require(self.dry or (res.rc == 0 and text.isdigit()), f"no free port: {res.err.strip()[:200] or text[:200]}")
            self.port = int(text) if text.isdigit() else 0
            c.require(self.dry or (1023 < self.port < 65536 and self.port != 6767), f"port {self.port} is not an unprivileged port, or is 6767")
            self.url = f"http://127.0.0.1:{self.port or '<port>'}"
            self.ctx.state["omnigent"].update(port=self.port)
            self.ctx.save_state()
            config = config_yaml(self.url, self.motel, self.version, self.args.cabin_type)
            res = self.user_run(["sh", "-c", OMNI_PUT_SCRIPT, "sh", f"{self.root}/config.yaml"], input=config)
            c.require(res.rc == 0, f"could not write the server config: {res.err.strip()[:200]}")
            self.server_started = True      # from here the drain stops it, whatever happens
            res = self.user_run(["sh", "-c", OMNI_START_SCRIPT, "sh", self.root, str(self.port or "<port>")], timeout=60)
            pid = res.out.strip().splitlines()[-1] if res.out.strip() else ""
            c.require(self.dry or (res.rc == 0 and pid.isdigit()), f"the server did not start (rc {res.rc}): {res.err.strip()[:200]}")
            self.ctx.state["omnigent"].update(pid=pid)
            self.ctx.save_state()
            c.evidence(f"server {self.url}, process group {pid}, HOME {self.home}, OMNIGENT_HARNESS_TMP_PARENT {self.root}/tmp, config {self.root}/config.yaml")
            wait_until(self.clock, lambda: self.api("GET", "/health")[0] == 200, timeout=self.HEALTH_TIMEOUT, interval=3,
                       what="the server's /health", dry=self.dry)
            res = self.user_run(["sh", "-c", OMNI_ENV_NAMES_SCRIPT, "sh", self.root])
            names = sorted(n for n in res.out.split() if n)
            c.evidence(f"the server's environment holds only: {names}")
            bad = [n for n in names if re.search(r"(?i)KEY|TOKEN|SECRET|PASSWORD|CREDENTIAL", n)]
            c.require(not bad, f"the server environment names a credential variable: {bad}")
            c.require(self.dry or "OMNIGENT_HARNESS_TMP_PARENT" in names, "the server was not started with OMNIGENT_HARNESS_TMP_PARENT")
            status, info = self.api("GET", "/v1/info")
            providers = self.doc(info).get("sandbox_providers")
            c.evidence(f"/v1/info: sandbox_providers {providers}, server_version {self.doc(info).get('server_version')}")
            c.require(self.dry or (status == 200 and isinstance(providers, list) and SERVER_PROVIDER in providers),
                      f"/v1/info (status {status}) does not list the motel provider: {providers}; see the server log")
        return not self.rep.last_failed()

    # ------------------------------------------------------------ 3. provision
    def pick_agent(self, c) -> str:
        status, doc = self.api("GET", "/v1/agents?limit=200")
        agents = [a for a in self.doc(doc).get("data") or [] if isinstance(a, dict) and a.get("id")]
        names = [a.get("name") for a in agents]
        c.evidence(f"the server's agents: {names}")
        c.require(self.dry or (status == 200 and agents), f"GET /v1/agents (status {status}) lists no agent")
        wanted = [self.args.omnigent_agent] if self.args.omnigent_agent else list(PREFERRED_AGENTS)
        for name in wanted:
            for agent in agents:
                if agent.get("name") == name:
                    return str(agent["id"])
        c.require(self.dry or not self.args.omnigent_agent, f"--omnigent-agent {self.args.omnigent_agent!r} is not one of {names}")
        return str(agents[0]["id"]) if agents else "<agent-id>"

    def provision(self) -> bool:
        with self.rep.check("omnigent: a managed session makes the provider check in exactly one cabin (visible in `motel cabin ls`)", self.clock) as c:
            before = [] if self.dry else self.cab.live_cabins()
            c.require(not before, f"cabins are live before the phase starts: {[r.get('lease_id') for r in before]}")
            agent_id = self.pick_agent(c)
            body = {"agent_id": agent_id, "host_type": "managed", "sandbox_provider": SERVER_PROVIDER, "title": "motel live qualification"}
            status, doc = self.api("POST", "/v1/sessions", body)
            self.session_id = str(self.doc(doc).get("id") or ("<session-id>" if self.dry else "")) or None
            c.evidence(f"POST /v1/sessions (host_type managed, sandbox_provider motel, agent {agent_id}) -> {status}"
                       f"{'' if self.session_id else ': ' + str(doc)[:300]}")
            c.require(self.dry or (status in (200, 201) and self.session_id), f"POST /v1/sessions -> {status}: {str(doc)[:300]}")
            self.mark("session created")
            seen: list = []

            def live():
                self.observe()
                self.sandbox_stage()
                rows = self.cab.live_cabins()
                seen[:] = rows
                return rows
            wait_until(self.clock, live, timeout=self.PROVISION_TIMEOUT, interval=self.POLL_S,
                       what="the provider to check a cabin in", dry=self.dry)
            ids = [str(r.get("lease_id")) for r in seen]
            c.require(self.dry or len(ids) == 1, f"{len(ids)} cabins are live, expected exactly one: {ids}")
            self.cabin_id = ids[0] if ids else "<cabin-id>"
            self.ctx.state["omnigent"].update(cabin=self.cabin_id)
            self.ctx.save_state()
            row = seen[0] if seen else {}
            c.evidence(f"cabin {self.cabin_id} (task {row.get('task')!r}, state {row.get('state')}) is live; stage {self._stage}")
        return not self.rep.last_failed()

    # ------------------------------------------------------------ 4. registration
    def register(self) -> bool:
        with self.rep.check("omnigent: the host on the cabin registers with the server through the relay", self.clock) as c:
            started = self.clock.time()

            def online():
                self.observe()
                self.sandbox_stage()
                found = [h for h in self.motel_hosts() if h.get("status") == "online"]
                if found:
                    self.mark(f"host {found[0].get('host_id')} online")
                return found
            found = wait_until(self.clock, online, timeout=self.REGISTER_TIMEOUT, interval=self.POLL_S,
                               what="the omnigent host on the cabin to register", dry=self.dry)
            host = found[0] if found and not self.dry else {}
            self.host_id = str(host.get("host_id") or ("<host-id>" if self.dry else "")) or None
            c.evidence(f"host {self.host_id} ({host.get('name')}) is online, sandbox_provider {host.get('sandbox_provider')}, "
                       f"{int(self.clock.time() - started)}s after the cabin was live")
            # The cabin firewall lets it reach only the public internet, so a registration at a loopback server URL can only have
            # come through the relay. The provider announces the tunnel in the server's log (best effort evidence).
            res = self.user_run(["sh", "-c", OMNI_COUNT_SCRIPT, "sh", self.root, f"tunnel: cabin 127.0.0.1:{self.port}"])
            c.evidence(f"server log lines announcing the relay for port {self.port}: {res.out.strip() or '0'}")
            c.evidence("timeline: " + "; ".join(self.timeline[-8:]))
        return not self.rep.last_failed()

    # ------------------------------------------------------------ 5. a command in the sandbox
    def command_checks(self) -> None:
        with self.rep.check("omnigent: a command run in the sandbox through the provider returns its output", self.clock) as c:
            res, doc = self.provider("run", self.cabin_id or "<cabin-id>", f"echo {self.token}; uname -s")
            d = self.doc(doc)
            c.evidence(f"provider.run on cabin {self.cabin_id}: rc {d.get('rc')}, stdout {str(d.get('stdout'))[:120]!r}")
            c.require(self.dry or (res.rc == 0 and d.get("rc") == 0 and self.token in str(d.get("stdout"))),
                      f"provider.run did not return the command's output (process rc {res.rc}): {res.err.strip()[-300:] or str(doc)[:300]}")
        if "shell" in self.skip:
            return
        with self.rep.check("omnigent: the session's shell endpoint runs a command on the cabin host and returns its output", self.clock) as c:
            sid = self.session_id or "<session-id>"

            def shell():
                self.observe()
                status, doc = self.api("GET", f"/v1/sessions/{sid}/resources/environments")
                envs = [e for e in self.doc(doc).get("data") or [] if isinstance(e, dict) and e.get("id")] if status == 200 else []
                env_id = str(envs[0]["id"]) if envs else "default"
                status, doc = self.api("POST", f"/v1/sessions/{sid}/resources/environments/{env_id}/shell",
                                       {"command": f"echo {self.token}", "timeout": 30})
                text = json.dumps(doc) if not isinstance(doc, str) else doc
                self.mark(f"shell endpoint -> {status}")
                self._shell_last = f"{status}: {text[:300]}"
                return status == 200 and self.token in text
            try:
                wait_until(self.clock, shell, timeout=self.SHELL_TIMEOUT, interval=self.POLL_S,
                           what="the shell endpoint to return the command's output", dry=self.dry)
            except AssertionError as exc:
                raise AssertionError(f"{exc}; last answer {self._shell_last or 'none'} (the session's runner may not have "
                                     "launched: skip this check with --omnigent-skip shell once the diagnostics say so)") from None
            c.evidence(f"shell endpoint returned the token: {self._shell_last[:120]}")

    # ------------------------------------------------------------ 6. keep_alive
    def keep_alive(self) -> None:
        with self.rep.check("omnigent: keep_alive extends the cabin's checkout time", self.clock) as c:
            cid = self.cabin_id or "<cabin-id>"
            rc, before = self.cabin_view(cid)
            c.require(self.dry or rc == 0, f"cabin status before: rc {rc}")
            res, doc = self.provider("keep_alive", cid)
            c.evidence(f"provider.keep_alive -> {self.doc(doc).get('result')} (process rc {res.rc})")
            c.require(self.dry or (res.rc == 0 and self.doc(doc).get("result") is True), f"keep_alive did not report success: {res.err.strip()[-300:] or doc}")
            rc, after = self.cabin_view(cid)
            c.require(self.dry or rc == 0, f"cabin status after: rc {rc}")
            t_before, t_after = parse_epoch(before.get("expires_at")), parse_epoch(after.get("expires_at"))
            c.evidence(f"checkout time {before.get('expires_at')} -> {after.get('expires_at')}; time left {before.get('time_left_s')}s -> {after.get('time_left_s')}s")
            c.require(self.dry or (t_before and t_after and t_after - t_before >= MIN_EXTENSION_S),
                      f"the checkout time moved by {None if not (t_before and t_after) else int(t_after - t_before)}s, expected at least {MIN_EXTENSION_S}s")

    # ------------------------------------------------------------ 7. terminate
    def release_state(self) -> str:
        """released, gone (absent), or the cabin's other state."""
        rc, cab = self.cabin_view(self.cabin_id or "<cabin-id>")
        if rc == 7:
            return "gone"
        return str(cab.get("state") or f"rc {rc}")

    def terminate(self) -> None:
        with self.rep.check("omnigent: terminate (the server's session delete) checks the cabin out and it reaches released", self.clock) as c:
            status, doc = self.api("DELETE", f"/v1/sessions/{self.session_id or '<session-id>'}", timeout=300)
            c.evidence(f"DELETE /v1/sessions/{self.session_id} -> {status}")
            c.require(self.dry or 200 <= status < 300, f"session delete -> {status}: {str(doc)[:300]}")
            self.session_deleted = True
            self.mark("session deleted")
            state = {"now": ""}

            def gone():
                self.observe()
                state["now"] = self.release_state()
                return state["now"] in ("released", "gone")
            try:
                wait_until(self.clock, gone, timeout=self.RELEASE_TIMEOUT, interval=self.POLL_S, what="the cabin to be released", dry=self.dry)
            except AssertionError as exc:
                raise AssertionError(f"{exc}; the cabin is {state['now']!r} (cleanup_pending still bills)") from None
            c.evidence(f"cabin {self.cabin_id} is {state['now']} after the session delete")
            c.evidence(f"hosts of the motel provider still listed: {[h.get('host_id') for h in self.motel_hosts()] if not self.dry else []}")

    # ------------------------------------------------------------ teardown
    def stop_server(self) -> str:
        """Stop what this phase started (its recorded process group). Returns the script's answer."""
        res = self.user_run(["sh", "-c", OMNI_STOP_SCRIPT, "sh", self.root], timeout=120)
        self.stopped = True
        return (res.out.strip() or res.err.strip() or f"rc {res.rc}") if not res.dry else "stopped <pid>"

    def drain(self) -> None:
        """Best effort first (delete the session, stop the server, check out whatever cabin is left), then the final checks."""
        answer = ""
        if not self.dry:
            if self.server_started and self.session_id and not self.session_deleted:
                self.api("DELETE", f"/v1/sessions/{self.session_id}", timeout=300)
            if self.server_started:
                answer = self.stop_server()
            for row in self.cab.live_cabins():
                if row.get("lease_id"):
                    self.cab.cabin("checkout", str(row["lease_id"]))
            self.cab.reap()
        if self.server_started or self.dry:
            with self.rep.check("omnigent: the disposable server is stopped (its recorded process group, nothing else) and no longer answers", self.clock) as c:
                if self.dry:
                    self.stop_server()
                    self.api("GET", "/health")
                else:
                    c.evidence(f"stop: {answer}")
                    c.require(answer.startswith(("stopped", "no-record")), f"the server was not stopped: {answer}")
                    status, _ = self.api("GET", "/health", timeout=30)
                    c.evidence(f"GET /health after the stop: {status} (0 = no answer)")
                    c.require(status == 0, f"the server still answers on port {self.port} (status {status})")
        with self.rep.check("omnigent: after the phase no cabin server, firewall, address or volume remains", self.clock) as c:
            self.cab.wait_clean()
            self.observe()
            c.evidence("the cabins project holds nothing new, and the rooms project gained no cabin resource")
        self.cost()

    def expected_cabins(self) -> int:
        """One cabin once a session was asked for; none when the phase stopped before that."""
        return 1 if self.session_id else 0

    def cost(self) -> None:
        if not self.dry:
            self.meter.poll()           # the last look, unthrottled: the cabins are gone by now
        rows = self.meter.rows()
        hours = sum(r["billed_hours"] for r in rows)
        price = None if self.dry else self.hcc.hourly_price(self.args.cabin_type, self.args.location)
        est = None if price is None else round(hours * price, 4)
        self.rep.meta["omnigent_cabins"] = {"count": len(rows), "expected": self.expected_cabins(), "billed_hours": hours,
                                            "hourly_eur_gross": price, "estimate_eur": est, "cabins": rows}
        with self.rep.check("omnigent: cost: the cabins this phase created, in billed hours", self.clock) as c:
            for r in rows:
                c.evidence(f"cabin {r['lease']} (server {r['server']}, {r['type']}): {r['seconds']} s, {r['billed_hours']} billed hour(s)"
                           f"{'' if r['deleted'] else ', STILL LISTED'}")
            c.evidence(f"total {len(rows)} cabin(s), {hours} billed hour(s)" + (f" x EUR {price} = EUR {est}" if price else " (price unknown)"))
            c.require(len(rows) == self.expected_cabins(),
                      f"{len(rows)} cabin(s) were created, expected {self.expected_cabins()}: a retry or a stray cabin is billed too")
            c.require(all(r["deleted"] for r in rows), "a cabin of this phase is still listed")

    # ------------------------------------------------------------ diagnostics
    def scrub(self, text) -> str:
        """Known secrets, key shapes, bearer values and ``NAME_TOKEN=value`` assignments (a launch token in a log line) are replaced."""
        return self.ctx.redact(text)

    def diagnose_failure(self, check_name: str) -> None:
        if check_name.startswith("omnigent:") and not self.dry:
            self.diagnose(check_name)

    def diagnose(self, why: str) -> str | None:
        """Scrubbed artifacts in ``<out dir>/omnigent-diagnostics/NN-<why>/``: the server and provider log tails, the install log, the
        session, host and info documents, ``motel cabin ls`` and ``status``, the cabin-side logs (host log, harness install log) of every
        live cabin, the cabins project's servers and the phase's timeline. Best effort: a failed step is noted in ``index.json``."""
        run_dir = getattr(self.ctx, "run_dir", None)
        if self.dry or not run_dir or self.diag_n >= self.MAX_DIAGNOSES:
            return None
        self.diag_n += 1
        slug = re.sub(r"[^a-z0-9]+", "-", why.lower()).strip("-")[:60] or "check"
        base = os.path.join(run_dir, DIAG_DIR, f"{self.diag_n:02d}-{slug}")
        os.makedirs(base, exist_ok=True)
        index: dict = {"why": self.scrub(why), "taken_at": datetime.now(UTC).isoformat(timespec="seconds"), "files": [], "errors": []}

        def save(name: str, text: str) -> None:
            path = os.path.join(base, name)
            os.makedirs(os.path.dirname(path), exist_ok=True)
            with open(path, "w", encoding="utf-8") as handle:
                handle.write(self.scrub(text))
            index["files"].append(name)

        def step(label: str, fn) -> None:
            try:
                fn()
            except Exception as exc:  # noqa: BLE001
                index["errors"].append(f"{label}: {type(exc).__name__}: {str(exc)[:200]}")

        def keep(name: str, res) -> None:
            save(name, res.out + (f"\n[stderr]\n{res.err}" if res.err.strip() else "") + (f"\n[rc={res.rc}]" if res.rc else ""))

        def doc_of(path: str, name: str) -> None:
            status, doc = self.api("GET", path, timeout=60)
            save(name, f"HTTP {status}\n" + (json.dumps(doc, indent=1, sort_keys=True) if not isinstance(doc, str) else doc))

        step("server and provider logs", lambda: save("server-and-provider-logs.txt", self.logs_text()))
        step("info", lambda: doc_of("/v1/info", "api-info.json"))
        step("hosts", lambda: doc_of("/v1/hosts", "api-hosts.json"))
        if self.session_id:
            step("session", lambda: doc_of(f"/v1/sessions/{self.session_id}", "api-session.json"))
        save("timeline.txt", "\n".join(self.timeline) + "\n")
        step("cabin ls", lambda: keep("cabin-ls.json", self.cab.cabin("ls", "--json", timeout=120)[0]))
        live: list = []

        def cabins() -> None:
            live.extend(str(r.get("lease_id")) for r in self.cab.live_cabins() if r.get("lease_id"))
            if self.cabin_id and self.cabin_id not in live:
                live.append(self.cabin_id)
        step("live cabins", cabins)
        for cid in live:
            step(f"cabin status {cid}", lambda cid=cid: keep(f"cabin-status-{cid}.json", self.cab.cabin("status", cid, "--json", timeout=120)[0]))
            step(f"cabin-side logs {cid}", lambda cid=cid: keep(
                f"cabin-side-logs-{cid}.txt",
                self.user_run(["env", f"HOME={self.home}", CLI, "cabin", "ssh", cid, "--", "sh", "-c", OMNI_CABIN_LOGS_SCRIPT],
                              timeout=120)))

        def servers() -> None:
            initial = set((self.ctx.cabins_initial or {}).get("server", {}))
            found = [sv for sv in self.hcc.list("server") if str(sv.get("id")) not in initial]
            save("cabins-servers.json", json.dumps(found, indent=1, sort_keys=True))
        step("cabins project servers", servers)
        save("index.json", json.dumps(index, indent=1, sort_keys=True))
        self.diag_dirs.append(base)
        self.rep.meta["omnigent_diagnostics"] = list(self.diag_dirs)
        return base
