"""The unprivileged ``motel cabin`` command with a fake process runner (no ssh, rsync or sudo is run)."""
import base64
import io
import json
import os
import re
import subprocess

import pytest
from cabin_fakes import pubkey

from motel.cabins import cli

LEASE = "swift-gull-0123456789ab"
NAME = "motel-" + LEASE
VIEW = {"lease_id": LEASE, "name": NAME, "state": "active", "address": "203.0.113.7", "host_key": pubkey("H"),
        "offer": {"server_type": "cpx32", "total_hourly_net_eur": "0.0318"}, "expires_at": "2026-11-08T14:00:00Z",
        "ssh": {"user": "cabin", "host": "203.0.113.7", "port": 22, "host_key_alias": NAME, "host_key": pubkey("H")},
        "access": {"mode": "ssh-allowlist"}, "time_left_s": 7200, "state_": "x"}


class Proc:
    def __init__(self, rc=0, stdout=b""):
        self.returncode, self.stdout = rc, stdout


class Runner:
    def __init__(self, tree=None, ssh_rc=0, rsync_rc=0, checkout_state="released", checkin_rc=0, checkin_doc=None,
                 checkout_script=None):
        self.calls, self.tree = [], tree or ["a.py", "src/b.py"]
        self.ssh_rc, self.rsync_rc, self.checkout_state = ssh_rc, rsync_rc, checkout_state
        self.checkin_rc, self.checkin_doc = checkin_rc, checkin_doc
        self.doctor_ok = True
        self.checkout_script = list(checkout_script or [])      # [(desk rc, reply document)], the last one repeats

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        if argv[:2] == ["/usr/bin/sudo", "-n"]:
            verb = argv[4]
            if verb == "checkin":
                if self.checkin_rc:
                    return Proc(self.checkin_rc, json.dumps(self.checkin_doc or {"ok": False, "error": "no vacancy",
                                                                                 "hint": "motel cabin ls"}).encode())
                return Proc(0, json.dumps({"ok": True, "cabin": VIEW}).encode())
            if verb == "checkout" and self.checkout_script:
                rc, doc = self.checkout_script.pop(0) if len(self.checkout_script) > 1 else self.checkout_script[0]
                return Proc(rc, json.dumps(doc).encode())
            if verb == "checkout":
                rc = 0 if self.checkout_state == "released" else 9
                return Proc(rc, json.dumps({"ok": True, "cabin": dict(VIEW, state=self.checkout_state,
                                                                      cost_estimate_net_eur="0.0100")}).encode())
            if verb == "doctor":
                ok = self.doctor_ok
                return Proc(0 if ok else 8, json.dumps({"schema": "motel/v1", "ok": ok, "checks": [
                    {"check": "setup and prerequisites", "ok": ok, "detail": "" if ok else "missing"}]}).encode())
            if verb in ("ls", "status", "extend"):
                return Proc(0, json.dumps({"schema": "motel/v1", "ok": True, "cabin": VIEW, "cabins": [VIEW],
                                           "orphans": [], "provider": {"inventory": "complete", "error": None},
                                           "verified_empty": False}).encode())
        if argv[0] == "git":
            return Proc(0, b"\0".join(n.encode() for n in self.tree) + b"\0")
        if argv[0] == "/usr/bin/rsync":
            return Proc(self.rsync_rc)
        if argv[0] == "/usr/bin/ssh":
            return Proc(self.ssh_rc)
        raise AssertionError(argv)

    def by(self, program):
        return [(a, k) for a, k in self.calls if a[0] == program]


def make_cli(tmp_path, runner, cwd=None):
    out, err = io.StringIO(), io.StringIO()
    (tmp_path / "work").mkdir(parents=True, exist_ok=True)
    for name in runner.tree:
        target = (tmp_path / "work" / name)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("x")

    def make_key(directory):
        with open(os.path.join(directory, "id_ed25519"), "w") as handle:
            handle.write("PRIVATE")
        return pubkey("U")
    return cli.Cli(runner=runner, root=str(tmp_path / "state"), make_key=make_key, out=out, err=err,
                   cwd=cwd or str(tmp_path / "work"), desk_exists=lambda: True), out, err


