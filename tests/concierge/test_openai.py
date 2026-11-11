"""OpenAI-format bindings: bearer auth, optional output caps, cached-token pricing, streamed chat usage."""
import json

import pytest
from concierge_fakes import FakeUpstream, Harness, error_kind

OPENAI = {"auth": "bearer", "paths": ["/v1/chat/completions", "/v1/responses", "/v1/responses/compact", "/v1/models/*"], "methods": ["POST", "GET"]}


@pytest.fixture
def setup():
    up = FakeUpstream()
    h = Harness(up.url, binding=OPENAI)
    yield h, up
    h.close()
    up.close()


def reply_with(up, obj, ctype="application/json"):
    up.reply = lambda req, handler: (200, {"Content-Type": ctype}, [obj if isinstance(obj, bytes) else json.dumps(obj).encode()])


def test_chat_usage_with_cached_tokens_is_priced_at_the_cached_rate(setup):
    h, up = setup
    reply_with(up, {"model": "gpt-4o-2024-08-06", "usage": {"prompt_tokens": 1000, "completion_tokens": 200,
                                                           "prompt_tokens_details": {"cached_tokens": 600}}})
    status, _, _ = h.post("/u/anthropic/v1/chat/completions", h.issue(), body={"model": "gpt-4o", "max_tokens": 500, "messages": []}, auth="bearer")
    assert status == 200 and up.header(0, "authorization")[0].startswith("Bearer sk-test-real")
    assert h.registry.usage("a-1")["usd"] == pytest.approx((400 * 2.5 + 600 * 1.25 + 200 * 10) / 1e6, abs=1e-9)
    assert h.registry.usage("a-1")["input_tokens"] == 1000


