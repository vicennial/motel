"""Review round: stragglers, planted files in out/, clean-repo verification, RESULT.json, redaction before the cap."""
import importlib.util
import json
import os
import signal
import subprocess
import sys
import time

import pytest
from guestkit import OAUTH, PKG, Guest, Result, git

from platform_marks import linux_only


@pytest.fixture
def guest(tmp_path):
    return Guest(tmp_path)


@pytest.fixture(scope="module")
def runner_module():
    spec = importlib.util.spec_from_file_location("runner_under_test", os.path.join(PKG, "runner.py"))
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def alive(pid):
    """True when the process exists and is not a zombie."""
    try:
        with open(f"/proc/{pid}/stat") as handle:
            return handle.read().rsplit(")", 1)[1].split()[0] != "Z"
    except OSError:
        return False


# ------------------------------------------------------------------------------------------------ H1 stragglers

STRAGGLER = r'''
import json, os, sys, time
os.makedirs(".github/workflows", exist_ok=True)
open(".github/workflows/ci.yml", "w").write("on: push\n")
open("README.md", "a").write("docs tweak\n")
if os.fork() == 0:
    os.setsid()
    null = os.open("/dev/null", os.O_RDWR)
    for fd in (0, 1, 2):
        os.dup2(null, fd)
    home = os.environ["HOME"]
    open(os.path.join(home, "straggler.pid"), "w").write(str(os.getpid()))
    run = os.path.join(home, "out", "run.json")
    for _ in range(600):
        time.sleep(0.05)
        try:
            doc = json.load(open(run))
        except Exception:
            continue
        doc.update(exit_code=0, termination="exited", changed_files=["README.md"])
        open(run + ".x", "w").write(json.dumps(doc))
        os.replace(run + ".x", run)
        open(os.path.join(home, "straggler-was-here"), "w").write("yes")
        break
    time.sleep(30)
    os._exit(0)
time.sleep(0.3)          # let the child record its pid
sys.exit(3)
'''


@linux_only
def test_r1_setsid_straggler_is_killed_and_cannot_rewrite_run_json(guest):
    res = guest.run(guest.script(STRAGGLER))
    wrote = res.text
    assert res.run["exit_code"] == 3 and res.run["termination"] == "exited"
    assert sorted(res.run["changed_files"]) == [".github/workflows/ci.yml", "README.md"]
    assert res.run["stragglers_killed"] >= 1
    pid = int(open(os.path.join(guest.home, "straggler.pid")).read())
    assert not alive(pid)
    time.sleep(1.0)
    with open(os.path.join(guest.out, "run.json")) as handle:
        assert handle.read() == wrote
    assert not os.path.exists(os.path.join(guest.home, "straggler-was-here"))


def test_stragglers_after_a_timeout_are_killed_too(guest):
    res = guest.run(guest.script(r'''
        import os, time
        if os.fork() == 0:
            os.setsid()
            open(os.path.join(os.environ["HOME"], "late.pid"), "w").write(str(os.getpid()))
            time.sleep(60)
            os._exit(0)
        time.sleep(60)
        ''', ttl_s=2), args=("--kill-grace", "0.5"))
    assert res.run["termination"] == "timeout"
    assert not alive(int(open(os.path.join(guest.home, "late.pid")).read()))


