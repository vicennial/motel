"""The launcher against a fake ``motel`` and a fake ``ssh``. No network, no cloud, no real Omnigent server.

Needs Omnigent (for the base classes); skipped without it.
"""

import importlib.metadata
import json
import logging
import os
import re
import shutil
import signal
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytest.importorskip("omnigent.onboarding.sandboxes.base")  # everything below needs Omnigent

from omnigent.community.sandbox.motel import transport as tp  # noqa: E402
from omnigent.community.sandbox.motel.launcher import MotelSandboxError  # noqa: E402
from omnigent_provider.fakes import CABIN, CABIN_ID, argv_text, fail, ok, sha  # noqa: E402
from omnigent_provider.helpers import HOST_COMMAND, KEY, TOKEN, make, seed  # noqa: E402


# -- provision and terminate ------------------------------------------------------------------------------------
def test_provision_checks_in_and_waits_for_ssh(rig, tmp_path):
    seed(rig)
    launcher = make(rig, tmp_path, server_type="cpx32", ttl="3h")
    assert launcher.provision("managed-a1b2c3d4") == CABIN_ID
    checkin = rig.argv()[0]
    assert checkin[:6] == ["cabin", "checkin", "--type", "cpx32", "--for", "3h"] and checkin[-1] == "--json"
    assert re.fullmatch(r"managed-a1b2c3d4-[0-9a-f]{8}", checkin[7])  # a unique label, for finding the cabin again
    assert rig.scripts() == [tp.wrap("true\n")]


def test_provision_falls_back_to_the_configured_task_for_an_unusable_name(rig, tmp_path):
    seed(rig)
    make(rig, tmp_path).provision("___")
    assert re.search(r"--task omnigent-[0-9a-f]{8} ", " ".join(rig.argv()[0]))


def test_provision_that_cannot_reach_the_cabin_checks_it_out_again(rig, tmp_path):
    seed(rig)
    rig.script_rule("true", exit=255, stderr="ssh: connect refused")
    with pytest.raises(MotelSandboxError, match="did not accept commands"):
        make(rig, tmp_path).provision("x1")
    assert rig.argv()[-1][:2] == ["cabin", "checkout"]


def test_provision_refused_by_a_cap_is_a_clean_error(rig, tmp_path):
    rig.rule("cabin", "checkin", **fail(3, "the price cap max_eur_per_hour would be exceeded", "motel cabin ls"))
    with pytest.raises(MotelSandboxError, match="price cap"):
        make(rig, tmp_path).provision("x1")


def test_missing_credential_fails_before_any_cabin_exists(rig, tmp_path):
    seed(rig)
    launcher = make(rig, tmp_path, env=["ANTHROPIC_API_KEY"])
    with pytest.raises(MotelSandboxError, match="ANTHROPIC_API_KEY"):
        launcher.provision("x1")
    assert rig.argv() == []


def test_terminate_confirms_release_and_forgets_state(rig, tmp_path):
    seed(rig)
    launcher = make(rig, tmp_path)
    launcher.terminate(CABIN_ID)
    assert rig.argv() == [["cabin", "checkout", CABIN_ID, "--json"]]


def test_terminate_reports_cleanup_pending(rig, tmp_path):
    rig.rule("cabin", "checkout", exit=9, **ok(cabin={**CABIN, "state": "cleanup_pending"}))
    with pytest.raises(MotelSandboxError, match="cleanup_pending") as excinfo:
        make(rig, tmp_path).terminate(CABIN_ID)
    assert "may still bill" in excinfo.value.message


def test_terminate_surfaces_a_failed_checkout_as_cleanup_pending(rig, tmp_path):
    rig.rule("cabin", "checkout", **fail(5, "the provider is unreachable"))
    with pytest.raises(MotelSandboxError, match="cleanup_pending"):
        make(rig, tmp_path).terminate(CABIN_ID)


def test_terminate_of_an_absent_cabin_is_success(rig, tmp_path):
    rig.rule("cabin", "checkout", **fail(7, "no such cabin"))
    make(rig, tmp_path).terminate(CABIN_ID)


# -- liveness ---------------------------------------------------------------------------------------------------
@pytest.mark.parametrize("state,expected", [("active", True), ("released", False), ("cleanup_pending", False),
                                            ("bootstrapping", None)])
