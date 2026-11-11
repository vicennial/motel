"""The guest listener against a loopback fake upstream: swap, refusals, redirects, streaming, limits."""
import http.client
import json
import threading
import time

import pytest
from concierge_fakes import BODY, MODEL, OK_COST, REAL, FakeUpstream, Harness, error_kind, raw_exchange, request, sse


@pytest.fixture
def upstream():
    up = FakeUpstream()
    yield up
    up.close()


@pytest.fixture
def h(upstream):
    harness = Harness(upstream.url)
    yield harness
    harness.close()


def test_token_swap_injects_the_real_key_and_strips_guest_auth(h, upstream):
    token = h.issue()
    status, _, body = h.post("/u/anthropic/v1/messages", token, headers={"Authorization": "Bearer other", "Cookie": "s=1",
                                                                        "Proxy-Authorization": "Basic eA=="})
    assert status == 200 and json.loads(body)["id"] == "msg_1"
    assert upstream.header(0, "x-api-key") == [REAL]
    assert upstream.header(0, "authorization") == [] and upstream.header(0, "cookie") == []
    assert upstream.header(0, "proxy-authorization") == []
    assert token not in json.dumps(upstream.requests[0]["headers"])
    assert upstream.header(0, "anthropic-version") == ["2023-06-01"]
    assert upstream.requests[0]["path"] == "/v1/messages"
    assert json.loads(upstream.requests[0]["body"]) == BODY


def test_bearer_binding_uses_authorization_and_accepts_a_bearer_token(upstream):
    h = Harness(upstream.url, binding={"auth": "bearer", "paths": ["/v1/chat/completions"]})
    try:
        token = h.issue()
        body = {"model": "gpt-4o", "max_tokens": 10, "messages": []}
        status, _, _ = h.post("/u/anthropic/v1/chat/completions", token, body=body, auth="bearer")
        assert status == 200
        assert upstream.header(0, "authorization") == [f"Bearer {REAL}"]
        assert upstream.header(0, "x-api-key") == []
    finally:
        h.close()


def test_guest_never_sees_the_real_key_even_if_the_upstream_echoes_it(h, upstream):
    def echo(req, handler):
        seen = json.dumps(req["headers"])
        body = json.dumps({"echo": seen, "usage": {"input_tokens": 1, "output_tokens": 1}, "model": MODEL}).encode()
        return 200, {"Content-Type": "application/json", "X-Echo": req["headers"][-1][1] + REAL, "X-Key": REAL,
                     "Set-Cookie": f"k={REAL}"}, [body[:40], body[40:]]

    upstream.reply = echo
    status, headers, body = h.post("/u/anthropic/v1/messages", h.issue())
    assert status == 200
    assert REAL.encode() not in body and REAL not in json.dumps(headers)
    assert b"[redacted]" in body
    assert "set-cookie" not in {k.lower() for k, _ in headers}
    assert h.events("settle")[0]["redactions"] >= 1


def test_key_split_across_stream_chunks_is_still_redacted(h, upstream):
    half = len(REAL) // 2
    upstream.reply = lambda req, handler: (200, {"Content-Type": "text/event-stream"},
                                           [b"data: {\"x\": \"" + REAL.encode()[:half], REAL.encode()[half:] + b"\"}\n\n"])
    status, _, body = h.post("/u/anthropic/v1/messages", h.issue())
    assert status == 200 and REAL.encode() not in body and b"[redacted]" in body


def test_wrong_binding_and_wrong_path_are_refused_before_dispatch(upstream):
    h = Harness(upstream.url, extra_bindings={"other": {"upstream": upstream.url, "auth": "bearer", "key_file": "/nonexistent/k",
                                                        "paths": ["/v1/x"]}})
    try:
        token = h.issue(bindings=("anthropic",))
        assert h.post("/u/other/v1/x", token)[0] == 403
        assert error_kind(h.post("/u/other/v1/x", token)[2]) == "motel_binding_denied"
        assert error_kind(h.post("/u/nosuch/v1/messages", token)[2]) == "motel_binding_denied"
        assert error_kind(h.post("/u/anthropic/v1/complete", token)[2]) == "motel_path_denied"
        assert error_kind(h.post("/u/anthropic/v1/messages/../models", token)[2]) == "motel_bad_request"
        assert error_kind(h.post("/u/anthropic/v1/%6dessages", token)[2]) == "motel_bad_request"
        assert error_kind(h.post("/u/anthropic/v1/messages", token, method="PUT")[2]) == "motel_method_not_allowed"
        assert h.post("/u/anthropic/v1/messages", token, method="GET", body=None)[0] == 405
        assert upstream.requests == []
        assert h.registry.usage("a-1")["refused"] == 5  # 3 binding, 1 path, 1 method (the others fail before authentication)
    finally:
        h.close()


