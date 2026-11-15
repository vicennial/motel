"""Adapter table for tour guests. Standard library only; runs on the guest (Python 3.10+), shipped beside runner.py.

An adapter says how one agent CLI is started inside a guest: its argv, the extra environment it wants and the
config files it needs in its fixed HOME. The runner owns everything else (process group, time limit, output caps,
git, result files), so an adapter is a few lines. To add an agent CLI, add one :class:`Adapter` to ``ADAPTERS``.

Sources for the two built-in agent CLIs (checked against claude 2.1.x and codex-cli 0.160.x, by the CLI's own
``--help`` output and by the option and setting names in the shipped binaries; no request was made to any API):

* Claude Code reads the API origin from ``ANTHROPIC_BASE_URL`` and the key from ``ANTHROPIC_API_KEY``. It appends
  ``/v1/messages`` to the base URL, so the base here is ``http://127.0.0.1:<port>/u/anthropic`` and the concierge
  sees ``/u/anthropic/v1/messages``. In ``-p`` (print) mode the key in the environment is used as is. Flags used:
  ``-p``, ``--output-format json``, ``--max-turns N`` (documented in the binary's error text), ``--no-session-persistence``
  (print mode only) and ``--dangerously-skip-permissions`` (a print-mode run cannot answer a permission prompt; the
  guest is a disposable, egress-restricted machine and the agent is an unprivileged user). Settings are read from
  ``$HOME/.claude/settings.json`` (``permissions.defaultMode``, ``env``) and ``$HOME/.claude.json``
  (``hasCompletedOnboarding``). ``DISABLE_AUTOUPDATER``, ``DISABLE_TELEMETRY``, ``DISABLE_ERROR_REPORTING`` and
  ``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`` stop calls that the guest firewall would drop anyway, so they cannot hang.
* Codex reads a custom provider from ``$CODEX_HOME/config.toml``: ``model_provider`` plus
  ``[model_providers.<id>]`` with ``base_url``, ``env_key`` (the NAME of the env var holding the key) and
  ``wire_api = "responses"`` (``"chat"`` is no longer supported). ``codex exec`` flags used: ``--json`` (JSONL
  events), ``--skip-git-repo-check``, ``--ephemeral`` and ``--dangerously-bypass-approvals-and-sandbox`` (the same
  reasoning as above; Codex's own sandbox needs kernel features a small cloud server may not offer). The id
  ``motel`` is used because built-in provider ids (such as ``openai``) are reserved. The base URL is
  ``http://127.0.0.1:<port>/u/openai/v1``: Codex appends ``/responses`` to it. ``OPENAI_BASE_URL`` is also set,
  because the contract names it, and it is what a plain OpenAI SDK reads.

Own-login mode (opt-in, claude adapter only, card field ``"auth": "own-login"``): the runner passes a long-lived
subscription token, read from its stdin, as ``CLAUDE_CODE_OAUTH_TOKEN`` and sets no ``ANTHROPIC_BASE_URL`` or
``ANTHROPIC_API_KEY``. Sources (Claude Code documentation, code.claude.com/docs/en): ``authentication`` ("Authentication
precedence" item 5, and "Generate a long-lived token": a one-year token from ``claude setup-token`` that can only make
model requests), ``env-vars`` (``CLAUDE_CODE_OAUTH_TOKEN``), and ``network-config`` ("Network access requirements").
Variables that cut non-essential traffic, from ``env-vars`` and ``network-config``:
``CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC`` (auto-update, telemetry, error reporting, feedback, release notes, the
Datadog intake hosts, feature-flag fetching), ``DISABLE_TELEMETRY`` and ``DO_NOT_TRACK`` (telemetry),
``DISABLE_ERROR_REPORTING``, ``DISABLE_AUTOUPDATER``, ``ENABLE_CLAUDEAI_MCP_SERVERS=false`` (no claude.ai connector
fetch through ``mcp-proxy.anthropic.com``) and ``CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL``.

Concierge (API-key) mode adds two variables for the claude adapter, from the capture of claude 2.1.319 in the concierge
part (``docs/concierge.md``, "Agent CLI profiles", branch ``tours-concierge``). ``CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS=1``:
without it the CLI sends beta headers that the concierge's ``claude-code`` profile refuses, and every request gets 403.
``CLAUDE_CODE_MAX_OUTPUT_TOKENS``: the CLI asks for 128000 output tokens on Opus 5.5, and the concierge reserves the
price of that for one request (about 2.56 USD), which is more than a 2 USD guest budget. The value comes from the card
field ``max_output_tokens`` (default ``DEFAULT_MAX_OUTPUT_TOKENS``). Neither is set in own-login mode, which has no
concierge.

The concierge placeholder token is only ever an environment value (``ANTHROPIC_API_KEY`` and friends). It is
never written to a config file or an argv.
"""

