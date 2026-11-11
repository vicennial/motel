"""Strict JSON: duplicate keys, non-finite numbers and the forwarded body."""
import json

import pytest
from concierge_fakes import BODY, MODEL, FakeUpstream, Harness, error_kind

URL = "/u/anthropic/v1/messages"


@pytest.fixture
def setup():
    up = FakeUpstream()
    h = Harness(up.url)
    yield h, up
    h.close()
    up.close()


def test_duplicate_keys_are_refused_at_any_depth(setup):
    """The review's repro: max_tokens 200000 then 1. Python read the last, another parser may read the first."""
    h, up = setup
    token = h.issue(budget=1.0)
    raw = b'{"model":"' + MODEL.encode() + b'","max_tokens":200000,"messages":[{"role":"user","content":"hi"}],"max_tokens":1}'
    status, _, reply = h.post(URL, token, raw_body=raw)
    assert status == 400 and error_kind(reply) == "motel_bad_request" and "max_tokens" in json.loads(reply)["error"]["message"]
    nested = b'{"model":"m","max_tokens":5,"metadata":{"a":{"b":1,"b":2}}}'
    assert h.post(URL, token, raw_body=nested)[0] == 400
    escaped = b'{"model":"m","max_tokens":5,"max\\u005ftokens":200000}'
    assert h.post(URL, token, raw_body=escaped)[0] == 400
    assert h.post("/u/anthropic/v1/messages/count_tokens", token, raw_body=raw)[0] == 400
    assert up.requests == [] and h.registry.usage("a-1")["refused"] == 4


def test_non_finite_numbers_are_refused(setup):
    h, up = setup
    for raw in (b'{"model":"m","max_tokens":5,"x":NaN}', b'{"model":"m","max_tokens":5,"x":Infinity}', b'{"model":"m","max_tokens":1e999}'):
        assert h.post(URL, h.issue("a-" + str(len(raw))), raw_body=raw)[0] == 400
    assert up.requests == []


def test_the_forwarded_body_is_the_parsed_body_with_a_matching_length(setup):
    h, up = setup
    token = h.issue(budget=5.0)
    raw = ('{ "model" : "' + MODEL + '",\n "max_tokens" : 100 , "messages":[{"role":"user","content":"h\\u00e9llo \\ud83d\\ude00"}] }').encode()
    assert h.post(URL, token, raw_body=raw)[0] == 200
    sent = up.requests[0]
    assert sent["body"] == json.dumps(json.loads(raw), separators=(",", ":"), ensure_ascii=False).encode()
    assert up.header(0, "content-length") == [str(len(sent["body"]))]
    # a UTF-16 body is accepted by json.loads on bytes; it must reach the upstream as UTF-8
    utf16 = json.dumps(BODY).encode("utf-16")
    assert h.post(URL, token, raw_body=utf16)[0] == 200
    assert json.loads(up.requests[1]["body"]) == BODY
    # a lone surrogate cannot be UTF-8: it is sent escaped, not mangled
    lone = b'{"model":"m","max_tokens":5,"messages":[{"role":"user","content":"\\ud800"}]}'
    assert h.post(URL, token, raw_body=lone)[0] == 200 and b"\\ud800" in up.requests[2]["body"]


def test_count_tokens_with_a_json_body_is_also_canonicalised(setup):
    h, up = setup
    h.post("/u/anthropic/v1/messages/count_tokens", h.issue(), raw_body=b'{ "model" : "m" }')
    assert up.requests[0]["body"] == b'{"model":"m"}'




