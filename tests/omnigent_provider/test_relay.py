"""The stdio relay and the transport helpers. Standard library only: these run with or without Omnigent."""

import os
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

import pytest

from omnigent.community.sandbox.motel import relay, transport
from omnigent.community.sandbox.motel.transport import ServerTarget, parse_server_target

_OPENED: list = []


def pipe_pair():
    r, w = os.pipe()
    pair = os.fdopen(r, "rb", buffering=0), os.fdopen(w, "wb", buffering=0)
    _OPENED.append(pair)
    return pair


@pytest.fixture(autouse=True)
def close_pipes():
    """Close every pipe a test made, writers first so the readers see EOF. Leaked descriptors would starve later tests
    that run under a low descriptor limit."""
    yield
    pairs = list(_OPENED)
    _OPENED.clear()
    for _, writer in pairs:
        try:
            writer.close()
        except OSError:
            pass
    time.sleep(0.05)
    for reader, _ in pairs:
        try:
            reader.close()
        except OSError:
            pass


def connect_when_listening(port, timeout=5.0):
    end = time.time() + timeout
    while True:
        try:
            return socket.create_connection(("127.0.0.1", port), timeout=2)
        except OSError:
            if time.time() > end:
                raise
            time.sleep(0.05)


@pytest.fixture
def tunnel(echo_server, free_port):
    """A cabin-side and a controller-side Relay joined by two pipes, in one process."""
    c2k_r, c2k_w = pipe_pair()  # controller -> cabin
    k2c_r, k2c_w = pipe_pair()  # cabin -> controller
    cabin = relay.Relay(c2k_r, k2c_w)
    controller = relay.controller_relay(k2c_r, c2k_w, "127.0.0.1", echo_server)
    port = free_port()
    assert cabin.listen(port)
    threads = [threading.Thread(target=r.serve, daemon=True) for r in (cabin, controller)]
    for t in threads:
        t.start()
    assert controller.ready.wait(5) and controller.listening
    yield port, cabin, controller
    cabin.close_all()
    controller.close_all()


def test_bytes_round_trip_through_the_relay(tunnel):
    port, _, _ = tunnel
    with connect_when_listening(port) as client:
        client.sendall(b"hello cabin")
        assert client.recv(100) == b"hello cabin"


