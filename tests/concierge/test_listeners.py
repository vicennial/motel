"""One listener per attempt: attribution before authentication, isolation, lifecycle and restart."""
import json
import os
import socket
import threading
import time

import pytest
from concierge_fakes import BODY, FakeUpstream, Harness, error_kind, raw_exchange, request

from motel.concierge import proxy
from motel.concierge.admin import ConciergeClient, dispatch
from motel.concierge.errors import AdminRefused, AttemptNotFound
from motel.concierge.settings import parse_tours_section

URL = "/u/anthropic/v1/messages"


@pytest.fixture
def up():
    server = FakeUpstream()
    yield server
    server.close()


def head(token=None, length=None, extra=""):
    lines = f"POST {URL} HTTP/1.1\r\nHost: x\r\n"
    if token:
        lines += f"x-api-key: {token}\r\n"
    if length is not None:
        lines += f"Content-Length: {length}\r\n"
    return (lines + extra + "\r\n").encode()


def read_all(sock, timeout=5):
    sock.settimeout(timeout)
    out = b""
    try:
        while chunk := sock.recv(65536):
            out += chunk
    except (TimeoutError, ConnectionResetError):
        pass
    return out


def refused(port):
    try:
        socket.create_connection(("127.0.0.1", port), timeout=2).close()
    except ConnectionRefusedError:
        return True
    return False


