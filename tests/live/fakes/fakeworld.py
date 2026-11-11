"""A tiny fake cloud plus a toy motel, shared by the fake `hcloud`, `ssh`, `scp` and `ssh-keygen` used in the harness tests.

There are two cloud projects, as in a real cabins run. The rooms project (``world.json``) holds the test server and is what the plain
`hcloud` fake talks to. The cabins project (``world-cabins.json``) is what a wrapper that sets ``FAKE_PROJECT=cabins`` talks to; the toy
motel creates cabins there. An API token belongs to a project (``tokens.json``: token -> project name; unlisted tokens belong to the
rooms project), and the toy's cabin setup applies motel's rule: it refuses a project that holds the host or anything motel did not create.

State lives in $FAKE_WORLD/world.json, and every fake command (and the harness's fake clock) reads, changes and writes it under one lock
(``world_lock``), because a read-modify-write that overlaps another one silently drops the other's update. The toy motel follows docs/lifecycle.md closely enough to drive the whole rooms scenario;
it is not a second implementation of motel, only a stand-in for the commands the harness issues.
"""

from __future__ import annotations

import contextlib
import fcntl
import hashlib
import json
import os
import re
import shlex
import sys
import time
from datetime import UTC, datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
from motel_live.remote import (  # noqa: E402
    ATTEMPTS_SCRIPT,
    CD_SCRIPT,
    ENV_SCRIPT,
    GIT_PREP_SCRIPT,
    HOLD_SCRIPT,
    KILL_SCRIPT,
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
    PREP_TOKEN_SCRIPT,
    PUT_SCRIPT,
    REACH_BANNER_SCRIPT,
    REACH_SCRIPT,
    SAMPLER_SCRIPT,
    SHA_SCRIPT,
    TOUR_REPO_SCRIPT,
    WRITE_SCRIPT,
)

MOUNT = "/mnt/motel"
TOKEN = "/etc/motel/credentials/hetzner.token"
CABINS_TOKEN = "/etc/motel/credentials/cabins.token"
TOURS_ROOT = "/var/lib/motel/tours"
INSPECT_DIR = "/var/lib/motel-inspect"
SAMPLES = "/tmp/motel-live-inspect-samples"
UNIT_FILES = ("/etc/systemd/system/motel-concierge.service", "/etc/systemd/system/motel-concierge.socket")
BOOKING_S = 15  # a toy guest books its cabin for this long, then runs
# how long each toy guest runs its command (seconds); None = until cancelled
RUN_S = {"alpha": 240, "bravo": 40, "fails": 20, "hangs": None}
OMNI_SCRIPTS = frozenset({OMNI_PUT_SCRIPT, OMNI_INSTALL_SCRIPT, OMNI_HELP_SCRIPT, OMNI_START_SCRIPT, OMNI_ENV_NAMES_SCRIPT, OMNI_STOP_SCRIPT,
                          OMNI_LOGS_SCRIPT, OMNI_COUNT_SCRIPT})
OMNI_HOST_S = 20  # a toy omnigent host registers this long (virtual seconds) after its session was created


def project_name(project: str | None = None) -> str:
    return project or os.environ.get("FAKE_PROJECT", "rooms")


def world_path(project: str | None = None) -> str:
    name = project_name(project)
    return os.path.join(os.environ["FAKE_WORLD"], "world.json" if name == "rooms" else f"world-{name}.json")


def load(project: str | None = None) -> dict:
    try:
        with open(world_path(project), encoding="utf-8") as handle:
            return json.load(handle)
    except FileNotFoundError:
        # ids are unique across projects at a real provider; start the other project's counter elsewhere
        return {"next_id": 1000 if project_name(project) == "rooms" else 5000, "servers": {}, "volumes": {}, "ssh_keys": {},
                "firewalls": {}, "calls": [], "offset": 0.0,
                "fs": {}, "users": [], "installed": False, "setup": False, "config": {}, "floors": {}, "rooms": {},
                "holders": [], "killed": False, "rebooted": False, "never_touch": [], "fail": [], "log": []}


def token_project(token: str) -> str:
    try:
        with open(os.path.join(os.environ["FAKE_WORLD"], "tokens.json"), encoding="utf-8") as handle:
            return json.load(handle).get(token.strip(), "rooms")
    except FileNotFoundError:
        return "rooms"


def save(w: dict, project: str | None = None) -> None:
    # a name no other writer shares (concurrent fake commands used to interleave in one fixed ".tmp"), then an atomic replace
    tmp = f"{world_path(project)}.{os.getpid()}.{os.urandom(4).hex()}.tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as handle:
            json.dump(w, handle)
        os.replace(tmp, world_path(project))
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


_LOCK = {"fd": None, "depth": 0}


def _lock_path() -> str:
    return os.path.join(os.environ["FAKE_WORLD"], ".world.lock")


def _acquire() -> None:
    fd = os.open(_lock_path(), os.O_RDWR | os.O_CREAT, 0o600)
    fcntl.flock(fd, fcntl.LOCK_EX)
    _LOCK["fd"] = fd


def _release() -> None:
    os.close(_LOCK["fd"])  # closing the descriptor drops the lock
    _LOCK["fd"] = None


@contextlib.contextmanager
def world_lock():
    """Hold the world's lock (re-entrant in one process) around a load ... save, so a concurrent fake command cannot drop the update.

    Without it, the harness's fake clock (which advances the world's offset between polls) and a fake ssh that had just stored the
    holder's pid file could overlap, the stale copy would be written last, and the holder would never appear."""
    if _LOCK["depth"] == 0:
        _acquire()
    _LOCK["depth"] += 1
    try:
        yield
    finally:
        _LOCK["depth"] -= 1
        if _LOCK["depth"] == 0:
            _release()


@contextlib.contextmanager
def world_unlocked():
    """Let go of the lock while a command waits for something (the holder's sleep, an interrupted checkin), then take it back.
    Whatever the command holds in memory is stale after the wait; callers load again."""
    depth, _LOCK["depth"] = _LOCK["depth"], 0
    if depth:
        _release()
    try:
        yield
    finally:
        if depth:
            _acquire()
        _LOCK["depth"] = depth


def now(w) -> datetime:
    return datetime.fromtimestamp(time.time() + w["offset"], UTC).replace(microsecond=0)


def iso(dt) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%SZ")


def parse_iso(text) -> datetime:
    return datetime.fromisoformat(text.replace("Z", "+00:00"))


def new_id(w) -> str:
    w["next_id"] += 1
    return str(w["next_id"])


def matches(item_labels: dict, selector: str | None) -> bool:
    if not selector:
        return True
    for part in selector.split(","):
        k, _, v = part.partition("=")
        if item_labels.get(k) != v:
            return False
    return True


# ====================================================================== hcloud
def hcloud(argv: list) -> int:
    w = load()
    w["calls"].append(argv)
    for pattern in w["fail"]:
        if argv[: len(pattern.split())] == pattern.split():
            save(w)
            print(f"fake hcloud: injected failure for {pattern}", file=sys.stderr)
            return 1
    kind, verb, rest = argv[0], argv[1], argv[2:]
    opts, pos, labels, i = {}, [], {}, 0
    while i < len(rest):
        a = rest[i]
        if a == "--label":
            k, _, v = rest[i + 1].partition("=")
            labels[k] = v
            i += 2
        elif a in ("-o", "-l", "--name", "--size", "--server", "--type", "--image", "--location", "--ssh-key", "--public-key-from-file"):
            opts[a] = rest[i + 1]
            i += 2
        else:
            pos.append(a)
            i += 1
    table = {"server": "servers", "volume": "volumes", "ssh-key": "ssh_keys", "firewall": "firewalls"}.get(kind)

    def out(doc) -> int:
        save(w)
        print(json.dumps(doc))
        return 0
    if kind == "primary-ip" and verb == "list":
        # As at Hetzner: each server gets an unlabelled, Hetzner-named IPv4 and IPv6 that live and die with it.
        return out([{"id": pid, "name": f"primary_ip-{pid}", "labels": {}, "type": fam, "assignee_id": int(s["id"]),
                     "assignee_type": "server", "auto_delete": True, "protection": {"delete": False}}
                    for s in w["servers"].values() for fam, pid in (("ipv4", ip_id(s, "ipv4")), ("ipv6", ip_id(s, "ipv6")))
                    if matches({}, opts.get("-l"))])
    if kind in ("floating-ip", "network") and verb == "list":
        return out([])
    if kind == "server-type" and verb == "describe":
        return out({"name": pos[0], "prices": [{"location": "nbg1", "price_hourly": {"gross": "0.0100", "net": "0.0084"}}]})
    if table is None:
        save(w)
        print(f"fake hcloud: unsupported {argv}", file=sys.stderr)
        return 2
    items = w[table]

    def find(ref):
        for item in items.values():
            if item["id"] == ref or item["name"] == ref:
                return item
        return None
    if verb == "list":
        return out([view(w, kind, v) for v in items.values() if matches(v["labels"], opts.get("-l"))])
    if verb == "describe":
        item = find(pos[0])
        if not item:
            save(w)
            print("fake hcloud: not found", file=sys.stderr)
            return 1
        return out(view(w, kind, item))
    if verb == "create":
        item = {"id": new_id(w), "name": opts["--name"], "labels": labels, "created": iso(now(w))}
        if kind == "server":
            if "leak-fw" in w.get("modes", []):  # simulates a resource created by the product under test that nothing cleans up
                w["firewalls"]["900"] = {"id": "900", "name": "cabin-fw", "labels": {}}
            item["ip"] = f"203.0.113.{len(items) + 10}"
            item["type"] = opts.get("--type")
        if kind == "volume":
            item["size"] = int(opts["--size"])
            target = None
            if "--server" in opts:
                target = next((s for s in w["servers"].values() if opts["--server"] in (s["id"], s["name"])), None)
                if target is None:
                    save(w)
                    print("fake hcloud: server not found", file=sys.stderr)
                    return 1
            item["server"] = target["id"] if target else None
        items[item["id"]] = item
        save(w)
        return 0
    if verb == "delete":
        item = find(pos[0])
        if not item:
            save(w)
            print("fake hcloud: not found", file=sys.stderr)
            return 1
        if kind == "volume" and item.get("server"):
            save(w)
            print("fake hcloud: volume is attached", file=sys.stderr)
            return 1
        if kind == "volume" and item.get("protected"):
            save(w)
            print("fake hcloud: volume is protected", file=sys.stderr)
            return 1
        del items[item["id"]]
        if kind == "server":
            for v in w["volumes"].values():
                if v.get("server") == item["id"]:
                    v["server"] = None
        save(w)
        return 0
    if verb == "disable-protection":
        item = find(pos[0])
        item["protected"] = False
        save(w)
        return 0
    save(w)
    return 2


