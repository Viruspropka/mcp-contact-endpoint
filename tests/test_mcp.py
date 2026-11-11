"""End-to-end over a real socket with the official MCP client."""
import anyio
import httpx
import pytest

from contact_endpoint import client
from contact_endpoint.config import load_settings
from contact_endpoint.service import ContactService
from contact_endpoint.sinks import FallbackChain
from contact_endpoint.testing import running_server

from .conftest import ListSink, agent_payload


@pytest.fixture
def server(service):
    with running_server(service) as base:
        yield base


def test_tool_is_listed_with_schema_and_annotations(server):
    tools = anyio.run(client.list_tools, f"{server}/mcp")
    assert [t.name for t in tools] == ["leave_message"]
    tool = tools[0]
    props = tool.inputSchema["properties"]
    assert {"name", "email", "message", "on_behalf_of", "assistant"} <= set(props)
    assert "ctx" not in props
    assert set(tool.inputSchema["required"]) == {"name", "email", "message"}
    assert "Sam Owner" in tool.description
    assert tool.annotations.destructiveHint is False and tool.annotations.openWorldHint is True


def call(server, **args):
    return anyio.run(lambda: client.leave_message(f"{server}/mcp", **args))


def test_valid_call_is_delivered(server, sink):
    text, is_error = call(server, **agent_payload(assistant="", on_behalf_of="Lee"))
    assert text.startswith("Delivered.") and not is_error
    note = sink.notes[0]
    assert note.channel == "agent" and "Assistant:  AI assistant (MCP)" in note.body
    assert "On behalf:  Lee" in note.body


def test_invalid_call_explains_what_to_fix(server, sink):
    text, _ = call(server, **agent_payload(email="not-an-email"))
    assert text.startswith("Not sent - input rejected (invalid email)")
    assert sink.notes == []


def test_rate_limit_over_mcp(server, sink):
    texts = [call(server, **agent_payload())[0] for _ in range(4)]
    assert [t.split(" ")[0] for t in texts] == ["Delivered.", "Delivered.", "Delivered.", "Not"]
    assert "rate limit" in texts[-1]
    assert len(sink.notes) == 3


def test_spam_drop_looks_like_success_to_the_agent(server, sink):
    links = "see http://a.example http://b.example http://c.example now"
    text, _ = call(server, **agent_payload(message=links))
    assert text.startswith("Delivered.") and sink.notes == []


def test_missing_required_argument_is_a_tool_error(server, sink):
    text, is_error = call(server, name="Dana Example", email="dana@example.org")
    assert is_error and sink.notes == []


def _raw_initialize(base: str, host: str) -> int:
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
        "protocolVersion": "2025-06-18", "capabilities": {},
        "clientInfo": {"name": "raw", "version": "0"}}}
    r = httpx.post(f"{base}/mcp", json=body, headers={
        "Host": host, "Accept": "application/json, text/event-stream"})
    return r.status_code


def test_dns_rebinding_protection_rejects_foreign_host(server):
    assert _raw_initialize(server, "127.0.0.1:1234") == 200
    assert _raw_initialize(server, "attacker.example") in (400, 421)


def test_public_mode_accepts_any_host(tmp_path, clock):
    settings = load_settings(env={}, mcp_dns_rebinding_protection=False,
                             audit_db_path=str(tmp_path / "a.sqlite"))
    svc = ContactService(settings, chain=FallbackChain([ListSink()]), clock=clock)
    with running_server(svc) as base:
        assert _raw_initialize(base, "contact.example.com") == 200


def test_configured_public_host_is_allowed(tmp_path, clock):
    settings = load_settings(env={}, mcp_allowed_hosts=("contact.example.com",),
                             audit_db_path=str(tmp_path / "a.sqlite"))
    svc = ContactService(settings, chain=FallbackChain([ListSink()]), clock=clock)
    with running_server(svc) as base:
        assert _raw_initialize(base, "contact.example.com") == 200
        assert _raw_initialize(base, "attacker.example") in (400, 421)
