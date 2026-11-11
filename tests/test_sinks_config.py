import hashlib
import hmac
import io
import json

import httpx
import pytest

from contact_endpoint.config import ConfigError, load_settings
from contact_endpoint.sinks import (
    ConsoleSink,
    DeliveryError,
    FileSink,
    Notification,
    WebhookSink,
    build_chain,
)

NOTE = Notification(subject="s", body="hello\nworld", reply_to="a@example.org", channel="agent")


def test_file_sink_appends_jsonl(tmp_path):
    path = tmp_path / "nested" / "outbox.jsonl"
    sink = FileSink(path)
    sink.deliver(NOTE)
    sink.deliver(NOTE)
    lines = path.read_text().splitlines()
    assert len(lines) == 2 and json.loads(lines[0])["body"] == "hello\nworld"


def test_console_sink_writes_plain_text():
    buf = io.StringIO()
    ConsoleSink(buf).deliver(NOTE)
    assert "Reply-To: a@example.org" in buf.getvalue() and "hello\nworld" in buf.getvalue()


def test_webhook_sink_signs_the_body():
    seen = {}

    def handler(request: httpx.Request) -> httpx.Response:
        seen["body"] = request.content
        seen["sig"] = request.headers["x-contact-signature"]
        return httpx.Response(204)

    WebhookSink("https://hooks.example/in", secret="s3cret", transport=httpx.MockTransport(handler)).deliver(NOTE)
    expected = hmac.new(b"s3cret", seen["body"], hashlib.sha256).hexdigest()
    assert seen["sig"] == f"sha256={expected}"
    assert json.loads(seen["body"])["reply_to"] == "a@example.org"


def test_webhook_sink_without_secret_sends_no_signature():
    def handler(request: httpx.Request) -> httpx.Response:
        assert "x-contact-signature" not in request.headers
        return httpx.Response(200)

    WebhookSink("https://hooks.example/in", transport=httpx.MockTransport(handler)).deliver(NOTE)


@pytest.mark.parametrize("response", [httpx.Response(500), httpx.Response(302)])
def test_webhook_sink_errors(response):
    sink = WebhookSink("https://hooks.example/in", transport=httpx.MockTransport(lambda r: response))
    with pytest.raises(DeliveryError, match=f"webhook_http_{response.status_code}"):
        sink.deliver(NOTE)


def test_webhook_transport_error_has_fixed_code():
    def boom(request):
        raise httpx.ConnectError("down")

    with pytest.raises(DeliveryError, match="webhook_transport_ConnectError"):
        WebhookSink("https://hooks.example/in", transport=httpx.MockTransport(boom)).deliver(NOTE)


def test_build_chain_validates_names(tmp_path):
    chain = build_chain(("file", "console"), file_path=str(tmp_path / "o.jsonl"),
                        webhook_url="", webhook_secret="", webhook_timeout_s=1)
    assert [s.name for s in chain.sinks] == ["file", "console"]
    with pytest.raises(ValueError):
        build_chain(("pager",), file_path="", webhook_url="", webhook_secret="", webhook_timeout_s=1)
    with pytest.raises(ValueError):
        build_chain(("webhook",), file_path="", webhook_url="", webhook_secret="", webhook_timeout_s=1)


def test_settings_from_env():
    s = load_settings(env={
        "CONTACT_PORT": "9001",
        "CONTACT_TRUST_PROXY_HEADERS": "yes",
        "CONTACT_SINKS": "file, webhook",
        "CONTACT_ALLOWED_ORIGINS": "https://a.example/, https://b.example",
        "CONTACT_WEBHOOK_SECRET": "x",
    })
    assert s.port == 9001 and s.trust_proxy_headers is True
    assert s.sinks == ("file", "webhook")
    assert s.allowed_origins == ("https://a.example", "https://b.example")
    assert s.webhook_secret == "x"


def test_settings_from_toml_with_env_winning(tmp_path):
    cfg = tmp_path / "contact.toml"
    cfg.write_text('recipient_name = "Sam"\nform_max_per_ip = 7\nsinks = ["file"]\n')
    s = load_settings(env={"CONTACT_CONFIG": str(cfg), "CONTACT_FORM_MAX_PER_IP": "9"})
    assert s.recipient_name == "Sam" and s.sinks == ("file",) and s.form_max_per_ip == 9


def test_secret_in_toml_is_refused(tmp_path):
    cfg = tmp_path / "contact.toml"
    cfg.write_text('webhook_secret = "oops"\n')
    with pytest.raises(ConfigError, match="environment"):
        load_settings(env={"CONTACT_CONFIG": str(cfg)})


def test_bad_values_raise_config_error(tmp_path):
    with pytest.raises(ConfigError):
        load_settings(env={"CONTACT_PORT": "eighty"})
    with pytest.raises(ConfigError):
        load_settings(env={"CONTACT_TRUST_PROXY_HEADERS": "maybe"})
    with pytest.raises(ConfigError):
        load_settings(env={"CONTACT_CONFIG": str(tmp_path / "missing.toml")})
    with pytest.raises(ConfigError):
        load_settings(env={}, no_such_setting=1)
