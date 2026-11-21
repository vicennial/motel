"""The spec key ``model``: shape, defaults versus per guest, the command adapter, and the wire form."""
import pytest

from motel.tours import spec
from motel.tours.errors import Usage

GOOD = ["claude-sonnet-5-5", "claude-opus-5-5[1m]", "sonnet", "gpt-6.1-sol", "a", "x" * 64, "vendor:model-1.2_b", "9lives"]
BAD = ["", "-x", "--model", "-", " sonnet", "sonnet ", "a b", "a;b", "a$(b)", "a/b", "a\nb", "a\x00b", "x" * 65, "é", 5, True, ["a"], {}]


def parse(guests, **defaults):
    return spec.parse({"tour": {"name": "m"}, "defaults": defaults, "guests": guests})


def one(**over):
    return {"id": "a", "prompt": "p", "secrets": ["anthropic"], "budget_usd": 1, **over}


@pytest.mark.parametrize("value", GOOD)
def test_good_model_ids_are_accepted_in_defaults_and_per_guest(value):
    assert parse([one()], model=value).guests[0].model == value
    assert parse([one(model=value)]).guests[0].model == value
    assert parse([one(adapter="codex", model=value)]).guests[0].model == value


@pytest.mark.parametrize("value", BAD)
def test_bad_model_ids_are_refused_in_defaults_and_per_guest(value):
    with pytest.raises(Usage, match="model"):
        parse([one()], model=value)
    with pytest.raises(Usage, match="model"):
        parse([one(model=value)])


def test_a_guest_value_overrides_the_default_and_others_keep_it():
    guests = parse([one(), one(id="b", model="haiku"), one(id="c", adapter="codex")], model="sonnet").guests
    assert [g.model for g in guests] == ["sonnet", "haiku", "sonnet"]


def test_the_command_adapter_takes_no_model():
    command = {"id": "lint", "adapter": "command", "argv": ["make", "lint"], "secrets": [], "budget_usd": 0}
    with pytest.raises(Usage, match=r"guest lint: `model` belongs to the claude and codex adapters"):
        parse([{**command, "model": "sonnet"}])
    with pytest.raises(Usage, match="`model` belongs to the claude and codex adapters"):
        parse([command], model="sonnet")       # a default reaches the command guest as well
    assert parse([command]).guests[0].model is None


def test_an_omitted_model_changes_nothing_in_the_wire_form():
    guest = parse([one()]).guests[0]
    assert guest.model is None and "model" not in guest.to_wire()
    assert set(guest.to_wire()) == {"id", "adapter", "env", "secrets", "egress", "server_type", "image", "max_turns", "artifacts",
                                    "base", "bundle", "auth", "prompt", "budget_usd"}   # the keys of a spec that never heard of model


def test_the_wire_form_carries_the_model_and_the_desk_checks_it_again():
    parsed = parse([one(model="claude-sonnet-5-5")])
    parsed.guests[0].base, parsed.guests[0].bundle = "b" * 40, "0" * 16
    wire = parsed.to_wire()
    assert wire["guests"][0]["model"] == "claude-sonnet-5-5"
    assert spec.parse(wire, wire=True).guests[0].model == "claude-sonnet-5-5"
    for bad in ("-x", "", "a b", "x" * 65, 7):
        forged = {**wire, "guests": [{**wire["guests"][0], "model": bad}]}
        with pytest.raises(Usage, match="model"):
            spec.parse(forged, wire=True)
