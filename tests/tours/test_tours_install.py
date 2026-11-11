"""Installer wiring for tours: sudoers, housekeeping, config, the concierge's user/units/directories, doctor."""
import io
import os
import stat
import tomllib
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

import pytest

from motel import installer
from motel.cabins import desk as cabin_desk
from motel.config import Config
from motel.tours import desk as tour_desk

ROOT = Path(__file__).resolve().parents[2]
SRC = ROOT / "src"
USER_VERBS = ("start", "ls", "status", "wait", "result", "fetch", "logs", "cancel")
OPERATOR_VERBS = ("supervise", "reap", "bake", "image", "own-login")


def commands(text):
    return [c.strip(" \\\n") for c in text.split("NOPASSWD:")[1].split(",")]


def test_the_sudoers_verbs_match_the_desk_verbs():
    assert installer.TOUR_USER_VERBS == tour_desk.USER_VERBS == USER_VERBS
    assert installer.CABIN_USER_VERBS == cabin_desk.USER_VERBS


def test_sudoers_pins_each_user_verb_and_never_a_wildcard_tour_rule():
    lines = commands(installer.render_sudoers(["alice", "@agents"], cabins=True, tours=True))
    desk = installer.DESK
    for verb in USER_VERBS:
        assert f"{desk} tour {verb} *" in lines
    assert f"{desk} tour ls" in lines
    assert f"{desk} tour *" not in lines and not any(line.endswith("tour") for line in lines)
    for verb in OPERATOR_VERBS:
        assert not any(f"tour {verb}" in line for line in lines)
    assert f"{desk} cabin checkin *" in lines                            # the cabin rules are still there
    assert "alice,%agents ALL=(root) NOPASSWD:" in installer.render_sudoers(["alice", "@agents"], tours=True)


def test_without_tours_there_is_no_tour_rule():
    assert " tour " not in installer.render_sudoers(["alice"], cabins=True)