def test_unknown_or_missing_token_is_401(h, upstream):
    assert h.post("/u/anthropic/v1/messages", None)[0] == 401
    status, _, body = h.post("/u/anthropic/v1/messages", "mt_" + "x" * 40)
    assert status == 401 and error_kind(body) == "motel_invalid_token"
    assert h.post("/u/anthropic/v1/messages", REAL)[0] == 401  # the real key is not a token
    assert upstream.requests == []


def test_host_headers_and_absolute_uris_cannot_change_the_destination(h, upstream):
    evil = FakeUpstream()
    try:
        token = h.issue()
        h.post("/u/anthropic/v1/messages", token, headers={"Host": f"127.0.0.1:{evil.port}", "X-Forwarded-Host": "evil.example",
                                                           "X-Forwarded-For": "192.0.2.9", "Forwarded": "host=evil.example",
                                                           "X-Original-URL": "/admin"})
        assert len(upstream.requests) == 1 and evil.requests == []
        assert upstream.header(0, "host") == [f"127.0.0.1:{upstream.port}"]
        for name in ("x-forwarded-host", "x-forwarded-for", "forwarded", "x-original-url"):
            assert upstream.header(0, name) == []
        for target in (f"http://127.0.0.1:{evil.port}/u/anthropic/v1/messages", "http://evil.example/v1/messages",
                       f"//127.0.0.1:{evil.port}/u/anthropic/v1/messages", "/u/anthropic/http://evil.example/v1/messages"):
            data = (f"POST {target} HTTP/1.1\r\nHost: 127.0.0.1\r\nx-api-key: {token}\r\nContent-Length: 2\r\n\r\n{{}}").encode()
            reply = raw_exchange(h.port, data)
            assert b" 4" in reply.split(b"\r\n")[0], reply
        assert evil.requests == [] and len(upstream.requests) == 1
    finally:
        evil.close()


def test_query_string_is_passed_but_cannot_select_a_destination(h, upstream):
    token = h.issue()
    assert h.post("/u/anthropic/v1/messages?beta=true", token)[0] == 200
    assert upstream.requests[-1]["path"] == "/v1/messages?beta=true"
    for bad in ("a=\"b\"", "a=<b>", "a=b\\c"):  # characters outside the URL-safe set
        data = (f"POST /u/anthropic/v1/messages?{bad} HTTP/1.1\r\nHost: x\r\nx-api-key: {token}\r\nContent-Length: 2\r\n\r\n{{}}").encode()
        assert b" 400 " in raw_exchange(h.port, data).split(b"\r\n")[0]
    assert len(upstream.requests) == 1


def test_a_redirect_is_passed_through_not_followed(h, upstream):
    thief = FakeUpstream()
    try:
        upstream.reply = lambda req, handler: (302, {"Location": f"http://127.0.0.1:{thief.port}/steal", "Content-Type": "text/plain"},
                                               [b"moved"])
        status, headers, body = h.post("/u/anthropic/v1/messages", h.issue())
        assert status == 302 and body == b"moved"
        assert dict(headers)["Location"].endswith("/steal")
        assert thief.requests == []
        assert len(upstream.requests) == 1
        assert REAL not in json.dumps(headers)
        assert h.registry.usage("a-1")["usd"] == 0  # a redirect is not billed
    finally:
        thief.close()


