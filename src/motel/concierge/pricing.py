"""Model price table and cost arithmetic. Prices are USD per million tokens.

The Claude entries come from the ``claude-api`` skill's model reference, whose price table is dated
``PRICING_AS_OF``. Only models that reference states a price for are listed; an older or newer model is **not** guessed
from memory and is billed at ``DEFAULT_PRICE``. The OpenAI entries are best-effort and not from that source
(``OPENAI_PRICING_AS_OF``, unverified). Prices go stale: re-check this table when you add a model.

A model id is matched exactly, or with a date, ``-latest``, ``[...]`` or ``@...`` suffix (so ``claude-sonnet-4-6`` and a
dated snapshot match, but a future ``claude-opus-5-6`` does not match ``claude-opus-5``). The operator overrides or
extends the table per binding (``[tours.bindings.NAME.pricing."model-prefix"]``, plain prefix match). A model that
matches nothing is billed at ``DEFAULT_PRICE``: deliberately high, so an unknown or newly released model can never be
cheaper than the budget thinks. An estimate that is too high only makes the concierge refuse early; one that is too low
lets spend run past the budget.

Long prompts: a price may carry a :class:`LongContext` rule (threshold, input multiplier, output multiplier) that
applies to the whole request once the prompt is over the threshold. The source states one for Claude Haiku 5.5 (over
100K tokens: USD 0.50 / 2.50 instead of 0.10 / 0.50) and states none for Claude Opus 4.8. For the other Claude models it
says nothing either way, so they carry ``ASSUMED_LONG_CONTEXT`` (over 200K tokens: input x2, output x1.5), which can only
over-charge. Operator-supplied prices carry no rule unless the operator's numbers already include it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, replace

PRICING_AS_OF = "2026-10-06"  # date of the claude-api skill's model/price table the Claude entries come from
OPENAI_PRICING_AS_OF = "2025-10"  # unverified, from memory
MILLION = 1_000_000


@dataclass(frozen=True)
class LongContext:
    """Once the prompt is over ``threshold`` tokens the whole request is billed at these multiples."""

    threshold: int
    input_mult: float
    output_mult: float


ASSUMED_LONG_CONTEXT = LongContext(200_000, 2.0, 1.5)  # not sourced: a deliberate over-estimate, see the module docstring


@dataclass(frozen=True)
class Price:
    """USD per million tokens. Cache prices default from the input price when built with :meth:`make`."""

    input: float
    output: float
    cache_read: float
    cache_write_5m: float
    cache_write_1h: float
    long_context: LongContext | None = None

    @classmethod
    def make(cls, input: float, output: float, cache_read: float | None = None, cache_write_5m: float | None = None,
             cache_write_1h: float | None = None, long_context: LongContext | None = None) -> Price:
        # No discount for cache reads unless one is stated; writes cost more than a plain input token.
        return cls(float(input), float(output),
                   float(input if cache_read is None else cache_read),
                   float(input * 1.25 if cache_write_5m is None else cache_write_5m),
                   float(input * 2.0 if cache_write_1h is None else cache_write_1h), long_context)

    def scaled(self, factor: float) -> Price:
        """Every rate times ``factor`` (fast mode and similar service levels)."""
        return replace(self, input=self.input * factor, output=self.output * factor, cache_read=self.cache_read * factor,
                       cache_write_5m=self.cache_write_5m * factor, cache_write_1h=self.cache_write_1h * factor)

    def for_input(self, prompt_tokens: int) -> Price:
        """The price that applies to a request whose prompt has ``prompt_tokens`` tokens (cache tokens included)."""
        rule = self.long_context
        if rule is None or prompt_tokens <= rule.threshold:
            return self
        return replace(self, input=self.input * rule.input_mult, output=self.output * rule.output_mult,
                       cache_read=self.cache_read * rule.input_mult, cache_write_5m=self.cache_write_5m * rule.input_mult,
                       cache_write_1h=self.cache_write_1h * rule.input_mult, long_context=None)


def _claude(inp: float, out: float, cache_read: float | None = None, long_context: LongContext | None = ASSUMED_LONG_CONTEXT) -> Price:
    # Cache reads default to 0.1x input, writes to 1.25x (5 minutes) and 2x (1 hour): the skill's general rule.
    return Price.make(inp, out, cache_read=inp * 0.1 if cache_read is None else cache_read, cache_write_5m=inp * 1.25,
                      cache_write_1h=inp * 2.0, long_context=long_context)


def _openai(inp: float, out: float, cached: float) -> Price:
    return Price.make(inp, out, cache_read=cached, cache_write_5m=inp, cache_write_1h=inp)


# Highest list price in the table, no cache discount, long-context premium assumed. See the module docstring.
DEFAULT_PRICE = Price.make(15.0, 75.0, long_context=ASSUMED_LONG_CONTEXT)

BUILTIN_PRICES: dict[str, Price] = {
    # Claude: prices and cache-read rates as stated by the source (see PRICING_AS_OF).
    "claude-fable-5-1": _claude(10, 50, cache_read=0.25),
    "claude-mythos-5-1": _claude(10, 50, cache_read=0.25),
    "claude-fable-5": _claude(10, 50, cache_read=1.0),
    "claude-mythos-5": _claude(10, 50, cache_read=1.0),
    "claude-opus-5-5": _claude(4, 20, cache_read=0.20),
    "claude-opus-5": _claude(5, 25),
    "claude-opus-4-8": _claude(5, 25, long_context=None),  # the source: 1M context at standard pricing, no premium
    "claude-opus-4-7": _claude(5, 25),
    "claude-opus-4-6": _claude(5, 25),
    "claude-sonnet-5-5": _claude(2, 10, cache_read=0.20),
    "claude-sonnet-5": _claude(2, 10),
    "claude-sonnet-4-6": _claude(3, 15),
    "claude-haiku-5-5": _claude(0.10, 0.50, long_context=LongContext(100_000, 5.0, 5.0)),  # the source: $0.50 / $2.50 over 100K
    "claude-haiku-4-5": _claude(1, 5, long_context=None),  # 200K context window: the threshold cannot be reached
    # OpenAI: not from the Claude source; unverified (OPENAI_PRICING_AS_OF).
    "gpt-5-nano": _openai(0.05, 0.4, 0.005),
    "gpt-5-mini": _openai(0.25, 2, 0.025),
    "gpt-5-codex": _openai(1.25, 10, 0.125),
    "gpt-5": _openai(1.25, 10, 0.125),
    "gpt-4.1-nano": _openai(0.1, 0.4, 0.025),
    "gpt-4.1-mini": _openai(0.4, 1.6, 0.1),
    "gpt-4.1": _openai(2, 8, 0.5),
    "gpt-4o-mini": _openai(0.15, 0.6, 0.075),
    "gpt-4o": _openai(2.5, 10, 1.25),
    "o4-mini": _openai(1.1, 4.4, 0.275),
    "o3-mini": _openai(1.1, 4.4, 0.55),
    "o3": _openai(2, 8, 0.5),
    "o1": _openai(15, 60, 7.5),
}

_SUFFIX = re.compile(r"^(-\d{4}-\d{2}-\d{2}|-\d{8}|-latest|\[[^\]]*\]|@.*)$")


def _matches(name: str, key: str, *, exact: bool) -> bool:
    key = key.lower()
    if not name.startswith(key):
        return False
    return not exact or name == key or bool(_SUFFIX.fullmatch(name[len(key):]))


def lookup(model: str | None, overrides: dict[str, Price] | None = None,
           default: Price = DEFAULT_PRICE) -> tuple[Price, bool]:
    """Return ``(price, known)`` for a model id. Operator overrides (prefix match) win over the built-in table."""
    name = (model or "").strip().lower()
    if name:
        for table, exact in ((overrides or {}, False), (BUILTIN_PRICES, True)):
            best = max((key for key in table if _matches(name, key, exact=exact)), key=len, default=None)
            if best is not None:
                return table[best], True
    return default, False


def cost_usd(price: Price, *, input_tokens: int = 0, output_tokens: int = 0, cache_read: int = 0, cache_write_5m: int = 0,
             cache_write_1h: int = 0, extra_usd: float = 0.0) -> float:
    """USD for already-normalised token counts (``input_tokens`` excludes cached tokens).

    The long-context rule, if the price has one, is applied from the prompt size (all input kinds added up).
    """
    price = price.for_input(input_tokens + cache_read + cache_write_5m + cache_write_1h)
    total = (input_tokens * price.input + output_tokens * price.output + cache_read * price.cache_read
             + cache_write_5m * price.cache_write_5m + cache_write_1h * price.cache_write_1h)
    return total / MILLION + extra_usd
