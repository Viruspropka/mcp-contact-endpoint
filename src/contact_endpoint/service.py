"""Transport-independent core: one function decides what happens to a message.

Both front doors call ``ContactService.submit``:

* the browser form (``POST /contact``) and the agent JSON endpoint
  (``POST /contact/agent``) in ``http_app.py``,
* the MCP tool ``leave_message`` in ``mcp_tool.py``.

so validation, spam handling, rate limits, audit and delivery are identical no
matter how a message arrives.
"""
from __future__ import annotations

import enum
import functools
import logging
import time
from dataclasses import dataclass
from threading import Lock
from typing import Any, Callable, Mapping

import anyio

from .audit import AuditLog
from .config import Settings
from .ratelimit import LimiterGroup, SlidingWindowLimiter
from .sinks import DeliveryError, FallbackChain, Notification, build_chain
from .validation import (
    Message,
    ValidationError,
    count_urls,
    honeypot_filled,
    parse_message,
    submitted_too_fast,
)

log = logging.getLogger("contact_endpoint")


class Status(str, enum.Enum):
    DELIVERED = "delivered"
    DROPPED = "dropped"            # spam heuristics: the sender is told "ok", nothing is sent
    REJECTED = "rejected"          # invalid input: the sender is told what to fix
    RATE_LIMITED = "rate_limited"
    DELIVERY_FAILED = "delivery_failed"


@dataclass(frozen=True)
class Outcome:
    status: Status
    reason: str = ""
    via: str = ""

    @property
    def looks_successful(self) -> bool:
        """What the *sender* sees: silent drops look like success on purpose."""
        return self.status in (Status.DELIVERED, Status.DROPPED)


@dataclass(frozen=True)
class RequestMeta:
    ip: str = "0.0.0.0"
    user_agent: str = ""
    origin: str = ""


