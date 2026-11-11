"""Spec parsing and validation. A tour may ask for less than an operator ceiling, never more."""
import json
from datetime import timedelta
from decimal import Decimal

import pytest

from motel.tours import spec
from motel.tours.config import Limits
from motel.tours.errors import Refused, Usage

LIMITS = Limits(enabled=True, bindings=("anthropic", "openai"), egress_allow=("github.com", "*.pythonhosted.org"),
                max_guests_per_tour=3, max_ttl=timedelta(hours=2), max_usd_model_per_guest=Decimal("2.0"),
                own_login_enabled=False)
TYPES = ("cpx32", "cpx52")

TOML = """
[tour]
name = "fix-flaky-tests"

[defaults]
adapter = "claude"
ttl = "45m"
budget_usd = 1.5
secrets = ["anthropic"]
egress = ["github.com"]
artifacts = ["reports/*.json"]
[defaults.env]
CI = "1"

[[guests]]
id = "fix-auth"
prompt = "Fix the failing auth tests."
base = "HEAD"

[[guests]]
id = "lint"
adapter = "command"
argv = ["make", "lint"]
secrets = []
budget_usd = 0
"""


def doc(text=TOML):
    return spec.load_text(text)


def test_toml_and_json_give_the_same_spec():
    from_toml = spec.parse(doc(), limits=LIMITS, cabin_types=TYPES)
    as_json = spec.load_text(json.dumps({"tour": {"name": "fix-flaky-tests"}, "defaults": {
        "adapter": "claude", "ttl": "45m", "budget_usd": 1.5, "secrets": ["anthropic"], "egress": ["github.com"],
        "artifacts": ["reports/*.json"], "env": {"CI": "1"}}, "guests": [
        {"id": "fix-auth", "prompt": "Fix the failing auth tests.", "base": "HEAD"},
        {"id": "lint", "adapter": "command", "argv": ["make", "lint"], "secrets": [], "budget_usd": 0}]}))
    assert spec.parse(as_json, limits=LIMITS, cabin_types=TYPES) == from_toml
    guest = from_toml.guests[0]
    assert (guest.ttl, guest.budget_usd, guest.env, guest.artifacts) == (timedelta(minutes=45), Decimal("1.5"), {"CI": "1"},
                                                                           ["reports/*.json"])
    assert from_toml.guests[1].secrets == [] and from_toml.guests[1].budget_usd == 0


def test_defaults_are_filled_from_the_ceilings_by_the_desk_only():
    d = {"guests": [{"id": "a", "prompt": "do it", "secrets": ["anthropic"]}]}
    user = spec.parse(d)                                   # the CLI does not know the ceilings
    assert user.guests[0].ttl is None and user.guests[0].budget_usd is None
    desk = spec.parse(d, limits=LIMITS, cabin_types=TYPES)
    assert desk.guests[0].ttl == timedelta(minutes=30) and desk.guests[0].budget_usd == Decimal("1.0")


@pytest.mark.parametrize("change,message", [
    (lambda d: d["guests"][0].update(ttl="3h"), "ttl is above the ceiling"),
    (lambda d: d["guests"][0].update(budget_usd=2.5), "above the ceiling of 2.0"),
    (lambda d: d["defaults"].update(secrets=["stripe"]), "no such binding: stripe"),
    (lambda d: d["defaults"].update(egress=["evil.example.com"]), "egress to evil.example.com is not allowed"),
    (lambda d: d["guests"][0].update(server_type="cpx999"), "server type 'cpx999' is not allowed"),
    (lambda d: d["guests"].extend([{"id": f"g{i}", "prompt": "x", "secrets": []} for i in range(3)]), "above the limit of 3 per tour"),
    (lambda d: d["guests"][0].update(auth="own-login"), "own-login is not enabled"),
])
def test_a_tour_cannot_raise_a_ceiling(change, message):
    d = doc()
    change(d)
    if d["guests"][0].get("auth"):
        d["guests"][0].update(secrets=[], budget_usd=0)
    with pytest.raises(Refused, match=message):
        spec.parse(d, limits=LIMITS, cabin_types=TYPES)


def test_the_ceiling_is_the_operators_not_the_specs():
    """Nothing in the spec (keys, env, or odd values) can name a ceiling or change one."""
    for key in ("max_ttl", "max_eur_per_tour", "ceilings", "limits", "max_guests_per_tour"):
        d = doc()
        d["defaults"][key] = 99999
        with pytest.raises(Usage, match="unknown key"):
            spec.parse(d, limits=LIMITS, cabin_types=TYPES)
    d = doc()
    d["tour"]["max_ttl"] = "99h"
    with pytest.raises(Usage, match="unknown key"):
        spec.parse(d, limits=LIMITS)


def test_wildcard_egress_entries_of_the_operator_match_subdomains():
    d = doc()
    d["defaults"]["egress"] = ["files.pythonhosted.org"]
    assert spec.parse(d, limits=LIMITS, cabin_types=TYPES).guests[0].egress == ["files.pythonhosted.org"]


