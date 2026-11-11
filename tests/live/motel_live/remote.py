"""ssh / scp to the throwaway test server. Commands are argv lists joined with shlex; secrets only ever travel as file paths."""

from __future__ import annotations

import shlex

from motel_live.core import Clock, CmdResult, Runner

REMOTE_SRC_PARENT = "/root"
REMOTE_SRC = f"{REMOTE_SRC_PARENT}/motel-src"  # where the harness unpacks the motel ref on the server

# Fixed remote snippets. The fake ssh used by the unit tests recognises them by equality.
WRITE_SCRIPT = 'printf %s "$2" > "$1"'
PUT_SCRIPT = 'cat > "$1" && chmod "$2" "$1"'
PREP_TOKEN_SCRIPT = 'umask 077; : > "$1"; chown root:root "$1"; chmod 0600 "$1"'
KILL_SCRIPT = 'kill "$(cat "$1")"'
GIT_PREP_SCRIPT = 'mkdir -p "$1" && cd "$1" && git init -q && echo hello > hello.txt'
CD_SCRIPT = 'cd "$1" && shift && exec "$@"'
HOLD_SCRIPT = 'echo $$ > "$2"; exec 9>>"$1"; exec sleep 3600'
# TCP connect from the test server to $1:22 (bash's /dev/tcp; no extra package needed). Exit 0 when it connects.
REACH_SCRIPT = "timeout 10 bash -c ': </dev/tcp/$0/22' \"$1\""
# Like REACH_SCRIPT, and prints the first line the cabin's port 22 sends (the sshd banner). $1 = the cabin's address. Exit 0 and a
# banner: sshd answers; exit 0 and no banner: connected, nothing said; 1: refused; 124: no answer (dropped).
REACH_BANNER_SCRIPT = "timeout 10 bash -c 'exec 3<>/dev/tcp/$0/22 && timeout 5 head -n 1 <&3' \"$1\""
# Every attempt record and the event log of one tour, each under a "== name" line. $1 = /var/lib/motel/tours/<tour>.
ATTEMPTS_SCRIPT = ('cd "$1" && for f in tour.json attempts/*.json events.jsonl; do [ -f "$f" ] && { printf "== %s\\n" "$f"; cat "$f"; echo; }; done; true')
# A small git repository with one commit on `main`, as the tour user, for `motel tour start` (the guests bundle it). $1 = directory.
TOUR_REPO_SCRIPT = ('mkdir -p "$1" && cd "$1" && git init -q -b main && echo hello > hello.txt && git add hello.txt && '
                    'git -c user.name=live -c user.email=live@example.invalid commit -q -m base')
# The environment of the running concierge, as its process was started: $1 = unit. Only the names the store logic reads.
ENV_SCRIPT = ('pid=$(systemctl show -p MainPID --value "$1"); [ "$pid" -gt 0 ] && '
              "tr '\\0' '\\n' < /proc/$pid/environ | grep -E '^(FDSTORE|MOTEL_FDSTORE_[A-Z]+)='")
# Samples the processes of the inspection user ($2) every 20 ms into $1 (command line plus the resource limits that process
# really has), until its transient unit is stopped. `prlimit` execs git, so the limits are read from /proc, not from argv.
SAMPLER_SCRIPT = ('while :; do for p in $(pgrep -u "$2"); do echo "$p $(tr \'\\0\' \' \' < /proc/$p/cmdline 2>/dev/null) # "'
                  '"$(grep -E \'^Max (address space|processes|file size|cpu time) \' /proc/$p/limits 2>/dev/null | tr -s \' \' | '
                  'tr \'\\n\' \';\')"; done; sleep 0.02; done >> "$1" 2>/dev/null')
# sha256 and size of the stored result bundle of one guest, as root. $1 = .../results/<guest> (the bundle is one level down, per attempt).
SHA_SCRIPT = 'sha256sum "$1"/*/result.bundle && stat -c %s "$1"/*/result.bundle'
# -- the omnigent phase (all run as the unprivileged cabin user unless noted; $1 is the phase's directory on the test server)
# Write stdin to $1 (creating its directory). The fake ssh used by the unit tests recognises every snippet here by equality.
OMNI_PUT_SCRIPT = 'mkdir -p "$(dirname "$1")" && cat > "$1"'
# A venv with a pinned omnigent and motel (the unpacked ref, $3). $1 = directory, $2 = omnigent version. Installs nothing else.
OMNI_INSTALL_SCRIPT = ('set -e; mkdir -p "$1/home" "$1/tmp" "$1/data"; python3 -m venv "$1/venv"; '
                       '"$1/venv/bin/pip" install --disable-pip-version-check --quiet "omnigent==$2" "$3" > "$1/install.log" 2>&1 '
                       '|| { tail -n 40 "$1/install.log" >&2; exit 1; }')
