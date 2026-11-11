"""Request policy (deny by default), strict JSON and the forwarded body."""
import base64
import json
import os

import pytest
from concierge_fakes import BODY, MODEL, FakeUpstream, Harness, error_kind

from motel.concierge import policy
from motel.concierge.errors import ConfigError
from motel.concierge.settings import parse_tours_section

URL = "/u/anthropic/v1/messages"


@pytest.fixture
def setup():
    up = FakeUpstream()
    h = Harness(up.url)
    yield h, up
    h.close()
    up.close()


def msg(**extra):
    return {**BODY, **extra}


def content(*blocks):
    return msg(messages=[{"role": "user", "content": list(blocks)}])


def make_pdf(pages: int, extra: bytes = b"") -> bytes:
    """A tiny PDF with ``pages`` page objects (the shape of the review's 600-page repro)."""
    kids = b" ".join(b"%d 0 R" % (3 + i) for i in range(pages))
    out = b"%PDF-1.4\n1 0 obj\n<< /Type /Catalog /Pages 2 0 R >>\nendobj\n"
    out += b"2 0 obj\n<< /Type /Pages /Kids [" + kids + b"] /Count %d >>\nendobj\n" % pages
    for i in range(pages):
        out += b"%d 0 obj\n<< /Type /Page /Parent 2 0 R /MediaBox [0 0 1 1] >>\nendobj\n" % (3 + i)
    return out + extra + b"trailer\n<< /Root 1 0 R >>\n%%EOF\n"


def b64(raw: bytes) -> str:
    return base64.b64encode(raw).decode()


def pdf_block(raw: bytes, media="application/pdf"):
    return {"type": "document", "source": {"type": "base64", "media_type": media, "data": b64(raw)}}


ONE_PAGE = make_pdf(1)

DENIED = {
    "server_state": [msg(thread={"id": "th_1"}), msg(container="cont_1"), msg(previous_response_id="resp_1"),
                     msg(conversation="conv_1"), msg(prompt={"id": "pmpt_1"}), msg(background=True),
                     content({"type": "document", "source": {"type": "file", "file_id": "file_1"}}),
                     content({"type": "input_file", "file_id": "file_1"}), content({"type": "container_upload", "file_id": "f"})],
    "url_sources": [content({"type": "document", "source": {"type": "url", "url": "https://example.invalid/a.pdf"}}),
                    content({"type": "image", "source": {"type": "url", "url": "https://example.invalid/a.png"}}),
                    content({"type": "image_url", "image_url": {"url": "https://example.invalid/a.png"}}),
                    content({"type": "input_image", "image_url": "https://example.invalid/a.png"}),
                    content({"type": "input_file", "file_url": "https://example.invalid/a.pdf"})],
    "pdf": [content(pdf_block(ONE_PAGE)), content(pdf_block(b"anything", "text/plain")),
            content({"type": "input_file", "filename": "a.pdf", "file_data": "data:application/pdf;base64," + b64(ONE_PAGE)}),
            content({"type": "file", "file": {"filename": "a.pdf", "file_data": "data:application/pdf;base64," + b64(ONE_PAGE)}})],
    "mcp": [msg(mcp_servers=[{"type": "url", "url": "https://mcp.example.invalid", "name": "x"}]),
            msg(tools=[{"type": "mcp_toolset", "mcp_server_name": "x"}]), msg(tools=[{"type": "mcp", "server_label": "x"}])],
    "web_search": [msg(tools=[{"type": "web_search_20250305", "name": "web_search"}]), msg(tools=[{"type": "web_search_preview"}])],
    "web_fetch": [msg(tools=[{"type": "web_fetch_20250910", "name": "web_fetch"}])],
    "code_execution": [msg(tools=[{"type": "code_execution_20250522", "name": "code_execution"}]),
                       msg(tools=[{"type": "code_interpreter", "container": {"type": "auto"}}])],
    "file_search": [msg(tools=[{"type": "file_search", "vector_store_ids": ["vs_1"]}])],
    "image_generation": [msg(tools=[{"type": "image_generation"}])],
    "fast_mode": [msg(speed="fast"), msg(service_tier="priority")],
    "fallbacks": [msg(fallbacks="default"), msg(fallbacks=[{"model": "claude-opus-4-8"}])],
}


