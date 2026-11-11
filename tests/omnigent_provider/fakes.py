"""Fakes for the Omnigent provider tests: a ``motel`` executable and an ``ssh`` that run nothing real.

The fake ``motel`` answers ``cabin checkin|ls|status|extend|checkout`` from canned rules (like tests/test_sdk.py) and
records every call. ``cabin ssh ID -- CMD`` records argv and the script on stdin separately, runs the cabin-side relay
for real (it only listens on loopback) and answers other commands from rules. No network, no cloud, no real Omnigent.
"""

import hashlib
import json
import stat
import sys
from pathlib import Path

CABIN_ID = "managed-a1b2c3d4-0123456789ab"
CABIN = {
    "schema": "motel/v1", "lease_id": CABIN_ID, "name": f"motel-{CABIN_ID}", "state": "active", "task": "managed-a1b2c3d4",
    "owner": "alice", "created_at": "2026-03-01T12:40:00Z", "expires_at": "2026-03-01T14:40:00Z",
    "offer": {"server_type": "cpx32", "location": "fsn1", "total_hourly_net_eur": "0.0200"}, "address": "192.0.2.10",
}

FAKE = """#!{python}
import hashlib, json, os, subprocess, sys, time
d = os.environ["FAKE_MOTEL_DIR"]
args = sys.argv[1:]
def append(name, doc):
    with open(os.path.join(d, name), "a") as f:
        f.write(json.dumps(doc) + "\\n")
append("argv.jsonl", sys.argv)
with open(os.path.join(d, "rules.json")) as f:
    rules = json.load(f)
if args[:2] == ["cabin", "ssh"]:
    remote = args[4:]
    if remote[:1] == ["python3"] and "-c" in remote:
        # Like the real CLI, run the "ssh session" (here: the relay program) as a CHILD with inherited stdio.
        i = remote.index("-c")
        rest = remote[i + 2:]
        if os.environ.get("FAKE_RELAY_LISTEN_PORT") and rest[:1] == ["cabin"]:
            rest = ["cabin", os.environ["FAKE_RELAY_LISTEN_PORT"], *rest[2:]]
        if os.environ.get("FAKE_RELAY_FAIL"):
            sys.stderr.write("motel: no access files for cabin. Try: motel cabin ls\\n")
            sys.exit(7)
        time.sleep(float(os.environ.get("FAKE_RELAY_DELAY", "0")))  # a slow ssh connect
        if os.environ.get("FAKE_RELAY_PRELUDE"):  # a login script that prints to stdout
            sys.stdout.write(os.environ["FAKE_RELAY_PRELUDE"])
            sys.stdout.flush()
        sys.exit(subprocess.run([sys.executable, "-I", "-c", remote[i + 1], *rest]).returncode)
    data = sys.stdin.buffer.read()
    if remote[:2] == ["sh", "-c"] and "cat >" in remote[2]:
        path = os.path.join(d, "root", remote[4].lstrip("/"))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "wb") as f:
            f.write(data)
        sys.exit(0)
    script = data.decode("utf-8", "replace")
    append("stdin.jsonl", {"remote": remote, "script": script})
    body = script.strip()
    if body.startswith("{") and body.endswith("}"):
        body = body[1:-1].strip()
    if body.startswith("sha256sum -- "):
        path = os.path.join(d, "root", body.split("-- ", 1)[1].strip().strip("'").lstrip("/"))
        digest = hashlib.sha256(open(path, "rb").read()).hexdigest()
        if rules and any(r.get("corrupt_upload") for r in rules):
            digest = "0" * 64
        print(digest + "  " + path)
        sys.exit(0)
    for rule in rules:
        if "script_contains" in rule and rule["script_contains"] in script:
            sys.stdout.write(rule.get("stdout", ""))
            sys.stderr.write(rule.get("stderr", ""))
            sys.exit(rule.get("exit", 0))
    if 'printf %s "$HOME"' in script:
        sys.stdout.write("/srv/cabin")
    sys.exit(0)
for rule in rules:
    if "match" in rule and [a for a in args if a != "--json"][:len(rule["match"])] == rule["match"]:
        time.sleep(rule.get("sleep", 0))
        sys.stdout.write(rule.get("stdout", ""))
        sys.stderr.write(rule.get("stderr", ""))
        sys.exit(rule.get("exit", 0))
sys.stderr.write("motel: fake has no rule for %s. Try: motel --help\\n" % args)
sys.exit(2)
"""

FAKE_SSH = """#!{python}
import json, os, sys, time
with open(os.environ["FAKE_MOTEL_DIR"] + "/ssh.jsonl", "a") as f:
    f.write(json.dumps(sys.argv) + "\\n")
mode = os.environ.get("FAKE_SSH_MODE", "hold")
if mode == "refuse":
    sys.stderr.write("Error: remote port forwarding failed for listen port\\n")
    sys.exit(255)
time.sleep(30)
"""


def ok(**doc):
    return {"stdout": json.dumps({"schema": "motel/v1", "ok": True, **doc})}


def fail(code, error, hint="motel cabin ls"):
    return {"exit": code, "stdout": json.dumps({"schema": "motel/v1", "ok": False, "error": error, "hint": hint, "code": code})}


class Rig:
    def __init__(self, path: Path):
        self.dir = path
        self.motel = path / "motel"
        self.ssh = path / "ssh"
        self.rules: list = []
        for target, body in ((self.motel, FAKE), (self.ssh, FAKE_SSH)):
            target.write_text(body.replace("{python}", sys.executable))
            target.chmod(target.stat().st_mode | stat.S_IXUSR)
        self.write_rules()

    def write_rules(self):
        (self.dir / "rules.json").write_text(json.dumps(self.rules))

    def rule(self, *match, **response):
        self.rules.append({"match": list(match), **response})
        self.write_rules()

    def script_rule(self, contains, **response):
        self.rules.append({"script_contains": contains, **response})
        self.write_rules()

    def lines(self, name):
        path = self.dir / name
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def argv(self):
        return [a[1:] for a in self.lines("argv.jsonl")]

    def scripts(self):
        return [entry["script"] for entry in self.lines("stdin.jsonl")]

    def uploaded(self, remote):
        return (self.dir / "root" / remote.lstrip("/")).read_bytes()


def sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def argv_text(rig) -> str:
    return json.dumps(rig.lines("argv.jsonl")) + json.dumps(rig.lines("ssh.jsonl"))