# `omnigent sandbox create --help` lists every registered provider; it must list motel. HOME is the throwaway one.
OMNI_HELP_SCRIPT = 'HOME="$1/home" "$1/venv/bin/omnigent" sandbox create --help 2>&1'
# A free unprivileged loopback port (the kernel's pick; never the 6767 of a real server).
OMNI_PORT_PY = 'import socket; s = socket.socket(); s.bind(("127.0.0.1", 0)); print(s.getsockname()[1])'
# Start the disposable server detached. $1 = directory, $2 = port. A clean environment (no key, no token of the caller), a throwaway
# HOME and OMNIGENT_HARNESS_TMP_PARENT, an explicit port and database (so it is a dedicated server that never touches a shared
# pid file). The inner shell records its own pid, then becomes the server: it is the leader of its own session, so that pid is
# the process group of everything the server starts, which is what the stop script signals.
OMNI_START_SCRIPT = (
    'rm -f "$1/server.pid"; '
    'setsid nohup sh -c \'echo $$ > "$1/server.pid"; exec env -i HOME="$1/home" PATH="$1/venv/bin:/usr/local/bin:/usr/bin:/bin" '
    'LANG=C.UTF-8 PYTHONUNBUFFERED=1 OMNIGENT_HARNESS_TMP_PARENT="$1/tmp" "$1/venv/bin/omnigent" server --host 127.0.0.1 '
    '--port "$2" --database-uri "sqlite:///$1/data/chat.db" --artifact-location "$1/data/artifacts" --config "$1/config.yaml" '
    '--no-open\' sh "$1" "$2" >> "$1/server.log" 2>&1 < /dev/null & '
    'for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do [ -s "$1/server.pid" ] && break; sleep 0.2; done; cat "$1/server.pid"')
# The NAMES (never values) of the variables the running server was started with: no key or token may be among them.
OMNI_ENV_NAMES_SCRIPT = 'tr "\\0" "\\n" < /proc/$(cat "$1/server.pid")/environ | cut -d= -f1'
# Stop what OMNI_START_SCRIPT started, and only that: the recorded process group, after checking the recorded pid still is this
# directory's server (a reused pid is left alone). Prints "stopped PID", "no-record" or "still-running PID" (exit 1).
OMNI_STOP_SCRIPT = (
    'pid=$(cat "$1/server.pid" 2>/dev/null); case "$pid" in \'\'|*[!0-9]*) echo no-record; exit 0 ;; esac; '
    'if grep -aqF "$1" /proc/$pid/cmdline 2>/dev/null; then kill -TERM -"$pid" 2>/dev/null; '
    'for i in 1 2 3 4 5 6 7 8 9 10 11 12 13 14 15 16 17 18 19 20; do kill -0 "$pid" 2>/dev/null || break; sleep 0.5; done; '
    'kill -0 "$pid" 2>/dev/null && { kill -KILL -"$pid" 2>/dev/null; sleep 1; }; fi; '
    'if kill -0 "$pid" 2>/dev/null; then echo "still-running $pid"; exit 1; fi; rm -f "$1/server.pid"; echo "stopped $pid"')
# The tail of the server's log (the provider logs into it too) and of every other log under the throwaway HOME.
OMNI_LOGS_SCRIPT = ('for f in "$1/server.log" "$1/install.log" $(find "$1/home" -name "*.log" -type f 2>/dev/null); do '
                    '[ -f "$f" ] && { printf "== %s\\n" "$f"; tail -n 400 "$f"; echo; }; done; true')
# The tail of the cabin-side logs, run on the cabin through `motel cabin ssh` (host log, background harness install log).
OMNI_CABIN_LOGS_SCRIPT = ('for f in /tmp/omnigent-host.log /tmp/omnigent-harness-install.log; do '
                          '[ -f "$f" ] && { printf "== %s\\n" "$f"; tail -n 200 "$f"; echo; }; done; true')
# Number of lines of the server log that contain $2.
OMNI_COUNT_SCRIPT = 'grep -c -F -- "$2" "$1/server.log"; true'
# One HTTP request to the server on this machine's loopback (stdlib only, no proxy, no credential). Prints the status code (0 when
# nothing answered), then the body. argv: METHOD URL; a body travels on stdin.
OMNI_API_PY = (
    'import sys, urllib.request, urllib.error\n'
    'method, url = sys.argv[1], sys.argv[2]\n'
    'data = None if method in ("GET", "DELETE") else sys.stdin.read().encode()\n'
    'req = urllib.request.Request(url, data=data, method=method, headers={"Content-Type": "application/json", "Accept": "application/json"})\n'
    'opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))\n'
    'try:\n'
    '    with opener.open(req, timeout=90) as r:\n'
    '        status, text = r.status, r.read().decode("utf-8", "replace")\n'
    'except urllib.error.HTTPError as e:\n'
    '    status, text = e.code, e.read().decode("utf-8", "replace")\n'
    'except Exception as e:\n'
    '    status, text = 0, type(e).__name__ + ": " + str(e)\n'
    'print(status)\n'
    'print(text)\n')