def test_entries_the_agent_put_into_out_are_removed(guest):
    elsewhere = os.path.join(guest.tmp, "elsewhere")
    res = guest.run(guest.script('''
        import os
        out = os.path.join(os.environ["HOME"], "out")
        open(os.path.join(out, "evil.txt"), "w").write("x")
        open(os.path.join(out, "RESULT.json"), "w").write("{}")
        open(os.path.join(out, "result.bundle"), "w").write("junk")
        os.makedirs(os.path.join(out, "artifacts", "sneaky"))
        open(os.path.join(out, "artifacts", "sneaky", "y"), "w").write("y")
        os.makedirs(os.path.join(out, "locked"))
        open(os.path.join(out, "locked", "z"), "w").write("z")
        os.chmod(os.path.join(out, "locked"), 0)
        open(os.path.join(out, "stdout.log"), "w").write("forged log")
        print("real output")
        '''))
    names = sorted(os.listdir(guest.out))
    assert names == ["artifacts", "run.json", "stderr.log", "stdout.log"], names
    assert os.listdir(os.path.join(guest.out, "artifacts")) == []
    assert res.read("stdout.log").decode().strip() == "real output"
    assert res.run["result_json"] is False and res.run["bundle_sha256"] is None
    assert not os.path.exists(elsewhere)


def test_out_replaced_by_a_symlink_is_replaced_by_a_real_directory(guest):
    elsewhere = os.path.join(guest.tmp, "elsewhere")
    os.makedirs(elsewhere)
    res = guest.run(guest.script(f'''
        import os
        out = os.path.join(os.environ["HOME"], "out")
        os.rename(out, out + ".bak")
        os.symlink({elsewhere!r}, out)
        '''))
    assert not os.path.islink(guest.out) and os.path.isfile(os.path.join(guest.out, "run.json"))
    assert os.listdir(elsewhere) == []
    assert res.run["termination"] == "exited"


def test_sweep_uses_kill_minus_one_only_for_the_guest_account(runner_module):
    me = os.getpid()
    live = {500: (1, "S", 4242), 501: (me, "S", 4242), 502: (501, "S", 4242), 503: (1, "S", 7)}
    calls = []

    def fake_kill(pid, sig):
        calls.append((pid, sig))
        if pid == -1:
            for k in [k for k, v in live.items() if v[2] == 4242]:
                del live[k]

    killed, left = runner_module.sweep_stragglers(me, uid=4242, bound_s=2, table=lambda: dict(live), kill=fake_kill,
                                                  reap=lambda: None)
    assert left == 0 and killed >= 3
    assert (-1, signal.SIGKILL) in calls and 503 in live        # another user's process is not touched
    # without a guest uid only the descendants are targeted, never kill(-1)
    live.update({600: (me, "S", 1), 601: (600, "S", 1), 602: (1, "S", 1)})
    calls.clear()

    def fake_kill2(pid, sig):
        calls.append((pid, sig))
        live.pop(pid, None)

    killed, left = runner_module.sweep_stragglers(me, uid=None, bound_s=2, table=lambda: dict(live), kill=fake_kill2,
                                                  reap=lambda: None)
    assert left == 0 and {pid for pid, _ in calls} == {600, 601} and 602 in live
    assert all(pid != -1 for pid, _ in calls)


def test_sweep_gives_up_after_its_bound_and_reports_what_is_left(runner_module):
    me = os.getpid()
    stuck = {700: (me, "D", 1)}
    killed, left = runner_module.sweep_stragglers(me, uid=None, bound_s=0.3, table=lambda: dict(stuck),
                                                  kill=lambda pid, sig: None, reap=lambda: None)
    assert left == 1 and killed == 1


# ------------------------------------------------------------------------------------------------ M3 redaction

OWN = {"adapter": "claude", "auth": "own-login", "bindings": [], "placeholder": ""}


def own_login(guest, source, **over):
    guest.fake_cli("claude", source)
    return guest.run(guest.card(prompt="go", **{**OWN, **over}), stdin=(OAUTH + "\n").encode())


