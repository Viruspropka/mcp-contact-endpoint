"""Call the ``leave_message`` tool of a running contact endpoint over MCP.

This is what any MCP-capable assistant does under the hood. Start the server
first (``contact-endpoint serve`` or ``docker compose up``), then:

    python examples/mcp_client.py --name "Dana Example" --email dana@example.org \
        --message "Hi, Dana would like to talk about a contract role."

Only the official ``mcp`` SDK is used, no code from this repository.
"""
from __future__ import annotations

import argparse

import anyio
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client


async def main(args: argparse.Namespace) -> None:
    async with streamable_http_client(args.url) as (read, write, _):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"connected to {init.serverInfo.name}")

            tools = (await session.list_tools()).tools
            for tool in tools:
                print(f"- tool {tool.name}: {(tool.description or '').splitlines()[0]}")

            result = await session.call_tool("leave_message", {
                "name": args.name,
                "email": args.email,
                "message": args.message,
                "assistant": "examples/mcp_client.py",
            })
            for part in result.content:
                print("result:", getattr(part, "text", part))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--url", default="http://127.0.0.1:8000/mcp")
    parser.add_argument("--name", default="Dana Example")
    parser.add_argument("--email", default="dana@example.org")
    parser.add_argument("--message", default="Hi! This is a test message sent by the example MCP client.")
    anyio.run(main, parser.parse_args())
