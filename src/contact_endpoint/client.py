"""Minimal MCP client helpers (used by the demo and the tests)."""
from __future__ import annotations

from typing import Any

from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def list_tools(url: str) -> list[Any]:
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            return (await session.list_tools()).tools


async def leave_message(url: str, **arguments: Any) -> tuple[str, bool]:
    """Call the tool once. Returns (text of the result, is_error)."""
    async with streamable_http_client(url) as (read, write, _):
        async with ClientSession(read, write) as session:
            await session.initialize()
            result = await session.call_tool("leave_message", arguments)
    text = "\n".join(getattr(part, "text", "") for part in result.content)
    return text, bool(result.isError)