class Time:
    """A monotonic clock that only the cli's sleep advances."""

    def __init__(self):
        self.now, self.sleeps = 0.0, []

    def monotonic(self):
        return self.now

    def sleep(self, seconds):
        self.sleeps.append(seconds)
        self.now += seconds


def main(tmp_path, argv, runner=None, clock=None, **kw):
    runner = runner or Runner(**kw)
    c, out, err = make_cli(tmp_path, runner)
    timing = {"monotonic": clock.monotonic, "sleep": clock.sleep} if clock else {}
    rc = cli.main(argv, runner=runner, root=c.root, make_key=c.make_key, out=out, err=err, cwd=c.cwd,
                  desk_exists=lambda: True, **timing)
    return rc, out.getvalue(), err.getvalue(), runner


def test_run_passes_through_the_command_exit_status_and_checks_out(tmp_path):
    rc, _out, err, runner = main(tmp_path, ["run", "--task", "build", "--", "make", "test"], ssh_rc=3)
    assert rc == 3
    verbs = [a[4] for a, _k in runner.calls if a[:2] == ["/usr/bin/sudo", "-n"]]
    assert verbs == ["checkin", "checkout"]
    assert "released" in err and f"cabin {LEASE}" in err
    assert not os.path.exists(os.path.join(str(tmp_path / "state"), LEASE, "id_ed25519"))


def test_run_success_returns_zero_and_sends_the_command_as_one_encoded_job(tmp_path):
    rc, _o, _e, runner = main(tmp_path, ["run", "--", "pytest", "-k", "a b; rm -rf /"])
    assert rc == 0
    ssh_argv = runner.by("/usr/bin/ssh")[0][0]
    assert ssh_argv[-1].startswith("motel-guest run ") and "pytest" not in ssh_argv[-1]
    job = json.loads(base64.b64decode(ssh_argv[-1].split()[-1]))
    assert job == {"argv": ["pytest", "-k", "a b; rm -rf /"], "cwd": "work"}
    assert "--" in ssh_argv and ssh_argv[ssh_argv.index("--") + 1] == NAME


def test_run_keep_skips_checkout(tmp_path):
    rc, _o, err, runner = main(tmp_path, ["run", "--keep", "--", "true"])
    assert rc == 0 and [a[4] for a, _ in runner.calls if a[:2] == ["/usr/bin/sudo", "-n"]] == ["checkin"]
    assert f"kept {LEASE}" in err


def test_run_forwards_type_for_task_and_disk_to_the_desk(tmp_path):
    _rc, _o, _e, runner = main(tmp_path, ["run", "--type", "cpx52", "--for", "90m", "--task", "ci", "--disk", "100G",
                                          "--", "true"])
    argv = runner.by("/usr/bin/sudo")[0][0]
    assert argv[:5] == ["/usr/bin/sudo", "-n", "/usr/local/sbin/motel-desk", "cabin", "checkin"]
    pairs = dict(zip(argv[5::2], argv[6::2], strict=False))
    assert pairs["--type"] == "cpx52" and pairs["--for"] == "90m" and pairs["--task"] == "ci"
    assert pairs["--disk"] == "100G" and pairs["--client-key"] == pubkey("U") and argv[-1] == "--json"


def test_rsync_syncs_exactly_the_selected_files_with_nul_separation(tmp_path):
    tree = ["a.py", "dir/with space.txt", "new untracked.md"]
    _rc, _o, _e, runner = main(tmp_path, ["run", "--", "true"], tree=tree)
    argv, kw = runner.by("/usr/bin/rsync")[0]
    assert "--from0" in argv and "--files-from=-" in argv and argv[-2:] == ["./", f"{NAME}:work/"]
    assert kw["input"] == b"".join(n.encode() + b"\0" for n in tree)
    assert kw["cwd"] == str(tmp_path / "work")
    assert "--delete" not in argv
    assert any(part.startswith("/usr/bin/ssh -F ") for part in argv)


