"""Listening sockets kept with systemd across a restart (``FileDescriptorStoreMax=``), against a model of systemd's store.

The store is the only thing that keeps an attempt live: a start never binds a live attempt's port again."""
import json
import os
import shutil
import socket

import pytest
from concierge_fakes import BODY, FakeSystemd, FakeUpstream, Harness, request, short_dir

from motel.concierge import fdstore, install
from motel.concierge.errors import AdminRefused, ConfigError
from motel.concierge.service import Concierge
from motel.concierge.settings import MAX_PORT_RANGE, parse_tours_section

URL = "/u/anthropic/v1/messages"
HEADERS = {"Content-Type": "application/json"}


@pytest.fixture
def up():
    server = FakeUpstream()
    yield server
    server.close()


def concierge(h, **kw):
    """Replace the harness's concierge by another with different wiring (it stays the one ``h.close`` stops)."""
    h.concierge.stop()
    h.concierge = Concierge(h.cfg, os.path.join(h.root, "state"), ephemeral_ports=True, fsync=False, **kw).start()
    h.registry = h.concierge.registry
    return h.concierge


def soon():
    from datetime import UTC, datetime, timedelta
    return (datetime.now(UTC) + timedelta(hours=1)).strftime("%Y-%m-%dT%H:%M:%SZ")


def listening(port=0):
    sock = socket.socket()
    sock.bind(("127.0.0.1", port))
    sock.listen(4)
    return sock


def post(port, token):
    return request(port, "POST", URL, {"x-api-key": token, **HEADERS}, json.dumps(BODY).encode())[0]


# -- the happy path ------------------------------------------------------------------------------------------------------------

def test_each_new_attempts_socket_is_stored_before_the_token_is_returned_and_comes_back(up):
    h = Harness(up.url)
    try:
        token = h.issue("a-1")
        old = h.port_of("a-1")
        assert h.systemd.names() == [("att-a-1", old)]
        h.concierge.stop()
        h.settle()
        assert h.systemd.names() == [("att-a-1", old)]  # stop leaves it with systemd
        h.restart()
        assert h.concierge.endpoints.port_of("a-1") == old and h.concierge._health_extra()["lost_listeners"] == {}
        assert post(old, token) == 200
        assert h.systemd.names() == [("att-a-1", old)]
    finally:
        h.close()


def test_revoking_or_expiry_drops_the_stored_socket_and_a_stale_one_is_dropped_at_start(up):
    now = [1_800_000_000.0]
    h = Harness(up.url, clock=lambda: now[0], sweep_interval=0.02)
    try:
        h.issue("a-rev"), h.issue("a-exp", ttl=60), h.issue("a-live")
        h.registry.revoke(attempt="a-rev")
        now[0] += 61
        assert h.concierge.wait_for_sweeps(2, timeout=30)  # the sweeper closes the expired listener
        h.settle()
        assert [name for name, _ in h.systemd.names()] == ["att-a-live"]
        # a stored socket for an attempt that is not live (a crash before the removal): closed and removed at start
        stale = listening()
        h.systemd.put("att-a-gone", stale)
        stale_port = stale.getsockname()[1]
        h.concierge.stop()
        h.restart()
        h.settle()
        assert [name for name, _ in h.systemd.names()] == ["att-a-live"]
        stale.close()
        assert socket.socket().connect_ex(("127.0.0.1", stale_port)) != 0
    finally:
        h.close()


# -- F4: names are not unique in the store --------------------------------------------------------------------------------------

@pytest.mark.parametrize("newest_first", [True, False])
def test_two_stored_sockets_with_one_name_keep_the_one_on_the_recorded_port_and_remove_the_other(up, newest_first):
    h = Harness(up.url, systemd=FakeSystemd(newest_first=newest_first))
    wrong = listening()
    try:
        token = h.issue("a-1")
        old = h.port_of("a-1")
        h.concierge.stop()
        h.settle()
        h.systemd.put("att-a-1", wrong)  # a second entry with the same name, on another port
        assert sorted(port for _, port in h.systemd.names()) == sorted([old, wrong.getsockname()[1]])
        h.restart()
        h.settle()
        assert h.concierge.endpoints.port_of("a-1") == old and h.registry.get("a-1").revoked_at is None
        assert h.systemd.names() == [("att-a-1", old)]  # exactly one, the right one: the removal did not take the kept socket too
        assert post(old, token) == 200
        wrong_port = wrong.getsockname()[1]
        wrong.close()
        assert socket.socket().connect_ex(("127.0.0.1", wrong_port)) != 0  # and the extra was really closed
        h.restart()  # and the next start still finds it
        assert h.concierge.endpoints.port_of("a-1") == old
    finally:
        wrong.close()
        h.close()


