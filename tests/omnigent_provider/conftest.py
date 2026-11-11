"""Fixtures for the Omnigent provider tests (the fakes themselves are in fakes.py)."""

import socket
import threading

import pytest

from omnigent_provider.fakes import Rig


@pytest.fixture(autouse=True)
def no_tunnels_left():
    """Tunnels live in a process-wide registry: end every one a test opened."""
    yield
    try:
        from omnigent.community.sandbox.motel import transport
    except ImportError:  # no Omnigent: nothing opened any
        return
    transport.REGISTRY.close_all()


@pytest.fixture(autouse=True)
def fresh_harness_warning_state():
    """keep_alive remembers which cabins it asked about and warned for, per process: start every test clean."""
    try:
        from omnigent.community.sandbox.motel import launcher
    except ImportError:
        yield
        return
    launcher._harness_seen.clear()
    launcher._harness_warned.clear()
    yield


@pytest.fixture
def rig(tmp_path, monkeypatch):
    work = tmp_path / "fake"
    work.mkdir()
    monkeypatch.setenv("FAKE_MOTEL_DIR", str(work))
    monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "state"))
    return Rig(work)


@pytest.fixture
def free_port():
    def pick():
        with socket.socket() as sock:
            sock.bind(("127.0.0.1", 0))
            return sock.getsockname()[1]
    return pick


@pytest.fixture
def echo_server():
    """A loopback TCP echo server standing in for the Omnigent server. Yields its port."""
    server = socket.socket()
    server.bind(("127.0.0.1", 0))
    server.listen(8)
    stop = threading.Event()

    def serve(conn):
        with conn:
            while data := conn.recv(65536):
                conn.sendall(data)

    def accept():
        while not stop.is_set():
            try:
                conn, _ = server.accept()
            except OSError:
                return
            threading.Thread(target=serve, args=(conn,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    yield server.getsockname()[1]
    stop.set()
    server.close()