def ip_id(server: dict, family: str) -> int:
    return (700000 if family == "ipv4" else 800000) + int(server["id"])


def view(w, kind, item) -> dict:
    doc = {"id": int(item["id"]) if item["id"].isdigit() else item["id"], "name": item["name"], "labels": item["labels"]}
    if kind == "server":
        doc["public_net"] = {"ipv4": {"id": ip_id(item, "ipv4"), "ip": item["ip"]},
                             "ipv6": {"id": ip_id(item, "ipv6"), "ip": f"2001:db8::{item['id']}"}}
        doc["server_type"] = {"name": item.get("type")}
        doc["created"] = item.get("created")
    if kind == "volume":
        doc.update(size=item["size"], server=int(item["server"]) if item.get("server") else None,
                   protection={"delete": bool(item.get("protected"))}, created=item.get("created"))
    if kind == "firewall":
        doc.update(rules=item.get("rules", []), applied_to=item.get("applied_to", []))
    return doc


# ====================================================================== ssh / scp
def ssh(argv: list) -> int:
    host = next(a for a in argv if re.fullmatch(r"\w+@[\d.]+", a))
    command = argv[argv.index(host) + 1:]
    w = load()
    server = next((s for s in w["servers"].values() if s["ip"] == host.split("@")[1]), None)
    if server is None:
        print("ssh: connect failed", file=sys.stderr)
        return 255
    if w.get("rebooted"):
        w["rebooted"] = False
        save(w)
        print("ssh: connection refused (rebooting)", file=sys.stderr)
        return 255
    stdin = sys.stdin.read() if not sys.stdin.isatty() else ""
    toks = shlex.split(command[0]) if command else []
    user = "root"
    if toks[:2] == ["sudo", "-n"] and "-u" in toks:
        user = toks[toks.index("-u") + 1]
        toks = toks[toks.index("--") + 1:]
    toy = Toy(w, server, user)
    rc, out, err = toy.run(toks, stdin)
    if not toy.nosave:
        save(w)
    sys.stdout.write(out)
    sys.stderr.write(err)
    sys.stdout.flush()
    return rc


def scp(argv: list) -> int:
    dest = argv[-1]
    local = argv[-2]
    m = re.fullmatch(r"\w+@([\d.]+):(.+)", dest)
    w = load()
    with open(local, "rb") as handle:
        data = handle.read()
    try:
        content = data.decode("utf-8")
    except UnicodeDecodeError:
        content = "<binary>"
    entry = w["fs"].get(m.group(2), {"mode": "644", "owner": "root"})
    entry["content"] = content
    w["fs"][m.group(2)] = entry
    save(w)
    return 0


def keygen(argv: list) -> int:
    path = argv[argv.index("-f") + 1]
    with open(path, "w", encoding="utf-8") as handle:
        handle.write("PRIVATE-KEY-FAKE\n")
    with open(path + ".pub", "w", encoding="utf-8") as handle:
        handle.write("ssh-ed25519 AAAAFAKE fake\n")
    return 0


def w_version(w) -> int:
    return w.get("systemd", 255)