def test_expired_and_revoked_tokens_are_refused(upstream):
    now = [1_800_000_000.0]
    h = Harness(upstream.url, clock=lambda: now[0])
    try:
        token = h.issue("a-exp", ttl=60)
        assert h.post("/u/anthropic/v1/messages", token)[0] == 200
        now[0] += 61
        status, _, body = h.post("/u/anthropic/v1/messages", token)
        assert status == 401 and error_kind(body) == "motel_token_expired"
        other = h.issue("a-rev", ttl=3600)
        assert h.registry.revoke(attempt="a-rev") == 1
        with pytest.raises(OSError):  # a revoked attempt's listener is closed: nothing is left to answer
            h.post("/u/anthropic/v1/messages", other)
        h.concierge.endpoints.sweep()  # and so is an expired one, at the next sweep
        with pytest.raises(OSError):
            h.post("/u/anthropic/v1/messages", token)
        assert len(upstream.requests) == 1
    finally:
        h.close()


def test_budget_exhaustion_is_refused_before_dispatch(h, upstream):
    token = h.issue(budget=0.01)
    big = dict(BODY, max_tokens=4096)  # worst case 4096 * $15/M = $0.06 on this model
    status, _, body = h.post("/u/anthropic/v1/messages", token, body=big)
    assert status == 403 and error_kind(body) == "motel_budget_exhausted"
    assert "max_tokens" in json.loads(body)["error"]["message"]
    assert upstream.requests == []
    assert h.registry.usage("a-1") == {"usd": 0, "input_tokens": 0, "output_tokens": 0, "requests": 0, "refused": 1}


def test_spend_accumulates_and_later_requests_are_refused_once_the_budget_is_gone(h, upstream):
    token = h.issue(budget=0.0035)  # a request reserves ~$0.0015 + input; the actual cost is $0.00105
    results = [h.post("/u/anthropic/v1/messages", token)[0] for _ in range(5)]
    assert results[:2] == [200, 200] and results[-1] == 403
    usage = h.registry.usage("a-1")
    assert usage["usd"] == pytest.approx(OK_COST * results.count(200), abs=1e-6)
    assert usage["input_tokens"] == 100 * results.count(200) and usage["output_tokens"] == 50 * results.count(200)


def test_max_tokens_is_required_for_the_anthropic_format_and_must_be_sane(h, upstream):
    token = h.issue()
    no_cap = {"model": MODEL, "messages": []}
    assert error_kind(h.post("/u/anthropic/v1/messages", token, body=no_cap)[2]) == "motel_max_tokens_required"
    for bad in (0, -5, "100", 1.5, True, 10**9):
        status, _, body = h.post("/u/anthropic/v1/messages", token, body=dict(BODY, max_tokens=bad))
        assert status == 400 and error_kind(body) == "motel_bad_max_tokens"
    assert error_kind(h.post("/u/anthropic/v1/messages", token, raw_body=b"not json")[2]) == "motel_bad_request"
    assert upstream.requests == []


def test_count_tokens_is_not_metered(h, upstream):
    token = h.issue(budget=0.0001)
    body = {"model": MODEL, "messages": [{"role": "user", "content": "x" * 5000}]}
    upstream.reply = lambda req, handler: (200, {"Content-Type": "application/json"}, [b'{"input_tokens": 1250}'])
    assert h.post("/u/anthropic/v1/messages/count_tokens", token, body=body)[0] == 200
    assert h.registry.usage("a-1")["usd"] == 0


