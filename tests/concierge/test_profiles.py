"""Agent CLI profiles: what a real ``claude -p`` and ``codex exec`` request looks like to a deny-by-default binding.

The fixtures in ``fixtures/`` were captured from the real CLIs against a loopback fake upstream (see docs/concierge.md);
text, ids and schemas are replaced, nothing secret is in them.
"""
import json
import os

import pytest
from concierge_fakes import FakeUpstream, Harness, error_kind

from motel.concierge import policy, profiles
from motel.concierge.errors import ConfigError
from motel.concierge.settings import parse_tours_section

FIXTURES = os.path.join(os.path.dirname(__file__), "fixtures")


def load(name):
    with open(os.path.join(FIXTURES, name)) as handle:
        return json.load(handle)


CLAUDE = load("claude_code_2_1_319_messages.json")["request"]  # CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1
CLAUDE_DEFAULT = load("claude_code_2_1_319_messages_default_env.json")["request"]  # the CLI's default environment
CLAUDE_BETAS = load("claude_code_2_1_319_betas.json")["observed"]
CODEX = load("codex_0_160_0_responses.json")["request"]
OPENAI = {"auth": "bearer", "paths": ["/v1/chat/completions", "/v1/responses", "/v1/models/*"], "methods": ["POST", "GET"]}


def claude_headers():
    return {k: v for k, v in CLAUDE["headers"].items() if k not in ("content-type",)}


def codex_headers():
    return {k: v for k, v in CODEX["headers"].items() if k not in ("content-type",)}


def codex_reply(up):
    up.reply = lambda req, handler: (200, {"Content-Type": "application/json"},
                                     [json.dumps({"model": "gpt-6.1-sol", "usage": {"input_tokens": 10, "output_tokens": 10}}).encode()])


@pytest.fixture
def parts():
    made = []

    def make(**binding):
        up = FakeUpstream()
        h = Harness(up.url, binding=binding)
        made.append((h, up))
        return h, up

    yield make
    for h, up in made:
        h.close()
        up.close()


def test_the_fixtures_are_what_the_docs_say_they_are():
    assert CLAUDE["path"] == "/v1/messages?beta=true" and CLAUDE["body"]["model"] == "claude-opus-5-5"
    assert CODEX["path"] == "/v1/responses" and CODEX["body"]["model"] == "gpt-6.1-sol"
    assert "authorization" not in CODEX["headers"] and "x-api-key" not in CLAUDE["headers"] and "openai-beta" not in CODEX["headers"]
    assert "anthropic-beta" in CLAUDE["headers"]


def test_a_real_claude_print_request_is_refused_without_the_profile_naming_the_betas(parts):
    h, up = parts()
    status, _, reply = h.post("/u/anthropic" + CLAUDE["path"], h.issue(budget=5.0), body=CLAUDE["body"], headers=claude_headers())
    assert status == 403 and error_kind(reply) == "motel_feature_denied" and "claude-code-20250219" in json.loads(reply)["error"]["message"]
    assert up.requests == []


def test_a_real_claude_print_request_is_served_with_the_profile(parts):
    h, up = parts(profile="claude-code")
    status, _, _ = h.post("/u/anthropic" + CLAUDE["path"], h.issue(budget=5.0), body=CLAUDE["body"], headers=claude_headers())
    assert status == 200 and len(up.requests) == 1
    # the beta header goes on to the upstream as the CLI sent it, and the key is the real one
    assert up.header(0, "anthropic-beta") == [CLAUDE["headers"]["anthropic-beta"]]
    assert up.header(0, "x-api-key")[0].startswith("sk-test-real")
    sent = json.loads(up.requests[0]["body"])
    assert sent["output_config"] == {"effort": "medium"} and sent["thinking"]["type"] == "adaptive"


