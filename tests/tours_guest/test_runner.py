"""The guest runner end to end: a temp git repo, the runner copied beside adapters.py, a fake agent command."""
import json
import os
import sys
import time

import pytest
from guestkit import OAUTH, PLACEHOLDER, Guest, git

from platform_marks import linux_only

REDACTED = "[motel:redacted]"        # the agent's own stdout is redacted, so the placeholder shows up as this


@pytest.fixture
def guest(tmp_path):
    return Guest(tmp_path)


# ------------------------------------------------------------------------------------------------ success

@linux_only
def test_success_end_to_end(guest, tmp_path):
    card = guest.script("""
        import json, os
        open("hello.txt", "w").write("hi\\n")
        os.makedirs("reports", exist_ok=True)
        open("reports/a.json", "w").write('{"ok": true}')
        open("reports/b.txt", "w").write("not declared by the glob")
        open(os.environ["MOTEL_RESULT"], "w").write(json.dumps({"summary": "done"}))
        print(json.dumps({"env": sorted(os.environ), "home": os.environ["HOME"], "path": os.environ["PATH"],
                          "foo": os.environ.get("FOO"), "cwd": os.getcwd()}))
        """, env={"FOO": "bar"}, artifacts=["reports/*.json"])
    res = guest.run(card)
    run = res.run
    assert run["schema"] == "motel/guest-run/v1" and run["attempt"] == "a-1"
    assert run["termination"] == "exited" and run["exit_code"] == 0 and run["command_started"] is True
    assert run["head"] != guest.base and run["base"] == guest.base
    assert sorted(run["changed_files"]) == ["hello.txt", "reports/a.json", "reports/b.txt"]
    assert run["bundle_sha256"] and os.path.getsize(res.out_path("result.bundle")) == run["bundle_bytes"]
    assert [a["path"] for a in run["artifacts"]] == ["reports/a.json"]
    assert run["artifacts"][0]["bytes"] == len('{"ok": true}')
    assert res.read("artifacts", "reports", "a.json") == b'{"ok": true}'
    assert json.loads(res.read("RESULT.json")) == {"summary": "done"} and run["result_json"] is True
    # the bundle really holds base..HEAD on the right commit
    clone = res.bundle_clone(str(tmp_path / "clone"))
    assert git(clone, "rev-parse", "result") == run["head"]
    assert git(clone, "log", "-1", "--format=%an <%ae>", "result") == "motel guest <guest@motel.invalid>"
    assert git(clone, "show", "result:hello.txt") == "hi"
    # the agent saw the fixed environment and nothing of the runner's own
    seen = json.loads(res.read("stdout.log").decode().strip().splitlines()[-1])
    assert "SECRET_CANARY" not in seen["env"] and seen["foo"] == "bar"
    assert seen["home"] == guest.home and seen["path"] == guest.bin + ":/usr/bin:/bin"
    assert os.path.realpath(seen["cwd"]) == os.path.realpath(guest.work)
    assert seen["env"] == sorted({"PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "TERM", "TMPDIR", "CI", "NO_COLOR",
                                  "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "GIT_AUTHOR_NAME", "GIT_AUTHOR_EMAIL",
                                  "GIT_COMMITTER_NAME", "GIT_COMMITTER_EMAIL", "GIT_TERMINAL_PROMPT", "MOTEL_TOUR",
                                  "MOTEL_GUEST", "MOTEL_ATTEMPT", "MOTEL_BASE", "MOTEL_BRANCH", "MOTEL_WORK",
                                  "MOTEL_RESULT", "FOO"})
    assert PLACEHOLDER.encode() not in res.all_bytes()


def test_agent_exit_code_and_no_changes(guest):
    res = guest.run(guest.script("import sys; print('x'); sys.exit(3)"))
    assert res.run["termination"] == "exited" and res.run["exit_code"] == 3
    assert res.run["head"] == guest.base and res.run["bundle_sha256"] is None and not res.exists("result.bundle")
    assert res.run["changed_files"] == []


