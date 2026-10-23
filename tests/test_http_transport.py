"""Exercise the real MCP HTTP adapter in-process, without opening a port."""

from starlette.testclient import TestClient

from research_hub import bridge


def test_http_authentication_and_read_only_tool_surface(monkeypatch):
    monkeypatch.setattr(bridge, "config", lambda: {"sco_enabled": False, "sco_executable": ""})
    secret = "synthetic-test-token-" * 3
    app = bridge.LoopbackBearer(bridge.mcp.streamable_http_app(), secret)
    headers = {"Authorization": "Bearer " + secret, "Accept": "application/json, text/event-stream"}
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "offline-test", "version": "1"},
        },
    }
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        assert client.get("/healthz").status_code == 200
        assert client.post("/mcp", json=initialize).status_code == 401
        assert (
            client.post(
                "/mcp", json=initialize, headers={**headers, "Origin": "https://untrusted.invalid"}
            ).status_code
            == 403
        )
        assert client.post("/mcp", json=initialize, headers=headers).status_code == 200
        result = client.post(
            "/mcp",
            json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
            headers=headers,
        ).json()
        tools = result["result"]["tools"]
        assert {t["name"] for t in tools} == {
            "get_bridge_health",
            "list_research_tasks",
            "get_task_progress",
            "get_experiment_report",
        }
        assert all(t["annotations"]["readOnlyHint"] for t in tools)
        assert all(not t["annotations"]["destructiveHint"] for t in tools)