def test_the_cli_default_environment_sends_betas_the_profile_refuses(parts):
    # without CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 the CLI adds five more betas, one of which enables server tools
    h, up = parts(profile="claude-code")
    status, _, reply = h.post("/u/anthropic" + CLAUDE_DEFAULT["path"], h.issue(budget=5.0), body=CLAUDE_DEFAULT["body"],
                              headers={k: v for k, v in CLAUDE_DEFAULT["headers"].items() if k != "content-type"})
    assert status == 403 and error_kind(reply) == "motel_feature_denied" and "advanced-tool-use-2025-11-20" in json.loads(reply)["error"]["message"]
    assert up.requests == []
    assert "context_management" in CLAUDE_DEFAULT["body"] and "context_management" not in CLAUDE["body"]


def test_every_beta_set_seen_in_the_captures_passes_the_profile_when_the_cli_is_told_not_to_send_experimental_betas():
    profile = profiles.get("claude-code")
    minimal, default = set(), set()
    for run in CLAUDE_BETAS:
        values = policy.beta_values([run["anthropic-beta"]])
        if run["env"] == "CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1":
            assert policy.denied_betas(profile.betas, values) == [], run
            minimal.update(values)
        else:
            default.update(values)
    assert minimal == set(profile.betas) and len(profile.betas) == len(set(profile.betas))
    # the default environment adds exactly these five, and the profile refuses every one of them
    assert default - minimal == {"advanced-tool-use-2025-11-20", "context-management-2025-06-27", "per-turn-control-2026-07-01",
                                 "mid-conversation-tool-changes-2026-07-01", "thinking-token-count-2026-05-13"}
    assert {m for run in CLAUDE_BETAS if run["env"] != "default" for m in run["models"]} >= {"claude-opus-5-5", "claude-haiku-5-5"}


def test_the_captured_claude_body_uses_no_gated_feature():
    found, images = policy.detect(CLAUDE["body"])
    assert found == set() and images == 0
    assert {t.get("type") for t in CLAUDE["body"]["tools"]} == {None}  # client-side tools only


def test_the_profile_allows_no_beta_that_enables_server_tools_state_fast_mode_fallbacks_or_a_second_model():
    forbidden = ("advanced-tool-use", "mcp", "files-api", "fast-mode", "fallback", "advisor", "web-search", "code-execution",
                 "skills", "thread", "container", "environments")
    for profile in profiles.PROFILES.values():
        assert not [b for b in profile.betas if any(word in b for word in forbidden)]


def test_the_profile_does_not_unlock_the_features_it_was_not_captured_with(parts):
    h, up = parts(profile="claude-code")
    token = h.issue(budget=5.0)
    for extra, feature in (({"tools": [{"type": "code_execution_20260120", "name": "code_execution"}]}, "code_execution"),
                           ({"tools": [{"type": "web_search_20260209", "name": "web_search"}]}, "web_search"),
                           ({"speed": "fast"}, "fast_mode"), ({"thread": {"id": "t"}}, "server_state"),
                           ({"mcp_servers": [{"type": "url", "url": "https://x.invalid", "name": "x"}]}, "mcp")):
        status, _, reply = h.post("/u/anthropic/v1/messages", token, body={**CLAUDE["body"], **extra}, headers=claude_headers())
        assert status == 403 and error_kind(reply) == "motel_feature_denied" and feature in json.loads(reply)["error"]["message"]
    for beta in ("fast-mode-2026-02-01", "mcp-client-2025-11-20", "files-api-2025-04-14", "server-side-fallback-2026-07-01",
                 "advanced-tool-use-2025-11-20", "advisor-tool-2026-03-01"):
        headers = {**claude_headers(), "anthropic-beta": CLAUDE["headers"]["anthropic-beta"] + "," + beta}
        status, _, reply = h.post("/u/anthropic/v1/messages", token, body=CLAUDE["body"], headers=headers)
        assert status == 403 and beta in json.loads(reply)["error"]["message"]
    assert up.requests == []


