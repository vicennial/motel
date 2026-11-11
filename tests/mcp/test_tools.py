import json

import pytest

ROOM_OK = {
    "schema": "motel/v1",
    "ok": True,
    "room": {
        "id": "video-render-k3f9", "task": "video-render", "owner": "alice", "uid": 1000, "size_gb": 200,
        "suite": False, "floor": "f-3a1c", "state": "active", "created": "2026-03-01T12:40:00Z",
        "expires": "2026-03-01T18:40:00Z", "time_left_h": 6.0, "warn": False,
        "path": "/mnt/motel/f-3a1c/rooms/video-render-k3f9",
    },
}


def text(reply):
    return reply["result"]["content"][0]["text"]


def ok(reply):
    assert "error" not in reply, reply
    assert reply["result"]["isError"] is False, text(reply)
    return reply["result"]


# --- argv of every tool -----------------------------------------------------------------------


def test_checkin_argv_and_result(env, client):
    env.set(stdout=json.dumps(ROOM_OK))
    result = ok(client.call("motel_checkin", {"size": "200G", "duration": "6h", "task": "video-render", "suite": True}))
    assert env.calls() == [["checkin", "200G", "--for", "6h", "--task", "video-render", "--suite", "--json"]]
    assert json.loads(result["content"][0]["text"]) == ROOM_OK  # the CLI's JSON passes through unchanged
    assert result["structuredContent"]["room"]["path"] == "/mnt/motel/f-3a1c/rooms/video-render-k3f9"


def test_checkin_minimal_and_int_size(env, client):
    ok(client.call("motel_checkin", {"size": 500}))
    ok(client.call("motel_checkin", {"size": "1T"}))
    assert env.calls() == [["checkin", "500", "--json"], ["checkin", "1T", "--json"]]


def test_list_scopes(env, client):
    listing = {"schema": "motel/v1", "ok": True, "rooms": [], "floors": [], "capacity": {"floors": 0}}
    env.set(stdout=json.dumps(listing))
    result = ok(client.call("motel_list", {}))
    assert result["structuredContent"] == listing
    env.set(stdout="[]")
    assert ok(client.call("motel_list", {}))["structuredContent"] == {"result": []}  # arrays are wrapped
    ok(client.call("motel_list", {"scope": "all"}))
    ok(client.call("motel_list", {"scope": None}))
    assert env.calls() == [
        ["ls", "--mine", "--json"], ["ls", "--mine", "--json"], ["ls", "--all", "--json"], ["ls", "--mine", "--json"],
    ]


def test_status_extend_checkout_restore_doctor(env, client):
    ok(client.call("motel_status", {"room": "video-render-k3f9"}))
    ok(client.call("motel_extend", {"room": "video-render-k3f9", "duration": "90m"}))
    ok(client.call("motel_checkout", {"room": "video-render-k3f9"}))
    ok(client.call("motel_checkout", {"room": "video-render-k3f9", "now": True}))
    ok(client.call("motel_restore", {"room": "video-render-k3f9"}))
    ok(client.call("motel_restore", {"room": "video-render-k3f9", "duration": "2d"}))
    ok(client.call("motel_doctor", {}))
    r = "video-render-k3f9"
    assert env.calls() == [
        ["status", r, "--json"],
        ["extend", r, "--for", "90m", "--json"],
        ["checkout", r, "--json"],
        ["checkout", r, "--now", "--json"],
        ["restore", r, "--json"],
        ["restore", r, "--for", "2d", "--json"],
        ["doctor", "--json"],
    ]


def test_non_json_output_passes_through(env, client):
    env.set(stdout="/mnt/rooms/x\n")
    result = ok(client.call("motel_checkin", {"size": "10G"}))
    assert result["content"][0]["text"] == "/mnt/rooms/x"
    assert "structuredContent" not in result


def test_missing_arguments_object_is_empty(env, client):
    ok(client.call("motel_doctor"))
    assert env.calls() == [["doctor", "--json"]]


# --- input validation refusals: the CLI must never be called ------------------------------------

