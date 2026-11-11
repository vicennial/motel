"""Tokens, budgets, revocation and restart semantics (the registry replays its ledger)."""
import json
import os

import pytest

from motel.concierge import ledger as ledger_mod
from motel.concierge.errors import AdminRefused, AttemptNotFound
from motel.concierge.registry import Refusal, Registry, hash_token

NOW = 1_800_000_000.0
FUTURE = "2027-01-15T10:00:00Z"  # NOW + a few hours


def iso(offset):
    from datetime import UTC, datetime
    return datetime.fromtimestamp(NOW + offset, UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


@pytest.fixture
def clock():
    state = [NOW]
    fn = lambda: state[0]  # noqa: E731
    fn.state = state
    return fn


def make(tmp_path, clock, **kw):
    led = ledger_mod.Ledger(str(tmp_path / "ledger.jsonl"), fsync=False, clock=clock)
    return Registry(led, known_bindings={"anthropic", "openai"}, max_budget_usd=kw.pop("max_budget_usd", 5.0), clock=clock), led


def issue(reg, attempt="a-1", **kw):
    args = dict(tour="t-1", guest="g-1", attempt=attempt, bindings=["anthropic"], expires_at=iso(3600), budget_usd=1.0, max_concurrent=2)
    args.update(kw)
    return reg.issue(**args)


def begin(reg, att, req="r1", usd=0.1):
    reg.begin(att, req=req, binding="anthropic", host_key="h:1", host_limit=8, reserve_usd=usd, path="/v1/messages", model="m")


@pytest.mark.parametrize("patch,fragment", [
    ({"tour": "../x"}, "bad tour"), ({"guest": ""}, "bad guest"), ({"attempt": "a b"}, "bad attempt"),
    ({"bindings": []}, "bindings"), ({"bindings": ["nope"]}, "unknown binding"), ({"bindings": ["anthropic", "anthropic"]}, "distinct"),
    ({"budget_usd": 0}, "budget_usd"), ({"budget_usd": float("nan")}, "budget_usd"), ({"budget_usd": True}, "budget_usd"),
    ({"budget_usd": 50.0}, "ceiling"), ({"max_concurrent": 0}, "max_concurrent"), ({"max_concurrent": 99}, "max_concurrent"),
    ({"expires_at": "yesterday"}, "ISO-8601"), ({"expires_at": iso(-5)}, "past"), ({"expires_at": iso(30 * 86400)}, "hours away"),
])
def test_issue_validation(tmp_path, clock, patch, fragment):
    reg, _ = make(tmp_path, clock)
    with pytest.raises(AdminRefused) as err:
        issue(reg, **patch)
    assert fragment in err.value.message and err.value.kind == "invalid"


def test_issue_returns_a_secret_token_stores_only_its_hash_and_refuses_a_second_issue(tmp_path, clock):
    reg, led = make(tmp_path, clock)
    token = issue(reg)
    assert token.startswith("mt_") and len(token) > 40 and issue(reg, "a-2") != token
    assert reg.lookup(token).attempt == "a-1" and reg.lookup(token + "x") is None and reg.lookup("") is None
    with pytest.raises(AdminRefused) as err:
        issue(reg)
    assert err.value.kind == "exists"
    text = (tmp_path / "ledger.jsonl").read_text()
    assert token not in text and hash_token(token) in text


def test_revoke_by_attempt_and_by_tour_counts_only_new_revocations(tmp_path, clock):
    reg, _ = make(tmp_path, clock)
    for i, tour in enumerate(("t-1", "t-1", "t-2")):
        issue(reg, f"a-{i}", tour=tour)
    assert reg.revoke(attempt="a-0") == 1 and reg.revoke(attempt="a-0") == 0
    assert reg.revoke(tour="t-1") == 1  # a-0 was already revoked
    assert reg.revoke(tour="t-9") == 0 and reg.revoke(tour="t-2", attempt="a-0") == 0
    with pytest.raises(AdminRefused):
        reg.revoke()


def test_usage_of_an_unknown_attempt_is_not_found(tmp_path, clock):
    reg, _ = make(tmp_path, clock)
    with pytest.raises(AttemptNotFound):
        reg.usage("nope")


def test_reservation_settle_and_budget_arithmetic(tmp_path, clock):
    reg, _ = make(tmp_path, clock)
    att = reg.lookup(issue(reg, budget_usd=1.0))
    begin(reg, att, "r1", 0.6)
    assert reg.usage("a-1")["usd"] == pytest.approx(0.6)  # in-flight reservations count (worst case)
    with pytest.raises(Refusal) as err:
        begin(reg, att, "r2", 0.5)
    assert err.value.kind == "budget_exhausted" and err.value.status == 403
    reg.finish(att, req="r1", host_key="h:1", reserved_usd=0.6, actual_usd=0.1, input_tokens=10, output_tokens=5)
    begin(reg, att, "r2", 0.5)  # the unused reservation came back
    assert reg.usage("a-1") == {"usd": pytest.approx(0.6), "input_tokens": 10, "output_tokens": 5, "requests": 2, "refused": 1}


def test_concurrency_limits(tmp_path, clock):
    reg, _ = make(tmp_path, clock)
    att = reg.lookup(issue(reg, max_concurrent=1))
    begin(reg, att, "r1", 0.01)
    with pytest.raises(Refusal) as err:
        begin(reg, att, "r2", 0.01)
    assert (err.value.kind, err.value.status) == ("concurrency", 429)
    other = reg.lookup(issue(reg, "a-2", max_concurrent=4))
    with pytest.raises(Refusal) as err:
        reg.begin(other, req="x", binding="anthropic", host_key="h:1", host_limit=1, reserve_usd=0.01, path="/p", model=None)
    assert err.value.kind == "host_busy"  # a-1 holds the only slot on h:1
    reg.finish(att, req="r1", host_key="h:1", reserved_usd=0.01, actual_usd=0)
    begin(reg, att, "r3", 0.01)


def test_expiry_and_revocation_checks_use_the_clock(tmp_path, clock):
    reg, _ = make(tmp_path, clock)
    att = reg.lookup(issue(reg, expires_at=iso(60)))
    assert reg.state_refusal(att) is None
    clock.state[0] += 61
    assert reg.state_refusal(att).kind == "token_expired"
    with pytest.raises(Refusal):
        begin(reg, att)


def test_restart_keeps_tokens_budgets_and_revocations(tmp_path, clock):
    reg, led = make(tmp_path, clock)
    t1, t2 = issue(reg, "a-1", budget_usd=1.0), issue(reg, "a-2", tour="t-2")
    a1 = reg.lookup(t1)
    begin(reg, a1, "r1", 0.4)
    reg.finish(a1, req="r1", host_key="h:1", reserved_usd=0.4, actual_usd=0.25, input_tokens=100, output_tokens=40)
    reg.refuse(a1, kind="path_denied", status=403)
    reg.revoke(tour="t-2")
    led.close()
    reg2, _ = make(tmp_path, clock)
    assert reg2.lookup(t1).attempt == "a-1" and reg2.state_refusal(reg2.lookup(t1)) is None
    assert reg2.state_refusal(reg2.lookup(t2)).kind == "token_revoked"
    assert reg2.usage("a-1") == {"usd": pytest.approx(0.25), "input_tokens": 100, "output_tokens": 40, "requests": 1, "refused": 1}
    with pytest.raises(Refusal):
        begin(reg2, reg2.lookup(t1), "r9", 0.8)  # the spent amount survived: 0.25 + 0.8 > 1.0


def test_a_request_in_flight_at_a_crash_is_charged_its_full_reservation(tmp_path, clock):
    reg, led = make(tmp_path, clock)
    att = reg.lookup(issue(reg, budget_usd=1.0))
    begin(reg, att, "r1", 0.7)
    led.close()  # "crash": no settle was written
    reg2, led2 = make(tmp_path, clock)
    assert reg2.usage("a-1")["usd"] == pytest.approx(0.7)
    settles = [e for e in ledger_mod.read_events(led2.path) if e["ev"] == "settle"]
    assert settles[-1]["outcome"] == "restart" and settles[-1]["usd"] == pytest.approx(0.7)
    led2.close()
    reg3, _ = make(tmp_path, clock)  # a second restart must not charge it again
    assert reg3.usage("a-1")["usd"] == pytest.approx(0.7)


def test_old_finished_attempts_are_forgotten_on_restart(tmp_path, clock):
    reg, led = make(tmp_path, clock)
    token = issue(reg, expires_at=iso(60))
    led.close()
    clock.state[0] += 60 + 25 * 3600
    reg2, _ = make(tmp_path, clock)
    assert reg2.lookup(token) is None
    with pytest.raises(AttemptNotFound):
        reg2.usage("a-1")


def test_a_torn_last_line_and_garbage_are_survivable(tmp_path, clock):
    reg, led = make(tmp_path, clock)
    token = issue(reg)
    led.close()
    with open(tmp_path / "ledger.jsonl", "ab") as handle:
        handle.write(b'not json\n{"ev":"settle","attempt":"a-1","usd":"x"}\n{"v":1,"ev":"reserve","attempt":"a-1","re')
    reg2, led2 = make(tmp_path, clock)
    assert reg2.lookup(token) is not None
    issue(reg2, "a-2")  # appending after a torn line must start a new line
    led2.close()
    assert "a-2" in [e.get("attempt") for e in ledger_mod.read_events(str(tmp_path / "ledger.jsonl"))]


def test_compaction_carries_live_state_into_a_fresh_file(tmp_path, clock, monkeypatch):
    reg, led = make(tmp_path, clock)
    token = issue(reg)
    att = reg.lookup(token)
    begin(reg, att, "r1", 0.2)
    reg.finish(att, req="r1", host_key="h:1", reserved_usd=0.2, actual_usd=0.15, input_tokens=5, output_tokens=6)
    led.close()
    monkeypatch.setattr(ledger_mod, "COMPACT_BYTES", 1)
    reg2, led2 = make(tmp_path, clock)
    rotated = [n for n in os.listdir(tmp_path) if n.startswith("ledger.jsonl.")]
    assert len(rotated) == 1 and reg2.usage("a-1")["usd"] == pytest.approx(0.15)
    led2.close()
    monkeypatch.setattr(ledger_mod, "COMPACT_BYTES", 10**9)
    reg3, _ = make(tmp_path, clock)
    assert reg3.usage("a-1")["usd"] == pytest.approx(0.15) and reg3.lookup(token) is not None
    kinds = [json.loads(line)["ev"] for line in (tmp_path / "ledger.jsonl").read_text().splitlines()]
    assert "snapshot" in kinds and kinds.count("issue") == 0


def test_health_counts(tmp_path, clock):
    reg, _ = make(tmp_path, clock)
    issue(reg)
    reg.lookup("mt_unknown")
    health = reg.health()
    assert health["status"] == "ok" and health["live_tokens"] == 1 and health["unknown_token_refusals"] == 1
