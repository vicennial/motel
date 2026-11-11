"""The real ssh transport, driven through a fake process: fixed commands, forwards, caps, and the token on stdin only."""
import io
import os
import shlex
import subprocess

import pytest
from doubles import OWN_TOKEN

from motel.tours import transport as tp
from motel.tours.guest import runner_argv
from platform_marks import linux_only

TARGET = tp.Target(lease_id="a-1-abc", alias="cabin-a-1", address="192.0.2.10", key_path="/etc/motel/keys/k",
                   known_hosts="/etc/motel/keys/kh")


class FakeProc:
    def __init__(self, payload=b"", status=0, hang=False):
        read, write = os.pipe()
        os.write(write, payload)
        if not hang:
            os.close(write)
        self._write = write if hang else None
        self.stdout = os.fdopen(read, "rb", buffering=0)
        self.stdin = io.BytesIO()
        self.stdin.close = lambda: None           # keep the buffer readable after the transport closes it
        self.returncode, self.status, self.hang, self.pid = (None if hang else status), status, hang, 2_000_000_000

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        self.returncode = self.status if self.returncode is None else self.returncode
        return self.returncode

    def kill(self):
        self.returncode = -9
        if self._write is not None:
            os.close(self._write)
            self._write = None


class Spawner:
    def __init__(self, *procs):
        self.procs, self.calls = list(procs), []

    def __call__(self, argv, **kw):
        self.calls.append((argv, kw))
        proc = self.procs.pop(0)
        return proc


def make(*procs, clock=None):
    spawn = Spawner(*procs)
    transport = tp.SshTransport(run_as=("motel-cabin", 990, 990), runner_argv=runner_argv(), exists=lambda p: p.endswith("setpriv"),
                                popen=spawn, monotonic=clock or __import__("time").monotonic)
    return transport, spawn


READY = b'{"profile": "tour-guest", "tour_nft": "loaded", "allowed_ips": 3, "guest_user": true}'


def test_every_ssh_runs_as_the_unprivileged_user_with_a_clean_environment():
    transport, spawn = make(FakeProc(READY))
    transport.preflight(TARGET, 7781)
    argv, kw = spawn.calls[0]
    assert argv[0].endswith("setpriv") and "--reuid" in argv and "990" in argv and "--no-new-privs" in argv
    assert kw["env"] == {"PATH": "/usr/bin:/bin", "LANG": "C.UTF-8"} and kw["start_new_session"] is True


def test_the_reverse_forward_can_end_at_the_attempts_own_listener():
    transport, spawn = make(FakeProc(READY))
    transport.preflight(TARGET, 17781, forward_to=17850)
    assert "127.0.0.1:17781:127.0.0.1:17850" in spawn.calls[0][0]


def test_the_reverse_forward_is_opened_only_for_preflight_and_run():
    transport, spawn = make(FakeProc(READY), FakeProc(), FakeProc(b"[]"), FakeProc())
    transport.preflight(TARGET, 7781)
    forwarded = spawn.calls[0][0]
    assert "-R" in forwarded and "127.0.0.1:7781:127.0.0.1:7781" in forwarded and "ClearAllForwardings=no" in forwarded
    assert "ExitOnForwardFailure=yes" in forwarded
    transport.stop(TARGET)
    plain = spawn.calls[1][0]
    assert "-R" not in plain and "ClearAllForwardings=yes" in plain


def test_remote_commands_are_fixed_and_the_name_of_an_input_is_checked():
    transport, _ = make()
    assert transport.run_command() == ("sudo -n -u guest -H -- /usr/bin/python3 -I /usr/local/lib/motel-tour/runner.py")
    card = tp.SshTransport.put_command("card.json")
    assert "/var/lib/motel-guest/in/card.json" in card and "umask 027" in card and "chgrp guest" in card
    assert "install -d" not in card                   # the input directory was made by cloud-init: never re-moded here
    runner = tp.SshTransport.put_command("adapters.py")
    assert "/usr/local/lib/motel-tour/adapters.py" in runner and "umask 022" in runner
    for bad in ("../card.json", "x; rm -rf /", "id_rsa", "run.json"):
        with pytest.raises(tp.TransportError):
            tp.SshTransport.put_command(bad)


def test_preflight_requires_the_tour_setup_to_be_finished_and_works_without_a_forward():
    transport, spawn = make(FakeProc(READY))
    transport.preflight(TARGET, None)
    assert "-R" not in spawn.calls[0][0] and spawn.calls[0][0][-1] == "/bin/cat /var/lib/motel-guest/tour-ready.json"
    for payload in (b"", b"not json", b'{"tour_nft": "missing", "guest_user": true}', b'{"tour_nft": "loaded"}', b"[]"):
        transport, _ = make(FakeProc(payload))
        with pytest.raises(tp.TransportError):
            transport.preflight(TARGET, 7781)
    transport, _ = make(FakeProc(READY, status=255))
    with pytest.raises(tp.TransportError, match="reverse forward"):
        transport.preflight(TARGET, 7781)


def test_put_streams_the_file_and_reports_a_failed_copy(tmp_path):
    src = tmp_path / "f"
    src.write_bytes(b"data")
    transport, spawn = make(FakeProc(status=0), FakeProc(status=3))
    transport.put(TARGET, "card.json", str(src))
    with pytest.raises(tp.TransportError, match="exit 3"):
        transport.put(TARGET, "runner.py", str(src))
    assert spawn.calls[0][1]["stdin"].name == str(src)