def test_the_default_opus_request_reserves_more_than_the_default_guest_budget(parts):
    # claude asks for max_tokens 128000; on Opus 5.5 (USD 20 / 1M out) that alone is USD 2.56
    h, up = parts(profile="claude-code")
    assert CLAUDE["body"]["max_tokens"] == 128000
    status, _, reply = h.post("/u/anthropic/v1/messages", h.issue(budget=2.0), body=CLAUDE["body"], headers=claude_headers())
    assert status == 403 and error_kind(reply) == "motel_budget_exhausted" and up.requests == []
    status, _, _ = h.post("/u/anthropic/v1/messages", h.issue("a-2", budget=2.0), body={**CLAUDE["body"], "max_tokens": 8000},
                          headers=claude_headers())
    assert status == 200


def test_a_real_codex_request_needs_no_beta_and_is_served_with_the_profile(parts):
    for binding in (OPENAI, {**OPENAI, "profile": "codex"}):
        h, up = parts(**binding)
        codex_reply(up)
        status, _, _ = h.post("/u/anthropic" + CODEX["path"], h.issue(budget=5.0), body=CODEX["body"], headers=codex_headers(),
                              auth="bearer")
        assert status == 200 and len(up.requests) == 1
        assert up.header(0, "authorization")[0].startswith("Bearer sk-test-real")
        assert up.header(0, "x-codex-beta-features") == ["remote_compaction_v2"]  # not a beta header; passed through


def test_the_captured_codex_body_uses_no_gated_feature():
    found, images = policy.detect(CODEX["body"])
    assert found == set() and images == 0 and CODEX["body"]["store"] is False


def test_the_captured_codex_request_carries_its_tools_in_a_namespace_of_exactly_the_known_fields():
    """The evidence for treating ``namespace`` as a client tool: the real request has tools, grouped like this."""
    items = [i for i in CODEX["body"]["input"] if i.get("type") == "additional_tools"]
    spaces = [t for i in items for t in i["tools"] if t.get("type") == "namespace"]
    assert spaces and all(set(t) <= policy.NAMESPACE_KEYS for t in spaces)
    kinds = {t["type"] for space in spaces for t in space["tools"]}
    assert kinds <= {"function", "custom"} and kinds


def test_a_websocket_beta_is_not_allowed_for_codex(parts):
    h, up = parts(**{**OPENAI, "profile": "codex"})
    status, _, reply = h.post("/u/anthropic/v1/responses", h.issue(), body=CODEX["body"], auth="bearer",
                              headers={"OpenAI-Beta": "responses_websockets=2026-02-06"})
    assert status == 403 and error_kind(reply) == "motel_feature_denied" and up.requests == []


def test_operator_betas_and_allow_are_added_to_the_profile(parts):
    cfg = parse_tours_section({"bindings": {"anthropic": {
        "upstream": "https://api.example.com", "auth": "x-api-key", "key_file": "/k", "paths": ["/v1/messages"],
        "profile": "claude-code", "betas": ["extra-2026-01-01", "effort-2025-11-24"], "allow": ["web_search"]}}})
    binding = cfg.bindings["anthropic"]
    assert binding.profile == "claude-code" and binding.allow == frozenset({"web_search"})
    assert binding.betas[:3] == profiles.get("claude-code").betas and binding.betas[3:] == ("extra-2026-01-01",)
    plain = parse_tours_section({"bindings": {"anthropic": {
        "upstream": "https://api.example.com", "auth": "x-api-key", "key_file": "/k", "paths": ["/v1/messages"]}}})
    assert plain.bindings["anthropic"].betas == () and plain.bindings["anthropic"].profile == ""


@pytest.mark.parametrize("value", ["nope", "Claude-Code", 3, ["claude-code"]])
def test_an_unknown_profile_is_a_configuration_error(value):
    with pytest.raises(ConfigError, match="profile must be one of"):
        parse_tours_section({"bindings": {"anthropic": {"upstream": "https://api.example.com", "auth": "x-api-key",
                                                         "key_file": "/k", "paths": ["/v1/messages"], "profile": value}}})
    with pytest.raises(ConfigError):
        profiles.get("nope")