from __future__ import annotations

import json
import re
from collections.abc import Callable
from dataclasses import dataclass, field

CLEAN_PATH = "/usr/local/bin:/usr/bin:/bin"
# Per output stream (stdout, stderr): the runner keeps at most this many bytes of each, and the engine fetches at most
# this many. One constant, imported by both (motel.tours.guest.LOG_CAP_BYTES), so the two cannot drift apart.
LOG_CAP_BYTES = 1024 * 1024
MAX_PROMPT_BYTES = 100_000          # one argv string may not exceed 128 KiB on Linux
DEFAULT_MAX_OUTPUT_TOKENS = 8000
MIN_OUTPUT_TOKENS = 256
MAX_OUTPUT_TOKENS_LIMIT = 128_000       # the CLI's own default on Opus 5.5 is this high
DEFAULT_MAX_TURNS = 30
MAX_TURNS_LIMIT = 1000

# Names of binding variables. A card may not set them itself: only the concierge pair reaches the agent.
BINDING_ENV = {
    "anthropic": ("ANTHROPIC_BASE_URL", "ANTHROPIC_API_KEY", ""),
    "openai": ("OPENAI_BASE_URL", "OPENAI_API_KEY", "/v1"),
}
# Environment names a card may not set: they carry a credential, point a client at another endpoint or change how a
# process starts. Compared in upper case, so ``http_proxy`` is covered by ``HTTP_PROXY``. The engine's spec check imports
# this list (motel.tours.guest.RESERVED_ENV_NAMES / RESERVED_ENV_PREFIXES / env_name_reserved), so it is the only copy.
RESERVED_ENV_NAMES = frozenset({
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "PWD", "OLDPWD", "IFS", "BASH_ENV", "ENV", "TMPDIR",
    "PYTHONPATH", "PYTHONSTARTUP", "NODE_OPTIONS", "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY", "NO_PROXY",
    "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE",
    "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "ANTHROPIC_BASE_URL", "CLAUDE_CODE_OAUTH_TOKEN",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_HOME",
})
RESERVED_ENV_PREFIXES = ("ANTHROPIC_", "OPENAI_", "CLAUDE_", "CODEX_", "MOTEL_", "AWS_", "AZURE_", "GOOGLE_", "GCP_",
                         "HCLOUD_", "GITHUB_", "GH_", "LD_", "SSH_", "SUDO_", "BASH_", "GIT_", "PYTHON", "NODE_")


def env_name_reserved(name: str) -> bool:
    upper = name.upper()
    return upper in RESERVED_ENV_NAMES or upper.startswith(RESERVED_ENV_PREFIXES)


# Card field ``model``: the value of the adapter CLI's model flag, one argv element (never a shell word). The first character
# is a letter or digit, so it can never read as an option. The spec and the runner both check it with this pattern.
MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:\[\]-]{0,63}$")
MODEL_MAX = 64
BINDING_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]{0,30}$")


def binding_env(binding: str, port: int, placeholder: str) -> dict:
    """The environment pair that points an SDK at the concierge for one binding."""
    if not BINDING_NAME_RE.fullmatch(binding):
        raise ValueError("invalid binding name")
    if binding in BINDING_ENV:
        base_name, key_name, suffix = BINDING_ENV[binding]
        return {base_name: f"http://127.0.0.1:{port}/u/{binding}{suffix}", key_name: placeholder}
    tag = binding.upper().replace("-", "_")
    return {f"MOTEL_BINDING_{tag}_URL": f"http://127.0.0.1:{port}/u/{binding}",
            f"MOTEL_BINDING_{tag}_TOKEN": placeholder}