@pytest.mark.parametrize("feature,body", [(f, b) for f, bodies in DENIED.items() for b in bodies])
def test_unlisted_features_are_refused_before_dispatch_and_allowed_when_listed(feature, body):
    up = FakeUpstream()
    h = Harness(up.url)
    try:
        token = h.issue(budget=5.0)
        status, _, reply = h.post(URL, token, body=body)
        assert status == 403 and error_kind(reply) == "motel_feature_denied" and feature in json.loads(reply)["error"]["message"]
        assert up.requests == [] and h.registry.usage("a-1")["refused"] == 1
    finally:
        h.close()
        up.close()
    up = FakeUpstream()
    h = Harness(up.url, binding={"allow": [feature]})
    try:
        assert h.post(URL, h.issue(budget=5.0), body=body)[0] == 200
        assert len(up.requests) == 1
    finally:
        h.close()
        up.close()


def test_an_allowed_feature_does_not_unlock_another(setup):
    up = FakeUpstream()
    h = Harness(up.url, binding={"allow": ["web_search"]})
    try:
        body = msg(tools=[{"type": "web_search_20250305", "name": "web_search"}], thread="t")
        assert error_kind(h.post(URL, h.issue(), body=body)[2]) == "motel_feature_denied"
    finally:
        h.close()
        up.close()


def test_plain_requests_and_client_tools_are_untouched(setup):
    h, up = setup
    tool_use = [{"role": "assistant", "content": [{"type": "tool_use", "id": "t1", "name": "read", "input": {"path": "x", "image_url": "https://example.invalid/a.png"}}]},
                {"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t1", "content": "ok"},
                                             {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "AAAA"}}]}]
    body = msg(messages=tool_use, tools=[{"name": "read", "description": "d", "input_schema": {"type": "object"}},
                                         {"type": "custom", "name": "c", "input_schema": {"type": "object"}},
                                         {"type": "function", "name": "f", "parameters": {"type": "object"}},
                                         {"type": "namespace", "name": "ns", "tools": [{"type": "function", "name": "g"}]}],
               thinking={"type": "adaptive"}, stream=False, context_management={"edits": []})
    assert h.post(URL, h.issue(budget=5.0), body=body)[0] == 200


def test_deeply_nested_bodies_are_refused_not_walked(setup):
    h, up = setup
    deep = current = {}
    for _ in range(policy.MAX_DEPTH + 20):
        current["x"] = {}
        current = current["x"]
    assert h.post(URL, h.issue(), body=msg(metadata=deep))[0] == 403


def test_beta_header_values_must_be_on_the_allowlist():
    up = FakeUpstream()
    h = Harness(up.url, binding={"betas": ["interleaved-thinking-2025-05-14"]})
    try:
        token = h.issue(budget=5.0)
        assert h.post(URL, token, headers={"anthropic-beta": "interleaved-thinking-2025-05-14"})[0] == 200
        assert up.header(0, "anthropic-beta") == ["interleaved-thinking-2025-05-14"]
        status, _, reply = h.post(URL, token, headers={"anthropic-beta": "interleaved-thinking-2025-05-14, files-api-2025-04-14"})
        assert status == 403 and error_kind(reply) == "motel_feature_denied" and "files-api-2025-04-14" in json.loads(reply)["error"]["message"]
        assert h.post(URL, token, headers={"openai-beta": "assistants=v2"})[0] == 403
        assert len(up.requests) == 1
    finally:
        h.close()
        up.close()


def test_default_binding_refuses_any_beta(setup):
    h, up = setup
    assert h.post(URL, h.issue(), headers={"anthropic-beta": "message-threads-2026-08-12"})[0] == 403
    assert up.requests == []


def test_server_held_input_cannot_dodge_the_reservation(setup):
    """The review's repro: tiny bodies whose real input is huge because the upstream holds the context."""
    h, up = setup
    token = h.issue(budget=1.0, max_concurrent=2)
    for body in (msg(thread="th_1"), content({"type": "document", "source": {"type": "file", "file_id": "f1"}})):
        assert h.post(URL, token, body=body)[0] == 403
    assert up.requests == [] and h.registry.usage("a-1")["usd"] == 0


