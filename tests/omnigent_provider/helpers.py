"""Helpers for the Omnigent provider tests that need Omnigent importable."""

import json
import socket
import time

from omnigent.community.sandbox.motel.config import MotelSandboxConfig
from omnigent.community.sandbox.motel.launcher import MotelSandboxLauncher
from omnigent_provider.fakes import CABIN, ok

TOKEN = "tok_launch_9f8e7d6c5b4a"  # gitleaks:allow (fake test value)
KEY = "sk-ant-test-0123456789abcdef"
HOST_COMMAND = (f"OMNIGENT_HOST_TOKEN='{TOKEN}' OMNIGENT_HOST_ID=host_1 OMNIGENT_HOST_NAME=managed-1 "
                "omnigent host --server '{url}'")


def make(rig, tmp_path=None, environ=None, **config):
    """A NEW launcher each call, as Omnigent's server builds one per launch, keep-alive and teardown."""
    config.setdefault("omnigent_version", "0.21.0")
    return MotelSandboxLauncher(MotelSandboxConfig(motel_binary=str(rig.motel), **config), environ=environ or {},
                                ssh_binary=str(rig.ssh), sleep=lambda _s: None, tunnel_interval=0.1, tunnel_failures=3)


def seed(rig):
    rig.rule("cabin", "checkin", stdout=json.dumps(CABIN))
    rig.rule("cabin", "checkout", **ok(cabin={**CABIN, "state": "released"}))


def echo_through(port, payload=b"through the tunnel"):
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.settimeout(5)
        client.sendall(payload)
        return client.recv(len(payload) + 10)


def connect_retry(port, timeout=8.0):
    end = time.time() + timeout
    while True:
        try:
            return echo_through(port)
        except OSError:
            if time.time() > end:
                raise
            time.sleep(0.05)


def wait_until(check, timeout=8.0, step=0.05):
    end = time.time() + timeout
    while time.time() < end:
        value = check()
        if value:
            return value
        time.sleep(step)
    return check()


def port_open(port) -> bool:
    try:
        socket.create_connection(("127.0.0.1", port), timeout=1).close()
        return True
    except OSError:
        return False
