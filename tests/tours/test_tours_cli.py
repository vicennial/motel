"""The unprivileged `motel tour` CLI against a real git repository and an in-process desk."""
import hashlib
import io
import json
import os
import subprocess

import doubles
import pytest

from motel.cabins.config import CabinPaths
from motel.tours import artifacts as art
from motel.tours import cli as tour_cli
from motel.tours import desk as tour_desk
from motel.tours import ownlogin

GIT = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@example.invalid", "GIT_CONFIG_NOSYSTEM": "1", "HOME": "/nonexistent",
       "PATH": os.environ["PATH"]}


def git(cwd, *args, check=True):
    proc = subprocess.run(["git", *args], cwd=cwd, env=GIT, capture_output=True, text=True)
    assert proc.returncode == 0 or not check, proc.stderr
    return proc.stdout.strip()


@pytest.fixture
def repo(tmp_path):
    path = tmp_path / "work" / "repo"
    path.mkdir(parents=True)
    git(path, "init", "-q", "-b", "main")
    (path / "a.txt").write_text("one\n")
    git(path, "add", ".")
    git(path, "commit", "-qm", "first")
    (path / "a.txt").write_text("two\n")
    git(path, "commit", "-qam", "second")
    return path


def real_guest(tmp_path, thin=False):
    """A guest that does what the contract says: clone the input bundle, commit on the branch, bundle the branch."""
    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        work = tmp_path / f"guest-{card['attempt']}"
        work.mkdir()
        (work / "in.bundle").write_bytes(files["input.bundle"])
        git(work, "init", "-q")
        git(work, "fetch", "-q", "in.bundle", "refs/heads/*:refs/remotes/in/*")
        git(work, "checkout", "-q", "-b", card["branch"], card["base"])
        (work / "fixed.txt").write_text("done by the guest\n")
        git(work, "add", ".")
        git(work, "commit", "-qm", "guest work")
        spec_ = f"{card['base']}..refs/heads/{card['branch']}" if thin else f"refs/heads/{card['branch']}"
        git(work, "bundle", "create", "out.bundle", spec_)
        data = (work / "out.bundle").read_bytes()
        out["result.bundle"] = data
        run = json.loads(out["run.json"])
        run.update(bundle_sha256=hashlib.sha256(data).hexdigest(), head=git(work, "rev-parse", "HEAD"))
        out["run.json"] = json.dumps(run).encode()
        return out, status
    return guest


class Bridge:
    """The CLI's runner, wired in process to the desk of the rig (what `sudo motel-desk tour ...` does)."""

    def __init__(self, rig, caller=None):
        self.rig, self.caller, self.calls = rig, caller or rig.alice, []

    def __call__(self, args, stdin=None):
        self.calls.append((list(args), stdin))
        out, err = io.StringIO(), io.StringIO()
        out.buffer = io.BytesIO()
        code = tour_desk.run(list(args), paths=CabinPaths(root=str(self.rig.tmp / "root")), caller=self.caller, out=out, err=err,
                             service=self.rig.service, limits=self.rig.limits, stdin=io.BytesIO(stdin or b""))
        raw = out.buffer.getvalue() or out.getvalue().encode()
        return code, raw, err.getvalue().encode()


def run_cli(rig, argv, repo, bridge=None, stdin=None):
    out, err = io.StringIO(), io.StringIO()
    code = tour_cli.main(argv, runner=bridge or Bridge(rig), out=out, err=err, cwd=str(repo), stdin=stdin)
    return code, out.getvalue(), err.getvalue()


SPEC = """
[tour]
name = "cli-test"
[[guests]]
id = "fixer"
prompt = "Fix it."
secrets = ["anthropic"]
budget_usd = 0.5
artifacts = ["reports/*.json"]
"""


def write_spec(repo, text=SPEC):
    path = repo / "tour.toml"
    (repo / ".git" / "info" / "exclude").write_text("tour.toml\n")
    path.write_text(text)
    return str(path)


