"""Tunnel lifetime: the process-wide registry, the watcher, orphan ssh sessions, the stdio handshake and recovery.

Two rules for these tests, taken from how Omnigent and motel really behave:

* Omnigent builds a FRESH launcher for every launch, keep-alive and teardown, so each operation here uses ``make()``.
* The real ``motel cabin ssh`` runs ssh as a CHILD process. The fake ``motel`` does the same (it runs the relay
  program as a child), so ending only the CLI would orphan it, and these tests see that.
"""

import json
import logging
import os
import socket
import subprocess
import sys
import threading
import time

import pytest

pytest.importorskip("omnigent.onboarding.sandboxes.base")  # everything below needs Omnigent

from omnigent.onboarding.sandboxes.base import SandboxGoneError  # noqa: E402

from omnigent.community.sandbox.motel import transport as tp  # noqa: E402
from omnigent.community.sandbox.motel.launcher import MotelSandboxError  # noqa: E402
from omnigent_provider.fakes import CABIN, CABIN_ID, fail, ok  # noqa: E402
from omnigent_provider.helpers import HOST_COMMAND, connect_retry, make, port_open, seed, wait_until  # noqa: E402


def relay_spawns(rig):
    return sum(1 for a in rig.argv() if a[:2] == ["cabin", "ssh"] and "python3" in a)


def group_gone(pid):
    try:
        os.killpg(pid, 0)
    except ProcessLookupError:
        return True
    return False


@pytest.fixture
def up(rig, monkeypatch, echo_server, free_port):
    """Launch a host through a loopback tunnel. Returns (cabin_port, server_port)."""
    seed(rig)
    rig.rule("cabin", "extend", **ok(cabin=CABIN))
    rig.rule("cabin", "status", **ok(cabin=CABIN))
    cabin_port = free_port()  # one machine plays both ends here, so the cabin side listens on another port
    monkeypatch.setenv("FAKE_RELAY_LISTEN_PORT", str(cabin_port))
    make(rig, bootstrap="none").run_background(CABIN_ID, HOST_COMMAND.format(url=f"http://127.0.0.1:{echo_server}"))
    assert connect_retry(cabin_port) == b"through the tunnel"
    return cabin_port, echo_server


# -- H1: fresh launcher per operation ---------------------------------------------------------------------------
def test_keep_alive_and_terminate_on_fresh_launchers_use_the_tunnel_the_launch_opened(rig, up):
    cabin_port, _ = up
    assert tp.REGISTRY.ids() == [CABIN_ID] and relay_spawns(rig) == 1

    assert make(rig).keep_alive(CABIN_ID) is True  # the server's keep-alive path: a new instance, tunnel is up
    assert relay_spawns(rig) == 1  # no second relay beside the live one

    make(rig).terminate(CABIN_ID)  # the server's teardown path: another new instance
    assert tp.REGISTRY.ids() == []
    assert [a for a in rig.argv() if a[:2] == ["cabin", "checkout"]]
    assert wait_until(lambda: not port_open(cabin_port), 6), "the tunnel outlived terminate"


def test_a_second_launch_for_the_same_cabin_reuses_the_live_tunnel(rig, up, echo_server):
    make(rig, bootstrap="none").run_background(CABIN_ID, HOST_COMMAND.format(url=f"http://127.0.0.1:{echo_server}"))
    assert relay_spawns(rig) == 1


def test_the_watcher_stops_for_good_when_the_cabin_is_released(rig, up):
    cabin_port, _ = up
    tunnel = tp.REGISTRY.get(CABIN_ID)
    rig.rules.clear()
    rig.rule("cabin", "status", **ok(cabin={**CABIN, "state": "released"}))
    tunnel.current.close()  # the ssh session ends because the cabin was deleted
    assert wait_until(lambda: tunnel.stop.is_set(), 6)
    assert wait_until(lambda: tp.REGISTRY.ids() == [], 2)  # Supervised.close drops the entry after it stops
    spawns = relay_spawns(rig)
    time.sleep(0.6)
    assert relay_spawns(rig) == spawns  # it does not keep trying
    assert not port_open(cabin_port)


