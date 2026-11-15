"""
Unit tests for WebSocket ticket auth (#550).

Covers ``services.ws_ticket_service``:

- ``mint_ticket`` writes a ticket-keyed entry to Redis with the
  configured TTL and returns the opaque token.
- ``consume_ticket`` returns the principal exactly once (single-use
  via ``GETDEL``).
- Missing / malformed / expired tickets return ``None``.
- Redis-down behavior fails closed (mint raises, consume returns
  ``None``).
- trinity-enterprise#595: every consumer names the scope it accepts
  (``consume_ticket(ticket, *, scope=...)``); a ticket minted for another
  surface is consumed AND refused, a payload that is not an object with a
  non-empty string ``sub`` is refused, supplemental claims round-trip without
  replacing ``sub``/``scope``, and a store failure is a typed, secret-free
  ``TicketStoreUnavailable``.
"""

from __future__ import annotations

import importlib.util
import inspect
import json
import logging
import sys
import types
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"


class _FakeRedis:
    """In-memory stand-in for the live Redis client.

    Implements the subset our service actually calls: ``setex``,
    ``getdel``. TTLs are tracked but treated as a one-shot expire flag
    (the ``expire_now`` helper).
    """

    def __init__(self) -> None:
        self.store: dict[str, str] = {}
        self.ttl: dict[str, int] = {}
        # trinity-enterprise#595: a store that raises, carrying a sentinel
        # that must never reach a log line.
        self.raise_on: set[str] = set()

    def setex(self, key: str, ttl: int, value: str) -> None:
        if "setex" in self.raise_on:
            raise ConnectionError(f"redis://u:SENTINEL-PASSWORD@host {key}")
        self.store[key] = value
        self.ttl[key] = ttl

    def getdel(self, key: str):
        if "getdel" in self.raise_on:
            raise ConnectionError(f"redis://u:SENTINEL-PASSWORD@host {key}")
        return self.store.pop(key, None)

    def expire_now(self, key: str) -> None:
        self.store.pop(key, None)
        self.ttl.pop(key, None)


@pytest.fixture
def ticket_service(monkeypatch):
    """Load ``services.ws_ticket_service`` with a stub ``routers.auth``.

    Loading the module via the regular package path triggers FastAPI
    + jose imports we don't want in a unit test. We stub the ``routers``
    package and the only function the service needs from it
    (``get_redis_client``).
    """
    fake = _FakeRedis()
    routers_pkg = types.ModuleType("routers")
    routers_pkg.__path__ = []
    auth_stub = types.ModuleType("routers.auth")
    auth_stub.get_redis_client = lambda: fake
    monkeypatch.setitem(sys.modules, "routers", routers_pkg)
    monkeypatch.setitem(sys.modules, "routers.auth", auth_stub)

    spec = importlib.util.spec_from_file_location(
        "ws_ticket_service_under_test",
        str(_BACKEND / "services" / "ws_ticket_service.py"),
    )
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module, fake, auth_stub


def test_mint_writes_ticket_with_ttl(ticket_service):
    module, fake, _ = ticket_service
    ticket = module.mint_ticket("alice", scope="user")

    assert isinstance(ticket, str) and len(ticket) >= 32
    key = f"ws_ticket:{ticket}"
    assert fake.store[key]
    assert fake.ttl[key] == 30
    payload = json.loads(fake.store[key])
    assert payload == {"sub": "alice", "scope": "user"}


def test_consume_returns_principal_then_invalidates(ticket_service):
    module, _fake, _ = ticket_service
    ticket = module.mint_ticket("bob")

    first = module.consume_ticket(ticket, scope="user")
    second = module.consume_ticket(ticket, scope="user")

    assert first == {"sub": "bob", "scope": "user"}
    assert second is None  # single-use: second exchange fails


def test_consume_unknown_ticket_returns_none(ticket_service):
    module, _fake, _ = ticket_service
    assert module.consume_ticket("never-issued", scope="user") is None
    assert module.consume_ticket("", scope="user") is None
    assert module.consume_ticket(None, scope="user") is None


def test_consume_expired_ticket_returns_none(ticket_service):
    module, fake, _ = ticket_service
    ticket = module.mint_ticket("carol")
    fake.expire_now(f"ws_ticket:{ticket}")

    assert module.consume_ticket(ticket, scope="user") is None


def test_mint_raises_when_redis_unavailable(ticket_service, monkeypatch):
    # The service binds ``get_redis_client`` at import time via
    # ``from routers.auth import get_redis_client``, so we patch the
    # name as it lives in the service module — not on the auth stub.
    module, _fake, _auth_stub = ticket_service
    monkeypatch.setattr(module, "get_redis_client", lambda: None)

    with pytest.raises(RuntimeError, match="Redis unavailable"):
        module.mint_ticket("alice")


def test_consume_returns_none_when_redis_unavailable(ticket_service, monkeypatch):
    module, _fake, _auth_stub = ticket_service
    monkeypatch.setattr(module, "get_redis_client", lambda: None)

    assert module.consume_ticket("anything", scope="user") is None


def test_consume_handles_malformed_payload(ticket_service):
    module, fake, _ = ticket_service
    fake.store["ws_ticket:badjson"] = "not-json"

    assert module.consume_ticket("badjson", scope="user") is None
    # Even malformed, the GETDEL still removed the entry — defensive.
    assert "ws_ticket:badjson" not in fake.store