BAD = [
    ("motel_checkin", {}),
    ("motel_checkin", {"size": "200"+"G; rm -rf /"}),
    ("motel_checkin", {"size": "$(id)"}),
    ("motel_checkin", {"size": "0G"}),
    ("motel_checkin", {"size": "-5G"}),
    ("motel_checkin", {"size": "200g"}),
    ("motel_checkin", {"size": "1.5T"}),
    ("motel_checkin", {"size": 0}),
    ("motel_checkin", {"size": -1}),
    ("motel_checkin", {"size": True}),
    ("motel_checkin", {"size": ["200G"]}),
    ("motel_checkin", {"size": "200G", "duration": "forever"}),
    ("motel_checkin", {"size": "200G", "duration": "6"}),
    ("motel_checkin", {"size": "200G", "duration": "0h"}),
    ("motel_checkin", {"size": "200G", "duration": "6h --now"}),
    ("motel_checkin", {"size": "200G", "task": "Has Space"}),
    ("motel_checkin", {"size": "200G", "task": "--now"}),
    ("motel_checkin", {"size": "200G", "task": "../etc"}),
    ("motel_checkin", {"size": "200G", "task": "a" * 41}),
    ("motel_checkin", {"size": "200G", "suite": "yes"}),
    ("motel_checkin", {"size": "200G", "unknown": 1}),
    ("motel_list", {"scope": "everyone"}),
    ("motel_list", {"scope": "mine; ls"}),
    ("motel_status", {}),
    ("motel_status", {"room": "ab"}),
    ("motel_status", {"room": "Room-1"}),
    ("motel_status", {"room": "-rf"}),
    ("motel_status", {"room": "--json"}),
    ("motel_status", {"room": "a/b/c"}),
    ("motel_status", {"room": "x" * 81}),
    ("motel_status", {"room": 12345}),
    ("motel_status", {"room": "ok-room\n"}),
    ("motel_extend", {"room": "ok-room"}),
    ("motel_extend", {"room": "ok-room", "duration": "soon"}),
    ("motel_checkout", {"room": "ok-room", "now": "true"}),
    ("motel_restore", {"room": "ok room"}),
    ("motel_doctor", {"verbose": True}),
]


@pytest.mark.parametrize("name,arguments", BAD)
def test_validation_refusals(env, client, name, arguments):
    reply = client.call(name, arguments)
    assert "error" not in reply  # tool errors are results the model can read, not protocol errors
    assert reply["result"]["isError"] is True
    assert text(reply)
    assert env.calls() == []


def test_non_object_arguments(env, client):
    reply = client.request("tools/call", {"name": "motel_doctor", "arguments": ["x"]})
    assert reply["result"]["isError"] is True and env.calls() == []


def test_shell_metacharacters_are_never_interpreted(env, client):
    # Strict patterns already refuse them; this documents that nothing was executed.
    reply = client.call("motel_status", {"room": "abc;touch-pwned"})
    assert reply["result"]["isError"] is True
    assert env.calls() == []


# --- exit code mapping --------------------------------------------------------------------------


EXIT_CASES = [
    (2, "usage", "motel --help"),
    (3, "Do not retry in a loop", "motel ls --mine"),
    (4, "files open", "motel ls --mine"),
    (5, "Retry once at most", "motel doctor"),
    (6, "Do not try to work around", "motel doctor"),
    (7, "motel_list", "motel ls --mine"),
    (8, "install or set up", "sudo motel-desk setup hetzner"),
    (9, "incomplete", "motel doctor"),
    (42, "do not retry", "motel doctor"),
]


@pytest.mark.parametrize("code,needle,hint", EXIT_CASES)
def test_exit_code_mapping_json_failure(env, client, code, needle, hint):
    """With --json the CLI prints {"ok": false, "error", "hint", "code"} on stdout."""
    failure = {"schema": "motel/v1", "ok": False, "error": "something specific happened", "hint": hint, "code": code}
    env.set(exit=code, stdout=json.dumps(failure))
    reply = client.call("motel_checkin", {"size": "800G"})
    assert reply["result"]["isError"] is True
    message = text(reply)
    assert f"motel exited {code}" in message
    assert "motel: something specific happened." in message  # the CLI's own message is kept
    assert f"CLI suggests: {hint}" in message
    assert "Next:" in message and needle in message
    assert reply["result"]["structuredContent"] == failure


@pytest.mark.parametrize("code,needle,hint", EXIT_CASES)
def test_exit_code_mapping_stderr_line(env, client, code, needle, hint):
    """Without JSON (an older CLI, or a crash before output) the failure is one stderr line."""
    env.set(exit=code, stdout="", stderr=f"motel: something specific happened. Try: {hint}")
    reply = client.call("motel_checkin", {"size": "800G"})
    assert reply["result"]["isError"] is True
    message = text(reply)
    assert f"motel exited {code}" in message
    assert f"something specific happened. Try: {hint}" in message
    assert needle in message


def test_failing_doctor_lists_failed_checks(env, client):
    report = {
        "schema": "motel/v1", "ok": False,
        "checks": [
            {"check": "host set up", "level": "ok", "ok": True, "detail": "fine"},
            {"check": "token scope", "level": "fail", "ok": False, "detail": "token sees other servers"},
            {"check": "timer", "level": "warn", "ok": True, "detail": "cron only"},
        ],
    }
    env.set(exit=5, stdout=json.dumps(report))
    reply = client.call("motel_doctor", {})
    message = text(reply)
    assert reply["result"]["isError"] is True
    assert "- token scope: token sees other servers" in message
    assert "host set up" not in message and "cron only" not in message


def test_missing_binary(env):
    c = env.client(extra_env={"PATH": "/nonexistent", "MOTEL_BIN": "/nonexistent/motel"})
    c.handshake()
    reply = c.call("motel_doctor", {})
    assert reply["result"]["isError"] is True
    assert "could not start motel" in text(reply)