def test_agent_killed_by_a_signal_is_reported_as_killed(guest):
    res = guest.run(guest.script("import os, signal; os.kill(os.getpid(), signal.SIGKILL)"))
    assert res.run["termination"] == "killed" and res.run["exit_code"] == 137 and res.run["signal"] == 9


def test_overlay_is_its_own_commit_and_the_agent_sees_it(guest):
    patch = ("diff --git a/src/app.txt b/src/app.txt\n--- a/src/app.txt\n+++ b/src/app.txt\n@@ -1 +1,2 @@\n one\n+two\n")
    res = guest.run(guest.script("print(open('src/app.txt').read().split())"), overlay=patch)
    assert res.run["overlay_commit"] and res.run["overlay_commit"] != guest.base
    assert "['one', 'two']" in res.read("stdout.log").decode()
    assert res.run["changed_files"] == ["src/app.txt"] and res.run["head"] == res.run["overlay_commit"]


def test_a_bad_overlay_fails_before_the_agent_starts(guest):
    res = guest.run(guest.script("print('ran')"), overlay="diff --git a/nope b/nope\n--- a/nope\n+++ b/nope\n@@ -1 +1 @@\n-x\n+y\n")
    assert res.run["termination"] == "failed" and res.run["reason"] == "overlay_failed"
    assert res.run["command_started"] is False and not res.exists("stdout.log")


def test_agent_that_moves_to_another_branch_is_still_collected_on_the_card_branch(guest, tmp_path):
    res = guest.run(guest.script("""
        import subprocess
        subprocess.run(["git", "checkout", "-q", "--detach"], check=True)
        open("x.txt", "w").write("x")
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "agent commit"], check=True)
        open("y.txt", "w").write("y")
        """))
    assert sorted(res.run["changed_files"]) == ["x.txt", "y.txt"]
    clone = res.bundle_clone(str(tmp_path / "c"))
    assert git(clone, "show", "result:y.txt") == "y"
    subjects = git(clone, "log", "--format=%s", "result", f"{guest.base}..result").splitlines()
    assert subjects[0].startswith("motel guest fix-auth") and subjects[1] == "agent commit"


def test_result_json_in_the_repo_root_is_copied_and_not_committed(guest):
    res = guest.run(guest.script("""open("RESULT.json", "w").write('{"a": 1}'); open("f.txt", "w").write("f")"""))
    assert json.loads(res.read("RESULT.json")) == {"a": 1}
    assert res.run["changed_files"] == ["f.txt"]


def test_oversized_or_invalid_result_json_is_withheld(guest):
    res = guest.run(guest.script("""open("RESULT.json", "w").write("x" * 70000)"""))
    assert not res.exists("RESULT.json") and {"path": "RESULT.json", "reason": "too_large"} in res.run["rejected"]
    res = guest.run(guest.script("""open("RESULT.json", "w").write("[1, 2]")"""))
    assert not res.exists("RESULT.json") and {"path": "RESULT.json", "reason": "invalid_json"} in res.run["rejected"]


def test_bindings_set_only_the_concierge_pair(guest):
    res = guest.run(guest.script("""
        import json, os
        print(json.dumps({k: v for k, v in os.environ.items() if "KEY" in k or "BASE_URL" in k or "TOKEN" in k}))
        """, bindings=["anthropic", "openai"]))
    seen = json.loads(res.read("stdout.log").decode().strip().splitlines()[-1])
    assert seen == {"ANTHROPIC_BASE_URL": "http://127.0.0.1:17781/u/anthropic", "ANTHROPIC_API_KEY": REDACTED,
                    "OPENAI_BASE_URL": "http://127.0.0.1:17781/u/openai/v1", "OPENAI_API_KEY": REDACTED}
    # the placeholder reached the agent's environment and the redacted log, nowhere else
    assert PLACEHOLDER.encode() not in res.all_bytes()