def test_run_uses_git_to_select_files_respecting_ignore_rules(tmp_path):
    _rc, _o, _e, runner = main(tmp_path, ["run", "--", "true"])
    git = runner.by("git")[0][0]
    assert git[:4] == ["git", "-C", str(tmp_path / "work"), "ls-files"]
    assert "--cached" in git and "--others" in git and "--exclude-standard" in git


def test_sync_failure_still_checks_out_and_returns_the_rsync_status(tmp_path):
    rc, _o, err, runner = main(tmp_path, ["run", "--", "true"], rsync_rc=23)
    assert rc == 23 and not runner.by("/usr/bin/ssh") and "rsync exit 23" in err
    assert [a[4] for a, _ in runner.calls if a[:2] == ["/usr/bin/sudo", "-n"]][-1] == "checkout"


def test_incomplete_checkout_is_reported_and_never_hidden(tmp_path):
    rc, _o, err, _r = main(tmp_path, ["run", "--", "true"], checkout_state="cleanup_pending")
    assert "WARNING" in err and "NOT complete" in err and f"motel cabin status {LEASE}" in err
    assert "exit status is the command's" in err


def test_run_exit_code_is_the_commands_whether_or_not_checkout_finished(tmp_path):
    for n, (ssh_rc, state, want) in enumerate([(0, "released", 0), (0, "cleanup_pending", 0), (7, "released", 7),
                                                (7, "cleanup_pending", 7), (4, "cleanup_pending", 4)]):
        rc, _o, err, runner = main(tmp_path / str(n), ["run", "--", "true"], checkout_state=state, ssh_rc=ssh_rc)
        assert rc == want, (ssh_rc, state)
        assert ("NOT complete" in err) == (state == "cleanup_pending")
        assert [a[4] for a, _ in runner.calls if a[:2] == ["/usr/bin/sudo", "-n"]][-1] == "checkout"


RELEASED = (0, {"ok": True, "cabin": dict(VIEW, state="released", cost_estimate_net_eur="0.0100")})
PENDING = (9, {"ok": True, "cabin": dict(VIEW, state="cleanup_pending")})
BUSY = (4, {"schema": "motel/v1", "ok": False, "error": "this cabin is busy with another motel operation",
            "hint": "motel cabin status", "code": 4})


def checkouts(runner):
    return [a for a, _ in runner.calls if a[:2] == ["/usr/bin/sudo", "-n"] and a[4] == "checkout"]


def test_a_busy_checkout_is_retried_with_a_short_backoff_then_succeeds(tmp_path):
    clock = Time()
    runner = Runner(checkout_script=[BUSY, BUSY, RELEASED])
    rc, _o, err, runner = main(tmp_path, ["run", "--", "true"], runner=runner, clock=clock)
    assert rc == 0 and len(checkouts(runner)) == 3 and f"receipt for cabin {LEASE} (released)" in err
    assert clock.sleeps == [2, 4] and "FAILED" not in err


def test_a_busy_checkout_that_stays_busy_returns_the_desks_code_and_says_the_cabin_is_active(tmp_path):
    clock = Time()
    rc, _o, err, runner = main(tmp_path, ["run", "--", "true"], runner=Runner(checkout_script=[BUSY]), clock=clock)
    assert rc == 4 and "FAILED" in err and "ACTIVE and billing" in err and f"Try: motel cabin checkout {LEASE}" in err
    assert "cleanup pending" not in err
    assert 90 <= clock.now <= 120 and len(checkouts(runner)) > 3                  # bounded by about 90 s


