"""Why a cabin is still booking: the guest channel's last error, a TCP probe, the guest's own boot report, and an abort."""
import json
import os
import re
import subprocess
import sys
from datetime import timedelta

import pytest
from cabin_fakes import Clock, RealGuestChannel, World, read_lib

from motel.cabins import guestchan, userdata
from motel.cabins.errors import CheckinAborted, Fail, GuestUnknown
from motel.cabins.guestchan import GuestChannel, probe_tcp, safe_detail, validate_ready

REC = {"lease_id": "build-0123456789ab", "name": "motel-build-0123456789ab",
       "access": {"mode": "ssh-allowlist", "address": "203.0.113.7"}}


# ------------------------------------------------------------------ the guest channel keeps the ssh client's last words

@pytest.mark.parametrize("text", [
    "systemctl enable --now cabin-guest-firewall.service failed", "kex_exchange_identification: Connection closed by remote host",
    "failed: line 28: systemctl enable --now motel-tour-firewall.service", "Connection timed out during banner exchange",
    "a" * 31 + " and " + "B1" * 15,
])
def test_safe_detail_keeps_ordinary_words_readable(text):
    assert safe_detail(text) == text


def test_safe_detail_keeps_the_last_informative_line_not_the_first():
    err = ("Warning: Permanently added '203.0.113.7' (ED25519) to the list of known hosts.\n"
           "@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@@\n"
           "@    WARNING: REMOTE HOST IDENTIFICATION HAS CHANGED!     @\n"
           "Host key verification failed.\n")
    assert safe_detail(err) == "Host key verification failed."
    assert safe_detail("Welcome to the cabin\nPermission denied (publickey).\n") == "Permission denied (publickey)."
    assert safe_detail("@@@@@\n@ WARNING @\n") == "@ WARNING @"                  # nothing else: the warning itself is shown
    assert safe_detail("\n\n") == ""


def test_safe_detail_is_one_clean_line_without_paths_or_tokens():
    raw = b"\nLoad key \"/var/lib/motel-cabin/keys/a-1/controller_ed25519\": invalid format\n"
    assert safe_detail(raw) == 'Load key "<path>": invalid format'
    assert safe_detail("sk-ant-api03-" + "A" * 40 + " rejected") == "<redacted> rejected"
    assert safe_detail("key " + "AbC1" * 10 + " rejected") == "key <redacted> rejected"            # 32+ characters, letters and digits
    assert safe_detail("blob " + "a" * 70) == "blob <redacted>"                                     # 64+ characters of anything
    assert safe_detail("ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIabc") == "ssh-ed25519 <redacted>"
    assert safe_detail(b"caf\xc3\xa9 \x1b[31mred") == "caf? ?[31mred"
    assert safe_detail("word " * 100) == ("word " * 100)[:120] and safe_detail(b"") == "" and safe_detail(None) == ""


def test_capture_runner_returns_a_bounded_copy_of_stderr():
    argv = [sys.executable, "-c", "import sys\nsys.stderr.write('ssh: refused\\n' + 'e' * 1000000)\nprint('{}')"]
    rc, out, err = guestchan.capture_runner(argv, 20)
    assert rc == 0 and out.strip() == b"{}" and err.startswith(b"ssh: refused\n") and len(err) == guestchan.STDERR_CAP


def test_capture_runner_still_kills_output_over_the_cap():
    argv = [sys.executable, "-c", "import sys\nwhile True: sys.stdout.write('x'*65536); sys.stdout.flush()"]
    with pytest.raises(GuestUnknown, match="too large"):
        guestchan.capture_runner(argv, 20, cap=100_000)


def channel(result):
    def runner(argv, timeout):
        if isinstance(result, Exception):
            raise result
        return result
    return GuestChannel("/nonexistent", runner=runner)


def test_a_failed_query_carries_the_exit_code_and_the_last_stderr_line():
    stderr = b"Warning: Permanently added '203.0.113.7' (ED25519) to the list of known hosts.\nssh: connect to host 203.0.113.7 port 22: Connection refused\n"
    with pytest.raises(GuestUnknown) as caught:
        channel((255, b"", stderr)).call(REC, "ready")
    exc = caught.value
    assert exc.rc == 255 and exc.detail == "ssh: connect to host 203.0.113.7 port 22: Connection refused"
    assert str(exc) == f"guest query exit 255: {exc.detail}"