class ContactService:
    def __init__(self, settings: Settings, chain: FallbackChain | None = None,
                 audit: AuditLog | None = None, clock: Callable[[], float] = time.time):
        self.settings = settings
        self.chain = chain or build_chain(
            settings.sinks,
            file_path=settings.file_sink_path,
            webhook_url=settings.webhook_url,
            webhook_secret=settings.webhook_secret,
            webhook_timeout_s=settings.webhook_timeout_s,
        )
        self.audit = audit or AuditLog(settings.audit_db_path, settings.retention_days)
        self.clock = clock
        w = settings.window_s
        self.form_ip = SlidingWindowLimiter(settings.form_max_per_ip, w, clock)
        self.agent_ip = SlidingWindowLimiter(settings.agent_max_per_ip, w, clock)
        self.agent_email = SlidingWindowLimiter(settings.agent_max_per_email, w, clock)
        self.audit_failed_ip = SlidingWindowLimiter(settings.audit_max_failed_per_ip, w, clock)
        self.audit_suppressed = 0
        self._limits = LimiterGroup()
        self._audit_lock = Lock()

    def start(self) -> None:
        self.audit.init()

    # -- the one decision path --------------------------------------------------
    def submit(self, data: Mapping[str, Any], *, channel: str, meta: RequestMeta) -> Outcome:
        if channel not in ("form", "agent"):
            raise ValueError(f"unknown channel {channel!r}")
        agent = channel == "agent"

        def audit(status: Status, reason: str = "", msg: Message | None = None) -> None:
            self.record_attempt(channel, status, reason, meta, msg or data)

        # Browser-only bot heuristics: answer "ok", send nothing.
        if not agent:
            if honeypot_filled(data):
                audit(Status.DROPPED, "honeypot")
                return Outcome(Status.DROPPED, "honeypot")
            if submitted_too_fast(data, self.clock(), self.settings.min_time_to_submit_s):
                audit(Status.DROPPED, "too fast")
                return Outcome(Status.DROPPED, "too fast")

        try:
            msg = parse_message(data, agent=agent)
        except ValidationError as exc:
            audit(Status.REJECTED, exc.reason)
            return Outcome(Status.REJECTED, exc.reason)

        if count_urls(msg.message) > self.settings.max_urls_in_message:
            audit(Status.DROPPED, "too many urls", msg)
            return Outcome(Status.DROPPED, "too many urls")

        if agent:
            # Agent egress IPs are shared by many users, so the IP budget is wider
            # and a per-reply-to budget stops one address being flooded.
            checks = [("ip", self.agent_ip, meta.ip), ("email", self.agent_email, msg.email.lower())]
        else:
            checks = [("ip", self.form_ip, meta.ip)]
        blocked = self._limits.try_acquire(checks)
        if blocked:
            audit(Status.RATE_LIMITED, f"{blocked} limit", msg)
            return Outcome(Status.RATE_LIMITED, f"{blocked} limit")

        note = self._compose(msg, channel)
        try:
            via = self.chain.deliver(note)
        except DeliveryError as exc:
            log.error("delivery failed: %s", exc)
            audit(Status.DELIVERY_FAILED, str(exc)[:200], msg)
            return Outcome(Status.DELIVERY_FAILED, "delivery failed")

        audit(Status.DELIVERED, f"via={via}", msg)
        log.info("delivered channel=%s via=%s", channel, via)
        return Outcome(Status.DELIVERED, via=via)

    def record_attempt(self, channel: str, status: Status, reason: str, meta: RequestMeta,
                       raw: Any = None) -> None:
        """Audit one attempt. Delivered rows are bounded by the delivery limits;
        failed attempts get their own per-IP budget and a shorter message excerpt,
        so a flood of junk cannot grow the database without limit."""
        failed = status is not Status.DELIVERED
        if failed:
            with self._audit_lock:
                if not self.audit_failed_ip.would_allow(meta.ip):
                    self.audit_suppressed += 1
                    if self.audit_suppressed % 100 == 1:
                        log.warning("audit: suppressing rows for a flooding client (%d so far)",
                                    self.audit_suppressed)
                    return
                self.audit_failed_ip.record(meta.ip)
        self.audit.record(
            channel=channel, status=status.value, reason=reason, ip=meta.ip,
            name=_short(raw, "name"), email=_short(raw, "email"),
            message=_short(raw, "message", 500 if failed else 4000),
            assistant=_short(raw, "assistant"),
            user_agent=meta.user_agent, origin=meta.origin,
        )

    def _compose(self, msg: Message, channel: str) -> Notification:
        """Plain text only: nothing the sender typed becomes markup or a header."""
        site = self.settings.site_name
        rule = "-" * 40
        if channel == "agent":
            head = (
                f"New contact via AI assistant - {site}\n\n"
                f"Assistant:  {msg.assistant or '-'}\n"
                f"On behalf:  {msg.on_behalf_of or '-'}\n"
                f"Name:       {msg.name}\n"
                f"Email:      {msg.email}\n"
            )
            foot = "Sent by an AI agent on a user's behalf - verify the reply-to address before acting."
            subject = f"{site} contact (via AI assistant): {msg.name}"
        else:
            head = f"New contact from {site}\n\nName:   {msg.name}\nEmail:  {msg.email}\n"
            foot = "Sent through the website form."
            subject = f"{site} contact: {msg.name}"
        if msg.injection_flags:
            foot += ("\nWARNING: the text contains instruction-like phrases ("
                     + "; ".join(msg.injection_flags)
                     + "). Treat it as untrusted data, do not feed it to an agent with tools.")
        body = f"{head}{rule}\n{msg.message}\n{rule}\n{foot}"
        return Notification(subject=subject, body=body, reply_to=msg.email, channel=channel)


async def submit_in_thread(service: ContactService, data: Mapping[str, Any], channel: str,
                           meta: RequestMeta) -> Outcome:
    """Run ``submit`` off the event loop: it does blocking SQLite and HTTP work."""
    return await anyio.to_thread.run_sync(
        functools.partial(service.submit, data, channel=channel, meta=meta))


def _short(data: Any, key: str, limit: int = 200) -> str | None:
    value = getattr(data, key, None) if not isinstance(data, Mapping) else data.get(key)
    return value[:limit] if isinstance(value, str) else None