@pytest.mark.parametrize("name", ["PATH", "HOME", "ANTHROPIC_API_KEY", "OPENAI_BASE_URL", "LD_PRELOAD", "GIT_DIR",
                                  "MOTEL_RESULT", "CLAUDE_CODE_OAUTH_TOKEN", "BASH_FUNC_x%%"])
def test_card_env_cannot_override_reserved_names(guest, name):
    res = guest.run(guest.script("print('ran')", env={name: "evil"}))
    assert res.run["termination"] == "failed" and res.run["reason"] == "invalid_card"
    assert res.run["command_started"] is False


@pytest.mark.parametrize("patch", [{"artifacts": ["../x"]}, {"artifacts": ["/etc/passwd"]}, {"artifacts": [".git/config"]},
                                   {"base": "main"}, {"ttl_s": 0}, {"branch": "-evil"}, {"adapter": "nope"},
                                   {"schema": "other"}, {"max_result_mb": 0}, {"concierge_port": 0}])
def test_invalid_cards_fail_closed(guest, patch):
    res = guest.run(guest.script("print('ran')", **patch))
    assert res.run["termination"] == "failed" and res.run["reason"] == "invalid_card"
    assert not res.exists("stdout.log")


def test_runner_refuses_to_run_as_root_unless_told_otherwise(guest):
    if os.geteuid() != 0:
        pytest.skip("only meaningful as root")
    res = guest.run(guest.script("print(1)"))
    assert res.run["reason"] == "runner_is_root"


# ------------------------------------------------------------------------------------------------ limits

def test_wall_clock_limit_terms_then_kills_the_whole_group(guest):
    marker = os.path.join(guest.tmp, "child.pid")
    code = f"""
        import os, signal, subprocess, sys, time
        child = subprocess.Popen(["sleep", "300"])
        open({marker!r}, "w").write(str(child.pid))
        signal.signal(signal.SIGTERM, signal.SIG_IGN)      # ignores TERM: only KILL stops it
        while True:
            time.sleep(0.1)
        """
    started = time.monotonic()
    res = guest.run(guest.script(code, ttl_s=1), args=["--kill-grace", "0.5"])
    assert time.monotonic() - started < 20
    assert res.run["termination"] == "timeout" and res.run["exit_code"] == 137
    pid = int(open(marker).read())
    for _ in range(50):                     # a killed child may linger as a zombie until its new parent reaps it
        try:
            state = open(f"/proc/{pid}/stat").read().rsplit(")", 1)[1].split()[0]
        except OSError:
            break
        if state == "Z":
            break
        time.sleep(0.1)
    else:
        raise AssertionError("the child in the agent's process group is still running")


def test_term_is_enough_for_a_well_behaved_agent(guest):
    res = guest.run(guest.script("import time; time.sleep(60)", ttl_s=1), args=["--kill-grace", "5"])
    assert res.run["termination"] == "timeout" and res.run["exit_code"] == 143 and res.run["duration_s"] < 10


def test_the_adapter_runs_in_its_own_session_and_process_group(guest):
    res = guest.run(guest.script("""
        import os
        print(os.getpid() == os.getsid(0) == os.getpgid(0))
        """))
    assert res.read("stdout.log").decode().strip() == "True"