def test_r4_token_cut_by_the_log_cap_is_redacted_whole(guest, runner_module):
    cap = 4096
    guest.fake_cli("claude", r'''
        import os, sys
        MARKER_RESERVE = @RES@
        tok = os.environ["CLAUDE_CODE_OAUTH_TOKEN"].encode()
        half = len(tok) // 2
        head = tail = (4096 - MARKER_RESERVE) // 2
        w = sys.stdout.buffer
        w.write(b"A" * (head - half) + tok)
        w.write(b"B" * 50000)
        w.write(tok + b"C" * (tail - (len(tok) - half)))
        w.flush()
        '''.replace("@RES@", str(runner_module.MARKER_RESERVE)))
    res = guest.run(guest.card(prompt="go", **OWN), stdin=(OAUTH + "\n").encode(), args=("--log-cap", str(cap)))
    log = res.read("stdout.log")
    tok = OAUTH.encode()
    half = len(tok) // 2
    assert tok not in log and tok[:half] not in log and tok[half:] not in log
    assert b"[motel:redacted]" in log and res.run["redactions"]["stdout"] == 2 and res.run["stdout_truncated"] is True
    assert len(log) <= cap


def test_token_split_across_reads_is_redacted(guest):
    res = own_login(guest, r'''
        import os, sys, time
        tok = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        for i in range(0, len(tok), 5):
            sys.stdout.write(tok[i:i + 5]); sys.stdout.flush(); time.sleep(0.01)
        sys.stderr.write("err " + tok[:9]); sys.stderr.flush(); time.sleep(0.05)
        sys.stderr.write(tok[9:] + "\n")
        ''')
    assert OAUTH.encode() not in res.handed_over()
    assert res.read("stdout.log") == b"[motel:redacted]"
    assert res.read("stderr.log") == b"err [motel:redacted]\n"
    assert res.run["redactions"]["stdout"] == 1 and res.run["redactions"]["stderr"] == 1


def test_partial_token_at_the_end_of_the_stream_is_redacted(guest):
    res = own_login(guest, r'''
        import os, sys
        tok = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        sys.stdout.write("cut off: " + tok[:25])
        ''')
    log = res.read("stdout.log")
    assert OAUTH[:25].encode() not in log and OAUTH[8:25].encode() not in log
    assert log == b"cut off: [motel:redacted]"


def test_stream_redactor_is_independent_of_read_sizes(runner_module):
    tokens = [b"sk-ant-oat01-SECRETSECRETSECRET", b"mtl_placeholder_0123456789"]
    text = b"x" * 7 + tokens[0] + b" and " + tokens[1] + b"," + tokens[0] + b" end"
    want = text.replace(tokens[0], runner_module.REDACTED).replace(tokens[1], runner_module.REDACTED)
    for size in (1, 2, 3, 5, 7, 11, 16, 29, 64, 1000):
        red = runner_module.StreamRedactor(tokens)
        out = b"".join(red.push(text[i:i + size]) for i in range(0, len(text), size)) + red.flush()
        assert out == want, size
        assert red.count == 3


# ------------------------------------------------------------------------------------------------ M1 bundle leak check