def test_start_bundles_the_repo_as_the_user_and_hands_it_to_the_desk(rig, repo):
    bridge = Bridge(rig)
    code, out, err = run_cli(rig, ["start", write_spec(repo)], repo, bridge)
    assert code == 0, err
    tid = out.strip()
    assert tid.startswith("t-") and "started" in err
    payload = json.loads(bridge.calls[0][1])
    guest = payload["spec"]["guests"][0]
    assert guest["base"] == git(repo, "rev-parse", "HEAD") and guest["bundle"] in payload["inputs"]
    [(key, entry)] = payload["inputs"].items()
    assert entry["kind"] == "bundle" and entry["path"].endswith(".bundle") and entry["sha256"].startswith(key)
    assert not os.path.exists(entry["path"])                          # the private temp directory is gone afterwards
    assert rig.store.exists(tid)                                       # the desk copied it first
    with open(rig.store.input_path(tid, key), "rb") as handle:
        assert handle.read(16).startswith(b"# v2 git bundle")


def test_start_resolves_a_branch_name_to_a_full_commit_and_refuses_a_bad_one(rig, repo):
    git(repo, "branch", "feature", "HEAD~1")
    bridge = Bridge(rig)
    run_cli(rig, ["start", write_spec(repo, SPEC.replace('prompt = "Fix it."', 'prompt = "x"\nbase = "feature"'))], repo, bridge)
    assert json.loads(bridge.calls[0][1])["spec"]["guests"][0]["base"] == git(repo, "rev-parse", "feature")
    code, _out, err = run_cli(rig, ["start", write_spec(repo, SPEC.replace('prompt = "Fix it."', 'prompt = "x"\nbase = "nope"'))], repo)
    assert code == 2 and "not a commit" in err


def test_an_overlay_is_copied_and_passed_by_digest(rig, repo):
    (repo / "o.patch").write_text("diff --git a/x b/x\n")
    bridge = Bridge(rig)
    code, _o, err = run_cli(rig, ["start", write_spec(repo, SPEC.replace('prompt = "Fix it."', 'prompt = "x"\noverlay = "o.patch"'))],
                            repo, bridge)
    assert code == 0, err
    payload = json.loads(bridge.calls[0][1])
    guest = payload["spec"]["guests"][0]
    assert payload["inputs"][guest["overlay_input"]]["kind"] == "overlay" and len(payload["inputs"]) == 2


def test_the_own_login_warning_is_printed_once_on_start(rig, repo):
    rig.limits = rig.limits.__class__(**{**rig.limits.__dict__, "own_login_enabled": True, "own_login_allow_users": ("alice",)})
    rig.service.limits = rig.limits
    spec_text = '[[guests]]\nid = "me"\nprompt = "x"\nauth = "own-login"\nsecrets = []\n'
    code, out, err = run_cli(rig, ["start", write_spec(repo, spec_text)], repo)
    assert code == 0, err
    assert err.count(ownlogin.WARNING) == 1 and "no per-job budget" in err and "account-wide" in err
    code, out, err = run_cli(rig, ["start", write_spec(repo)], repo)
    assert ownlogin.WARNING not in err


def test_own_login_is_refused_by_the_desk_when_the_operator_did_not_enable_it(rig, repo):
    spec_text = '[[guests]]\nid = "me"\nprompt = "x"\nauth = "own-login"\nsecrets = []\n'
    code, out, err = run_cli(rig, ["start", write_spec(repo, spec_text)], repo)
    assert code != 0 and "own-login is not enabled" in err and rig.store.ids() == []


def test_start_reads_a_spec_from_stdin_and_json_output(rig, repo):
    code, out, err = run_cli(rig, ["start", "-", "--json"], repo, stdin=io.StringIO(SPEC))
    assert code == 0, err
    assert json.loads(out)["tour"]["id"].startswith("t-")


