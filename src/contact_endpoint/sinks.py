"""Delivery sinks: where an accepted message goes.

A sink takes a plain-text ``Notification`` and either accepts it or raises
``DeliveryError``. ``FallbackChain`` tries sinks in order and reports which one
took the message, the same pattern as the original service (e-mail first,
chat-bot fallback second, HTTP 502 when nothing took it).

Built in:
    console  - prints to stdout (default, good for local runs)
    file     - appends one JSON object per line to a file (a local "outbox")
    webhook  - POSTs JSON to a URL, optionally signed with HMAC-SHA256;
               point it at Slack/Discord/n8n/your mailer
"""
from __future__ import annotations

import hashlib
import hmac
import json
import sys
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock
from typing import Protocol, TextIO

import httpx


class DeliveryError(RuntimeError):
    """A sink did not accept the message. The text is a fixed code, never content."""


@dataclass(frozen=True)
class Notification:
    subject: str
    body: str
    reply_to: str
    channel: str  # "form" or "agent"
    created_at: str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())


class Sink(Protocol):
    name: str

    def deliver(self, note: Notification) -> None: ...


class ConsoleSink:
    name = "console"

    def __init__(self, stream: TextIO | None = None):
        self._stream = stream

    def deliver(self, note: Notification) -> None:
        stream = self._stream or sys.stdout
        bar = "=" * 60
        stream.write(f"\n{bar}\n{note.subject}\nReply-To: {note.reply_to}\n{bar}\n{note.body}\n{bar}\n")
        stream.flush()


class FileSink:
    name = "file"

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._lock = Lock()

    def deliver(self, note: Notification) -> None:
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            line = json.dumps(asdict(note), ensure_ascii=False)
            with self._lock, self.path.open("a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except OSError as exc:
            raise DeliveryError(f"file_{type(exc).__name__}") from None


class WebhookSink:
    """POST the notification as JSON. With a secret, the body is signed:

    ``X-Contact-Signature: sha256=<hex HMAC of the raw body>``
    """

    name = "webhook"

    def __init__(self, url: str, secret: str = "", timeout_s: float = 10.0,
                 transport: httpx.BaseTransport | None = None):
        if not url:
            raise ValueError("webhook sink needs CONTACT_WEBHOOK_URL")
        self.url = url
        self._secret = secret.encode()
        self._timeout = timeout_s
        self._transport = transport

    def deliver(self, note: Notification) -> None:
        body = json.dumps(asdict(note), ensure_ascii=False).encode()
        headers = {"Content-Type": "application/json", "User-Agent": "mcp-contact-endpoint/0.1"}
        if self._secret:
            sig = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
            headers["X-Contact-Signature"] = f"sha256={sig}"
        try:
            with httpx.Client(timeout=self._timeout, transport=self._transport) as client:
                resp = client.post(self.url, content=body, headers=headers)
        except httpx.HTTPError as exc:
            raise DeliveryError(f"webhook_transport_{type(exc).__name__}") from None
        if not 200 <= resp.status_code < 300:
            raise DeliveryError(f"webhook_http_{resp.status_code}")


class FallbackChain:
    def __init__(self, sinks: list[Sink]):
        if not sinks:
            raise ValueError("at least one sink is required")
        self.sinks = sinks

    def deliver(self, note: Notification) -> str:
        """Return the name of the sink that accepted the message."""
        errors: list[str] = []
        for sink in self.sinks:
            try:
                sink.deliver(note)
                return sink.name
            except DeliveryError as exc:
                errors.append(f"{sink.name}:{exc}")
        raise DeliveryError("all_sinks_failed " + " ".join(errors))


def build_chain(names: tuple[str, ...], *, file_path: str, webhook_url: str,
                webhook_secret: str, webhook_timeout_s: float) -> FallbackChain:
    sinks: list[Sink] = []
    for name in names:
        if name == "console":
            sinks.append(ConsoleSink())
        elif name == "file":
            sinks.append(FileSink(file_path))
        elif name == "webhook":
            sinks.append(WebhookSink(webhook_url, webhook_secret, webhook_timeout_s))
        else:
            raise ValueError(f"unknown sink {name!r} (use console, file, webhook)")
    return FallbackChain(sinks)
