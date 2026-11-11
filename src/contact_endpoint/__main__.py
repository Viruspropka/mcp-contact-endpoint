"""Command line: ``contact-endpoint serve`` or ``contact-endpoint demo``."""
from __future__ import annotations

import argparse
import logging
import sys

from .config import ConfigError, load_settings


def _serve(args: argparse.Namespace) -> int:
    import uvicorn

    from .http_app import create_app

    overrides = {}
    if args.host:
        overrides["host"] = args.host
    if args.port:
        overrides["port"] = args.port
    try:
        settings = load_settings(**overrides)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(name)s: %(message)s")
    uvicorn.run(create_app(settings), host=settings.host, port=settings.port,
                proxy_headers=False, log_level="info")
    return 0


def _demo(_: argparse.Namespace) -> int:
    from .demo import run_demo

    return run_demo()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="contact-endpoint", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    serve = sub.add_parser("serve", help="run the HTTP + MCP server")
    serve.add_argument("--host", default="")
    serve.add_argument("--port", type=int, default=0)
    serve.set_defaults(func=_serve)
    demo = sub.add_parser("demo", help="start a throwaway server and walk through every path")
    demo.set_defaults(func=_demo)
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    raise SystemExit(main())