def test_the_whole_loop_wait_result_logs_fetch(rig, repo, tmp_path):
    rig.transport.guest = real_guest(tmp_path)
    code, out, err = run_cli(rig, ["start", write_spec(repo)], repo)
    tid = out.strip()
    rig.supervise(tid)
    code, out, err = run_cli(rig, ["wait", tid, "--timeout", "5"], repo)
    assert code == 0 and "done" in out
    code, out, err = run_cli(rig, ["ls"], repo)
    assert tid in out
    code, out, err = run_cli(rig, ["result", tid, "fixer"], repo)
    assert code == 0 and "UNTRUSTED" in out.upper() or "untrusted" in out
    code, out, err = run_cli(rig, ["logs", tid, "fixer"], repo)
    assert "agent says hello" in out and "untrusted" in err
    head_before = git(repo, "rev-parse", "HEAD")
    branches_before = git(repo, "branch", "--list")
    code, out, err = run_cli(rig, ["fetch", tid, "fixer", "--artifacts", str(tmp_path / "arts")], repo)
    assert code == 0, err
    branch = f"motel/{tid}/fixer"
    assert git(repo, "show", f"{branch}:fixed.txt") == "done by the guest"
    assert git(repo, "rev-parse", "HEAD") == head_before                      # nothing was checked out or merged
    assert git(repo, "status", "--porcelain") == "" and git(repo, "symbolic-ref", "--short", "HEAD") == "main"
    assert branch in git(repo, "branch", "--list") and branches_before.strip() in git(repo, "branch", "--list")
    assert (tmp_path / "arts" / "reports" / "a.json").read_bytes() == b'{"ok": true}'


def test_fetch_refuses_bytes_that_do_not_match_the_recorded_digest(rig, repo, tmp_path):
    rig.transport.guest = real_guest(tmp_path)
    tid = run_cli(rig, ["start", write_spec(repo)], repo)[1].strip()
    rig.supervise(tid)
    bridge = Bridge(rig)

    def lying(args, stdin=None):
        code, raw, err = bridge(args, stdin)
        return (code, raw + b"tampered", err) if args[0] == "fetch" else (code, raw, err)
    code, _out, err = run_cli(rig, ["fetch", tid, "fixer"], repo, lying)
    assert code != 0 and "digest" in err and f"motel/{tid}/fixer" not in git(repo, "branch", "--list")


def test_fetch_into_a_repository_without_the_base_commit_says_so(rig, repo, tmp_path):
    rig.transport.guest = real_guest(tmp_path, thin=True)
    tid = run_cli(rig, ["start", write_spec(repo)], repo)[1].strip()
    rig.supervise(tid)
    other = tmp_path / "other"
    other.mkdir()
    git(other, "init", "-q")
    code, _out, err = run_cli(rig, ["fetch", tid, "fixer"], other)
    assert code != 0 and "does not have the commit" in err


def test_fetch_never_overwrites_a_branch_with_other_history(rig, repo, tmp_path):
    rig.transport.guest = real_guest(tmp_path)
    tid = run_cli(rig, ["start", write_spec(repo)], repo)[1].strip()
    rig.supervise(tid)
    tree = git(repo, "rev-parse", "HEAD^{tree}")
    unrelated = git(repo, "commit-tree", tree, "-m", "unrelated")
    git(repo, "branch", f"motel/{tid}/fixer", unrelated)
    code, _out, err = run_cli(rig, ["fetch", tid, "fixer"], repo)
    assert code != 0 and "could not import" in err
    assert git(repo, "rev-parse", f"motel/{tid}/fixer") == unrelated


def test_artifact_paths_from_the_result_cannot_escape_the_target_directory(rig, repo, tmp_path):
    cli = tour_cli.Cli(runner=lambda args, stdin=None: (1, b"", b""), cwd=str(repo), out=io.StringIO(), err=io.StringIO())
    with pytest.raises(Exception, match="unsafe"):
        cli.save_artifacts("t-0000000000", "g", [{"path": "../../x", "bytes": 1, "sha256": "a" * 64}], str(tmp_path / "d"))
    assert art.safe_relpath("reports/a.json")


def test_cancel_and_status_render_text_and_json(rig, repo):
    tid = run_cli(rig, ["start", write_spec(repo)], repo)[1].strip()
    code, out, _ = run_cli(rig, ["status", tid], repo)
    assert tid in out and "fixer" in out
    code, out, _ = run_cli(rig, ["cancel", tid, "--json"], repo)
    assert code == 0 and json.loads(out)["tour"]["status"] == "cancelled"


def test_bad_input_is_a_usage_error(rig, repo):
    assert run_cli(rig, ["start", str(repo / "missing.toml")], repo)[0] == 2
    assert run_cli(rig, ["status", "not-a-tour"], repo)[0] == 2
    assert run_cli(rig, ["fetch", "t-0000000000", "Bad Guest"], repo)[0] == 2
