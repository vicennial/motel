"""Slow and idle connections: header deadline, body rate, per-attempt connection cap, host-wide body budget."""
import json
import socket
import threading
import time

import pytest
from concierge_fakes import BODY, FakeUpstream, Harness, error_kind, raw_exchange

from motel.concierge import proxy
from motel.concierge.settings import NODE_BYTES, worst_case_charge

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


class ParseGate:
    """Holds every request whose JSON text contains ``"hold"`` at the parse step, after its whole memory charge is taken,
    so a test can read what the budget holds. Event-driven: ``wait_held(n)`` returns once n requests are at the gate."""

    def __init__(self, monkeypatch):
        self.release = threading.Event()
        self._entered = threading.Semaphore(0)
        real = proxy.parse_json

        def parse(body):
            if isinstance(body, str) and '"hold"' in body:
                self._entered.release()
                assert self.release.wait(30)
            return real(body)
        monkeypatch.setattr(proxy, "parse_json", parse)

    def wait_held(self, n):
        for _ in range(n):
            assert self._entered.acquire(timeout=30)


def padded(size, char="x", **extra):
    """A valid request body of exactly ``size`` bytes (ASCII) that the gate holds."""
    body = {**BODY, "hold": True, **extra}
    raw = json.dumps(body, separators=(",", ":"))
    pad = size - len(raw.encode()) - len(',"pad":""')
    return json.dumps({**body, "pad": char * pad}, separators=(",", ":")).encode()


def in_threads(h, token, raw, results, count=1):
    threads = [threading.Thread(target=lambda: results.append(h.post(URL, token, raw_body=raw, timeout=40))) for _ in range(count)]
    [t.start() for t in threads]
    return threads


def read_all(sock, timeout=5):
    sock.settimeout(timeout)
    out = b""
    try:
        while chunk := sock.recv(65536):
            out += chunk
    except (TimeoutError, ConnectionResetError):
        pass
    return out


def test_a_header_trickle_is_cut_at_the_deadline_not_at_the_idle_timeout(up, monkeypatch):
    monkeypatch.setattr(proxy, "HEADER_DEADLINE_S", 1.0)
    h = Harness(up.url)
    try:
        token = h.issue()
        with socket.create_connection(("127.0.0.1", h.port_of(token))) as s:
            started = time.monotonic()
            try:
                for byte in head(token, 2, "X-Pad: " + "a" * 60 + "\r\n"):  # one byte per 100 ms: each read is "in time"
                    s.sendall(bytes([byte]))
                    time.sleep(0.1)
            except OSError:
                pass
            assert read_all(s, 2) == b"" and time.monotonic() - started < 3
    finally:
        h.close()


def test_a_body_that_stalls_is_cut_by_the_minimum_rate(up, monkeypatch):
    monkeypatch.setattr(proxy, "BODY_GRACE_S", 1.0)
    h = Harness(up.url)
    try:
        token = h.issue()
        with socket.create_connection(("127.0.0.1", h.port_of(token))) as s:
            s.sendall(head(token, 5000) + b"{")
            started = time.monotonic()
            assert read_all(s, 5) == b"" and time.monotonic() - started < 3.5
        assert up.requests == []
    finally:
        h.close()


def test_a_normal_slow_enough_body_is_accepted(up):
    h = Harness(up.url)
    try:
        body = json.dumps(BODY).encode()
        token = h.issue()
        with socket.create_connection(("127.0.0.1", h.port_of(token))) as s:
            s.sendall(head(token, len(body)) + body[:10])
            time.sleep(0.5)
            s.sendall(body[10:])
            assert b" 200 " in read_all(s, 5).split(b"\r\n")[0]
    finally:
        h.close()


def test_one_attempt_cannot_hold_more_than_its_concurrency_plus_a_margin(up):
    h = Harness(up.url)
    held = []
    try:
        t1 = h.issue("a-1", max_concurrent=1)
        other = h.issue("a-2", guest="g-2")
        for _ in range(1 + 2):  # max_concurrent + the margin of 2
            s = socket.create_connection(("127.0.0.1", h.port_of(t1)))
            s.sendall(head(t1, 100) + b"{")  # authenticated, body still to come
            held.append(s)
        time.sleep(0.3)
        status, headers, body = h.post(URL, t1)
        assert status == 429 and error_kind(body) == "motel_concurrency" and dict(headers)["Retry-After"] == "1"
        assert h.post(URL, other)[0] == 200  # the pool is not exhausted for anyone else
        held.pop().close()  # a slot comes back as soon as a connection goes away
        time.sleep(0.3)
        assert h.post(URL, t1)[0] == 200
    finally:
        for s in held:
            s.close()
        h.close()