def test_a_two_value_runner_still_works_and_gives_the_bare_exit_code():
    with pytest.raises(GuestUnknown) as caught:
        channel((255, b"")).call(REC, "ready")
    assert str(caught.value) == "guest query exit 255" and caught.value.rc == 255 and caught.value.detail == ""


def test_a_timeout_is_named_as_one():
    with pytest.raises(GuestUnknown, match="timed out after 60s") as caught:
        channel(subprocess.TimeoutExpired("ssh", 60)).call(REC, "ready")
    assert caught.value.detail == "timeout"


# ------------------------------------------------------------------ a plain TCP probe tells a drop from a refusal

class Conn:
    def __init__(self, banner=b"", error=None):
        self.banner, self.error = banner, error

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def settimeout(self, _t):
        pass

    def recv(self, _n):
        if self.error:
            raise self.error
        return self.banner


def connect_with(result):
    def connect(_addr, timeout=None):
        if isinstance(result, Exception):
            raise result
        return result
    return connect


@pytest.mark.parametrize("result,expected", [
    (Conn(b"SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.5\r\nrest"), "open (SSH-2.0-OpenSSH_9.6p1 Ubuntu-3ubuntu13.5)"),
    (Conn(b"HTTP/1.1 400"), "open (no ssh banner)"),
    (Conn(error=TimeoutError()), "open (no ssh banner)"),
    (ConnectionRefusedError(), "refused"),
    (TimeoutError(), "timeout"),
    (TimeoutError(), "timeout"),
    (OSError(113, "No route to host"), "error 113"),
])
def test_probe_tcp(result, expected):
    assert probe_tcp("203.0.113.7", connect=connect_with(result)) == expected


# ------------------------------------------------------------------ the guest's own boot report

def test_validate_ready_keeps_a_cleaned_boot_report_for_an_unfinished_bootstrap():
    doc = {"schema": "motel-guest/v1", "bootstrap": "running",
           "boot": {"stage": "tour-firewall", "cloud_init": "error", "last": "failed: line 28: systemctl restart ssh.socket", "x": 1}}
    assert validate_ready(doc) == {"bootstrap": "running", "boot": {"stage": "tour-firewall", "cloud_init": "error",
                                                                    "last": "failed: line 28: systemctl restart ssh.socket"}}


@pytest.mark.parametrize("boot", [None, [], "x", {"stage": 5, "cloud_init": "A B", "last": 7}, {"stage": "x" * 90}])
def test_validate_ready_drops_a_hostile_boot_report(boot):
    assert validate_ready({"bootstrap": "running", "boot": boot}) == {"bootstrap": "running", "boot": {}}


@pytest.fixture
def ch(tmp_path):
    return RealGuestChannel(str(tmp_path), Clock())


def status_file(tmp_path, v1):
    path = tmp_path / "status.json"
    path.write_text(json.dumps({"v1": v1}))
    return str(path)


def test_the_guest_reports_its_last_stage_and_last_line(ch, tmp_path):
    log = tmp_path / "boot.log"
    log.write_text("x" * 5000 + "\nstage: packages\nReading package lists...\nstage: tour-firewall\n"
                   "failed: line 28: systemctl enable --now motel-tour-firewall.service\n")
    boot = ch.g.boot(log=str(log), status=status_file(tmp_path, {"modules-final": {"errors": ["scripts-user failed"], "finished": 1.0}}))
    assert boot == {"stage": "tour-firewall", "cloud_init": "error",
                    "last": "failed: line 28: systemctl enable --now motel-tour-firewall.service",
                    "errors": ["failed: line 28: systemctl enable --now motel-tour-firewall.service"]}


def test_the_guest_reports_apts_own_error_lines_not_only_the_last_line(ch, tmp_path):
    log = tmp_path / "boot.log"
    log.write_text("stage: packages\nErr:1 http://mirror.example noble/main amd64 git 1.0\n  Could not connect\n"
                   + "Reading package lists...\n" * 300
                   + "E: Failed to fetch http://mirror.example/pool/main/g/git/git_1.0_amd64.deb  Could not connect\n"
                   "E: Unable to fetch some archives\nfailed: line 19: apt-get install -y -q git\n")
    boot = ch.g.boot(log=str(log), status=str(tmp_path / "none"))
    assert boot["errors"] == ["E: Failed to fetch http://mirror.example/pool/main/g/git/git_1.0_amd64.deb  Could not connect",
                              "E: Unable to fetch some archives", "failed: line 19: apt-get install -y -q git"]
    assert boot["last"] == "failed: line 19: apt-get install -y -q git"