def test_sse_usage_is_parsed_and_the_stream_is_relayed_in_order(h, upstream):
    events = sse(("message_start", {"type": "message_start", "message": {"model": MODEL, "usage": {
                    "input_tokens": 1000, "cache_read_input_tokens": 2000, "cache_creation_input_tokens": 500, "output_tokens": 1}}}),
                 ("content_block_delta", {"type": "content_block_delta", "delta": {"text": "hello"}}),
                 ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 300}}),
                 ("message_stop", {"type": "message_stop"}))
    gate = threading.Event()

    def reply(req, handler):
        return 200, {"Content-Type": "text/event-stream"}, [events[0], events[1], lambda: (gate.wait(5), events[2])[1], events[3]]

    upstream.reply = reply
    token = h.issue()
    result = {}
    thread = threading.Thread(target=lambda: result.update(zip(("status", "headers", "body"),
                                                               h.post("/u/anthropic/v1/messages", token, body=dict(BODY, stream=True)), strict=True)))
    thread.start()
    time.sleep(0.3)
    assert "body" not in result  # still streaming: the guest's request has not completed
    gate.set()
    thread.join(10)
    assert result["status"] == 200
    assert result["body"] == b"".join(events)
    cost = (1000 * 3 + 2000 * 0.3 + 500 * 3.75 + 300 * 15) / 1e6
    usage = h.registry.usage("a-1")
    assert usage["usd"] == pytest.approx(cost, abs=1e-6)
    assert usage["input_tokens"] == 3500 and usage["output_tokens"] == 300
    settle = h.events("settle")[0]
    assert settle["outcome"] == "ok" and settle["cache_read"] == 2000 and settle["price_unknown"] is False


def test_an_interrupted_stream_is_charged_input_plus_worst_case_output(h, upstream):
    events = sse(("message_start", {"type": "message_start", "message": {"model": MODEL, "usage": {"input_tokens": 1000, "output_tokens": 1}}}))
    upstream.reply = lambda req, handler: (200, {"Content-Type": "text/event-stream"}, events)  # ends without message_delta
    h.post("/u/anthropic/v1/messages", h.issue(), body=dict(BODY, stream=True, max_tokens=200))
    assert h.registry.usage("a-1")["usd"] == pytest.approx((1000 * 3 + 200 * 15) / 1e6, abs=1e-6)
    assert h.events("settle")[0]["outcome"] == "partial"


def test_an_unmetered_success_is_charged_at_the_reserved_worst_case(h, upstream):
    upstream.reply = lambda req, handler: (200, {"Content-Type": "application/json"}, [b'{"no": "usage"}'])
    h.post("/u/anthropic/v1/messages", h.issue())
    settle = h.events("settle")[0]
    assert settle["outcome"] == "unmetered" and settle["usd"] == pytest.approx(settle["reserved"])
    assert settle["usd"] > OK_COST


def test_upstream_error_responses_pass_through_and_cost_nothing(h, upstream):
    upstream.reply = lambda req, handler: (429, {"Content-Type": "application/json", "retry-after": "7"}, [b'{"type":"error"}'])
    status, headers, body = h.post("/u/anthropic/v1/messages", h.issue())
    assert status == 429 and dict(headers)["retry-after"] == "7" and body == b'{"type":"error"}'
    assert h.registry.usage("a-1")["usd"] == 0


def test_unknown_model_is_priced_high(h, upstream):
    upstream.reply = lambda req, handler: (200, {"Content-Type": "application/json"}, [json.dumps(
        {"model": "future-model-9", "usage": {"input_tokens": 1000, "output_tokens": 100}}).encode()])
    h.post("/u/anthropic/v1/messages", h.issue(budget=2.0), body=dict(BODY, model="future-model-9"))
    assert h.registry.usage("a-1")["usd"] == pytest.approx((1000 * 15 + 100 * 75) / 1e6, abs=1e-6)
    assert h.events("settle")[0]["price_unknown"] is True


def test_per_attempt_concurrency_limit_and_release(h, upstream):
    gate, entered = threading.Event(), threading.Semaphore(0)

    def slow(req, handler):
        entered.release()
        gate.wait(10)
        return default_reply_tuple()

    upstream.reply = slow
    token = h.issue(max_concurrent=2, budget=3.0)
    out = []
    threads = [threading.Thread(target=lambda: out.append(h.post("/u/anthropic/v1/messages", token)[0])) for _ in range(2)]
    for t in threads:
        t.start()
    entered.acquire(timeout=5)
    entered.acquire(timeout=5)
    status, headers, body = h.post("/u/anthropic/v1/messages", token)
    assert status == 429 and error_kind(body) == "motel_concurrency" and dict(headers)["Retry-After"] == "1"
    gate.set()
    for t in threads:
        t.join(10)
    assert out == [200, 200]
    assert h.post("/u/anthropic/v1/messages", token)[0] == 200  # the slots were released


