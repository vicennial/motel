"""The admin socket: peer-credential enforcement, the JSON-line protocol and the client."""
import json
import os
import socket
import stat
import threading

import pytest
from concierge_fakes import Harness

from motel.concierge import admin
from motel.concierge.admin import ConciergeClient, check_peer, listen_fds
from motel.concierge.errors import AdminRefused, AttemptNotFound, ConciergeError
from platform_marks import linux_only

ME = os.getuid()


def test_check_peer_allows_only_the_listed_uids():
    assert check_peer((4242, 0, 0)) is True  # root
    assert check_peer((4242, 1000, 1000)) is False
    assert check_peer((4242, 1, 0)) is False
    assert check_peer((4242, 1000, 0), frozenset({1000})) is True
    assert check_peer(None) is False and check_peer(()) is False and check_peer((1, 2)) is False
    assert check_peer((1, 0, 0), frozenset()) is False


@linux_only  # SO_PEERCRED does not exist on macOS (LOCAL_PEERCRED is a different call); the concierge is a Linux service
def test_peer_credentials_report_this_process_over_a_socketpair():
    a, b = socket.socketpair()
    try:
        pid, uid, gid = admin.peer_credentials(a)
        assert (pid, uid) == (os.getpid(), ME)
    finally:
        a.close()
        b.close()


@pytest.fixture
def served():
    h = Harness("http://127.0.0.1:9", admin=True, allowed_uids=frozenset({ME}))
    yield h
    h.close()


def test_the_socket_file_is_private(served):
    mode = stat.S_IMODE(os.stat(served.admin_path).st_mode)
    assert mode == 0o600


def test_client_round_trip(served):
    client = ConciergeClient(served.admin_path)
    assert client.health()["status"] == "ok" and client.health()["bindings"] == {"anthropic": True}
    token = client.issue(tour="t-1", guest="g", attempt="a-1", bindings=["anthropic"], expires_at=_soon(), budget_usd=1.5)
    assert token.startswith("mt_") and served.registry.lookup(token).budget_usd == 1.5
    assert client.usage(attempt="a-1") == {"usd": 0, "input_tokens": 0, "output_tokens": 0, "requests": 0, "refused": 0}
    assert client.revoke(attempt="a-1") == 1 and client.revoke(attempt="a-1") == 0
    client.issue(tour="t-2", guest="g", attempt="a-2", bindings=["anthropic"], expires_at=_soon(), budget_usd=1)
    client.issue(tour="t-2", guest="g", attempt="a-3", bindings=["anthropic"], expires_at=_soon(), budget_usd=1)
    assert client.revoke(tour="t-2") == 2


def _soon():
    from datetime import UTC, datetime, timedelta
    return (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def test_client_errors_map_to_exceptions(served):
    client = ConciergeClient(served.admin_path)
    with pytest.raises(AdminRefused) as err:
        client.issue(tour="t", guest="g", attempt="a", bindings=["nope"], expires_at=_soon(), budget_usd=1)
    assert err.value.kind == "invalid" and "unknown binding" in err.value.message
    with pytest.raises(AttemptNotFound):
        client.usage(attempt="missing")
    with pytest.raises(AdminRefused, match="needs an attempt or a tour"):
        client.revoke()
    with pytest.raises(AdminRefused, match="ceiling"):
        client.issue(tour="t", guest="g", attempt="a", bindings=["anthropic"], expires_at=_soon(), budget_usd=99)


def test_a_peer_with_another_uid_is_dropped_without_an_answer():
    h = Harness("http://127.0.0.1:9", admin=True)  # default: root only; this test process is not root
    try:
        if ME == 0:
            pytest.skip("running as root")
        with pytest.raises(ConciergeError, match="only root is served"):
            ConciergeClient(h.admin_path).health()
        assert h.registry.health()["attempts"] == 0
        assert h.registry.lookup("mt_anything") is None
    finally:
        h.close()


def _raw(path, data):
    with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
        sock.settimeout(5)
        sock.connect(path)
        sock.sendall(data)
        sock.shutdown(socket.SHUT_WR)
        out = b""
        while chunk := sock.recv(65536):
            out += chunk
    return json.loads(out)


def test_protocol_edge_cases(served):
    assert _raw(served.admin_path, b"not json\n")["kind"] == "bad_request"
    assert _raw(served.admin_path, b"[1,2]\n")["kind"] == "bad_request"
    assert _raw(served.admin_path, b'{"op": "nope"}\n')["kind"] == "bad_request"
    assert _raw(served.admin_path, b'{"op": "usage", "attempt": [1]}\n')["kind"] == "bad_request"
    assert _raw(served.admin_path, b'{"op":"issue","bindings":[[1]]}\n')["ok"] is False
    big = _raw(served.admin_path, b'{"op": "health", "pad": "' + b"x" * 70000 + b'"}\n')
    assert big["ok"] is False and "too large" in big["error"]


def test_a_slow_client_does_not_wedge_the_server(served):
    held = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    held.connect(served.admin_path)
    try:
        assert ConciergeClient(served.admin_path).health()["status"] == "ok"
    finally:
        held.close()


def test_client_without_a_server_says_so(sock_dir):
    with pytest.raises(ConciergeError, match="not running"):
        ConciergeClient(os.path.join(sock_dir, "nothing.sock")).health()


def test_stale_socket_file_is_replaced_and_removed_on_stop():
    h = Harness("http://127.0.0.1:9", admin=True, allowed_uids=frozenset({ME}))
    h.concierge.stop()
    open(h.admin_path, "w").close()  # a stale file where the socket goes
    h.concierge = type(h.concierge)(h.cfg, os.path.join(h.root, "state"), admin_path=h.admin_path, ephemeral_ports=True,
                                    allowed_uids=frozenset({ME}), fsync=False).start()
    try:
        assert ConciergeClient(h.admin_path).health()["status"] == "ok"
    finally:
        h.concierge.stop()
    assert not os.path.exists(h.admin_path)


def test_listen_fds_follows_the_systemd_protocol():
    assert listen_fds({"LISTEN_PID": "77", "LISTEN_FDS": "1"}, pid=77) == [3]
    assert listen_fds({"LISTEN_PID": "77", "LISTEN_FDS": "2"}, pid=77) == [3, 4]
    assert listen_fds({"LISTEN_PID": "78", "LISTEN_FDS": "1"}, pid=77) == []  # meant for another process
    assert listen_fds({"LISTEN_FDS": "1"}, pid=77) == [] and listen_fds({}, pid=77) == []
    assert listen_fds({"LISTEN_PID": "x", "LISTEN_FDS": "y"}, pid=77) == []


def test_an_inherited_socket_is_served_as_is(sock_dir, tmp_path):
    """The systemd path: the socket already exists (and is secured) before the service starts; the server never touches it."""
    path = os.path.join(sock_dir, "pre.sock")
    pre = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    pre.bind(path)
    os.chmod(path, 0o600)
    pre.listen(4)
    h = Harness("http://127.0.0.1:9", root=str(tmp_path))
    server = admin.AdminServer(h.registry, fd=os.dup(pre.fileno()), allowed_uids=frozenset({ME}))
    thread = threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.1}, daemon=True)
    thread.start()
    try:
        assert ConciergeClient(path).health()["status"] == "ok"
        assert stat.S_IMODE(os.stat(path).st_mode) == 0o600
    finally:
        server.shutdown()
        server.server_close()
        pre.close()
        h.close()
