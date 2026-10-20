"""#3637 — an SMTP send runs off the event loop and carries a socket timeout.

`smtplib` is synchronous. Called inside the `send_email` coroutine it blocks
the worker's loop for the whole connect/auth/send, and a caller's
`asyncio.wait_for` bound cannot fire. The fake below blocks the thread the
way a slow SMTP server does (`time.sleep`, never `asyncio.sleep`).
"""
from __future__ import annotations

import asyncio
import time

import pytest

pytestmark = pytest.mark.unit


class _FakeSMTP:
    calls: list = []
    block_s = 0.0

    def __init__(self, *args, **kwargs):
        _FakeSMTP.calls.append((args, kwargs))
        time.sleep(_FakeSMTP.block_s)

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def starttls(self):
        pass

    def login(self, *a):
        pass

    def sendmail(self, *a):
        pass


@pytest.fixture
def svc(monkeypatch):
    import services.email_service as es

    _FakeSMTP.calls = []
    _FakeSMTP.block_s = 0.0
    monkeypatch.setattr(es.smtplib, "SMTP", _FakeSMTP)
    monkeypatch.setattr(es, "SMTP_HOST", "smtp.example.com")
    monkeypatch.setattr(es, "SMTP_USER", "mailer")
    monkeypatch.setattr(es, "SMTP_PASSWORD", "secret")
    service = es.EmailService()
    monkeypatch.setattr(service, "_runtime_config", lambda: ("smtp", "", "noreply@example.com"))
    return service, es


def test_send_returns_the_smtp_result_and_passes_a_timeout(svc):
    service, es = svc
    assert asyncio.run(service.send_email("user@example.com", "s", "b")) is True
    (args, kwargs), = _FakeSMTP.calls
    assert args == ("smtp.example.com", es.SMTP_PORT)
    assert kwargs.get("timeout") == es.SMTP_TIMEOUT_SECONDS > 0


def test_blocking_smtp_leaves_the_loop_responsive_and_wait_for_fires(svc):
    service, _ = svc
    _FakeSMTP.block_s = 0.5

    async def main():
        ticks = 0

        async def ticker():
            nonlocal ticks
            while True:
                await asyncio.sleep(0.01)
                ticks += 1

        t = asyncio.create_task(ticker())
        start = time.monotonic()
        with pytest.raises(asyncio.TimeoutError):
            await asyncio.wait_for(service.send_email("user@example.com", "s", "b"), 0.1)
        elapsed = time.monotonic() - start
        t.cancel()
        return ticks, elapsed

    ticks, elapsed = asyncio.run(main())
    assert elapsed < 0.4
    assert ticks >= 3
