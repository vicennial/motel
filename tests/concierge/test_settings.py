"""``parse_tours_section``: defaults, validation, and the allowed-path matcher."""
import pytest

from motel.concierge.errors import ConfigError
from motel.concierge.pricing import DEFAULT_PRICE
from motel.concierge.settings import parse_tours_section

GOOD = {"upstream": "https://api.example.com", "auth": "x-api-key", "key_file": "/etc/motel/llm-keys/a.key",
        "paths": ["/v1/messages", "/v1/models/*"]}


def test_empty_table_is_all_contract_defaults_and_disabled():
    cfg = parse_tours_section({})
    assert (cfg.enabled, cfg.concierge_port, cfg.max_concurrent_guests, cfg.max_guests_per_tour) == (False, 7781, 4, 20)
    assert (cfg.max_eur_per_tour, cfg.max_usd_model_per_guest, cfg.max_ttl_s, cfg.max_result_mb) == (5.0, 2.0, 4 * 3600, 64)
    assert (cfg.default_image, cfg.default_server_type, cfg.egress_allow, cfg.bindings) == ("ubuntu-24.04", "", (), {})
    assert cfg.max_request_bytes == 8 * 1024 * 1024
    assert parse_tours_section(None).enabled is False


def test_the_contract_example_parses():
    cfg = parse_tours_section({"enabled": True, "concierge_port": 7781, "egress_allow": ["pypi.org"], "max_ttl": "90m",
                               "bindings": {"anthropic": {**GOOD, "paths": ["/v1/messages", "/v1/messages/count_tokens"]}}})
    binding = cfg.bindings["anthropic"]
    assert cfg.max_ttl_s == 5400 and cfg.egress_allow == ("pypi.org",)
    assert (binding.scheme, binding.host, binding.port, binding.upstream) == ("https", "api.example.com", 443, "https://api.example.com")
    assert binding.format == "anthropic" and binding.methods == ("POST",) and binding.default_price == DEFAULT_PRICE


def test_format_defaults_from_the_auth_style_and_can_be_overridden():
    assert parse_tours_section({"bindings": {"o": {**GOOD, "auth": "bearer"}}}).bindings["o"].format == "openai"
    assert parse_tours_section({"bindings": {"o": {**GOOD, "auth": "bearer", "format": "anthropic"}}}).bindings["o"].format == "anthropic"


def test_pricing_overrides_parse():
    cfg = parse_tours_section({"bindings": {"a": {**GOOD, "default_price": {"input": 20, "output": 100},
                                                  "pricing": {"claude-x": {"input": 1, "output": 2, "cache_read": 0.1}}}}})
    b = cfg.bindings["a"]
    assert b.default_price.output == 100 and b.pricing["claude-x"].cache_read == 0.1 and b.pricing["claude-x"].cache_write_5m == 1.25


@pytest.mark.parametrize("patch,fragment", [
    ({"upstream": "http://api.example.com"}, "plain http only for a loopback"),
    ({"upstream": "https://api.example.com/v1"}, "must be an origin"),
    ({"upstream": "https://user:pw@api.example.com"}, "must be an origin"),
    ({"upstream": "ftp://api.example.com"}, "must be an origin"),
    ({"upstream": ""}, "upstream is required"),
    ({"auth": "basic"}, "auth must be one of"),
    ({"key_file": "relative.key"}, "absolute path"),
    ({"paths": []}, "non-empty list"),
    ({"paths": ["v1/messages"]}, "absolute path"),
    ({"paths": ["/v1/../etc"]}, "absolute path"),
    ({"paths": ["/v1//x"]}, "absolute path"),
    ({"paths": ["/v1/%2e"]}, "absolute path"),
    ({"methods": ["DELETE"]}, "methods must be"),
    ({"format": "gemini"}, "format must be"),
    ({"max_concurrent": 0}, "max_concurrent"),
    ({"typo_key": 1}, "unknown key 'typo_key'"),
    ({"default_price": {"input": 1}}, "needs input and output"),
    ({"pricing": {"m": {"input": -1, "output": 1}}}, "input"),
])
def test_invalid_bindings_are_rejected_with_a_clear_message(patch, fragment):
    with pytest.raises(ConfigError) as err:
        parse_tours_section({"bindings": {"a": {**GOOD, **patch}}})
    assert fragment in err.value.message and err.value.line().startswith("motel: ") and "Try: " in err.value.line()


def test_loopback_http_upstream_is_allowed():
    assert parse_tours_section({"bindings": {"a": {**GOOD, "upstream": "http://127.0.0.1:4000"}}}).bindings["a"].port == 4000
    assert parse_tours_section({"bindings": {"a": {**GOOD, "upstream": "http://localhost:4000"}}}).bindings["a"].scheme == "http"


def test_bad_top_level_values_are_collected_in_one_error():
    with pytest.raises(ConfigError) as err:
        parse_tours_section({"enabled": "yes", "concierge_port": 80, "max_ttl": "soon", "egress_allow": "pypi.org",
                             "max_usd_model_per_guest": 0, "bindings": {"Bad Name": GOOD}})
    message = err.value.message
    for fragment in ("tours.enabled", "concierge_port", "max_ttl", "egress_allow", "max_usd_model_per_guest", "binding name"):
        assert fragment in message


def test_unknown_top_level_keys_are_ignored_unless_strict():
    assert parse_tours_section({"engine_only_key": 1}).enabled is False
    with pytest.raises(ConfigError, match="unknown key 'engine_only_key'"):
        parse_tours_section({"engine_only_key": 1}, strict=True)


def test_path_matching_is_exact_or_a_segment_prefix():
    b = parse_tours_section({"bindings": {"a": GOOD}}).bindings["a"]
    assert b.path_allowed("/v1/messages") and b.path_allowed("/v1/models/abc") and b.path_allowed("/v1/models/a/b")
    assert not b.path_allowed("/v1/messages/") and not b.path_allowed("/v1/messagesx") and not b.path_allowed("/v1/models")
    assert not b.path_allowed("/v1/modelsx/abc") and not b.path_allowed("/V1/messages") and not b.path_allowed("")