class RefusalScanner:
    """Notices concierge refusals in an agent's captured output, as the stream goes by.

    The concierge answers a refused request with HTTP 401, 403, 429 or 502 and a JSON body
    ``{"type": "error", "error": {"type": "motel_<reason>", "message": ...}}`` (see the tours contract). The runner
    never sees those responses, but every agent CLI prints the error it got, so the body shows up in stdout or
    stderr. This scanner counts each ``motel_<reason>`` error type and keeps the HTTP status when the agent printed
    it just before the body (for example ``API Error: 429 {...}``). It sees only what the agent chose to print: a
    refusal that was retried silently or swallowed is not seen here. The concierge's own ledger is the full record
    (``ConciergeClient.usage()["refused"]``). A match split across two reads is still counted once.
    """

    BODY = re.compile(rb'(?:\b([45][0-9]{2})\b[^0-9{}]{0,24})?\{\s*\\?"type\\?"\s*:\s*\\?"error\\?"\s*,\s*\\?"error\\?"\s*:\s*\{'
                      rb'\s*\\?"type\\?"\s*:\s*\\?"(motel_[a-z0-9_]{1,60})\\?"')
    KEEP = 160
    MAX_KINDS = 16

    def __init__(self):
        self.tail = b""
        self.found = {}                 # (type, status) -> count

    def feed(self, data):
        window = self.tail + data
        fresh_from = len(self.tail)
        for match in self.BODY.finditer(window):
            if match.end() <= fresh_from:
                continue                # already counted in the previous read
            key = (match.group(2).decode("ascii"), int(match.group(1)) if match.group(1) else None)
            if key in self.found or len(self.found) < self.MAX_KINDS:
                self.found[key] = self.found.get(key, 0) + 1
        self.tail = window[-self.KEEP:]

    def result(self):
        """A list of ``{"type": "motel_...", "status": 429 or None, "count": n}``, sorted."""
        return [{"type": kind, "status": status, "count": count}
                for (kind, status), count in sorted(self.found.items(), key=lambda item: (item[0][0], item[0][1] or 0))]


@dataclass
class Context:
    """What an adapter may look at. ``card`` is the validated guest card (a dict)."""
    card: dict
    home: str
    work: str
    port: int


@dataclass(frozen=True)
class Adapter:
    name: str
    # Executable to look up on the clean PATH. None means "the first word of the card's argv".
    binary: str | None
    argv: Callable[[Context], list]
    env: Callable[[Context], dict] = lambda ctx: {}
    # Config files the CLI wants under HOME: {relative path: text}. Never holds the placeholder token.
    files: Callable[[Context], dict] = lambda ctx: {}
    # Bindings the card must list for this adapter to make sense.
    requires: tuple = ()
    needs_prompt: bool = True
    # Factory for the object that watches the captured output for concierge refusals (see RefusalScanner).
    refusals: Callable = RefusalScanner
    notes: str = field(default="", compare=False)


def _turns(ctx: Context) -> int:
    value = ctx.card.get("max_turns", DEFAULT_MAX_TURNS)
    if isinstance(value, bool) or not isinstance(value, int) or not 1 <= value <= MAX_TURNS_LIMIT:
        raise ValueError(f"max_turns must be an integer from 1 to {MAX_TURNS_LIMIT}")
    return value


def card_model_ok(value) -> bool:
    return isinstance(value, str) and len(value) <= MODEL_MAX and bool(MODEL_RE.fullmatch(value))


def _model_flag(ctx: Context, flag: str) -> list:
    """``[flag, model]`` when the card names a model, else nothing (the CLI keeps its own default)."""
    if "model" not in ctx.card:
        return []
    value = ctx.card["model"]
    if not card_model_ok(value):
        raise ValueError(f"model must be a string of 1 to {MODEL_MAX} characters, as in claude-sonnet-5-5")
    return [flag, value]


