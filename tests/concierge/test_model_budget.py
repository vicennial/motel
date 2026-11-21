"""A tour spec can name any model id (``model``), so the concierge must price every one of them. Nothing new is needed:
a model the price table does not know is billed at DEFAULT_PRICE (the highest rate), so it can only be refused sooner."""
import json

import pytest
from concierge_fakes import BODY, FakeUpstream, Harness, error_kind

from motel.concierge import pricing
from motel.tours.guest import MODEL_RE


@pytest.fixture
def upstream():
    up = FakeUpstream()
    yield up
    up.close()


@pytest.fixture
def h(upstream):
    harness = Harness(upstream.url)
    yield harness
    harness.close()


# A model id the spec accepts, whatever it is, always has a price; an unlisted one never gets a cheaper rate than the table's top.
@pytest.mark.parametrize("model", ["sonnet", "haiku", "gpt-6.1-sol", "claude-sonnet-9", "claude-opus-5-5[1m]", "a", "x" * 64,
                                   "claude-haiku-5-5-preview", "claude-sonnet-5-5.1"])
def test_every_model_id_a_spec_accepts_has_a_price_and_an_unlisted_one_gets_the_default(model):
    assert MODEL_RE.fullmatch(model)
    price, known = pricing.lookup(model)
    if known:
        assert price in pricing.BUILTIN_PRICES.values()
    else:
        assert price == pricing.DEFAULT_PRICE and (price.input, price.output) == (15.0, 75.0)


def test_an_unlisted_model_cannot_slip_a_request_past_a_budget_that_a_listed_one_fits(h, upstream):
    token_unlisted, token_listed = h.issue("a-1", budget=0.004), h.issue("a-2", budget=0.004)
    small = dict(BODY, max_tokens=100)          # listed sonnet 5.5 reserves about 0.001, the default rate about 0.0075
    status, _, body = h.post("/u/anthropic/v1/messages", token_unlisted, body=dict(small, model="claude-sonnet-9"))
    assert status == 403 and error_kind(body) == "motel_budget_exhausted"
    assert upstream.requests == []
    status, _, _ = h.post("/u/anthropic/v1/messages", token_listed, body=dict(small, model="claude-sonnet-5-5"))
    assert status == 200 and len(upstream.requests) == 1


def test_an_alias_that_reaches_the_proxy_unresolved_is_priced_high_too(h, upstream):
    status, _, body = h.post("/u/anthropic/v1/messages", h.issue(budget=0.004), body=dict(BODY, model="sonnet", max_tokens=100))
    assert status == 403 and error_kind(body) == "motel_budget_exhausted" and upstream.requests == []
    assert json.loads(body)["error"]["message"]