HISTORY_AGENT = r'''
import json, os, subprocess, sys
tok = os.environ["R3_LEAK"]
mode = os.environ["R3_MODE"]
env = dict(os.environ, GIT_CONFIG_GLOBAL="/dev/null", GIT_CONFIG_SYSTEM="/dev/null")
def git(*a, **kw):
    return subprocess.run(["git", "-c", "commit.gpgsign=false", *a], check=True, capture_output=True, text=True,
                          env=kw.pop("env", env), **kw).stdout.strip()
if mode == "committer":
    open("notes.txt", "w").write("x\n")
    git("add", "notes.txt")
    git("commit", "-q", "-m", "notes", env=dict(env, GIT_COMMITTER_NAME=tok))
elif mode == "merges":
    base = git("rev-parse", "HEAD")
    git("checkout", "-q", "-b", "side")
    open("side.txt", "w").write("side\n"); git("add", "side.txt"); git("commit", "-q", "-m", "side")
    git("checkout", "-q", "-")
    git("merge", "-q", "--no-ff", "--no-commit", "side")
    open("leak.txt", "w").write(tok + "\n"); git("add", "leak.txt")
    git("commit", "-q", "-m", "merge side")
    git("checkout", "-q", "-b", "side2", base)
    open("side2.txt", "w").write("side2\n"); git("add", "side2.txt"); git("commit", "-q", "-m", "side2")
    git("checkout", "-q", "-")
    git("merge", "-q", "--no-ff", "--no-commit", "side2")
    git("rm", "-q", "leak.txt")
    git("commit", "-q", "-m", "merge side2")
elif mode == "replace":
    open("leak.txt", "w").write(tok + "\n"); git("add", "leak.txt"); git("commit", "-q", "-m", "add file")
    bad = git("rev-parse", "HEAD")
    open("leak.txt", "w").write("harmless\n"); git("add", "leak.txt"); git("commit", "-q", "--amend", "-m", "add file")
    good = git("rev-parse", "HEAD")
    git("reset", "-q", "--hard", bad)
    git("replace", bad, good)
    os.remove("leak.txt")
    git("rm", "-q", "--cached", "leak.txt")
    open("leak.txt", "w").write("harmless\n")
elif mode == "textconv":
    with open(".git/config", "a") as fh:
        fh.write('[diff "hide"]\n\ttextconv = sed s/sk-ant-oat01-[A-Za-z0-9]*/clean/\n')
    open(".gitattributes", "w").write("*.txt diff=hide\n")
    open("leak.txt", "w").write(tok + "\n"); git("add", "-A"); git("commit", "-q", "-m", "add")
    open("leak.txt", "w").write("now clean\n"); git("add", "-A"); git("commit", "-q", "-m", "clean")
print(json.dumps({"type": "result", "result": "ok"}))
'''


def history_run(guest, mode, leak):
    guest.fake_cli("claude", HISTORY_AGENT)
    card = guest.card(prompt="go", env={"R3_MODE": mode, "R3_LEAK": leak}, **OWN)
    return guest.run(card, stdin=(OAUTH + "\n").encode())


@pytest.mark.parametrize("mode", ["committer", "merges", "replace", "textconv"])
def test_r3_token_hidden_in_history_withholds_the_bundle(guest, mode):
    res = history_run(guest, mode, OAUTH)
    assert res.run["termination"] == "failed" and res.run["reason"] == "secret_leak"
    assert {"path": "result.bundle", "reason": "secret_leak"} in res.run["rejected"]
    assert not res.exists("result.bundle") and res.run["bundle_sha256"] is None
    assert OAUTH.encode() not in res.handed_over()


@pytest.mark.parametrize("mode", ["committer", "merges", "replace", "textconv"])
def test_r3_same_history_without_the_token_is_handed_over(guest, mode, tmp_path):
    res = history_run(guest, mode, "harmless")
    assert res.run["termination"] == "exited" and not res.run["rejected"]
    clone = res.bundle_clone(str(tmp_path / "clone"))
    assert git(clone, "rev-parse", "result") == res.run["head"]
    assert res.run["changed_files"], mode


def test_changed_files_lists_both_sides_of_a_rename(guest):
    res = guest.run(guest.script('''
        import os
        os.makedirs(".github/workflows", exist_ok=True)
        os.rename("README.md", ".github/workflows/README.md")
        '''))
    assert sorted(res.run["changed_files"]) == [".github/workflows/README.md", "README.md"]


def test_an_agent_repo_config_cannot_change_how_the_runner_commits(guest, tmp_path):
    marker = os.path.join(guest.tmp, "hook-ran")
    res = guest.run(guest.script(f'''
        import os
        with open(".git/config", "a") as fh:
            fh.write('[core]\\n\\tfsmonitor = touch {marker}\\n[filter "x"]\\n\\tclean = touch {marker}\\n\\trequired = true\\n')
        os.makedirs(".git/hooks", exist_ok=True)
        for hook in ("pre-commit", "commit-msg", "post-commit", "pre-push"):
            with open(".git/hooks/" + hook, "w") as fh:
                fh.write("#!/bin/sh\\ntouch {marker}\\n")
            os.chmod(".git/hooks/" + hook, 0o755)
        open(".gitattributes", "w").write("*.txt filter=x\\n")
        open("a.txt", "w").write("a")
        '''))
    assert res.run["termination"] == "exited" and res.run["changed_files"] == [".gitattributes", "a.txt"]
    assert not os.path.exists(marker)