def test_a_request_without_an_output_cap_reserves_the_binding_default(setup):
    h, up = setup
    reply_with(up, {"model": "gpt-5", "usage": {"input_tokens": 10, "output_tokens": 10}})
    token = h.issue(budget=5.0)
    body = {"model": "gpt-5", "input": "hi", "store": False}
    status, _, _ = h.post("/u/anthropic/v1/responses", token, body=body, auth="bearer")
    assert status == 200
    reserve = h.events("reserve")[0]["usd"]
    assert reserve == pytest.approx((len(json.dumps(body)) // 3 + 1) * 1.25 / 1e6 + 32768 * 10 / 1e6, rel=0.05)


# -- review F2: a Responses request is kept upstream unless it says store: false --------------------------------------------------

@pytest.mark.parametrize("path", ["/v1/responses", "/v1/responses/compact"])
@pytest.mark.parametrize("store", [{}, {"store": True}, {"store": None}, {"store": "false"}, {"store": 0}])
def test_a_responses_request_that_may_be_stored_is_refused_as_server_state(setup, path, store):
    h, up = setup
    reply_with(up, {"model": "gpt-5", "usage": {"input_tokens": 1, "output_tokens": 1}})
    status, _, reply = h.post("/u/anthropic" + path, h.issue(budget=5.0), body={"model": "gpt-5", "input": "hi", **store}, auth="bearer")
    assert status == 403 and error_kind(reply) == "motel_feature_denied" and "server_state" in json.loads(reply)["error"]["message"]
    assert not up.requests


def test_store_false_is_served_and_a_binding_may_allow_server_state():
    up = FakeUpstream()
    reply_with(up, {"model": "gpt-5", "usage": {"input_tokens": 1, "output_tokens": 1}})
    try:
        for allow, store, want in (([], False, 200), (["server_state"], True, 200), (["server_state"], None, 200)):
            h = Harness(up.url, binding={**OPENAI, "allow": allow})
            try:
                body = {"model": "gpt-5", "input": "hi"} | ({} if store is None else {"store": store})
                assert h.post("/u/anthropic/v1/responses", h.issue(budget=5.0), body=body, auth="bearer")[0] == want
            finally:
                h.close()
    finally:
        up.close()


def test_store_is_not_required_off_the_responses_api(setup):
    h, up = setup
    reply_with(up, {"model": "gpt-4o", "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    token = h.issue(budget=5.0)
    assert h.post("/u/anthropic/v1/chat/completions", token, body={"model": "gpt-4o", "max_tokens": 5, "messages": []}, auth="bearer")[0] == 200


@pytest.mark.parametrize("item", [{"type": "item_reference", "id": "msg_1"}, {"id": "msg_1"}, {"type": "reasoning", "id": "rs_1"},
                                  {"type": "item_reference"}])
def test_an_input_item_that_only_names_a_stored_item_is_refused_even_with_store_false(setup, item):
    h, up = setup
    reply_with(up, {"model": "gpt-5", "usage": {"input_tokens": 1, "output_tokens": 1}})
    body = {"model": "gpt-5", "store": False, "input": [{"role": "user", "content": "hi"}, item]}
    status, _, reply = h.post("/u/anthropic/v1/responses", h.issue(budget=5.0), body=body, auth="bearer")
    assert status == 403 and "server_state" in json.loads(reply)["error"]["message"]


def test_items_that_carry_their_content_are_not_stored_references(setup):
    h, up = setup
    reply_with(up, {"model": "gpt-5", "usage": {"input_tokens": 1, "output_tokens": 1}})
    items = [{"role": "user", "content": "hi"}, {"type": "message", "id": "m1", "role": "assistant", "content": []},
             {"type": "reasoning", "id": "rs_1", "summary": [], "encrypted_content": "x"},
             {"type": "function_call", "id": "fc1", "call_id": "c", "name": "n", "arguments": "{}"}]
    body = {"model": "gpt-5", "store": False, "input": items}
    assert h.post("/u/anthropic/v1/responses", h.issue(budget=5.0), body=body, auth="bearer")[0] == 200


def test_n_multiplies_the_reserved_output(setup):
    h, up = setup
    reply_with(up, {"model": "gpt-4o", "usage": {"prompt_tokens": 1, "completion_tokens": 1}})
    token = h.issue(budget=5.0)
    h.post("/u/anthropic/v1/chat/completions", token, body={"model": "gpt-4o", "max_tokens": 1000, "n": 1}, auth="bearer")
    h.post("/u/anthropic/v1/chat/completions", token, body={"model": "gpt-4o", "max_tokens": 1000, "n": 4}, auth="bearer")
    first, second = (e["usd"] for e in h.events("reserve"))
    assert second == pytest.approx(first * 4, rel=0.02)


def test_streamed_chat_gets_include_usage_injected_so_it_can_be_metered(setup):
    h, up = setup
    reply_with(up, b'data: {"choices":[{"delta":{"content":"a"}}]}\n\ndata: {"model":"gpt-4o","choices":[],"usage":{"prompt_tokens":10,"completion_tokens":4}}\n\ndata: [DONE]\n\n',
               "text/event-stream")
    status, _, _ = h.post("/u/anthropic/v1/chat/completions", h.issue(), auth="bearer",
                          body={"model": "gpt-4o", "max_tokens": 50, "stream": True, "stream_options": {"x": 1}})
    assert status == 200
    sent = json.loads(up.requests[0]["body"])
    assert sent["stream_options"] == {"x": 1, "include_usage": True}
    assert h.registry.usage("a-1")["usd"] == pytest.approx((10 * 2.5 + 4 * 10) / 1e6, abs=1e-9)


def test_get_with_a_wildcard_path_is_not_metered(setup):
    h, up = setup
    reply_with(up, {"id": "gpt-4o"})
    token = h.issue(budget=0.0001)
    assert h.post("/u/anthropic/v1/models/gpt-4o", token, method="GET", body=None, auth="bearer")[0] == 200
    assert up.requests[0]["method"] == "GET" and up.requests[0]["body"] == b""
    assert error_kind(h.post("/u/anthropic/v1/models", token, method="GET", body=None, auth="bearer")[2]) == "motel_path_denied"
    assert error_kind(h.post("/u/anthropic/v1/models/x", token, method="GET", body=b"{}".decode(), auth="bearer")[2]) == "motel_bad_request"


def test_long_prompts_are_priced_at_the_long_context_premium():
    up = FakeUpstream()
    h = Harness(up.url)
    try:
        up.reply = lambda req, handler: (200, {"Content-Type": "application/json"}, [json.dumps(
            {"model": "claude-sonnet-4-6", "usage": {"input_tokens": 300_000, "output_tokens": 1000}}).encode()])
        h.post("/u/anthropic/v1/messages", h.issue(budget=5.0))
        assert h.registry.usage("a-1")["usd"] == pytest.approx((300_000 * 6 + 1000 * 22.5) / 1e6, abs=1e-6)
    finally:
        h.close()
        up.close()


def test_header_injection_attempts_are_refused(setup):
    h, up = setup
    from concierge_fakes import raw_exchange
    token = h.issue()
    for evil in (f"x-api-key: {token}\r\nX-Evil: a\r\n b", f"x-api-key: {token}\r\nBad Name: x", f"x-api-key: {token}\r\nX-A:\x00b"):
        data = f"POST /u/anthropic/v1/chat/completions HTTP/1.1\r\nHost: x\r\n{evil}\r\nContent-Length: 2\r\n\r\n{{}}".encode()
        status_line = raw_exchange(h.port, data).split(b"\r\n")[0]
        assert status_line.split()[1].startswith(b"4"), status_line  # refused, however the parser got there
    assert up.requests == []