def test_many_concurrent_connections_and_a_large_payload(tunnel):
    port, _, _ = tunnel
    payload = os.urandom(1 << 20)
    results = {}

    def one(i):
        with connect_when_listening(port) as client:
            client.settimeout(10)
            client.sendall(payload if i == 0 else bytes([i]) * 1000)
            want = len(payload) if i == 0 else 1000
            got = b""
            while len(got) < want:
                got += client.recv(65536)
            results[i] = got

    threads = [threading.Thread(target=one, args=(i,)) for i in range(6)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(20)
    assert results[0] == payload
    assert all(results[i] == bytes([i]) * 1000 for i in range(1, 6))


# -- M1: closes reach the far end ---------------------------------------------------------------------------------
def make_tunnel(target_port, **kwargs):
    c2k_r, c2k_w = pipe_pair()
    k2c_r, k2c_w = pipe_pair()
    cabin = relay.Relay(c2k_r, k2c_w, **kwargs)
    controller = relay.controller_relay(k2c_r, c2k_w, "127.0.0.1", target_port, **kwargs)
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    assert cabin.listen(port)
    for end in (cabin, controller):
        threading.Thread(target=end.serve, daemon=True).start()
    assert controller.ready.wait(5) and controller.listening
    return port, cabin, controller


def recv_until_eof(sock, timeout=4.0):
    sock.settimeout(timeout)
    got = b""
    while True:
        chunk = sock.recv(65536)  # a timeout here is a test failure: the EOF never arrived
        if not chunk:
            return got
        got += chunk


def test_a_server_that_closes_gives_the_client_eof_with_the_data_before_it():
    """r1, case A: the SERVER side answers and closes."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def answer_and_close():
        conn, _ = srv.accept()
        conn.recv(100)
        conn.sendall(b"bye")
        conn.close()

    threading.Thread(target=answer_and_close, daemon=True).start()
    port, cabin, controller = make_tunnel(srv.getsockname()[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(b"hi")
        assert recv_until_eof(client) == b"bye"
    deadline = time.time() + 5
    while (cabin.streams or controller.streams) and time.time() < deadline:
        time.sleep(0.02)
    assert cabin.streams == {} and controller.streams == {}, "the stream was not cleaned up"
    srv.close()


def test_a_client_that_closes_gives_the_server_eof():
    """r1, case B: the CLIENT side closes. The server must see EOF."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)
    seen = {}

    def serve_one():
        conn, _ = srv.accept()
        conn.settimeout(4)
        conn.recv(100)
        conn.sendall(b"ok")
        try:
            seen["data"] = conn.recv(100)  # b"" is EOF
        except TimeoutError:
            seen["data"] = "timeout"
        conn.close()

    thread = threading.Thread(target=serve_one, daemon=True)
    thread.start()
    port, cabin, controller = make_tunnel(srv.getsockname()[1])
    client = socket.create_connection(("127.0.0.1", port), timeout=5)
    client.sendall(b"x")
    assert client.recv(10) == b"ok"
    client.close()
    thread.join(6)
    assert seen["data"] == b"", f"the server never saw EOF: {seen}"
    srv.close()


def test_half_close_lets_the_reply_still_arrive():
    """A client that shuts down its write side (an HTTP/1.0 style request) still receives the whole reply."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def reply_after_eof():
        conn, _ = srv.accept()
        request = b""
        while chunk := conn.recv(100):  # returns only once the client's FIN was forwarded
            request += chunk
        conn.sendall(b"reply to " + request)
        conn.close()

    threading.Thread(target=reply_after_eof, daemon=True).start()
    port, _, _ = make_tunnel(srv.getsockname()[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(b"GET")
        client.shutdown(socket.SHUT_WR)
        assert recv_until_eof(client) == b"reply to GET"
    srv.close()


def test_a_reset_on_the_target_side_closes_the_stream_everywhere():
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(8)

    def reset():
        conn, _ = srv.accept()
        conn.recv(10)
        conn.setsockopt(socket.SOL_SOCKET, socket.SO_LINGER, b"\x01\x00\x00\x00\x00\x00\x00\x00")  # close sends RST
        conn.close()

    threading.Thread(target=reset, daemon=True).start()
    port, cabin, _ = make_tunnel(srv.getsockname()[1])
    with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
        client.sendall(b"x")
        client.settimeout(4)
        try:
            assert client.recv(10) == b""
        except ConnectionResetError:
            pass  # a reset is also an acceptable way to learn the target went away
    srv.close()


# -- M2: one stalled stream must not block the others -------------------------------------------------------------
def test_a_client_that_stops_reading_does_not_freeze_other_streams():
    """r2: one cabin-side client asks for a huge reply and never reads it."""
    srv = socket.socket()
    srv.bind(("127.0.0.1", 0))
    srv.listen(16)

    def handle(conn):
        with conn:
            while data := conn.recv(65536):
                if data.startswith(b"BIG"):
                    try:
                        conn.sendall(b"z" * (64 << 20))
                    except OSError:
                        return
                else:
                    conn.sendall(data)

    def accept():
        while True:
            try:
                conn, _ = srv.accept()
            except OSError:
                return
            threading.Thread(target=handle, args=(conn,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    port, cabin, controller = make_tunnel(srv.getsockname()[1])

    def ping():
        with socket.create_connection(("127.0.0.1", port), timeout=5) as client:
            client.settimeout(5)
            client.sendall(b"ping")
            return client.recv(10)

    assert ping() == b"ping"
    stalled = socket.create_connection(("127.0.0.1", port), timeout=5)
    stalled.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4096)
    stalled.sendall(b"BIG")
    time.sleep(1.5)  # the stalled stream's buffers and its window are full by now
    started = time.time()
    for _ in range(5):
        assert ping() == b"ping"  # a TimeoutError here is the head-of-line block
    assert time.time() - started < 3
    # the stalled stream is bounded by its window: it did not make either side buffer 64 MiB
    queued = sum(s.queued for s in list(cabin.streams.values()) + list(controller.streams.values()))
    assert queued <= 4 * relay.WINDOW
    stalled.close()
    srv.close()


def test_a_sender_that_ignores_the_window_gets_its_stream_reset():
    r1, w1 = pipe_pair()
    r2, w2 = pipe_pair()
    cabin = relay.Relay(r1, w2)
    stream = relay.Stream(cabin, 1)
    cabin.streams[1] = stream
    a, b = socket.socketpair()
    stream.sock = a  # no writer thread: nothing drains the queue
    stream.feed(b"x" * (2 * relay.WINDOW))
    assert stream.queued == 2 * relay.WINDOW
    stream.feed(b"y")
    assert stream.dead and cabin.streams == {}
    b.close()
    del r1, w1, r2


# -- L4: a cap on streams -----------------------------------------------------------------------------------------
def test_streams_over_the_cap_are_refused_cleanly(echo_server):
    port, cabin, _ = make_tunnel(echo_server, max_streams=2)
    held = [connect_when_listening(port) for _ in range(2)]
    for client in held:
        client.sendall(b"a")
        assert client.recv(5) == b"a"
    third = connect_when_listening(port)
    third.settimeout(4)
    assert third.recv(5) == b"", "the extra connection was not refused with an immediate close"
    assert cabin.refused == 1
    held[0].close()  # a slot frees up
    deadline = time.time() + 5
    while len(cabin.streams) > 1 and time.time() < deadline:
        time.sleep(0.02)
    fourth = connect_when_listening(port)
    fourth.sendall(b"b")
    fourth.settimeout(4)
    assert fourth.recv(5) == b"b"
    for client in (*held[1:], third, fourth):
        client.close()


def test_closing_one_side_closes_the_other(tunnel):
    port, cabin, _ = tunnel
    client = connect_when_listening(port)
    client.sendall(b"x")
    assert client.recv(10) == b"x"
    deadline = time.time() + 5
    while not cabin.streams and time.time() < deadline:
        time.sleep(0.01)
    client.close()
    deadline = time.time() + 5
    while cabin.streams and time.time() < deadline:
        time.sleep(0.02)
    assert cabin.streams == {}


def test_a_busy_port_is_reported_not_swallowed():
    r1, w1 = pipe_pair()
    r2, w2 = pipe_pair()
    held = socket.socket()
    held.bind(("127.0.0.1", 0))
    held.listen(1)
    port = held.getsockname()[1]
    cabin = relay.Relay(r1, w2)
    assert cabin.listen(port) is False
    controller = relay.Relay(r2, w1, expect_hello=True)
    threading.Thread(target=controller.serve, daemon=True).start()
    assert controller.ready.wait(5)
    assert not controller.listening and "cannot listen" in controller.error
    held.close()
    cabin.close_all()
    controller.close_all()


def test_the_relay_runs_as_a_standalone_program(echo_server, free_port):
    """The cabin runs ``python3 -I -c SOURCE cabin PORT`` with only the standard library."""
    port = free_port()
    proc = subprocess.Popen([sys.executable, "-I", "-c", transport.relay_source(), "cabin", str(port)],
                            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    controller = relay.controller_relay(proc.stdout, proc.stdin, "127.0.0.1", echo_server)
    threading.Thread(target=controller.serve, daemon=True).start()
    try:
        assert controller.ready.wait(10) and controller.listening
        with connect_when_listening(port) as client:
            client.sendall(b"ping")
            assert client.recv(10) == b"ping"
    finally:
        proc.terminate()
        proc.wait(5)
        controller.close_all()


def test_a_dead_ssh_session_ends_the_cabin_side():
    proc = subprocess.Popen([sys.executable, "-I", "-c", transport.relay_source(), "cabin", "0"], stdin=subprocess.PIPE,
                            stdout=subprocess.PIPE)
    proc.stdin.close()  # the ssh session ended
    assert proc.wait(10) == 0


# -- handshake ----------------------------------------------------------------------------------------------------
def handshake(data: bytes):
    r, w = pipe_pair()
    w.write(data)
    w.close()
    sink = os.fdopen(os.open(os.devnull, os.O_WRONLY), "wb")
    _OPENED.append((r, sink))
    end = relay.Relay(r, sink, expect_hello=True)
    return end, end._handshake()


def test_the_hello_is_found_after_stray_text():
    end, ok = handshake(b"Last login: today\nmotd line\n" + relay.HELLO + b"rest")
    assert ok and end.stray == b"Last login: today\nmotd line\n"


def test_a_clean_start_has_no_stray_bytes():
    end, ok = handshake(relay.HELLO)
    assert ok and end.stray == b""


def test_a_session_that_never_says_hello_reports_what_it_printed():
    end, ok = handshake(b"bash: python3: command not found\n")
    assert not ok and "never said hello" in end.error and "python3: command not found" in end.error


def test_a_session_that_ends_silently_is_reported():
    end, ok = handshake(b"")
    assert not ok and "session ended" in end.error


def test_another_protocol_version_is_refused():
    end, ok = handshake(relay.MAGIC + b"9\n")
    assert not ok and "protocol" in end.error


@pytest.mark.parametrize("url,expected", [
    ("http://127.0.0.1:6767", ServerTarget("http", "127.0.0.1", 6767, True)),
    ("http://localhost:8080/", ServerTarget("http", "localhost", 8080, True)),
    ("https://omnigent.example.com", ServerTarget("https", "omnigent.example.com", 443, False)),
    ("http://203.0.113.9:6767", ServerTarget("http", "203.0.113.9", 6767, False)),
    ("http://[::1]:6767", ServerTarget("http", "::1", 6767, True)),
])
def test_server_target_comes_from_the_host_command(url, expected):
    command = f"OMNIGENT_HOST_TOKEN='tok' OMNIGENT_HOST_ID=h omnigent host --server '{url}'"
    assert parse_server_target(command) == expected


def test_no_server_flag_means_no_target():
    assert parse_server_target("printf %s hello") is None
    assert parse_server_target("omnigent host --server") is None
    assert parse_server_target("omnigent host --server ftp://x") is None


def test_exported_values_survive_the_shell_exactly():
    env = {"A": "plain", "B": "it's \"quoted\" $HOME `id`\nand a newline", "C": ""}
    out = subprocess.run(["sh", "-c", transport.export_lines(env) + 'printf "%s|%s|%s" "$A" "$B" "$C"'], capture_output=True,
                         text=True, check=True)
    assert out.stdout == f"plain|{env['B']}|"


def test_install_scripts_pin_the_version_and_quote_packages():
    script = transport.ensure_omnigent_script("0.21.0", ["@openai/codex@1.2.3"])
    assert "'omnigent==0.21.0'" in script or "omnigent==0.21.0" in script
    assert "command -v omnigent" in script and "npm install -g --prefix" in script and "@openai/codex@1.2.3" in script
    assert "nodejs npm" in script
    plain = transport.ensure_omnigent_script("0.21.0", [])
    assert "npm" not in plain
    wheels = transport.wheel_install_script("/tmp/oa-wheels.tgz", [])
    assert "--force-reinstall" in wheels and "oa-wheels/*.whl" in wheels and "--no-deps" not in wheels


def test_reverse_forward_command_line():
    argv = transport.reverse_forward_argv("/state/cabins/x/ssh_config", "motel-x", 6767, "127.0.0.1", 6767)
    assert argv == ["/usr/bin/ssh", "-F", "/state/cabins/x/ssh_config", "-N", "-T", "-o", "ExitOnForwardFailure=yes", "-o",
                    "ClearAllForwardings=no", "-o", "BatchMode=yes", "-R", "127.0.0.1:6767:127.0.0.1:6767", "--", "motel-x"]


def test_forward_tunnel_surfaces_a_refusal(rig):
    os.environ["FAKE_SSH_MODE"] = "refuse"
    try:
        tunnel = transport.ForwardTunnel("/cfg", "motel-x", 6767, "127.0.0.1", ssh=str(rig.ssh), settle=5)
        with pytest.raises(transport.TransportError, match="remote port forwarding failed"):
            tunnel.start()
    finally:
        del os.environ["FAKE_SSH_MODE"]
    assert "127.0.0.1:6767:127.0.0.1:6767" in rig.lines("ssh.jsonl")[0]


def test_forward_tunnel_holds_when_the_cabin_allows_it(rig):
    tunnel = transport.ForwardTunnel("/cfg", "motel-x", 6767, "127.0.0.1", ssh=str(rig.ssh), settle=0.5)
    tunnel.start()
    assert tunnel.alive()
    tunnel.close()


def test_motel_core_never_imports_omnigent():
    root = Path(__file__).resolve().parents[2] / "src" / "motel"
    for path in root.rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        assert "import omnigent" not in text and "from omnigent" not in text, path.name