def test_the_watcher_stops_when_the_cabin_is_not_found(rig, up):
    tunnel = tp.REGISTRY.get(CABIN_ID)
    rig.rules.clear()
    rig.rule("cabin", "status", **fail(7, "no such cabin"))
    tunnel.current.close()
    assert wait_until(lambda: tunnel.stop.is_set(), 6)
    assert wait_until(lambda: tp.REGISTRY.ids() == [], 2)  # Supervised.close drops the entry after it stops


def test_repeated_restart_failures_end_with_one_error_line(rig, up, monkeypatch, caplog):
    tunnel = tp.REGISTRY.get(CABIN_ID)
    monkeypatch.setenv("FAKE_RELAY_FAIL", "1")  # every new ssh session fails; the cabin itself is still active
    with caplog.at_level(logging.DEBUG, logger="omnigent.community.sandbox.motel"):
        tunnel.current.close()
        assert wait_until(lambda: tunnel.stop.is_set(), 10)
    assert wait_until(lambda: tp.REGISTRY.ids() == [], 2)
    noisy = [r for r in caplog.records if r.levelno >= logging.WARNING]
    assert [r.levelno for r in noisy] == [logging.ERROR]
    assert "giving up after 3 failed restarts" in noisy[0].getMessage()


def test_a_dead_tunnel_comes_back_through_keep_alive_on_a_fresh_launcher(rig, up):
    cabin_port, _ = up
    tunnel = tp.REGISTRY.get(CABIN_ID)
    tunnel.stop.set()  # keep the background watcher out of this check
    tunnel.current.close()
    tunnel.stop.clear()
    assert not port_open(cabin_port) or wait_until(lambda: not port_open(cabin_port), 5)
    assert make(rig).keep_alive(CABIN_ID) is True
    assert connect_retry(cabin_port) == b"through the tunnel"
    assert relay_spawns(rig) == 2


def test_keep_alive_without_a_tunnel_does_not_open_one(rig):
    rig.rule("cabin", "extend", **ok(cabin=CABIN))
    assert make(rig).keep_alive(CABIN_ID) is True
    assert relay_spawns(rig) == 0 and tp.REGISTRY.ids() == []


# -- M3: recovery after the server restarted --------------------------------------------------------------------
def test_wake_path_resume_reopens_the_tunnel_and_stops_the_old_host(rig, up, echo_server, monkeypatch):
    cabin_port, _ = up
    tp.REGISTRY.close_all()  # the server process restarted: the registry is empty and the old relay is gone
    assert wait_until(lambda: not port_open(cabin_port), 6)
    assert make(rig).capabilities.resume_stopped is True

    make(rig).resume(CABIN_ID)  # Omnigent's wake path calls resume(), then start_host()
    stop = [s for s in rig.scripts() if "motel-omnigent/host.pid" in s and "kill" in s]
    assert stop, "resume did not stop the recorded host"
    assert ["cabin", "extend", CABIN_ID, "--for", "2h", "--json"] in rig.argv()

    make(rig, bootstrap="none").run_background(CABIN_ID, HOST_COMMAND.format(url=f"http://127.0.0.1:{echo_server}"))
    assert connect_retry(cabin_port) == b"through the tunnel"


@pytest.mark.parametrize("response", [fail(7, "no such cabin"), ok(cabin={**CABIN, "state": "released"}),
                                      ok(cabin={**CABIN, "state": "cleanup_pending"})])
def test_resume_of_a_cabin_that_is_gone_lets_omnigent_launch_a_new_one(rig, response):
    rig.rule("cabin", "status", **response)
    with pytest.raises(SandboxGoneError):
        make(rig).resume(CABIN_ID)


def test_resume_of_a_cabin_still_starting_is_an_error_not_a_gone(rig):
    rig.rule("cabin", "status", **ok(cabin={**CABIN, "state": "bootstrapping"}))
    with pytest.raises(MotelSandboxError, match="not active"):
        make(rig).resume(CABIN_ID)


