"""Run the app in a background thread on a free local port (demo and tests)."""
from __future__ import annotations

import socket
import threading
import time
from contextlib import contextmanager
from typing import Iterator

import uvicorn

from .http_app import create_app
from .service import ContactService


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]


@contextmanager
def running_server(service: ContactService, port: int | None = None) -> Iterator[str]:
    """Yield the base URL (``http://127.0.0.1:<port>``) while the server runs."""
    port = port or free_port()
    config = uvicorn.Config(create_app(service=service), host="127.0.0.1", port=port,
                            log_level="warning", lifespan="on")
    server = uvicorn.Server(config)
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    deadline = time.monotonic() + 15
    while not server.started:
        if time.monotonic() > deadline or not thread.is_alive():
            raise RuntimeError("server did not start")
        time.sleep(0.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        thread.join(timeout=10)
