"""`operator_queue.addressed_to_email` and its roster validation (ent#364).

The OSS half of workspace asks: an edition-agnostic primitive (the column and the
rule that fills it), with the read/answer surface gated separately — the same split
as `users.suspended_at` in #995, where OSS owns the column AND its enforcement.

This field is the one piece of an agent-authored queue item that is an
AUTHORIZATION decision: it decides who may answer the ask and whose Workspace
sidebar it appears in. Which is exactly why it is not a key inside `context` —
`context` is free-form agent JSON whose hygiene clamp bounds size and type only, so
an addressee living there would be an agent choosing its own audience.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture()
def roster(monkeypatch):
    state = {"members": {("scout", "client@example.com")}, "calls": [], "raises": False}

    import client_portal.service as portal_service

    def _on_roster(agent_name, email, include_owned=False):
        state["calls"].append((agent_name, email, include_owned))
        if state["raises"]:
            raise RuntimeError("roster down")
        return (agent_name, (email or "").lower()) in state["members"]

    monkeypatch.setattr(portal_service, "agent_on_roster", _on_roster)
    return state


def _clamp(raw_email, agent="scout"):
    from services.operator_queue_service import _clamp_ingested_item

    out = _clamp_ingested_item(
        {"id": "req-1", "title": "t", "question": "q", "addressed_to_email": raw_email},
        agent,
    )
    return out["addressed_to_email"]


def test_an_on_roster_address_is_kept_and_normalised(roster):
    assert _clamp("Client@Example.com  ") == "client@example.com"


def test_an_off_roster_address_becomes_an_operator_ask(roster):
    """The whole point: an agent may not decide who answers for it. Dropped to
    NULL — which is a perfectly valid item, just one the operator answers."""
    assert _clamp("stranger@example.com") is None


def test_an_address_for_a_different_agent_does_not_carry_over(roster):
    """Roster membership is per agent. `sage` cannot address `scout`'s client."""
    assert _clamp("client@example.com", agent="sage") is None


@pytest.mark.parametrize("bad", [None, 123, {"email": "x@y.z"}, [], "", "   ", "not-an-email"])
def test_malformed_values_are_dropped_without_a_roster_call(roster, bad):
    """Refused before the DB read: a non-string or address-shaped-nothing can never
    match a roster row, and a lookup per garbage value is a free amplification."""
    assert _clamp(bad) is None
    assert roster["calls"] == []


def test_an_absurdly_long_value_is_refused_before_the_lookup(roster):
    from services.operator_queue_service import OPERATOR_QUEUE_EMAIL_MAX

    assert _clamp("a" * (OPERATOR_QUEUE_EMAIL_MAX + 1) + "@example.com") is None
    assert roster["calls"] == []


def test_a_roster_failure_fails_closed(roster):
    """Addressing an ask we could not validate is worse than not addressing it."""
    roster["raises"] = True
    assert _clamp("client@example.com") is None


def test_the_lookup_never_unlocks_the_owned_agents_branch(roster):
    """`include_owned=True` would hand a client agents nobody shared with them —
    see `agent_on_roster`'s own docstring. It is the rule here, not a default."""
    _clamp("client@example.com")
    assert roster["calls"] == [("scout", "client@example.com", False)]


def test_the_addressee_is_never_read_from_context(roster):
    """A `context.addressed_to_email` must not become the addressee: `context` is
    agent-authored and only size/type-clamped, so honouring it would reintroduce
    exactly the hole the column exists to close."""
    from services.operator_queue_service import _clamp_ingested_item

    out = _clamp_ingested_item(
        {
            "id": "req-2", "title": "t", "question": "q",
            "context": {"addressed_to_email": "client@example.com"},
        },
        "scout",
    )
    assert out["addressed_to_email"] is None


# --- persistence ------------------------------------------------------------

@pytest.fixture()
def queue_db(tmp_path, monkeypatch):
    import sqlite3

    db_file = tmp_path / "trinity-asks-oss.db"
    monkeypatch.setenv("TRINITY_DB_PATH", str(db_file))

    import db.connection as conn_mod
    monkeypatch.setattr(conn_mod, "DB_PATH", str(db_file))

    from db.schema import init_schema
    raw = sqlite3.connect(db_file)
    init_schema(raw.cursor(), raw)
    raw.commit()
    raw.close()
    yield str(db_file)


def test_the_column_round_trips(queue_db, roster):
    from database import db

    item_id = db.create_operator_queue_item("scout", {
        "id": "req-round-trip", "title": "t", "question": "q",
        "addressed_to_email": "client@example.com",
    })

    assert db.get_operator_queue_item(item_id)["addressed_to_email"] == "client@example.com"


def test_an_unaddressed_item_reads_back_as_none(queue_db, roster):
    """Every pre-ent#364 row is this row: NULL means the operator answers."""
    from database import db

    item_id = db.create_operator_queue_item("scout", {
        "id": "req-operator", "title": "t", "question": "q",
    })

    assert db.get_operator_queue_item(item_id)["addressed_to_email"] is None