# -- M5 / r4: no orphan ssh --------------------------------------------------------------------------------------
def test_closing_a_tunnel_ends_the_whole_process_tree_not_only_the_cli(rig, monkeypatch, echo_server, free_port):
    cabin_port = free_port()
    monkeypatch.setenv("FAKE_RELAY_LISTEN_PORT", str(cabin_port))
    transport = tp.CabinTransport(str(rig.motel))
    tunnel = tp.RelayTunnel(transport, CABIN_ID, echo_server, "127.0.0.1")
    tunnel.start()
    pid = tunnel.proc.pid
    assert connect_retry(cabin_port) == b"through the tunnel"
    assert not group_gone(pid)  # motel plus its child, in a group of their own (start_new_session)
    tunnel.close()
    assert wait_until(lambda: group_gone(pid), 6), "a process of the tunnel's group is still running"
    assert wait_until(lambda: not port_open(cabin_port), 6), "the relay on the cabin side outlived close()"


def test_a_start_that_times_out_leaves_nothing_bound(rig, monkeypatch, echo_server, free_port):
    cabin_port = free_port()
    monkeypatch.setenv("FAKE_RELAY_LISTEN_PORT", str(cabin_port))
    monkeypatch.setenv("FAKE_RELAY_DELAY", "2")  # a slow ssh connect
    tunnel = tp.RelayTunnel(tp.CabinTransport(str(rig.motel)), CABIN_ID, echo_server, "127.0.0.1", ready_timeout=0.5)
    with pytest.raises(tp.TransportError, match="did not start"):
        tunnel.start()
    time.sleep(3)  # long enough for an orphaned session to have come up
    assert not port_open(cabin_port), "an orphan relay bound the port after the failed start"
    monkeypatch.delenv("FAKE_RELAY_DELAY")
    again = tp.RelayTunnel(tp.CabinTransport(str(rig.motel)), CABIN_ID, echo_server, "127.0.0.1")
    again.start()  # the restart is not blocked by the failed one
    assert connect_retry(cabin_port) == b"through the tunnel"
    again.close()


def test_stream_exec_close_ends_the_process_tree(rig, tmp_path):
    rig.script_rule("emit", stdout="x\n")
    stream = make(rig, tmp_path).stream_exec(CABIN_ID, "emit")
    assert next(iter(stream.lines)) == "x\n"
    stream.close()
    assert stream._proc.poll() is not None


# -- handshake ---------------------------------------------------------------------------------------------------
def test_stray_output_before_the_relay_is_skipped_and_reported(rig, monkeypatch, echo_server, free_port, caplog):
    cabin_port = free_port()
    monkeypatch.setenv("FAKE_RELAY_LISTEN_PORT", str(cabin_port))
    monkeypatch.setenv("FAKE_RELAY_PRELUDE", "Welcome to the cabin! \x01\x02\x03\n")
    tunnel = tp.RelayTunnel(tp.CabinTransport(str(rig.motel)), CABIN_ID, echo_server, "127.0.0.1")
    with caplog.at_level(logging.WARNING):
        tunnel.start()
    try:
        assert tunnel.relay.stray.startswith(b"Welcome to the cabin!")
        assert "printed 26 bytes before the relay started" in caplog.text
        assert connect_retry(cabin_port) == b"through the tunnel"
    finally:
        tunnel.close()


def test_a_cabin_that_prints_too_much_before_the_relay_is_an_error(rig, monkeypatch, echo_server, free_port):
    monkeypatch.setenv("FAKE_RELAY_LISTEN_PORT", str(free_port()))
    monkeypatch.setenv("FAKE_RELAY_PRELUDE", "noise " * 4000)
    tunnel = tp.RelayTunnel(tp.CabinTransport(str(rig.motel)), CABIN_ID, echo_server, "127.0.0.1", ready_timeout=10)
    with pytest.raises(tp.TransportError, match="too much output"):
        tunnel.start()


# -- tunnel modes ------------------------------------------------------------------------------------------------
def test_max_streams_reaches_the_cabin_side(rig, monkeypatch, echo_server, free_port):
    monkeypatch.setenv("FAKE_RELAY_LISTEN_PORT", str(free_port()))
    make(rig, bootstrap="none", max_streams=7).run_background(
        CABIN_ID, HOST_COMMAND.format(url=f"http://127.0.0.1:{echo_server}"))
    (argv,) = [a for a in rig.argv() if "python3" in a]
    assert argv[-3] == "cabin" and argv[-1] == "7"


def test_public_urls_are_dialled_directly(rig):
    make(rig, bootstrap="none").run_background(CABIN_ID, HOST_COMMAND.format(url="https://omnigent.example.com"))
    assert tp.REGISTRY.ids() == [] and rig.argv() == [["cabin", "ssh", CABIN_ID, "--", "sh", "-s"]]


