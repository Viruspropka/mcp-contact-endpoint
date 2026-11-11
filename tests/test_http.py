import pytest
from fastapi.testclient import TestClient

from contact_endpoint.http_app import create_app
from contact_endpoint.web import client_ip, origin_allowed

from .conftest import ORIGIN, agent_payload, form_payload


@pytest.fixture
def http(service):
    with TestClient(create_app(service=service)) as client:
        yield client


def test_health(http):
    assert http.get("/health").json() == {"status": "ok", "sinks": ["memory"]}


def test_llms_txt_points_agents_to_mcp(http):
    text = http.get("/llms.txt", headers={"Host": "attacker.example"}).text
    assert "- MCP (Streamable HTTP): /mcp" in text and "leave_message" in text and "Sam Owner" in text
    assert "attacker.example" not in text  # the Host header is never echoed


def test_llms_txt_uses_configured_public_url(settings, sink, clock):
    from dataclasses import replace

    from contact_endpoint.service import ContactService
    from contact_endpoint.sinks import FallbackChain

    svc = ContactService(replace(settings, public_base_url="https://contact.example.com/"),
                         chain=FallbackChain([sink]), clock=clock)
    with TestClient(create_app(service=svc)) as client:
        assert "https://contact.example.com/mcp" in client.get("/llms.txt").text


def test_form_happy_path(http, sink):
    r = http.post("/contact", json=form_payload(), headers={"Origin": ORIGIN})
    assert r.status_code == 200 and r.json() == {"success": True}
    assert len(sink.notes) == 1


def test_form_referer_with_path_is_accepted(http):
    r = http.post("/contact", json=form_payload(), headers={"Referer": ORIGIN + "/contact/"})
    assert r.status_code == 200


@pytest.mark.parametrize("headers", [
    {},
    {"Origin": "https://evil.example"},
    {"Origin": ORIGIN + ".evil.example"},
    {"Referer": ORIGIN + ".evil.example/page"},
])
def test_form_rejects_foreign_origins(http, sink, headers):
    r = http.post("/contact", json=form_payload(), headers=headers)
    assert r.status_code == 403
    assert sink.notes == []


def test_form_validation_error_is_400(http):
    r = http.post("/contact", json=form_payload(email="bad"), headers={"Origin": ORIGIN})
    assert r.status_code == 400 and r.json()["detail"] == "invalid email"


def test_form_honeypot_looks_like_success(http, sink):
    r = http.post("/contact", json=form_payload(website="x"), headers={"Origin": ORIGIN})
    assert r.status_code == 200 and sink.notes == []


def test_invalid_json_and_wrong_shape(http):
    r = http.post("/contact/agent", content=b"{not json", headers={"Content-Type": "application/json"})
    assert r.status_code == 400 and r.json()["detail"] == "invalid json"
    r = http.post("/contact/agent", json=["a", "list"])
    assert r.status_code == 400


def test_oversized_body_is_413(http, sink):
    r = http.post("/contact/agent", json=agent_payload(message="x" * 9000))
    assert r.status_code == 413 and sink.notes == []


def test_oversized_body_without_content_length_is_413(http, sink):
    def chunks():  # chunked transfer: no Content-Length header at all
        yield b'{"name": "Dana Example", "message": "'
        for _ in range(20):
            yield b"x" * 1024
        yield b'"}'

    r = http.post("/contact/agent", content=chunks(), headers={"Content-Type": "application/json"})
    assert r.status_code == 413 and sink.notes == []


def test_form_rate_limit_returns_429(http):
    codes = [http.post("/contact", json=form_payload(), headers={"Origin": ORIGIN}).status_code
             for _ in range(6)]
    assert codes == [200] * 5 + [429]


def test_agent_endpoint_and_its_limits(http, sink):
    codes = [http.post("/contact/agent", json=agent_payload()).status_code for _ in range(4)]
    assert codes == [200, 200, 200, 429]
    assert all("via AI assistant" in n.subject for n in sink.notes)


def test_delivery_failure_is_502(http, sink):
    sink.fail = True
    r = http.post("/contact/agent", json=agent_payload())
    assert r.status_code == 502 and r.json()["detail"] == "delivery_failed"


def test_cors_preflight_only_for_allowed_origin(http):
    ok = http.options("/contact", headers={"Origin": ORIGIN, "Access-Control-Request-Method": "POST"})
    assert ok.headers.get("access-control-allow-origin") == ORIGIN
    bad = http.options("/contact", headers={"Origin": "https://evil.example",
                                            "Access-Control-Request-Method": "POST"})
    assert "access-control-allow-origin" not in bad.headers


class _Req:
    def __init__(self, headers, host="10.0.0.5"):
        self.headers = headers
        self.client = type("C", (), {"host": host})()


def test_proxy_headers_are_ignored_unless_trusted():
    req = _Req({"x-forwarded-for": "1.1.1.1, 2.2.2.2", "x-real-ip": "3.3.3.3"})
    assert client_ip(req, trust_proxy_headers=False) == "10.0.0.5"
    assert client_ip(req, trust_proxy_headers=True) == "3.3.3.3"
    assert client_ip(req, trust_proxy_headers=True, header="x-forwarded-for") == "2.2.2.2"
    assert client_ip(None, trust_proxy_headers=True) == "0.0.0.0"


def test_only_the_named_proxy_header_counts():
    # Proxy configured for X-Real-IP: a client-supplied X-Forwarded-For is ignored.
    spoof = _Req({"x-forwarded-for": "6.6.6.6"})
    assert client_ip(spoof, trust_proxy_headers=True, header="x-real-ip") == "10.0.0.5"
    spoof = _Req({"x-real-ip": "6.6.6.6"})
    assert client_ip(spoof, trust_proxy_headers=True, header="x-forwarded-for") == "10.0.0.5"


def test_origin_matching_is_exact():
    allowed = ["https://site.example"]
    assert origin_allowed("https://site.example", "", allowed)
    assert origin_allowed("", "https://site.example/form", allowed)
    assert not origin_allowed("https://site.example.evil.net", "", allowed)
    assert not origin_allowed("", "https://site.examplex/form", allowed)