def test_case_folded_duplicates_are_refused_in_the_top_level_request_fields_only(setup):
    """N7. A case-insensitive parser could read ``MAX_TOKENS`` as ``max_tokens``; nested objects hold arbitrary names."""
    h, up = setup
    token = h.issue(budget=5.0, max_concurrent=4)
    for raw in (b'{"model":"m","max_tokens":5,"MAX_TOKENS":200000}', b'{"model":"m","max_tokens":5,"Max_Tokens":1}',
                b'{"model":"m","max_tokens":5,"MODEL":"claude-fable-5-1"}'):
        status, _, reply = h.post(URL, token, raw_body=raw)
        assert status == 400 and error_kind(reply) == "motel_bad_request"
    assert up.requests == []
    nested = (b'{"model":"' + MODEL.encode() + b'","max_tokens":5,"messages":[{"role":"user","content":"hi"}],'
              b'"tools":[{"name":"t","input_schema":{"type":"object","properties":{"id":{"type":"string"},"ID":{"type":"string"}}}}]}')
    assert h.post(URL, token, raw_body=nested)[0] == 200
    assert json.loads(up.requests[0]["body"])["tools"][0]["input_schema"]["properties"].keys() == {"id", "ID"}
    assert h.post(URL, token, raw_body=b'{"model":"m","max_tokens":5,"metadata":{"a":1,"a":2}}')[0] == 400  # exact repeat, nested


@pytest.mark.parametrize("raw", [b"[1]", b'"text"', b"5", b"null", b"not json", b'{"model":"m",', b"\xff\xfe", b'{"x":NaN}',
                                 b'{"x":' + b"[" * 3000 + b"]" * 3000 + b"}", b'{"a":1,"a":2}', b"  "])
def test_count_tokens_refuses_a_body_that_is_not_a_strict_json_object(setup, raw):
    """N5. count_tokens is not metered, but it is not a way to send bytes past the policy either."""
    h, up = setup
    status, _, reply = h.post("/u/anthropic/v1/messages/count_tokens", h.issue(), raw_body=raw)
    assert up.requests == []
    if raw.count(b"[") > 1000:  # a parser that copes with deep nesting hands it to the depth check, which refuses it too
        assert status in (400, 403)
    else:
        assert status == 400 and error_kind(reply) == "motel_bad_request"


def test_count_tokens_with_an_empty_body_is_still_forwarded(setup):
    h, up = setup
    assert h.post("/u/anthropic/v1/messages/count_tokens", h.issue(), raw_body=b"")[0] == 200 and up.requests[0]["body"] == b""


def test_count_tokens_runs_the_policy_scan_too(setup):
    h, up = setup
    url_block = {"role": "user", "content": [{"type": "image", "source": {"type": "url", "url": "https://example.invalid/a.png"}}]}
    status, _, reply = h.post("/u/anthropic/v1/messages/count_tokens", h.issue(), body={"model": "m", "messages": [url_block]})
    assert status == 403 and error_kind(reply) == "motel_feature_denied" and up.requests == []


# -- N6: node cap -------------------------------------------------------------------------------------------------------

def _doc(n_nodes_body: bytes) -> bytes:
    return b'{"model":"' + MODEL.encode() + b'","max_tokens":5,"messages":[{"role":"user","content":"hi"}],"metadata":' + n_nodes_body + b"}"


HOSTILE = {
    "empty arrays": b"[" + b"[]," * 300_000 + b"[]]",
    "empty objects": b"[" + b"{}," * 300_000 + b"{}]",
    "short numbers": b"[" + b"1," * 600_000 + b"1]",
    "short strings": b"[" + b'"a",' * 400_000 + b'"a"]',
    "nulls": b"[" + b"null," * 300_000 + b"null]",
    "keys": b"{" + b",".join(b'"k%d":1' % i for i in range(150_000)) + b"}",
}


@pytest.mark.parametrize("name", sorted(HOSTILE))
def test_a_body_with_too_many_json_nodes_is_refused_before_it_is_parsed(setup, name, monkeypatch):
    """N6: a few megabytes of tiny values would become hundreds of megabytes of Python objects."""
    from motel.concierge import proxy
    h, up = setup
    parsed = []
    real = proxy.parse_json
    monkeypatch.setattr(proxy, "parse_json", lambda *a: (parsed.append(1), real(*a))[1])
    status, _, reply = h.post(URL, h.issue(), raw_body=_doc(HOSTILE[name]))
    assert status == 413 and error_kind(reply) == "motel_request_too_large" and "keys and values" in json.loads(reply)["error"]["message"]
    assert up.requests == [] and parsed == []