# ------------------------------------------------------------------------------------------------ M2 RESULT.json

def test_r2_result_json_planted_in_out_is_never_handed_over(guest):
    guest.fake_cli("claude", r'''
        import json, os
        tok = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        open(os.path.join(os.environ["HOME"], "out", "RESULT.json"), "w").write(json.dumps({"note": tok}))
        ''')
    res = guest.run(guest.card(prompt="go", **OWN), stdin=(OAUTH + "\n").encode())
    assert not res.exists("RESULT.json") and res.run["result_json"] is False
    assert OAUTH.encode() not in res.handed_over()


def test_r2_json_escaped_token_in_result_json_is_withheld(guest):
    guest.fake_cli("claude", r'''
        import os
        tok = os.environ["CLAUDE_CODE_OAUTH_TOKEN"]
        esc = "".join("\\u%04x" % ord(c) for c in tok[:2]) + tok[2:]
        open(os.environ["MOTEL_RESULT"], "w").write('{"note": "%s"}' % esc)
        ''')
    res = guest.run(guest.card(prompt="go", **OWN), stdin=(OAUTH + "\n").encode())
    assert not res.exists("RESULT.json") and res.run["result_json"] is False
    assert {"path": "RESULT.json", "reason": "secret_leak"} in res.run["rejected"]
    assert res.run["termination"] == "failed" and res.run["reason"] == "secret_leak"
    assert OAUTH.encode() not in res.handed_over()


def test_result_json_is_handed_over_in_canonical_form_only_when_it_passed(guest):
    res = guest.run(guest.script('''
        import os
        open(os.environ["MOTEL_RESULT"], "w").write('{ "b": 1,\\n "a": "caf\\u00e9 \\\\u00e9", "a": "last", "c": [1.5, true, null] }')
        '''))
    assert res.run["result_json"] is True
    assert res.read("RESULT.json") == b'{"a":"last","b":1,"c":[1.5,true,null]}'


@pytest.mark.parametrize("text", ["[1, 2]", "{\"a\": NaN}", "{\"a\": 1e999}", "not json", "\"x\""])
def test_result_json_that_is_not_a_plain_object_is_withheld(guest, text):
    res = guest.run(guest.script(f'''
        import os
        open(os.environ["MOTEL_RESULT"], "w").write({text!r})
        '''))
    assert res.run["result_json"] is False and not res.exists("RESULT.json")
    assert {"path": "RESULT.json", "reason": "invalid_json"} in res.run["rejected"]


# ------------------------------------------------------------------------------------------------ shared constants

def test_log_cap_default_is_the_shared_constant(guest):
    from motel.tours import guest as package
    assert package.LOG_CAP_BYTES == 1024 * 1024
    res = guest.run(guest.script('''
        import sys
        sys.stdout.write("o" * 3_000_000)
        sys.stderr.write("e" * 2_000_000)
        '''))
    for name in ("stdout.log", "stderr.log"):
        size = len(res.read(name))
        assert package.LOG_CAP_BYTES - 200 < size <= package.LOG_CAP_BYTES, size
    assert res.run["stdout_truncated"] and res.run["stderr_truncated"]
    assert res.run["stdout_total_bytes"] == 3_000_000


@pytest.mark.parametrize("name", ["GH_TOKEN", "AWS_REGION", "SSH_AUTH_SOCK", "http_proxy", "NODE_OPTIONS", "GIT_DIR",
                                  "GOOGLE_APPLICATION_CREDENTIALS", "PYTHONPATH", "SUDO_ASKPASS", "BASH_ENV",
                                  "CLAUDE_CODE_SUBPROCESS_ENV_SCRUB", "Ld_Preload"])
