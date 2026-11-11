"""Price table, cost arithmetic, usage parsing (JSON and SSE) and the settlement rule."""
import json
import random

import pytest

from motel.concierge import pricing, redact, usage
from motel.concierge.proxy import settle_amount
from motel.concierge.settings import parse_tours_section

BINDING = parse_tours_section({"bindings": {"a": {"upstream": "https://x.example", "auth": "x-api-key", "key_file": "/k", "paths": ["/p"]}}}).bindings["a"]


def test_longest_prefix_wins_and_dated_ids_match():
    price, known = pricing.lookup("claude-sonnet-4-6-20260101")
    assert known and price.input == 3 and price.output == 15
    assert pricing.lookup("gpt-4o-mini-2024-07-18")[0].input == 0.15  # not the gpt-4o price
    assert pricing.lookup("gpt-4o")[0].input == 2.5
    assert pricing.lookup("CLAUDE-HAIKU-4-5")[0].output == 5


def test_the_claude_five_family_is_priced_from_the_source_table():
    expected = {"claude-fable-5-1": (10, 50, 0.25), "claude-mythos-5-1": (10, 50, 0.25), "claude-fable-5": (10, 50, 1.0),
                "claude-opus-5-5": (4, 20, 0.2), "claude-opus-5": (5, 25, 0.5), "claude-opus-4-8": (5, 25, 0.5),
                "claude-sonnet-5-5": (2, 10, 0.2), "claude-sonnet-5": (2, 10, 0.2), "claude-sonnet-4-6": (3, 15, 0.3),
                "claude-haiku-5-5": (0.10, 0.50, 0.01), "claude-haiku-4-5": (1, 5, 0.1)}
    for model, (inp, out, cached) in expected.items():
        price, known = pricing.lookup(model)
        assert known and (price.input, price.output) == (inp, out) and price.cache_read == pytest.approx(cached), model
        assert price.cache_write_5m == pytest.approx(inp * 1.25) and price.cache_write_1h == pytest.approx(inp * 2)
    assert pricing.PRICING_AS_OF == "2026-10-06"


def test_models_the_source_does_not_price_are_not_guessed():
    for model in ("claude-opus-4-5", "claude-sonnet-4-5", "claude-opus-4-1", "claude-3-5-haiku-20241022", "claude-opus-5-6",
                  "claude-opus-5-5-preview", "claude-sonnet-5-50"):
        price, known = pricing.lookup(model)
        assert not known and price == pricing.DEFAULT_PRICE, model
    assert pricing.lookup("claude-opus-5-5-20261101")[1] and pricing.lookup("claude-fable-5-1[1m]")[1]
    assert pricing.lookup("claude-opus-5")[0].output == 25  # a boundary-checked match, not a bare prefix


def test_long_context_rules():
    haiku = pricing.lookup("claude-haiku-5-5")[0]
    assert pricing.cost_usd(haiku, input_tokens=100_000, output_tokens=1000) == pytest.approx((100_000 * 0.10 + 1000 * 0.50) / 1e6)
    assert pricing.cost_usd(haiku, input_tokens=100_001, output_tokens=1000) == pytest.approx((100_001 * 0.50 + 1000 * 2.50) / 1e6)
    assert pricing.cost_usd(haiku, input_tokens=10, cache_read=200_000) == pytest.approx((10 * 0.5 + 200_000 * 0.05) / 1e6)  # cache counts
    opus48 = pricing.lookup("claude-opus-4-8")[0]
    assert pricing.cost_usd(opus48, input_tokens=900_000, output_tokens=1000) == pytest.approx((900_000 * 5 + 1000 * 25) / 1e6)  # none
    sonnet = pricing.lookup("claude-sonnet-5-5")[0]  # the source says nothing: the cautious assumption applies
    assert pricing.cost_usd(sonnet, input_tokens=300_000) == pytest.approx(300_000 * 4 / 1e6)
    assert pricing.cost_usd(sonnet, input_tokens=200_000) == pytest.approx(200_000 * 2 / 1e6)
    assert pricing.cost_usd(pricing.Price.make(1, 2), input_tokens=900_000) == pytest.approx(0.9)  # operator prices carry no rule


def test_unknown_or_missing_models_get_the_high_default():
    for model in ("brand-new-model", "", None):
        price, known = pricing.lookup(model)
        assert not known and price == pricing.DEFAULT_PRICE
    assert pricing.DEFAULT_PRICE.output >= max(p.output for p in pricing.BUILTIN_PRICES.values())
    assert pricing.DEFAULT_PRICE.input >= max(p.input for p in pricing.BUILTIN_PRICES.values())
    assert pricing.DEFAULT_PRICE.cache_read == pricing.DEFAULT_PRICE.input  # no cache discount assumed