def test_the_node_cap_also_covers_utf16_and_a_nan_tail_and_count_tokens(setup):
    h, up = setup
    token = h.issue(max_concurrent=4)
    utf16 = _doc(HOSTILE["empty arrays"]).decode().encode("utf-16")
    assert h.post(URL, token, raw_body=utf16)[0] == 413
    assert h.post(URL, token, raw_body=_doc(b"[" + b"[]," * 300_000 + b"NaN]"))[0] == 413  # counted past a constant that is refused later
    assert h.post("/u/anthropic/v1/messages/count_tokens", token, raw_body=_doc(HOSTILE["nulls"]))[0] == 413
    assert up.requests == []


def test_the_cap_is_configurable_and_an_ordinary_large_request_passes():
    up = FakeUpstream()
    h = Harness(up.url, tour={"concierge_max_json_nodes": 100})
    try:
        token = h.issue(budget=5.0, max_concurrent=4)
        few = _doc(b"[" + b",".join(b"1" for _ in range(60)) + b"]")
        many = _doc(b"[" + b",".join(b"1" for _ in range(200)) + b"]")
        assert h.post(URL, token, raw_body=few)[0] == 200 and h.post(URL, token, raw_body=many)[0] == 413
    finally:
        h.close()
        up.close()
    up = FakeUpstream()
    h = Harness(up.url)  # the default: 200000
    try:
        # 1 MB of ordinary text with many commas and quotes inside strings is one node, not hundreds of thousands
        text = ('he said, "yes", and left, ' * 40_000).replace('"', '\\"')
        body = {"model": MODEL, "max_tokens": 5, "messages": [{"role": "user", "content": json.loads('"' + text + '"')}]}
        status, _, _ = h.post(URL, h.issue(budget=5.0), body=body)
        assert status == 200
        assert h.cfg.concierge_max_json_nodes == 200_000
    finally:
        h.close()
        up.close()


def test_the_node_counter_agrees_with_the_parser_on_valid_json():
    from motel.concierge import proxy
    docs = ['{"a":[1,2.5,-3e2,true,false,null,"x\\"y",{"b":[]}],"c":{}}', "[]", "{}", '"s"', "7", " [ 1 , 2 ] ",
            '{"k":"v,with:punct]}"}', '[[[[1]]]]']
    def count(v):
        if isinstance(v, dict):
            return 1 + sum(1 + count(x) for x in v.values())  # the object, then each key and its value
        if isinstance(v, list):
            return 1 + sum(count(x) for x in v)
        return 1

    for text in docs:
        n = count(json.loads(text))
        assert not proxy.too_many_nodes(text, n) and proxy.too_many_nodes(text, n - 1), text


def test_the_buffered_budget_must_hold_one_worst_case_request_nodes_included():
    from motel.concierge.errors import ConfigError
    from motel.concierge.settings import BODY_CHARGE_FACTOR, NODE_BYTES, parse_tours_section, worst_case_charge
    assert BODY_CHARGE_FACTOR == 14 and NODE_BYTES == 200
    assert parse_tours_section({}).max_buffered_bytes == 192 * 1024 * 1024 >= worst_case_charge(8 * 1024 * 1024, 200_000)
    # 8 MiB x 14 = 112 MiB, plus 200 x 200000 = 38.1 MiB: 150.1 MiB
    with pytest.raises(ConfigError, match="at least 150.1 MiB"):
        parse_tours_section({"concierge_max_request_mb": 8, "concierge_max_buffered_mb": 150})
    assert parse_tours_section({"concierge_max_request_mb": 8, "concierge_max_buffered_mb": 151}).concierge_max_json_nodes == 200_000
    with pytest.raises(ConfigError, match="at least"):  # more nodes allowed, more memory needed
        parse_tours_section({"concierge_max_request_mb": 8, "concierge_max_buffered_mb": 151, "concierge_max_json_nodes": 500_000})
    with pytest.raises(ConfigError):
        parse_tours_section({"concierge_max_json_nodes": 5})
