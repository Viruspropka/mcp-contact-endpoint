"""Input normalisation and validation. Pure functions, no I/O.

Everything a visitor or an AI agent sends is treated as untrusted text:

* control characters and Unicode bidi overrides are stripped (they can hide or
  reorder text in the notification the owner reads),
* single-line fields (name, assistant, on_behalf_of) are collapsed to one line,
  so a name such as ``"Bob\\nEmail: ceo@bank.example"`` cannot forge a header
  line in the plain-text notification,
* lengths and the e-mail format are checked,
* links are counted (more than ``max_urls`` = spam, dropped silently),
* common prompt-injection phrases are *flagged* (not blocked) so a human or a
  downstream LLM triage step knows the text tries to give instructions.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Mapping

NAME_MIN, NAME_MAX = 2, 100
EMAIL_MAX = 254
MSG_MIN, MSG_MAX = 10, 4000
ASSISTANT_MAX = 60
ON_BEHALF_MAX = 100

EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$")
URL_RE = re.compile(r"https?://", re.IGNORECASE)

# C0/C1 controls except \t and \n, zero-width and bidi control characters.
_CTRL_RE = re.compile(
    "[\x00-\x08\x0b-\x1f\x7f-\x9f​-‏‪-‮⁦-⁩﻿]"
)
_WS_RE = re.compile(r"\s+")

# Heuristic markers of text that tries to instruct an LLM reading it later.
# Flag only: a real person may legitimately write "ignore my previous message".
INJECTION_PATTERNS = (
    re.compile(r"\bignore\s+(all\s+|any\s+)?(previous|prior|above|earlier)\s+(instructions|prompts?|rules)", re.I),
    re.compile(r"\b(system|developer)\s+prompt\b", re.I),
    re.compile(r"\byou\s+are\s+now\b", re.I),
    re.compile(r"</?\s*(system|assistant|instructions?)\s*>", re.I),
    re.compile(r"\b(disregard|override)\s+(your|the)\s+(instructions|rules|guidelines)", re.I),
)


class ValidationError(ValueError):
    """The input is malformed; the caller gets a 400 / a 'rejected' answer."""

    def __init__(self, reason: str):
        super().__init__(reason)
        self.reason = reason


@dataclass(frozen=True)
class Message:
    name: str
    email: str
    message: str
    assistant: str = ""
    on_behalf_of: str = ""
    injection_flags: tuple[str, ...] = field(default_factory=tuple)


def strip_controls(text: str, *, keep_newlines: bool) -> str:
    # Normalise line endings first: a bare \r is a line break, not junk.
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _CTRL_RE.sub("", text)
    if not keep_newlines:
        text = _WS_RE.sub(" ", text)
    return text.strip()


def _field(data: Mapping[str, Any], key: str, limit: int, *, multiline: bool = False) -> str:
    raw = data.get(key)
    if raw is None:
        return ""
    if not isinstance(raw, str):
        raise ValidationError(f"{key} must be a string")
    return strip_controls(raw, keep_newlines=multiline)[:limit]


def count_urls(text: str) -> int:
    return len(URL_RE.findall(text))


def injection_flags(*texts: str) -> tuple[str, ...]:
    hits: list[str] = []
    for text in texts:
        for pattern in INJECTION_PATTERNS:
            match = pattern.search(text)
            if match and match.group(0).lower() not in hits:
                hits.append(match.group(0).lower())
    return tuple(hits)


def parse_message(data: Mapping[str, Any], *, agent: bool) -> Message:
    """Normalise and validate a submission. Raises ValidationError."""
    if not isinstance(data, Mapping):
        raise ValidationError("payload must be a JSON object")
    name = _field(data, "name", NAME_MAX)
    email = _field(data, "email", EMAIL_MAX)
    message = _field(data, "message", MSG_MAX, multiline=True)
    assistant = _field(data, "assistant", ASSISTANT_MAX) if agent else ""
    on_behalf = _field(data, "on_behalf_of", ON_BEHALF_MAX) if agent else ""

    if len(name) < NAME_MIN:
        raise ValidationError("name too short")
    if not EMAIL_RE.match(email):
        raise ValidationError("invalid email")
    if len(message) < MSG_MIN:
        raise ValidationError("message too short")

    return Message(
        name=name,
        email=email,
        message=message,
        assistant=assistant,
        on_behalf_of=on_behalf,
        injection_flags=injection_flags(name, message, assistant, on_behalf),
    )


def honeypot_filled(data: Mapping[str, Any]) -> bool:
    """Browser form only: the hidden ``website`` field is filled by bots."""
    return bool(data.get("website") or data.get("hp"))


def submitted_too_fast(data: Mapping[str, Any], now_s: float, min_seconds: float) -> bool:
    """Browser form only: ``ts`` is the page-load time in ms set by the form script."""
    ts = data.get("ts")
    if isinstance(ts, bool) or not isinstance(ts, (int, float)) or ts <= 0:
        return False
    return now_s - ts / 1000.0 < min_seconds
