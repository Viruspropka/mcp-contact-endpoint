"""Runtime configuration: defaults, then an optional TOML file, then env vars.

Precedence (last wins):
    1. the defaults in ``Settings``
    2. the TOML file named by ``CONTACT_CONFIG`` (see ``config.example.toml``)
    3. environment variables ``CONTACT_<FIELD_NAME_UPPERCASE>``

Secrets (the webhook signing secret) are read from the environment only, never
from the TOML file, so the file can be committed next to a deployment.
"""
from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass, field, fields, replace
from pathlib import Path
from typing import Any, Mapping

ENV_PREFIX = "CONTACT_"
SECRET_FIELDS = frozenset({"webhook_secret"})


class ConfigError(ValueError):
    """Raised when a config value cannot be parsed."""


@dataclass(frozen=True)
class Settings:
    # Who receives the messages. Used in the MCP tool description and replies.
    recipient_name: str = "the site owner"
    site_name: str = "example.com"
    # Absolute base URL used in /llms.txt; empty = relative paths (never echo the Host header).
    public_base_url: str = ""

    # Network
    host: str = "127.0.0.1"
    port: int = 8000
    # Only trust a proxy header when a reverse proxy you control sets it. Exactly
    # one header is read: "x-real-ip" (the proxy must overwrite it) or
    # "x-forwarded-for" (the rightmost entry, i.e. the one your proxy appended).
    trust_proxy_headers: bool = False
    client_ip_header: str = "x-real-ip"

    # Browser form: exact Origin match (CSRF guard) and CORS allow-list.
    allowed_origins: tuple[str, ...] = ("http://localhost:8000", "http://127.0.0.1:8000")

    # Delivery: ordered fallback chain, first sink that accepts the message wins.
    sinks: tuple[str, ...] = ("console",)
    file_sink_path: str = "data/outbox.jsonl"
    webhook_url: str = ""
    webhook_secret: str = field(default="", repr=False)  # env only: CONTACT_WEBHOOK_SECRET
    webhook_timeout_s: float = 10.0

    # Audit log (every attempt, including silent drops). Empty path disables it.
    audit_db_path: str = "data/audit.sqlite"
    retention_days: int = 90
    # Rows for failed attempts (dropped, rejected, rate-limited) per IP per window;
    # beyond this a flood is only counted in the service log, not stored.
    audit_max_failed_per_ip: int = 200

    # Limits
    window_s: int = 24 * 3600
    form_max_per_ip: int = 5
    agent_max_per_ip: int = 20
    agent_max_per_email: int = 3
    min_time_to_submit_s: float = 3.0
    max_urls_in_message: int = 2
    max_body_bytes: int = 8 * 1024

    # MCP transport. DNS-rebinding protection pins the Host header: loopback by
    # default; list your public host (e.g. "contact.example.com") when deployed,
    # or switch it off for a deliberately public endpoint behind a proxy.
    mcp_dns_rebinding_protection: bool = True
    mcp_allowed_hosts: tuple[str, ...] = ()
    mcp_max_body_bytes: int = 64 * 1024


def _coerce(name: str, raw: Any, default: Any) -> Any:
    """Convert a raw TOML/env value to the type of the field's default."""
    try:
        if isinstance(default, bool):
            if isinstance(raw, bool):
                return raw
            text = str(raw).strip().lower()
            if text in {"1", "true", "yes", "on"}:
                return True
            if text in {"0", "false", "no", "off", ""}:
                return False
            raise ValueError(raw)
        if isinstance(default, int):
            return int(raw)
        if isinstance(default, float):
            return float(raw)
        if isinstance(default, tuple):
            if isinstance(raw, (list, tuple)):
                items = raw
            else:
                items = str(raw).split(",")
            return tuple(str(i).strip().rstrip("/") if name == "allowed_origins" else str(i).strip()
                         for i in items if str(i).strip())
        return str(raw)
    except (TypeError, ValueError) as exc:
        raise ConfigError(f"bad value for {name!r}: {raw!r}") from exc


def load_settings(env: Mapping[str, str] | None = None, **overrides: Any) -> Settings:
    """Build Settings from defaults, the optional TOML file and the environment.

    ``overrides`` (keyword arguments) win over everything; tests and the demo use them.
    """
    env = os.environ if env is None else env
    base = Settings()
    known = {f.name: getattr(base, f.name) for f in fields(Settings)}
    values: dict[str, Any] = {}

    config_path = env.get(ENV_PREFIX + "CONFIG", "")
    if config_path:
        path = Path(config_path)
        try:
            data = tomllib.loads(path.read_text(encoding="utf-8"))
        except (OSError, tomllib.TOMLDecodeError) as exc:
            raise ConfigError(f"cannot read config file {path}: {exc}") from exc
        for key, raw in data.items():
            if key in SECRET_FIELDS:
                raise ConfigError(f"{key!r} must come from the environment, not the config file")
            if key in known:
                values[key] = _coerce(key, raw, known[key])

    for key, default in known.items():
        env_key = ENV_PREFIX + key.upper()
        if env_key in env:
            values[key] = _coerce(key, env[env_key], default)

    for key, raw in overrides.items():
        if key not in known:
            raise ConfigError(f"unknown setting {key!r}")
        values[key] = raw if not isinstance(known[key], tuple) else _coerce(key, raw, known[key])

    return replace(base, **values)
