"""The guest side of ``model``: the card is checked again, and the value is one argv element after the CLI's model flag."""
import json
import os

import pytest
from guestkit import OAUTH, Guest

DUMP_CLI = """
    import json, sys
    print(json.dumps({"argv": sys.argv[1:]}))
    """


@pytest.fixture
def guest(tmp_path):
    return Guest(tmp_path)


GOOD = ["claude-sonnet-5-5", "claude-opus-5-5[1m]", "sonnet", "gpt-6.1-sol", "x" * 64]
BAD = ["", "-x", "--help", "a b", "a;b", "$(id)", "a\nb", "x" * 65, "é", 5, True, None, ["a"]]


def argv_seen(guest, cli, **card):
    guest.fake_cli(cli, DUMP_CLI)
    bindings = ["anthropic"] if cli == "claude" else ["openai"]
    res = guest.run(guest.card(adapter=cli, prompt="do it", bindings=bindings, max_turns=7, **card))
    return res, json.loads(res.read("stdout.log").decode().strip().splitlines()[-1])["argv"]


@pytest.mark.parametrize("value", GOOD)
def test_claude_gets_model_as_one_element_before_the_prompt(guest, value):
    res, argv = argv_seen(guest, "claude", model=value)
    assert res.run["termination"] == "exited"
    assert argv == ["-p", "--output-format", "json", "--max-turns", "7", "--model", value, "--no-session-persistence",
                    "--dangerously-skip-permissions", "--", "do it"]


@pytest.mark.parametrize("value", GOOD)
def test_codex_gets_model_as_one_element_before_the_prompt(guest, value):
    res, argv = argv_seen(guest, "codex", model=value)
    assert res.run["termination"] == "exited"
    assert argv == ["exec", "--json", "--skip-git-repo-check", "--ephemeral", "--model", value,
                    "--dangerously-bypass-approvals-and-sandbox", "--", "do it"]


def test_claude_with_own_login_passes_the_model_too(guest):
    guest.fake_cli("claude", DUMP_CLI)
    res = guest.run(guest.card(adapter="claude", prompt="go", auth="own-login", bindings=[], model="sonnet"),
                    stdin=(OAUTH + "\n").encode())
    argv = json.loads(res.read("stdout.log").decode().strip().splitlines()[-1])["argv"]
    assert argv[argv.index("--model") + 1] == "sonnet"


@pytest.mark.parametrize("cli", ["claude", "codex"])
@pytest.mark.parametrize("value", BAD)
def test_a_bad_model_is_an_invalid_card_and_the_cli_never_runs(guest, cli, value):
    guest.fake_cli(cli, "pass")
    res = guest.run(guest.card(adapter=cli, prompt="go", bindings=["anthropic" if cli == "claude" else "openai"], model=value))
    assert res.run["termination"] == "failed" and res.run["reason"] == "invalid_card"
    assert not os.path.exists(os.path.join(guest.home, "out", "stdout.log"))   # nothing ran


def test_the_command_adapter_card_cannot_carry_a_model(guest):
    res = guest.run(guest.card(model="sonnet"))
    assert res.run["termination"] == "failed" and res.run["reason"] == "invalid_card"


@pytest.mark.parametrize("cli", ["claude", "codex"])
def test_an_omitted_model_leaves_the_argv_as_it_was(guest, cli):
    _, argv = argv_seen(guest, cli)
    assert "--model" not in argv and "-m" not in argv