def test_operator_overrides_beat_the_builtin_table():
    mine = {"claude-sonnet": pricing.Price.make(9, 99)}
    assert pricing.lookup("claude-sonnet-4-6", mine)[0].output == 99
    assert pricing.lookup("gpt-5", mine)[0].output == 10


def test_cost_arithmetic():
    price = pricing.Price.make(3, 15, cache_read=0.3, cache_write_5m=3.75, cache_write_1h=6)
    got = pricing.cost_usd(price, input_tokens=1_000_000, output_tokens=1_000_000, cache_read=1_000_000, cache_write_5m=1_000_000,
                           cache_write_1h=1_000_000, extra_usd=0.5)
    assert got == pytest.approx(3 + 15 + 0.3 + 3.75 + 6 + 0.5)


def test_json_usage_anthropic_and_openai_shapes():
    anth = usage.from_json_body("anthropic", json.dumps({"model": "m", "usage": {
        "input_tokens": 10, "output_tokens": 20, "cache_read_input_tokens": 5, "cache_creation_input_tokens": 7,
        "server_tool_use": {"web_search_requests": 2}}}).encode())
    assert (anth.input_tokens, anth.output_tokens, anth.cache_read, anth.cache_write_5m, anth.final) == (10, 20, 5, 7, True)
    assert anth.extra_usd == pytest.approx(0.02) and anth.total_input == 22
    split = usage.from_json_body("anthropic", json.dumps({"usage": {"input_tokens": 1, "output_tokens": 1, "cache_creation_input_tokens": 9,
                                                                   "cache_creation": {"ephemeral_5m_input_tokens": 4, "ephemeral_1h_input_tokens": 5}}}).encode())
    assert (split.cache_write_5m, split.cache_write_1h) == (4, 5)
    chat = usage.from_json_body("openai", json.dumps({"model": "gpt-4o", "usage": {
        "prompt_tokens": 100, "completion_tokens": 30, "prompt_tokens_details": {"cached_tokens": 40}}}).encode())
    assert (chat.input_tokens, chat.cache_read, chat.output_tokens) == (60, 40, 30)  # cached tokens are carved out of the prompt
    resp = usage.from_json_body("openai", json.dumps({"usage": {"input_tokens": 50, "output_tokens": 9,
                                                                "input_tokens_details": {"cached_tokens": 999}}}).encode())
    assert (resp.input_tokens, resp.cache_read) == (0, 50)  # cached cannot exceed the prompt


@pytest.mark.parametrize("body", [b"", b"not json", b"[]", b"null", b'{"usage": 5}', b'{"usage": {"input_tokens": "x"}}',
                                  b'{"usage": {"input_tokens": -4, "output_tokens": 1e400}}', b"{" * 100000])
def test_garbage_never_raises(body):
    got = usage.from_json_body("anthropic", body)
    assert got is None or (got.input_tokens >= 0 and got.output_tokens >= 0)
    parser = usage.SseUsage("openai")
    parser.feed(b"data: " + body + b"\n\n")


def test_sse_anthropic_split_at_every_byte_boundary():
    events = (b'event: message_start\ndata: {"type":"message_start","message":{"model":"claude-sonnet-4-6","usage":{"input_tokens":25,'
              b'"cache_read_input_tokens":3,"output_tokens":1}}}\n\n'
              b'event: content_block_delta\ndata: {"type":"content_block_delta","delta":{"text":"hi"}}\n\n'
              b'event: message_delta\r\ndata: {"type":"message_delta","usage":{"output_tokens":77}}\r\n\r\n'
              b'event: message_stop\ndata: {"type":"message_stop"}\n\n')
    for cut in range(1, len(events)):
        parser = usage.SseUsage("anthropic")
        parser.feed(events[:cut])
        parser.feed(events[cut:])
        got = parser.usage
        assert (got.input_tokens, got.cache_read, got.output_tokens, got.final, got.model) == (25, 3, 77, True, "claude-sonnet-4-6"), cut


def test_sse_message_delta_without_input_keeps_the_start_values_and_a_delta_with_input_replaces_them():
    parser = usage.SseUsage("anthropic")
    parser.feed(b'data: {"type":"message_start","message":{"usage":{"input_tokens":25,"output_tokens":1}}}\n'
                b'data: {"type":"message_delta","usage":{"output_tokens":9}}\n')
    assert (parser.usage.input_tokens, parser.usage.output_tokens) == (25, 9)
    parser.feed(b'data: {"type":"message_delta","usage":{"input_tokens":30,"output_tokens":12}}\n')
    assert (parser.usage.input_tokens, parser.usage.output_tokens) == (30, 12)