def test_the_command_status_is_not_reported_when_the_checkout_failed_but_it_is_named_in_the_message(tmp_path):
    rc, _o, err, _r = main(tmp_path, ["run", "--", "false"], runner=Runner(ssh_rc=7, checkout_script=[BUSY]),
                           clock=Time())
    assert rc == 4 and "The command exited 7" in err


@pytest.mark.parametrize("desk_rc", [5, 6, 7, 8])
def test_other_checkout_failures_return_the_desks_code_and_are_not_retried(tmp_path, desk_rc):
    reply = (desk_rc, {"schema": "motel/v1", "ok": False, "error": "provider said no", "hint": "doctor", "code": desk_rc})
    clock = Time()
    rc, _o, err, runner = main(tmp_path, ["run", "--", "true"], runner=Runner(checkout_script=[reply]), clock=clock)
    assert rc == desk_rc and "FAILED" in err and "ACTIVE and billing" in err and "provider said no" in err
    assert len(checkouts(runner)) == 1 and clock.sleeps == []


def test_desk_exit_9_without_a_cleanup_pending_cabin_is_not_taken_for_a_pending_cleanup(tmp_path):
    odd = (9, {"ok": False, "error": "unreadable", "hint": "motel doctor", "code": 9})
    rc, _o, err, _r = main(tmp_path, ["run", "--", "true"], runner=Runner(checkout_script=[odd]), clock=Time())
    assert rc == 9 and "FAILED" in err and "NOT complete" not in err


def test_pending_after_a_passing_command_keeps_the_command_status_and_warns(tmp_path):
    rc, out, err, runner = main(tmp_path, ["run", "--json", "--", "true"],
                                runner=Runner(checkout_script=[PENDING]), clock=Time())
    doc = json.loads(out.strip().splitlines()[-1])
    assert rc == 0 and "WARNING" in err and doc["released"] is False and len(checkouts(runner)) == 1


def test_json_after_a_failed_checkout_reports_the_desks_exit_code(tmp_path):
    rc, out, _e, _r = main(tmp_path, ["run", "--json", "--", "true"], runner=Runner(checkout_script=[BUSY]),
                           clock=Time())
    doc = json.loads(out.strip().splitlines()[-1])
    assert rc == 4 and doc["exit_code"] == 4 and doc["ok"] is False and doc["released"] is False


def test_sync_failure_with_an_incomplete_checkout_still_returns_the_rsync_status(tmp_path):
    rc, _o, err, _r = main(tmp_path, ["run", "--", "true"], rsync_rc=23, checkout_state="cleanup_pending")
    assert rc == 23 and "NOT complete" in err


def test_checkin_refusal_is_reported_and_nothing_is_run(tmp_path):
    rc, _o, err, runner = main(tmp_path, ["run", "--", "true"], checkin_rc=3)
    assert rc == 3 and "no vacancy" in err and not runner.by("/usr/bin/ssh") and not runner.by("/usr/bin/rsync")


def test_run_without_command_is_a_usage_error(tmp_path):
    rc, _o, err, runner = main(tmp_path, ["run"])
    assert rc == 2 and "after --" in err and runner.calls == []


def test_bad_arguments_are_rejected_before_calling_the_desk(tmp_path):
    for argv in (["run", "--task", "Bad Task", "--", "x"], ["run", "--for", "soon", "--", "x"],
                 ["run", "--disk", "lots", "--", "x"], ["bogus"], ["ls", "--", "x"], ["status", "../etc"],
                 ["checkout", "bad/id"]):
        rc, _o, _e, runner = main(tmp_path, argv)
        assert rc == 2 and runner.calls == [], argv


def test_checkin_json_prints_the_view_and_ls_lists(tmp_path):
    rc, out, _e, _r = main(tmp_path, ["checkin", "--json"])
    assert rc == 0 and json.loads(out)["lease_id"] == LEASE
    rc, out, _e, _r = main(tmp_path / "plain", ["checkin"])
    assert rc == 0 and out.strip() == LEASE
    rc, out, _e, _r = main(tmp_path, ["ls", "--json"])
    assert rc == 0 and json.loads(out)["cabins"][0]["lease_id"] == LEASE
    rc, out, _e, _r = main(tmp_path, ["ls"])
    assert rc == 0 and LEASE in out


