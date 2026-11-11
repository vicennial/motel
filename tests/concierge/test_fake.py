"""FakeConcierge must behave like the client for everything the engine relies on."""
import pytest

from motel.concierge.errors import AdminRefused, AttemptNotFound, ConciergeError
from motel.testing import FakeClock, FakeConcierge

ARGS = dict(tour="t-1", guest="g-1", attempt="a-1", bindings=["anthropic"], expires_at="2026-01-01T13:00:00Z", budget_usd=1.0)


@pytest.fixture
def fake():
    return FakeConcierge(clock=FakeClock())  # 2026-01-01T12:00:00Z


def test_issue_revoke_usage_health(fake):
    token = fake.issue(**ARGS)
    assert token.startswith("mt_") and fake.token_for("a-1") == token and fake.is_valid(token)
    assert fake.usage(attempt="a-1") == {"usd": 0.0, "input_tokens": 0, "output_tokens": 0, "requests": 0, "refused": 0}
    fake.spend("a-1", 0.25, input_tokens=10, output_tokens=5)
    fake.spend("a-1", 0.0, refused=1, requests=0)
    assert fake.usage(attempt="a-1") == {"usd": 0.25, "input_tokens": 10, "output_tokens": 5, "requests": 1, "refused": 1}
    assert fake.health()["live_tokens"] == 1
    assert fake.revoke(attempt="a-1") == 1 and fake.revoke(attempt="a-1") == 0 and not fake.is_valid(token)
    assert [name for name, _ in fake.calls] == ["issue", "usage", "usage", "health", "revoke", "revoke"]


def test_endpoint_mirrors_the_real_call(fake):
    """Same shape as ConciergeClient.endpoint: {"port": int}; unknown attempt -> not found; revoked or expired -> kind "gone"."""
    assert isinstance(fake.issue(**ARGS), str)  # issue still returns the token alone
    fake.issue(**{**ARGS, "attempt": "a-2"})
    first, second = fake.endpoint(attempt="a-1"), fake.endpoint(attempt="a-2")
    assert set(first) == {"port"} and isinstance(first["port"], int) and first != second
    assert fake.endpoint(attempt="a-1") == first  # stable for an attempt, like the real listener's port
    with pytest.raises(AttemptNotFound):
        fake.endpoint(attempt="missing")
    fake.revoke(attempt="a-1")
    with pytest.raises(AdminRefused) as err:
        fake.endpoint(attempt="a-1")
    assert err.value.kind == "gone" and not isinstance(err.value, AttemptNotFound)
    fake.unavailable = True
    with pytest.raises(ConciergeError, match="not running"):
        fake.endpoint(attempt="a-2")
    assert [name for name, _ in fake.calls if name == "endpoint"] == ["endpoint"] * 6


def test_revoke_by_tour_and_expiry():
    clock = FakeClock()
    fake = FakeConcierge(clock=clock)
    fake.issue(**ARGS)
    fake.issue(**{**ARGS, "attempt": "a-2"})
    fake.issue(**{**ARGS, "attempt": "a-3", "tour": "t-2"})
    assert fake.revoke(tour="t-1") == 2
    assert fake.is_valid(fake.token_for("a-3"))
    clock.advance(hours=2)
    assert not fake.is_valid(fake.token_for("a-3"))


def test_validation_matches_the_real_registry(fake):
    with pytest.raises(AdminRefused, match="unknown binding"):
        fake.issue(**{**ARGS, "bindings": ["other"]})
    with pytest.raises(AdminRefused, match="past"):
        fake.issue(**{**ARGS, "expires_at": "2025-01-01T00:00:00Z"})
    fake.issue(**ARGS)
    with pytest.raises(AdminRefused) as err:
        fake.issue(**ARGS)
    assert err.value.kind == "exists"
    with pytest.raises(AttemptNotFound):
        fake.usage(attempt="missing")
    with pytest.raises(AdminRefused):
        fake.revoke()
    capped = FakeConcierge(clock=FakeClock(), max_budget_usd=0.5)
    with pytest.raises(AdminRefused, match="ceiling"):
        capped.issue(**ARGS)


def test_unavailable_mode_raises_like_a_dead_socket(fake):
    fake.unavailable = True
    for call in (lambda: fake.health(), lambda: fake.usage(attempt="a"), lambda: fake.revoke(attempt="a"), lambda: fake.issue(**ARGS)):
        with pytest.raises(ConciergeError, match="not running"):
            call()


def test_signatures_match_the_client():
    import inspect

    from motel.concierge.admin import ConciergeClient
    for name in ("issue", "revoke", "usage", "health", "endpoint"):
        real, fake = inspect.signature(getattr(ConciergeClient, name)), inspect.signature(getattr(FakeConcierge, name))
        assert list(real.parameters) == list(fake.parameters), name
        assert {k: p.default for k, p in real.parameters.items()} == {k: p.default for k, p in fake.parameters.items()}, name


def test_a_lost_listener_is_revoked_and_reported_like_the_real_concierge(fake):
    fake.issue(**ARGS)
    fake.lose_listener(ARGS["attempt"])
    assert list(fake.health()["lost_listeners"]) == [ARGS["attempt"]]
    with pytest.raises(AdminRefused) as info:
        fake.endpoint(attempt=ARGS["attempt"])
    assert info.value.kind == "gone"