def test_validate_ready_keeps_at_most_three_cleaned_error_lines():
    doc = {"bootstrap": "running", "boot": {"errors": ["E: one", "E: two", "E: three", "E: four\x1b[31m"]}}
    errors = validate_ready(doc)["boot"]["errors"]
    assert len(errors) == 3 and errors[0] == "E: two" and "\x1b" not in errors[-1]
    assert "errors" not in validate_ready({"bootstrap": "running", "boot": {"errors": [1, 2]}})["boot"]
    assert "errors" not in validate_ready({"bootstrap": "running", "boot": {"errors": "E: x"}})["boot"]


def test_a_chatty_stage_cannot_hide_its_name(ch, tmp_path):
    """apt prints far more than the log tail the guest reads: the stage still comes from the file the stage wrote."""
    log = tmp_path / "boot.log"
    log.write_text("stage: packages\n" + "Get:1 http://mirror.example/pool/main/x.deb 100 kB\n" * 400 + "Setting up foo (1.0) ...\n")
    (tmp_path / "guest" / "boot-stage").write_text("packages\n")
    boot = ch.g.boot(log=str(log), status=str(tmp_path / "none"))
    assert boot["stage"] == "packages" and boot["last"] == "Setting up foo (1.0) ..."
    assert len(log.read_bytes()) > 4096


def test_the_stage_file_wins_over_the_log_tail_and_junk_in_it_is_ignored(ch, tmp_path):
    log = tmp_path / "boot.log"
    log.write_text("stage: access\nsomething\n")
    stage = tmp_path / "guest" / "boot-stage"
    stage.write_text("disk\n")
    assert ch.g.boot(log=str(log), status=str(tmp_path / "none"))["stage"] == "disk"
    stage.write_text("not a stage name!\x00\n")
    assert ch.g.boot(log=str(log), status=str(tmp_path / "none"))["stage"] == "access"     # falls back to the log tail


@pytest.mark.parametrize("v1,expected", [
    ({"init": {"errors": [], "finished": 1.0}, "modules-final": {"errors": [], "finished": None}}, "running"),
    ({"modules-final": {"errors": [], "finished": 2.0}, "errors": []}, "done"),
    ({"init": {"errors": ["boom"]}, "modules-final": {}}, "error"),
    ({}, "unknown"),
])
def test_the_guest_reads_cloud_inits_verdict(ch, tmp_path, v1, expected):
    assert ch.g.boot(log=str(tmp_path / "none"), status=status_file(tmp_path, v1))["cloud_init"] == expected


def test_the_guest_reports_nothing_when_it_has_nothing(ch, tmp_path):
    assert ch.g.boot(log=str(tmp_path / "none"), status=str(tmp_path / "none")) == {"stage": None, "cloud_init": "unknown", "last": None}


def test_ready_carries_the_boot_report_only_while_the_bootstrap_is_unfinished(ch):
    ch.g.security_probe = lambda: {}
    ch.g.boot = lambda: {"stage": "access", "cloud_init": "running", "last": "stage: access"}
    with open(os.path.join(ch.root, "ready.json"), "w") as handle:
        json.dump({"bootstrap": "running"}, handle)
    assert ch.g.ready()["boot"] == {"stage": "access", "cloud_init": "running", "last": "stage: access"}
    with open(os.path.join(ch.root, "ready.json"), "w") as handle:
        json.dump({"bootstrap": "done"}, handle)
    assert "boot" not in ch.g.ready()


# ------------------------------------------------------------------ the controller records what it saw, and can be told to stop

@pytest.fixture
def world(tmp_path):
    return World(str(tmp_path))


def refusing(world, detail="ssh: connect to host 203.0.113.10 port 22: Connection refused"):
    def call(rec, *words, timeout=60):
        raise GuestUnknown(f"guest query exit 255: {detail}", rc=255, detail=detail)
    world.guest.call = call