def test_reserved_env_names_are_refused_by_the_runner(guest, name):
    res = guest.run(guest.script("pass", env={name: "x"}))
    assert res.run["termination"] == "failed" and res.run["reason"] == "invalid_card"


def test_runner_reserves_everything_the_engines_spec_reserves(runner_module):
    spec = pytest.importorskip("motel.tours.spec")
    from motel.tours import guest as package
    for prefix in spec.RESERVED_ENV_PREFIXES:
        assert package.env_name_reserved(prefix + "X"), prefix
    for name in spec.RESERVED_ENV_NAMES:
        assert package.env_name_reserved(name), name


def test_unreserved_names_still_work(guest):
    res = guest.run(guest.script("import os; print(os.environ['FEATURE_FLAG'], os.environ['TZ'])",
                                 env={"FEATURE_FLAG": "on", "TZ": "UTC"}))
    assert res.run["termination"] == "exited" and res.read("stdout.log") == b"on UTC\n"


# ------------------------------------------------------------------------------------------------ claude env

def printed_env(guest, **over):
    guest.fake_cli("claude", r'''
        import json, os
        print(json.dumps({k: v for k, v in os.environ.items() if k.startswith("CLAUDE_CODE_") or k == "ANTHROPIC_BASE_URL"}))
        ''')
    card = guest.card(adapter="claude", prompt="go", bindings=["anthropic"], **over)
    res = guest.run(card)
    return res, json.loads(res.read("stdout.log").decode().strip().splitlines()[-1])


def test_claude_behind_the_concierge_gets_no_betas_and_a_bounded_output(guest):
    res, env = printed_env(guest)
    assert res.run["termination"] == "exited"
    assert env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] == "1"
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == "8000"
    assert env["ANTHROPIC_BASE_URL"] == "http://127.0.0.1:17781/u/anthropic"


@pytest.mark.parametrize("value", [256, 4096, 128_000])
def test_max_output_tokens_comes_from_the_card(guest, value):
    _, env = printed_env(guest, max_output_tokens=value)
    assert env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] == str(value)


@pytest.mark.parametrize("value", [0, -1, 255, 128_001, "8000", True, 1.5])
def test_bad_max_output_tokens_is_an_invalid_card(guest, value):
    guest.fake_cli("claude", "pass")
    res = guest.run(guest.card(adapter="claude", prompt="go", bindings=["anthropic"], max_output_tokens=value))
    assert res.run["termination"] == "failed" and res.run["reason"] == "invalid_card"


def test_own_login_claude_sets_neither_concierge_variable(guest):
    guest.fake_cli("claude", r'''
        import json, os
        print(json.dumps(sorted(k for k in os.environ if k.startswith("CLAUDE_CODE_"))))
        ''')
    res = guest.run(guest.card(prompt="go", **OWN), stdin=(OAUTH + "\n").encode())
    names = json.loads(res.read("stdout.log").decode())
    assert "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS" not in names and "CLAUDE_CODE_MAX_OUTPUT_TOKENS" not in names
    assert "CLAUDE_CODE_OAUTH_TOKEN" in names


def test_codex_is_unchanged_by_the_claude_variables(guest):
    guest.fake_cli("codex", r'''
        import json, os
        print(json.dumps(sorted(k for k in os.environ if k.startswith(("CLAUDE_", "OPENAI_")))))
        ''')
    res = guest.run(guest.card(adapter="codex", prompt="go", bindings=["openai"]))
    assert json.loads(res.read("stdout.log").decode()) == ["OPENAI_API_KEY", "OPENAI_BASE_URL"]


# ------------------------------------------------------------------------------------------------ round 2

