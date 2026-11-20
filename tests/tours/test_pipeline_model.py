"""The ``model`` field on the whole engine rig: the card, the result record, status and the text output."""
import pytest

from motel.tours.cli import render_result, render_tour

G = {"id": "fix-auth", "prompt": "p", "secrets": ["anthropic"], "budget_usd": 0.5}


def test_an_omitted_model_leaves_the_card_status_and_result_as_they_were(rig):
    doc = rig.run_tour()
    assert "model" not in rig.transport.cards[0]
    assert all("model" not in g for g in rig.service.status(rig.alice, doc["id"])["tour"]["guests"])
    record = rig.result_record(doc["id"], "fix-auth")
    assert "model" not in record
    assert " · model " not in render_result(record) and "  model " not in render_tour(rig.service.status(rig.alice, doc["id"])["tour"])


def test_a_default_model_reaches_the_card_status_and_result(rig):
    doc = rig.run_tour([G], model="claude-sonnet-5-5")
    assert rig.transport.cards[0]["model"] == "claude-sonnet-5-5"
    tour = rig.service.status(rig.alice, doc["id"])["tour"]
    assert tour["guests"][0]["model"] == "claude-sonnet-5-5"
    assert "model claude-sonnet-5-5" in render_tour(tour)
    record = rig.result_record(doc["id"], "fix-auth")
    assert record["model"] == "claude-sonnet-5-5" and "model claude-sonnet-5-5" in render_result(record)


def test_a_guest_model_overrides_the_default_and_guests_differ(rig):
    doc = rig.run_tour([{**G, "id": "a", "model": "haiku"}, {**G, "id": "b"}, {**G, "id": "c", "adapter": "codex", "secrets": [], "budget_usd": 0}],
                       model="sonnet")
    models = {c["guest"]: c["model"] for c in rig.transport.cards}
    assert models == {"a": "haiku", "b": "sonnet", "c": "sonnet"}
    assert {g["id"]: g["model"] for g in rig.service.status(rig.alice, doc["id"])["tour"]["guests"]} == models


def test_a_forged_model_in_a_started_tour_is_refused_by_the_desk(rig):
    from motel.tours.errors import Usage
    wire = rig.wire([G], model="sonnet")
    wire["spec"]["guests"][0]["model"] = "--dangerously-anything"
    with pytest.raises(Usage, match="model"):
        rig.service.start(rig.alice, wire)
