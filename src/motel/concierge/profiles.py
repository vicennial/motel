"""Named request profiles: the ``betas`` and ``allow`` an agent CLI needs for a plain print-mode run.

A binding with ``profile = "claude-code"`` starts from that profile's lists; the binding's own ``betas`` and ``allow`` are
added on top (they never remove anything). The lists come from capture, not from memory: each CLI was run with its print
mode against a loopback fake upstream and the headers and body it sent were recorded
(``tests/concierge/fixtures``). ``docs/concierge.md`` lists every captured beta with the reason it is allowed.

A profile never allows a beta that enables server tools, server-held state (threads, files, containers, skills), MCP, fast
mode, fallbacks or a second model (advisor), so ``allow`` is empty in every built-in profile. ``claude-code`` is the set
``claude -p`` sends with ``CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1`` in its environment; the guest adapter must set it (the
CLI's default set also holds ``advanced-tool-use``, which enables server tools, and is refused).
"""

from __future__ import annotations

from dataclasses import dataclass

from motel.concierge.errors import ConfigError


@dataclass(frozen=True)
class Profile:
    name: str
    betas: tuple[str, ...]
    allow: tuple[str, ...]
    source: str


PROFILES = {
    "claude-code": Profile(
        name="claude-code",
        betas=(
            "claude-code-20250219",
            "interleaved-thinking-2025-05-14",
            "effort-2025-11-24",
        ),
        allow=(),
        source="claude 2.1.319, `claude -p` with CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1 (tests/concierge/fixtures/claude_code_2_1_319_betas.json)",
    ),
    "codex": Profile(
        name="codex",
        betas=(),
        allow=(),
        source="codex-cli 0.160.0, `codex exec`, custom provider with wire_api = responses",
    ),
}


def get(name: str) -> Profile:
    try:
        return PROFILES[name]
    except KeyError:
        raise ConfigError(f"unknown profile {name!r}; known profiles: {', '.join(sorted(PROFILES))}") from None


def merged(profile: str, betas: list, allow: list) -> tuple[tuple[str, ...], tuple[str, ...]]:
    """Profile lists first, then the binding's own, without duplicates."""
    base = get(profile) if profile else Profile("", (), (), "")
    return (tuple(dict.fromkeys([*base.betas, *betas])), tuple(dict.fromkeys([*base.allow, *allow])))