DEEP = r'''
import os
fd = os.open(os.path.join(os.environ["HOME"], "out"), os.O_RDONLY)
for _ in range(2500):
    os.mkdir("d", dir_fd=fd)
    n = os.open("d", os.O_RDONLY, dir_fd=fd)
    os.close(fd)
    fd = n
'''


def build_chain(base, depth):
    """A directory chain far deeper than PATH_MAX, built relative to descriptors."""
    os.makedirs(base)
    fd = os.open(base, os.O_RDONLY)
    for _ in range(depth):
        os.mkdir("d", dir_fd=fd)
        nfd = os.open("d", os.O_RDONLY, dir_fd=fd)
        os.close(fd)
        fd = nfd
    os.close(fd)


def test_r9_remove_tree_copes_with_a_chain_of_2500_directories(runner_module, tmp_path):
    out = str(tmp_path / "out")
    build_chain(out, 2500)
    runner_module.reset_out(out)
    assert sorted(os.listdir(out)) == ["artifacts"]
    # unreadable directories, links (not followed) and plain files go too
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "keep").write_text("x")
    os.mkdir(os.path.join(out, "locked"))
    open(os.path.join(out, "locked", "f"), "w").close()
    os.chmod(os.path.join(out, "locked"), 0)
    os.symlink(str(outside), os.path.join(out, "link"))
    runner_module.reset_out(out)
    assert sorted(os.listdir(out)) == ["artifacts"] and (outside / "keep").read_text() == "x"


def test_r9_a_deep_chain_left_by_the_agent_does_not_stop_run_json(guest):
    res = guest.run(guest.script(DEEP), timeout=300)
    assert res.run["termination"] == "exited" and res.run.get("reason") is None
    assert sorted(os.listdir(guest.out)) == ["artifacts", "run.json", "stderr.log", "stdout.log"]


def run_patched(guest, card, patch):
    """Run the guest's copy of the runner in a child interpreter after ``patch`` (python source) changed its module."""
    with open(os.path.join(guest.indir, "card.json"), "w") as handle:
        json.dump(card, handle)
    driver = (
        "import importlib.util, sys\n"
        f"spec = importlib.util.spec_from_file_location('runner_patched', {os.path.join(guest.lib, 'runner.py')!r})\n"
        "runner = importlib.util.module_from_spec(spec); spec.loader.exec_module(runner)\n"
        + patch + "\n"
        f"sys.exit(runner.main(['--in', {guest.indir!r}, '--home', {guest.home!r}, '--path', {guest.bin + ':/usr/bin:/bin'!r}]))\n")
    proc = subprocess.run([sys.executable, "-c", driver], capture_output=True, timeout=120, check=False)
    return Result(guest, proc)


def test_r9_a_failing_final_prune_still_writes_run_json(guest):
    res = run_patched(guest, guest.script("pass"), "def boom(out, keep):\n    raise RecursionError('deep')\nrunner.prune_out = boom")
    assert res.run["termination"] == "failed" and res.run["reason"] == "out_not_clean"
    assert res.run["detail"] == "RecursionError"


# R-L3: with a process left alive nothing it could shape is collected

LEFT = "runner.sweep_stragglers = lambda *a, **k: (0, 2)"


def test_l3_stragglers_left_means_no_bundle_artifacts_or_result_json(guest):
    card = guest.script('''
        import json, os
        open("README.md", "a").write("changed\\n")
        os.makedirs("report", exist_ok=True)
        open("report/a.txt", "w").write("artifact")
        open("RESULT.json", "w").write(json.dumps({"summary": "done"}))
        ''', artifacts=["report"])
    res = run_patched(guest, card, LEFT)
    run = res.run
    assert run["termination"] == "failed" and run["reason"] == "stragglers_left" and run["stragglers_left"] == 2
    assert run["bundle_sha256"] is None and run["artifacts"] == [] and run["result_json"] is False
    assert run["changed_files"] == []
    assert sorted(os.listdir(guest.out)) == ["artifacts", "run.json", "stderr.log", "stdout.log"]
    assert os.listdir(os.path.join(guest.out, "artifacts")) == []