def test_sudoers_validates_with_visudo(tmp_path):
    import shutil
    import subprocess
    visudo = shutil.which("visudo", path="/usr/sbin:/usr/bin")
    if visudo is None:
        pytest.skip("visudo is not installed")
    path = tmp_path / "rule"
    path.write_text(installer.render_sudoers(["alice", "@agents"], cabins=True, tours=True))
    result = subprocess.run([visudo, "-cf", str(path)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr


def test_the_tour_reaper_has_its_own_unit_and_timer_with_its_own_timeout():
    from motel.tours import install as tinstall
    service = tinstall.render_tours_service()
    assert f"ExecStart={installer.DESK} tour reap --apply --json" in service and "TimeoutStartSec=15min" in service
    assert "Type=oneshot" in service and "ExecStopPost" not in service
    timer = tinstall.render_tours_timer()
    assert f"Unit={tinstall.TOURS_SERVICE}" in timer and "OnUnitActiveSec=10min" in timer
    for text in (installer.render_service(),):
        assert "tour reap" not in text and "tours" not in text               # the rooms unit knows nothing of tours
    cron = [x for x in installer.render_cron(cabins=True, tours=True).splitlines() if not x.startswith("#")]
    assert any("tour reap --apply" in x for x in cron) and not any("tour reap" in x for x in installer.render_cron())


def test_the_default_tours_config_is_valid_and_own_login_is_off():
    text = installer.render_config(["alice"], "hetzner", True, True)
    cfg = Config.from_dict(tomllib.loads(text))
    assert cfg.tours.enabled is True and cfg.tours.own_login_enabled is False
    assert cfg.tours.concierge_port == 7781


class Run:
    def __init__(self, prefix):
        self.prefix = prefix

    def __call__(self, *args):
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = installer.main(["--prefix", str(self.prefix), "--src", str(SRC), "--no-visudo", *args])
        return code, out.getvalue(), err.getvalue()


@pytest.fixture
def run(tmp_path):
    prefix = tmp_path / "root"
    prefix.mkdir()
    return Run(prefix)


def test_a_tours_install_creates_the_concierge_pieces(run):
    code, out, err = run("--users", "alice", "--cabins", "--tours")
    assert code == 0, err
    p = run.prefix
    mode = lambda path: stat.S_IMODE((p / path).stat().st_mode)  # noqa: E731
    assert mode("etc/motel/credentials/own-login") == 0o700 and mode("etc/motel/credentials") == 0o700
    assert mode("var/lib/motel/tours") == 0o700
    assert mode("etc/motel/llm-keys") == 0o750 and mode("var/lib/motel-concierge") == 0o700
    assert mode("usr/local/sbin/motel-concierge") == 0o755 and mode("etc/systemd/system/motel-concierge.service") == 0o644
    service = (p / "etc/systemd/system/motel-concierge.service").read_text()
    assert "ExecStart=/usr/local/sbin/motel-concierge\n" in service and "--port" not in service
    assert "FileDescriptorStoreMax=512\n" in service and "Environment=MOTEL_FDSTORE_MAX=512\n" in service and "NotifyAccess=main\n" in service     # listeners survive a restart
    assert "SocketMode=0600" in (p / "etc/systemd/system/motel-concierge.socket").read_text()
    assert "motel.concierge.service" in (p / "usr/local/sbin/motel-concierge").read_text()
    assert (p / "usr/local/lib/motel/motel/concierge/service.py").is_file()
    assert stat.S_IMODE((p / "etc/motel/motel.toml").stat().st_mode) == 0o644      # readable by the concierge: no secrets
    assert "tour reap" not in (p / "etc/systemd/system/motel-housekeeping.service").read_text()
    assert "tour reap --apply" in (p / "etc/systemd/system/motel-tours-housekeeping.service").read_text()
    assert "motel-tours-housekeeping.service" in (p / "etc/systemd/system/motel-tours-housekeeping.timer").read_text()
    assert "tour start *" in (p / "etc/sudoers.d/motel").read_text()
    assert Config.load(str(p / "etc/motel/motel.toml")).tours.enabled is True


def test_without_tours_nothing_of_them_is_installed(run):
    assert run("--users", "alice", "--cabins")[0] == 0
    p = run.prefix
    for path in ("etc/systemd/system/motel-concierge.service", "usr/local/sbin/motel-concierge", "etc/motel/llm-keys",
                 "var/lib/motel/tours", "var/lib/motel-concierge", "etc/motel/credentials/own-login",
                 "etc/systemd/system/motel-tours-housekeeping.service", "etc/systemd/system/motel-tours-housekeeping.timer"):
        assert not (p / path).exists(), path
    assert " tour " not in (p / "etc/sudoers.d/motel").read_text()


def test_the_existing_config_decides_about_tours(run):
    run("--users", "alice", "--cabins")
    code, out, _ = run("--tours")
    assert code == 0 and "does not enable [tours]" in out
    assert not (run.prefix / "etc/systemd/system/motel-concierge.service").exists()
    config = run.prefix / "etc/motel/motel.toml"
    config.write_text(config.read_text() + '\n[tours]\nenabled = true\nconcierge_port = 7790\n')
    assert run()[0] == 0
    assert (run.prefix / "etc/systemd/system/motel-concierge.service").is_file()


def test_tours_need_cabins(run):
    code, _out, err = run("--users", "alice", "--tours")
    assert code != 0 and "cabins" in err
    assert not (run.prefix / "etc/systemd/system/motel-concierge.service").exists()


def test_a_dry_run_writes_nothing(run):
    code, out, _ = run("--users", "alice", "--cabins", "--tours", "--dry-run")
    assert code == 0 and "motel-concierge.service" in out and list(run.prefix.iterdir()) == []


def test_the_installer_creates_the_unprivileged_inspection_user(run):
    from motel.tours import install as tinstall
    user = tinstall.inspect_user()
    assert user.name == "motel-inspect" and user.group == "motel-inspect"
    args = user.useradd_args()
    assert "--system" in args and "--no-create-home" in args and args[args.index("--shell") + 1] == "/usr/sbin/nologin"
    code, out, _ = run("--users", "alice", "--cabins", "--tours", "--dry-run")
    assert code == 0 and "ensure system user and group motel-inspect" in out
    code, _o, _e = run("--users", "alice", "--cabins", "--tours")
    assert code == 0 and stat.S_IMODE((run.prefix / "var/lib/motel-inspect").stat().st_mode) == 0o711


@pytest.mark.parametrize("version,preserved", [(255, True), (254, True), (252, False), (None, False)])
def test_the_concierge_unit_is_written_for_the_systemd_that_runs(run, monkeypatch, version, preserved):
    from motel.concierge import install as cinstall
    monkeypatch.setattr(cinstall, "detect_systemd_version", lambda *a, **k: version)
    code, _out, err = run("--users", "alice", "--cabins", "--tours")
    assert code == 0, err
    unit = (run.prefix / "etc/systemd/system/motel-concierge.service").read_text()
    assert ("FileDescriptorStorePreserve=yes" in unit) is preserved and "FileDescriptorStoreMax=" in unit and "Environment=MOTEL_FDSTORE_MAX=" in unit


# -- the concierge unit lifecycle: what is written and which commands run ---------------------------------------------

class Host(installer.Installer):
    """A 'real' root install laid out below a directory, so the systemctl steps are taken (and recorded, not run)."""

    def __init__(self, base, *, dry_run=False):
        self.lines = []
        super().__init__(str(base), str(SRC), dry_run=dry_run, log=self.lines.append)
        self.real = True

    def at(self, path):
        return str(Path(self.prefix) / path.lstrip("/"))

    def _own(self, path):
        pass


class Commands(list):
    def __call__(self, argv, **kw):
        from types import SimpleNamespace
        self.append(" ".join(argv))
        return SimpleNamespace(returncode=0, stderr="")


UNITS = "etc/systemd/system"
ENABLE = ["systemctl enable --now motel-concierge.socket", "systemctl enable --now motel-concierge.service"]


def lifecycle(base, *, version=255, systemd=True, dry=False):
    from motel.concierge import install as cinstall
    from motel.tours import install as tinstall
    host, run = Host(base, dry_run=dry), Commands()
    tinstall.install_units(host, cinstall, systemd=systemd, version=version, run=run)
    return host.lines, list(run)


def unit_file(base, name="motel-concierge.service"):
    return Path(base, UNITS, name)


def test_a_first_install_writes_both_units_reloads_and_enables_the_socket_then_the_service(tmp_path):
    lines, commands = lifecycle(tmp_path)
    assert (unit_file(tmp_path).stat().st_mode & 0o777) == 0o644 and unit_file(tmp_path, "motel-concierge.socket").exists()
    assert "FileDescriptorStorePreserve=yes" in unit_file(tmp_path).read_text()
    assert commands == ["systemctl daemon-reload"] + ENABLE                      # nothing to restart: the service is new
    assert [line for line in lines if line.startswith("write")] == [
        f"write /{UNITS}/motel-concierge.service 644", f"write /{UNITS}/motel-concierge.socket 644"]
    assert not any("not_preserved" in line or "warning" in line for line in lines)


def test_an_unchanged_rerun_writes_nothing_and_does_not_reload_or_restart(tmp_path):
    lifecycle(tmp_path)
    before = {p: p.stat().st_mtime_ns for p in unit_file(tmp_path).parent.iterdir()}
    lines, commands = lifecycle(tmp_path)
    assert commands == ENABLE
    assert not any(line.startswith("write") for line in lines) and "the concierge units are up to date" in lines
    assert before == {p: p.stat().st_mtime_ns for p in unit_file(tmp_path).parent.iterdir()}


def test_an_upgrade_from_the_misspelled_key_unit_rewrites_it_and_restarts_the_service(tmp_path):
    from motel.concierge import install as cinstall
    old = cinstall.unit_text(systemd_version=255).replace("FileDescriptorStoreMax=", "FileDescriptorStore=")
    old = "\n".join(line for line in old.splitlines() if not line.startswith(("Environment=MOTEL_FDSTORE", "FileDescriptorStorePreserve")))
    unit_file(tmp_path).parent.mkdir(parents=True)
    unit_file(tmp_path).write_text(old + "\n")
    unit_file(tmp_path, "motel-concierge.socket").write_text(cinstall.socket_unit_text())
    assert cinstall.unit_problems(old)
    lines, commands = lifecycle(tmp_path)
    assert cinstall.unit_problems(unit_file(tmp_path).read_text()) == []
    assert commands == ["systemctl daemon-reload", "systemctl restart motel-concierge.service"] + ENABLE
    assert [line for line in lines if line.startswith("write")] == [f"write /{UNITS}/motel-concierge.service 644"]
    note = next(line for line in lines if "not_preserved" in line)
    assert "upgrade between tours" in note


def test_a_unit_that_already_keeps_its_store_is_restarted_without_the_rollout_note(tmp_path):
    from motel.concierge import install as cinstall
    lifecycle(tmp_path, version=252)
    lines, commands = lifecycle(tmp_path, version=255)           # systemd was upgraded: the unit gains Preserve=yes
    assert "FileDescriptorStorePreserve=yes" in unit_file(tmp_path).read_text()
    assert commands == ["systemctl daemon-reload", "systemctl restart motel-concierge.service"] + ENABLE
    assert not any("not_preserved" in line for line in lines)
    assert cinstall.unit_problems(unit_file(tmp_path).read_text()) == []


def test_a_changed_socket_unit_is_reloaded_but_the_service_is_not_restarted(tmp_path):
    lifecycle(tmp_path)
    unit_file(tmp_path, "motel-concierge.socket").write_text("[Socket]\nListenStream=/tmp/other\n")
    lines, commands = lifecycle(tmp_path)
    assert commands == ["systemctl daemon-reload"] + ENABLE
    assert [line for line in lines if line.startswith("write")] == [f"write /{UNITS}/motel-concierge.socket 644"]


@pytest.mark.parametrize("version", [252, 100])
def test_on_systemd_before_254_the_unit_has_no_preserve_line(tmp_path, version):
    lines, commands = lifecycle(tmp_path, version=version)
    text = unit_file(tmp_path).read_text()
    assert "FileDescriptorStorePreserve" not in text and "MOTEL_FDSTORE_SIGNAL" not in text
    assert "FileDescriptorStoreMax=" in text and "Environment=MOTEL_FDSTORE_MAX=" in text
    assert commands == ["systemctl daemon-reload"] + ENABLE and not any("warning" in line for line in lines)


def test_an_unknown_systemd_version_warns_and_writes_the_older_shape(tmp_path):
    lines, commands = lifecycle(tmp_path, version=None)
    warning = next(line for line in lines if line.startswith("warning:"))
    assert "unknown" in warning and "254" in warning
    assert "FileDescriptorStorePreserve" not in unit_file(tmp_path).read_text()
    assert commands == ["systemctl daemon-reload"] + ENABLE


def test_a_dry_run_writes_nothing_and_runs_nothing_but_prints_the_plan(tmp_path):
    lines, commands = lifecycle(tmp_path, dry=True)
    assert commands == [] and not unit_file(tmp_path).exists()
    assert [line for line in lines if line.startswith("would run")] == ["would run: systemctl daemon-reload"] + [
        "would run: " + c for c in ENABLE]
    assert sum(line.startswith("write") for line in lines) == 2


def test_a_dry_run_of_an_upgrade_plans_the_restart_and_leaves_the_old_unit(tmp_path):
    from motel.concierge import install as cinstall
    unit_file(tmp_path).parent.mkdir(parents=True)
    unit_file(tmp_path).write_text("[Service]\nFileDescriptorStore=64\n")
    lines, commands = lifecycle(tmp_path, dry=True)
    assert commands == [] and unit_file(tmp_path).read_text() == "[Service]\nFileDescriptorStore=64\n"
    assert "would run: systemctl restart motel-concierge.service" in lines and any("not_preserved" in line for line in lines)
    assert cinstall.unit_problems(unit_file(tmp_path).read_text())


def test_no_systemd_writes_the_files_and_runs_no_command(tmp_path):
    lines, commands = lifecycle(tmp_path, systemd=False)
    assert commands == [] and unit_file(tmp_path).exists() and unit_file(tmp_path, "motel-concierge.socket").exists()
    assert any(line.startswith("note: --no-systemd") and "motel-concierge.service" in line for line in lines)


def test_a_unit_that_fails_the_verify_step_fails_the_install_with_a_clear_message(tmp_path, monkeypatch):
    from motel.concierge import install as cinstall
    from motel.errors import Usage
    monkeypatch.setattr(cinstall, "unit_text", lambda *a, **k: "[Service]\nExecStart=/bin/true\n")
    with pytest.raises(Usage, match="not usable.*FileDescriptorStoreMax=.*MOTEL_FDSTORE_MAX") as caught:
        lifecycle(tmp_path)
    assert "motel install --upgrade" in str(caught.value.hint if hasattr(caught.value, "hint") else caught.value)


def test_a_failing_systemctl_is_reported_and_does_not_stop_the_install(tmp_path):
    from types import SimpleNamespace

    from motel.concierge import install as cinstall
    from motel.tours import install as tinstall
    host = Host(tmp_path)
    tinstall.install_units(host, cinstall, systemd=True, version=255, run=lambda argv, **kw: SimpleNamespace(returncode=1, stderr="boom"))
    assert any(line.startswith("warning: `systemctl daemon-reload` failed: boom") for line in host.lines)


def test_cron_mode_reaps_tours_from_its_own_cron_line_and_starts_no_unit(run):
    code, out, err = run("--users", "alice", "--cabins", "--tours", "--cron")
    assert code == 0, err
    cron = (run.prefix / "etc/cron.d/motel").read_text().splitlines()
    assert sum("tour reap --apply" in line for line in cron) == 1 and sum("cabin reap --apply" in line for line in cron) == 1
    assert not (run.prefix / "etc/systemd/system/motel-tours-housekeeping.timer").exists()
    assert "note: --no-systemd or --cron: nothing is reloaded or started" in out


def test_a_tours_install_creates_the_cabin_and_the_inspection_directories_with_their_modes(run):
    code, out, err = run("--users", "alice", "--cabins", "--tours")
    assert code == 0, err
    p = run.prefix
    mode = lambda path: stat.S_IMODE((p / path).stat().st_mode)           # noqa: E731
    assert mode("var/lib/motel-cabin") == 0o750 and mode("var/lib/motel-cabin/keys") == 0o2750
    assert mode("var/lib/motel-inspect") == 0o711
    assert "ensure system user and group motel-cabin" in out and "ensure system user and group motel-inspect" in out
    for name in ("motel-housekeeping", "motel-cabins-housekeeping", "motel-tours-housekeeping"):    # three separate passes
        assert (p / f"etc/systemd/system/{name}.timer").exists() and (p / f"etc/systemd/system/{name}.service").exists()


def configure(run, *, tours, cabins="enabled = true"):
    """Install with tours, then rewrite the tables the way an operator edits motel.toml."""
    assert run("--users", "alice", "--cabins", "--tours")[0] == 0
    config = run.prefix / "etc/motel/motel.toml"
    text = config.read_text().replace("[tours]\nenabled = true", f"[tours]\n{tours}")
    config.write_text(text.replace("[cabins]\nenabled = true", f"[cabins]\n{cabins}"))
    return config


def sudoers(run):
    return (run.prefix / "etc/sudoers.d/motel").read_text()


def test_the_tour_reaper_stays_while_tour_state_exists_after_the_tours_table_is_removed(run):
    config = configure(run, tours="enabled = false")
    import re
    config.write_text(re.sub(r"\[tours\.own_login\].*?(?=\n\[|\Z)", "", config.read_text(), flags=re.S).replace(
        "[tours]\nenabled = false", "[ignored]\nenabled = false"))
    assert "tours" not in tomllib.loads(config.read_text())
    state = run.prefix / "var/lib/motel/tours/t-0123456789ab"
    state.mkdir(parents=True, exist_ok=True)
    assert run("--users", "alice", "--upgrade")[0] == 0
    units = run.prefix / "etc/systemd/system"
    assert (units / "motel-tours-housekeeping.timer").exists()                  # unfinished tours still bill: keep the reaper
    state.rmdir()
    assert run("--users", "alice", "--upgrade")[0] == 0
    assert not (units / "motel-tours-housekeeping.timer").exists() and not (units / "motel-tours-housekeeping.service").exists()


# -- L3: the tours CLEANUP mode mirrors cabins ----------------------------------------------------------------------------

READ_AND_END = ("ls", "status", "wait", "result", "fetch", "logs", "cancel")


def test_cleanup_sudoers_keeps_the_read_and_end_verbs_but_not_start():
    desk = installer.DESK
    lines = commands(installer.render_sudoers(["alice"], cabins=installer.CABINS_ON, tours=installer.TOURS_CLEANUP))
    for verb in READ_AND_END:
        assert f"{desk} tour {verb} *" in lines
    assert f"{desk} tour ls" in lines
    assert not any(" tour start" in line for line in lines)
    assert not any(f"tour {verb}" in line for line in lines for verb in OPERATOR_VERBS)
    on = commands(installer.render_sudoers(["alice"], cabins=installer.CABINS_ON, tours=installer.TOURS_ON))
    assert f"{desk} tour start *" in on and installer.TOUR_CLEANUP_VERBS == READ_AND_END
    assert " tour " not in installer.render_sudoers(["alice"], cabins=installer.CABINS_ON, tours=installer.TOURS_OFF)


@pytest.mark.parametrize("config,state,cabins,wanted,expected", [
    (None, False, installer.CABINS_ON, False, installer.TOURS_OFF),
    (None, False, installer.CABINS_ON, True, installer.TOURS_ON),
    (None, True, installer.CABINS_ON, False, installer.TOURS_CLEANUP),
    ({}, False, installer.CABINS_ON, False, installer.TOURS_OFF),
    ({}, True, installer.CABINS_ON, False, installer.TOURS_CLEANUP),
    ({"tours": {"enabled": False}}, False, installer.CABINS_ON, False, installer.TOURS_CLEANUP),
    ({"tours": {"enabled": True}}, False, installer.CABINS_ON, False, installer.TOURS_ON),
    ({"tours": {"enabled": True, "max_guests_per_tour": 0}}, False, installer.CABINS_ON, False, installer.TOURS_ON),   # raw flag
    ({"tours": {"enabled": "yes"}}, False, installer.CABINS_ON, False, installer.TOURS_CLEANUP),
    ({"tours": {"enabled": True}}, False, installer.CABINS_CLEANUP, False, installer.TOURS_CLEANUP),                # L5
    ({"tours": {"enabled": True}}, False, installer.CABINS_OFF, False, installer.TOURS_CLEANUP),
])
def test_tours_mode(config, state, cabins, wanted, expected):
    assert installer.tours_mode(config, state_exists=state, wanted=wanted, cabins=cabins) is expected


def test_disabling_tours_keeps_the_concierge_the_reaper_and_the_read_verbs_but_removes_start(run):
    configure(run, tours="enabled = false")
    assert run("--users", "alice", "--upgrade")[0] == 0
    text = sudoers(run)
    assert "tour status *" in text and "tour cancel *" in text and "tour start" not in text
    units = run.prefix / "etc/systemd/system"
    assert (units / "motel-tours-housekeeping.timer").exists() and (units / "motel-concierge.service").exists()
    assert "tour reap --apply" in (units / "motel-tours-housekeeping.service").read_text()


def test_a_config_without_tours_and_without_tour_state_installs_no_tour_rule(run):
    assert run("--users", "alice", "--cabins")[0] == 0
    assert " tour " not in sudoers(run) and not (run.prefix / "etc/systemd/system/motel-tours-housekeeping.timer").exists()


# -- L5: an upgrade is never blocked by tours without cabins -----------------------------------------------------------------

def test_an_upgrade_with_tours_but_no_cabins_installs_tours_in_cleanup_mode_with_a_note(run):
    configure(run, tours="enabled = true", cabins="enabled = false")
    code, out, err = run("--users", "alice", "--upgrade")
    assert code == 0, err
    assert "note: [tours] is enabled but [cabins] is not" in out and "cleanup mode" in out
    text = sudoers(run)
    assert "tour status *" in text and "tour start" not in text
    assert (run.prefix / "etc/systemd/system/motel-tours-housekeeping.timer").exists()


def test_a_new_install_that_asks_for_tours_without_cabins_is_still_refused(run):
    code, _out, err = run("--users", "alice", "--tours")
    assert code != 0 and "tours run on cabins" in err


# -- L4 and L5 in the doctor ---------------------------------------------------------------------------------------------------

def doctor_checks(fx):
    import json
    _code, out, _err = fx.desk("doctor", "--json")
    return {c["check"]: c for c in json.loads(out)["checks"]}


def write_sudoers(fx, **kw):
    path = fx.paths.real("/etc/sudoers.d/motel")
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w") as handle:
        handle.write(installer.render_sudoers(["alice", "@agents"], **kw))


def test_an_invalid_tours_table_does_not_also_report_a_sudoers_mismatch():
    from helpers import BASE_TOML, Fixture
    cabins = '\n[cabins]\nenabled = true\ntypes = ["cpx32"]\nmax_eur_per_hour = 0.2\n'
    fx = Fixture(BASE_TOML + cabins + "[tours]\nenabled = true\nmax_guests_per_tour = 0\n")
    try:
        write_sudoers(fx, cabins=installer.CABINS_ON, tours=installer.TOURS_ON)      # what the installer writes for enabled = true
        checks = doctor_checks(fx)
        assert checks["[tours] is valid"]["level"] == "fail"
        assert checks["sudoers rule matches allow_users"]["level"] == "ok"
    finally:
        fx.close()


def test_the_doctor_warns_when_tours_are_enabled_without_cabins_and_expects_the_cleanup_rule():
    from helpers import BASE_TOML, Fixture
    cabins = '\n[cabins]\nenabled = false\ntypes = ["cpx32"]\nmax_eur_per_hour = 0.2\n'
    fx = Fixture(BASE_TOML + cabins + "[tours]\nenabled = true\n")
    try:
        write_sudoers(fx, cabins=installer.CABINS_CLEANUP, tours=installer.TOURS_CLEANUP)
        checks = doctor_checks(fx)
        assert checks["tours have cabins"]["level"] == "warn" and "cleanup mode" in checks["tours have cabins"]["detail"]
        assert checks["sudoers rule matches allow_users"]["level"] == "ok"
    finally:
        fx.close()