def test_is_running_follows_cabin_status(rig, tmp_path, state, expected):
    rig.rule("cabin", "status", **ok(cabin={**CABIN, "state": state}))
    assert make(rig, tmp_path).is_running(CABIN_ID) is expected


def test_is_running_false_when_gone_and_unknown_on_errors(rig, tmp_path):
    rig.rule("cabin", "status", **fail(7, "no such cabin"))
    assert make(rig, tmp_path).is_running(CABIN_ID) is False
    rig.rules.clear()
    rig.rule("cabin", "status", **fail(5, "provider error"))
    assert make(rig, tmp_path).is_running(CABIN_ID) is None


def test_keep_alive_extends_the_checkout_time(rig, tmp_path):
    rig.rule("cabin", "extend", **ok(cabin=CABIN))
    assert make(rig, tmp_path, ttl="90m", harness_packages=[]).keep_alive(CABIN_ID) is True
    assert rig.argv() == [["cabin", "extend", CABIN_ID, "--for", "90m", "--json"]]


def test_keep_alive_counts_an_already_long_enough_lease_as_success(rig, tmp_path):
    rig.rule("cabin", "extend", **fail(3, "the cabin already runs until 2026-03-01T14:40:00Z; an extension must add at least 5 minutes"))
    assert make(rig, tmp_path).keep_alive(CABIN_ID) is True


def test_keep_alive_soft_fails_at_the_lifetime_ceiling(rig, tmp_path):
    rig.rule("cabin", "extend", **fail(3, "the maximum lifetime ends at 2026-03-08T12:00:00Z"))
    assert make(rig, tmp_path).keep_alive(CABIN_ID) is False


def test_attach_requires_an_active_cabin(rig, tmp_path):
    rig.rule("cabin", "status", **ok(cabin={**CABIN, "state": "cleanup_pending"}))
    with pytest.raises(MotelSandboxError, match="cleanup_pending"):
        make(rig, tmp_path).attach(CABIN_ID)


def test_prepare_reports_a_front_desk_that_is_not_set_up(rig, tmp_path):
    rig.rule("cabin", "ls", **fail(8, "the front desk is not installed", "sudo ./install.sh"))
    with pytest.raises(MotelSandboxError, match="not ready"):
        make(rig, tmp_path).prepare()


def test_prepare_passes_when_motel_works(rig, tmp_path):
    rig.rule("cabin", "ls", **ok(cabins=[]))
    make(rig, tmp_path).prepare()


# -- commands and files -----------------------------------------------------------------------------------------
def test_run_sends_the_command_on_stdin_never_in_argv(rig, tmp_path):
    rig.script_rule("echo hello-from-cabin", stdout="hello-from-cabin\n")
    result = make(rig, tmp_path).run(CABIN_ID, "echo hello-from-cabin")
    assert (result.returncode, result.stdout) == (0, "hello-from-cabin\n")
    assert rig.argv() == [["cabin", "ssh", CABIN_ID, "--", "sh", "-s"]]
    assert 'PATH="$HOME/.local/bin:$PATH"' in rig.scripts()[0]


def test_run_raises_on_failure_unless_check_is_off(rig, tmp_path):
    rig.script_rule("false-cmd", exit=3, stderr="boom")
    launcher = make(rig, tmp_path)
    with pytest.raises(MotelSandboxError, match="exit 3"):
        launcher.run(CABIN_ID, "false-cmd")
    assert launcher.run(CABIN_ID, "false-cmd", check=False).returncode == 3


def test_run_returns_home_for_the_default_workspace_probe(rig, tmp_path):
    assert make(rig, tmp_path).run(CABIN_ID, 'printf %s "$HOME"').stdout == "/srv/cabin"


def test_put_copies_the_file_and_checks_its_checksum(rig, tmp_path):
    payload = b"wheel bytes " * 5000
    source = tmp_path / "oa-wheels.tgz"
    source.write_bytes(payload)
    make(rig, tmp_path).put(CABIN_ID, source, "/tmp/oa-wheels.tgz")
    assert rig.uploaded("/tmp/oa-wheels.tgz") == payload
    assert any("sha256sum -- " in s for s in rig.scripts()) and sha(payload)


def test_put_detects_a_damaged_transfer(rig, tmp_path):
    rig.rules.append({"corrupt_upload": True})
    rig.write_rules()
    source = tmp_path / "f.bin"
    source.write_bytes(b"abc")
    with pytest.raises(MotelSandboxError, match="damaged"):
        make(rig, tmp_path).put(CABIN_ID, source, "/tmp/f.bin")