def test_a_stored_socket_on_another_port_than_the_ledger_is_removed_and_the_attempt_is_revoked(up):
    h = Harness(up.url)
    wrong = listening()
    try:
        h.issue("a-1")
        h.concierge.stop()
        h.settle()
        with h.systemd._lock:
            for _, fd in h.systemd.entries:
                os.close(fd)
            h.systemd.entries = []
        h.systemd.put("att-a-1", wrong)
        h.restart()
        h.settle()
        assert h.registry.get("a-1").revoked_at is not None and h.concierge.endpoints.port_of("a-1") is None
        assert h.concierge._health_extra()["lost_listeners"] == {"a-1": "not_preserved"}
        assert h.systemd.names() == []  # the untrusted socket is not left in the store
    finally:
        wrong.close()
        h.close()


def test_inherited_listeners_keep_every_socket_of_a_name(monkeypatch):
    first, second, third = listening(), listening(), listening()
    fds = [os.dup(s.fileno()) for s in (first, second, third)]
    monkeypatch.setattr(fdstore, "listen_fd_names", lambda environ=None, pid=None: [(fds[0], "motel-concierge.socket"), (fds[1], "att-a-1"),
                                                                                    (fds[2], "att-a-1")])
    try:
        stored, other = fdstore.inherited_listeners()
        assert list(stored) == ["a-1"] and len(stored["a-1"]) == 2 and other == [fds[0]]
        assert [s.getsockname() for s in stored["a-1"]] == [second.getsockname(), third.getsockname()]
    finally:
        for sock in (first, second, third):
            sock.close()
        os.close(fds[0])
        for sock in stored["a-1"]:
            sock.close()


def test_listen_fd_names_split_the_admin_socket_from_stored_listeners():
    env = {"LISTEN_PID": "77", "LISTEN_FDS": "3", "LISTEN_FDNAMES": "motel-concierge.socket:att-a-1:att-b.2"}
    assert fdstore.listen_fd_names(env, pid=77) == [(3, "motel-concierge.socket"), (4, "att-a-1"), (5, "att-b.2")]
    assert fdstore.listen_fd_names({"LISTEN_PID": "77", "LISTEN_FDS": "1"}, pid=77) == [(3, "")]
    assert fdstore.listen_fd_names({"LISTEN_PID": "78", "LISTEN_FDS": "1"}, pid=77) == []


# -- F1: no store, no attempt ---------------------------------------------------------------------------------------------------

def test_without_a_store_no_attempt_is_issued(up):
    h = Harness(up.url)
    try:
        concierge(h)  # no store at all
        with pytest.raises(AdminRefused) as info:
            h.issue("a-1")
        assert info.value.kind == "busy" and "a-1" not in h.ledger_text() and h.concierge.endpoints.count() == 0
    finally:
        h.close()


def test_a_dead_notify_socket_refuses_the_attempt_instead_of_running_it_unprotected(up):
    h = Harness(up.url)
    try:
        concierge(h, store=fdstore.FdStore({"NOTIFY_SOCKET": "/nonexistent/notify", "LISTEN_PID": str(os.getpid())}))
        with pytest.raises(AdminRefused) as info:
            h.issue("a-1")
        assert info.value.kind == "busy" and "a-1" not in h.ledger_text() and h.concierge.endpoints.count() == 0
    finally:
        h.close()