def test_tunnel_none_never_tunnels(rig):
    make(rig, bootstrap="none", tunnel="none").run_background(CABIN_ID, HOST_COMMAND.format(url="http://127.0.0.1:6767"))
    assert tp.REGISTRY.ids() == []


@pytest.mark.parametrize("url,message", [("http://[::1]:6767", "127.0.0.1"), ("http://127.0.0.1:80", "above 1023")])
def test_unusable_loopback_urls_are_explained(rig, url, message):
    with pytest.raises(MotelSandboxError, match=message):
        make(rig, bootstrap="none").run_background(CABIN_ID, HOST_COMMAND.format(url=url))


def test_an_explicit_tunnel_mode_refuses_a_public_url(rig):
    with pytest.raises(MotelSandboxError, match="loopback"):
        make(rig, bootstrap="none", tunnel="relay").run_background(
            CABIN_ID, HOST_COMMAND.format(url="https://omnigent.example.com"))


def test_the_relay_failing_to_listen_is_a_clear_error(rig, monkeypatch, echo_server):
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    monkeypatch.setenv("FAKE_RELAY_LISTEN_PORT", str(held.getsockname()[1]))
    # the cabin side retries a busy port for a while (a previous relay may still be leaving); keep the test short
    monkeypatch.setattr("omnigent.community.sandbox.motel.relay.Relay.listen",
                        lambda self, port, wait=0.0, _orig=tp.relay.Relay.listen: _orig(self, port, wait=0.0))
    with pytest.raises(MotelSandboxError, match="did not start: cannot listen"):
        make(rig, bootstrap="none").run_background(CABIN_ID, HOST_COMMAND.format(url=f"http://127.0.0.1:{echo_server}"))
    held.close()


def test_reverse_forward_mode_runs_ssh_with_the_cabins_access_files(rig, tmp_path, monkeypatch):
    cabin_dir = tmp_path / "state" / "motel" / "cabins" / CABIN_ID
    cabin_dir.mkdir(parents=True)
    (cabin_dir / "receipt.json").write_text(json.dumps({"name": CABIN["name"]}))
    original = tp.ForwardTunnel.__init__

    def quick(self, *a, **k):
        k["settle"] = 0.5
        original(self, *a, **k)

    monkeypatch.setattr(tp.ForwardTunnel, "__init__", quick)
    make(rig, bootstrap="none", tunnel="reverse-forward").run_background(
        CABIN_ID, HOST_COMMAND.format(url="http://127.0.0.1:6767"))
    (logged,) = rig.lines("ssh.jsonl")
    assert logged[1:] == ["-F", str(cabin_dir / "ssh_config"), "-N", "-T", "-o", "ExitOnForwardFailure=yes", "-o",
                          "ClearAllForwardings=no", "-o", "BatchMode=yes", "-R", "127.0.0.1:6767:127.0.0.1:6767", "--",
                          CABIN["name"]]


# -- N1: stopping a process whose stdin writer is blocked ---------------------------------------------------------
def test_stop_process_does_not_wait_for_a_writer_blocked_on_a_full_pipe():
    """The ssh child is not reading (a stalled network). A pump blocked in write holds the file's lock, so closing stdin
    first would hang and the TERM would never be sent."""
    proc = subprocess.Popen([sys.executable, "-c", "import time; time.sleep(30)"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE, stderr=subprocess.PIPE, start_new_session=True)
    ended = []

    def writer():
        try:
            proc.stdin.write(b"x" * (1 << 20))
            proc.stdin.flush()
        except (OSError, ValueError):
            ended.append(True)

    threading.Thread(target=writer, daemon=True).start()
    time.sleep(0.5)  # the pipe buffer is full and the writer is blocked
    done = threading.Event()

    def stop():
        tp.stop_process(proc, grace=1.0)
        done.set()

    threading.Thread(target=stop, daemon=True).start()
    try:
        assert done.wait(10), "stop_process hung behind the blocked writer"
        assert proc.poll() is not None
        assert wait_until(lambda: bool(ended), 5), "the blocked writer was not released"
    finally:
        if proc.poll() is None:
            proc.kill()
