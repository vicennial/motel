"""The opt-in own-login mode: gated by the operator, token only on ssh stdin, never stored, never in any output."""
import json
import os
import stat

import doubles
import pytest

from motel.tours import ownlogin
from motel.tours.errors import NotSetUp, Refused, Usage

OWN = [{"id": "me", "prompt": "do it", "auth": "own-login", "secrets": []}]


def enable(rig, **over):
    rig.limits = rig.limits.__class__(**{**rig.limits.__dict__, "own_login_enabled": True, "own_login_allow_users": ("alice",), **over})
    rig.service.limits = rig.limits
    rig.env.limits = rig.limits
    ownlogin.set_token(rig.token_file, (doubles.OWN_TOKEN + "\n").encode())


def test_a_tour_cannot_use_own_login_unless_the_operator_enabled_it(rig):
    with pytest.raises(Refused, match="own-login is not enabled"):
        rig.start(OWN)
    assert rig.servers_left() == [] and rig.store.ids() == []


def test_it_works_only_with_the_claude_adapter(rig):
    enable(rig)
    with pytest.raises(Usage, match="claude adapter"):
        rig.start([{"id": "me", "adapter": "command", "argv": ["ls"], "auth": "own-login", "secrets": []}])


def test_it_takes_no_secrets_and_no_budget(rig):
    enable(rig)
    with pytest.raises(Usage, match="no `secrets` and no budget"):
        rig.start([{"id": "me", "prompt": "x", "auth": "own-login", "secrets": ["anthropic"], "budget_usd": 1}])


def test_the_own_login_ttl_ceiling_holds(rig):
    enable(rig, own_login_max_ttl=__import__("datetime").timedelta(minutes=30))
    with pytest.raises(Refused, match="own-login ceiling"):
        rig.start([{**OWN[0], "ttl": "1h"}])
    rig.start([{**OWN[0], "ttl": "30m"}])


def test_the_own_login_concurrency_ceiling_uses_its_own_slots(rig):
    enable(rig, own_login_max_concurrent=1)
    pool = rig.env.own_slots
    first = pool.acquire(lambda: False)
    assert first is not None
    assert pool.acquire(lambda: True) is None                           # a second one waits and gives up on cancel
    first.release()


def test_start_prints_the_warning_for_own_login_only(rig):
    enable(rig)
    reply = rig.service.start(rig.alice, rig.wire(OWN))
    assert reply["warnings"] == [ownlogin.WARNING]
    assert "budget" in ownlogin.WARNING and "account-wide" in ownlogin.WARNING
    assert rig.service.start(rig.alice, rig.wire())["warnings"] == []


def test_the_token_goes_to_the_runner_on_stdin_and_nowhere_else(rig):
    enable(rig)
    doc = rig.run_tour(OWN)
    assert doc["status"] == "done"
    assert rig.transport.stdin_seen == [(doubles.OWN_TOKEN + "\n").encode()]       # exactly one line "token\n"
    card = rig.transport.cards[0]
    assert card["auth"] == "own-login" and card["placeholder"] == "" and card["bindings"] == []
    assert doubles.OWN_TOKEN not in json.dumps(card)
    assert "issue" not in rig.log.kinds() and "revoke" not in rig.log.kinds()       # no concierge placeholder at all
    assert rig.log.kinds().count("put") == 4
    for put_file in rig.transport.inputs.values():
        assert all(doubles.OWN_TOKEN.encode() not in data for data in put_file.values())


def test_the_token_is_in_no_state_log_result_or_argv(rig):
    enable(rig)
    doc = rig.run_tour(OWN)
    leaked = []
    for base in (str(rig.tmp / "tours"), str(rig.tmp / "cabins")):
        for root, _d, files in os.walk(base):
            for name in files:
                path = os.path.join(root, name)
                with open(path, "rb") as handle:
                    found = doubles.OWN_TOKEN.encode() in handle.read()
                if found:
                    leaked.append(path)
    assert leaked == []                      # not in tours/ (state, events, results, logs) nor in the cabin store
    assert doubles.OWN_TOKEN.encode() not in open(os.path.join(rig.store.path(doc["id"]), "events.jsonl"), "rb").read()
    result = json.dumps(rig.service.result(rig.alice, doc["id"], "me"))
    logs = json.dumps([rig.service.logs(rig.alice, doc["id"], "me", s) for s in ("stdout", "stderr", "runner")])
    assert doubles.OWN_TOKEN not in result and doubles.OWN_TOKEN not in logs