def test_the_deadline_message_names_the_last_channel_error_and_the_tcp_probe(world):
    refusing(world)
    seen = []
    c = world.controller(tcp_probe=lambda ip, port: "refused")
    with pytest.raises(Fail, match=r"bootstrap deadline passed: guest not ready \(guest query exit 255: ssh: connect.*Connection refused; tcp/22 refused\)"):
        world.checkin(c, on_boot=seen.append)
    last = seen[-1]
    assert last["channel"]["rc"] == 255 and last["channel"]["tcp22"] == "refused" and "Connection refused" in last["channel"]["error"]
    assert last["polls"] > 10 and last["ready"] is None and last["at"] and last["summary"].startswith("guest not ready")
    assert not world.hz.servers and not world.hz.firewalls


def test_the_tcp_probe_runs_about_once_a_minute_not_every_poll(world):
    refusing(world)
    calls = []
    c = world.controller(tcp_probe=lambda ip, port: calls.append(ip) or "timeout")
    with pytest.raises(Fail):
        world.checkin(c)
    assert 15 <= len(calls) <= 21                      # 20 minutes of polls, one probe a minute


def test_a_probe_that_fails_does_not_end_the_wait(world):
    refusing(world)

    def boom(ip, port):
        raise RuntimeError("no route")
    with pytest.raises(Fail, match="bootstrap deadline"):
        world.checkin(world.controller(tcp_probe=boom))


def test_the_deadline_message_names_the_guests_boot_stage(world):
    world.guest.ready_doc = {"schema": "motel-guest/v1", "bootstrap": "running",
                             "boot": {"stage": "tour-firewall", "cloud_init": "error", "last": "failed: line 28: systemctl enable now"}}
    seen = []
    with pytest.raises(Fail) as caught:
        world.checkin(on_boot=seen.append)
    assert ("guest bootstrap running (stage tour-firewall; cloud-init error; last line: failed: line 28: systemctl enable now)"
            in caught.value.message)
    assert seen[-1]["ready"] == {"bootstrap": "running", "boot": {"stage": "tour-firewall", "cloud_init": "error",
                                                                  "last": "failed: line 28: systemctl enable now"}}
    assert seen[-1]["channel"] is None


def test_a_hook_that_raises_does_not_end_the_wait(world):
    def hook(info):
        raise RuntimeError("disk full")
    world.guest.ready_doc = {"schema": "motel-guest/v1", "bootstrap": "running"}
    with pytest.raises(Fail, match="bootstrap deadline"):
        world.checkin(on_boot=hook)


def test_an_abort_ends_the_wait_at_once_and_deletes_every_resource(world):
    world.guest.ready_doc = {"schema": "motel-guest/v1", "bootstrap": "running"}
    polls = []
    started = world.clock()
    with pytest.raises(CheckinAborted, match="checkin aborted; cleanup: released"):
        world.checkin(on_boot=polls.append, abort=lambda: len(polls) >= 3)
    assert len(polls) == 3 and world.clock() - started < timedelta(minutes=2)      # not the twenty minutes of the deadline
    assert not world.hz.servers and not world.hz.firewalls and not world.hz.primary_ips
    rec = world.store.all()[0]
    assert rec["state"] == "released" and rec["cleanup_reason"] == "checkin_aborted"


def ledger_events(world):
    with open(os.path.join(world.store.root, "guestbook.jsonl"), encoding="utf-8") as handle:
        return [json.loads(line)["event"] for line in handle if line.strip()]


def test_an_abort_asked_before_the_first_poll_still_cleans_up(world):
    with pytest.raises(CheckinAborted):
        world.checkin(abort=lambda: True)
    assert not world.hz.servers and not world.hz.firewalls


def test_an_abort_during_the_firewall_and_volume_creation_never_creates_a_server(world, monkeypatch):
    created = []
    ctl = world.controller()
    real = ctl.hz.create_server
    monkeypatch.setattr(ctl.hz, "create_server", lambda *a, **k: created.append(1) or real(*a, **k))
    with pytest.raises(CheckinAborted, match="checkin aborted; cleanup: released"):
        world.checkin(ctl, abort=lambda: True)
    assert created == [] and not world.hz.servers and not world.hz.firewalls
    rec = world.store.all()[0]
    assert rec["state"] == "released" and rec["cleanup_reason"] == "checkin_aborted"