def test_checkin_writes_pinned_access_files_with_private_key_mode(tmp_path):
    main(tmp_path, ["checkin"])
    directory = tmp_path / "state" / LEASE
    config = (directory / "ssh_config").read_text()
    assert "StrictHostKeyChecking yes" in config and "IdentityAgent none" in config
    assert "ForwardAgent no" in config and f"HostKeyAlias {NAME}" in config and "AddressFamily inet" in config
    assert (directory / "known_hosts").read_text() == f"{NAME} {pubkey('H')}\n"
    assert oct((tmp_path / "state").stat().st_mode & 0o777) == "0o700"


def test_ssh_verb_opens_a_shell_or_runs_a_command(tmp_path):
    main(tmp_path, ["checkin"])
    runner = Runner()
    c, out, err = make_cli(tmp_path, runner)
    rc = cli.main(["ssh", LEASE], runner=runner, root=str(tmp_path / "state"), out=out, err=err,
                  cwd=c.cwd, desk_exists=lambda: True)
    assert rc == 0 and runner.calls[0][0][:3] == ["/usr/bin/ssh", "-F", str(tmp_path / "state" / LEASE / "ssh_config")]
    assert "-T" not in runner.calls[0][0]
    runner2 = Runner(ssh_rc=7)
    rc = cli.main(["ssh", LEASE, "--", "ls", "-la"], runner=runner2, root=str(tmp_path / "state"), out=out, err=err,
                  cwd=c.cwd, desk_exists=lambda: True)
    assert rc == 7 and "-T" in runner2.calls[0][0] and runner2.calls[0][0][-1].startswith("motel-guest run ")


def test_checkout_removes_the_private_key_only_when_released(tmp_path):
    main(tmp_path, ["checkin"])
    key = tmp_path / "state" / LEASE / "id_ed25519"
    assert key.exists()
    rc, _o, _e, _r = main(tmp_path, ["checkout", LEASE], checkout_state="cleanup_pending")
    assert rc == 9 and key.exists()
    rc, _o, _e, _r = main(tmp_path, ["checkout", LEASE])
    assert rc == 0 and not key.exists()


def test_missing_desk_is_exit_8(tmp_path):
    runner = Runner()
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(["ls"], runner=runner, root=str(tmp_path / "s"), out=out, err=err, desk_exists=lambda: False)
    assert rc == 8 and "front desk is not installed" in err.getvalue() and runner.calls == []


def test_desk_failure_json_passthrough(tmp_path):
    rc, out, _e, _r = main(tmp_path, ["checkin", "--json"], checkin_rc=3)
    assert rc == 3 and json.loads(out)["error"] == "no vacancy"


def test_the_cli_never_passes_environment_or_paths_to_the_desk(tmp_path):
    _rc, _o, _e, runner = main(tmp_path, ["run", "--", "true"])
    for argv, kw in runner.by("/usr/bin/sudo"):
        assert kw["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"}
        assert not any(str(tmp_path) in part for part in argv)


# ---- worktree_files against a real git repository

def git(cwd, *args):
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True,
                   env={"PATH": os.environ["PATH"], "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid", "HOME": str(cwd)})