# --- timeout --------------------------------------------------------------------------------------


def test_timeout_kills_cli_and_warns_for_mutations(env):
    env.set(sleep=30)
    c = env.client("--timeout", "1")
    c.handshake()
    reply = c.call("motel_checkin", {"size": "10G"})
    assert reply["result"]["isError"] is True
    message = text(reply)
    assert "timed out after 1s" in message
    assert "may still have completed" in message  # do not blindly call checkin twice


def test_timeout_read_only_has_no_mutation_warning(env):
    env.set(sleep=30)
    c = env.client("--timeout", "1")
    c.handshake()
    message = text(c.call("motel_list", {}))
    assert "timed out" in message and "may still have completed" not in message


def test_ping_answered_while_tool_call_runs(env):
    env.set(sleep=2)
    c = env.client()
    c.handshake()
    c.send({"jsonrpc": "2.0", "id": 100, "method": "tools/call", "params": {"name": "motel_doctor", "arguments": {}}})
    c.send({"jsonrpc": "2.0", "id": 101, "method": "ping"})
    first, second = c.recv(), c.recv()
    assert first["id"] == 101 and second["id"] == 100


# --- cabins ---------------------------------------------------------------------------------------


def test_cabin_tools_argv(env):
    c = env.client("--with-cabins")
    c.handshake()
    ok(c.call("motel_cabin_checkin", {"type": "cpx52", "duration": "2h", "task": "test-suite"}))
    ok(c.call("motel_cabin_list", {}))
    ok(c.call("motel_cabin_status", {"cabin": "swift-gull"}))
    ok(c.call("motel_cabin_extend", {"cabin": "swift-gull", "duration": "1h"}))
    ok(c.call("motel_cabin_checkout", {"cabin": "swift-gull"}))
    ok(c.call("motel_cabin_run", {"command": ["make", "test", "-j4"], "type": "cpx52", "duration": "2h"}))
    assert env.calls() == [
        ["cabin", "checkin", "--type", "cpx52", "--for", "2h", "--task", "test-suite", "--json"],
        ["cabin", "ls", "--json"],
        ["cabin", "status", "swift-gull", "--json"],
        ["cabin", "extend", "swift-gull", "--for", "1h", "--json"],
        ["cabin", "checkout", "swift-gull", "--json"],
        ["cabin", "run", "--type", "cpx52", "--for", "2h", "--json", "--", "make", "test", "-j4"],
    ]


def test_cabin_run_keeps_the_pending_cleanup_warning_when_the_command_exited_zero(env):
    warning = ("motel: WARNING: checkout of swift-gull-0123456789ab is NOT complete (cleanup pending; it still bills "
               "until the reaper finishes). The exit status is the command's. Try: motel cabin status swift-gull")
    done = {"schema": "motel/v1", "ok": True, "cabin": "swift-gull-0123456789ab", "exit_code": 0, "released": False}
    env.set(stdout="tests passed\n\n" + json.dumps(done), stderr="motel: cabin swift-gull · ready in 40s\n" + warning,
            exit=0)
    c = env.client("--with-cabins")
    c.handshake()
    reply = text(c.call("motel_cabin_run", {"command": ["make", "test"]}))
    assert "tests passed" in reply and "NOT complete" in reply and '"released": false' in reply
    env.set(stdout="tests passed\n", stderr="motel: released swift-gull-0123456789ab · est. cost EUR 0.01 net\n", exit=0)
    assert "WARNING" not in text(c.call("motel_cabin_run", {"command": ["make", "test"]}))


def test_cabin_exit_4_is_a_busy_cabin_not_open_files(env):
    env.set(stderr="motel: checkout of swift-gull-0123456789ab FAILED (busy). Try: motel cabin checkout swift-gull\n",
            exit=4)
    c = env.client("--with-cabins")
    c.handshake()
    run = c.call("motel_cabin_run", {"command": ["make", "test"]})["result"]
    assert run["isError"] is True and "checkout busy" in run["content"][0]["text"]
    assert "files open" not in run["content"][0]["text"] and "motel_cabin_checkout" in run["content"][0]["text"]
    out = text(c.call("motel_cabin_checkout", {"cabin": "swift-gull"}))
    assert "cabin busy" in out and "files open" not in out
    env.set(stderr="motel: busy\n", exit=4)
    assert "files open" in text(c.call("motel_status", {"room": "video-render-k3f9"}))     # rooms keep their hint


@pytest.mark.parametrize(
    "arguments",
    [
        {},
        {"command": "make test"},
        {"command": []},
        {"command": [""]},
        {"command": ["make", 1]},
        {"command": ["make"], "type": "CPX52; x"},
        {"command": ["make"], "duration": "later"},
    ],
)
def test_cabin_run_validation(env, arguments):
    c = env.client("--with-cabins")
    c.handshake()
    assert c.call("motel_cabin_run", arguments)["result"]["isError"] is True
    assert env.calls() == []