def test_host_wide_buffered_bodies_are_capped(monkeypatch):
    gate = ParseGate(monkeypatch)
    up = FakeUpstream()
    h = Harness(up.url, tour={"concierge_max_request_mb": 0.01, "concierge_max_buffered_mb": 0.18,
                                  "concierge_max_json_nodes": 100})  # cap 10485 B, budget 188743 B
    results, threads = [], []
    try:
        raw = padded(9000)  # about 5 x 9000 + its nodes = 47000 bytes of the budget while it is parsed
        tokens = [h.issue(f"a-{i}", guest=f"g-{i}") for i in range(4)]
        for token in tokens[:3]:
            threads += in_threads(h, token, raw, results)
        gate.wait_held(3)
        status, headers, body = h.post(URL, tokens[3], raw_body=raw)
        assert status == 429 and error_kind(body) == "motel_busy" and dict(headers)["Retry-After"] == "1"
        assert h.post(URL, tokens[3])[0] == 200  # a small request still fits
        gate.release.set()
        [t.join(30) for t in threads]
        assert sorted(r[0] for r in results) == [200, 200, 200]
        assert h.concierge.core.body_budget.used == 0  # everything came back
    finally:
        gate.release.set()
        h.close()
        up.close()


def test_the_body_is_not_invited_before_authentication(up):
    h = Harness(up.url)
    try:
        expect = "Expect: 100-continue\r\n"
        reply = raw_exchange(h.port, head("mt_wrong", 10, expect))
        assert reply.startswith(b"HTTP/1.1 401") and b"100 Continue" not in reply
        body = json.dumps(BODY).encode()
        token = h.issue()
        with socket.create_connection(("127.0.0.1", h.port_of(token))) as s:
            s.sendall(head(token, len(body), expect))
            first = s.recv(1024)
            assert first.startswith(b"HTTP/1.1 100 Continue")  # sent once the request is admitted
            s.sendall(body)
            assert b" 200 " in read_all(s, 5).split(b"\r\n")[0]
    finally:
        h.close()


def test_a_guest_that_hangs_up_before_it_reads_a_refusal_causes_no_error_log(caplog):
    """N8: writing the refusal to a gone peer must not become a logged traceback."""
    import logging

    from motel.concierge import proxy

    class Gone:
        def write(self, data):
            raise BrokenPipeError

    handler = object.__new__(proxy.GuestHandler)
    handler.wfile = Gone()
    handler.request_version = "HTTP/1.1"
    handler._headers_buffer = []
    handler.send_response_only = lambda status, message=None: None
    handler.send_header = lambda name, value: None
    handler.end_headers = lambda: None
    with caplog.at_level(logging.DEBUG):
        handler._reply_error(401, "invalid_token", "unknown token")
        handler._reply_error(401, "invalid_token", "again")  # already answered: a no-op
    assert handler._responded and handler.close_connection and not caplog.records


# -- N2: per-attempt share of the byte budget, and release once the body has gone upstream ---------------------------------

SMALL = {"concierge_max_request_mb": 0.01, "concierge_max_buffered_mb": 0.4, "concierge_max_json_nodes": 100}  # cap 10485 B; budget 419430 B


def test_one_attempt_cannot_take_more_than_its_share_of_the_byte_budget(up, monkeypatch):
    """share = the larger of one worst-case request (14 x 10485 + 200 x 100 nodes = 166790) and a quarter of the budget (104857)."""
    gate = ParseGate(monkeypatch)
    h = Harness(up.url, tour=SMALL)
    results, threads = [], []
    try:
        budget = h.concierge.core.body_budget
        assert (budget.limit, budget.per_owner) == (419430, 166790)
        hog = h.issue("a-1", max_concurrent=6)
        other = h.issue("a-2", guest="g-2")
        raw = padded(9000)
        nodes = proxy.count_nodes(raw.decode(), 10**6)
        each = 5 * len(raw) + nodes * NODE_BYTES  # the bytes, one more copy, three text copies, the tree
        assert 2 * each < budget.per_owner < 4 * each
        threads += in_threads(h, hog, raw, results, count=3)
        gate.wait_held(3)
        assert budget.held_by("a-1") == 3 * each
        status, headers, body = h.post(URL, hog, raw_body=raw)  # a fourth would be over the share
        assert status == 429 and error_kind(body) == "motel_busy" and dict(headers)["Retry-After"] == "1"
        assert budget.used < budget.limit // 2
        assert h.post(URL, other, raw_body=b"x" * 9000)[0] == 400  # not busy: another attempt's body is read (and is not JSON)
        assert h.post(URL, other)[0] == 200
        gate.release.set()
        [t.join(30) for t in threads]
        assert budget.used == 0
    finally:
        gate.release.set()
        h.close()