def test_an_abort_is_ledgered_as_an_abort_and_a_failure_as_a_failure(world):
    world.guest.ready_doc = {"schema": "motel-guest/v1", "bootstrap": "running"}
    with pytest.raises(CheckinAborted):
        world.checkin(abort=lambda: True)
    events = ledger_events(world)
    assert "checkin_aborted" in events and "checkin_failed" not in events
    with pytest.raises(Fail, match="bootstrap deadline"):
        world.checkin()
    assert "checkin_failed" in ledger_events(world)


# ------------------------------------------------------------------ what the guest is sent

def render(template, **over):
    kw = {"rec": {"lease_id": "build-0123456789ab", "name": "motel-build-0123456789ab", "expires_at": "2026-11-08T14:00:00Z"},
          "mode": "ssh-allowlist", "host_private": "-----BEGIN OPENSSH PRIVATE KEY-----\n" + ("H" * 70 + "\n") * 3 + "-----END OPENSSH PRIVATE KEY-----\n",
          "host_public": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + "H" * 43, "client_public": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + "C" * 43,
          "controller_public": "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAI" + "C" * 43, "guest_agent": b"print(1)\n", "packages": ["git"],
          "sources": ["203.0.113.10/32"]}
    kw.update(over)
    return userdata.render_user_data(template, **kw)


def test_comment_only_lines_of_the_template_are_not_sent_but_the_markers_stay():
    template = read_lib("cloud-init.yaml")
    text = render(template)
    assert text.startswith("#cloud-config\n") and "#!/bin/bash\n" in text
    assert not [line for line in text.splitlines()[1:] if line.lstrip().startswith("#") and not line.lstrip().startswith("#!")]


def test_the_stripping_leaves_the_template_file_and_the_filled_in_values_alone():
    out = userdata._strip_comments("#cloud-config\n# gone\n#\na: 1\n  # gone too\n    #!/bin/bash\nb: '#kept'\nc: @@V@@\n")
    assert out == "#cloud-config\na: 1\n    #!/bin/bash\nb: '#kept'\nc: @@V@@\n"
    keep = "#cloud-config\n#include\n#include https://example.test/x\n#includedir /etc/x\n#!/bin/sh\n"
    assert userdata._strip_comments(keep) == keep
    assert render(read_lib("cloud-init.yaml"), packages=["git"]).count("Never enable xtrace") == 0
    assert "Never enable xtrace" in read_lib("cloud-init.yaml")


def _is_comment(line):
    return re.match(r"^\s*#( |$)", line) is not None


def _without_comments(node):
    if isinstance(node, str):
        return "\n".join(x for x in node.split("\n") if not _is_comment(x))
    if isinstance(node, list):
        return [_without_comments(x) for x in node]
    if isinstance(node, dict):
        return {k: _without_comments(v) for k, v in node.items()}
    return node


def test_stripping_changes_nothing_but_comment_lines(monkeypatch):
    """The stripped user data and the unstripped one are the same text but for comment lines, and parse to the same YAML
    (comment lines inside the embedded scripts aside)."""
    template = read_lib("cloud-init.yaml")
    kwargs = {"packages": ["git", "curl"]}
    stripped = render(template, **kwargs)
    monkeypatch.setattr(userdata, "_strip_comments", lambda text: text)
    unstripped = render(template, **kwargs)
    assert len(stripped) < len(unstripped)
    assert [x for x in unstripped.splitlines() if not _is_comment(x)] == stripped.splitlines()
    yaml = pytest.importorskip("yaml")
    assert _without_comments(yaml.safe_load(unstripped)) == yaml.safe_load(stripped)


def test_the_bootstrap_names_its_stages_and_the_command_that_failed():
    script = render(read_lib("cloud-init.yaml"))
    stages = [line.split()[1] for line in script.splitlines() if line.strip().startswith("stage ")]
    assert stages == ["packages", "firewall", "access", "disk", "finish"]
    assert "stage() { echo \"stage: $1\"; printf '%s\\n' \"$1\" > /var/lib/motel-guest/boot-stage || true; }" in script
    assert "set -Eeuo pipefail" in script                  # -E: the ERR trap also fires inside functions and subshells
    assert """trap 'echo "failed: line $LINENO: $BASH_COMMAND"' ERR""" in script
    assert script.index("exec >>/var/log/cabin-bootstrap.log") < script.index("trap 'echo") < script.index("stage() {") < script.index("stage packages")