# ---------------------------------------------------------------------------
# trinity-enterprise#595 — the consumer names the surface it accepts
# ---------------------------------------------------------------------------

_SCOPES = ("user", "voice:vs_1", "voip:call_1")


def test_consume_requires_a_keyword_only_scope_with_no_default(ticket_service):
    """The contract, asserted BEFORE any call: a consumer cannot forget the
    scope, because there is no default to fall back on."""
    module, _fake, _ = ticket_service
    params = inspect.signature(module.consume_ticket).parameters
    assert "scope" in params, "consume_ticket takes no expected scope"
    assert params["scope"].kind is inspect.Parameter.KEYWORD_ONLY
    assert params["scope"].default is inspect.Parameter.empty
    assert params["raise_on_unavailable"].default is False


@pytest.mark.parametrize("minted", _SCOPES)
@pytest.mark.parametrize("presented", _SCOPES)
def test_a_ticket_opens_only_the_surface_it_was_minted_for(ticket_service, minted, presented):
    module, fake, _ = ticket_service
    ticket = module.mint_ticket("alice", scope=minted)
    got = module.consume_ticket(ticket, scope=presented)
    if minted == presented:
        assert got == {"sub": "alice", "scope": minted}
    else:
        assert got is None
    # Either way the ticket is spent: a wrong-surface attempt burns it, so it
    # cannot be retried against the right surface afterwards.
    assert f"ws_ticket:{ticket}" not in fake.store
    assert module.consume_ticket(ticket, scope=minted) is None


@pytest.mark.parametrize("raw", [
    "[1, 2]", '"alice"', "5", "null", "true",
    '{"scope": "user"}',                       # no subject
    '{"sub": "", "scope": "user"}',            # empty subject
    '{"sub": 7, "scope": "user"}',             # non-string subject
    '{"sub": ["a"], "scope": "user"}',
    '{"sub": "alice"}',                        # no scope at all
    '{"sub": "alice", "scope": null}',
])
def test_a_payload_that_is_not_a_subject_object_is_refused(ticket_service, raw):
    module, fake, _ = ticket_service
    fake.store["ws_ticket:t"] = raw
    assert module.consume_ticket("t", scope="user") is None
    assert "ws_ticket:t" not in fake.store


def test_supplemental_claims_round_trip_and_existing_payloads_stay_exact(ticket_service):
    module, fake, _ = ticket_service
    ticket = module.mint_ticket("alice", scope="voice:vs_9",
                                claims={"user_id": 42, "origin": "workspace"})
    assert json.loads(fake.store[f"ws_ticket:{ticket}"]) == {
        "sub": "alice", "scope": "voice:vs_9", "user_id": 42, "origin": "workspace"}
    assert module.consume_ticket(ticket, scope="voice:vs_9") == {
        "sub": "alice", "scope": "voice:vs_9", "user_id": 42, "origin": "workspace"}
    # The /ws and VoIP payloads carry nothing new.
    user = module.mint_ticket("bob", scope="user")
    voip = module.mint_ticket("3", scope="voip:c1", ttl_seconds=90)
    assert json.loads(fake.store[f"ws_ticket:{user}"]) == {"sub": "bob", "scope": "user"}
    assert json.loads(fake.store[f"ws_ticket:{voip}"]) == {"sub": "3", "scope": "voip:c1"}
    assert fake.ttl[f"ws_ticket:{voip}"] == 90


@pytest.mark.parametrize("claim", ["sub", "scope"])
def test_a_claim_cannot_replace_the_subject_or_the_scope(ticket_service, claim):
    module, fake, _ = ticket_service
    with pytest.raises(ValueError):
        module.mint_ticket("alice", scope="voice:vs_1", claims={claim: "mallory"})
    assert fake.store == {}


def test_a_store_that_cannot_be_written_is_a_typed_unavailable(ticket_service, caplog):
    module, fake, _ = ticket_service
    fake.raise_on.add("setex")
    caplog.set_level(logging.DEBUG)
    with pytest.raises(module.TicketStoreUnavailable) as exc:
        module.mint_ticket("alice", scope="voice:vs_1")
    assert isinstance(exc.value, RuntimeError)      # existing callers still catch it
    assert "SENTINEL" not in str(exc.value)
    assert "SENTINEL" not in caplog.text
    assert "ws_ticket:" not in caplog.text


def test_a_store_that_cannot_be_read_is_none_by_default_and_typed_on_request(ticket_service, caplog):
    module, fake, _ = ticket_service
    ticket = module.mint_ticket("alice", scope="voice:vs_1")
    fake.raise_on.add("getdel")
    caplog.set_level(logging.DEBUG)
    assert module.consume_ticket(ticket, scope="voice:vs_1") is None
    with pytest.raises(module.TicketStoreUnavailable):
        module.consume_ticket(ticket, scope="voice:vs_1", raise_on_unavailable=True)
    assert "SENTINEL" not in caplog.text
    assert ticket not in caplog.text


def test_no_store_at_all_is_typed_on_request(ticket_service, monkeypatch):
    module, _fake, _ = ticket_service
    monkeypatch.setattr(module, "get_redis_client", lambda: None)
    assert module.consume_ticket("x" * 43, scope="user") is None
    with pytest.raises(module.TicketStoreUnavailable):
        module.consume_ticket("x" * 43, scope="user", raise_on_unavailable=True)
    with pytest.raises(module.TicketStoreUnavailable):
        module.mint_ticket("alice")