def test_stream_exec_yields_lines(rig, tmp_path):
    rig.script_rule("emit-lines", stdout="one\ntwo\n")
    proc = make(rig, tmp_path).stream_exec(CABIN_ID, "emit-lines")
    assert list(proc.lines) == ["one\n", "two\n"]
    assert proc.wait() == 0
    proc.close()


def test_exec_foreground_runs_with_a_pid_record_and_returns_the_exit_code(rig, tmp_path):
    rig.script_rule("exec sh -c", exit=3)
    code = make(rig, tmp_path).exec_foreground(CABIN_ID, "omnigent host --server https://omnigent.example.com")
    assert code == 3
    script = rig.scripts()[-1]
    assert "mkdir -m 700 /tmp/oa-foreground-" in script and "exec sh -c 'omnigent host --server https://omnigent.example.com' < /dev/null" in script


def test_wheel_install_command_overlays_shipped_wheels(rig, tmp_path):
    command = make(rig, tmp_path).wheel_install_command("/tmp/oa-wheels.tgz")
    assert "tar xzf /tmp/oa-wheels.tgz" in command and "--force-reinstall" in command


# -- the host start: pinned install, credentials, token ---------------------------------------------------------
def test_host_start_installs_the_pinned_omnigent_once_and_keeps_secrets_off_argv(rig, tmp_path):
    launcher = make(rig, tmp_path, env=["ANTHROPIC_API_KEY"], environ={"ANTHROPIC_API_KEY": KEY}, harness_packages=[])
    command = HOST_COMMAND.format(url="https://omnigent.example.com")
    for _ in range(2):
        result = launcher.run_background(CABIN_ID, command)
        assert result.returncode == 0
    scripts = rig.scripts()
    installs = [s for s in scripts if "omnigent==0.21.0" in s]
    assert len(installs) == 1  # the second start finds omnigent already ensured
    start = scripts[-1]
    assert f"export ANTHROPIC_API_KEY={KEY}" in start  # the key is only in the stdin script (no quoting needed here)
    assert TOKEN in start and "setsid nohup sh -c" in start and "/tmp/omnigent-host.log" in start
    # nothing secret in any argv, on the controller side or in the command the cabin's gate would see
    assert KEY not in argv_text(rig) and TOKEN not in argv_text(rig)
    # nothing secret on disk from the launcher
    state = Path(os.environ["XDG_STATE_HOME"])
    for path in state.rglob("*") if state.exists() else []:
        assert not path.is_file() or (KEY not in path.read_text() and TOKEN not in path.read_text())


def test_env_names_fall_back_to_the_environment_variable(rig, tmp_path):
    launcher = make(rig, tmp_path, bootstrap="none",
                    environ={"OMNIGENT_MOTEL_SANDBOX_ENV": "OPENAI_API_KEY, GIT_TOKEN", "OPENAI_API_KEY": "sk-o-1", "GIT_TOKEN": "gh-1"})
    launcher.run_background(CABIN_ID, HOST_COMMAND.format(url="https://omnigent.example.com"))
    start = rig.scripts()[-1]
    assert "export OPENAI_API_KEY=sk-o-1" in start and "export GIT_TOKEN=gh-1" in start


def test_unlisted_variables_are_not_passed(rig, tmp_path):
    launcher = make(rig, tmp_path, bootstrap="none", env=[], environ={"ANTHROPIC_API_KEY": KEY})
    launcher.run_background(CABIN_ID, HOST_COMMAND.format(url="https://omnigent.example.com"))
    assert KEY not in rig.scripts()[-1]


def test_a_failed_host_start_never_echoes_the_token_or_the_key(rig, tmp_path):
    rig.script_rule("setsid nohup", exit=1, stderr=f"sh: error near {TOKEN} and {KEY}")
    launcher = make(rig, tmp_path, bootstrap="none", env=["ANTHROPIC_API_KEY"], environ={"ANTHROPIC_API_KEY": KEY})
    with pytest.raises(MotelSandboxError) as excinfo:
        launcher.run_background(CABIN_ID, HOST_COMMAND.format(url="https://omnigent.example.com"))
    assert TOKEN not in excinfo.value.message and KEY not in excinfo.value.message
    assert "[redacted]" in excinfo.value.message