def test_worktree_files_selects_tracked_and_untracked_but_not_ignored_or_deleted(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    (repo / ".gitignore").write_text("secret.env\nbuild/\n")
    (repo / "tracked.py").write_text("1")
    (repo / "gone.py").write_text("1")
    git(repo, "add", ".")
    git(repo, "commit", "-qm", "init")
    os.unlink(repo / "gone.py")
    (repo / "untracked.md").write_text("1")
    (repo / "secret.env").write_text("TOKEN=1")
    (repo / "build").mkdir()
    (repo / "build" / "x").write_text("1")
    (repo / "with space.txt").write_text("1")
    assert sorted(cli.worktree_files(str(repo))) == [".gitignore", "tracked.py", "untracked.md", "with space.txt"]


def test_worktree_files_refuses_a_non_repository(tmp_path):
    from motel.cabins.errors import Usage
    with pytest.raises(Usage, match="not a git worktree"):
        cli.worktree_files(str(tmp_path))


def desk_verbs(runner):
    return [a[4] for a, _k in runner.calls if a[:2] == ["/usr/bin/sudo", "-n"]]


def test_doctor_asks_the_desk_for_the_one_user_verb_the_sudoers_rule_pins(tmp_path):
    rc, out, _e, runner = main(tmp_path, ["doctor"])
    assert rc == 0 and "ok   setup and prerequisites" in out
    [argv] = runner.by("/usr/bin/sudo")
    assert argv[0][:5] == ["/usr/bin/sudo", "-n", "/usr/local/sbin/motel-desk", "cabin", "doctor"]
    from motel.cabins import desk
    assert argv[0][4] in desk.USER_VERBS


def test_a_failing_doctor_prints_its_checks_and_exits_with_the_desks_code(tmp_path):
    runner = Runner()
    runner.doctor_ok = False
    rc, out, _e, _r = main(tmp_path, ["doctor", "--json"], runner=runner)
    assert rc == 8
    assert json.loads(out)["checks"][0]["ok"] is False


def test_run_json_ends_with_one_object_with_the_cabin_the_exit_code_and_the_cost(tmp_path):
    rc, out, err, runner = main(tmp_path, ["run", "--json", "--", "make", "test"], ssh_rc=3)
    assert rc == 3
    doc = json.loads(out.strip().splitlines()[-1])
    receipt = doc.pop("receipt")      # the stable `receipt` key: see test_cabin_receipt.py for its shape
    assert doc == {"schema": "motel/v1", "ok": False, "cabin": LEASE, "exit_code": 3, "kept": False, "released": True,
                   "hourly_net_eur": "0.0318", "cost_estimate_net_eur": "0.0100"}
    assert receipt["final"] is True and receipt["servers_left"] == 0 and receipt["estimated_total_net_eur"] == "0.0100"
    assert "servers left  0" in err  # the human lines stay on stderr
    ssh = runner.by("/usr/bin/ssh")[0][1]
    assert "stdout" not in ssh and "stderr" not in ssh  # the command's own streams are inherited, never captured or merged


def test_run_json_success_and_keep(tmp_path):
    rc, out, _e, _r = main(tmp_path, ["run", "--json", "--keep", "--", "true"])
    doc = json.loads(out.strip().splitlines()[-1])
    assert rc == 0 and doc["ok"] is True and doc["kept"] is True and doc["released"] is False
    assert doc["cost_estimate_net_eur"] is None and doc["receipt"] is None   # nothing was released: no receipt


def test_a_json_flag_after_the_double_dash_belongs_to_the_command(tmp_path):
    rc, out, _e, _r = main(tmp_path, ["run", "--", "tool", "--json"])
    assert rc == 0 and out == ""


def test_run_json_reports_a_failed_checkin_as_an_error_document(tmp_path):
    rc, out, _e, runner = main(tmp_path, ["run", "--json", "--", "true"], checkin_rc=3)
    assert rc == 3 and json.loads(out)["ok"] is False and runner.by("/usr/bin/ssh") == []


def test_incomplete_checkout_is_visible_in_the_json(tmp_path):
    rc, out, _e, _r = main(tmp_path, ["run", "--json", "--", "true"], checkout_state="cleanup_pending")
    doc = json.loads(out.strip().splitlines()[-1])
    assert rc == 0 and doc["released"] is False and doc["exit_code"] == 0 and doc["ok"] is True
    rc, out, _e, _r = main(tmp_path / "failed", ["run", "--json", "--", "false"], checkout_state="cleanup_pending",
                           ssh_rc=3)
    doc = json.loads(out.strip().splitlines()[-1])
    assert rc == 3 and doc["released"] is False and doc["exit_code"] == 3 and doc["ok"] is False


def held_cabin(tmp_path):
    """A cabin this account already holds: the access files a checkin leaves behind."""
    runner = Runner()
    rc, _o, _e, _r = main(tmp_path, ["checkin", "--task", "held"], runner=runner)
    assert rc == 0
    return runner


def test_run_in_an_existing_cabin_syncs_and_runs_without_checkin_or_checkout(tmp_path):
    held = held_cabin(tmp_path)
    before = len(held.calls)
    rc, out, _e, runner = main(tmp_path, ["run", "--cabin", LEASE, "--json", "--", "pytest"], runner=held)
    assert rc == 0
    new = held.calls[before:]
    assert [a[0] for a, _k in new if a[0] != "git"] == ["/usr/bin/rsync", "/usr/bin/ssh"]
    assert desk_verbs(runner) == ["checkin"]  # only the earlier checkin; this run made no desk call at all
    doc = json.loads(out.strip().splitlines()[-1])
    assert doc["cabin"] == LEASE and doc["kept"] is True and doc["released"] is False and doc["exit_code"] == 0


def test_run_in_an_existing_cabin_passes_the_exit_status_through(tmp_path):
    held = held_cabin(tmp_path)
    held.ssh_rc = 7
    rc, _o, _e, _r = main(tmp_path, ["run", "--cabin", LEASE, "--", "false"], runner=held)
    assert rc == 7


def test_run_cabin_rejects_launch_options_unknown_ids_and_a_missing_command(tmp_path):
    held = held_cabin(tmp_path)
    for argv in (["run", "--cabin", LEASE, "--keep", "--", "true"], ["run", "--cabin", LEASE, "--type", "cpx52", "--", "true"],
                 ["run", "--cabin", LEASE], ["run", "--cabin", "not-a-lease", "--", "true"]):
        rc, _o, err, _r = main(tmp_path, argv, runner=held)
        assert rc != 0 and "motel:" in err
    rc, _o, err, _r = main(tmp_path, ["run", "--cabin", "swift-gull-ffffffffffff", "--", "true"], runner=held)
    assert rc == 7 and "no access files" in err


BILLED = (0, {"ok": True, "cabin": dict(VIEW, state="released", cost_estimate_net_eur="0.0318",
                                         billing={"granularity": "started_hour", "billed_hours": 2})})


def test_run_states_the_billed_hours_in_its_receipt_and_json_summary(tmp_path):
    rc, out, err, _r = main(tmp_path, ["run", "--json", "--", "true"], runner=Runner(checkout_script=[BILLED]))
    assert rc == 0
    assert "billed        2 started hours" in err and "est. total    EUR 0.0318 net" in err
    doc = json.loads(out.strip().splitlines()[-1])
    assert doc["billed_hours"] == 2 and doc["receipt"]["billed_hours"] == 2 and doc["receipt"]["basis"] == "started_hours"


def test_run_without_hourly_billing_says_so_in_its_receipt_and_has_no_billed_hours_key(tmp_path):
    rc, out, err, _r = main(tmp_path, ["run", "--json", "--", "true"])
    assert "billed        by the time used" in err and "started hour" not in err
    doc = json.loads(out.strip().splitlines()[-1])
    assert "billed_hours" not in doc and doc["receipt"]["billed_hours"] is None and doc["receipt"]["basis"] == "prorated"


# ---------------------------------------------------------------- receipts, help and first-run errors

def test_checkout_prints_the_receipt_on_stderr_and_keeps_stdout_to_the_render(tmp_path):
    rc, out, err, _r = main(tmp_path, ["checkout", LEASE], runner=Runner(checkout_script=[BILLED]))
    assert rc == 0
    assert f"motel: receipt for cabin {LEASE} (released)" in err and "billed        2 started hours" in err
    assert "servers left  0" in err and "receipt" not in out           # stdout is the render, unchanged


def test_checkout_json_has_the_receipt_under_the_cabin_and_one_object_on_stdout(tmp_path):
    rc, out, err, _r = main(tmp_path, ["checkout", LEASE, "--json"], runner=Runner(checkout_script=[BILLED]))
    doc = json.loads(out)                                               # exactly one JSON object, nothing else
    assert rc == 0 and doc["cabin"]["receipt"]["billed_hours"] == 2 and doc["cabin"]["receipt"]["servers_left"] == 0
    assert "receipt for cabin" in err


def test_a_desks_own_receipt_is_shown_as_it_is(tmp_path):
    own = {"server_type": "cpx32", "vcpus": 4, "memory_gb": 8.0, "location": "fsn1", "disk_gb": None, "wall_seconds": 61,
           "billed_hours": 1, "basis": "started_hours", "hourly_net_eur": "0.0318", "estimated_total_net_eur": "0.0318",
           "currency": "EUR", "final": True, "servers_left": 0}
    reply = (0, {"ok": True, "cabin": dict(VIEW, state="released", cost_estimate_net_eur="9.9999", receipt=own)})
    rc, out, err, _r = main(tmp_path, ["run", "--json", "--", "true"], runner=Runner(checkout_script=[reply]))
    assert "cpx32 (4 vCPU, 8 GB RAM) in fsn1" in err and "wall time     1m 01s" in err
    assert json.loads(out.strip().splitlines()[-1])["receipt"] == own


def test_a_cabin_whose_cleanup_is_pending_prints_its_receipt_with_one_server_left(tmp_path):
    rc, _o, err, _r = main(tmp_path, ["run", "--", "true"], runner=Runner(checkout_script=[PENDING]))
    assert "NOT complete" in err and "servers left  1" in err and "servers left  0" not in err


def test_the_help_is_real_text_not_the_module_docstring(tmp_path):
    rc, out, err, _r = main(tmp_path, ["--help"])
    assert rc == 0 and err == ""
    assert "``main(argv)``" not in out and "examples:" in out and "receipt" in out and "deletion is final" in out
    for verb in ("run", "checkin", "ls", "status", "extend", "checkout", "ssh", "doctor"):
        assert re.search(rf"^    {verb}\b", out, re.M), verb
    assert "motel try --cabin" in out
    rc, out, err, _r = main(tmp_path, [])                               # no verb: the same help, exit 2
    assert rc == 2 and "examples:" in out


def test_without_the_front_desk_the_error_says_how_to_install_or_try_the_demo(tmp_path):
    out, err = io.StringIO(), io.StringIO()
    rc = cli.main(["ls"], out=out, err=err, root=str(tmp_path / "state"), desk=str(tmp_path / "no-such-desk"))
    line = err.getvalue()
    assert rc == 8 and "front desk is not installed" in line
    assert "Try: sudo uvx --from motel motel install --users $USER" in line and "motel try" in line
    assert "Try: motel doctor" not in line


@pytest.mark.parametrize("reply", [Proc(1, b""), Proc(1, b"not json")])
def test_a_desk_call_sudo_never_ran_does_not_send_the_user_back_to_the_failing_command(tmp_path, reply):
    class Refused(Runner):
        def __call__(self, argv, **kw):
            if argv[:2] == ["/usr/bin/sudo", "-n"]:
                return reply
            return super().__call__(argv, **kw)
    rc, out, err, _r = main(tmp_path, ["ls", "--json"], runner=Refused())
    doc = json.loads(out)
    if reply.stdout == b"":
        assert "allow_users" in doc["error"] and "allow_users in /etc/motel/motel.toml" in doc["hint"]
    else:
        assert doc["error"] == "unreadable front desk reply"            # a reply that is not JSON keeps the old answer