def test_sse_openai_chat_and_responses_streams():
    chat = usage.SseUsage("openai")
    chat.feed(b'data: {"choices":[{"delta":{"content":"a"}}],"usage":null}\n\ndata: {"model":"gpt-4o","choices":[],'
              b'"usage":{"prompt_tokens":10,"completion_tokens":4}}\n\ndata: [DONE]\n\n')
    assert (chat.usage.input_tokens, chat.usage.output_tokens, chat.usage.final, chat.usage.model) == (10, 4, True, "gpt-4o")
    resp = usage.SseUsage("openai")
    resp.feed(b'event: response.completed\ndata: {"type":"response.completed","response":{"model":"gpt-5","usage":{"input_tokens":7,"output_tokens":3}}}\n\n')
    assert (resp.usage.input_tokens, resp.usage.output_tokens, resp.usage.model) == (7, 3, "gpt-5")


def test_an_overlong_sse_line_is_dropped_without_killing_the_parser():
    parser = usage.SseUsage("anthropic")
    parser.feed(b"data: " + b"x" * (usage.MAX_LINE + 10))
    parser.feed(b"yyyy\n" + b'data: {"type":"message_delta","usage":{"output_tokens":5}}\n')
    assert parser.dropped_lines == 1 and parser.usage.output_tokens == 5


def test_settlement_rules():
    complete = usage.Usage(input_tokens=1000, output_tokens=100, model="claude-sonnet-4-6", saw_input=True, final=True)
    usd, outcome, unknown = settle_amount(binding=BINDING, req_model="claude-sonnet-4-6", got=complete, status=200, reserved=0.5, max_out_total=4096)
    assert (outcome, unknown) == ("ok", False) and usd == pytest.approx((3000 + 1500) / 1e6)
    partial = usage.Usage(input_tokens=1000, model="claude-sonnet-4-6", saw_input=True)
    usd, outcome, _ = settle_amount(binding=BINDING, req_model="claude-sonnet-4-6", got=partial, status=200, reserved=0.5, max_out_total=200)
    assert outcome == "partial" and usd == pytest.approx((3000 + 200 * 15) / 1e6)
    assert settle_amount(binding=BINDING, req_model="m", got=None, status=200, reserved=0.5, max_out_total=1) == (0.5, "unmetered", True)
    assert settle_amount(binding=BINDING, req_model="m", got=complete, status=500, reserved=0.5, max_out_total=1)[:2] == (0.0, "upstream_error")
    assert settle_amount(binding=BINDING, req_model="m", got=None, status=302, reserved=0.5, max_out_total=1)[:2] == (0.0, "redirect")


def test_the_dearer_of_the_request_and_response_model_is_charged():
    got = usage.Usage(input_tokens=1000, output_tokens=1000, model="claude-fable-5-1", saw_input=True, final=True)
    usd, _, _ = settle_amount(binding=BINDING, req_model="claude-haiku-4-5", got=got, status=200, reserved=1, max_out_total=1)
    assert usd == pytest.approx((1000 * 10 + 1000 * 50) / 1e6)


def test_redactor_catches_the_secret_however_the_stream_is_cut():
    secret = "sk-test-real-0123456789abcdefghij"
    text = f'prefix {secret} middle "{secret}" end {secret[:10]} tail'.encode()
    rng = random.Random(7)
    for _ in range(300):
        cuts = sorted(rng.sample(range(1, len(text)), rng.randint(1, 6)))
        parts = [text[a:b] for a, b in zip([0, *cuts], [*cuts, len(text)], strict=True)]
        red = redact.StreamRedactor(secret)
        out = b"".join(red.feed(p) for p in parts) + red.finish()
        assert secret.encode() not in out
        assert out == f'prefix [redacted] middle "[redacted]" end {secret[:10]} tail'.encode()
        assert red.hits == 2


def test_redactor_holds_back_only_a_real_key_prefix():
    red = redact.StreamRedactor("sk-test-real-0123456789abcdefghij")
    assert red.feed(b"data: hello world\n\n") == b"data: hello world\n\n"  # nothing held, so streams are not delayed
    assert red.feed(b"x sk-te") == b"x "
    assert red.finish() == b"sk-te"
    assert red.scrub_text("Bearer sk-test-real-0123456789abcdefghij") == "Bearer [redacted]"
