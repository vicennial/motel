"""Tests for the GitHub Action: ``action.yml`` structure, the driver script ``action/motel-action.sh``, and the docs.

Standard library only (PyYAML is used when it happens to be installed). The driver script runs against a fake
``sudo`` and a fake ``motel`` on PATH: no root, no network, no cloud token.
"""

import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

from platform_marks import linux_only

ROOT = Path(__file__).resolve().parent.parent
ACTION = ROOT / "action.yml"
SCRIPT = ROOT / "action" / "motel-action.sh"
DOC = ROOT / "docs" / "github-action.md"
SHA_PIN = re.compile(r"@[0-9a-f]{40}(\s|$)")
TOKEN = "hcloud-test-token-0123456789"


# --- action.yml ----------------------------------------------------------------------------------------


def load_action() -> dict:
    """The action as {inputs, outputs, steps}. Uses PyYAML if present, else a small reader for this file's layout."""
    text = ACTION.read_text(encoding="utf-8")
    try:
        import yaml
    except ImportError:
        return _read_layout(text)
    doc = yaml.safe_load(text)
    return {"inputs": doc["inputs"], "outputs": doc["outputs"], "steps": doc["runs"]["steps"], "using": doc["runs"]["using"]}


def _read_layout(text: str) -> dict:
    """Reads the fixed layout of action.yml: two-space top-level keys, ``- `` steps at four spaces, env at eight."""
    lines = text.splitlines()
    out: dict = {"inputs": {}, "outputs": {}, "steps": [], "using": None}
    section = sub = step = None
    block_key = None
    for raw in lines:
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        indent = len(raw) - len(raw.lstrip())
        body = raw.strip()
        if indent == 0:
            section, sub, step, block_key = body.rstrip(":"), None, None, None
        elif section in ("inputs", "outputs") and indent == 2:
            sub = body.rstrip(":")
            out[section][sub] = {}
        elif section in ("inputs", "outputs") and indent == 4:
            key, _, value = body.partition(":")
            out[section][sub][key] = value.strip().strip('"')
        elif section == "runs":
            if indent == 2:
                key, _, value = body.partition(":")
                if key == "using":
                    out["using"] = value.strip()
            elif body.startswith("- ") and indent == 4:
                step, block_key = {}, None
                out["steps"].append(step)
                body = body[2:]
                indent += 2
            if step is None:
                continue
            if indent == 6:
                key, _, value = body.partition(":")
                block_key = None
                if key == "env":
                    step["env"] = {}
                    block_key = "env"
                else:
                    step[key] = value.strip().strip('"')
            elif indent == 8 and block_key == "env":
                key, _, value = body.partition(":")
                step["env"][key] = value.strip()
    return out


class ActionYamlTest(unittest.TestCase):
    def setUp(self):
        self.action = load_action()
        self.text = ACTION.read_text(encoding="utf-8")

    def test_is_a_composite_action_with_the_documented_interface(self):
        self.assertEqual(self.action["using"], "composite")
        self.assertTrue({"mode", "command", "server-type", "ttl", "size", "sync-path", "hetzner-token", "motel-version"}
                        <= set(self.action["inputs"]))
        self.assertTrue({"exit-code", "cabin-id", "room-path", "cost-estimate"} <= set(self.action["outputs"]))

    def test_every_used_action_is_pinned_by_full_commit_sha(self):
        uses = [line.strip() for line in self.text.splitlines() if re.match(r"\s*(-\s+)?uses:", line)]
        for line in uses:
            self.assertRegex(line, SHA_PIN, f"not pinned by a 40-hex commit SHA: {line}")

    def test_the_token_is_only_ever_an_environment_variable(self):
        references = [i for i, line in enumerate(self.text.splitlines()) if "inputs.hetzner-token" in line]
        self.assertEqual(len(references), 1, "the token input must be referenced exactly once")
        steps_with_token = [s for s in self.action["steps"] if "hetzner-token" in json.dumps(s.get("env", {}))]
        self.assertEqual(len(steps_with_token), 1)
        env = steps_with_token[0]["env"]
        self.assertEqual(env["HETZNER_TOKEN"].replace(" ", ""), "${{inputs.hetzner-token}}")

    def test_no_run_body_interpolates_expressions(self):
        for step in self.action["steps"]:
            run = step.get("run", "")
            self.assertNotIn("${{", run, "inputs reach scripts through env: only (no script injection)")
            self.assertNotIn("TOKEN", run)
            self.assertNotIn("hetzner-token", run)

    def test_cleanup_is_the_last_step_and_runs_always(self):
        last = self.action["steps"][-1]
        self.assertEqual(last["if"].strip(), "always()")
        self.assertIn("cleanup", last["run"])
        self.assertEqual([s["id"] for s in self.action["steps"]], ["init", "prepare", "run", "cleanup"])
        others = [s for s in self.action["steps"] if s is not last]
        self.assertTrue(all("if" not in s for s in others), "only the cleanup step is conditional")

    def test_every_step_names_its_shell(self):
        for step in self.action["steps"]:
            self.assertEqual(step.get("shell"), "bash")

    def test_outputs_come_from_the_run_step(self):
        for name, spec in self.action["outputs"].items():
            self.assertIn(f"steps.run.outputs.{name}", spec["value"])