def test_auto_version_pins_the_omnigent_running_here(rig, tmp_path):
    launcher = make(rig, tmp_path, omnigent_version="auto", harness_packages=[])
    launcher.run_background(CABIN_ID, HOST_COMMAND.format(url="https://omnigent.example.com"))
    assert f"omnigent=={importlib.metadata.version('omnigent')}" in "".join(rig.scripts())


def test_bootstrap_none_installs_nothing(rig, tmp_path):
    launcher = make(rig, tmp_path, bootstrap="none")
    launcher.run_background(CABIN_ID, HOST_COMMAND.format(url="https://omnigent.example.com"))
    assert len(rig.scripts()) == 1


def test_an_install_failure_is_reported(rig, tmp_path):
    rig.script_rule("pip install", exit=1, stderr="no matching distribution")
    with pytest.raises(MotelSandboxError, match="installing omnigent .* failed"):
        make(rig, tmp_path).run_background(CABIN_ID, HOST_COMMAND.format(url="https://omnigent.example.com"))




# -- L2: a lost check-in answer ----------------------------------------------------------------------------------
def test_a_checkin_that_times_out_looks_the_cabin_up_by_its_label_and_checks_it_out(rig, monkeypatch):
    monkeypatch.setattr("omnigent.community.sandbox.motel.launcher.task_name", lambda _name, _default: "managed-x-0a1b2c3d")
    rig.rule("cabin", "checkin", sleep=3, stdout=json.dumps(CABIN))  # created, but the answer comes too late
    rig.rule("cabin", "ls", **ok(cabins=[{**CABIN, "task": "managed-x-0a1b2c3d"},
                                         {**CABIN, "lease_id": "other-0123456789ab", "task": "other"}]))
    rig.rule("cabin", "checkout", **ok(cabin={**CABIN, "state": "released"}))
    with pytest.raises(MotelSandboxError, match="did not confirm"):
        make(rig, checkin_timeout_s=0.5).provision("managed-x")
    checkouts = [a for a in rig.argv() if a[:2] == ["cabin", "checkout"]]
    assert checkouts == [["cabin", "checkout", CABIN_ID, "--json"]]  # ours only, not the unrelated cabin


def test_no_lookup_when_the_checkin_was_refused(rig):
    rig.rule("cabin", "checkin", **fail(3, "no vacancy"))
    with pytest.raises(MotelSandboxError):
        make(rig).provision("x1")
    assert [a for a in rig.argv() if a[:2] == ["cabin", "ls"]] == []


# -- L3: no command text and no secret in errors -----------------------------------------------------------------
@pytest.mark.parametrize("secret", ["sk-ant-test-0123456789abcdef", "it's a \"tricky\" $HOME key", "k1"])
def test_errors_never_carry_the_command_or_the_secret_in_any_spelling(rig, secret):
    import shlex
    rig.script_rule("run-it", exit=2, stderr=f"bad: {secret} / {shlex.quote(secret)} / {json.dumps(secret)}")
    launcher = make(rig, env=["ANTHROPIC_API_KEY"], environ={"ANTHROPIC_API_KEY": secret}, bootstrap="none")
    command = f"OMNIGENT_HOST_TOKEN={shlex.quote(TOKEN)} run-it --opt {shlex.quote(secret)}"
    with pytest.raises(MotelSandboxError) as excinfo:
        launcher.run(CABIN_ID, command)
    message = excinfo.value.message
    assert "run-it" not in message and TOKEN not in message
    for form in (secret, shlex.quote(secret), json.dumps(secret)[1:-1]):
        assert form not in message
    assert "[redacted]" in message
    result = launcher.run(CABIN_ID, command, check=False)
    assert secret not in result.stderr and shlex.quote(secret) not in result.stderr


def test_redact_handles_quoted_forms_and_short_values():
    from omnigent.community.sandbox.motel.launcher import redact
    assert redact("x 'a'\"'\"'b' y", ["a'b"]) == "x [redacted] y"
    assert redact("pin 1234 here", ["1234"]) == "pin [redacted] here"
    assert redact("nothing", [""]) == "nothing"


# -- L6: env names -----------------------------------------------------------------------------------------------
@pytest.mark.parametrize("name", ["NODE_OPTIONS", "SHELLOPTS", "BASHOPTS", "PROMPT_COMMAND", "PS4", "OMNIGENT_HOST_TOKEN",
                                  "OMNIGENT_SERVER_URL", "PATH", "LD_PRELOAD", "BASH_FUNC_x%%"])