@pytest.mark.parametrize("change", [
    lambda d: d["guests"][0].update(id="Bad_ID"),
    lambda d: d["guests"][0].update(id="x" * 40),
    lambda d: d["guests"].append(dict(d["guests"][0])),                       # duplicate id
    lambda d: d["guests"][0].update(prompt=""),
    lambda d: d["guests"][0].update(argv=["ls"]),                             # argv belongs to the command adapter
    lambda d: d["guests"][1].update(prompt="not for command"),
    lambda d: d["guests"][1].update(argv=[]),
    lambda d: d["guests"][0].update(adapter="gpt"),
    lambda d: d["guests"][0].update(ttl="forever"),
    lambda d: d["guests"][0].update(max_turns=0),
    lambda d: d["guests"][0].update(max_turns=True),
    lambda d: d["guests"][0].update(budget_usd=-1),
    lambda d: d["guests"][0].update(budget_usd="lots"),
    lambda d: d["guests"][0].update(artifacts=["../etc/passwd"]),
    lambda d: d["guests"][0].update(artifacts=["/etc/passwd"]),
    lambda d: d["guests"][0].update(artifacts=["a//b"]),
    lambda d: d["guests"][0].update(base="--upload-pack=x"),
    lambda d: d["guests"][0].update(egress=["not a host"]),
    lambda d: d["guests"][0].update(secrets=["anthropic"], budget_usd=0),
    lambda d: d["guests"][1].update(budget_usd=1),                            # budget with no binding
    lambda d: d.pop("guests"),
    lambda d: d.update(guests=[]),
    lambda d: d["tour"].update(name="bad\nname"),
    lambda d: d.update(extra=1),
])
def test_malformed_specs_are_usage_errors(change):
    d = doc()
    change(d)
    with pytest.raises(Usage):
        spec.parse(d, limits=LIMITS, cabin_types=TYPES)


@pytest.mark.parametrize("name", ["ANTHROPIC_API_KEY", "ANTHROPIC_BASE_URL", "OPENAI_API_KEY", "MY_TOKEN", "DB_PASSWORD", "PATH",
                                  "LD_PRELOAD", "AWS_REGION", "GITHUB_PAT", "HTTPS_PROXY"])
def test_env_cannot_carry_credentials_or_redirect_endpoints(name):
    d = doc()
    d["guests"][0]["env"] = {name: "x"}
    with pytest.raises(Usage, match="reserved or looks like a secret"):
        spec.parse(d, limits=LIMITS, cabin_types=TYPES)


def test_env_limits():
    d = doc()
    d["guests"][0]["env"] = {f"V{i}": "x" for i in range(40)}
    with pytest.raises(Usage):
        spec.parse(d, limits=LIMITS)
    d["guests"][0]["env"] = {"V": "x" * 5000}
    with pytest.raises(Usage):
        spec.parse(d, limits=LIMITS)


def test_the_wire_shape_is_validated_again_by_the_desk():
    wire = spec.parse(doc(), limits=LIMITS, cabin_types=TYPES)
    for guest in wire.guests:
        guest.base, guest.bundle = "a" * 40, "0123456789abcdef"
    document = wire.to_wire()
    assert spec.parse(document, limits=LIMITS, cabin_types=TYPES, wire=True).guests[0].base == "a" * 40
    document["guests"][0]["ttl_s"] = 3 * 3600                       # a lying CLI cannot raise the ceiling
    with pytest.raises(Refused):
        spec.parse(document, limits=LIMITS, cabin_types=TYPES, wire=True)
    document = wire.to_wire()
    document["guests"][0]["base"] = "HEAD"
    with pytest.raises(Usage, match="40-character"):
        spec.parse(document, limits=LIMITS, cabin_types=TYPES, wire=True)
    document = wire.to_wire()
    document["guests"][0]["repo"] = "/etc"                          # paths do not exist in the wire shape
    with pytest.raises(Usage, match="unknown key"):
        spec.parse(document, limits=LIMITS, cabin_types=TYPES, wire=True)


def test_text_that_is_neither_toml_nor_json_is_a_usage_error():
    with pytest.raises(Usage):
        spec.load_text("[[guests")
    with pytest.raises(Usage):
        spec.load_text('{"guests": ')
    with pytest.raises(Usage):
        spec.load_text("x" * (3 * 1024 * 1024))


def test_limits_read_the_contract_keys_from_a_dict_or_an_object():
    section = {"enabled": True, "max_ttl": "90m", "max_eur_per_tour": 2.5, "egress_allow": ["GitHub.com"],
               "bindings": {"anthropic": {"upstream": "https://x.invalid"}}, "own_login": {"enabled": True, "max_ttl": "30m"}}
    limits = Limits.from_section(section)
    assert (limits.max_ttl, limits.max_eur_per_tour, limits.egress_allow, limits.bindings) == (
        timedelta(minutes=90), Decimal("2.5"), ("github.com",), ("anthropic",))
    assert limits.own_login_enabled and limits.own_login_max_ttl == timedelta(minutes=30)

    class Obj:
        enabled, max_guests_per_tour = True, 7
    assert Limits.from_section(Obj()).max_guests_per_tour == 7
