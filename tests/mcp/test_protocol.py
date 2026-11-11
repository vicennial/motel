import json

from motel import mcp_server


def test_handshake_echoes_supported_version(env):
    c = env.client()
    reply = c.handshake("2025-06-18")
    result = reply["result"]
    assert result["protocolVersion"] == "2025-06-18"
    assert result["capabilities"] == {"tools": {"listChanged": False}}
    assert result["serverInfo"]["name"] == "motel"


def test_handshake_offers_newest_for_unknown_version(env):
    c = env.client()
    reply = c.handshake("2099-01-01")
    assert reply["result"]["protocolVersion"] == mcp_server.SUPPORTED_PROTOCOLS[0]


def test_handshake_accepts_newer_spec(env):
    c = env.client()
    assert c.handshake("2025-11-25")["result"]["protocolVersion"] == "2025-11-25"


def test_ping(client):
    assert client.request("ping")["result"] == {}


def test_initialized_notification_gets_no_reply(client):
    client.notify("notifications/initialized")
    client.notify("notifications/cancelled", {"requestId": 99})
    # The next request is answered first and in order: no stray replies were queued.
    assert client.request("ping")["result"] == {}


def test_unknown_method(client):
    reply = client.request("resources/list")
    assert reply["error"]["code"] == -32601


def test_parse_error_keeps_server_alive(client):
    client.send("{not json")
    reply = client.recv()
    assert reply["error"]["code"] == -32700 and reply["id"] is None
    assert client.request("ping")["result"] == {}


def test_invalid_request(client):
    client.send(json.dumps({"id": 5, "method": 3}))
    assert client.recv()["error"]["code"] == -32600


def test_unknown_tool_is_protocol_error(client):
    reply = client.call("motel_nope", {})
    assert reply["error"]["code"] == -32602


def test_server_exits_on_eof(env):
    c = env.client()
    c.handshake()
    c.close()
    assert c.proc.returncode == 0


def test_tools_list_schema(client):
    tools = client.request("tools/list")["result"]["tools"]
    names = [t["name"] for t in tools]
    assert names == [
        "motel_checkin",
        "motel_list",
        "motel_status",
        "motel_extend",
        "motel_checkout",
        "motel_restore",
        "motel_doctor",
    ]
    by_name = {t["name"]: t for t in tools}
    for t in tools:
        assert t["description"]
        schema = t["inputSchema"]
        assert schema["type"] == "object"
        assert schema["additionalProperties"] is False
        assert "readOnlyHint" in t["annotations"]
    assert by_name["motel_checkin"]["inputSchema"]["required"] == ["size"]
    assert set(by_name["motel_checkin"]["inputSchema"]["properties"]) == {"size", "duration", "task", "suite"}
    assert by_name["motel_extend"]["inputSchema"]["required"] == ["room", "duration"]
    assert by_name["motel_checkout"]["annotations"]["destructiveHint"] is True
    assert by_name["motel_status"]["annotations"]["readOnlyHint"] is True
    assert "required" not in by_name["motel_doctor"]["inputSchema"]


def test_cabin_tools_hidden_by_default_and_listed_with_flag(env):
    c = env.client()
    c.handshake()
    assert not any("cabin" in t["name"] for t in c.request("tools/list")["result"]["tools"])
    c2 = env.client("--with-cabins")
    c2.handshake()
    names = [t["name"] for t in c2.request("tools/list")["result"]["tools"]]
    assert "motel_cabin_run" in names and "motel_cabin_checkout" in names
    assert c.call("motel_cabin_ls", {})["error"]["code"] == -32602


def test_batch_requests(client):
    client.send(json.dumps([
        {"jsonrpc": "2.0", "id": 1, "method": "ping"},
        {"jsonrpc": "2.0", "method": "notifications/initialized"},
        {"jsonrpc": "2.0", "id": 2, "method": "ping"},
    ]))
    reply = client.recv()
    assert [r["id"] for r in reply] == [1, 2]
