"""#4797: a duplicate event subscription is a 409 on SQLite and PostgreSQL.

The route used to look for SQLite's "UNIQUE constraint" wording, so PostgreSQL's
unique violation escaped as a 500.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy.exc import IntegrityError

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

# What each driver says for the same UNIQUE violation.
SQLITE_TEXT = (
    "UNIQUE constraint failed: agent_event_subscriptions.subscriber_agent, "
    "agent_event_subscriptions.source_agent, agent_event_subscriptions.event_type"
)
POSTGRES_TEXT = (
    "(psycopg2.errors.UniqueViolation) duplicate key value violates unique constraint "
    '"agent_event_subscriptions_subscriber_agent_source_agent_eve_key"'
)


def _data():
    from db_models import EventSubscriptionCreate

    return EventSubscriptionCreate(
        source_agent="agent-a", event_type="duplicate.test", target_message="First"
    )


def _create_through_route(ops):
    from routers.event_subscriptions import create_event_subscription

    mock_db = MagicMock()
    mock_db.create_event_subscription.side_effect = ops.create_subscription
    with patch("routers.event_subscriptions.db", mock_db):
        return asyncio.run(
            create_event_subscription("agent-a", _data(), SimpleNamespace(username="owner"))
        )


def test_4797_duplicate_is_409_on_a_real_database(db_backend):  # noqa: F811
    from fastapi import HTTPException

    from db.event_subscriptions import EventSubscriptionOperations

    ops = EventSubscriptionOperations()
    _create_through_route(ops)
    with pytest.raises(HTTPException) as info:
        _create_through_route(ops)
    assert info.value.status_code == 409
    assert len(ops.list_subscriptions(subscriber_agent="agent-a")) == 1


@pytest.mark.parametrize("driver_text", [SQLITE_TEXT, POSTGRES_TEXT], ids=["sqlite", "postgresql"])
def test_4797_duplicate_is_409_whatever_the_driver_calls_it(driver_text):
    from fastapi import HTTPException

    from db.event_subscriptions import EventSubscriptionOperations

    engine = MagicMock()
    engine.begin.return_value.__enter__.return_value.execute.side_effect = IntegrityError(
        "INSERT", {}, Exception(driver_text)
    )
    # The follow-up lookup finds the row that is already there.
    engine.connect.return_value.__enter__.return_value.execute.return_value.first.return_value = (
        "esub_existing",
    )
    with patch("db.event_subscriptions.get_engine", return_value=engine):
        with pytest.raises(HTTPException) as info:
            _create_through_route(EventSubscriptionOperations())
    assert info.value.status_code == 409


def test_4797_other_integrity_errors_are_not_reported_as_duplicates():
    from db.event_subscriptions import EventSubscriptionOperations

    engine = MagicMock()
    engine.begin.return_value.__enter__.return_value.execute.side_effect = IntegrityError(
        "INSERT", {}, Exception("some other constraint")
    )
    engine.connect.return_value.__enter__.return_value.execute.return_value.first.return_value = None
    with patch("db.event_subscriptions.get_engine", return_value=engine):
        with pytest.raises(IntegrityError):
            _create_through_route(EventSubscriptionOperations())