def test_dangerous_env_names_are_refused_by_config_and_launcher(rig, name):
    from omnigent.community.sandbox.motel.config import MotelSandboxConfig
    with pytest.raises(ValueError):
        MotelSandboxConfig(env=[name])
    launcher = make(rig, environ={"OMNIGENT_MOTEL_SANDBOX_ENV": name, name: "v"})
    with pytest.raises(MotelSandboxError, match="not an allowed variable name"):
        launcher.provision("x1")
    assert rig.argv() == []


def test_ordinary_credential_names_are_accepted():
    from omnigent.community.sandbox.motel.config import MotelSandboxConfig
    MotelSandboxConfig(env=["ANTHROPIC_API_KEY", "OPENAI_API_KEY", "CLAUDE_CODE_OAUTH_TOKEN", "GIT_TOKEN"])


# -- L7: scripts are one group -----------------------------------------------------------------------------------
def test_every_script_sent_on_stdin_is_wrapped_in_one_group(rig, tmp_path):
    rig.script_rule("echo hi", stdout="hi\n")
    launcher = make(rig, bootstrap="none")
    launcher.run(CABIN_ID, "echo hi")
    launcher.run_background(CABIN_ID, HOST_COMMAND.format(url="https://omnigent.example.com"))
    launcher.stream_exec(CABIN_ID, "echo hi").close()
    scripts = rig.scripts()
    assert len(scripts) == 3
    for script in scripts:
        assert script.startswith("{\n") and script.endswith("\n}\n")


def test_a_cut_off_script_runs_nothing():
    import subprocess

    from omnigent.community.sandbox.motel.transport import wrap
    script = wrap("echo first\necho second\necho third")
    cut = script[: script.index("second")]  # the ssh session dropped half way
    out = subprocess.run(["sh", "-s"], input=cut.encode(), capture_output=True)
    assert out.stdout == b"" and out.returncode != 0
    whole = subprocess.run(["sh", "-s"], input=script.encode(), capture_output=True)
    assert whole.stdout == b"first\nsecond\nthird\n"


# -- first launch: host first, harness CLIs later ------------------------------------------------------------------
def test_harness_clis_install_in_the_background_after_omnigent(rig):
    make(rig, harness_packages=["@openai/codex@1.2.3"]).run_background(
        CABIN_ID, HOST_COMMAND.format(url="https://omnigent.example.com"))
    install = next(s for s in rig.scripts() if "omnigent==0.21.0" in s)
    assert install.index("pip install") < install.rindex("setsid nohup sh -c")
    assert "/tmp/omnigent-harness-install.log" in install and "@openai/codex@1.2.3" in install
    assert install.rstrip("\n}").rstrip().endswith("&")  # nothing waits for npm
    assert "python3-yaml" in install


def test_harness_clis_can_be_waited_for(rig):
    make(rig, harness_packages=["@openai/codex"], wait_for_harnesses=True).run_background(
        CABIN_ID, HOST_COMMAND.format(url="https://omnigent.example.com"))
    install = next(s for s in rig.scripts() if "omnigent==0.21.0" in s)
    assert "setsid nohup sh -c" not in install and "npm install -g" in install


def test_the_install_script_is_valid_shell(rig):
    import subprocess

    from omnigent.community.sandbox.motel import transport as tp
    for waited in (False, True):
        script = tp.wrap(tp.ensure_omnigent_script("0.21.0", ["@openai/codex"], waited))
        assert subprocess.run(["sh", "-n"], input=script.encode(), capture_output=True).returncode == 0
    wheels = tp.wrap(tp.wheel_install_script("/tmp/oa-wheels.tgz", ["@openai/codex"]))
    assert subprocess.run(["sh", "-n"], input=wheels.encode(), capture_output=True).returncode == 0


# -- M4: the launch token lives as long as the cabin can -----------------------------------------------------------
def test_token_ttl_is_the_cabins_max_lifetime_plus_a_margin(tmp_path):
    from omnigent.community.sandbox.motel.config import TOKEN_MARGIN_S, lease_ceiling_s, token_ttl_s
    cfg = tmp_path / "motel.toml"
    cfg.write_text('[cabins]\nmax_lifetime = "3d"\n')
    assert lease_ceiling_s(str(cfg)) == 3 * 86400
    assert token_ttl_s(str(cfg)) == 3 * 86400 + TOKEN_MARGIN_S
    cfg.write_text("[cabins]\n")  # the default is 7d
    assert lease_ceiling_s(str(cfg)) == 7 * 86400


