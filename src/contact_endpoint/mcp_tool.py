"""MCP front door: one tool, ``leave_message``, over Streamable HTTP at ``/mcp``.

Stateless and JSON-only (no server-side sessions, no SSE streams), so it scales
like any other HTTP endpoint and survives restarts. The tool calls the same
``ContactService`` as the HTTP endpoints, so an agent gets exactly the checks
a browser gets, plus the agent-specific limits.
"""
import inspect

from mcp.server.fastmcp import Context, FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from mcp.types import ToolAnnotations

from .service import ContactService, Status, submit_in_thread
from .web import request_meta

LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOOPBACK_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def _transport_security(service: ContactService) -> TransportSecuritySettings:
    s = service.settings
    if not s.mcp_dns_rebinding_protection:
        # A deliberately public endpoint reached by arbitrary remote MCP clients.
        # The abuse controls live in ContactService, not in Host pinning.
        return TransportSecuritySettings(enable_dns_rebinding_protection=False)
    return TransportSecuritySettings(
        enable_dns_rebinding_protection=True,
        allowed_hosts=list(s.mcp_allowed_hosts) or LOOPBACK_HOSTS,
        allowed_origins=LOOPBACK_ORIGINS + list(s.allowed_origins),
    )


def build_mcp(service: ContactService) -> FastMCP:
    s = service.settings
    who = s.recipient_name
    mcp = FastMCP(
        "contact-endpoint",
        instructions=(
            f"Contact endpoint of {s.site_name}. Use the leave_message tool only when a real "
            f"user asks you to get in touch with {who}."
        ),
        stateless_http=True,
        json_response=True,
        log_level="WARNING",  # the SDK logs every request at INFO
        max_request_body_size=s.mcp_max_body_bytes,
        transport_security=_transport_security(service),
    )

    async def leave_message(
        name: str,
        email: str,
        message: str,
        ctx: Context,
        on_behalf_of: str = "",
        assistant: str = "",
    ) -> str:
        data = {
            "name": name,
            "email": email,
            "message": message,
            "on_behalf_of": on_behalf_of,
            "assistant": assistant or "AI assistant (MCP)",
        }
        request = getattr(ctx.request_context, "request", None)
        meta = request_meta(request, s.trust_proxy_headers, s.client_ip_header)
        outcome = await submit_in_thread(service, data, "agent", meta)

        if outcome.status is Status.DELIVERED or outcome.status is Status.DROPPED:
            # Silent drops (spam heuristics) answer like a success on purpose.
            return f"Delivered. The message reached {who}; any reply will go to {email}."
        if outcome.status is Status.REJECTED:
            return (
                f"Not sent - input rejected ({outcome.reason}). Provide the person's name "
                "(2-100 characters), a valid reply-to email and a message of 10-4000 characters."
            )
        if outcome.status is Status.RATE_LIMITED:
            return "Not sent - rate limit reached for this sender. Do not retry today."
        return "Not sent - delivery failed on the server side. Try again later."

    description = inspect.cleandoc(f"""Leave a message for {who} on behalf of a user who wants to contact them.

    Use this ONLY when a real user asks you to reach {who} (a job offer, a
    collaboration, a question). The message is delivered as plain text and is
    clearly labelled as sent by an AI assistant. Do not send tests, ads or
    unsolicited messages; senders are rate-limited per IP and per reply-to address.

    Args:
        name: The person the message is from (2-100 characters).
        email: A real reply-to email address of that person.
        message: What they want to say (10-4000 characters, at most 2 links).
        on_behalf_of: Optional. The human you act for, if different from `name`.
        assistant: Optional. Your own name or model, for example "Claude".

    Returns:
        A short status line: starts with "Delivered." or "Not sent".
    """)

    mcp.tool(
        name="leave_message",
        title=f"Leave a message for {who}",
        description=description,
        annotations=ToolAnnotations(
            readOnlyHint=False,
            destructiveHint=False,
            idempotentHint=False,
            openWorldHint=True,
        ),
    )(leave_message)
    return mcp