class ScriptHygieneTest(unittest.TestCase):
    def test_token_never_reaches_a_command_line(self):
        allowed = (r'printf \'%s\' "\$HETZNER_TOKEN" \|', r'\[ -n "\$\{HETZNER_TOKEN:-\}" \]', r"unset HETZNER_TOKEN",
                   r"^\s*#")
        for number, line in enumerate(SCRIPT.read_text().splitlines(), 1):
            if "TOKEN" in line.upper() and "HETZNER_TOKEN" in line:
                self.assertTrue(any(re.search(p, line) for p in allowed), f"{SCRIPT.name}:{number}: {line.strip()}")

    def test_no_tracing_and_no_eval(self):
        text = SCRIPT.read_text()
        self.assertNotRegex(text, r"set\s+-\w*x")
        self.assertNotIn("xtrace", text)
        self.assertNotRegex(text, r"\beval\b")

    def test_script_syntax(self):
        self.assertEqual(subprocess.run(["bash", "-n", str(SCRIPT)], capture_output=True).returncode, 0)

    def test_every_motel_command_the_script_runs_exists(self):
        """The argv shapes the script uses must parse with the real CLI parsers, so no flag is invented."""
        from motel import cli as room_cli
        from motel import desk as room_desk
        from motel.cabins import cli as cabin_cli
        from motel.cabins import desk as cabin_desk

        cabin = cabin_cli.build_parser()
        args = cabin.parse_args(["run", "--for", "1h", "--task", "gha-1-1-ab12", "--type", "cpx32"])
        self.assertEqual((args.verb, args.ttl, args.server_type), ("run", "1h", "cpx32"))
        cabin.parse_args(["ls", "--json"])
        cabin.parse_args(["checkout", "swift-gull-0123456789ab", "--json"])
        rooms = room_cli.build_parser()
        rooms.parse_args(["checkin", "20", "--for", "1h", "--task", "gha-1-1-ab12", "--json"])
        rooms.parse_args(["checkout", "video-render-k3f9", "--now", "--json"])
        rooms.parse_args(["ls", "--mine", "--json"])
        rooms.parse_args(["doctor", "--json"])
        room_desk.build_parser().parse_args(["setup", "hetzner", "--token-file", "/x"])
        room_desk.build_parser().parse_args(["setup", "local"])
        cabin_desk.build_parser().parse_args(["setup", "hetzner", "--token-file", "/x"])


# --- the driver script against fakes ---------------------------------------------------------------------

FAKE_SUDO = """#!{python}
import json, os, stat, sys
argv = sys.argv[1:]
log = os.environ["FAKE_DIR"] + "/sudo.jsonl"
entry = {{"argv": argv}}
if "--token-file" in argv:
    path = argv[argv.index("--token-file") + 1]
    st = os.stat(path)
    entry["token_file_mode"] = oct(stat.S_IMODE(st.st_mode))
    entry["token_file_has_token"] = open(path).read() == os.environ["EXPECT_TOKEN"]
with open(log, "a") as f:
    f.write(json.dumps(entry) + "\\n")
if argv[:1] == ["install"] and argv[-1].startswith("/") and argv[-2] == "/dev/null":
    os.makedirs(os.path.dirname(argv[-1]), exist_ok=True)
    fd = os.open(argv[-1], os.O_WRONLY | os.O_CREAT, 0o600)
    os.close(fd)
    os.chmod(argv[-1], 0o600)
elif argv[:1] in (["tee"], ["rm"]):
    os.execvp(argv[0], argv)
"""

FAKE_MOTEL = """#!{python}
import json, os, sys
args = [a for a in sys.argv[1:] if a != "--json"]
d = os.environ["FAKE_DIR"]
if "--task" in args:
    open(d + "/task", "w").write(args[args.index("--task") + 1])
task = open(d + "/task").read() if os.path.exists(d + "/task") else "unknown"
with open(d + "/motel.jsonl", "a") as f:
    f.write(json.dumps({{"args": sys.argv[1:], "cwd": os.getcwd()}}) + "\\n")
for rule in json.load(open(d + "/rules.json")):
    if args[:len(rule["match"])] == rule["match"]:
        sys.stdout.write(rule.get("stdout", "").replace("@TASK@", task))
        sys.stderr.write(rule.get("stderr", "").replace("@TASK@", task))
        sys.exit(rule.get("exit", 0))
sys.stderr.write("fake motel: no rule for %s\\n" % args)
sys.exit(99)
"""