def test_a_single_worst_case_request_always_fits_its_attempts_share(up):
    h = Harness(up.url, tour={"concierge_max_request_mb": 1, "concierge_max_buffered_mb": 20, "concierge_max_json_nodes": 1000})
    try:
        budget = h.concierge.core.body_budget
        worst = worst_case_charge(h.cfg.max_request_bytes, h.cfg.concierge_max_json_nodes)
        assert worst == 14 * 1024 * 1024 + 200 * 1000 and budget.per_owner == max(worst, budget.limit // 4)
        assert budget.acquire(worst, "a-1")  # nodes included
        assert not budget.acquire(1, "a-1")
    finally:
        h.close()


def test_the_default_share_is_one_worst_case_request_with_its_nodes():
    from motel.concierge.proxy import Core
    from motel.concierge.settings import parse_tours_section
    cfg = parse_tours_section({})
    core = Core(cfg, None, None)
    worst = 14 * cfg.max_request_bytes + 200 * cfg.concierge_max_json_nodes
    assert core.body_budget.per_owner == max(worst, cfg.max_buffered_bytes // 4) == worst == 157_440_512 <= cfg.max_buffered_bytes


# -- review F3: a request that can never fit is 413, not 429 -----------------------------------------------------------------------

def test_a_request_whose_charge_can_never_fit_the_share_is_413_and_one_that_must_wait_is_429(up, monkeypatch):
    gate = ParseGate(monkeypatch)
    h = Harness(up.url)
    results, threads = [], []
    try:
        token = h.issue("a-1", max_concurrent=4)
        raw = padded(9000)
        each = 5 * len(raw) + proxy.count_nodes(raw.decode(), 10**6) * NODE_BYTES
        # an attempt share that holds one request but not two, and a host total that holds two
        h.concierge.core.body_budget = proxy.ByteBudget(2 * each + 1000, each + 1000)
        budget = h.concierge.core.body_budget
        threads += in_threads(h, token, raw, results)
        gate.wait_held(1)
        status, headers, body = h.post(URL, token, raw_body=raw)  # fits an empty share, not the share's remainder: wait
        assert status == 429 and error_kind(body) == "motel_busy" and dict(headers)["Retry-After"] == "1"
        gate.release.set()
        [t.join(30) for t in threads]
        assert results[0][0] == 200 and budget.used == 0
        too_big = padded(12_000)  # its own charge exceeds the share: waiting would never help
        status, headers, body = h.post(URL, token, raw_body=too_big)
        assert status == 413 and error_kind(body) == "motel_request_too_large" and "Retry-After" not in dict(headers)
        assert budget.used == 0 and budget.held_by("a-1") == 0
        h.concierge.core.body_budget = proxy.ByteBudget(each // 2, each // 2)  # the host total too small for the raw body's steps
        assert h.post(URL, token, raw_body=raw)[0] == 413
    finally:
        gate.release.set()
        h.close()


def test_the_node_charge_counts_toward_the_ceiling(up):
    """A body of many tiny nodes: small in bytes, large in objects. With a share that holds its bytes but not its nodes: 413."""
    h = Harness(up.url, tour={"concierge_max_request_mb": 1, "concierge_max_buffered_mb": 60, "concierge_max_json_nodes": 100000})
    try:
        token = h.issue("a-1", budget=5.0)
        raw = b'{"model":"claude-sonnet-4-6","max_tokens":5,"messages":[],"x":[' + b"1," * 60_000 + b"1]}"
        h.concierge.core.body_budget = proxy.ByteBudget(8 * len(raw) + 100, 8 * len(raw) + 100)  # holds the bytes, not 60000 x 200 more
        status, _, body = h.post(URL, token, raw_body=raw)
        assert status == 413 and error_kind(body) == "motel_request_too_large"
        assert h.concierge.core.body_budget.used == 0
    finally:
        h.close()


# -- R4: the charge follows the decoded text's real size ---------------------------------------------------------------------

def test_the_text_bound_follows_the_string_kind_and_covers_the_real_size():
    import sys
    cases = {"ascii": ("a" * 1000, "utf-8"), "latin1": ("é" * 1000, "utf-8"), "bmp": ("€" * 1000, "utf-8"),
             "ascii+bmp": ("a" * 1000 + "€", "utf-8"), "ascii+astral": ("a" * 1000 + "\U0001F600", "utf-8"),
             "utf-16 bmp": ("€" * 1000, "utf-16"), "utf-32": ("\U0001F600" * 1000, "utf-32")}
    for name, (text, codec) in cases.items():
        raw = text.encode(codec)
        encoding = json.detect_encoding(raw)
        bound = proxy.text_bytes_bound(raw, encoding)
        real = sys.getsizeof(proxy.decode_body(raw))
        assert bound >= real - 80, (name, bound, real)  # 80: the str object's own header
    assert proxy.text_bytes_bound(("a" * 1000).encode()) == 1000
    assert proxy.text_bytes_bound(("é" * 1000).encode()) == 2000  # Latin-1 range: the bound is the body length (one byte per character)
    assert proxy.text_bytes_bound(("€" * 1000).encode()) == 2 * 3000
    assert proxy.text_bytes_bound(("a" * 1000 + "\U0001F600").encode()) == 4 * 1004


def test_an_ascii_body_is_charged_far_less_than_one_with_an_emoji_in_it(up, monkeypatch):
    gate = ParseGate(monkeypatch)
    h = Harness(up.url)
    results = []
    try:
        budget = h.concierge.core.body_budget
        plain = padded(40_000)
        ascii_tok, emoji_tok = h.issue("a-1"), h.issue("a-2", guest="g-2")
        nodes = proxy.count_nodes(plain.decode(), 10**6)
        threads = in_threads(h, ascii_tok, plain, results)
        gate.wait_held(1)
        assert budget.held_by("a-1") == 5 * len(plain) + nodes * NODE_BYTES  # not the flat 14x of the worst case
        # the same size, with one emoji: the whole text is 4 bytes per character in memory
        wide = json.dumps({**json.loads(plain), "e": "\U0001F600"}, separators=(",", ":"), ensure_ascii=False).encode()
        threads += in_threads(h, emoji_tok, wide, results)
        gate.wait_held(1)
        assert budget.held_by("a-2") == 2 * len(wide) + 3 * 4 * len(wide) + proxy.count_nodes(wide.decode(), 10**6) * NODE_BYTES
        gate.release.set()
        [t.join(30) for t in threads]
        assert budget.used == 0 and sorted(r[0] for r in results) == [200, 200]
    finally:
        gate.release.set()
        h.close()


def test_the_body_charge_is_released_once_the_body_has_gone_upstream(up):
    h = Harness(up.url)
    gate = threading.Event()
    seen = threading.Event()
    result = {}

    def slow(req, handler):
        seen.set()
        gate.wait(10)
        return 200, {"Content-Type": "application/json"}, [json.dumps({"model": BODY["model"], "usage": {"input_tokens": 1, "output_tokens": 1}}).encode()]

    up.reply = slow
    try:
        token = h.issue(budget=5.0)
        t = threading.Thread(target=lambda: result.update(r=h.post(URL, token, body=BODY, timeout=15)))
        t.start()
        assert seen.wait(5)  # the upstream has the whole body and has not answered yet
        time.sleep(0.2)
        budget = h.concierge.core.body_budget
        assert budget.used == 0 and budget.held_by("a-1") == 0
        gate.set()
        t.join(10)
        assert result["r"][0] == 200 and budget.used == 0
    finally:
        gate.set()
        h.close()


def test_the_charge_comes_back_when_the_request_fails_before_the_upstream(up):
    h = Harness(up.url, binding={"allow": []})
    try:
        token = h.issue(budget=1.0)
        bad = json.dumps({**BODY, "thread": {"id": "t"}}).encode()
        assert h.post(URL, token, raw_body=bad)[0] == 403  # refused after the body was read
        assert h.post(URL, token, raw_body=b"{" * 100)[0] == 400
        budget = h.concierge.core.body_budget
        assert budget.used == 0 and budget.held_by("a-1") == 0
    finally:
        h.close()