def test_a_full_store_refuses_the_attempt_and_a_revoke_makes_room(up):
    h = Harness(up.url)
    try:
        concierge(h, store=h.systemd.store(limit=2))
        h.issue("a-1")
        h.issue("a-2", guest="g-2")
        with pytest.raises(AdminRefused) as info:
            h.issue("a-3", guest="g-3")
        assert info.value.kind == "busy" and "a-3" not in h.ledger_text() and h.concierge.endpoints.count() == 2
        h.registry.revoke(attempt="a-1")
        h.settle()
        h.issue("a-3", guest="g-3")
        assert sorted(name for name, _ in h.systemd.names()) == ["att-a-2", "att-a-3"]
    finally:
        h.close()


def test_a_failed_issue_leaves_nothing_in_the_store(up):
    h = Harness(up.url)
    try:
        h.issue("a-1")
        with pytest.raises(AdminRefused, match="already has a token"):
            h.issue("a-1")
        with pytest.raises(AdminRefused):
            h.registry.issue(tour="t", guest="g", attempt="a-bad", bindings=["nope"], expires_at=soon(), budget_usd=1)
        assert [name for name, _ in h.systemd.names()] == ["att-a-1"]
    finally:
        h.close()


# -- the unsafe development mode ------------------------------------------------------------------------------------------------

def test_dev_rebind_runs_without_a_store_and_binds_the_recorded_port_again(up):
    h = Harness(up.url)
    try:
        concierge(h, dev_rebind=True)
        token = h.issue("a-1")
        old = h.port_of("a-1")
        assert h.concierge._health_extra()["dev_rebind"] is True
        h.concierge.stop()
        concierge(h, dev_rebind=True)
        assert h.concierge.endpoints.port_of("a-1") == old and post(old, token) == 200
    finally:
        h.close()


def test_dev_rebind_still_revokes_an_attempt_whose_port_is_taken(up):
    h = Harness(up.url)
    squatter = socket.socket()
    try:
        concierge(h, dev_rebind=True)
        h.issue("a-1")
        old = h.port_of("a-1")
        h.concierge.stop()
        squatter.bind(("127.0.0.1", old))
        squatter.listen(1)
        concierge(h, dev_rebind=True)
        assert h.registry.get("a-1").revoked_at is not None and h.concierge._health_extra()["lost_listeners"] == {"a-1": "port_taken"}
    finally:
        squatter.close()
        h.close()


def test_the_dev_rebind_flag_exists_and_the_service_refuses_to_start_without_systemd(tmp_path, capsys):
    from motel.concierge import service
    cfg = tmp_path / "motel.toml"
    key = tmp_path / "k"
    key.write_text("sk-test\n")
    cfg.write_text(f'[tours]\nenabled = true\n[tours.bindings.a]\nupstream = "https://api.example.com"\nauth = "x-api-key"\n'
                   f'key_file = "{key}"\npaths = ["/v1/messages"]\n')
    assert service.main(["--config", str(cfg), "--state-dir", str(tmp_path)]) != 0
    assert "--dev-rebind" in capsys.readouterr().err


# -- F5: only a store systemd really set up ---------------------------------------------------------------------------------------

def test_the_notify_socket_is_trusted_only_when_listen_pid_is_this_process():
    assert not fdstore.FdStore({"NOTIFY_SOCKET": "/run/systemd/notify"}).enabled  # a shell that merely inherited it
    assert not fdstore.FdStore({"NOTIFY_SOCKET": "/run/systemd/notify", "LISTEN_PID": "1"}, pid=2).enabled
    assert fdstore.FdStore({"NOTIFY_SOCKET": "/run/systemd/notify", "LISTEN_PID": "2"}, pid=2).enabled
    # review L2: INVOCATION_ID is no substitute; a child of the service inherits it together with NOTIFY_SOCKET
    assert not fdstore.FdStore({"NOTIFY_SOCKET": "/run/systemd/notify", "INVOCATION_ID": "abc"}).enabled
    assert not fdstore.FdStore({"NOTIFY_SOCKET": "/run/systemd/notify", "INVOCATION_ID": "abc", "LISTEN_PID": "1"}, pid=2).enabled
    assert not fdstore.FdStore({"LISTEN_PID": "2"}, pid=2).enabled  # no socket
    assert fdstore.FdStore({"NOTIFY_SOCKET": "@abstract", "LISTEN_PID": "2"}, pid=2).path == "\0abstract"


