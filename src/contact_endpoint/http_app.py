"""The ASGI app: classic HTTP endpoints plus the MCP endpoint on one port.

    GET  /health          liveness + which sinks are configured
    GET  /llms.txt        plain-text discovery note for AI agents
    POST /contact         browser form (Origin check, honeypot, time-to-submit)
    POST /contact/agent   JSON endpoint for agents that do not speak MCP
    POST /mcp             MCP Streamable HTTP (tool: leave_message)
"""
from __future__ import annotations

import json
import logging
from contextlib import asynccontextmanager
from typing import Any, AsyncIterator

from fastapi import FastAPI, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, PlainTextResponse

from .config import Settings, load_settings
from .mcp_tool import build_mcp
from .service import ContactService, Outcome, Status, submit_in_thread
from .web import origin_allowed, request_meta

log = logging.getLogger("contact_endpoint")

_HTTP_STATUS = {
    Status.DELIVERED: 200,
    Status.DROPPED: 200,  # look like success to bots
    Status.REJECTED: 400,
    Status.RATE_LIMITED: 429,
    Status.DELIVERY_FAILED: 502,
}


def _response(outcome: Outcome) -> JSONResponse:
    code = _HTTP_STATUS[outcome.status]
    if code == 200:
        return JSONResponse({"success": True})
    detail = outcome.reason if outcome.status is Status.REJECTED else outcome.status.value
    return JSONResponse({"success": False, "detail": detail}, status_code=code)


async def _read_capped(request: Request, limit: int) -> bytes | None:
    """Read at most ``limit`` bytes; None when the body is larger.

    Rejects on Content-Length before reading anything, then counts while
    streaming, so a lying or missing Content-Length cannot make us buffer more.
    """
    declared = request.headers.get("content-length", "")
    if declared.isdigit() and int(declared) > limit:
        return None
    chunks: list[bytes] = []
    size = 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > limit:
            return None
        chunks.append(chunk)
    return b"".join(chunks)


async def _read_json(request: Request, limit: int) -> tuple[Any, JSONResponse | None]:
    body = await _read_capped(request, limit)
    if body is None:
        return None, JSONResponse({"success": False, "detail": "payload too large"}, status_code=413)
    try:
        data = json.loads(body)
    except ValueError:
        return None, JSONResponse({"success": False, "detail": "invalid json"}, status_code=400)
    if not isinstance(data, dict):
        return None, JSONResponse({"success": False, "detail": "payload must be a JSON object"},
                                  status_code=400)
    return data, None


def create_app(settings: Settings | None = None, service: ContactService | None = None) -> FastAPI:
    settings = settings or (service.settings if service else load_settings())
    service = service or ContactService(settings)
    mcp = build_mcp(service)
    mcp_app = mcp.streamable_http_app()  # creates mcp.session_manager

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        service.start()
        async with mcp.session_manager.run():
            yield

    app = FastAPI(title="mcp-contact-endpoint", lifespan=lifespan,
                  docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    app.add_middleware(
        CORSMiddleware,
        allow_origins=list(settings.allowed_origins),
        allow_methods=["POST", "OPTIONS"],
        allow_headers=["Content-Type", "Accept"],
        allow_credentials=False,
    )

    @app.get("/health")
    def health() -> dict[str, Any]:
        return {"status": "ok", "sinks": [s.name for s in service.chain.sinks]}

    @app.get("/llms.txt", response_class=PlainTextResponse)
    def llms_txt() -> str:
        base = settings.public_base_url.rstrip("/")  # empty: relative paths
        return (
            f"# {settings.site_name} contact\n\n"
            f"> AI assistants can leave a message for {settings.recipient_name} on a user's behalf.\n\n"
            f"- MCP (Streamable HTTP): {base}/mcp - tool `leave_message`\n"
            f"- JSON: POST {base}/contact/agent with name, email, message, "
            "on_behalf_of (optional), assistant (optional)\n"
            "- Use only when a real user asks to get in touch. Rate-limited per IP and per reply-to.\n"
        )

    @app.post("/contact")
    async def contact(request: Request) -> JSONResponse:
        meta = request_meta(request, settings.trust_proxy_headers, settings.client_ip_header)
        referer = request.headers.get("referer") or ""
        if not origin_allowed(meta.origin, referer, settings.allowed_origins):
            service.record_attempt("form", Status.REJECTED, "origin", meta)
            return JSONResponse({"success": False, "detail": "forbidden origin"}, status_code=403)
        data, error = await _read_json(request, settings.max_body_bytes)
        if error:
            return error
        return _response(await submit_in_thread(service, data, "form", meta))

    @app.post("/contact/agent")
    async def contact_agent(request: Request) -> JSONResponse:
        meta = request_meta(request, settings.trust_proxy_headers, settings.client_ip_header)
        data, error = await _read_json(request, settings.max_body_bytes)
        if error:
            return error
        return _response(await submit_in_thread(service, data, "agent", meta))

    # Everything else (i.e. /mcp) goes to the MCP Starlette app.
    app.mount("/", mcp_app)
    return app