def test_per_host_concurrency_limit_spans_attempts(upstream):
    h = Harness(upstream.url, binding={"max_concurrent": 1})
    try:
        gate, entered = threading.Event(), threading.Semaphore(0)

        def slow(req, handler):
            entered.release()
            gate.wait(10)
            return default_reply_tuple()

        upstream.reply = slow
        t1, t2 = h.issue("a-1"), h.issue("a-2")
        thread = threading.Thread(target=lambda: h.post("/u/anthropic/v1/messages", t1))
        thread.start()
        entered.acquire(timeout=5)
        status, _, body = h.post("/u/anthropic/v1/messages", t2)
        assert status == 429 and error_kind(body) == "motel_host_busy"
        gate.set()
        thread.join(10)
    finally:
        h.close()


def default_reply_tuple():
    from concierge_fakes import default_reply
    return default_reply(None, None)


def test_oversized_bodies_are_refused_without_dispatch(upstream):
    h = Harness(upstream.url, tour={"concierge_max_request_mb": 0.01})
    try:
        token = h.issue()
        big = json.dumps(dict(BODY, pad="x" * 20000)).encode()
        status, _, body = h.post("/u/anthropic/v1/messages", token, raw_body=big)
        assert status == 413 and error_kind(body) == "motel_request_too_large"
        # Declared length above the cap is refused from the header alone, with no body sent at all.
        data = (f"POST /u/anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nx-api-key: {token}\r\nContent-Length: 99999999\r\n\r\n").encode()
        assert b" 413 " in raw_exchange(h.port, data).split(b"\r\n")[0]
        assert upstream.requests == []
    finally:
        h.close()


def test_chunked_and_duplicate_length_requests_are_refused(h, upstream):
    token = h.issue()
    chunked = (f"POST /u/anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nx-api-key: {token}\r\nTransfer-Encoding: chunked\r\n\r\n"
               "2\r\n{}\r\n0\r\n\r\n").encode()
    assert b" 411 " in raw_exchange(h.port, chunked).split(b"\r\n")[0]
    twice = (f"POST /u/anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nx-api-key: {token}\r\nContent-Length: 2\r\nContent-Length: 2\r\n\r\n{{}}").encode()
    assert b" 400 " in raw_exchange(h.port, twice).split(b"\r\n")[0]
    assert upstream.requests == []


def test_two_attempts_never_cross(h, upstream):
    t1, t2 = h.issue("a-1", tour="t-1", budget=1.0), h.issue("a-2", tour="t-2", budget=1.0)
    assert t1 != t2
    assert h.post("/u/anthropic/v1/messages", t1)[0] == 200
    assert h.registry.usage("a-1")["requests"] == 1 and h.registry.usage("a-2")["requests"] == 0
    assert h.post("/u/anthropic/v1/messages", t2)[0] == 200
    status, _, body = request(h.port_of(t2), "POST", "/u/anthropic/v1/messages", {"x-api-key": t1, "Content-Type": "application/json"},
                              json.dumps(BODY).encode())
    assert status == 401 and error_kind(body) == "motel_invalid_token"  # a token is accepted on its own attempt's listener only
    assert h.registry.revoke(tour="t-1") == 1
    with pytest.raises(OSError):
        h.post("/u/anthropic/v1/messages", t1)
    assert h.post("/u/anthropic/v1/messages", t2)[0] == 200  # revoking one tour leaves the other alone
    assert h.registry.usage("a-1")["usd"] == pytest.approx(OK_COST, abs=1e-6)
    assert h.registry.usage("a-2")["usd"] == pytest.approx(2 * OK_COST, abs=1e-6)


def test_the_ledger_holds_neither_tokens_nor_keys(h, upstream):
    t1, t2 = h.issue("a-1"), h.issue("a-2")
    h.post("/u/anthropic/v1/messages", t1)
    h.post("/u/anthropic/v1/messages", t2)
    h.post("/u/anthropic/v1/messages", "mt_wrong_token_value")
    h.post("/u/anthropic/v1/other", t1)
    h.registry.revoke(attempt="a-1")
    text = h.ledger_text()
    for secret in (REAL, t1, t2, "mt_wrong_token_value"):
        assert secret not in text
    assert {e["ev"] for e in h.events()} >= {"start", "issue", "reserve", "settle", "refuse", "revoke"}
    assert all(len(e["token_sha256"]) == 64 for e in h.events("issue"))