def test_the_notify_socket_is_removed_from_the_environment_once_read(monkeypatch):
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    monkeypatch.setenv("LISTEN_PID", str(os.getpid()))
    assert fdstore.FdStore().enabled and "NOTIFY_SOCKET" not in os.environ
    monkeypatch.setenv("NOTIFY_SOCKET", "/run/systemd/notify")
    monkeypatch.setenv("INVOCATION_ID", "abc")
    monkeypatch.delenv("LISTEN_PID")
    assert not fdstore.FdStore().enabled and "NOTIFY_SOCKET" not in os.environ  # removed even when it is not trusted


def test_without_a_notify_socket_the_store_refuses():
    store = fdstore.FdStore({})
    sock = socket.socket()
    assert not store.enabled and not store.add("a-1", sock) and not store.remove("a-1")
    sock.close()


# -- the unit and the settings ----------------------------------------------------------------------------------------------------

def test_the_unit_keeps_one_descriptor_per_possible_attempt_and_preserves_the_store_on_new_systemd():
    for version in (254, 255, 259):
        text = install.unit_text(systemd_version=version)
        assert f"\nFileDescriptorStoreMax={MAX_PORT_RANGE}\n" in text and "FileDescriptorStorePreserve=yes" in text
        assert f"Environment=MOTEL_FDSTORE_MAX={MAX_PORT_RANGE}\n" in text and "Environment=MOTEL_FDSTORE_SIGNAL=1\n" in text
    for version in (None, 253, 252, 249):
        text = install.unit_text(systemd_version=version)
        assert f"\nFileDescriptorStoreMax={MAX_PORT_RANGE}\n" in text and f"Environment=MOTEL_FDSTORE_MAX={MAX_PORT_RANGE}\n" in text
        assert "FileDescriptorStorePreserve" not in text and "MOTEL_FDSTORE_SIGNAL" not in text
    assert "NotifyAccess=main" in install.unit_text()


def test_the_misspelled_key_is_gone():
    """H1: ``FileDescriptorStore=`` is not a systemd key; the unit has to say ``FileDescriptorStoreMax=``."""
    for version in (None, 252, 259):
        assert "FileDescriptorStore=" not in install.unit_text(systemd_version=version)


def test_the_systemd_version_is_read_from_systemctl():
    class Out:
        def __init__(self, stdout):
            self.stdout = stdout
    assert install.detect_systemd_version(lambda *a, **k: Out("systemd 255 (255.4-1ubuntu8)\n+PAM +AUDIT\n")) == 255
    assert install.detect_systemd_version(lambda *a, **k: Out("systemd 249 (249.11-0ubuntu3)\n")) == 249
    assert install.detect_systemd_version(lambda *a, **k: Out("something else")) is None

    def missing(*a, **k):
        raise FileNotFoundError
    assert install.detect_systemd_version(missing) is None


def test_a_port_range_larger_than_the_store_can_hold_is_refused():
    base = {}
    assert parse_tours_section({**base, "concierge_port_range": f"20000-{20000 + MAX_PORT_RANGE - 1}"}).concierge_port_range[1] == 20000 + MAX_PORT_RANGE - 1
    with pytest.raises(ConfigError, match="at most 512"):
        parse_tours_section({**base, "concierge_port_range": f"20000-{20000 + MAX_PORT_RANGE}"})
    with pytest.raises(ConfigError, match="at most 512"):
        parse_tours_section({"concierge_port_range": "1024-65535"})


# -- L1: two concurrent issues of one attempt ------------------------------------------------------------------------------------------

