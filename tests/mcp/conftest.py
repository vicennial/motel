"""Fixtures: a fake `motel` executable on PATH and a helper that drives the MCP server over stdio."""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

SRC = Path(__file__).resolve().parents[2] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

FAKE_MOTEL = """#!{python}
import json, os, sys, time
# Behaviour comes from a control file that tests rewrite between calls.
with open(os.environ["FAKE_MOTEL_LOG"], "a") as f:
    f.write(json.dumps(sys.argv[1:]) + "\\n")
try:
    with open(os.environ["FAKE_MOTEL_CONTROL"]) as f:
        ctl = json.load(f)
except OSError:
    ctl = {{}}
time.sleep(float(ctl.get("sleep", 0)))
sys.stdout.write(ctl.get("stdout", '{{"ok": true}}'))
sys.stderr.write(ctl.get("stderr", ""))
sys.exit(int(ctl.get("exit", 0)))
"""


class Client:
    """A running `python -m motel.mcp_server` process speaking newline-delimited JSON-RPC."""

    def __init__(self, env: dict, args: tuple = ()):
        self.proc = subprocess.Popen(
            [sys.executable, "-m", "motel.mcp_server", *args],
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=env,
        )
        self._id = 0

    def send(self, message: dict | str) -> None:
        line = message if isinstance(message, str) else json.dumps(message)
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def recv(self) -> dict:
        line = self.proc.stdout.readline()
        assert line, f"server closed stdout; stderr={self.proc.stderr.read()}"
        return json.loads(line)

    def request(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        message = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)
        reply = self.recv()
        assert reply["id"] == self._id
        return reply

    def notify(self, method: str, params: dict | None = None) -> None:
        message = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            message["params"] = params
        self.send(message)

    def handshake(self, version: str = "2025-06-18") -> dict:
        reply = self.request(
            "initialize",
            {"protocolVersion": version, "capabilities": {}, "clientInfo": {"name": "test", "version": "0"}},
        )
        self.notify("notifications/initialized")
        return reply

    def call(self, name: str, arguments: dict | None = None) -> dict:
        params = {"name": name}
        if arguments is not None:
            params["arguments"] = arguments
        return self.request("tools/call", params)

    def close(self) -> None:
        self.proc.stdin.close()
        self.proc.wait(timeout=10)
        self.proc.stdout.close()
        self.proc.stderr.close()


class Env:
    def __init__(self, tmp_path: Path):
        self.bindir = tmp_path / "bin"
        self.bindir.mkdir()
        self.log = tmp_path / "argv.log"
        fake = self.bindir / "motel"
        fake.write_text(FAKE_MOTEL.format(python=sys.executable))
        fake.chmod(0o755)
        self.vars = {
            "PATH": f"{self.bindir}{os.pathsep}{os.environ.get('PATH', '')}",
            "PYTHONPATH": str(SRC),
            "FAKE_MOTEL_LOG": str(self.log),
            "FAKE_MOTEL_CONTROL": str(tmp_path / "control.json"),
        }
        self.control = tmp_path / "control.json"
        self.clients: list[Client] = []

    def set(self, **kw) -> None:
        """Set the fake's behaviour (stdout, stderr, exit, sleep) for subsequent calls."""
        self.control.write_text(json.dumps(kw))

    def client(self, *args: str, extra_env: dict | None = None) -> Client:
        env = {k: v for k, v in os.environ.items() if k != "MOTEL_BIN"} | self.vars | (extra_env or {})
        c = Client(env, args)
        self.clients.append(c)
        return c

    def calls(self) -> list[list[str]]:
        if not self.log.exists():
            return []
        return [json.loads(line) for line in self.log.read_text().splitlines()]


@pytest.fixture
def env(tmp_path):
    e = Env(tmp_path)
    yield e
    for c in e.clients:
        if c.proc.poll() is None:
            try:
                c.close()
            except (OSError, subprocess.SubprocessError):
                c.proc.kill()


@pytest.fixture
def client(env):
    c = env.client()
    c.handshake()
    return c