def test_a_runner_that_echoes_the_token_is_redacted(rig):
    enable(rig)
    doc = rig.run_tour(OWN)                       # the fake runner log contains "got <token>"
    logs = rig.service.logs(rig.alice, doc["id"], "me", "runner")
    assert doubles.OWN_TOKEN not in logs["text"] and "[redacted]" in logs["text"]


def test_a_runner_that_prints_the_token_in_its_output_is_redacted(rig):
    enable(rig)

    def guest(card, files):
        out, status = doubles.default_guest(card, files)
        out["stdout.log"] = f"token is {doubles.OWN_TOKEN}\n".encode()
        out["RESULT.json"] = json.dumps({"summary": doubles.OWN_TOKEN}).encode()
        return out, status
    rig.transport.guest = guest
    doc = rig.run_tour(OWN)
    out = json.dumps(rig.service.result(rig.alice, doc["id"], "me")) + rig.service.logs(rig.alice, doc["id"], "me")["text"]
    assert doubles.OWN_TOKEN not in out and doubles.OWN_TOKEN not in rig.tree_text()


def test_the_result_says_not_metered(rig):
    enable(rig)
    doc = rig.run_tour(OWN)
    record = rig.result_record(doc["id"], "me")
    assert record["auth"] == "own-login"
    assert record["verified"]["usage"]["metered"] is False and "not metered" in record["verified"]["usage"]["note"]


def test_without_a_stored_token_the_attempt_fails_before_any_cabin(rig):
    enable(rig)
    os.unlink(rig.token_file)
    doc = rig.run_tour(OWN)
    assert doc["guests"]["me"]["status"] == "failed" and rig.servers_left() == [] and "run" not in rig.log.kinds()


def test_the_token_file_commands(rig):
    path = rig.token_file
    assert ownlogin.status(path) == {"present": False, "modified": None}
    info = ownlogin.set_token(path, (doubles.OWN_TOKEN + "\n").encode())
    assert info["present"] is True and doubles.OWN_TOKEN not in json.dumps(info) and "length" not in info
    assert stat.S_IMODE(os.stat(path).st_mode) == 0o600 and open(path).read() == doubles.OWN_TOKEN + "\n"
    assert ownlogin.read_token(path) == doubles.OWN_TOKEN
    assert ownlogin.clear(path)["present"] is False and not os.path.exists(path)


@pytest.mark.parametrize("data", [b"", b"short", b"two\nlines-0123456789abcdefghij", b"x" * 5000, b"has space 0123456789abcdefghij",
                                  b"\xff\xfe0123456789abcdefghijk"])
def test_bad_tokens_are_refused(data):
    with pytest.raises(Usage):
        ownlogin.parse_token(data)


def test_a_token_file_that_others_can_read_is_not_used(rig):
    ownlogin.set_token(rig.token_file, (doubles.OWN_TOKEN + "\n").encode())
    os.chmod(rig.token_file, 0o644)
    with pytest.raises(NotSetUp):
        ownlogin.read_token(rig.token_file)


def test_a_symlinked_token_file_is_not_followed(rig, tmp_path):
    real = tmp_path / "real"
    real.write_text(doubles.OWN_TOKEN + "\n")
    os.chmod(real, 0o600)
    os.makedirs(os.path.dirname(rig.token_file), exist_ok=True)
    os.symlink(str(real), rig.token_file)
    with pytest.raises(NotSetUp):
        ownlogin.read_token(rig.token_file)


def test_the_service_verbs_for_the_token(rig):
    assert rig.service.own_login("status")["own_login"]["present"] is False
    reply = rig.service.own_login("set", (doubles.OWN_TOKEN + "\n").encode())
    assert reply["own_login"]["present"] is True and doubles.OWN_TOKEN not in json.dumps(reply)
    assert rig.service.own_login("clear")["own_login"]["present"] is False


def test_redact_removes_every_copy():
    assert ownlogin.redact(b"a TOK b TOK", "TOK") == b"a [redacted] b [redacted]" and ownlogin.redact(b"x", None) == b"x"