def test_a_second_concurrent_issue_of_the_same_attempt_is_refused_and_never_touches_the_winners_stored_socket(up):
    import threading
    h = Harness(up.url)
    try:
        real = h.concierge.endpoints.preserve
        stored, release = threading.Event(), threading.Event()

        def slow_preserve(attempt, sock):
            real(attempt, sock)  # the winner's socket is in the store now ...
            stored.set()
            assert release.wait(30)  # ... and its issue is still in progress

        h.concierge.endpoints.preserve = slow_preserve
        result = {}
        winner = threading.Thread(target=lambda: result.update(token=h.issue("a-1")))
        winner.start()
        assert stored.wait(30)
        with pytest.raises(AdminRefused) as info:  # the loser arrives while the winner is mid-issue
            h.registry.issue(tour="t-1", guest="g-1", attempt="a-1", bindings=["anthropic"], expires_at=soon(), budget_usd=1.0)
        assert info.value.kind == "exists"
        h.settle()
        assert [name for name, _ in h.systemd.names()] == ["att-a-1"]  # the loser did not unstore the winner's socket
        release.set()
        winner.join(30)
        port = h.port_of("a-1")
        assert h.systemd.names() == [("att-a-1", port)] and post(port, result["token"]) == 200
        h.concierge.stop()
        h.settle()
        h.concierge.endpoints.preserve = real
        h.restart()  # and it survives a restart: the store entry was never dropped
        assert h.concierge.endpoints.port_of("a-1") == port and post(port, result["token"]) == 200
    finally:
        release.set()
        h.close()


def test_many_threads_issuing_one_attempt_yield_exactly_one_token_and_one_stored_socket(up):
    import threading
    h = Harness(up.url)
    try:
        outcomes, barrier = [], threading.Barrier(8)

        def go():
            barrier.wait()
            try:
                outcomes.append(("ok", h.registry.issue(tour="t", guest="g", attempt="a-1", bindings=["anthropic"],
                                                         expires_at=soon(), budget_usd=1.0)))
            except AdminRefused as exc:
                outcomes.append(("refused", exc.kind))

        threads = [threading.Thread(target=go) for _ in range(8)]
        [t.start() for t in threads]
        [t.join(30) for t in threads]
        assert sorted(kind for kind, _ in outcomes) == ["ok"] + ["refused"] * 7 and {v for k, v in outcomes if k == "refused"} == {"exists"}
        h.settle()
        assert [name for name, _ in h.systemd.names()] == ["att-a-1"] and h.concierge.endpoints.count() == 1
    finally:
        h.close()


# -- review L5/L-a: the unit's marker (and $FDSTORE on systemd >= 254) must show a store at least as large as the range ----------------------------------------------------------

def test_the_manager_limit_is_read_from_fdstore():
    assert fdstore.manager_limit({"FDSTORE": "512"}) == 512 and fdstore.manager_limit({"FDSTORE": "0"}) == 0
    assert fdstore.manager_limit({}) is None and fdstore.manager_limit({"FDSTORE": "many"}) is None and fdstore.manager_limit({"FDSTORE": "-1"}) is None


def unit_env(**env):
    return {k: v for k, v in env.items() if v is not None}


def check(needed=200, **env):
    store = fdstore.FdStore({"NOTIFY_SOCKET": "/run/systemd/notify", "LISTEN_PID": "7"}, pid=7)
    message = store.check_unit(needed, unit_env(**env))
    return message, store


def test_a_unit_without_the_marker_is_old_or_hand_written_and_refused_whatever_fdstore_says():
    for env in ({}, {"FDSTORE": "512"}, {"FDSTORE": "512", "MOTEL_FDSTORE_SIGNAL": "1"}, {"MOTEL_FDSTORE_MAX": "many"}):
        message, _ = check(**env)
        assert "MOTEL_FDSTORE_MAX marker" in message and "old or hand-written" in message and "sudo motel install" in message


def test_a_marker_smaller_than_the_port_range_is_refused():
    message, _ = check(MOTEL_FDSTORE_MAX="199")
    assert "sized 199" in message and "200 live attempts" in message and "sudo motel install" in message
    assert check(MOTEL_FDSTORE_MAX="200")[0] is None and check(MOTEL_FDSTORE_MAX="512")[0] is None


def test_on_systemd_before_254_the_marker_alone_is_enough_because_fdstore_is_never_set():
    message, store = check(MOTEL_FDSTORE_MAX="512")  # no $FDSTORE, no signal: a unit written for an older systemd
    assert message is None and store.limit == 512