@pytest.mark.parametrize("content", [None, "not toml [[[", '[cabins]\nmax_lifetime = "forever"\n',
                                     '[cabins]\nmax_lifetime = "30d"\n'])
def test_token_ttl_falls_back_to_motels_own_ceiling(tmp_path, content):
    from omnigent.community.sandbox.motel.config import HARD_MAX_LIFETIME_S, lease_ceiling_s
    cfg = tmp_path / "motel.toml"
    if content is not None:
        cfg.write_text(content)
    assert lease_ceiling_s(str(cfg)) == HARD_MAX_LIFETIME_S == 14 * 86400


# -- round 3: N2 short values, N4 recorded host, N5 harness failure hint ----------------------------------------
@pytest.mark.parametrize("name,value", [("CI", "1"), ("REGION", "a"), ("APP_ENV", "srv")])
def test_a_short_passed_value_does_not_rewrite_parsed_output(rig, tmp_path, name, value):
    """N2: the $HOME probe is parsed by Omnigent (prepare_workspace); REGION=a must not turn /srv/cabin into garbage."""
    launcher = make(rig, tmp_path, env=[name], environ={name: value}, bootstrap="none")
    assert launcher.run(CABIN_ID, 'printf %s "$HOME"').stdout == "/srv/cabin"


def test_a_long_secret_is_still_removed_from_output_and_any_value_from_errors(rig, tmp_path):
    rig.script_rule("leaky", exit=1, stdout=f"key={KEY}", stderr="short=a and " + KEY)
    launcher = make(rig, tmp_path, env=["ANTHROPIC_API_KEY", "REGION"],
                    environ={"ANTHROPIC_API_KEY": KEY, "REGION": "a"}, bootstrap="none")
    result = launcher.run(CABIN_ID, "leaky", check=False)
    assert KEY not in result.stdout and "[redacted]" in result.stdout
    assert KEY not in result.stderr and "short=a" not in result.stderr  # stderr and errors keep full redaction
    with pytest.raises(MotelSandboxError) as excinfo:
        launcher.run(CABIN_ID, "leaky")
    assert KEY not in excinfo.value.message


def test_the_host_start_records_its_process_group_in_a_pidfile(rig, tmp_path):
    make(rig, tmp_path, bootstrap="none").run_background(CABIN_ID, HOST_COMMAND.format(url="https://omnigent.example.com"))
    start = rig.scripts()[-1]
    assert 'echo $$ > "$HOME/.local/state/motel-omnigent/host.pid"' in start and "setsid nohup sh -c" in start
    assert 'mkdir -p "$(dirname "$HOME/.local/state/motel-omnigent/host.pid")"' in start
    assert "/tmp/omnigent-host.pid" not in start  # tmpfiles aging must not be able to remove it


# Tests that run the real stop script must never let its pkill fallback reach live hosts on the test machine.
NO_MATCH = "^motel-test-no-match$"