def test_upstream_unreachable_is_a_502_and_costs_nothing(h, upstream):
    upstream.close()
    status, _, body = h.post("/u/anthropic/v1/messages", h.issue())
    assert status == 502 and error_kind(body) == "motel_upstream_error"
    assert h.registry.usage("a-1")["usd"] == 0


def test_key_file_problems_are_a_502_without_leaking_anything(h, upstream):
    token = h.issue()
    open(h.key_file, "w").write("short")
    status, _, body = h.post("/u/anthropic/v1/messages", token)
    assert status == 502 and error_kind(body) == "motel_key_unavailable" and b"short" not in body
    import os
    os.unlink(h.key_file)
    assert h.post("/u/anthropic/v1/messages", token)[0] == 502
    assert upstream.requests == []


def test_a_rotated_key_file_takes_effect_without_a_restart(h, upstream):
    token = h.issue(budget=3.0)
    h.post("/u/anthropic/v1/messages", token)
    new_key = "sk-test-rotated-9876543210zyxw"  # gitleaks:allow (fake test value)
    import os
    with open(h.key_file + ".new", "w") as handle:
        handle.write(new_key)
    os.replace(h.key_file + ".new", h.key_file)
    h.post("/u/anthropic/v1/messages", token)
    assert upstream.header(0, "x-api-key") == [REAL] and upstream.header(1, "x-api-key") == [new_key]


def test_an_encoded_upstream_reply_is_refused(h, upstream):
    upstream.reply = lambda req, handler: (200, {"Content-Encoding": "gzip", "Content-Type": "application/json"}, [b"\x1f\x8b"])
    status, _, body = h.post("/u/anthropic/v1/messages", h.issue())
    assert status == 502 and error_kind(body) == "motel_upstream_encoding"
    assert upstream.header(0, "accept-encoding") == ["identity"]


def test_revoking_cuts_a_running_stream_and_charges_the_worst_case(h, upstream):
    gate, started = threading.Event(), threading.Event()

    def stream(req, handler):
        def first():
            started.set()
            return sse(("message_start", {"type": "message_start", "message": {"usage": {"input_tokens": 10}}}))[0]
        return 200, {"Content-Type": "text/event-stream"}, [first, lambda: (gate.wait(8), b"data: {}\n\n")[1]]

    upstream.reply = stream
    token = h.issue()
    out = []

    def call():
        try:
            out.append(h.post("/u/anthropic/v1/messages", token, body=dict(BODY, stream=True)))
        except http.client.IncompleteRead:
            out.append("cut")  # the guest sees a broken stream, not a clean end

    thread = threading.Thread(target=call)
    thread.start()
    started.wait(5)
    time.sleep(0.3)
    began = time.time()
    assert h.registry.revoke(attempt="a-1") == 1
    thread.join(5)
    gate.set()
    assert time.time() - began < 4 and out == ["cut"]
    settle = h.events("settle")[0]
    assert settle["outcome"] == "revoked" and h.registry.usage("a-1")["usd"] > 0


def test_tls_upstream_is_verified(tmp_path):
    from concierge_fakes import make_tls
    material = make_tls(str(tmp_path))
    if material is None:
        pytest.skip("openssl is not installed")
    up = FakeUpstream(tls=material)
    try:
        good = Harness(up.url, tls_ca=material["ca"])
        try:
            assert good.post("/u/anthropic/v1/messages", good.issue())[0] == 200
            assert up.header(0, "x-api-key") == [REAL]
        finally:
            good.close()
        untrusted = Harness(up.url)  # default trust store: the test CA is unknown
        try:
            status, _, body = untrusted.post("/u/anthropic/v1/messages", untrusted.issue())
            assert status == 502 and error_kind(body) == "motel_upstream_tls"
            assert len(up.requests) == 1  # the key was never sent
        finally:
            untrusted.close()
    finally:
        up.close()


