"""Unit tests for #903 — per-message speaker attribution on public_chat_messages.

Thread-scoping the Slack channel session key (see
``test_903_slack_session_thread.py``) lets a multi-participant thread share one
session. To keep attribution and per-user memory correct, #903 decomposes the
formerly-overloaded session key by adding two nullable columns to
``public_chat_messages``:

- ``sender_email`` — the verified speaker; the MEM-001 summarizer filters on it so
  a shared thread never feeds one user's turns into another user's memory (F-MEM).
- ``sender_label`` — display label; ``build_context_prompt`` replays each turn
  attributed (``Alice:`` / ``Bob:``) instead of a flat ``User:`` (F-ATTRIB).

Also covers the F-RACE guard on ``get_or_create_session`` (thread-scoping newly
lets two *different* users race the select-then-insert on a brand-new thread key).

Runs in-process against an ephemeral DB (db_harness, #300): SQLite always, and
PostgreSQL when TEST_POSTGRES_URL is set. Exercises ``PublicChatOperations``
directly (SQLAlchemy-Core, resolves ``get_engine()`` fresh per call), mirroring
``test_public_chat_context.py``.
"""
from __future__ import annotations

import secrets
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

# ---------------------------------------------------------------------------
# Bootstrap: make src/backend importable without shadowing tests/utils.
# ---------------------------------------------------------------------------
_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)

# Modules whose sys.modules identity this file swaps so production code
# re-resolves against the db_harness engine. Snapshotted + restored by the
# autouse `_restore_sys_modules` fixture below (the sanctioned escape hatch for
# tests/lint_sys_modules.py — a self-contained snapshot/restore, no leakage
# into sibling tests; guards the test_904 module-identity footgun).
_STUBBED_MODULE_NAMES = [
    "utils",
    "utils.api_client",
    "utils.assertions",
    "utils.cleanup",
    "db.public_chat",
]

for _shadow in ("utils", "utils.api_client", "utils.assertions", "utils.cleanup"):
    sys.modules.pop(_shadow, None)
while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _restore_sys_modules():
    """Snapshot the modules this file swaps and restore them afterwards so a
    rebound `db.public_chat` never leaks into a sibling test."""
    saved = {name: sys.modules.get(name) for name in _STUBBED_MODULE_NAMES}
    try:
        yield
    finally:
        for name, mod in saved.items():
            if mod is None:
                sys.modules.pop(name, None)
            else:
                sys.modules[name] = mod


@pytest.fixture()
def ops(db_backend):
    """PublicChatOperations on the active backend (db_harness, #300)."""
    sys.modules.pop("db.public_chat", None)
    from db.public_chat import PublicChatOperations
    return PublicChatOperations()


def _session(ops, link_id: str = "link-1") -> str:
    return ops.get_or_create_session(link_id, secrets.token_urlsafe(8), "anonymous").id


# ---------------------------------------------------------------------------
# Schema accessor (learnings.md guard) — the real footgun is tables.py drift.
# ---------------------------------------------------------------------------

def test_sender_columns_are_queryable(ops, db_backend):
    """`select(public_chat_messages.c.sender_email)` must run against a migrated
    DB — catches a tables.py MetaData omission that schema-parity wouldn't."""
    from sqlalchemy import select
    from db.engine import get_engine
    from db.tables import public_chat_messages

    with get_engine().connect() as conn:
        conn.execute(
            select(
                public_chat_messages.c.sender_email,
                public_chat_messages.c.sender_label,
            ).limit(1)
        ).all()  # no rows needed — the point is the columns resolve


# ---------------------------------------------------------------------------
# Persist + round-trip
# ---------------------------------------------------------------------------

def test_add_message_persists_sender_fields(ops):
    sid = _session(ops)
    ops.add_message(
        sid, "user", "hi", sender_email="alice@example.com",
        sender_label="Alice (@alice)",
    )
    msgs = ops.get_session_messages(sid)
    assert len(msgs) == 1
    assert msgs[0].sender_email == "alice@example.com"
    assert msgs[0].sender_label == "Alice (@alice)"


def test_add_message_defaults_sender_fields_null(ops):
    """Web/DM/assistant turns leave both null — backward compatible."""
    sid = _session(ops)
    ops.add_message(sid, "assistant", "hello")
    msg = ops.get_session_messages(sid)[0]
    assert msg.sender_email is None
    assert msg.sender_label is None


# ---------------------------------------------------------------------------
# F-ATTRIB — build_context_prompt attributes history by sender_label
# ---------------------------------------------------------------------------

def test_build_context_prompt_attributes_by_sender_label(ops):
    sid = _session(ops)
    ops.add_message(sid, "user", "Q3 numbers?", sender_email="alice@x.com", sender_label="Alice")
    ops.add_message(sid, "assistant", "Here they are", sender_label="finance-bot")
    ops.add_message(sid, "user", "and Tokyo?", sender_email="bob@x.com", sender_label="Bob")

    ctx = ops.build_context_prompt(sid, "new question", max_turns=10)

    assert "Alice: Q3 numbers?" in ctx
    assert "Bob: and Tokyo?" in ctx
    assert "finance-bot: Here they are" in ctx