def _spawn_recorded_host(pidfile):
    """A stand-in for the supervisor: a session leader that records its pid, as the real wrapper does."""
    script = f"echo $$ > {pidfile}; sleep 60"
    proc = subprocess.Popen(["setsid", "sh", "-c", script], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    deadline = time.time() + 5
    while time.time() < deadline and not (pidfile.exists() and pidfile.read_text().strip()):
        time.sleep(0.05)
    return proc


def test_resume_stops_the_recorded_host_group_and_nothing_else(tmp_path):
    from omnigent.community.sandbox.motel.launcher import stop_host_script
    pidfile = tmp_path / "host.pid"
    host = _spawn_recorded_host(pidfile)
    assert pidfile.read_text().strip(), "the stand-in host never recorded its pid"
    bystander = subprocess.Popen(["sleep", "60"])  # not the host: must survive
    try:
        subprocess.run(["sh", "-c", stop_host_script(str(pidfile), pattern=NO_MATCH)], check=True, timeout=20)
        assert host.wait(5) is not None
        assert bystander.poll() is None
        assert not pidfile.exists()
    finally:
        for p in (host, bystander):
            if p.poll() is None:
                p.kill()


def test_a_recorded_pid_that_is_not_the_host_is_left_alone(tmp_path):
    """A reused pid: the record names a live process whose command line does not mention the pidfile."""
    from omnigent.community.sandbox.motel.launcher import stop_host_script
    pidfile = tmp_path / "host.pid"
    other = subprocess.Popen(["sleep", "60"])
    pidfile.write_text(f"{other.pid}\n")
    try:
        subprocess.run(["sh", "-c", stop_host_script(str(pidfile), pattern=NO_MATCH)], check=True, timeout=20)
        assert other.poll() is None
    finally:
        other.kill()


def _fake_pip_host(root):
    """What a cabin has: ``venv/bin/omnigent`` is a pip console script, a python shebang, reached through a symlink on
    PATH. Its command line is therefore ``.../venv/bin/python3 .../omnigent host ...``."""
    venv, bin_ = root / "venv" / "bin", root / "bin"
    venv.mkdir(parents=True)
    bin_.mkdir()
    py = venv / "python3"
    py.symlink_to(shutil.which("python3") or sys.executable)
    script = venv / "omnigent"
    script.write_text(f"#!{py}\nimport signal, sys, time\nsignal.signal(signal.SIGTERM, lambda *a: sys.exit(143))\n"
                      "time.sleep(60)\n")
    script.chmod(0o755)
    (bin_ / "omnigent").symlink_to(script)
    return bin_


def test_the_fallback_pattern_matches_the_real_host_command_line_and_nothing_else(tmp_path):
    """L2. pgrep only LISTS matches (a real host may run on this machine), and only our own pids are ever signalled."""
    from omnigent.community.sandbox.motel.launcher import HOST_PATTERN
    bin_ = _fake_pip_host(tmp_path)
    host = subprocess.Popen(["omnigent", "host", "--server", "http://127.0.0.1:1"],
                            env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"})
    # a shell and a file whose command line merely mention the words
    mention = subprocess.Popen(["sh", "-c", "sleep 60 # omnigent host"])
    editor = subprocess.Popen(["sleep", "61"], env={**os.environ, "NOTE": "omnigent host"})
    try:
        time.sleep(0.5)
        matched = subprocess.run(["pgrep", "-f", HOST_PATTERN], capture_output=True, text=True).stdout.split()
        assert str(host.pid) in matched, "the fallback pattern does not match a pip-installed host"
        assert str(mention.pid) not in matched and str(editor.pid) not in matched
        # the same pattern in the real script, with the signal swapped for a listing
        script = stop_host_script_for_test(tmp_path / "none.pid")
        listed = subprocess.run(["sh", "-c", script], capture_output=True, text=True, timeout=20).stdout.split()
        assert str(host.pid) in listed
    finally:
        for p in (host, mention, editor):
            p.kill()
            p.wait()


def stop_host_script_for_test(pidfile):
    """The real fallback branch with ``pkill`` swapped for ``pgrep``: it lists instead of signalling."""
    from omnigent.community.sandbox.motel.launcher import stop_host_script
    script = stop_host_script(str(pidfile))
    assert "pkill -f" in script
    listed = script.replace("pkill -f", "pgrep -f")
    assert "pkill" not in listed  # nothing in this script may signal a process
    return listed


def test_a_term_to_the_host_alone_ends_the_supervisor_loop_instead_of_restarting_it(tmp_path):
    """The fallback signals only the host. The host exits 143 and the loop stands down (it would restart on a crash)."""
    from omnigent.onboarding.sandboxes.base import supervise_host_command
    bin_ = _fake_pip_host(tmp_path)
    loop = subprocess.Popen(["sh", "-c", supervise_host_command("omnigent host --server http://127.0.0.1:1")],
                            env={**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}"}, start_new_session=True)
    try:
        host = None
        for _ in range(50):
            kids = subprocess.run(["pgrep", "-P", str(loop.pid)], capture_output=True, text=True).stdout.split()
            if kids:
                host = int(kids[0])
                break
            time.sleep(0.1)
        assert host, "the supervisor never started the host"
        time.sleep(0.3)
        os.kill(host, signal.SIGTERM)  # a child of our own loop only
        assert loop.wait(10) == 143
    finally:
        if loop.poll() is None:
            os.killpg(loop.pid, signal.SIGKILL)


def test_a_failure_says_when_the_background_harness_install_failed(rig, tmp_path):
    rig.script_rule("omnigent-harness-install.failed", stdout="failed\n")
    rig.script_rule("run-the-agent", exit=127, stderr="claude: command not found")
    launcher = make(rig, tmp_path, bootstrap="pip")
    with pytest.raises(MotelSandboxError) as excinfo:
        launcher.run(CABIN_ID, "run-the-agent")
    assert "harness CLI install failed" in excinfo.value.message and "omnigent-harness-install.log" in excinfo.value.message


def test_no_harness_hint_when_the_install_did_not_fail(rig, tmp_path):
    rig.script_rule("run-the-agent", exit=1, stderr="boom")
    launcher = make(rig, tmp_path, bootstrap="pip")
    with pytest.raises(MotelSandboxError) as excinfo:
        launcher.run(CABIN_ID, "run-the-agent")
    assert "harness" not in excinfo.value.message


def test_the_background_install_writes_a_marker_on_failure():
    script = tp.harness_install(["@x/y"], wait=False)
    assert "omnigent-harness-install.failed" in script and "rm -f /tmp/omnigent-harness-install.failed" in script


# -- L3: the failure marker is logged and retried ------------------------------------------------------------------
@pytest.fixture
def marker_present(rig):
    rig.script_rule("omnigent-harness-install.failed", stdout="failed\n")
    return rig


def _warnings(caplog):
    return [r for r in caplog.records if r.levelno == logging.WARNING and "harness CLI install failed" in r.getMessage()]


def test_keep_alive_logs_one_warning_per_cabin_when_the_install_failed(marker_present, tmp_path, caplog):
    rig = marker_present
    rig.rule("cabin", "extend", **ok(cabin=CABIN))
    with caplog.at_level(logging.WARNING, logger="omnigent.community.sandbox.motel"):
        for _ in range(3):
            make(rig, tmp_path, bootstrap="pip").keep_alive(CABIN_ID)
    assert len(_warnings(caplog)) == 1
    probes = [s for s in rig.scripts() if "omnigent-harness-install.failed" in s]
    assert len(probes) == 1  # asked once, not on every keep-alive


def test_keep_alive_does_not_probe_when_nothing_installs_in_the_background(marker_present, tmp_path, caplog):
    rig = marker_present
    rig.rule("cabin", "extend", **ok(cabin=CABIN))
    with caplog.at_level(logging.WARNING, logger="omnigent.community.sandbox.motel"):
        make(rig, tmp_path, bootstrap="none").keep_alive(CABIN_ID)
        make(rig, tmp_path, bootstrap="pip", wait_for_harnesses=True).keep_alive(CABIN_ID)
    assert not _warnings(caplog) and not rig.scripts()


def test_resume_logs_the_warning_and_retries_the_install(marker_present, tmp_path, caplog):
    rig = marker_present
    rig.rule("cabin", "status", **ok(cabin=CABIN))
    rig.rule("cabin", "extend", **ok(cabin=CABIN))
    with caplog.at_level(logging.WARNING, logger="omnigent.community.sandbox.motel"):
        make(rig, tmp_path, bootstrap="pip").resume(CABIN_ID)
    assert len(_warnings(caplog)) == 1
    last = rig.scripts()[-1]
    assert "motel-omnigent/host.pid" in last and "npm install -g" in last
    assert "if [ -e /tmp/omnigent-harness-install.failed ]" in last


def test_a_launch_retries_a_failed_install_only_while_the_marker_exists(rig, tmp_path):
    make(rig, tmp_path, bootstrap="pip").run_background(CABIN_ID, HOST_COMMAND.format(url="https://omnigent.example.com"))
    ensure = [s for s in rig.scripts() if "omnigent==" in s][0]
    guard = ensure.index("if [ -e /tmp/omnigent-harness-install.failed ]")
    assert guard < ensure.index("exit 0") < ensure.index("pip install")  # the retry sits in the already-installed branch
    assert "npm install -g" in ensure[guard:ensure.index("exit 0")]


def test_the_marker_is_cleared_when_the_install_succeeds():
    script = tp.harness_install(["@x/y"], wait=False)
    assert "then rm -f /tmp/omnigent-harness-install.failed" in script
    assert "echo failed > /tmp/omnigent-harness-install.failed" in script