def test_request_reaches_nothing_but_the_fixed_origin(h, upstream):
    token = h.issue()
    status, _, _ = request(h.port, "POST", "/u/anthropic/v1/messages", {"x-api-key": token, "Content-Type": "application/json",
                                                                       "Host": "attacker.example:99"}, json.dumps(BODY).encode())
    assert status == 200 and len(upstream.requests) == 1


def test_revoking_while_the_upstream_connection_is_still_being_made_stops_the_request(h, upstream, monkeypatch):
    """The review's repro: revoke between add_handle and the socket existing found nothing to cut."""
    from motel.concierge import proxy
    original = proxy.GuestHandler._connect

    def slow_connect(self, core, binding, handle):
        time.sleep(0.6)  # a slow TCP or TLS handshake
        return original(self, core, binding, handle)

    monkeypatch.setattr(proxy.GuestHandler, "_connect", slow_connect)
    token = h.issue()
    out = []
    thread = threading.Thread(target=lambda: out.append(h.post("/u/anthropic/v1/messages", token)))
    thread.start()
    time.sleep(0.25)  # admitted, now inside _connect
    assert h.registry.revoke(attempt="a-1") == 1
    thread.join(5)
    status, _, body = out[0]
    assert status == 401 and error_kind(body) == "motel_token_revoked"
    assert upstream.requests == []  # the request was never sent
    settle = h.events("settle")[0]
    assert settle["outcome"] == "revoked" and settle["usd"] == 0 and h.registry.usage("a-1")["usd"] == 0


def test_a_stream_cut_after_its_final_usage_is_recorded_as_revoked_not_ok(h, upstream):
    done, hold = threading.Event(), threading.Event()
    events = sse(("message_start", {"type": "message_start", "message": {"model": MODEL, "usage": {"input_tokens": 10}}}),
                 ("message_delta", {"type": "message_delta", "usage": {"output_tokens": 20}}))

    def stream(req, handler):
        def last():
            done.set()
            hold.wait(8)
            return b"data: {}\n\n"
        return 200, {"Content-Type": "text/event-stream"}, [events[0], events[1], last]

    upstream.reply = stream
    token = h.issue()
    out = []

    def call():
        try:
            out.append(h.post("/u/anthropic/v1/messages", token, body=dict(BODY, stream=True)))
        except http.client.IncompleteRead:
            out.append("cut")

    thread = threading.Thread(target=call)
    thread.start()
    done.wait(5)
    time.sleep(0.3)
    h.registry.revoke(attempt="a-1")
    thread.join(5)
    hold.set()
    assert out == ["cut"]
    settle = h.events("settle")[0]
    assert settle["outcome"] == "revoked"
    assert settle["usd"] == pytest.approx((10 * 3 + 20 * 15) / 1e6)  # the usage it did report is still what is charged


def test_an_upstream_that_drops_mid_body_is_recorded_as_cut(h, upstream):
    def short(req, handler):
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", "1000")
        handler.end_headers()
        handler.wfile.write(b'{"partial":')
        handler.wfile.flush()
        handler.close_connection = True
        handler.connection.close()
        return None

    upstream.reply = lambda req, handler: short(req, handler) or (200, {}, [])
    try:
        h.post("/u/anthropic/v1/messages", h.issue())
    except http.client.HTTPException:
        pass
    settle = h.events("settle")[0]
    assert settle["outcome"] == "cut" and settle["usd"] == settle["reserved"]


def test_connect_and_unknown_methods_are_not_implemented(h, upstream):
    from concierge_fakes import raw_exchange
    token = h.issue()
    for line in (b"CONNECT example.invalid:443 HTTP/1.1\r\nHost: example.invalid:443\r\n\r\n",
                 b"FOO /u/anthropic/v1/messages HTTP/1.1\r\nHost: x\r\nx-api-key: " + token.encode() + b"\r\n\r\n"):
        reply = raw_exchange(h.port, line)
        assert reply.startswith(b"HTTP/1.1 501") and b"motel_bad_request" in reply
    assert upstream.requests == []