def test_no_built_in_profile_allows_a_gated_feature():
    for profile in profiles.PROFILES.values():
        assert not profile.allow  # an allowed feature would need its own captured reason in docs/concierge.md
        assert all(b and "," not in b for b in profile.betas)


# -- review L4: codex compaction -----------------------------------------------------------------------------------------------------
# codex 0.160.0 (remote_compaction_v2) compacts through an ordinary POST /v1/responses whose last input item is
# {"type": "compaction_trigger"}; it sent no /v1/responses/compact (checked: a provider named "OpenAI" with an auto-compact limit
# of 1000 tokens against a loopback fake). The older /v1/responses/compact shape could not be produced with this CLI, so the tests
# for that path below use a synthetic body, built from the same codex request with the trigger item left out; that is stated here
# because it is not a capture.
COMPACTION = load("codex_0_160_0_compaction_v2.json")
OPENAI_COMPACT = {**OPENAI, "paths": [*OPENAI["paths"], "/v1/responses/compact"]}


def test_the_captured_compaction_request_is_a_plain_responses_request_with_a_trigger_item():
    request = COMPACTION["request"]
    assert request["path"] == "/v1/responses" and request["body"]["store"] is False
    assert request["body"]["input"][-1] == {"type": "compaction_trigger"}
    assert [item["type"] for item in COMPACTION["follow_up"]["body"]["input"]][-1] == "compaction"
    for entry in (request, COMPACTION["follow_up"]):
        assert policy.detect(entry["body"], responses=True)[0] == set()  # no gated feature, no stored reference


@pytest.mark.parametrize("entry", ["request", "follow_up"])
def test_a_real_codex_compaction_round_trip_is_served_with_the_codex_profile(parts, entry):
    h, up = parts(**{**OPENAI, "profile": "codex"})
    codex_reply(up)
    sent = COMPACTION[entry]
    headers = {k: v for k, v in sent["headers"].items() if k != "content-type"}
    status, _, reply = h.post("/u/anthropic" + sent["path"], h.issue(budget=5.0), body=sent["body"], headers=headers, auth="bearer")
    assert status == 200, reply
    forwarded = json.loads(up.requests[-1]["body"])
    assert forwarded["store"] is False and forwarded["input"][-1]["type"] == sent["body"]["input"][-1]["type"]


def test_a_compaction_request_that_would_be_stored_gets_a_clear_permanent_403(parts):
    h, up = parts(**{**OPENAI, "profile": "codex"})
    codex_reply(up)
    body = dict(COMPACTION["request"]["body"])
    del body["store"]  # a missing store means true
    status, headers, reply = h.post("/u/anthropic/v1/responses", h.issue(budget=5.0), body=body, auth="bearer")
    message = json.loads(reply)["error"]["message"]
    assert status == 403 and error_kind(reply) == "motel_feature_denied" and "Retry-After" not in dict(headers)
    assert '"store": false' in message and "compaction" in message and "same 403" in message and not up.requests


def test_the_older_compact_path_is_a_responses_request_too(parts):
    h, up = parts(**{**OPENAI_COMPACT, "profile": "codex"})
    codex_reply(up)
    body = {k: v for k, v in COMPACTION["request"]["body"].items() if k != "store"}
    body["input"] = body["input"][:-1]  # synthetic: no trigger item (see the comment above)
    token = h.issue(budget=5.0)
    status, _, reply = h.post("/u/anthropic/v1/responses/compact", token, body=body, auth="bearer")
    assert status == 403 and "responses/compact" in json.loads(reply)["error"]["message"] and not up.requests
    assert h.post("/u/anthropic/v1/responses/compact", token, body={**body, "store": False}, auth="bearer")[0] == 200