def test_build_context_prompt_falls_back_to_role_when_null(ops):
    """Null sender_label (web/DM turns, pre-migration rows) → role label."""
    sid = _session(ops)
    ops.add_message(sid, "user", "hello there")
    ops.add_message(sid, "assistant", "hi back")

    ctx = ops.build_context_prompt(sid, "next", max_turns=10)

    assert "User: hello there" in ctx
    assert "Assistant: hi back" in ctx


# ---------------------------------------------------------------------------
# F-MEM — sender-filtered recent messages feed only the current user's memory
# ---------------------------------------------------------------------------

def test_get_recent_messages_filters_by_sender_email(ops):
    """A shared thread with Alice + Bob turns: filtering to Bob returns only
    Bob's turns — Alice's content never reaches Bob's memory summarizer."""
    sid = _session(ops)
    ops.add_message(sid, "user", "Alice private detail", sender_email="alice@x.com", sender_label="Alice")
    ops.add_message(sid, "assistant", "ack alice", sender_label="bot")
    ops.add_message(sid, "user", "Bob private detail", sender_email="bob@x.com", sender_label="Bob")
    ops.add_message(sid, "assistant", "ack bob", sender_label="bot")

    bob_only = ops.get_recent_messages(sid, limit=20, sender_email="bob@x.com")

    contents = [m.content for m in bob_only]
    assert "Bob private detail" in contents
    assert "Alice private detail" not in contents
    # Assistant turns carry no sender_email → excluded from the per-user feed.
    assert all(m.sender_email == "bob@x.com" for m in bob_only)


def test_sender_filter_includes_assistant_stamped_with_that_email(ops):
    """Single-participant parity (#903): web/DM stamp the assistant reply with the
    recipient's email, so the sender-filtered summarizer feed keeps assistant
    context (the pre-#903 behavior) instead of dropping it as null."""
    sid = _session(ops)
    ops.add_message(sid, "user", "my question", sender_email="bob@x.com", sender_label="Bob")
    ops.add_message(sid, "assistant", "my answer", sender_email="bob@x.com", sender_label="bot")

    bob_feed = ops.get_recent_messages(sid, limit=20, sender_email="bob@x.com")

    contents = [m.content for m in bob_feed]
    assert contents == ["my question", "my answer"], (
        "a single-participant assistant turn stamped with the user's email must "
        "stay in that user's filtered memory feed"
    )


def test_get_recent_messages_unfiltered_returns_all(ops):
    """No sender_email → unchanged behavior (all turns), so history replay and
    web/DM paths are unaffected."""
    sid = _session(ops)
    ops.add_message(sid, "user", "one", sender_email="a@x.com", sender_label="A")
    ops.add_message(sid, "assistant", "two", sender_label="bot")

    allm = ops.get_recent_messages(sid, limit=20)
    assert {m.content for m in allm} == {"one", "two"}


# ---------------------------------------------------------------------------
# F-RACE — concurrent get_or_create_session on one new key → one row, no 500
# ---------------------------------------------------------------------------

def test_concurrent_get_or_create_session_single_row(ops):
    """Two users racing a brand-new thread key: both get the same session, exactly
    one row is created, and neither sees an IntegrityError surface."""
    link_id = "link-race"
    key = "T1:Cchan:thread-race"
    barrier = threading.Barrier(2)
    errors: list[Exception] = []

    def worker():
        try:
            barrier.wait(timeout=10)
            return ops.get_or_create_session(link_id, key, "anonymous").id
        except Exception as e:  # noqa: BLE001 — captured for assertion
            errors.append(e)
            raise

    with ThreadPoolExecutor(max_workers=2) as pool:
        ids = [f.result() for f in [pool.submit(worker), pool.submit(worker)]]

    assert not errors, f"race raised: {errors}"
    assert ids[0] == ids[1], "both callers must converge on one session"

    # Exactly one row for the key.
    from sqlalchemy import select, func
    from db.engine import get_engine
    from db.tables import public_chat_sessions

    with get_engine().connect() as conn:
        count = conn.execute(
            select(func.count())
            .select_from(public_chat_sessions)
            .where(public_chat_sessions.c.session_identifier == key)
        ).scalar()
    assert count == 1


def test_get_or_create_session_is_idempotent(ops):
    """Sequential double-call on one key → same session, one row (the winner
    path the race guard also converges to)."""
    a = ops.get_or_create_session("link-2", "T1:Cx:thread-1", "anonymous")
    b = ops.get_or_create_session("link-2", "T1:Cx:thread-1", "anonymous")
    assert a.id == b.id