def test_a_fdstore_that_disagrees_with_the_marker_is_refused():
    message, _ = check(MOTEL_FDSTORE_MAX="512", FDSTORE="64")
    assert "$FDSTORE is 64" in message and "marker says 512" in message and "disagree" in message
    assert check(MOTEL_FDSTORE_MAX="512", FDSTORE="512")[0] is None
    assert check(MOTEL_FDSTORE_MAX="512", FDSTORE="512", MOTEL_FDSTORE_SIGNAL="1")[0] is None
    assert check(MOTEL_FDSTORE_MAX="256", FDSTORE="512")[0] is not None  # the other way round


def test_a_unit_written_for_systemd_254_whose_manager_sets_no_fdstore_is_refused():
    """The review's H1: systemd ignored the (misspelled) store key, so it set no $FDSTORE; the marker alone looked fine."""
    message, _ = check(MOTEL_FDSTORE_MAX="512", MOTEL_FDSTORE_SIGNAL="1")
    assert "$FDSTORE is not set" in message and "ignored FileDescriptorStoreMax=" in message
    assert check(MOTEL_FDSTORE_MAX="512", MOTEL_FDSTORE_SIGNAL="1", FDSTORE="512")[0] is None


def test_a_store_below_the_codes_limit_lowers_the_limit_and_warns(caplog):
    with caplog.at_level("WARNING", logger="motel.concierge"):
        message, store = check(MOTEL_FDSTORE_MAX="250")
    assert message is None and store.limit == 250 and "below the code's limit" in caplog.text
    assert check(MOTEL_FDSTORE_MAX="512")[1].limit == 512


def start_main(tmp_path, monkeypatch, env, argv=()):
    from motel.concierge import service
    key = tmp_path / "k"
    key.write_text("sk-test\n")
    cfg = tmp_path / "motel.toml"
    cfg.write_text(f'[tours]\nenabled = true\nconcierge_port_range = "20000-20199"\n[tours.bindings.a]\nupstream = "https://api.example.com"\n'
                   f'auth = "x-api-key"\nkey_file = "{key}"\npaths = ["/v1/messages"]\n')
    for name in ("NOTIFY_SOCKET", "LISTEN_PID", "LISTEN_FDS", "FDSTORE", "MOTEL_FDSTORE_MAX", "MOTEL_FDSTORE_SIGNAL"):
        monkeypatch.delenv(name, raising=False)
    for name, value in env.items():
        monkeypatch.setenv(name, value)
    started = []
    monkeypatch.setattr(service.Concierge, "start", lambda self: started.append(self) or self)
    monkeypatch.setattr(service.threading.Event, "wait", lambda self, *a, **k: True)  # main returns instead of serving
    sock_dir = short_dir()
    try:
        code = service.main(["--config", str(cfg), "--state-dir", str(tmp_path), "--admin-socket", os.path.join(sock_dir, "a.sock"), *argv])
    finally:
        shutil.rmtree(sock_dir, ignore_errors=True)
    return code, started


def test_the_service_refuses_to_start_on_an_old_unit_or_a_small_or_disagreeing_store(tmp_path, monkeypatch, capsys):
    base = {"NOTIFY_SOCKET": "/run/systemd/notify", "LISTEN_PID": str(os.getpid())}
    for extra, fragment in (({}, "MOTEL_FDSTORE_MAX"), ({"MOTEL_FDSTORE_MAX": "64"}, "sized 64"),
                            ({"MOTEL_FDSTORE_MAX": "512", "FDSTORE": "64"}, "disagree"),
                            ({"MOTEL_FDSTORE_MAX": "512", "MOTEL_FDSTORE_SIGNAL": "1"}, "$FDSTORE is not set")):
        code, started = start_main(tmp_path, monkeypatch, {**base, **extra})
        err = capsys.readouterr().err
        assert code != 0 and not started and fragment in err and "sudo motel install" in err, (extra, err)
    for extra in ({"MOTEL_FDSTORE_MAX": "512"}, {"MOTEL_FDSTORE_MAX": "512", "FDSTORE": "512", "MOTEL_FDSTORE_SIGNAL": "1"}):
        code, started = start_main(tmp_path, monkeypatch, {**base, **extra})
        assert code == 0 and len(started) == 1
    code, started = start_main(tmp_path, monkeypatch, {}, ["--dev-rebind"])  # the unsafe dev mode needs no store
    assert code == 0 and len(started) == 1