def test_the_own_login_token_goes_on_stdin_and_in_no_argv_or_env(tmp_path):
    proc = FakeProc(b"output\n")
    transport, spawn = make(proc)
    result = transport.run(TARGET, forward_port=7781, timeout=5, cancel=lambda: False, log_path=str(tmp_path / "log"),
                           stdin_line=(OWN_TOKEN + "\n").encode())
    argv, kw = spawn.calls[0]
    assert proc.stdin.getvalue() == (OWN_TOKEN + "\n").encode()
    assert OWN_TOKEN not in " ".join(argv) and OWN_TOKEN not in repr(kw["env"])
    assert result.exit_status == 0 and (tmp_path / "log").read_bytes() == b"output\n"
    assert oct((tmp_path / "log").stat().st_mode & 0o777) == "0o600"


def test_without_a_token_the_runner_gets_no_stdin(tmp_path):
    transport, spawn = make(FakeProc())
    transport.run(TARGET, forward_port=7781, timeout=5, cancel=lambda: False, log_path=str(tmp_path / "log"))
    assert spawn.calls[0][1]["stdin"] == __import__("subprocess").DEVNULL


def test_cancel_and_timeout_kill_the_process(tmp_path):
    transport, _ = make(FakeProc(hang=True))
    result = transport.run(TARGET, forward_port=1, timeout=30, cancel=lambda: True, log_path=str(tmp_path / "a"))
    assert result.cancelled is True and result.exit_status is None
    ticks = iter(range(0, 1000, 100))
    transport, _ = make(FakeProc(hang=True), clock=lambda: next(ticks))
    result = transport.run(TARGET, forward_port=1, timeout=30, cancel=lambda: False, log_path=str(tmp_path / "b"))
    assert result.timed_out is True


def test_the_runner_log_is_capped(tmp_path, monkeypatch):
    monkeypatch.setattr(tp, "LOG_CAP", 10)
    transport, _ = make(FakeProc(b"x" * 100))
    result = transport.run(TARGET, forward_port=1, timeout=5, cancel=lambda: False, log_path=str(tmp_path / "log"))
    assert (tmp_path / "log").read_bytes() == b"x" * 10 and result.output_bytes == 10


def test_a_log_path_that_is_a_symlink_is_refused(tmp_path):
    (tmp_path / "real").write_text("x")
    os.symlink(str(tmp_path / "real"), str(tmp_path / "log"))
    transport, _ = make(FakeProc())
    with pytest.raises(OSError):
        transport.run(TARGET, forward_port=1, timeout=5, cancel=lambda: False, log_path=str(tmp_path / "log"))


def test_listing_parses_find_output_and_survives_hostile_names():
    payload = b"f 12 run.json\0d 4096 artifacts\0l 9 evil\0f 3 odd name\nwith newline\0"
    transport, spawn = make(FakeProc(payload))
    entries = transport.list_out(TARGET)
    assert [(e.kind, e.size) for e in entries] == [("f", 12), ("d", 4096), ("l", 9), ("f", 3)]
    assert entries[3].path == "odd name\nwith newline"
    assert spawn.calls[0][0][-1] == "sudo -n -u guest /bin/sh -c " + shlex.quote(tp.LIST_SCRIPT)


@linux_only
def test_the_listing_script_runs_under_real_find_from_a_directory_it_cannot_read(tmp_path):
    """The real command, not a fake: options in the right place, and it does not trip over an unreadable starting
    directory (the ssh account's home is closed to the guest)."""
    if not os.path.exists("/usr/bin/find") or os.geteuid() == 0:
        pytest.skip("needs GNU find and a non-root user")
    out = tmp_path / "out"
    (out / "artifacts").mkdir(parents=True)
    (out / "run.json").write_text("{}")
    os.symlink("/etc/passwd", str(out / "link"))
    closed = tmp_path / "closed"
    closed.mkdir()
    script = tp.LIST_SCRIPT.replace(tp.OUT_DIR, str(out))
    proc = subprocess.run(["/bin/sh", "-c", f"cd {shlex.quote(str(closed))} && chmod 000 . && {script}"],
                          capture_output=True, check=False)
    closed.chmod(0o755)
    assert proc.returncode == 0, proc.stderr
    records = sorted(r.decode() for r in proc.stdout.split(b"\0") if r)
    assert [(r.split(" ")[0], r.split(" ", 2)[2]) for r in records] == [("d", "artifacts"), ("f", "run.json"), ("l", "link")]
    assert "f 2 run.json" in records


def test_fetch_enforces_the_byte_cap_and_the_path_check():
    transport, _ = make(FakeProc(b"x" * 100))
    with pytest.raises(tp.TooLarge):
        b"".join(transport.fetch(TARGET, "result.bundle", max_bytes=10))
    transport, _ = make()
    for bad in ("../x", "/etc/passwd", "a b"):
        with pytest.raises(tp.TransportError):
            list(transport.fetch(TARGET, bad, max_bytes=10))
    transport, spawn = make(FakeProc(b"abc"))
    assert b"".join(transport.fetch(TARGET, "artifacts/a.json", max_bytes=10)) == b"abc"
    assert spawn.calls[0][0][-1].endswith("/out/artifacts/a.json")


def test_a_failed_remote_read_is_an_error():
    transport, _ = make(FakeProc(b"", status=1))
    with pytest.raises(tp.TransportError, match="exit 1"):
        list(transport.fetch(TARGET, "run.json", max_bytes=10))


def test_without_setpriv_or_runuser_the_channel_is_refused():
    transport = tp.SshTransport(run_as=("motel-cabin", 990, 990), runner_argv=runner_argv(), exists=lambda p: False, popen=Spawner())
    with pytest.raises(tp.TransportError):
        transport.argv(TARGET, "true")