# The provider itself, driven from a second process in the same venv (a fresh launcher object, as Omnigent builds one per call).
# argv: OP MOTEL_BINARY TTL CABIN [COMMAND]. `run` returns the command's result; `keep_alive` pushes the checkout time.
OMNI_PROVIDER_PY = (
    'import json, sys\n'
    'from omnigent.community.sandbox.motel.config import MotelSandboxConfig\n'
    'from omnigent.community.sandbox.motel.launcher import MotelSandboxLauncher\n'
    'op, motel, ttl, cabin = sys.argv[1:5]\n'
    'launcher = MotelSandboxLauncher(MotelSandboxConfig(motel_binary=motel, ttl=ttl, harness_packages=[], tunnel="none"))\n'
    'if op == "run":\n'
    '    res = launcher.run(cabin, sys.argv[5], check=False)\n'
    '    print(json.dumps({"rc": res.returncode, "stdout": res.stdout, "stderr": res.stderr}))\n'
    'elif op == "keep_alive":\n'
    '    print(json.dumps({"result": launcher.keep_alive(cabin)}))\n')
REMOTE_CABINS_TOKEN = "/root/.motel-live-cabins-token"  # the cabins project token, kept apart from the rooms one
DESK = "/usr/local/sbin/motel-desk"
CLI = "/usr/local/bin/motel"


class Remote:
    def __init__(self, runner: Runner, clock: Clock, *, ssh_cmd: list, scp_cmd: list, host: str, key: str, known_hosts: str,
                 user: str = "root"):
        self.r, self.clock = runner, clock
        self.ssh_cmd, self.scp_cmd, self.host, self.key, self.known_hosts, self.user = ssh_cmd, scp_cmd, host, key, known_hosts, user

    def _opts(self) -> list:
        # -F /dev/null: an isolated client config; never read the controller's system or user ssh config.
        return ["-F", "/dev/null", "-i", self.key, "-o", "IdentitiesOnly=yes", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=accept-new",
                "-o", f"UserKnownHostsFile={self.known_hosts}", "-o", "ConnectTimeout=10", "-o", "ServerAliveInterval=15"]

    def ssh_argv(self, remote_argv: list) -> list:
        return [*self.ssh_cmd, *self._opts(), f"{self.user}@{self.host}", shlex.join(str(a) for a in remote_argv)]

    def run(self, remote_argv: list, *, as_user: str | None = None, input: str | None = None, timeout: float = 300) -> CmdResult:
        if as_user:
            remote_argv = ["sudo", "-n", "-u", as_user, "-H", "--", *remote_argv]
        return self.r.run(self.ssh_argv(remote_argv), input=input, timeout=timeout)

    def popen(self, remote_argv: list, *, as_user: str | None = None):
        if as_user:
            remote_argv = ["sudo", "-n", "-u", as_user, "-H", "--", *remote_argv]
        return self.r.popen(self.ssh_argv(remote_argv))

    def scp_to(self, local: str, remote_path: str, timeout: float = 600) -> CmdResult:
        return self.r.run([*self.scp_cmd, *self._opts(), "-q", local, f"{self.user}@{self.host}:{remote_path}"], timeout=timeout)

    def wait_ssh(self, timeout: float = 420, interval: float = 6, *, gone_first: bool = False) -> bool:
        """Poll until ``ssh true`` works. With ``gone_first`` wait for a failure first (a reboot) so a stale session is not mistaken."""
        if self.r.dry_run:
            self.r.run(self.ssh_argv(["true"]))
            return True
        deadline = self.clock.time() + timeout
        if gone_first:
            for _ in range(20):
                if self.r.run(self.ssh_argv(["true"]), timeout=20).rc != 0:
                    break
                self.clock.sleep(3)
        while self.clock.time() < deadline:
            if self.r.run(self.ssh_argv(["true"]), timeout=30).rc == 0:
                return True
            self.clock.sleep(interval)
        return False

    # ----------------------------------------------------------- conveniences
    def desk(self, *args, timeout: float = 600) -> CmdResult:
        """The front desk as root (operator commands: setup, reap)."""
        return self.run([DESK, *args], timeout=timeout)

    def motel(self, user: str, *args, timeout: float = 600) -> CmdResult:
        """The unprivileged CLI as ``user`` (goes through the real sudo rule)."""
        return self.run([CLI, *args], as_user=user, timeout=timeout)

    def write_file(self, path: str, content: str, as_user: str | None = None) -> CmdResult:
        return self.run(["sh", "-c", WRITE_SCRIPT, "sh", path, content], as_user=as_user)

    def read_file(self, path: str, as_user: str | None = None) -> CmdResult:
        return self.run(["cat", path], as_user=as_user)

    def put_text(self, path: str, text: str, mode: str = "0644") -> CmdResult:
        """Write ``text`` (sent on stdin, never on the command line) to a root-owned file."""
        return self.run(["sh", "-c", PUT_SCRIPT, "sh", path, mode], input=text)