# ====================================================================== the toy motel
class Toy:
    def __init__(self, w, server, user):
        self.w, self.server, self.user = w, server, user
        self.sid = server["id"]
        self.nosave = False  # set by commands that block: the state they hold is stale by the time they return

    # -- plumbing
    def cfg(self, key, default):
        return self.w["config"].get(key, default)

    def minutes(self, key, default):
        text = self.w["config"].get(key, default)
        n, unit = int(text[:-1]), text[-1]
        return timedelta(minutes=n * {"m": 1, "h": 60, "d": 1440}[unit])

    def doc(self, ok=True, **kw):
        return json.dumps({"schema": "motel/v1", "ok": ok, **kw}, indent=1)

    def fail(self, code, msg):
        return code, self.doc(False, error=msg, hint="motel ls --mine", code=code), ""

    def vols(self):
        return [v for v in self.w["volumes"].values() if v["labels"].get("managed-by") == "motel"]

    def readable(self, path, who):
        entry = self.w["fs"].get(path)
        if entry is None:
            return False
        if who == "root":
            return True
        if path == TOKEN or "/.lost-and-found/" in path:
            return False
        return entry.get("owner") == who or entry.get("mode") == "644"

    # -- dispatch
    def run(self, t: list, stdin: str):
        w = self.w
        if not t:
            return 0, "", ""
        c = t[0]
        if t == ["true"] or c in ("cloud-init", "chmod", "chown"):
            return 0, "", ""
        if c == "useradd":
            w["users"].append(t[-1])
            return 0, "", ""
        if c in ("id", "getent", "date", "systemd-analyze", "systemd-run") or (c == "systemctl" and self.systemctl_handles(t)):
            return self.system(t)
        if c == "env":
            rest = t[1:]
            while rest and (rest[0] == "-i" or re.fullmatch(r"[A-Za-z_]\w*=.*", rest[0])):
                rest = rest[1:]
            return self.run(rest, stdin)
        if c in ("mkdir", "cp"):
            return 0, "", ""
        if c.endswith("python3") and t[1:2] == ["-c"]:
            return self.omni_python(t[2], t[3:], stdin)
        if c.endswith("python3") and t[1:3] == ["-P", "-c"]:
            return self.omni_python(t[3], t[4:], stdin)
        if c.endswith("/venv/bin/pip"):
            return self.omni_pip()
        if c == "git" and t[1:2] == ["-C"]:
            return self.repo_git(t[2:])
        if c == "tar":
            return 0, "", ""
        if c == "git":
            return 0, "", ""
        if t[:2] == ["sh", "-c"]:
            return self.shell(t, stdin)
        if c == "sh" and t[1].endswith("install.sh"):
            w["installed"], w["timer"] = True, True
            if "--tours" in t:
                return self.install_tours(t)
            w["config"] = {"allow_users": t[t.index("--users") + 1]}
            return 0, "installed.\n", ""
        if c == "systemctl":
            if t[1] == "is-enabled":
                return 0, ("enabled\n" if w.get("timer") else "disabled\n"), ""
            if t[1:3] == ["disable", "--now"]:
                w["timer"] = False
            if t[1] == "reboot":
                self.reboot()
            return 0, "", ""
        if c == "rm":
            w["fs"].pop(t[-1], None)
            return 0, "", ""
        if c == "test":
            return (0 if t[-1] in w["fs"] or t[-1] in self.dirs() else 1), "", ""
        if c == "cat":
            entry = w["fs"].get(t[1])
            if entry is None or not self.readable(t[1], self.user):
                return 1, "", f"cat: {t[1]}: Permission denied or missing\n"
            return 0, entry["content"], ""
        if c == "stat":
            path = t[-1]
            if path.startswith(MOUNT + "/") and path in self.dirs():
                owner = self.w["dirs"][path]["owner"] if "dirs" in w and path in w["dirs"] else "root"
                return 0, f"{owner} 700\n", ""
            entry = w["fs"].get(path)
            if entry:
                return 0, f"{entry['owner']}:{entry['owner']} {entry['mode']}\n", ""
            return 1, "", ""
        if c == "grep":
            return 1, "0\n", ""
        if c == "df":
            floor = next((f for f in w["floors"].values() if t[-1] == f"{MOUNT}/{f['id']}"), None)
            if floor and floor.get("mounted"):
                return 0, f"1G-blocks\n{floor['size']}\n", ""
            return 1, "", "df: no such file\n"
        if c == "findmnt":
            floor = next((f for f in w["floors"].values() if t[-1] == f"{MOUNT}/{f['id']}"), None)
            if floor and floor.get("mounted"):
                return 0, "rw,nosuid,nodev,noatime,discard\n", ""
            return 1, "", ""
        if c == "cmp":
            a, b = self.w["fs"].get(t[-2]), self.w["fs"].get(t[-1])
            if a is None or b is None:
                return 2, "", "cmp: no such file\n"
            return (0 if a["content"] == b["content"] else 1), "", ""
        if c == "pkill":
            w["killed"] = True
            return 0, "", ""
        if c == "ls":
            return 1 if self.user != "root" else 0, "", ""
        if c == "systemctl":
            return 0, "", ""
        if c == "journalctl":
            return 0, ("2026-11-11T03:17:41+0000 fake motel-tour-t-1[1]: supervising\n"
                       "2026-11-11T03:17:42+0000 fake motel-concierge[2]: upstream key sk-ant-LEAKY1234567890abcdef\n"
                       "-----BEGIN OPENSSH PRIVATE KEY-----\nAAAAB3NzaC1\n-----END OPENSSH PRIVATE KEY-----\n"), ""
        if c in ("/usr/local/bin/motel", "motel"):
            return self.motel(t[1:])
        if c == "/usr/local/sbin/motel-desk" or c == "sudo":
            if self.user != "root":
                return 1, "", "sudo: a password is required\n"
            return self.desk(t[1:] if c != "sudo" else t[3:], stdin)
        return 127, "", f"fake ssh: unsupported {t}\n"

    def dirs(self):
        return self.w.setdefault("dirs", {})

    def shell(self, t, stdin):
        w, script = self.w, t[2]
        args = t[4:]
        if script == PUT_SCRIPT:
            w["fs"][args[0]] = {"content": stdin, "owner": "root", "mode": args[1][-3:]}
            if args[0] == "/etc/motel/motel.toml":
                self.parse_config(stdin)
            return 0, "", ""
        if script == PREP_TOKEN_SCRIPT:
            w["fs"][args[0]] = {"content": "", "owner": "root", "mode": "600"}
            return 0, "", ""
        if script == WRITE_SCRIPT:
            d = args[0].rsplit("/", 1)[0]
            if d not in self.dirs() or self.dirs()[d]["owner"] != self.user or not self.mounted_for(d):
                return 1, "", "sh: cannot create file\n"
            w["fs"][args[0]] = {"content": args[1], "owner": self.user, "mode": "600"}
            return 0, "", ""
        if script == HOLD_SCRIPT:
            w["fs"][args[1]] = {"content": "4242\n", "owner": self.user, "mode": "644"}
            w["holders"].append(args[0])
            save(w)
            self.nosave = True
            with world_unlocked():     # the holder runs for as long as the test needs it; other fake commands must not wait for it
                end = time.time() + 20
                while time.time() < end:
                    time.sleep(0.1)
                    if not load()["holders"]:
                        break
            return 0, "", ""
        if script == GIT_PREP_SCRIPT:
            return 0, "", ""
        if script == TOUR_REPO_SCRIPT:
            w["repo"] = {"path": args[0], "branches": {}}
            return 0, "", ""
        if script == ENV_SCRIPT:
            return self.concierge_env()
        if script == SHA_SCRIPT:
            return self.stored_bundle(args[0])
        if script == SAMPLER_SCRIPT:
            return 0, "", ""
        if script == CD_SCRIPT:  # cd DIR && exec CMD...
            return self.run(args[1:], stdin)
        if script == REACH_SCRIPT:  # TCP connect to <ip>:22 from THIS server: works only if a cabin firewall admits this server
            return self.reach(args[0])
        if script == REACH_BANNER_SCRIPT:
            rc, out, err = self.reach(args[0])
            return rc, ("SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.5\n" if rc == 0 else ""), err
        if script == ATTEMPTS_SCRIPT:  # the attempt records and events of one tour, as root
            tid = args[0].rsplit("/", 1)[-1]
            tour = w.get("tours", {}).get(tid)
            if tour is None:
                return 0, "", ""
            return 0, "".join(f"== attempts/a-{i}.json\n" + json.dumps(self.attempt_record(tour, g), indent=1, sort_keys=True) + "\n"
                              for i, g in enumerate(tour["guests"], 1)) + "== events.jsonl\n", ""
        if script.startswith(("DEBIAN_FRONTEND", "export DEBIAN_FRONTEND")):
            return 0, "", ""
        if script in OMNI_SCRIPTS:
            return self.omni_shell(script, args, stdin)
        if script == KILL_SCRIPT:
            w["holders"] = []
            w["fs"].pop(args[0], None)
            return 0, "", ""
        if "wc -l" in script:
            n = len([f for f in w["floors"].values() if f.get("mounted")])
            return 0, f"{n}\n", ""
        return 127, "", f"fake ssh: unsupported shell {script}\n"

    def mounted_for(self, d):
        return any(d.startswith(f"{MOUNT}/{f['id']}/") and f.get("mounted") for f in self.w["floors"].values())

    def reboot(self):
        self.w["rebooted"] = True
        for f in self.w["floors"].values():
            f["mounted"] = False

    def parse_config(self, text):
        cfg = self.w["config"]
        for key in ("min_floor_gb", "max_floors", "max_floor_gb", "max_total_gb"):
            m = re.search(rf"^{key}\s*=\s*(\d+)", text, re.M)
            if m:
                cfg[key] = int(m.group(1))
        for key in ("grace", "lost_and_found", "retire_after", "warn_before", "max_ttl_call", "idle_timeout"):
            m = re.search(rf'^{key}\s*=\s*"(\w+)"', text, re.M)
            if m:
                cfg[key] = m.group(1)
        self.w["cabins"] = "[cabins]" in text
        self.w["tours_cfg"] = "[tours]" in text
        m = re.search(r'^token_file\s*=\s*"([^"]+)"', text[text.find("[cabins]"):] if "[cabins]" in text else "", re.M)
        cfg["cabin_token_file"] = m.group(1) if m else CABINS_TOKEN

    # -- the desk (root)
    def desk(self, t, stdin):
        verb = t[0]
        if verb == "setup":
            token = self.w["fs"].get(t[t.index("--token-file") + 1])
            if token is None or len(token["content"].strip()) < 16:
                return 2, self.doc(False, error="that does not look like an API token", code=2), ""
            if "leak" in self.w.get("modes", []):
                sys.stderr.write(f"debug: token is {token['content'].strip()}\n")
            keep = set(self.w["never_touch"])
            for v in self.w["volumes"].values():
                if v["labels"].get("managed-by") != "motel":
                    keep.add(v["id"])
            self.w["never_touch"] = sorted(keep)
            self.w["setup"] = True
            self.w["fs"][TOKEN] = {"content": token["content"], "owner": "root", "mode": "600"}
            self.w["fs"]["/etc/motel/setup-receipt.json"] = {
                "content": json.dumps({"provider": "hetzner", "server_id": self.sid, "location": "nbg1",
                                       "never_touch": [{"id": i} for i in self.w["never_touch"]]}), "owner": "root", "mode": "644"}
            return 0, self.doc(provider="hetzner", server_id=self.sid, location="nbg1", never_touch=len(keep)), ""
        if verb == "cabin":
            if "interactive-cabin-setup" in self.w.get("modes", []):  # a ref whose cabin setup is interactive-only
                return 2, self.doc(False, error="setup reads the token only from an interactive terminal", code=2), ""
            return self.cabin_desk(t[1:])
        if verb == "reap":
            return self.reap("--apply" in t)
        if verb == "tour":
            return self.tour_cli(t[1:], root=True)
        return 127, "", "unsupported desk verb\n"

    # -- cabins: disposable servers in the CABINS project (a second world); the lease records live on this host (the rooms world)
    def project_of_cabin_token(self):
        entry = self.w["fs"].get(self.cfg("cabin_token_file", CABINS_TOKEN))
        return token_project(entry["content"]) if entry else None

    def cabin_labels(self, lease_id):
        return {"managed-by": "motel", "role": "cabin", "controller": self.sid, "lease": lease_id}

    def cabin_world(self):
        """(world, save) of the project cabins are created in. 'misroute' simulates cabins landing in the rooms project."""
        if "misroute" in self.w.get("modes", []):
            return self.w, lambda: None
        cw = load("cabins")
        return cw, lambda: save(cw, "cabins")

    def cabin_desk(self, t):
        w, verb = self.w, t[0]
        if verb == "setup":
            if t[1:2] != ["hetzner"] or "--token-file" not in t:
                return 2, self.doc(False, error="setup reads the token only from an interactive terminal", code=2), ""
            entry = w["fs"].get(t[t.index("--token-file") + 1])
            if entry is None or len(entry["content"].strip()) < 16:
                return 2, self.doc(False, error="that does not look like an API token", code=2), ""
            if entry["owner"] != "root" or entry["mode"] != "600":
                return 2, self.doc(False, error="the token file must be root-owned, mode 0600", code=2), ""
            token = entry["content"].strip()
            project = token_project(token)
            pw = w if project == "rooms" else load(project)
            # motel's rule: the project holds only motel cabins of this controller and cannot see this host
            for table in ("servers", "volumes", "firewalls"):
                for item in pw[table].values():
                    labels = item["labels"]
                    if (item["id"] == self.sid or labels.get("managed-by") != "motel" or labels.get("role") != "cabin"
                            or labels.get("controller") != self.sid or not item["name"].startswith("motel-")):
                        return 6, self.doc(False, error="refused, nothing changed: dedicated cabin project required: the project holds this "
                                           f"host or a {table[:-1]} that motel did not create", code=6), ""
            path = self.cfg("cabin_token_file", CABINS_TOKEN)
            w["fs"][path] = {"content": token + "\n", "owner": "root", "mode": "600"}
            w["cabin_setup"] = True
            return 0, self.doc(host_server_id=self.sid, public_ipv4=self.server["ip"], project_servers=len(pw["servers"])), ""
        if not w.get("cabin_setup"):
            return self.fail(5, "cabins are not set up")
        if verb == "doctor":
            return 0, self.doc(checks=[{"check": "cabin project dedicated", "level": "ok", "ok": True, "detail": ""}]), ""
        if verb == "reap":
            return self.cabin_reap("--apply" in t)
        return 127, "", "unsupported cabin desk verb\n"

    def make_cabin(self, task, kind, ttl, disk):
        w = self.w
        cw, commit = self.cabin_world()
        lease_id = f"{task}-{new_id(cw)[-6:]}"
        sid = new_id(cw)
        cw["servers"][sid] = {"id": sid, "name": f"motel-{lease_id}", "labels": self.cabin_labels(lease_id), "type": kind,
                              "ip": f"192.0.2.{len(cw['servers']) + 10}", "created": iso(now(w))}
        fid = new_id(cw)
        sources = [f"{self.server['ip']}/32"]
        cw["firewalls"][fid] = {"id": fid, "name": f"motel-{lease_id}", "labels": self.cabin_labels(lease_id),
                                "rules": [{"direction": "in", "protocol": "tcp", "port": "22", "source_ips": sources}],
                                "applied_to": [{"type": "server", "server": {"id": int(sid)}}]}
        vid = None
        if disk:
            vid = new_id(cw)
            cw["volumes"][vid] = {"id": vid, "name": f"motel-{lease_id}", "labels": self.cabin_labels(lease_id), "size": 10, "server": sid}
        commit()
        created = now(w)
        w.setdefault("leases", {})[lease_id] = {"lease_id": lease_id, "server_id": sid, "firewall_id": fid, "volume_id": vid,
                                                "created": iso(created), "expires": iso(created + ttl), "task": task,
                                                "address": cw["servers"][sid]["ip"], "sources": sources}
        return w["leases"][lease_id]

    def drop_cabin(self, lease_id, leak_firewall=False):
        w = self.w
        lease = w["leases"].pop(lease_id)
        w.setdefault("released_leases", {})[lease_id] = lease   # the real `cabin ls` keeps released cabins as history
        cw, commit = self.cabin_world()
        cw["servers"].pop(lease["server_id"], None)
        if lease["volume_id"]:
            cw["volumes"].pop(lease["volume_id"], None)
        if not (leak_firewall and "leak-cabin-firewall" in w.get("modes", [])):  # the fault: a remnant the product forgot
            cw["firewalls"].pop(lease["firewall_id"], None)
        commit()

    def cabin_view(self, lease, state="active"):
        return {"lease_id": lease["lease_id"], "state": state if state != "active" else lease.get("state", "active"),
                "server_id": lease["server_id"], "firewall_id": lease["firewall_id"],
                "volume_id": lease["volume_id"], "address": lease["address"], "access": {"mode": "ssh-allowlist", "sources": lease["sources"]},
                "expires": lease["expires"], "expires_at": lease["expires"], "task": lease["task"],
                "time_left_s": int((parse_iso(lease["expires"]) - now(self.w)).total_seconds())}

    def cabin_cli(self, t):
        w, verb = self.w, t[0]
        if not w.get("cabin_setup"):
            return self.fail(8, "cabins are not set up (motel-desk cabin setup hetzner)")
        w.setdefault("leases", {})
        if verb == "run":
            sep = t.index("--")
            cmd = t[sep + 1:]
            lease = self.make_cabin("run", t[t.index("--type") + 1], timedelta(hours=1), False)
            try:
                if cmd[:2] == ["sh", "-c"] and re.fullmatch(r"exit \d+", cmd[2]):
                    return int(cmd[2].split()[1]), "", ""
                return 0, "Linux motel-cabin 6.8.0 x86_64 GNU/Linux\n", ""
            finally:
                self.drop_cabin(lease["lease_id"])
        if verb == "checkin":
            ttl = self.dur(t[t.index("--for") + 1]) if "--for" in t else timedelta(hours=2)
            task = t[t.index("--task") + 1] if "--task" in t else "cabin"
            lease = self.make_cabin(task, t[t.index("--type") + 1], ttl, "--disk" in t)
            return 0, json.dumps({"schema": "motel/v1", "ok": True, **self.cabin_view(lease)}), ""
        if verb == "ls":
            rows = [self.cabin_view(v) for v in w["leases"].values()]
            rows += [self.cabin_view(v, "released") for v in w.get("released_leases", {}).values()]
            return 0, self.doc(cabins=rows, active_or_pending=list(w["leases"])), ""
        if verb == "checkout":
            if t[1] not in w["leases"]:
                return self.fail(9, f"no cabin {t[1]}")
            self.drop_cabin(t[1], leak_firewall=True)
            return 0, self.doc(), ""
        if verb == "status":
            if t[1] in w["leases"]:
                return 0, self.doc(cabin=self.cabin_view(w["leases"][t[1]])), ""
            if t[1] in w.get("released_leases", {}):
                return 0, self.doc(cabin=self.cabin_view(w["released_leases"][t[1]], "released")), ""
            return self.fail(7, f"no cabin {t[1]}")
        if verb == "extend":
            lease = w["leases"].get(t[1])
            if lease is None:
                return self.fail(7, f"no cabin {t[1]}")
            new_end = now(w) + self.dur(t[t.index("--for") + 1])
            if new_end - parse_iso(lease["expires"]) < timedelta(minutes=5):
                return self.fail(3, f"the cabin already runs until {lease['expires']}")
            lease["expires"] = iso(new_end)
            return 0, self.doc(cabin=self.cabin_view(lease)), ""
        if verb == "ssh":
            lease = w["leases"].get(t[1])
            if lease is None:
                return self.fail(7, f"no cabin {t[1]}")
            cmd = t[t.index("--") + 1:]
            if cmd[:2] == ["sh", "-c"] and cmd[2] == OMNI_CABIN_LOGS_SCRIPT:
                return 0, ("== /tmp/omnigent-host.log\nconnecting to http://127.0.0.1:41000\n"
                           "OMNIGENT_HOST_LAUNCH_TOKEN=launchsecret12345 host started\n"), ""  # gitleaks:allow (fake test value)
            return 0, "", ""
        return 2, "", "usage\n"

    def release_due(self, created, n):
        """Hourly billing, exactly as the product decides it (motel.billing.release_window): a resource that could be released is
        kept until the release window of the billed hour it is in, the last 10 minutes before `release_margin` (default 6m)
        ahead of the next billed hour, and released earlier only when the next pass (10 minutes, plus a minute of slack) would
        come after the window closed. So from minute 43 for the default margin."""
        elapsed = (n - parse_iso(created)).total_seconds()
        hour = int(elapsed // 3600) + 1
        closes = hour * 3600 - self.minutes("release_margin", "6m").total_seconds()
        opens = closes - 600
        return elapsed >= opens or elapsed + 600 + 60 > closes

    def idle_release_due(self, lease, n):
        """An idle cabin is sealed but deleted only in its release window. The fault mode deletes it the moment it is idle."""
        if "cabin-idle-delete-at-once" in self.w.get("modes", []):
            return True
        return self.release_due(lease["created"], n)

    def cabin_reap(self, apply):
        w, n, actions = self.w, now(self.w), []
        idle = self.minutes("idle_timeout", "30m")
        for lease_id, lease in list(w.get("leases", {}).items()):
            if n >= parse_iso(lease["expires"]):
                actions.append(f"cabin {lease_id} passed its TTL: hard delete")
            elif n >= parse_iso(lease["created"]) + idle and self.idle_release_due(lease, n):
                actions.append(f"cabin {lease_id} idle for {idle}: torn down")
            else:
                continue
            if apply:
                self.drop_cabin(lease_id)
        return 0, self.doc(apply=apply, actions=actions, warnings=[], errors=[]), ""

    def reach(self, ip):
        """A TCP connect to a cabin's port 22 succeeds from the test server only: the firewall admits just that server's address."""
        for cw in (load("cabins"), self.w):
            for fw in cw["firewalls"].values():
                server_ids = [str(a["server"]["id"]) for a in fw.get("applied_to", [])]
                if any(cw["servers"].get(sid, {}).get("ip") == ip for sid in server_ids) and \
                        any(f"{self.server['ip']}/32" in r["source_ips"] for r in fw.get("rules", [])):
                    return 0, "", ""
        return 1, "", "connect failed\n"

    # -- the omnigent phase: a toy `omnigent server` on the test server, its API, the provider's launcher in a second process
    def omni(self):
        return self.w.setdefault("omni", {"installed": False, "server": None, "sessions": {}, "ports": 0})

    def omni_shell(self, script, args, stdin):
        om, fs = self.omni(), self.w["fs"]
        if script == OMNI_PUT_SCRIPT:
            fs[args[0]] = {"content": stdin, "owner": self.user, "mode": "644"}
            return 0, "", ""
        if script == OMNI_INSTALL_SCRIPT:
            if "omni-install-fails" in self.modes():
                return 1, "", "ERROR: Could not find a version that satisfies the requirement omnigent==" + args[1] + "\n"
            om.update(installed=True, version=args[1], root=args[0])
            return 0, "", ""
        if script == OMNI_HELP_SCRIPT:
            if not om["installed"]:
                return 127, "sh: omnigent: not found\n", ""
            providers = "modal|daytona|e2b" + ("" if "omni-no-provider" in self.modes() else "|motel")
            return 0, f"Usage: omnigent sandbox create [OPTIONS]\n\n  --provider [{providers}]  Sandbox provider to use.\n", ""
        if script == OMNI_START_SCRIPT:
            if f"{args[0]}/config.yaml" not in fs or not om["installed"]:
                return 1, "", "no config or no venv\n"
            om["ports"] += 1
            om["server"] = {"pid": 5000 + om["ports"], "port": int(args[1]), "root": args[0], "running": True, "home": f"{args[0]}/home"}
            om["start_script"] = script
            fs[f"{args[0]}/server.pid"] = {"content": f"{om['server']['pid']}\n", "owner": self.user, "mode": "644"}
            return 0, f"{om['server']['pid']}\n", ""
        if script == OMNI_ENV_NAMES_SCRIPT:
            names = ["HOME", "PATH", "LANG", "PYTHONUNBUFFERED", "OMNIGENT_HARNESS_TMP_PARENT"]
            return 0, "\n".join(names + (["ANTHROPIC_API_KEY"] if "omni-key-in-env" in self.modes() else [])) + "\n", ""
        if script == OMNI_STOP_SCRIPT:
            srv = om["server"]
            if not srv:
                return 0, "no-record\n", ""
            if "omni-server-sticks" in self.modes():
                return 1, f"still-running {srv['pid']}\n", ""
            srv["running"] = False
            return 0, f"stopped {srv['pid']}\n", ""
        if script == OMNI_LOGS_SCRIPT:
            return 0, (f"== {args[0]}/server.log\nINFO managed launch for session\n"
                       "Authorization: Bearer launchbearer1234567\nOMNIGENT_HOST_LAUNCH_TOKEN=launchsecret12345\n"), ""  # gitleaks:allow (fake test value)
        if script == OMNI_COUNT_SCRIPT:
            return 0, "1\n", ""
        return 127, "", f"fake ssh: unsupported omnigent script {script}\n"

    def omni_pip(self):
        om = self.omni()
        if not om["installed"]:
            return 1, "", "WARNING: Package(s) not found\n"
        return 0, f"Name: omnigent\nVersion: {om['version']}\nName: motel\nVersion: 0.1.0.dev0\n", ""

    def omni_python(self, script, args, stdin):
        om = self.omni()
        if script == OMNI_PORT_PY:
            return 0, f"{41000 + om['ports']}\n", ""
        if script == OMNI_API_PY:
            return self.omni_api(args[0], args[1], stdin)
        if script == OMNI_PROVIDER_PY:
            return self.omni_provider(*args)
        return 127, "", "fake ssh: unsupported python\n"

    def omni_provider(self, op, motel, ttl, cabin, command=""):
        lease = self.w.setdefault("leases", {}).get(cabin)
        if lease is None:
            return 1, "", f"MotelSandboxError: cabin {cabin} does not exist\n"
        if op == "run":
            m = re.match(r"echo (\S+?);", command)
            return 0, json.dumps({"rc": 0, "stdout": f"{m.group(1) if m else ''}\nLinux\n", "stderr": ""}) + "\n", ""
        if "omni-keepalive-noop" not in self.modes():
            end = now(self.w) + self.dur(ttl)
            if end > parse_iso(lease["expires"]):
                lease["expires"] = iso(end)
        return 0, json.dumps({"result": True}) + "\n", ""

    def omni_out(self, status, doc):
        return 0, f"{status}\n{json.dumps(doc) if not isinstance(doc, str) else doc}\n", ""

    def omni_session_age(self, sess):
        return (now(self.w) - parse_iso(sess["created"])).total_seconds()

    def omni_rollback(self, sess):
        for lease_id in sess["cabins"]:
            if lease_id in self.w.get("leases", {}):
                self.drop_cabin(lease_id)

    def omni_api(self, method, url, stdin):
        om, w = self.omni(), self.w
        srv = om["server"]
        m = re.fullmatch(r"http://127\.0\.0\.1:(\d+)(/.*)", url)
        if not srv or not srv["running"] or not m or int(m.group(1)) != srv["port"]:
            return self.omni_out(0, "URLError: <urlopen error [Errno 111] Connection refused>")
        path = m.group(2)
        body = json.loads(stdin) if stdin.strip() else {}
        sessions = om["sessions"]
        if (method, path) == ("GET", "/health"):
            return self.omni_out(200, {"status": "ok"})
        if (method, path) == ("GET", "/v1/info"):
            return self.omni_out(200, {"server_version": om["version"],
                                       "sandbox_providers": [] if "omni-no-provider" in self.modes() else ["motel"]})
        if path.startswith("/v1/agents"):
            return self.omni_out(200, {"data": [{"id": "ag_claude", "name": "assistant"}, {"id": "ag_polly", "name": "polly"}]})
        if (method, path) == ("POST", "/v1/sessions"):
            if "omni-session-rejected" in self.modes() or body.get("host_type") != "managed" or body.get("sandbox_provider") != "motel":
                return self.omni_out(422, {"detail": "invalid session"})
            ttl = re.search(r"ttl: (\d+[mh])", w["fs"][f"{srv['root']}/config.yaml"]["content"]).group(1)
            sid = f"conv_{len(sessions) + 1:04d}"
            count = 2 if "omni-extra-cabin" in self.modes() else 1
            cabins = [self.make_cabin(f"managed-{sid[-4:]}-{i}", "cx23", self.dur(ttl), False)["lease_id"] for i in range(count)]
            om.setdefault("agents_used", []).append(body.get("agent_id"))
            sessions[sid] = {"id": sid, "created": iso(now(w)), "cabins": cabins, "agent": body.get("agent_id")}
            return self.omni_out(201, {"id": sid, "host_id": None, "sandbox_status": {"stage": "provisioning", "provider": "motel"}})
        m = re.fullmatch(r"/v1/sessions/(conv_\d+)(/.*)?", path)
        sess = sessions.get(m.group(1)) if m else None
        if (method, path) == ("GET", "/v1/hosts"):
            hosts = []
            for s in sessions.values():
                if self.omni_session_age(s) >= OMNI_HOST_S and "omni-host-never" not in self.modes() and "omni-launch-failed" not in self.modes():
                    hosts.append({"host_id": f"host_{s['id'][-4:]}", "name": f"managed-{s['id'][-4:]}", "status": "online",
                                  "sandbox_provider": "motel"})
            return self.omni_out(200, {"hosts": hosts})
        if m is None or sess is None:
            return self.omni_out(404, {"detail": "not found"})
        rest = m.group(2) or ""
        age = self.omni_session_age(sess)
        if method == "GET" and rest == "":
            if "omni-launch-failed" in self.modes() and age >= OMNI_HOST_S:
                self.omni_rollback(sess)       # the server tears its own sandbox down when the launch fails
                return self.omni_out(200, {"id": sess["id"], "sandbox_status": {
                    "stage": "failed", "error": "managed sandbox launch failed: the host did not register within 120s"}})
            stage = "provisioning" if age < 8 else "starting" if (age < OMNI_HOST_S or "omni-host-never" in self.modes()) else None
            doc = {"id": sess["id"], "host_id": None if stage else f"host_{sess['id'][-4:]}"}
            if stage:
                doc["sandbox_status"] = {"stage": stage, "provider": "motel"}
            return self.omni_out(200, doc)
        if method == "GET" and rest == "/resources/environments":
            return self.omni_out(200, {"data": [{"id": "default"}]})
        if method == "POST" and rest.endswith("/shell"):
            if "omni-no-runner" in self.modes() or age < OMNI_HOST_S:
                return self.omni_out(502, {"detail": "no runner bound to the session"})
            m2 = re.match(r"echo (\S+)", body.get("command", ""))
            return self.omni_out(200, {"stdout": (m2.group(1) if m2 else "") + "\n", "stderr": "", "exit_code": 0})
        if method == "DELETE" and rest == "":
            if "omni-terminate-leaks" not in self.modes():
                self.omni_rollback(sess)
            del sessions[sess["id"]]
            return self.omni_out(200, {"deleted": True})
        return self.omni_out(404, {"detail": f"unsupported {method} {path}"})


    # -- system tools used by the tours phase (systemd, users, the clock)
    def modes(self):
        return self.w.get("modes", [])

    def concierge(self):
        return self.w.setdefault("concierge", {"pid": 4100, "restarts": []})

    def systemctl_handles(self, t):
        return len(t) > 1 and t[1] in ("is-active", "--version", "show", "restart", "stop")

    def system(self, t):
        w, c = self.w, t[0]
        inspect = w.get("tours_installed")
        if c == "id":
            return (0, "994\n", "") if t[-1] == "motel-inspect" and inspect else (1, "", f"id: '{t[-1]}': no such user\n")
        if c == "getent":
            if t[-1] == "motel-inspect" and inspect:
                return 0, "motel-inspect:x:994:994::/var/lib/motel-inspect:/usr/sbin/nologin\n", ""
            return 2, "", ""
        if c == "date":
            return 0, f"{int(now(w).timestamp())}\n", ""
        if c == "systemd-analyze":
            if "bad-unit" in self.modes():
                return 1, "", "/etc/systemd/system/motel-concierge.service:12: Unknown key 'FileDescriptorStoreMaxx' in section [Service], ignoring.\n"
            missing = [u for u in t[2:] if u not in w["fs"]]
            return (1, "", f"Failed to load {missing[0]}: No such file or directory\n") if missing else (0, "", "")
        if c == "systemd-run":
            w["sampler"] = {"file": t[-2]}
            w["fs"][t[-2]] = {"content": "", "owner": "root", "mode": "644"}
            return 0, "", ""
        version = w_version(w)
        verb = t[1]
        if verb == "--version":
            return 0, f"systemd {version} ({version}.4-1ubuntu8)\n+PAM +AUDIT +SELINUX\n", ""
        if verb == "is-active":
            return 0, "active\n" * len(t[2:]), ""
        if verb == "stop":
            if "sampler" in t[-1]:
                w.pop("sampler", None)
            return 0, "", ""
        if verb == "restart":
            conc = self.concierge()
            conc["pid"] += 7
            conc["restarts"].append(now(w).timestamp())
            return 0, "", ""
        props = {"MainPID": str(self.concierge()["pid"]),
                 "FileDescriptorStoreMax": "0" if "no-fdstore" in self.modes() else "512",
                 "FileDescriptorStorePreserve": "yes" if version >= 254 else "no",
                 "NFileDescriptorStore": str(self.stored_listeners())}
        wanted = [t[i + 1] for i, a in enumerate(t) if a == "-p"]
        return 0, "".join(f"{k}={props.get(k, '')}\n" for k in wanted), ""

    def install_tours(self, t):
        w = self.w
        w["config"] = {**w["config"], "allow_users": t[t.index("--users") + 1]}  # an upgrade keeps the config
        w["tours_installed"] = True
        w["fs"][INSPECT_DIR] = {"content": "", "owner": "root", "mode": "755" if "bad-inspect-dir" in self.modes() else "711"}
        for unit in UNIT_FILES:
            w["fs"][unit] = {"content": "[Service]\n", "owner": "root", "mode": "644"}
        return 0, "installed.\n", ""

    def concierge_env(self):
        env = ["MOTEL_FDSTORE_MAX=512"]
        if w_version(self.w) >= 254:
            env += ["FDSTORE=512", "MOTEL_FDSTORE_SIGNAL=1"]
        if "no-fdstore" in self.modes():
            env = []
        return (0, "\n".join(env) + "\n", "") if env else (1, "", "")

    def stored_listeners(self):
        """Guests whose listener socket systemd holds for the concierge right now."""
        if "no-fdstore" in self.modes():
            return 0
        lost = "listener-lost" in self.modes() and self.concierge()["restarts"]
        return 0 if lost else sum(1 for tour in self.w.get("tours", {}).values() for g in tour["guests"]
                                  if g["secrets"] and g["status"] == "running")

    def stored_bundle(self, folder):
        m = re.fullmatch(rf"{TOURS_ROOT}/(t-\w+)/results/(\w+)", folder)
        tour = self.w.get("tours", {}).get(m.group(1)) if m else None
        g = next((x for x in tour["guests"] if x["id"] == m.group(2)), None) if tour else None
        if g is None or not g.get("bundle"):
            return 1, "", "sha256sum: no such file\n"
        sha = g["bundle"]["sha256"] if "bundle-mismatch" not in self.modes() else "0" * 64
        return 0, f"{sha}  {folder}/a-1/result.bundle\n{g['bundle']['bytes']}\n", ""

    def repo_git(self, t):
        repo = self.w.get("repo")
        if not repo or t[0] != repo["path"]:
            return 128, "", "fatal: not a git repository\n"
        rest = t[1:]
        if rest[:2] == ["rev-parse", "--abbrev-ref"]:
            return 0, "main\n", ""
        branches = repo["branches"]
        if rest[:2] == ["rev-parse", "--verify"]:
            b = branches.get(rest[2])
            return (0, b["tip"] + "\n", "") if b else (128, "", "fatal: needed a single revision\n")
        if rest[0] == "show":
            branch, _, name = rest[1].partition(":")
            content = branches.get(branch, {}).get("files", {}).get(name)
            return (0, content, "") if content is not None else (128, "", "fatal: path not in the branch\n")
        if rest[:2] == ["diff", "--name-only"]:
            b = branches.get(rest[2].partition("..")[2])
            return (0, "".join(f"{n}\n" for n in b["changed"]), "") if b else (128, "", "fatal: bad revision\n")
        return 128, "", f"fake git: unsupported {rest}\n"

    # -- tours: a stand-in for `motel tour ...`. Guests follow a timeline in the world's virtual time, not real work.
    TERMINAL = ("done", "failed", "cancelled", "timed_out", "over_budget")

    def tour_cli(self, t, root=False):
        w = self.w
        if not w.get("tours_installed") or not w.get("tours_cfg"):
            return self.fail(8, "tours are not set up (install.sh --tours and [tours] in the config)")
        verb, tours = t[0], w.setdefault("tours", {})
        if verb == "start":
            return self.tour_start(t[1])
        if verb in ("ls", "reap", "supervise"):
            for tour in tours.values():
                self.tour_tick(tour)
            if verb == "ls":
                rows = [self.tour_doc(x) for x in tours.values() if root or x["owner"] == self.user]
                return 0, self.doc(tours=rows), ""
            return 0, self.doc(apply="--apply" in t, actions=[]), ""
        tour = tours.get(t[1]) if len(t) > 1 else None
        if tour is None or not (root or tour["owner"] == self.user):
            return self.fail(7, f"no tour {t[1] if len(t) > 1 else ''}")
        self.tour_tick(tour)
        if verb == "status":
            return 0, self.doc(tour=self.tour_doc(tour, status=True)), ""
        if verb == "wait":
            done = all(g["status"] in self.TERMINAL for g in tour["guests"])
            return (0 if done else 9), self.doc(satisfied=done, tour=self.tour_doc(tour)), ""
        if verb == "cancel":
            if all(g["status"] in self.TERMINAL for g in tour["guests"]):
                return 0, self.doc(already_finished=True, tour=self.tour_doc(tour)), ""
            tour["cancel"] = True
            self.tour_tick(tour)
            return 0, self.doc(cancelling=True, tour=self.tour_doc(tour)), ""
        g = next((x for x in tour["guests"] if x["id"] == (t[2] if len(t) > 2 else "")), None)
        if g is None:
            return self.fail(7, f"no guest {t[2] if len(t) > 2 else ''}")
        if verb == "logs":
            stream = t[t.index("--stream") + 1] if "--stream" in t else "stdout"
            text = self.guest_log(tour, g) if stream == "stdout" else ""
            return 0, self.doc(tour=tour["id"], guest=g["id"], stream=stream, truncated=False, untrusted=True, text=text), ""
        if g["status"] not in self.TERMINAL:
            return self.fail(3, "that guest has no result yet")
        if verb == "result":
            return 0, self.doc(result=self.guest_result(tour, g), note="verified is what motel measured"), ""
        if verb == "fetch":
            if not g.get("bundle"):
                return 0, self.doc(tour=tour["id"], guest=g["id"], branch=None, tip=None, artifacts=[]), ""
            repo = w.get("repo")
            if repo is None:
                return self.fail(5, "not inside a git repository")
            branch = f"motel/{tour['id']}/{g['id']}"
            repo["branches"][branch] = {"tip": g["head"], "changed": list(g["changed"]),
                                        "files": {name: f"{g['id']}-change\n" for name in g["changed"]}}
            return 0, self.doc(tour=tour["id"], guest=g["id"], branch=branch, tip=g["head"], artifacts=[]), ""
        return 2, "", "usage\n"

    def tour_start(self, path):
        w = self.w
        entry = w["fs"].get(path)
        if entry is None:
            return self.fail(2, f"no spec file {path}")
        spec = json.loads(entry["content"])
        defaults, guests = spec.get("defaults", {}), spec.get("guests", [])
        repo = self.w.get("repo") or {}
        for g in guests:  # as the real CLI: the guest's repo is relative to the spec file's directory
            where = os.path.normpath(os.path.join(os.path.dirname(path), g.get("repo") or "."))
            if where != repo.get("path"):
                return self.fail(2, f"{where} is not a git repository")
        if len(guests) > 2:
            return self.fail(3, "refused: more guests than max_guests_per_tour")
        tid = f"t-{int(new_id(w)):010x}"
        start = now(w).timestamp()
        tour = {"id": tid, "name": spec.get("tour", {}).get("name", "tour"), "owner": self.user, "start": start, "cancel": False,
                "guests": []}
        for raw in guests:
            gid = raw["id"]
            argv = raw.get("argv", [])
            kind = raw.get("server_type") or defaults.get("server_type", "cx23")
            lease = self.make_cabin(f"tour-{gid}", kind, timedelta(minutes=15), False)
            g = {"id": gid, "script": argv[2] if argv[:2] == ["sh", "-c"] else " ".join(argv),
                 "secrets": list(raw.get("secrets", defaults.get("secrets", []))),
                 "egress": list(raw.get("egress", defaults.get("egress", []))),
                 "lease": lease["lease_id"], "status": "booking", "cleanup": "live", "run_s": RUN_S.get(gid, 30),
                 "exit": 3 if gid == "fails" else 0, "bundle": None, "changed": [], "head": None, "ended": None, "reason": None}
            tour["guests"].append(g)
            if gid == "bravo" and "extra-cabin" in self.modes():  # a retry: one more cabin than the plan, billed too
                g["extra"] = self.make_cabin("tour-retry", kind, timedelta(minutes=15), False)["lease_id"]
        w.setdefault("tours", {})[tid] = tour
        return 0, self.doc(tour=self.tour_doc(tour), warnings=[]), ""

    def tour_tick(self, tour):
        t = now(self.w).timestamp()
        run_from, restarts = tour["start"] + BOOKING_S, self.concierge()["restarts"]
        for g in tour["guests"]:
            if g["status"] in self.TERMINAL:
                continue
            lost = [r for r in restarts if r > run_from] if g["id"] == "alpha" and "listener-lost" in self.modes() else []
            if tour["cancel"]:
                self.tour_end(tour, g, "cancelled", t)
            elif lost and t >= lost[0]:
                self.tour_end(tour, g, "failed", lost[0], reason="concierge_listener_lost")
            elif "boot-stuck" in self.modes():
                g["status"] = "booking"      # the cabin never finishes its bootstrap: the guest stays booking for good
            elif g["run_s"] is not None and t >= run_from + g["run_s"]:
                failed = g["exit"] != 0 and "fail-not-reported" not in self.modes()
                self.tour_end(tour, g, "failed" if failed else "done", run_from + g["run_s"])
            else:
                g["status"] = "running" if t >= run_from else "booking"

    @staticmethod
    def attempt_record(tour, g):
        boot = {"polls": 30, "summary": "guest bootstrap running (stage tour-firewall; cloud-init error)", "phase": "waiting for guest readiness",
                "channel": None, "ready": {"bootstrap": "running", "boot": {"stage": "tour-firewall", "cloud_init": "error"}}}
        return {"id": f"a-{g['id']}", "tour": tour["id"], "guest": g["id"], "status": g["status"], "phase": "checkin", "boot": boot,
                "error": None, "lease_id": g["lease"]}

    def tour_end(self, tour, g, status, at, reason=None):
        w = self.w
        g.update(status=status, cleanup="deleted", ended=at, reason=reason)
        keep = status == "cancelled" and "cancel-leaks" in self.modes() and g["id"] == "hangs"
        for lease in (g["lease"], g.get("extra")):
            if lease in w.get("leases", {}) and not keep:
                self.drop_cabin(lease)
        if status == "done":
            g["changed"] = [f"{g['id']}.txt"]
            g["head"] = hashlib.sha1(f"{tour['id']}/{g['id']}".encode()).hexdigest()
            g["bundle"] = {"sha256": hashlib.sha256(f"bundle {tour['id']}/{g['id']}".encode()).hexdigest(), "bytes": 1200 + len(g["id"])}
            if w.get("sampler") and "inspect-as-root" not in self.modes():  # the inspection ran as motel-inspect, under prlimit
                limit = "unlimited unlimited" if "no-prlimit" in self.modes() else "1073741824 1073741824"
                line = (f"4321 git -c core.hooksPath=/dev/null -c protocol.allow=never fetch --quiet # Max cpu time 60 60 seconds ;"
                        f"Max file size 104857600 104857600 bytes ;Max processes 64 64 processes ;Max address space {limit} bytes ;\n")
                w["fs"][w["sampler"]["file"]]["content"] += line

    def tour_doc(self, tour, status=False):
        states = [g["status"] for g in tour["guests"]]
        over = "running" if any(x not in self.TERMINAL for x in states) else (
            "cancelled" if tour["cancel"] else "done" if all(x == "done" for x in states) else "failed")
        doc = {"id": tour["id"], "name": tour["name"], "status": over, "cleanup": "deleted" if over != "running" else "live",
               "owner": tour["owner"], "cancel_requested": tour["cancel"], "counts": {x: states.count(x) for x in set(states)},
               "guests": [{"id": g["id"], "status": g["status"], "cleanup": g["cleanup"], "attempts": 1,
                           **({"reason": g["reason"]} if g["reason"] else {})} for g in tour["guests"]]}
        if status:
            lost = {"a-1": "not_preserved"} if any(g["reason"] == "concierge_listener_lost" for g in tour["guests"]) else {}
            doc.update(lost_listeners=lost, supervisor={"alive": True})
            for row, g in zip(doc["guests"], tour["guests"], strict=True):
                rec = self.attempt_record(tour, g)
                row["attempt_list"] = [{k: rec.get(k) for k in ("id", "status", "phase", "lease_id", "boot", "error")}]
        return doc

    def guest_log(self, tour, g):
        """What the guest printed so far, as the canned scripts of the harness would print it."""
        t = now(self.w).timestamp()
        end = g["ended"] if g["ended"] is not None else t
        run_from, restarts, lines = tour["start"] + BOOKING_S, self.concierge()["restarts"], []
        if g["id"] != "alpha":
            return f"{g['id']}\n" if end > run_from else ""
        if "LISTEN" in g["script"]:
            for k in range(24):
                ts = int(run_from + 10 * k + 1)
                if ts <= end:
                    broken = "listener-lost" in self.modes() and any(r <= ts for r in restarts)
                    lines.append(f"LISTEN {ts} 000 " if broken else f"LISTEN {ts} 403 motel_path_denied")
        if g["status"] == "done" and "OUTSIDE" in g["script"]:
            host = g["egress"][0] if g["egress"] else "pypi.org"
            wide = "egress-open" in self.modes()
            lines += [f"OUTSIDE example.com {'200' if wide else '000'}", "OUTSIDE api.anthropic.com 000", "OUTSIDE 1.1.1.1 000",
                      f"ALLOWED {host} 200", "PLACEHOLDER foreign_host 403 motel_binding_denied",
                      "PLACEHOLDER other_binding 403 motel_binding_denied", "PLACEHOLDER bad_path 403 motel_path_denied",
                      "PLACEHOLDER bad_token 401 motel_invalid_token",
                      f"KEYSCAN env=0 procs=0 files={1 if 'key-on-guest' in self.modes() else 0}"]
        if g["status"] == "done":
            lines.append("ALPHA-DONE")
        return "".join(x + "\n" for x in lines)

    def guest_result(self, tour, g):
        done = g["status"] == "done"
        probes = self.guest_log(tour, g).count("LISTEN ") + (3 if "OUTSIDE" in g["script"] and done else 0)
        return {"schema": "motel/tour-result/v1", "tour": tour["id"], "guest": g["id"], "attempt": "a-1", "status": g["status"],
                "cleanup": g["cleanup"], "auth": "api-key", "branch": f"motel/{tour['id']}/{g['id']}", "partial": False, "error": None,
                "reason": g["reason"],
                "verified": {"duration_s": int((g["ended"] or 0) - tour["start"] - BOOKING_S), "bundle": g["bundle"], "head": g["head"],
                             "changed_files": g["changed"], "artifacts": [], "cost_eur": "0.001",
                             "usage": {"metered": True, "refused": probes}, "cabin": g["lease"]},
                "runner_reported": {"runner_exit": 0, "exit_code": g["exit"] if g["status"] != "cancelled" else None,
                                    "termination": "exited" if g["status"] in ("done", "failed") else "killed"},
                "concierge_refusals": {"reported_by_runner": [], "counted_by_concierge": probes if g["secrets"] else 0},
                "violations": [], "notes": []}

    # -- the CLI (any allowed user)
    def motel(self, t):
        w = self.w
        if self.user not in w["config"].get("allow_users", "").split(","):
            return 5, self.doc(False, error="sudo refused to run the front desk (allow_users)", code=5), ""
        verb = t[0]
        if verb == "doctor":
            return (0 if w["setup"] else 5), self.doc(w["setup"], checks=[{"check": "host set up", "level": "ok", "ok": True, "detail": ""}]), ""
        if not w["setup"]:
            return self.fail(8, "not set up")
        if verb == "cabin":
            return self.cabin_cli(t[1:])
        if verb == "tour":
            return self.tour_cli(t[1:])
        if verb == "checkin":
            return self.checkin(t)
        if verb == "ls":
            rooms = [self.describe(r) for r in w["rooms"].values() if r["state"] != "purged" and r["owner"] == self.user]
            floors = [f for f in w["floors"].values() if f["state"] != "deleted"]
            return 0, self.doc(rooms=rooms, floors=floors, capacity={"floors": len(floors), "total_gb": sum(f["size"] for f in floors)}), ""
        room = w["rooms"].get(t[1]) if len(t) > 1 else None
        if room is None:
            return self.fail(7, f"no room {t[1]}")
        if room["owner"] != self.user:
            return self.fail(3, "not yours")
        if verb == "status":
            return 0, self.doc(room=self.describe(room)), ""
        if verb == "extend":
            mins = self.dur(t[t.index("--for") + 1])
            if mins > self.minutes("max_ttl_call", "7d"):
                return self.fail(3, "over the limit per extension")
            room["expires"] = iso(max(parse_iso(room["expires"]), now(w)) + mins)
            room["state"] = "active"
            return 0, self.doc(room=self.describe(room)), ""
        if verb == "checkout":
            return self.checkout(room, "--now" in t)
        if verb == "restore":
            if room["state"] != "lost-and-found":
                return self.fail(3, "not in lost and found")
            self.move(room, f"{MOUNT}/{room['floor']}/.lost-and-found/{room['id']}", room["path"])
            room.update(state="active", expires=iso(now(w) + self.dur(t[t.index("--for") + 1])) if "--for" in t else room["expires"])
            return 0, self.doc(room=self.describe(room)), ""
        return 2, "", "usage\n"

    def dur(self, text):
        return timedelta(minutes=int(text[:-1]) * {"m": 1, "h": 60, "d": 1440}[text[-1]])

    def describe(self, room):
        w = self.w
        state = room["state"]
        if state == "active" and now(w) >= parse_iso(room["expires"]):
            state = "grace"
        out = {k: room[k] for k in ("id", "task", "size_gb", "floor", "suite", "created", "expires")}
        out.update(owner=room["owner"], state=state, path=room["path"] if state != "purged" else None, warn=False,
                   time_left_h=round((parse_iso(room["expires"]) - now(w)).total_seconds() / 3600, 2))
        return out

    def move(self, room, src, dst):
        w = self.w
        for path in list(w["fs"]):
            if path.startswith(src + "/"):
                w["fs"][dst + path[len(src):]] = w["fs"].pop(path)
        self.dirs().pop(src, None)
        self.dirs()[dst] = {"owner": room["owner"]}

    def checkout(self, room, now_flag):
        w = self.w
        if room["state"] in ("active", "grace"):
            if any(h.startswith(room["path"]) for h in w["holders"]):
                return self.fail(4, "room is in use")
            trash = f"{MOUNT}/{room['floor']}/.lost-and-found/{room['id']}"
            self.move(room, room["path"], trash)
            room.update(state="lost-and-found", trashed=iso(now(w)))
        if now_flag or self.minutes("lost_and_found", "24h") == timedelta(0):
            self.purge(room)
        return 0, self.doc(room=self.describe(room)), ""

    def purge(self, room):
        trash = f"{MOUNT}/{room['floor']}/.lost-and-found/{room['id']}"
        for path in list(self.w["fs"]):
            if path.startswith(trash + "/") or path.startswith(room["path"] + "/"):
                del self.w["fs"][path]
        self.dirs().pop(trash, None)
        self.dirs().pop(room["path"], None)
        room["state"] = "purged"

    def new_volume(self, floor_id, size, suite=False):
        w = self.w
        vid = new_id(w)
        w["volumes"][vid] = {"id": vid, "name": f"motel-{floor_id}", "size": size, "server": self.sid, "created": iso(now(w)),
                             "labels": {"managed-by": "motel", "role": "floor", "host": self.sid, "floor": floor_id,
                                    "suite": "1" if suite else "0"}}
        return vid

    def checkin(self, t):
        w = self.w
        size = int(t[1].rstrip("Gg"))
        ttl = self.dur(t[t.index("--for") + 1]) if "--for" in t else timedelta(minutes=30)
        task = t[t.index("--task") + 1] if "--task" in t else "room"
        suite = "--suite" in t
        if ttl > self.minutes("max_ttl_call", "7d"):
            return self.fail(3, "checkout time over the limit")
        floors = [f for f in w["floors"].values() if f["state"] != "deleted"]
        floor = None
        if not suite:
            for f in floors:
                if not f["suite"] and f["size"] - sum(r["size_gb"] for r in w["rooms"].values()
                                                       if r["floor"] == f["id"] and r["state"] in ("active", "grace")) >= size:
                    floor = f
                    break
        if floor is None:
            grown = False
            want = size if suite else max(size, self.cfg("min_floor_gb", 20))
            want = -(-want // 10) * 10
            if len(floors) + 1 > self.cfg("max_floors", 3) or want > self.cfg("max_floor_gb", 500) \
                    or sum(f["size"] for f in floors) + want > self.cfg("max_total_gb", 1000):
                if not suite:  # no new floor: grow the smallest shared floor, like the real desk
                    for f in sorted((f for f in floors if not f["suite"] and f["state"] == "active"), key=lambda f: f["size"]):
                        used = sum(r["size_gb"] for r in w["rooms"].values() if r["floor"] == f["id"] and r["state"] in ("active", "grace"))
                        new_size = -(-(used + size) // 10) * 10
                        if new_size <= self.cfg("max_floor_gb", 500) and \
                                sum(x["size"] for x in floors) - f["size"] + new_size <= self.cfg("max_total_gb", 1000):
                            f["size"] = new_size
                            w["volumes"][f["volume"]]["size"] = new_size
                            floor, grown = f, True
                            break
                if floor is None:
                    return self.fail(3, "no vacancy")
            if not grown:
                fid = "f-" + new_id(w)
                vid = self.new_volume(fid, want, suite)
                floor = {"id": fid, "volume": vid, "size": want, "suite": suite, "state": "created", "mounted": False, "idle_since": iso(now(w)),
                         "created": iso(now(w))}
                w["floors"][fid] = floor
                if task == "intr":
                    w["killed"] = False
                save(w)
                if task == "intr":
                    with world_unlocked():
                        end = time.time() + 12
                        while time.time() < end and not load()["killed"]:
                            time.sleep(0.1)
                    w2 = load()
                    if w2["killed"]:
                        w2["killed"] = False
                        save(w2)
                        self.nosave = True
                        return 137, "", "Killed\n"
                    w.update(w2)
                    floor = w["floors"][fid]
                floor.update(state="active", mounted=True)
        rid = f"{task}-{new_id(w)[-4:]}"
        path = f"{MOUNT}/{floor['id']}/rooms/{rid}"
        room = {"id": rid, "task": task, "owner": self.user, "size_gb": size, "floor": floor["id"], "suite": suite,
                "created": iso(now(w)), "expires": iso(now(w) + ttl), "state": "active", "path": path}
        w["rooms"][rid] = room
        self.dirs()[path] = {"owner": self.user}
        floor["idle_since"] = None
        return 0, self.doc(room=self.describe(room)), ""

    def floor_held(self, floor, n):
        """An empty SHARED floor waits for its release window; a suite floor, `retire_after = "0m"` and the fault mode do not."""
        if floor.get("suite") or "floor-retired-at-once" in self.w.get("modes", []) or self.minutes("retire_after", "15m") <= timedelta(0):
            return False
        return not self.release_due(floor.get("created") or floor["idle_since"], n)

    def reap(self, apply):
        w = self.w
        actions, n, held = [], now(w), []
        for room in w["rooms"].values():
            exp = parse_iso(room["expires"])
            if room["state"] in ("active", "grace") and n >= exp + self.minutes("grace", "6h"):
                actions.append(f"room {room['id']} grace over: moved to lost & found")
                if apply and not any(h.startswith(room["path"]) for h in w["holders"]):
                    self.move(room, room["path"], f"{MOUNT}/{room['floor']}/.lost-and-found/{room['id']}")
                    room.update(state="lost-and-found", trashed=iso(n))
            elif room["state"] == "active" and n >= exp:
                actions.append(f"room {room['id']} passed its checkout time")
            elif room["state"] == "lost-and-found" and n >= parse_iso(room["trashed"]) + self.minutes("lost_and_found", "24h"):
                actions.append(f"room {room['id']} purged from lost & found")
                if apply:
                    self.purge(room)
        for floor in w["floors"].values():
            if floor["state"] == "deleted":
                continue
            busy = [r for r in w["rooms"].values() if r["floor"] == floor["id"] and r["state"] != "purged"]
            if busy:
                if apply and not floor["mounted"]:
                    floor["mounted"] = True
                continue
            if floor.get("idle_since") is None:
                floor["idle_since"] = iso(n)
            elif n - parse_iso(floor["idle_since"]) >= self.minutes("retire_after", "15m"):
                if self.floor_held(floor, n):
                    held.append({"floor": floor["id"], "reason": "empty"})
                    continue
                actions.append(f"floor {floor['id']} has no rooms: unmount, detach, delete")
                if apply:
                    w["volumes"].pop(floor["volume"], None)
                    floor.update(state="deleted", mounted=False)
        floors = [{"id": f["id"]} for f in w["floors"].values() if f["state"] != "deleted"]
        rooms = [self.describe(r) for r in w["rooms"].values() if r["state"] != "purged"]
        return 0, self.doc(apply=apply, actions=actions, warnings=[], refused=[], orphans=[], errors=[], floors=floors, rooms=rooms, held=held), ""


if __name__ == "__main__":
    tool, args = sys.argv[1], sys.argv[2:]
    with world_lock():
        code = {"hcloud": hcloud, "ssh": ssh, "scp": scp, "ssh-keygen": keygen}[tool](args)
    raise SystemExit(code)