def soon():
    from datetime import UTC, datetime, timedelta
    return (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def restart(h):
    """Stop the concierge and start another on the same state directory: the sockets come back from the fake systemd."""
    return h.restart()


# -- attribution ---------------------------------------------------------------------------------------------------------

def test_every_attempt_gets_its_own_listener_and_a_token_works_only_on_its_own(up):
    h = Harness(up.url)
    try:
        t1, t2 = h.issue("a-1"), h.issue("a-2", guest="g-2")
        p1, p2 = h.port_of("a-1"), h.port_of("a-2")
        assert p1 != p2 and h.concierge.endpoints.port_of("a-1") == p1 and h.concierge.endpoints.count() == 2
        assert h.post(URL, t1)[0] == 200 and h.post(URL, t2)[0] == 200
        for port, token in ((p2, t1), (p1, t2)):
            status, _, body = request(port, "POST", URL, {"x-api-key": token, "Content-Type": "application/json"}, json.dumps(BODY).encode())
            assert status == 401 and error_kind(body) == "motel_invalid_token"
        assert len(up.requests) == 2
    finally:
        h.close()


def test_listeners_bind_loopback_only(up):
    h = Harness(up.url)
    try:
        h.issue("a-1")
        assert h.concierge.endpoints.bind_host == "127.0.0.1"
        server = h.concierge.endpoints._servers["a-1"]
        assert server.server_address[0] == "127.0.0.1"
    finally:
        h.close()


def test_the_shared_guest_listener_is_gone(up):
    h = Harness(up.url)
    try:
        assert not hasattr(h.concierge, "guest") and not hasattr(h.concierge, "guest_port")
    finally:
        h.close()


# -- lifecycle -----------------------------------------------------------------------------------------------------------

def test_revoking_closes_the_listener_and_expiry_closes_it_at_the_next_sweep(up):
    now = [1_800_000_000.0]
    h = Harness(up.url, clock=lambda: now[0])
    try:
        h.issue("a-rev", ttl=3600)
        h.issue("a-exp", ttl=60)
        h.issue("a-live", ttl=3600)
        revoked, expiring, live = h.port_of("a-rev"), h.port_of("a-exp"), h.port_of("a-live")
        assert not refused(expiring)
        h.registry.revoke(attempt="a-rev")
        h.settle()  # the stored socket goes with the revoke (the fake systemd closes it asynchronously, like the real one)
        assert refused(revoked) and h.concierge.endpoints.port_of("a-rev") is None
        now[0] += 61
        h.concierge.endpoints.sweep()
        h.settle()
        assert refused(expiring) and not refused(live) and h.concierge.endpoints.count() == 1
    finally:
        h.close()


def test_the_sweeper_thread_closes_an_expired_listener_by_itself(up):
    """Deterministic: a fast sweeper, a fake clock, and a wait on completed passes (not a sleep). The wait is for two passes
    that begin after the clock moves, because a pass already running may have read the old time."""
    now = [1_800_000_000.0]
    h = Harness(up.url, clock=lambda: now[0], sweep_interval=0.02)
    try:
        h.issue("a-exp", ttl=60)
        port = h.port_of("a-exp")
        assert not refused(port)
        now[0] += 61
        assert h.concierge.wait_for_sweeps(2, timeout=30)
        h.settle()
        assert h.concierge.endpoints.port_of("a-exp") is None and refused(port)
    finally:
        h.close()


def test_one_sweep_pass_closes_the_listener_of_an_expired_attempt_and_keeps_a_live_one(up):
    now = [1_800_000_000.0]
    h = Harness(up.url, clock=lambda: now[0], sweep_interval=3600)  # the thread never fires; the test triggers each pass
    try:
        h.issue("a-exp", ttl=60)
        h.issue("a-live", ttl=3600)
        now[0] += 61
        h.concierge.sweep_now()
        assert h.concierge.endpoints.port_of("a-exp") is None and h.concierge.endpoints.port_of("a-live") is not None
    finally:
        h.close()


def test_an_issue_that_fails_leaves_no_listener_and_no_ledger_entry(up):
    h = Harness(up.url)
    try:
        h.issue("a-1")
        with pytest.raises(AdminRefused, match="already has a token"):
            h.issue("a-1")
        with pytest.raises(AdminRefused):
            h.registry.issue(tour="t", guest="g", attempt="a-bad", bindings=["nope"], expires_at=soon(), budget_usd=1)
        assert h.concierge.endpoints.count() == 1
        assert [e["attempt"] for e in h.events("issue")] == ["a-1"]
    finally:
        h.close()


def adjacent_free_ports(count):
    """``count`` adjacent loopback ports that are free right now (skips the test if the host has none)."""
    for _ in range(200):
        probe = socket.socket()
        probe.bind(("127.0.0.1", 0))
        low = probe.getsockname()[1]
        probe.close()
        taken = []
        try:
            for port in range(low, low + count):
                sock = socket.socket()
                sock.bind(("127.0.0.1", port))
                taken.append(sock)
        except OSError:
            continue
        finally:
            for sock in taken:
                sock.close()
        if len(taken) == count:
            return list(range(low, low + count))
    pytest.skip(f"{count} adjacent free ports were not available")


def test_the_port_range_is_used_and_running_out_of_ports_is_an_error_that_a_revoke_cures(up):
    ports = adjacent_free_ports(2)
    h = Harness(up.url, tour={"concierge_port_range": f"{ports[0]}-{ports[1]}"})
    try:
        h.concierge.endpoints.port_range = (ports[0], ports[1])
        h.issue("a-1")
        h.issue("a-2", guest="g-2")
        assert sorted([h.port_of("a-1"), h.port_of("a-2")]) == ports
        with pytest.raises(AdminRefused, match="no free port") as info:
            h.issue("a-3", guest="g-3")
        assert info.value.kind == "busy" and "a-3" not in h.ledger_text()
        with pytest.raises(AdminRefused, match="already has a token") as info:  # review R5: a duplicate is 'exists', never 'busy'
            h.issue("a-2", guest="g-2")
        assert info.value.kind == "exists" and h.concierge.endpoints.count() == 2
        h.registry.revoke(attempt="a-1")
        h.settle()  # the stored socket is released when the fake systemd has processed the removal
        h.issue("a-3", guest="g-3")
        assert h.port_of("a-3") in ports
    finally:
        h.close()


def test_a_freed_port_is_not_reused_while_another_is_free_and_then_the_longest_freed_goes_first(up):
    """Review R6: a forward or a retry that belonged to the old attempt must not land on the next attempt's listener."""
    ports = adjacent_free_ports(3)
    now = [1_800_000_000.0]
    h = Harness(up.url, clock=lambda: now[0], tour={"concierge_port_range": f"{ports[0]}-{ports[2]}"})
    try:
        h.concierge.endpoints.port_range = (ports[0], ports[2])
        h.issue("a-1")
        h.issue("a-2", guest="g-2")
        p1, p2 = h.port_of("a-1"), h.port_of("a-2")
        spare = (set(ports) - {p1, p2}).pop()
        h.registry.revoke(attempt="a-1")
        h.settle()
        now[0] += 10
        h.issue("a-3", guest="g-3")
        assert h.port_of("a-3") == spare  # not p1, which was freed 10 s ago
        h.registry.revoke(attempt="a-2")  # p1 freed at t, p2 freed at t + 10
        h.settle()
        now[0] += 10
        h.issue("a-4", guest="g-4")
        assert h.port_of("a-4") == p1  # everything free was freed recently: the one freed longest ago
        now[0] += 10
        h.issue("a-5", guest="g-5")
        assert h.port_of("a-5") == p2
        h.registry.revoke(attempt="a-4")
        h.settle()
        now[0] += 601  # past the quarantine: it is an ordinary free port again
        h.issue("a-6", guest="g-6")
        assert h.port_of("a-6") == p1
        assert h.concierge.endpoints._freed == {}
    finally:
        h.close()


def test_the_port_range_setting_is_validated():
    assert parse_tours_section({}).concierge_port_range == (17800, 17999)
    assert parse_tours_section({"concierge_port_range": "20000 - 20010"}).concierge_port_range == (20000, 20010)
    from motel.concierge.errors import ConfigError
    for bad in ("20010-20000", "80-90", "1024-70000", "17800", "a-b", 17800, ["17800-17999"], "17800-17999-1"):
        with pytest.raises(ConfigError, match="concierge_port_range"):
            parse_tours_section({"concierge_port_range": bad})


# -- the endpoint admin call -----------------------------------------------------------------------------------------------

def test_endpoint_over_the_admin_socket():
    h = Harness("http://127.0.0.1:9", admin=True, allowed_uids=frozenset({os.getuid()}))
    try:
        client = ConciergeClient(h.admin_path)
        token = client.issue(tour="t", guest="g", attempt="a-1", bindings=["anthropic"], expires_at=soon(), budget_usd=1)
        reply = client.endpoint(attempt="a-1")
        assert reply == {"port": h.concierge.endpoints.port_of("a-1")} and isinstance(reply["port"], int)
        assert token.startswith("mt_")  # issue is unchanged: it still returns the token, a string
        with pytest.raises(AttemptNotFound):
            client.endpoint(attempt="nope")
        client.revoke(attempt="a-1")
        with pytest.raises(AdminRefused) as info:
            client.endpoint(attempt="a-1")
        assert info.value.kind == "gone" and not isinstance(info.value, AttemptNotFound)
        assert dispatch(h.registry, {"op": "endpoint", "attempt": "../x"}, None, h.concierge.endpoints)["kind"] == "invalid"
        assert dispatch(h.registry, {"op": "endpoint"}, None, h.concierge.endpoints)["ok"] is False
        health = client.health()
        assert health["listeners"] == 0 and health["port_range"] == "17800-17999" and "port" not in health
    finally:
        h.close()


# -- restart -------------------------------------------------------------------------------------------------------------

def test_a_restart_reopens_each_live_attempts_listener_on_its_old_port(up):
    now = [1_800_000_000.0]
    h = Harness(up.url, clock=lambda: now[0])
    try:
        live = h.issue("a-live", ttl=3600)
        h.issue("a-rev", ttl=3600)
        h.issue("a-exp", ttl=60)
        old = {name: h.port_of(name) for name in ("a-live", "a-rev", "a-exp")}
        h.registry.revoke(attempt="a-rev")
        now[0] += 61
        restart(h)
        endpoints = h.concierge.endpoints
        assert endpoints.port_of("a-live") == old["a-live"] and endpoints.count() == 1
        assert endpoints.port_of("a-rev") is None and endpoints.port_of("a-exp") is None
        assert request(old["a-live"], "POST", URL, {"x-api-key": live, "Content-Type": "application/json"}, json.dumps(BODY).encode())[0] == 200
        h.settle()
        assert refused(old["a-rev"]) and refused(old["a-exp"])
    finally:
        h.close()


def test_while_the_service_is_down_systemd_keeps_the_port_bound_so_nobody_can_take_it(up):
    h = Harness(up.url)
    squatter = socket.socket()
    try:
        token = h.issue("a-1")
        old = h.port_of("a-1")
        h.concierge.stop()
        h.settle()
        with pytest.raises(OSError):
            squatter.bind(("127.0.0.1", old))  # still held by the store
        h.restart()
        assert h.concierge.endpoints.port_of("a-1") == old and h.registry.get("a-1").revoked_at is None
        assert h.concierge._health_extra()["lost_listeners"] == {}
        assert request(old, "POST", URL, {"x-api-key": token, "Content-Type": "application/json"}, json.dumps(BODY).encode())[0] == 200
    finally:
        squatter.close()
        h.close()


def test_a_start_whose_sockets_did_not_come_back_revokes_every_live_attempt_and_binds_nothing(up):
    """Review F1 (and R1): the port is never bound again. The store is lost (a reboot, a crash that dropped it) and another local
    process takes the old port while the service is down, reads the placeholder token from the cabin's reconnecting forward and
    lets go before the restart. The attempt must not be live any more, so the captured token is worth nothing."""
    h = Harness(up.url)
    try:
        token = h.issue("a-1")
        h.issue("a-2", guest="g-2")
        old = h.port_of("a-1")
        h.concierge.stop()
        with h.systemd._lock:  # the store did not survive
            for _, fd in h.systemd.entries:
                os.close(fd)
            h.systemd.entries = []
        squatter = socket.socket()
        squatter.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        squatter.bind(("127.0.0.1", old))
        squatter.listen(4)
        def stale_forward():  # the cabin's forward, still dialling the old port
            try:
                request(old, "POST", URL, {"x-api-key": token, "Content-Type": "application/json"}, json.dumps(BODY).encode(), timeout=3)
            except OSError:
                pass
            except Exception:  # noqa: BLE001 - the squatter hangs up on it: http.client reports that in its own way
                pass

        threading.Thread(target=stale_forward, daemon=True).start()
        squatter.settimeout(5)
        conn, _ = squatter.accept()
        seen = b""
        while b"\r\n\r\n" not in seen:
            seen += conn.recv(65536)
        assert token.encode() in seen  # the squatter has the token
        conn.close()
        squatter.close()  # lets go before the restart
        h.restart()
        endpoints = h.concierge.endpoints
        assert endpoints.count() == 0 and endpoints.port_of("a-1") is None and endpoints.port_of("a-2") is None
        assert h.registry.get("a-1").revoked_at is not None and h.registry.get("a-2").revoked_at is not None
        assert h.concierge._health_extra()["lost_listeners"] == {"a-1": "not_preserved", "a-2": "not_preserved"}
        assert refused(old)  # nothing listens there, so the captured token has nowhere to be replayed
        assert not up.requests
        for name in ("a-1", "a-2"):
            reply = dispatch(h.registry, {"op": "endpoint", "attempt": name}, None, endpoints)
            assert reply["ok"] is False and reply["kind"] == "gone"
        h.restart()  # revoked for good: the ledger says so, and health reports only this process's start
        assert h.concierge._health_extra()["lost_listeners"] == {} and h.registry.get("a-1").revoked_at is not None
        assert any(e["ev"] == "revoke" and e["attempt"] == "a-1" for e in h.events())
    finally:
        h.close()


def test_only_the_attempts_whose_socket_came_back_stay_live(up):
    h = Harness(up.url)
    try:
        t1, _ = h.issue("a-1"), h.issue("a-2", guest="g-2")
        p1 = h.port_of("a-1")
        h.concierge.stop()
        h.settle()
        with h.systemd._lock:  # only a-2's socket is gone from the store
            keep = []
            for name, fd in h.systemd.entries:
                if name == "att-a-2":
                    os.close(fd)
                else:
                    keep.append((name, fd))
            h.systemd.entries = keep
        h.restart()
        assert h.concierge.endpoints.port_of("a-1") == p1 and h.registry.get("a-2").revoked_at is not None
        assert h.concierge._health_extra()["lost_listeners"] == {"a-2": "not_preserved"}
        assert h.post(URL, t1)[0] == 200
    finally:
        h.close()


# -- isolation ---------------------------------------------------------------------------------------------------------

def test_a_reconnect_flood_on_one_attempts_port_does_not_touch_another_attempt(up, monkeypatch):
    """The review's repro (N1): connections that never finish their headers, re-opened as soon as they are dropped."""
    monkeypatch.setattr(proxy, "HEADER_DEADLINE_S", 0.5)
    monkeypatch.setattr(proxy, "PENDING_WAIT_S", 0.2)
    h = Harness(up.url)
    stop = threading.Event()
    try:
        h.issue("a-1")
        other = h.issue("a-2", guest="g-2")
        flood_port = h.port_of("a-1")

        def attacker():
            while not stop.is_set():
                try:
                    s = socket.create_connection(("127.0.0.1", flood_port), timeout=1)
                except OSError:
                    time.sleep(0.01)
                    continue
                try:
                    s.sendall(b"POST /u/anthropic/v1/messages HTTP/1.1\r\n")
                    s.settimeout(1)
                    while not stop.is_set() and s.recv(1):
                        pass
                except OSError:
                    pass
                finally:
                    s.close()

        threads = [threading.Thread(target=attacker, daemon=True) for _ in range(16)]
        [t.start() for t in threads]
        time.sleep(0.6)
        ok = 0
        t0 = time.monotonic()
        while time.monotonic() - t0 < 3.0:
            started = time.monotonic()
            status, _, _ = h.post(URL, other, timeout=3)
            assert status == 200 and time.monotonic() - started < 1.0
            ok += 1
            time.sleep(0.1)
        assert ok >= 10
        # the flood itself is bounded: its own listener holds at most the pre-auth slots, so few threads, not 16 x reconnects
        assert threading.active_count() < 60
    finally:
        stop.set()
        h.close()


def test_the_pre_auth_pool_of_one_listener_is_two_connections(up, monkeypatch):
    monkeypatch.setattr(proxy, "PENDING_WAIT_S", 0.3)
    h = Harness(up.url)
    socks = []
    try:
        mine = h.issue("a-1")
        other = h.issue("a-2", guest="g-2")
        for _ in range(proxy.PENDING_AUTH):
            s = socket.create_connection(("127.0.0.1", h.port_of("a-1")))
            s.sendall(b"POST /u/anthropic/v1/messages HTTP/1.1\r\n")
            socks.append(s)
        time.sleep(0.2)
        started = time.monotonic()
        assert raw_exchange(h.port_of("a-1"), head(mine, 2) + b"{}", timeout=2) == b""  # a third waits, then is dropped
        assert time.monotonic() - started < 2
        assert h.post(URL, other)[0] == 200  # nobody else notices
        socks.pop().close()  # as soon as a slot frees, the attempt itself is served again
        time.sleep(0.2)
        assert h.post(URL, mine)[0] == 200
    finally:
        for s in socks:
            s.close()
        h.close()


def test_authenticated_connections_do_not_use_the_pre_auth_slots(up, monkeypatch):
    """A connection that has authenticated and is still sending its body frees its pre-auth slot at once."""
    monkeypatch.setattr(proxy, "PENDING_WAIT_S", 0.3)
    h = Harness(up.url)
    socks = []
    try:
        mine = h.issue("a-1", max_concurrent=4)
        for _ in range(4):  # more than the 2 pre-auth slots, all authenticated and waiting for their body
            s = socket.create_connection(("127.0.0.1", h.port_of("a-1")))
            s.sendall(head(mine, 100) + b"{")
            socks.append(s)
            time.sleep(0.1)
        assert h.post(URL, mine)[0] == 200  # a fifth connection still gets in (6 authenticated are allowed)
    finally:
        for s in socks:
            s.close()
        h.close()


def test_a_trickle_after_an_early_reply_is_cut_after_one_second_before_authentication_too(up):
    """The review's repro (N1): bad target, giant Content-Length, then a byte every 0.3 s."""
    h = Harness(up.url)
    try:
        s = socket.create_connection(("127.0.0.1", h.port), timeout=2)
        s.sendall(b"POST /nope HTTP/1.1\r\nHost: x\r\nContent-Length: 100000000\r\n\r\n")
        started = time.monotonic()
        closed_at = None
        while time.monotonic() - started < 6:
            try:
                s.sendall(b"x")
            except OSError:
                closed_at = time.monotonic() - started
                break
            time.sleep(0.3)
        s.close()
        assert closed_at is not None and proxy.LINGER_TOTAL_S - 0.1 <= closed_at < proxy.LINGER_TOTAL_S + 1.5, closed_at
    finally:
        h.close()


def test_a_trickle_after_an_authenticated_early_reply_is_cut_after_one_second(up):
    h = Harness(up.url)
    try:
        token = h.issue()
        s = socket.create_connection(("127.0.0.1", h.port_of(token)), timeout=2)
        s.sendall(head(token, 4_000_000, "anthropic-beta: not-allowed\r\n"))  # refused before its body is read
        started = time.monotonic()
        closed_at = None
        while time.monotonic() - started < 8:
            try:
                s.sendall(b"x")
            except OSError:
                closed_at = time.monotonic() - started
                break
            time.sleep(0.3)
        s.close()
        assert closed_at is not None and closed_at < proxy.LINGER_TOTAL_S + 1.5, closed_at
    finally:
        h.close()


def test_a_small_body_sent_with_a_refused_request_still_gets_its_answer(up):
    """No linger before authentication must not turn ordinary refusals into resets: what has arrived is drained."""
    h = Harness(up.url)
    try:
        for token in (None, "mt_wrong"):
            reply = raw_exchange(h.port, head(token, 2000) + b"x" * 2000)
            assert reply.startswith(b"HTTP/1.1 401"), reply[:40]
        mine = h.issue()
        reply = raw_exchange(h.port_of(mine), head(mine, 2000, "anthropic-beta: nope\r\n") + b"x" * 2000)
        assert reply.startswith(b"HTTP/1.1 403")
    finally:
        h.close()


def test_a_large_body_with_a_pre_authentication_refusal_gets_the_json_error_not_a_reset(up):
    """Review R2: with no linger, a 2 MB body behind an unknown token or a malformed path met a reset on some tries."""
    import http.client
    h = Harness(up.url)
    try:
        live = h.issue("a-1", budget=5.0)
        port = h.port_of("a-1")
        big = json.dumps({**BODY, "messages": [{"role": "user", "content": "x" * 2_000_000}]}).encode()
        for token, path, status in (("mt_nope", URL, 401), (live, URL + "%2f", 400)):
            for _ in range(10):
                conn = http.client.HTTPConnection("127.0.0.1", port, timeout=5)
                try:
                    conn.request("POST", path, body=big, headers={"x-api-key": token, "Content-Type": "application/json"})
                    reply = conn.getresponse()
                    assert reply.status == status and reply.read().startswith(b'{"type": "error"')
                finally:
                    conn.close()
    finally:
        h.close()