def test_l3_without_stragglers_the_same_agent_is_collected(guest):
    card = guest.script('''
        import json, os
        open("README.md", "a").write("changed\\n")
        open("RESULT.json", "w").write(json.dumps({"summary": "done"}))
        ''')
    res = run_patched(guest, card, "pass")
    assert res.run["bundle_sha256"] and res.run["result_json"] is True and res.run["termination"] == "exited"


# R-L2: the omitted marker is part of the cap

@pytest.mark.parametrize("cap", [100_000, 4096, 1024 * 1024])
def test_l2_the_whole_log_including_its_marker_stays_within_the_cap(guest, cap):
    res = guest.run(guest.script("import sys; sys.stdout.write('x' * 3_000_000); sys.stderr.write('e' * 9_000_000)"),
                    args=("--log-cap", str(cap)))
    for name in ("stdout.log", "stderr.log"):
        data = res.read(name)
        assert len(data) <= cap and b"bytes of output omitted" in data, (name, len(data))
    assert res.run["stdout_bytes"] <= cap and res.run["stdout_truncated"] is True


def test_l2_the_sink_never_exceeds_its_cap_even_for_tiny_caps(runner_module):
    for cap in (0, 10, 40, 100, 1000):
        sink = runner_module.CappedSink(cap)
        sink.feed(b"z" * 5000)
        data, omitted = sink.finish()
        assert len(data) <= cap and omitted is True, cap
    sink = runner_module.CappedSink(1000)
    sink.feed(b"small")
    assert sink.finish() == (b"small", False)


# ------------------------------------------------------------------------------------------------ round 3: remove_tree

def test_l2_a_directory_with_16000_children_is_removed_in_linear_time(runner_module, tmp_path):
    top = tmp_path / "wide"
    top.mkdir()
    for i in range(16000):
        os.mkdir(os.path.join(top, f"d{i:06d}"))
    for i in range(0, 16000, 4):
        open(os.path.join(top, f"d{i:06d}", "f"), "w").close()
    started = time.monotonic()
    runner_module.remove_tree(str(top))
    took = time.monotonic() - started
    assert not top.exists() and took < 8, took


def test_l2_depth_2500_needs_only_a_handful_of_descriptors(tmp_path):
    out = str(tmp_path / "out")
    build_chain(out, 2500)
    code = (
        "import importlib.util, os, resource, sys\n"
        "resource.setrlimit(resource.RLIMIT_NOFILE, (24, 24))\n"
        f"spec = importlib.util.spec_from_file_location('r', {os.path.join(PKG, 'runner.py')!r})\n"
        "r = importlib.util.module_from_spec(spec); spec.loader.exec_module(r)\n"
        f"r.reset_out({out!r})\n"
        f"print(sorted(os.listdir({out!r})))\n")
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, timeout=120, check=False)
    assert proc.returncode == 0, proc.stderr[-300:]
    assert proc.stdout.strip() == "['artifacts']"


def test_l2_a_swapped_directory_on_the_way_up_is_refused(runner_module, tmp_path):
    top = tmp_path / "t"
    (top / "a" / "b").mkdir(parents=True)
    real_rmdir = os.rmdir
    swapped = {}

    def swap(path, *a, **k):
        if path == "b" and not swapped:       # the walk is about to go up out of b: move a away and put another in place
            swapped["done"] = True
            os.rename(str(top / "a"), str(tmp_path / "moved"))
            os.mkdir(str(top / "a"))
        return real_rmdir(path, *a, **k)
    runner_module.os.rmdir = swap
    try:
        with pytest.raises(OSError):
            runner_module.remove_tree(str(top))
    finally:
        runner_module.os.rmdir = real_rmdir
