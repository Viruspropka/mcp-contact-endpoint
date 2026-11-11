"""Small HTTP helpers shared by the HTTP endpoints and the MCP tool."""
from __future__ import annotations

from typing import Any, Iterable

from .service import RequestMeta


def client_ip(request: Any, trust_proxy_headers: bool, header: str = "x-real-ip") -> str:
    """The caller's IP. Proxy headers are spoofable, so exactly one named header
    counts, and only when a reverse proxy you control sets it
    (``CONTACT_TRUST_PROXY_HEADERS=true`` plus ``CONTACT_CLIENT_IP_HEADER``)."""
    if request is None:
        return "0.0.0.0"
    if trust_proxy_headers:
        value = request.headers.get(header.lower(), "")
        if header.lower() == "x-forwarded-for":
            # The proxy appends the real peer last; earlier entries are client-supplied.
            value = value.split(",")[-1]
        value = value.strip()
        if value:
            return value
    client = getattr(request, "client", None)
    return client.host if client else "0.0.0.0"


def request_meta(request: Any, trust_proxy_headers: bool, header: str = "x-real-ip") -> RequestMeta:
    if request is None:
        return RequestMeta()
    return RequestMeta(
        ip=client_ip(request, trust_proxy_headers, header),
        user_agent=(request.headers.get("user-agent") or "")[:300],
        origin=request.headers.get("origin") or "",
    )


def origin_allowed(origin: str, referer: str, allowed: Iterable[str]) -> bool:
    """CSRF guard for the browser form. Origin is scheme+host and must match
    exactly (``startswith`` would also pass ``https://example.com.evil.net``);
    Referer carries a path, so it must extend an allowed origin with ``/``."""
    for base in allowed:
        if origin == base or referer == base or referer.startswith(base + "/"):
            return True
    return False