# A sudo that really runs the staging commands (cp, rm, chmod, install -d) as the current user. There is no root here, so
# ownership changes are no-ops, and `sh .../install.sh` audits the staged tree with the installer's own audit and then runs
# the real installer as a dry run into a temp prefix.
EXEC_SUDO = """#!{python}
import json, os, subprocess, sys
sys.path.insert(0, {src!r})
argv = sys.argv[1:]
rec = {{"argv": argv}}
run = argv
if argv[:1] == ["chown"]:
    run = None
elif argv[:1] == ["chmod"] and not all(os.path.realpath(p).startswith(os.path.realpath(os.environ["FAKE_DIR"]) + os.sep)
                                       or os.path.realpath(p).startswith(os.path.realpath(os.environ.get("MOTEL_ACTION_INSTALL_TOP", "/nonexistent")) + os.sep)
                                       for p in argv[2:]):
    rec["refused"] = "chmod outside the test tree"   # never change the real host's directories from a test
    run = None
elif argv[:1] == ["install"]:
    run, it = [], iter(argv)
    for a in it:
        if a in ("-o", "-g"):
            next(it)
        else:
            run.append(a)
elif argv[:1] == ["sh"] and argv[1].endswith("install.sh"):
    from motel.installer import audit_source
    stage = os.path.dirname(argv[1])
    rec["audit"] = audit_source(os.path.join(stage, "src", "motel"), owner_uid=os.getuid(), chain=False)
    modes = set()
    for base, dirs, files in os.walk(stage):
        modes.update(os.lstat(os.path.join(base, n)).st_mode & 0o777 for n in dirs + files)
    rec["modes"] = sorted(oct(m) for m in modes)
    rec["stage_mode"] = oct(os.stat(stage).st_mode & 0o777)
    dry = subprocess.run(["sh", *argv[1:], "--dry-run", "--prefix", os.environ["FAKE_DIR"] + "/prefix"], capture_output=True, text=True)
    rec["dry_run_rc"], rec["dry_run_err"] = dry.returncode, dry.stderr[-300:]
    run = None
elif argv[0].startswith("/fake/"):
    run = None
with open(os.environ["FAKE_DIR"] + "/sudo.jsonl", "a") as f:
    f.write(json.dumps(rec) + "\\n")
if run:
    os.execvp(run[0], run)
"""

CABIN_ID = "gha-run-0123456789ab"
CABIN_STDERR = (f"motel: cabin {CABIN_ID} · EUR 0.0200/h net · ready in 40s\n"
                f"motel: released {CABIN_ID} · est. cost EUR 0.0150 net\n")


def cabins_doc(state: str) -> str:
    return json.dumps({"schema": "motel/v1", "provider": {"inventory": "complete"},
                       "cabins": [{"lease_id": CABIN_ID, "task": "@TASK@", "state": state}]})


@unittest.skipUnless(all(os.path.exists(p) for p in ("/usr/bin/rsync", "/usr/bin/ssh", "/usr/bin/ssh-keygen")),
                     "the cabin preflight needs rsync and ssh on the machine that runs the tests")
class DriverTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.tmp = Path(tmp.name)
        self.fake = self.tmp / "fake"
        self.bin = self.tmp / "bin"
        self.work = self.tmp / "work"
        self.runner_temp = self.tmp / "runner-temp"
        for d in (self.fake, self.bin, self.work, self.runner_temp):
            d.mkdir()
        for name, template in (("sudo", FAKE_SUDO), ("motel", FAKE_MOTEL)):
            path = self.bin / name
            path.write_text(template.format(python=sys.executable))
            path.chmod(path.stat().st_mode | stat.S_IXUSR)
        subprocess.run(["git", "init", "-q", str(self.work)], check=True)
        self.output = self.tmp / "github_output"
        self.output.touch()
        self.rules: list = []
        self.env = {
            "PATH": f"{self.bin}{os.pathsep}/usr/bin{os.pathsep}/bin", "HOME": str(self.tmp), "FAKE_DIR": str(self.fake),
            "EXPECT_TOKEN": TOKEN, "MOTEL_ACTION_SUDO": str(self.bin / "sudo"), "MOTEL_ACTION_DESK": "/fake/motel-desk",
            "RUNNER_TEMP": str(self.runner_temp), "GITHUB_OUTPUT": str(self.output), "GITHUB_RUN_ID": "4242",
            "GITHUB_RUN_ATTEMPT": "1", "ACTION_PATH": str(ROOT), "MOTEL_ACTION_STAGE": str(self.tmp / "staged"), "MOTEL_ACTION_TEST_MODE": "1",
        }
        self.state = ""
        self.task = ""

    def set_rules(self, *rules):
        (self.fake / "rules.json").write_text(json.dumps(list(rules)))

    def step(self, verb, **extra):
        env = {**self.env, "STATE_DIR": self.state, "TASK": self.task, **extra}
        return subprocess.run(["bash", str(SCRIPT), verb], capture_output=True, text=True, env=env, cwd=self.work)

    def init(self):
        proc = self.step("init")
        self.assertEqual(proc.returncode, 0, proc.stderr)
        out = self.outputs()
        self.state, self.task = out["state"], out["task"]

    def outputs(self):
        pairs = (line.split("=", 1) for line in self.output.read_text().splitlines() if "=" in line)
        return dict(pairs)

    def sudo_calls(self):
        path = self.fake / "sudo.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def motel_calls(self):
        path = self.fake / "motel.jsonl"
        return [json.loads(line) for line in path.read_text().splitlines()] if path.exists() else []

    def cabin_env(self, **extra):
        return {"MODE": "cabin-run", "COMMAND": "make test", "TTL": "30m", "SERVER_TYPE": "cpx32", "SYNC_PATH": str(self.work),
                "HETZNER_TOKEN": TOKEN, **extra}

    # -- init
    def test_init_makes_a_private_directory_and_a_valid_task(self):
        self.init()
        self.assertEqual(stat.S_IMODE(os.stat(self.state).st_mode), 0o700)
        self.assertRegex(self.task, r"^gha-4242-1-[0-9a-f]{4}$")

    # -- cabin-run
    @linux_only
    def test_cabin_run_happy_path(self):
        self.set_rules({"match": ["cabin", "ls"], "stdout": cabins_doc("released")},
                       {"match": ["cabin", "run"], "stdout": "tests passed\n", "stderr": CABIN_STDERR})
        self.init()
        env = self.cabin_env()
        self.assertEqual(self.step("prepare", **env).returncode, 0)
        run = self.step("run", **env)
        self.assertEqual(run.returncode, 0, run.stderr)
        self.assertIn("tests passed", run.stdout)
        out = self.outputs()
        self.assertEqual((out["exit-code"], out["cabin-id"], out["cost-estimate"]), ("0", CABIN_ID, "0.0150"))
        call = next(c for c in self.motel_calls() if c["args"][:2] == ["cabin", "run"])
        self.assertEqual(call["args"], ["cabin", "run", "--for", "30m", "--task", self.task, "--type", "cpx32", "--",
                                        "sh", "-c", "make test"])
        self.assertEqual(Path(call["cwd"]).resolve(), self.work.resolve())
        self.assertEqual(self.step("cleanup", **env).returncode, 0)
        self.assertFalse(Path(self.state).exists())

    @linux_only
    def test_the_token_is_in_a_private_root_owned_file_and_never_in_any_argv(self):
        self.set_rules({"match": ["cabin", "ls"], "stdout": cabins_doc("released")},
                       {"match": ["cabin", "run"], "stderr": CABIN_STDERR})
        self.init()
        env = self.cabin_env()
        self.step("prepare", **env)
        self.step("run", **env)
        self.step("cleanup", **env)
        calls = self.sudo_calls()
        setup = next(c for c in calls if "--token-file" in c["argv"])
        self.assertEqual(setup["argv"][:5], ["/fake/motel-desk", "cabin", "setup", "hetzner", "--token-file"])
        self.assertEqual(setup["token_file_mode"], "0o600")
        self.assertTrue(setup["token_file_has_token"])
        everything = json.dumps(calls) + json.dumps(self.motel_calls())
        self.assertNotIn(TOKEN, everything)
        install = next(c for c in calls if c["argv"][:1] == ["install"])
        self.assertEqual(install["argv"][1:7], ["-m", "0600", "-o", "root", "-g", "root"])
        self.assertTrue(any(c["argv"][:2] == ["rm", "-f"] for c in calls), "the token file is removed")

    def test_nonzero_command_fails_the_step_but_cleanup_still_checks_out(self):
        self.set_rules({"match": ["cabin", "ls"], "stdout": cabins_doc("active")},
                       {"match": ["cabin", "run"], "exit": 3, "stderr": "motel: cabin gha-run-0123456789ab · EUR 0.02/h net\n"},
                       {"match": ["cabin", "checkout"], "stdout": json.dumps({"ok": True})})
        self.init()
        env = self.cabin_env()
        self.assertEqual(self.step("prepare", **env).returncode, 0)
        run = self.step("run", **env)
        self.assertEqual(run.returncode, 3)
        self.assertEqual(self.outputs()["exit-code"], "3")
        self.assertEqual(self.step("cleanup", **env).returncode, 0)
        self.assertIn(["cabin", "checkout", CABIN_ID, "--json"], [c["args"] for c in self.motel_calls()])
        self.assertFalse(Path(self.state).exists())

    def test_fail_on_error_false_reports_the_status_without_failing(self):
        self.set_rules({"match": ["cabin", "ls"], "stdout": cabins_doc("released")}, {"match": ["cabin", "run"], "exit": 7})
        self.init()
        env = self.cabin_env(FAIL_ON_ERROR="false")
        self.assertEqual(self.step("run", **env).returncode, 0)
        self.assertEqual(self.outputs()["exit-code"], "7")

    def test_failed_prepare_still_cleans_up_the_token(self):
        self.set_rules({"match": ["cabin", "ls"], "exit": 8, "stdout": json.dumps({"ok": False, "error": "cabins are not set up"})})
        self.init()
        env = self.cabin_env()
        prepare = self.step("prepare", **env)
        self.assertEqual(prepare.returncode, 2)
        self.assertIn("::error::", prepare.stderr)
        self.assertTrue((Path(self.state) / "hetzner.token").exists())
        self.assertEqual(self.step("cleanup", **env).returncode, 0)
        self.assertFalse(Path(self.state).exists())
        self.assertTrue(any(c["argv"][:2] == ["rm", "-f"] for c in self.sudo_calls()))

    def test_cleanup_reports_a_cabin_it_cannot_check_out(self):
        self.set_rules({"match": ["cabin", "ls"], "stdout": cabins_doc("active")}, {"match": ["cabin", "checkout"], "exit": 9})
        self.init()
        (self.fake / "task").write_text(self.task)
        (Path(self.state) / "leasing").touch()
        cleanup = self.step("cleanup", **self.cabin_env())
        self.assertEqual(cleanup.returncode, 1)
        self.assertIn("still bills", cleanup.stderr + cleanup.stdout)

    def test_cleanup_fails_loudly_when_it_cannot_list_cabins(self):
        self.set_rules({"match": ["cabin", "ls"], "exit": 5})
        self.init()
        (Path(self.state) / "leasing").touch()
        cleanup = self.step("cleanup", **self.cabin_env())
        self.assertEqual(cleanup.returncode, 1)
        self.assertFalse(Path(self.state).exists())

    # -- room
    def room_env(self, **extra):
        return {"MODE": "room", "COMMAND": "pwd > marker.txt", "TTL": "1h", "SIZE": "20", "HETZNER_TOKEN": "", **extra}

    def room_rules(self, path, state="active"):
        room = {"id": "gha-room-k3f9", "task": "@TASK@", "state": state, "path": str(path)}
        return [{"match": ["doctor"], "stdout": json.dumps({"ok": True, "checks": []})},
                {"match": ["checkin"], "stdout": json.dumps({"ok": True, "room": room})},
                {"match": ["ls"], "stdout": json.dumps({"ok": True, "rooms": [room]})},
                {"match": ["checkout"], "stdout": json.dumps({"ok": True, "room": room})}]

    @linux_only
    def test_room_runs_the_command_in_the_room_and_checks_it_out(self):
        room_dir = self.tmp / "room"
        room_dir.mkdir()
        self.set_rules(*self.room_rules(room_dir))
        self.init()
        env = self.room_env()
        self.assertEqual(self.step("prepare", **env).returncode, 0)
        self.assertEqual(self.step("run", **env).returncode, 0)
        out = self.outputs()
        self.assertEqual((out["room-id"], out["room-path"], out["exit-code"]), ("gha-room-k3f9", str(room_dir), "0"))
        self.assertEqual((room_dir / "marker.txt").read_text().strip(), str(room_dir.resolve()))
        checkin = next(c["args"] for c in self.motel_calls() if c["args"][0] == "checkin")
        self.assertEqual(checkin, ["checkin", "20", "--for", "1h", "--task", self.task, "--json"])
        self.assertEqual(self.step("cleanup", **env).returncode, 0)
        self.assertIn(["checkout", "gha-room-k3f9", "--now", "--json"], [c["args"] for c in self.motel_calls()])

    def test_room_sync_path_is_copied_in(self):
        room_dir = self.tmp / "room"
        room_dir.mkdir()
        (self.work / "data.txt").write_text("hello")
        self.set_rules(*self.room_rules(room_dir))
        self.init()
        env = self.room_env(COMMAND="cat data.txt > copy.txt", SYNC_PATH=str(self.work))
        self.assertEqual(self.step("run", **env).returncode, 0)
        self.assertEqual((room_dir / "copy.txt").read_text(), "hello")

    def test_keep_room_skips_the_checkout(self):
        room_dir = self.tmp / "room"
        room_dir.mkdir()
        self.set_rules(*self.room_rules(room_dir))
        self.init()
        env = self.room_env(COMMAND="", KEEP_ROOM="true")
        self.assertEqual(self.step("run", **env).returncode, 0)
        self.assertEqual(self.step("cleanup", **env).returncode, 0)
        self.assertNotIn("checkout", [c["args"][0] for c in self.motel_calls()])

    def test_room_checkin_refusal_fails_the_step(self):
        self.set_rules({"match": ["checkin"], "exit": 3, "stdout": '{"ok": false, "error": "no vacancy"}'})
        self.init()
        run = self.step("run", **self.room_env())
        self.assertEqual(run.returncode, 2)
        self.assertIn("no vacancy", run.stderr)

    # -- validation
    def test_bad_inputs_are_rejected_before_motel_runs(self):
        self.init()
        cases = [{"MODE": "fleet"}, {"TTL": "tomorrow"}, {"TTL": "1h; rm -rf /"}, {"SERVER_TYPE": "Big Server"},
                 {"SIZE": "-5"}, {"COMMAND": ""}, {"MOTEL_VERSION": "--upload-pack=x"}, {"KEEP_ROOM": "maybe"}]
        for case in cases:
            with self.subTest(case=case):
                proc = self.step("prepare", **{**self.cabin_env(), **case})
                self.assertEqual(proc.returncode, 2, proc.stdout + proc.stderr)
        self.assertEqual(self.motel_calls(), [])

    def test_cabin_run_needs_a_git_checkout(self):
        self.init()
        plain = self.tmp / "plain"
        plain.mkdir()
        proc = self.step("prepare", **self.cabin_env(SYNC_PATH=str(plain)))
        self.assertEqual(proc.returncode, 2)
        self.assertIn("git checkout", proc.stderr)

    def test_installs_from_the_action_checkout_when_motel_is_missing(self):
        # No motel on PATH: the script runs install.sh from the action's own checkout, then `setup local` for rooms.
        # The fake sudo installs nothing, so the final doctor cannot run and prepare fails; the calls are what we check.
        (self.bin / "motel").unlink()
        self.init()
        src = self.tmp / "src"
        src.mkdir()
        missing = self.step("prepare", **self.room_env(), ACTION_PATH=str(src))
        self.assertEqual(missing.returncode, 2)
        self.assertIn("install.sh", missing.stderr)
        (src / "install.sh").write_text("#!/bin/sh\n")
        (src / "src" / "motel").mkdir(parents=True)
        self.step("prepare", **self.room_env(), ACTION_PATH=str(src))
        user = subprocess.run(["id", "-un"], capture_output=True, text=True, check=True).stdout.strip()
        argvs = [c["argv"] for c in self.sudo_calls()]
        self.assertIn(["sh", str(self.tmp / "staged" / "install.sh"), "--users", user, "--provider", "local"], argvs)
        # the root-owned copy is staged first, the source is never run directly
        self.assertNotIn(["sh", str(src / "install.sh"), "--users", user, "--provider", "local"], argvs)
        self.assertIn(["install", "-o", "root", "-g", "root", "-m", "0644", "/dev/null", str(self.tmp / "staged" / ".motel-action-stage")], argvs)
        self.assertIn(["/fake/motel-desk", "setup", "local"], argvs)

    def test_a_token_selects_the_hetzner_provider_for_the_install(self):
        (self.bin / "motel").unlink()
        self.init()
        src = self.tmp / "src"
        src.mkdir()
        (src / "install.sh").write_text("#!/bin/sh\n")
        (src / "src" / "motel").mkdir(parents=True)
        self.step("prepare", **self.room_env(HETZNER_TOKEN=TOKEN), ACTION_PATH=str(src))
        argvs = [c["argv"] for c in self.sudo_calls()]
        self.assertTrue(any(a[:2] == ["sh", str(self.tmp / "staged" / "install.sh")] and a[-2:] == ["--provider", "hetzner"] for a in argvs))
        self.assertIn(["/fake/motel-desk", "setup", "hetzner", "--token-file"], [a[:4] for a in argvs])

    def stage_checkout(self):
        """A user-owned checkout of this repository with the sloppy modes a runner's checkout can have."""
        checkout = self.tmp / "checkout"
        checkout.mkdir()
        shutil.copy(ROOT / "install.sh", checkout / "install.sh")
        shutil.copytree(ROOT / "src", checkout / "src", ignore=shutil.ignore_patterns("__pycache__"))
        (checkout / "src" / "motel").chmod(0o775)
        (checkout / "src" / "motel" / "guard.py").chmod(0o664)
        return checkout

    @linux_only
    def test_the_staged_copy_passes_the_installers_own_source_audit(self):
        # Real staging commands (cp, rm, chmod, install -d) behind a sudo shim in a temp root; no fake of the audit.
        from motel.installer import audit_source

        (self.bin / "motel").unlink()
        (self.bin / "sudo").write_text(EXEC_SUDO.format(python=sys.executable, src=str(ROOT / "src")))
        checkout = self.stage_checkout()
        stage = self.tmp / "usr" / "local" / "src" / "motel-action"
        self.assertNotEqual([], audit_source(str(checkout / "src" / "motel"), owner_uid=os.getuid(), chain=False),
                            "the control: the raw checkout would be refused")
        self.init()
        env = self.room_env(ACTION_PATH=str(checkout), MOTEL_ACTION_STAGE=str(stage))
        self.step("prepare", **env)
        installs = [c for c in self.sudo_calls() if c["argv"][:1] == ["sh"]]
        self.assertEqual(1, len(installs), self.sudo_calls())
        call = installs[0]
        self.assertEqual(str(stage / "install.sh"), call["argv"][1])
        self.assertEqual([], call["audit"])
        self.assertEqual(0, call["dry_run_rc"], call["dry_run_err"])
        self.assertEqual("0o755", call["stage_mode"])
        self.assertTrue(all(int(m, 8) & 0o022 == 0 for m in call["modes"]), call["modes"])
        self.assertTrue((stage / "src" / "motel" / "guard.py").exists())

    def test_staging_replaces_the_previous_copy_so_reruns_are_safe(self):
        (self.bin / "motel").unlink()
        (self.bin / "sudo").write_text(EXEC_SUDO.format(python=sys.executable, src=str(ROOT / "src")))
        checkout = self.stage_checkout()
        stage = self.tmp / "usr" / "local" / "src" / "motel-action"
        self.init()
        env = self.room_env(ACTION_PATH=str(checkout), MOTEL_ACTION_STAGE=str(stage))
        self.step("prepare", **env)
        (stage / "src" / "motel" / "leftover.py").write_text("x = 1\n")
        (stage / "src" / "motel" / "guard.py").write_text("tampered = True\n")
        self.step("prepare", **env)
        self.assertFalse((stage / "src" / "motel" / "leftover.py").exists())
        self.assertEqual((checkout / "src" / "motel" / "guard.py").read_text(), (stage / "src" / "motel" / "guard.py").read_text())
        audits = [c["audit"] for c in self.sudo_calls() if c["argv"][:1] == ["sh"]]
        self.assertEqual([[], []], audits)

    def exec_sudo(self):
        (self.bin / "motel").unlink()
        (self.bin / "sudo").write_text(EXEC_SUDO.format(python=sys.executable, src=str(ROOT / "src")))
        return self.stage_checkout()

    def stage_with(self, checkout, stage, **extra):
        self.init()
        return self.step("prepare", **self.room_env(ACTION_PATH=str(checkout), MOTEL_ACTION_STAGE=str(stage), **extra))

    def installs(self):
        return [c for c in self.sudo_calls() if c["argv"][:1] == ["sh"]]

    def install_tree(self):
        """A stand-in for /usr/local as GitHub-hosted runners have it: bin world-writable, sbin group-writable, lib missing."""
        local = self.tmp / "usr" / "local"
        for name, mode in (("", 0o755), ("bin", 0o777), ("sbin", 0o775)):
            (local / name).mkdir(parents=True, exist_ok=True)
            (local / name).chmod(mode)
        return local

    def tighten(self, local, **extra):
        checkout = self.exec_sudo()
        dirs = " ".join(str(local / name) for name in ("lib/motel", "sbin", "bin"))
        env = {"MOTEL_ACTION_INSTALL_DIRS": dirs, "MOTEL_ACTION_INSTALL_TOP": str(self.tmp), "MOTEL_ACTION_OWNER_UID": str(os.getuid()), **extra}
        return self.stage_with(checkout, self.tmp / "usr" / "local" / "src" / "motel-action", **env)

    def test_install_directories_that_others_can_write_are_tightened_before_the_installer_runs(self):
        local = self.install_tree()
        proc = self.tighten(local)   # the preflight after the install fails (no motel on the shim PATH); the install step ran
        self.assertEqual([0o755, 0o755, 0o755], [stat.S_IMODE(os.stat(local / name).st_mode) for name in ("", "bin", "sbin")])
        self.assertFalse((local / "lib").exists(), "a directory that does not exist yet is left to the installer")
        argvs = [c["argv"] for c in self.sudo_calls()]
        chmods = [a for a in argvs if a[:2] == ["chmod", "go-w"] and str(local) in a[2]]
        self.assertEqual(sorted(str(local / n) for n in ("bin", "sbin")), sorted(a[2] for a in chmods), "only what was writable")
        first_install = next(i for i, a in enumerate(argvs) if a[:1] == ["sh"])
        self.assertTrue(all(argvs.index(a) < first_install for a in chmods), "tightened before install.sh runs")
        self.assertIn("removing group and other write permission", proc.stdout)
        from motel.installer import audit_chain

        for name in ("bin", "sbin"):   # what the installer checks for each install directory, below the temp root
            self.assertEqual([], [p for p in audit_chain(str(local / name), owner_uid=os.getuid()) if p.startswith(str(local))])

    def test_directories_not_owned_by_root_are_left_for_the_installer_to_refuse(self):
        local = self.install_tree()
        self.tighten(local, MOTEL_ACTION_OWNER_UID=str(os.getuid() + 1))   # not "root": the installer must refuse these itself
        self.assertEqual(0o777, stat.S_IMODE(os.stat(local / "bin").st_mode))
        self.assertEqual([], [c for c in self.sudo_calls() if c["argv"][:2] == ["chmod", "go-w"] and str(local) in c["argv"][2]])

    def test_outside_test_mode_the_stage_must_be_a_motel_directory_directly_under_usr_local_src(self):
        checkout = self.exec_sudo()
        victim = self.work / "motel-stage"        # looks right, is not under /usr/local/src
        victim.mkdir()
        (victim / "precious.txt").write_text("keep")
        cases = {
            "a workspace path": str(victim),
            "a relative path": "motel-stage",
            "/opt": "/opt/motel-stage",
            "not motel-": "/usr/local/src/other",
            "a deeper path": "/usr/local/src/motel-a/b",
            "not normalised": "/usr/local/src/../src/motel-x",
            "trailing slash": "/usr/local/src/motel-x/",
            "double slash": "/usr/local//src/motel-x",
            "the bare prefix": "/usr/local/src/motel-",
        }
        for name, path in cases.items():
            with self.subTest(name):
                proc = self.stage_with(checkout, path, MOTEL_ACTION_TEST_MODE="")
                self.assertEqual(proc.returncode, 2, proc.stderr)
                self.assertIn("stage", proc.stderr)
        self.assertEqual([], self.installs())
        self.assertEqual("keep", (victim / "precious.txt").read_text())
        self.assertFalse([c for c in self.sudo_calls() if c["argv"][:1] in (["rm"], ["cp"])], "nothing was touched")

    def test_the_default_stage_passes_the_path_check_outside_test_mode(self):
        checkout = self.exec_sudo()
        proc = self.stage_with(checkout, "/usr/local/src/motel-action", MOTEL_ACTION_TEST_MODE="")
        self.assertNotIn("stage path", proc.stderr)
        self.assertNotIn("stage directory", proc.stderr)

    def test_a_symlink_as_the_stage_is_refused_and_its_target_is_untouched(self):
        checkout = self.exec_sudo()
        target = self.tmp / "elsewhere"
        target.mkdir()
        (target / ".motel-action-stage").write_text("")
        (target / "precious.txt").write_text("keep")
        stage = self.tmp / "usr" / "local" / "src" / "motel-action"
        stage.parent.mkdir(parents=True)
        os.symlink(target, stage)
        proc = self.stage_with(checkout, stage)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("symbolic link", proc.stderr)
        self.assertEqual("keep", (target / "precious.txt").read_text())
        self.assertEqual([], self.installs())

    def test_a_directory_without_the_marker_is_never_removed(self):
        checkout = self.exec_sudo()
        stage = self.tmp / "usr" / "local" / "src" / "motel-action"
        stage.mkdir(parents=True)
        (stage / "somebody-elses.txt").write_text("keep")
        proc = self.stage_with(checkout, stage)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("marker", proc.stderr)
        self.assertEqual("keep", (stage / "somebody-elses.txt").read_text())
        self.assertEqual([], self.installs())

    def test_a_marker_that_is_a_symlink_does_not_count(self):
        checkout = self.exec_sudo()
        stage = self.tmp / "usr" / "local" / "src" / "motel-action"
        stage.mkdir(parents=True)
        (stage / "real").write_text("")
        os.symlink(stage / "real", stage / ".motel-action-stage")
        proc = self.stage_with(checkout, stage)
        self.assertEqual(proc.returncode, 2)
        self.assertTrue((stage / "real").exists())

    def test_an_existing_parent_keeps_its_mode_and_the_stage_gets_the_marker(self):
        checkout = self.exec_sudo()
        parent = self.tmp / "usr" / "local" / "src"
        parent.mkdir(parents=True)
        parent.chmod(0o711)
        stage = parent / "motel-action"
        self.stage_with(checkout, stage)
        self.assertEqual(0o711, stat.S_IMODE(parent.stat().st_mode))
        self.assertTrue((stage / ".motel-action-stage").is_file())
        parent_calls = [c["argv"] for c in self.sudo_calls() if c["argv"][:2] == ["install", "-d"] and c["argv"][-1] == str(parent)]
        self.assertEqual([], parent_calls, "an existing parent is not re-moded or re-owned")

    def test_a_missing_parent_is_created_root_owned_and_0755(self):
        checkout = self.exec_sudo()
        stage = self.tmp / "usr" / "local" / "src" / "motel-action"
        self.stage_with(checkout, stage)
        self.assertEqual(0o755, stat.S_IMODE(stage.parent.stat().st_mode))
        self.assertEqual(1, len(self.installs()))

    def test_a_symlink_in_the_checkout_stays_visible_to_the_audit(self):
        # Staging does not launder a link: cp -R copies it as a link and the installer's audit refuses it.
        (self.bin / "motel").unlink()
        (self.bin / "sudo").write_text(EXEC_SUDO.format(python=sys.executable, src=str(ROOT / "src")))
        checkout = self.stage_checkout()
        os.symlink("/etc/hostname", checkout / "src" / "motel" / "leak")
        stage = self.tmp / "stage"
        self.init()
        self.step("prepare", **self.room_env(ACTION_PATH=str(checkout), MOTEL_ACTION_STAGE=str(stage)))
        audit = [c["audit"] for c in self.sudo_calls() if c["argv"][:1] == ["sh"]][0]
        self.assertTrue(any("symbolic link" in p for p in audit), audit)


# --- docs ----------------------------------------------------------------------------------------------


class DocsTest(unittest.TestCase):
    def setUp(self):
        self.text = DOC.read_text(encoding="utf-8")

    def test_example_workflows_pin_third_party_actions_by_sha(self):
        for line in self.text.splitlines():
            match = re.match(r"\s*(?:-\s+)?uses:\s*(\S+)", line)
            if match and not match.group(1).startswith("vicennial/motel@"):
                self.assertRegex(line + "\n", SHA_PIN, line)

    def test_example_passes_the_token_as_a_secret_input(self):
        self.assertIn("hetzner-token: ${{ secrets.", self.text)
        self.assertNotRegex(self.text, r"hcloud|HCLOUD_TOKEN=")

    def test_documents_the_prerequisites_and_limits(self):
        for phrase in ("Hetzner Cloud", "passwordless sudo", "rsync", "cabin-run", "room", "if: always()", "keep-room"):
            self.assertIn(phrase, self.text)


if __name__ == "__main__":
    unittest.main()