def test_fast_mode_is_reserved_and_settled_at_the_fast_price():
    up = FakeUpstream()
    h = Harness(up.url, binding={"allow": ["fast_mode"]})
    try:
        token = h.issue(budget=5.0)
        h.post(URL, token)
        h.post(URL, token, body=msg(speed="fast"))
        standard, fast = (e["usd"] for e in h.events("reserve"))
        assert fast == pytest.approx(standard * 2, rel=0.02)
        first, second = (e["usd"] for e in h.events("settle"))
        assert second == pytest.approx(first * 2)
        # a response that says it ran fast is charged fast even when the request did not say so
        up.reply = lambda req, handler: (200, {"Content-Type": "application/json"}, [json.dumps(
            {"model": MODEL, "usage": {"input_tokens": 100, "output_tokens": 50, "speed": "fast"}}).encode()])
        h.post(URL, token)
        assert h.events("settle")[-1]["usd"] == pytest.approx(first * 2)
    finally:
        h.close()
        up.close()


def test_images_add_a_token_allowance_to_the_reservation(setup):
    h, up = setup
    token = h.issue(budget=5.0)
    pic = {"type": "image", "source": {"type": "base64", "media_type": "image/png", "data": "iVBORw0KGgo="}}
    h.post(URL, token, body=content({"type": "text", "text": "hi"}))
    h.post(URL, token, body=content({"type": "text", "text": "hi"}, pic, pic))
    plain, with_images = (e["usd"] for e in h.events("reserve"))
    assert with_images - plain >= 2 * policy.IMAGE_ALLOWANCE_TOKENS * 3 / 1e6 * 0.99


# -- settings ----------------------------------------------------------------------------------------------------

GOOD = {"upstream": "https://api.example.com", "auth": "x-api-key", "key_file": "/etc/motel/llm-keys/a.key", "paths": ["/v1/messages"]}


def test_betas_and_allow_parse_and_default_to_nothing():
    plain = parse_tours_section({"bindings": {"a": GOOD}}).bindings["a"]
    assert plain.betas == () and plain.allow == frozenset()
    cfg = parse_tours_section({"bindings": {"a": {**GOOD, "betas": ["x-1"], "allow": ["web_search", "fast_mode"]}}}).bindings["a"]
    assert cfg.betas == ("x-1",) and cfg.allow == {"web_search", "fast_mode"}


@pytest.mark.parametrize("patch,fragment", [({"allow": ["web_browse"]}, "allow must be"), ({"allow": "web_search"}, "allow must be"),
                                           ({"betas": "x"}, "betas must be"), ({"betas": ["a,b"]}, "betas must be"), ({"betas": [1]}, "betas must be")])
def test_bad_betas_and_allow_are_rejected(patch, fragment):
    with pytest.raises(ConfigError, match=fragment):
        parse_tours_section({"bindings": {"a": {**GOOD, **patch}}})


def with_tools(*tools):
    return msg(tools=list(tools))


FILE_REFS = [content({"type": "file", "file": {"file_id": "file-abc"}}),  # OpenAI chat file part
             content({"type": "input_file", "file_id": "file_1"}), content({"type": "input_image", "file_id": "file_1"}),
             content({"type": "container_upload", "file_id": "f"}), content({"type": "document", "file_id": "f"}),
             content({"type": "document", "source": {"type": "file", "file_id": "file_1"}}),
             content({"type": "image", "source": {"file_id": "file_1"}}),
             # at depth: a block inside a tool result is still a block
             msg(messages=[{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": [
                 {"type": "image", "source": {"type": "file", "file_id": "f"}}]}]}]),
             msg(input=[{"role": "user", "content": [{"type": "input_file", "file_id": "file_1"}]}]),
             msg(tools=[{"type": "code_interpreter", "container": {"type": "auto", "file_ids": ["f1"]}}])]


