from __future__ import annotations

import time
from pathlib import Path

import pytest

from contact_endpoint.config import load_settings
from contact_endpoint.service import ContactService
from contact_endpoint.sinks import DeliveryError, FallbackChain, Notification

ORIGIN = "https://site.example"


class ListSink:
    """Records notifications in memory; can be told to fail."""

    def __init__(self, name: str = "memory", fail: bool = False):
        self.name = name
        self.fail = fail
        self.notes: list[Notification] = []

    def deliver(self, note: Notification) -> None:
        if self.fail:
            raise DeliveryError("memory_down")
        self.notes.append(note)


class FakeClock:
    def __init__(self, start: float = 1_800_000_000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def sink() -> ListSink:
    return ListSink()


@pytest.fixture
def settings(tmp_path: Path):
    return load_settings(
        env={},
        site_name="site.example",
        recipient_name="Sam Owner",
        allowed_origins=(ORIGIN,),
        audit_db_path=str(tmp_path / "audit.sqlite"),
    )


@pytest.fixture
def service(settings, sink, clock) -> ContactService:
    svc = ContactService(settings, chain=FallbackChain([sink]), clock=clock)
    svc.start()
    return svc


def agent_payload(**overrides):
    data = {
        "name": "Dana Example",
        "email": "dana@example.org",
        "message": "Dana would like to discuss a contract role.",
        "assistant": "TestAgent",
    }
    data.update(overrides)
    return data


def form_payload(**overrides):
    data = {
        "name": "Erin Visitor",
        "email": "erin@example.com",
        "message": "Hello from the website form, nice project!",
        "website": "",
        "ts": int((time.time() - 30) * 1000),
    }
    data.update(overrides)
    return data