def _claude_argv(ctx: Context) -> list:
    return ["claude", "-p", "--output-format", "json", "--max-turns", str(_turns(ctx)), *_model_flag(ctx, "--model"),
            "--no-session-persistence", "--dangerously-skip-permissions", "--", ctx.card["prompt"]]


def max_output_tokens(ctx: Context) -> int:
    value = ctx.card.get("max_output_tokens", DEFAULT_MAX_OUTPUT_TOKENS)
    if isinstance(value, bool) or not isinstance(value, int) or not MIN_OUTPUT_TOKENS <= value <= MAX_OUTPUT_TOKENS_LIMIT:
        raise ValueError(f"max_output_tokens must be an integer from {MIN_OUTPUT_TOKENS} to {MAX_OUTPUT_TOKENS_LIMIT}")
    return value


def _claude_env(ctx: Context) -> dict:
    env = {"DISABLE_AUTOUPDATER": "1", "DISABLE_TELEMETRY": "1", "DISABLE_ERROR_REPORTING": "1",
           "DO_NOT_TRACK": "1", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": "1",
           "CLAUDE_CODE_DISABLE_OFFICIAL_MARKETPLACE_AUTOINSTALL": "1", "ENABLE_CLAUDEAI_MCP_SERVERS": "false"}
    if ctx.card.get("auth", "api-key") != "own-login":
        # Behind the concierge: no experimental betas (it refuses them) and a bounded output size (see the docstring).
        env["CLAUDE_CODE_DISABLE_EXPERIMENTAL_BETAS"] = "1"
        env["CLAUDE_CODE_MAX_OUTPUT_TOKENS"] = str(max_output_tokens(ctx))
    return env


def _claude_files(ctx: Context) -> dict:
    settings = {"permissions": {"defaultMode": "bypassPermissions"}, "skipDangerousModePermissionPrompt": True,
                "includeCoAuthoredBy": False}
    state = {"hasCompletedOnboarding": True, "numStartups": 1}
    return {".claude/settings.json": json.dumps(settings, indent=2, sort_keys=True) + "\n",
            ".claude.json": json.dumps(state, indent=2, sort_keys=True) + "\n"}


def _codex_argv(ctx: Context) -> list:
    return ["codex", "exec", "--json", "--skip-git-repo-check", "--ephemeral", *_model_flag(ctx, "--model"),
            "--dangerously-bypass-approvals-and-sandbox", "--", ctx.card["prompt"]]


def _codex_env(ctx: Context) -> dict:
    return {"CODEX_HOME": ctx.home.rstrip("/") + "/.codex"}


def codex_config_toml(port: int) -> str:
    return "\n".join([
        '# written by the motel tour runner; the key is read from the OPENAI_API_KEY environment variable',
        'model_provider = "motel"',
        'approval_policy = "never"',
        'sandbox_mode = "danger-full-access"',
        'check_for_update_on_startup = false',
        '',
        '[model_providers.motel]',
        'name = "motel concierge"',
        f'base_url = "http://127.0.0.1:{int(port)}/u/openai/v1"',
        'env_key = "OPENAI_API_KEY"',
        'wire_api = "responses"',
        ''])


def _codex_files(ctx: Context) -> dict:
    return {".codex/config.toml": codex_config_toml(ctx.port)}


def _command_argv(ctx: Context) -> list:
    argv = ctx.card.get("argv")
    if not isinstance(argv, list) or not argv or not all(isinstance(a, str) for a in argv):
        raise ValueError("the command adapter needs a non-empty argv list of strings")
    return list(argv)


ADAPTERS = {
    "claude": Adapter("claude", "claude", _claude_argv, _claude_env, _claude_files, requires=("anthropic",),
                      notes="claude -p with the key and base URL from the environment"),
    "codex": Adapter("codex", "codex", _codex_argv, _codex_env, _codex_files, requires=("openai",),
                     notes="codex exec with a custom provider in CODEX_HOME/config.toml"),
    "command": Adapter("command", None, _command_argv, needs_prompt=False,
                       notes="argv exactly as given on the card"),
}