def test_runner_term_stops_the_agent_and_still_writes_run_json(guest):
    import signal
    import subprocess
    marker = os.path.join(guest.tmp, "started")
    card = guest.script(f"import time; open({marker!r}, 'w').write('1'); time.sleep(60)", ttl_s=60)
    with open(os.path.join(guest.indir, "card.json"), "w") as handle:
        json.dump(card, handle)
    proc = subprocess.Popen([sys.executable, os.path.join(guest.lib, "runner.py"), "--in", guest.indir, "--home",
                             guest.home, "--path", guest.bin + ":/usr/bin:/bin", "--kill-grace", "2"],
                            stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    for _ in range(100):
        if os.path.exists(marker):
            break
        time.sleep(0.1)
    proc.send_signal(signal.SIGTERM)
    proc.wait(timeout=30)
    with open(os.path.join(guest.out, "run.json")) as handle:
        run = json.load(handle)
    assert run["termination"] == "killed" and proc.returncode == 0


def test_oversized_output_is_capped_head_and_tail(guest):
    res = guest.run(guest.script("""
        import sys
        sys.stdout.write("HEAD-MARK\\n" + "x" * 3_000_000 + "\\nTAIL-MARK\\n")
        sys.stderr.write("e" * 500_000)
        """), args=["--log-cap", "100000"])
    out = res.read("stdout.log")
    assert len(out) <= 100000
    assert out.startswith(b"HEAD-MARK") and out.rstrip().endswith(b"TAIL-MARK") and b"bytes of output omitted" in out
    run = res.run
    assert run["stdout_truncated"] and run["stdout_total_bytes"] > 3_000_000 and run["stdout_bytes"] == len(out)
    assert run["stderr_truncated"] and run["stderr_bytes"] <= 100000


def test_result_over_the_size_cap_is_withheld(guest):
    res = guest.run(guest.script("""
        import os
        open("big.bin", "wb").write(os.urandom(2_000_000))
        """, max_result_mb=1, artifacts=["big.bin"]))
    reasons = sorted(item["reason"] for item in res.run["rejected"])
    assert reasons == ["too_large", "too_large"] and not res.exists("result.bundle") and res.run["artifacts"] == []
    assert res.run["termination"] == "exited"          # too large is a refusal to hand over, not a leak


# ------------------------------------------------------------------------------------------------ artifacts

def test_artifacts_accept_only_regular_files(guest, tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret.txt").write_text("outside data")
    res = guest.run(guest.script(f"""
        import os
        os.makedirs("reports/sub", exist_ok=True)
        open("reports/ok.json", "w").write("{{}}")
        open("reports/sub/deep.json", "w").write("{{}}")
        os.symlink("/etc/hostname", "reports/link.json")
        os.symlink({str(outside)!r}, "evil")
        os.symlink("nowhere", "reports/dangling.json")
        os.mkfifo("reports/pipe.json")
        """, artifacts=["reports/**/*.json", "reports/*.json", "evil/*"]))
    accepted = sorted(a["path"] for a in res.run["artifacts"])
    assert accepted == ["reports/ok.json", "reports/sub/deep.json"]
    rejected = {item["path"]: item["reason"] for item in res.run["rejected"]}
    assert rejected["reports/link.json"] == "symlink" and rejected["reports/dangling.json"] == "symlink"
    assert rejected["reports/pipe.json"] == "special_file" and rejected["evil/secret.txt"] == "symlinked_parent"
    for root, dirs, files in os.walk(res.out_path("artifacts")):
        for name in dirs + files:
            assert not os.path.islink(os.path.join(root, name))
    assert b"outside data" not in res.all_bytes()


# ------------------------------------------------------------------------------------------------ the placeholder

def test_placeholder_is_redacted_from_logs_and_files_and_fails_closed_elsewhere(guest, tmp_path):
    res = guest.run(guest.script("""
        import os, sys
        key = os.environ["ANTHROPIC_API_KEY"]
        print("key is", key)
        print("e:" + key, file=sys.stderr)
        open("notes.txt", "w").write("token=" + key + "\\n")
        open("fine.txt", "w").write("fine\\n")
        """, bindings=["anthropic"]))
    run = res.run
    assert run["termination"] == "exited" and run["redactions"]["stdout"] == 1 and run["redactions"]["stderr"] == 1
    assert b"[motel:redacted]" in res.read("stdout.log") and run["redactions"]["files"] == 1
    clone = res.bundle_clone(str(tmp_path / "c"))
    assert git(clone, "show", "result:notes.txt") == "token=[motel:redacted]"
    assert PLACEHOLDER.encode() not in res.all_bytes() and PLACEHOLDER not in res.text


def test_placeholder_in_an_artifact_fails_the_run(guest):
    res = guest.run(guest.script("""
        import os
        open(".gitignore", "w").write("report.json\\n")       # ignored files are not part of the tree scan
        open("report.json", "w").write(os.environ["ANTHROPIC_API_KEY"])
        open("clean.json", "w").write("{}")
        """, bindings=["anthropic"], artifacts=["*.json"]))
    assert res.run["termination"] == "failed" and res.run["reason"] == "secret_leak"
    assert res.run["command_started"] is True and res.run["exit_code"] == 0
    assert [a["path"] for a in res.run["artifacts"]] == ["clean.json"]
    assert not res.exists("artifacts", "report.json") and PLACEHOLDER.encode() not in res.handed_over()


def test_placeholder_in_a_tracked_artifact_is_redacted_in_place(guest):
    res = guest.run(guest.script("""
        import os
        open("report.json", "w").write('{"k": "' + os.environ["ANTHROPIC_API_KEY"] + '"}')
        """, bindings=["anthropic"], artifacts=["report.json"]))
    assert res.run["termination"] == "exited" and res.run["redactions"]["files"] == 1
    assert res.read("artifacts", "report.json") == b'{"k": "[motel:redacted]"}'


def test_placeholder_in_result_json_fails_the_run(guest):
    res = guest.run(guest.script("""
        import json, os
        open(os.environ["MOTEL_RESULT"], "w").write(json.dumps({"k": os.environ["ANTHROPIC_API_KEY"]}))
        """, bindings=["anthropic"]))
    assert res.run["reason"] == "secret_leak" and not res.exists("RESULT.json")


def test_placeholder_in_agent_commit_history_withholds_the_bundle(guest):
    res = guest.run(guest.script("""
        import os, subprocess
        key = os.environ["ANTHROPIC_API_KEY"]
        open("leak.txt", "w").write(key)
        subprocess.run(["git", "add", "-A"], check=True)
        subprocess.run(["git", "commit", "-q", "-m", "oops"], check=True)
        os.remove("leak.txt")
        open("after.txt", "w").write("after")
        """, bindings=["anthropic"]))
    assert res.run["reason"] == "secret_leak" and res.run["termination"] == "failed"
    assert not res.exists("result.bundle") and res.run["bundle_sha256"] is None
    assert PLACEHOLDER.encode() not in res.handed_over()      # the agent's own .git is not something we hand over


def test_a_placeholder_split_across_read_chunks_is_still_found(guest):
    res = guest.run(guest.script("""
        import os
        pad = "p" * (1 << 20)
        open(".gitignore", "w").write("big.json\\n")
        open("big.json", "w").write(pad[:-10] + os.environ["ANTHROPIC_API_KEY"] + pad)
        """, bindings=["anthropic"], artifacts=["big.json"]))
    assert res.run["reason"] == "secret_leak" and not res.exists("artifacts", "big.json")


# ------------------------------------------------------------------------------------------------ missing CLI

def test_missing_adapter_cli_is_failed_adapter_missing(guest, tmp_path):
    empty = tmp_path / "empty"
    empty.mkdir()
    res = guest.run(guest.card(adapter="claude", prompt="do it", bindings=["anthropic"]),
                    path=f"{empty}:/usr/bin:/bin")
    run = res.run
    assert run["termination"] == "failed" and run["reason"] == "adapter_missing"
    assert run["command_started"] is False and run["exit_code"] == 127
    assert run["head"] == guest.base and not res.exists("stdout.log")


def test_missing_command_binary_is_adapter_missing_too(guest):
    res = guest.run(guest.card(argv=["definitely-not-installed-xyz", "--flag"]))
    assert res.run["reason"] == "adapter_missing" and res.run["command_started"] is False


# ------------------------------------------------------------------------------------------------ built-in adapters

DUMP_CLI = """
    import json, os, sys
    print(json.dumps({"argv": sys.argv[1:], "env": {k: v for k, v in os.environ.items()
                      if k.startswith(("ANTHROPIC", "OPENAI", "CODEX", "CLAUDE", "DISABLE", "DO_NOT", "ENABLE"))}}))
    """


def test_claude_adapter_argv_env_and_sandbox_config(guest):
    guest.fake_cli("claude", DUMP_CLI)
    res = guest.run(guest.card(adapter="claude", prompt="--fix the bug", bindings=["anthropic"], max_turns=7))
    seen = json.loads(res.read("stdout.log").decode().strip().splitlines()[-1])
    assert seen["argv"] == ["-p", "--output-format", "json", "--max-turns", "7", "--no-session-persistence",
                            "--dangerously-skip-permissions", "--", "--fix the bug"]
    env = seen["env"]
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:17781/u/anthropic" and env["ANTHROPIC_API_KEY"] == REDACTED
    assert env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] == "1" and env["DISABLE_TELEMETRY"] == "1"
    assert "OPENAI_API_KEY" not in env and "CLAUDE_CODE_OAUTH_TOKEN" not in env
    settings = json.load(open(os.path.join(guest.home, ".claude", "settings.json")))
    assert settings["permissions"]["defaultMode"] == "bypassPermissions"
    assert json.load(open(os.path.join(guest.home, ".claude.json")))["hasCompletedOnboarding"] is True
    assert PLACEHOLDER.encode() not in open(os.path.join(guest.home, ".claude", "settings.json"), "rb").read()


def test_claude_adapter_needs_the_anthropic_binding(guest):
    guest.fake_cli("claude", DUMP_CLI)
    res = guest.run(guest.card(adapter="claude", prompt="x", bindings=[]))
    assert res.run["reason"] == "invalid_card"


def test_codex_adapter_argv_env_and_config(guest):
    guest.fake_cli("codex", DUMP_CLI)
    res = guest.run(guest.card(adapter="codex", prompt="do it", bindings=["openai"]))
    seen = json.loads(res.read("stdout.log").decode().strip().splitlines()[-1])
    assert seen["argv"] == ["exec", "--json", "--skip-git-repo-check", "--ephemeral",
                            "--dangerously-bypass-approvals-and-sandbox", "--", "do it"]
    assert seen["env"]["OPENAI_BASE_URL"] == "http://127.0.0.1:17781/u/openai/v1"
    assert seen["env"]["OPENAI_API_KEY"] == REDACTED and seen["env"]["CODEX_HOME"] == os.path.join(guest.home, ".codex")
    config = open(os.path.join(guest.home, ".codex", "config.toml")).read()
    assert 'base_url = "http://127.0.0.1:17781/u/openai/v1"' in config and 'env_key = "OPENAI_API_KEY"' in config
    assert 'wire_api = "responses"' in config and PLACEHOLDER not in config


def test_unknown_binding_gets_a_generic_pair(guest):
    res = guest.run(guest.script("""
        import json, os
        print(json.dumps({k: v for k, v in os.environ.items() if k.startswith("MOTEL_BINDING")}))
        """, bindings=["github"]))
    seen = json.loads(res.read("stdout.log").decode().strip().splitlines()[-1])
    assert seen == {"MOTEL_BINDING_GITHUB_URL": "http://127.0.0.1:17781/u/github", "MOTEL_BINDING_GITHUB_TOKEN": REDACTED}


# ------------------------------------------------------------------------------------------------ own-login

OWN_CLI = """
    import json, os, sys
    mode = os.environ.get("FAKE_MODE", "")
    token = os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", "")
    print(json.dumps({"argv": sys.argv[1:], "has_token": bool(token),
                      "anthropic_vars": sorted(k for k in os.environ if k.startswith("ANTHROPIC_")),
                      "traffic": os.environ.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC")}))
    if mode == "print":
        print("my token is " + token)
        print("err " + token, file=sys.stderr)
    if mode == "artifact":
        open("out.json", "w").write(json.dumps({"t": token}))
    if mode == "repo":
        open("notes.txt", "w").write(token)
    if mode == "result":
        open(os.environ["MOTEL_RESULT"], "w").write(json.dumps({"t": token}))
    """


def own(guest, mode="", stdin=None, **over):
    stdin = (OAUTH + "\n").encode() if stdin is None else stdin
    guest.fake_cli("claude", OWN_CLI)
    card = guest.card(adapter="claude", prompt="work", auth="own-login", bindings=[], placeholder="",
                      env={"FAKE_MODE": mode}, artifacts=["out.json"], **over)
    card.pop("concierge_port", None)
    return guest.run(card, stdin=stdin)


def test_own_login_token_reaches_only_the_adapter_environment(guest):
    res = own(guest)
    seen = json.loads(res.read("stdout.log").decode().strip().splitlines()[-1])
    assert seen["has_token"] is True and seen["anthropic_vars"] == [] and seen["traffic"] == "1"
    assert res.run["termination"] == "exited" and res.run["auth"] == "own-login"
    assert OAUTH.encode() not in res.all_bytes()          # not on disk, not in run.json, not in any output
    assert not os.path.exists(os.path.join(guest.home, ".claude", ".credentials.json"))
    assert OAUTH not in res.proc.stdout.decode() + res.proc.stderr.decode()


def test_own_login_token_is_not_in_the_adapter_argv(guest):
    res = own(guest)
    seen = json.loads(res.read("stdout.log").decode().strip().splitlines()[-1])
    assert OAUTH not in " ".join(seen["argv"])


def test_own_login_token_is_redacted_from_captured_output(guest):
    res = own(guest, "print")
    assert OAUTH.encode() not in res.read("stdout.log") and OAUTH.encode() not in res.read("stderr.log")
    assert b"my token is [motel:redacted]" in res.read("stdout.log")
    assert res.run["redactions"]["stdout"] == 1 and res.run["redactions"]["stderr"] == 1
    assert res.run["termination"] == "exited"
    assert OAUTH.encode() not in res.all_bytes()


def test_own_login_token_in_an_artifact_fails_the_run(guest):
    res = own(guest, "artifact")
    assert res.run["termination"] == "failed" and res.run["reason"] == "secret_leak"
    assert not res.exists("artifacts", "out.json") and res.run["artifacts"] == []
    assert OAUTH.encode() not in res.handed_over()


def test_own_login_token_in_the_repository_withholds_the_bundle(guest):
    res = own(guest, "repo")
    assert res.run["reason"] == "secret_leak" and not res.exists("result.bundle")
    assert OAUTH.encode() not in open(os.path.join(guest.out, "run.json"), "rb").read()


def test_own_login_token_in_result_json_fails_the_run(guest):
    res = own(guest, "result")
    assert res.run["reason"] == "secret_leak" and not res.exists("RESULT.json")


def test_own_login_without_a_token_fails_before_anything_runs(guest):
    for stdin in (b"", b"\n", b"   \n"):
        res = own(guest, stdin=stdin)
        assert res.run["termination"] == "failed" and res.run["reason"] == "own_login_missing_token"
        assert res.run["command_started"] is False and not res.exists("stdout.log")


def test_own_login_rejects_an_oversized_or_odd_token_line(guest):
    res = own(guest, stdin=b"a" * 5000 + b"\n")
    assert res.run["reason"] == "own_login_invalid_token"
    res = own(guest, stdin=b"has space in it here\n")
    assert res.run["reason"] == "own_login_invalid_token"


def test_own_login_reads_exactly_one_line(guest):
    res = own(guest, "print", stdin=(OAUTH + "\nsecond line is ignored\n").encode())
    assert res.run["termination"] == "exited" and b"second line" not in res.all_bytes()


def test_own_login_is_only_for_the_claude_adapter_and_excludes_the_anthropic_binding(guest):
    res = guest.run(guest.script("print(1)", auth="own-login"), stdin=(OAUTH + "\n").encode())
    assert res.run["reason"] == "invalid_card"
    guest.fake_cli("claude", OWN_CLI)
    res = guest.run(guest.card(adapter="claude", prompt="x", auth="own-login", bindings=["anthropic"]),
                    stdin=(OAUTH + "\n").encode())
    assert res.run["reason"] == "invalid_card"


def test_api_key_mode_ignores_stdin(guest):
    res = guest.run(guest.script("""
        import os
        print("CLAUDE_CODE_OAUTH_TOKEN" in os.environ)
        """), stdin=(OAUTH + "\n").encode())
    assert res.read("stdout.log").decode().strip() == "False"


# ------------------------------------------------------------------------------------------------ concierge refusals

def test_concierge_refusals_are_recorded_in_run_json(guest):
    res = guest.run(guest.script("""
        import json, sys
        budget = {"type": "error", "error": {"type": "motel_budget_exceeded", "message": "over budget"}}
        busy = {"type": "error", "error": {"type": "motel_concurrency", "message": "slow down"}}
        print("API Error: 403 " + json.dumps(budget))
        print("API Error: 429 " + json.dumps(busy))
        print("API Error: 429 " + json.dumps(busy), file=sys.stderr)
        # the CLI's json mode embeds the body as an escaped string inside its own JSON
        print(json.dumps({"is_error": True, "result": "API Error: 403 " + json.dumps(budget)}))
        print("429 " + json.dumps(busy))
        print("an unrelated error: " + json.dumps({"type": "error", "error": {"type": "overloaded_error"}}))
        """))
    assert res.run["termination"] == "exited"
    assert res.run["concierge_refusals"] == [{"type": "motel_budget_exceeded", "status": 403, "count": 2},
                                              {"type": "motel_concurrency", "status": 429, "count": 3}]


def refusal_scanner():
    import importlib.util

    from guestkit import PKG
    spec = importlib.util.spec_from_file_location("adapters_under_test", os.path.join(PKG, "adapters.py"))
    module = importlib.util.module_from_spec(spec)
    sys.modules["adapters_under_test"] = module
    spec.loader.exec_module(module)
    return module.RefusalScanner()


def test_a_refusal_body_split_across_reads_is_counted_once_at_every_split_point():
    text = b'noise 429 {"type": "error", "error": {"type": "motel_concurrency", "message": "slow down"}} more\n'
    for cut in range(1, len(text)):
        scanner = refusal_scanner()
        scanner.feed(text[:cut])
        scanner.feed(text[cut:])
        assert scanner.result() == [{"type": "motel_concurrency", "status": 429, "count": 1}], cut
    for size in (1, 3, 7, 50):
        scanner = refusal_scanner()
        for i in range(0, len(text), size):
            scanner.feed(text[i:i + size])
        assert scanner.result() == [{"type": "motel_concurrency", "status": 429, "count": 1}], size


def test_no_refusals_is_an_empty_list_and_a_missing_cli_has_the_field_too(guest, tmp_path):
    assert guest.run(guest.script("print('fine')")).run["concierge_refusals"] == []
    empty = tmp_path / "empty"
    empty.mkdir()
    res = guest.run(guest.card(adapter="claude", prompt="x", bindings=["anthropic"]), path=f"{empty}:/usr/bin:/bin")
    assert res.run["concierge_refusals"] == []


def test_refusals_beyond_the_log_cap_are_still_counted(guest):
    res = guest.run(guest.script("""
        import json
        body = json.dumps({"type": "error", "error": {"type": "motel_budget_exceeded"}})
        print("x" * 500000)
        print("403 " + body)
        print("y" * 500000)
        """), args=["--log-cap", "20000"])
    assert res.run["stdout_truncated"] is True
    assert res.run["concierge_refusals"] == [{"type": "motel_budget_exceeded", "status": 403, "count": 1}]