@pytest.mark.parametrize("body", FILE_REFS)
def test_a_file_named_in_a_content_block_source_or_file_part_is_server_state(body):
    """N4: the upstream would read a file the concierge never saw and cannot price (the code_interpreter one is also its own feature)."""
    refused = policy.denied(("code_execution",), body)
    assert refused == ["server_state"] and policy.denied(("server_state", "code_execution"), body) == []


# Review R3: a file_id that is only data is not a file reference. These were false positives of the any-depth rule.
DATA_FILE_IDS = [
    with_tools({"name": "t", "description": "d", "input_schema": {"type": "object", "properties": {"file_id": {"type": "string"}}}}),
    with_tools({"type": "function", "function": {"name": "t", "parameters": {"type": "object", "properties": {"file_id": {"type": "string"},
                                                                                  "file_ids": {"type": "array"}}}}}),
    with_tools({"name": "t", "input_schema": {"type": "object", "properties": {"f": {"type": "file", "file_id": "x"}}}}),
    msg(messages=[{"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "n", "input": {"file_id": "x", "file_ids": ["y"]}}]}]),
    msg(messages=[{"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "n", "input": {"type": "file", "file_id": "x"}}]}]),
    msg(messages=[{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": "file_id: abc"}]}]),
    msg(metadata={"file_id": "mine"}), content({"type": "text", "text": "x", "file_id": "f"}),
    msg(response_format={"type": "json_schema", "json_schema": {"name": "n", "schema": {"type": "object", "properties": {"file_id": {}}}}}),
]


@pytest.mark.parametrize("body", DATA_FILE_IDS)
def test_a_file_id_that_is_only_data_is_not_a_file_reference(body):
    assert policy.denied((), body) == []


def test_a_null_file_id_names_no_file():
    assert policy.denied((), content({"type": "input_file", "file_id": None}, {"type": "image", "source": {"type": "base64", "file_id": None}})) == []


@pytest.mark.parametrize("value", [True, 1, "yes", "false", {"a": 1}, [True], None, 0])
def test_any_background_value_but_false_is_server_state(value):
    assert policy.denied((), msg(background=value)) == ["server_state"]
    assert policy.denied((), msg(background=False)) == [] and policy.denied((), BODY) == []


CLIENT_TOOLS = [{"name": "t"}, {"type": "custom", "name": "t"}, {"type": "function", "name": "t", "parameters": {}},
                {"type": "namespace", "name": "ns", "tools": [{"type": "custom", "name": "a"}, {"type": "function", "name": "b"}]}]


@pytest.mark.parametrize("tool", CLIENT_TOOLS)
def test_client_tools_have_no_type_or_a_client_type(tool):
    assert policy.denied((), with_tools(tool)) == []


@pytest.mark.parametrize("extra", [{"server_url": "https://e.example/mcp"}, {"connector_id": "c"}, {"authorization": "x"},
                                   {"defer_loading": True}, {"server_label": "s"}])
def test_a_namespace_with_fields_of_its_own_beyond_name_description_and_tools_needs_an_allow(extra):
    """Review R7: only the shape codex really sends is a client tool; a namespace that carries anything else is not."""
    tool = {"type": "namespace", "name": "n", "description": "d", "tools": [{"type": "function", "name": "f"}], **extra}
    assert policy.denied((), with_tools(tool)) == ["tool:namespace"]
    assert policy.denied(("tool:namespace",), with_tools(tool)) == []


def test_a_namespace_does_not_hide_the_tools_inside_it():
    tool = {"type": "namespace", "name": "n", "tools": [{"type": "web_search_20260209", "name": "w"}, {"type": "mystery_1", "name": "m"}]}
    assert policy.denied((), with_tools(tool)) == ["web_search", "tool:mystery_1"]


UNKNOWN_TOOLS = ["advisor_20260301", "tool_search_tool_regex_20251119", "tool_search_tool_bm25_20251119", "bash_20250124",
                 "text_editor_20250728", "computer_20251124", "memory_20250818", "local_shell", "shell", "apply_patch",
                 "a_tool_from_2031", "memory"]


@pytest.mark.parametrize("kind", UNKNOWN_TOOLS)
def test_any_other_tool_type_needs_its_exact_name_in_allow(kind):
    body = with_tools({"type": kind, "name": "x"})
    assert policy.denied((), body) == ["tool:" + kind]
    assert policy.denied(("tool:" + kind,), body) == []
    assert policy.denied(("tool:" + kind + "_x", "web_search", "server_state"), body) == ["tool:" + kind]  # exact, not a prefix


def test_unusual_tool_types_are_refused_not_crashed_on():
    for kind in (5, ["a"], {"a": 1}, "has space", "x" * 200, "ünï"):
        assert policy.denied((), with_tools({"type": kind, "name": "x"})) == ["tool:invalid"]
    assert policy.denied((), with_tools("not a dict", 5, None)) == ["tool:invalid"]


def test_a_server_tool_family_still_follows_its_own_feature_name():
    body = with_tools({"type": "web_search_20260209", "name": "web_search"}, {"type": "advisor_20260301", "name": "a"})
    assert policy.denied((), body) == ["web_search", "tool:advisor_20260301"]
    assert policy.denied(("web_search",), body) == ["tool:advisor_20260301"]


def test_tools_nested_inside_codex_style_input_items_are_checked_too():
    item = {"type": "additional_tools", "role": "developer", "tools": [{"type": "namespace", "name": "x",
                                                                      "tools": [{"type": "advisor_20260301", "name": "a"}]}]}
    assert policy.denied((), msg(input=[item])) == ["tool:advisor_20260301"]


def test_the_settings_accept_exact_tool_allow_entries_only():
    from motel.concierge.settings import parse_tours_section
    base = {"upstream": "https://api.example.com", "auth": "x-api-key", "key_file": "/k", "paths": ["/v1/messages"]}
    ok = parse_tours_section({"bindings": {"a": {**base, "allow": ["tool:bash_20250124", "web_search"]}}})
    assert ok.bindings["a"].allow == frozenset({"tool:bash_20250124", "web_search"})
    for bad in ("tool:", "tool:a b", "tool:" + "x" * 65, "bash_20250124", "tool:*", 5):
        with pytest.raises(ConfigError, match="allow must be a list"):
            parse_tours_section({"bindings": {"a": {**base, "allow": [bad]}}})


def test_the_refusal_names_the_tool_type_and_the_allow_entry_unlocks_it(setup):
    h, up = setup
    status, _, reply = h.post(URL, h.issue(budget=5.0), body=with_tools({"type": "advisor_20260301", "name": "a", "model": "m"}))
    assert status == 403 and error_kind(reply) == "motel_feature_denied" and "tool:advisor_20260301" in json.loads(reply)["error"]["message"]
    other = FakeUpstream()
    allowed = Harness(other.url, binding={"allow": ["tool:advisor_20260301"]})
    try:
        assert allowed.post(URL, allowed.issue(budget=5.0), body=with_tools({"type": "advisor_20260301", "name": "a"}))[0] == 200
    finally:
        allowed.close()
        other.close()


# -- N3: PDFs ----------------------------------------------------------------------------------------------------------

def test_plain_text_documents_are_not_pdfs_but_base64_ones_are():
    text_doc = content({"type": "document", "source": {"type": "text", "media_type": "text/plain", "data": "hello"}})
    content_doc = content({"type": "document", "source": {"type": "content", "content": "hello"}})
    assert policy.denied((), text_doc) == [] and policy.denied((), content_doc) == []
    assert policy.denied((), content(pdf_block(ONE_PAGE))) == ["pdf"]
    assert policy.denied(("pdf",), content(pdf_block(ONE_PAGE))) == []


def test_a_pdf_is_reserved_per_page(setup):
    h, up = setup
    h2 = Harness(up.url, binding={"allow": ["pdf"]})
    try:
        token = h2.issue(budget=5.0, max_concurrent=4)
        base = h2.post(URL, token, body=content(pdf_block(make_pdf(1))))
        three = h2.post(URL, token, body=content(pdf_block(make_pdf(3))))
        assert base[0] == three[0] == 200
        first, second = h2.events("reserve")
        sent = [len(r["body"]) for r in up.requests[-2:]]
        expected = lambda size, pages: (-(-size // 3) + pages * 3000) * 3 / 1e6 + 100 * 15 / 1e6  # noqa: E731
        assert first["usd"] == pytest.approx(expected(sent[0], 1), abs=1e-6)
        assert second["usd"] == pytest.approx(expected(sent[1], 3), abs=1e-6)
    finally:
        h2.close()


def test_pages_are_counted_from_page_objects_or_the_declared_count():
    assert policy.pdf_pages(content(pdf_block(make_pdf(7)))) == 7
    declared = b"%PDF-1.4\n<< /Type /Pages /Kids [] /Count 40 >>\n"
    assert policy.pdf_pages(content(pdf_block(declared + b"<< /Type /Page >>"))) == 40
    assert policy.pdf_pages(content(pdf_block(make_pdf(2)), pdf_block(make_pdf(5)))) == 7  # blocks add up
    assert policy.pdf_pages(msg()) == 0


@pytest.mark.parametrize("raw", [b"not a pdf at all", b"", b"%PDF-1.7\n1 0 obj << /Type /Catalog >>", make_pdf(2, b"<< /Type /ObjStm >>"),
                                 b"%PDF-1.4\n<< /Type /Pages /Count 3 >> << /Type /ObjStm /N 100 >>"])
def test_a_pdf_whose_pages_cannot_be_counted_is_assumed_to_have_600(raw):
    assert policy.pdf_pages(content(pdf_block(raw))) == 600


def test_bad_base64_and_an_openai_file_part_are_counted_too():
    broken = {"type": "document", "source": {"type": "base64", "media_type": "application/pdf", "data": "%%%not base64%%%"}}
    assert policy.pdf_pages(content(broken)) == 600
    part = {"type": "input_file", "file_data": "data:application/pdf;base64," + b64(make_pdf(4))}
    assert policy.pdf_pages(content(part)) == 4
    bare = {"type": "file", "file": {"file_data": b64(make_pdf(9))}}
    assert policy.pdf_pages(content(bare)) == 9
    assert policy.pdf_pages(content(pdf_block(b"hello", "text/plain"))) == 0


def test_the_review_repro_a_tiny_600_page_pdf_cannot_slip_under_the_reservation():
    """70 KB, 600 pages: 1.8M input tokens. The old reservation (one token per 3 bytes) was about 23000 tokens."""
    raw = make_pdf(600)
    assert len(raw) < 100_000
    up = FakeUpstream()
    h = Harness(up.url, binding={"allow": ["pdf"]})
    try:
        status, _, reply = h.post(URL, h.issue(budget=5.0), body=content(pdf_block(raw)))
        assert status == 403 and error_kind(reply) == "motel_budget_exhausted" and up.requests == []  # 1.8M x USD 3 = USD 5.4
        assert h.post(URL, h.issue("a-2", budget=5.0), body=content(pdf_block(make_pdf(100))))[0] == 200
    finally:
        h.close()
        up.close()


def test_without_the_allow_entry_a_pdf_never_reaches_the_upstream(setup):
    h, up = setup
    status, _, reply = h.post(URL, h.issue(), body=content(pdf_block(make_pdf(600))))
    assert status == 403 and error_kind(reply) == "motel_feature_denied" and "pdf" in json.loads(reply)["error"]["message"]
    assert up.requests == []


# -- review F2: Responses input items that only name stored items; store must be false ---------------------------------------------

@pytest.mark.parametrize("item", [{"type": "item_reference", "id": "msg_1"}, {"type": "item_reference"}, {"id": "msg_1"},
                                  {"type": "reasoning", "id": "rs_1"}, {"type": "message", "id": "m"}])
def test_an_input_item_with_only_an_id_and_a_type_is_server_state(item):
    assert policy.denied((), msg(input=[{"role": "user", "content": "x"}, item])) == ["server_state"]
    assert policy.denied(("server_state",), msg(input=[item])) == []


@pytest.mark.parametrize("item", [{"role": "user", "content": "x"}, {"type": "message", "id": "m", "content": []},
                                  {"type": "reasoning", "id": "r", "summary": []}, {"type": "function_call", "id": "f", "name": "n"}, {}, "text"])
def test_an_input_item_that_carries_content_is_not_a_stored_reference(item):
    assert policy.denied((), msg(input=[item])) == []


def test_store_must_be_false_on_a_responses_request_only():
    for store in ({}, {"store": True}, {"store": None}, {"store": 0}, {"store": "false"}):
        assert policy.detect(msg(**store), responses=True)[0] == {"server_state"}
        assert policy.detect(msg(**store))[0] == set()  # not a Responses request: no such rule
    assert policy.detect(msg(store=False), responses=True)[0] == set()
    assert policy.is_responses_path("/v1/responses") and policy.is_responses_path("/v1/responses/compact")
    assert not policy.is_responses_path("/v1/chat/completions") and not policy.is_responses_path("/v1/responses/abc/cancel")


def test_the_captured_codex_request_says_store_false_and_has_no_stored_reference():
    detect = policy.detect
    path = os.path.join(os.path.dirname(__file__), "fixtures", "codex_0_160_0_responses.json")
    body = json.load(open(path))["request"]["body"]
    assert body["store"] is False and detect(body, responses=True)[0] == set()


# -- review F6: opaque only where a value really is a schema or a tool call's arguments -----------------------------------------------

FILE_BLOCK = {"type": "input_file", "file_id": "file_abc"}
DOC_FILE = {"type": "document", "source": {"type": "file", "file_id": "file_abc"}}

HIDDEN_BEFORE = [
    # a tool_use at the top of the body is the request, not a tool call: its input is read
    msg(type="tool_use", input=[FILE_BLOCK]),
    # ... and so is one that is not an entry of a content list
    msg(metadata={"type": "tool_use", "input": FILE_BLOCK}),
    msg(input=[{"type": "tool_use", "id": "t", "name": "n", "input": FILE_BLOCK}]),
    # a content block with a key named like a schema is not a schema
    content({"type": "text", "text": "x", "schema": DOC_FILE}),
    content({"type": "text", "text": "x", "parameters": FILE_BLOCK}),
    content({"type": "text", "text": "x", "input_schema": FILE_BLOCK}),
    msg(metadata={"schema": FILE_BLOCK}),
    msg(messages=[{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": [], "schema": FILE_BLOCK}]}]),
    # a schema key under a tools-like name that is not a tool entry
    msg(extras={"tools": {"schema": FILE_BLOCK}}),
]


@pytest.mark.parametrize("body", HIDDEN_BEFORE)
def test_a_file_reference_is_found_unless_it_sits_where_a_schema_or_a_tool_calls_arguments_go(body):
    assert policy.denied((), body) == ["server_state"]


OPAQUE_PLACES = [
    with_tools({"name": "t", "input_schema": {"properties": {"f": FILE_BLOCK}}}),
    with_tools({"name": "t", "parameters": {"properties": {"f": FILE_BLOCK}}}),
    with_tools({"type": "function", "function": {"name": "t", "parameters": {"f": FILE_BLOCK}}}),
    with_tools({"type": "custom", "name": "t", "custom": {"input_schema": {"f": FILE_BLOCK}}}),
    with_tools({"type": "namespace", "name": "n", "tools": [{"type": "function", "name": "f", "parameters": {"x": FILE_BLOCK}}]}),
    msg(input=[{"type": "additional_tools", "tools": [{"type": "function", "name": "f", "parameters": {"x": FILE_BLOCK}}]}]),
    msg(response_format={"type": "json_schema", "json_schema": {"name": "n", "schema": {"f": FILE_BLOCK}}}),
    msg(text={"format": {"type": "json_schema", "name": "n", "schema": {"f": FILE_BLOCK}}}),
    msg(output_config={"format": {"type": "json_schema", "schema": {"f": FILE_BLOCK}}}),
    msg(messages=[{"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "n", "input": {"a": FILE_BLOCK}}]}]),
    msg(messages=[{"role": "assistant", "content": [{"type": "server_tool_use", "id": "t", "name": "n", "input": {"a": FILE_BLOCK}}]}]),
    msg(messages=[{"role": "assistant", "content": [{"type": "mcp_tool_use", "id": "t", "name": "n", "input": [FILE_BLOCK]}]}]),
]


@pytest.mark.parametrize("body", OPAQUE_PLACES)
def test_a_file_shaped_value_in_a_schema_or_a_tool_calls_arguments_is_data(body):
    assert policy.denied(("mcp",), body) == []  # (an mcp_tool_use block is the mcp feature in its own right)


def test_a_tool_calls_input_is_opaque_but_its_neighbours_are_not():
    call = {"type": "tool_use", "id": "t", "name": "n", "input": {"a": FILE_BLOCK}, "extra": FILE_BLOCK}
    assert policy.denied((), msg(messages=[{"role": "assistant", "content": [call]}])) == ["server_state"]


# -- review L3: the opaque places are fixed positions, not any key of that name at any depth -------------------------------------------

SCHEMA = {"properties": {"f": FILE_BLOCK}}
ELSEWHERE = [
    # the same shapes as the opaque places, one level too deep or under another parent
    msg(metadata={"tools": [{"name": "t", "input_schema": SCHEMA}]}),
    msg(extra={"tools": [{"name": "t", "parameters": SCHEMA}]}),
    msg(messages=[{"role": "user", "content": [{"type": "text", "text": "x", "tools": [{"name": "t", "input_schema": SCHEMA}]}]}]),
    msg(tools=[{"name": "t", "wrapper": {"input_schema": SCHEMA}}]),
    msg(tools=[{"type": "function", "function": {"nested": {"parameters": SCHEMA}}}]),
    msg(tools=[{"type": "namespace", "name": "n", "tools": [{"type": "namespace", "name": "m", "tools": [
        {"type": "function", "name": "f", "parameters": SCHEMA}]}]}]),  # a third level
    msg(input=[{"type": "message", "role": "user", "content": [{"type": "input_text", "text": "x", "tools": [{"name": "t", "parameters": SCHEMA}]}]}]),
    msg(input=[{"role": "user", "extra": {"tools": [{"name": "t", "parameters": SCHEMA}]}}]),
    msg(extra={"response_format": {"json_schema": {"schema": SCHEMA}}}),
    msg(metadata={"text": {"format": {"schema": SCHEMA}}}),
    msg(messages=[{"role": "user", "content": [{"type": "text", "text": "x", "output_config": {"format": {"schema": SCHEMA}}}]}]),
    # a tool_use whose input holds a file block, but not in messages[].content[]
    msg(messages=[{"role": "user", "content": [{"type": "tool_result", "tool_use_id": "t", "content": [
        {"type": "tool_use", "id": "t", "name": "n", "input": {"a": FILE_BLOCK}}]}]}]),
    msg(system=[{"type": "tool_use", "id": "t", "name": "n", "input": {"a": FILE_BLOCK}}]),
    msg(extra=[{"type": "tool_use", "id": "t", "name": "n", "input": {"a": FILE_BLOCK}}]),
    msg(wrapper={"messages": [{"role": "assistant", "content": [{"type": "tool_use", "id": "t", "name": "n", "input": {"a": FILE_BLOCK}}]}]}),
]


@pytest.mark.parametrize("body", ELSEWHERE)
def test_a_schema_or_tool_call_shape_at_another_depth_is_still_inspected(body):
    assert policy.denied((), body) == ["server_state"]


def test_the_opaque_positions_are_exactly_the_documented_ones():
    paths = policy._TOOL_SCHEMAS | policy._FORMAT_SCHEMAS
    assert ("tools", "[]", "input_schema") in paths and ("input", "[]", "tools", "[]", "parameters") in paths
    assert ("tools", "[]", "tools", "[]", "parameters") in paths and ("input", "[]", "tools", "[]", "function", "parameters") in paths
    assert ("text", "format", "schema") in paths and ("response_format", "json_schema", "schema") in paths
    assert all(p[0] in ("tools", "input", "response_format", "text", "output_config") for p in paths)  # all anchored at the root
    assert ("metadata", "tools", "[]", "input_schema") not in paths and ("tools", "[]", "tools", "[]", "tools", "[]", "parameters") not in paths
