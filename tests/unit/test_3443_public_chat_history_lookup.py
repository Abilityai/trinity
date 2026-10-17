"""Unit tests for #3443 — an anonymous public-chat session finds its own row.

``PublicChatOperations.get_or_create_session`` lowercases the identifier only
for ``identifier_type == 'email'``; an anonymous identifier is a
``secrets.token_urlsafe(16)`` token stored with its case intact. The lookup the
history route (``GET /api/public/history/{token}``) and the session-clear route
(``DELETE /api/public/session/{token}``) both go through,
``get_session_by_identifier``, lowercased its argument unconditionally — so a
mixed-case anonymous token never matched the row it had just written, history
came back empty after every reload, and "New conversation" cleared nothing.

Driven at the layer the defect lives in: the create path writes the row, the
lookup reads it back, against a real ephemeral DB (db_harness, #300 — SQLite
always, PostgreSQL when TEST_POSTGRES_URL is set). Setup mirrors
``test_903_public_chat_sender.py``.
"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parent.parent.parent / "src" / "backend"
_BACKEND_STR = str(_BACKEND)

# Modules whose sys.modules identity this file swaps so production code
# re-resolves against the db_harness engine; snapshotted + restored by the
# autouse fixture below (the sanctioned escape hatch for
# tests/lint_sys_modules.py).
_STUBBED_MODULE_NAMES = [
    "db.public_chat",
]

while _BACKEND_STR in sys.path:
    sys.path.remove(_BACKEND_STR)
sys.path.insert(0, _BACKEND_STR)

from db_harness import db_backend  # noqa: E402,F401

pytestmark = pytest.mark.unit

LINK = "link-3443"
# The shape `secrets.token_urlsafe(16)` produces: mixed case, digits, `-`/`_`.
ANON_TOKEN = "Zk3-QwErTy_9aBcDeFgHiJ"


@pytest.fixture(autouse=True)
def _restore_sys_modules():
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


# ---------------------------------------------------------------------------
# The reported defect
# ---------------------------------------------------------------------------

def test_mixed_case_anonymous_token_finds_its_own_session(ops):
    """The reload path: a turn is stored under the token, then the history
    route looks the session up by the same token, case intact."""
    created = ops.get_or_create_session(LINK, ANON_TOKEN, "anonymous")
    ops.add_message(created.id, "user", "hello")
    ops.add_message(created.id, "assistant", "hi there")

    found = ops.get_session_by_identifier(LINK, ANON_TOKEN)

    assert found is not None, "an anonymous token must find the row it created"
    assert found.id == created.id
    assert found.identifier_type == "anonymous"
    assert [m.content for m in ops.get_recent_messages(found.id, limit=100)] == [
        "hello",
        "hi there",
    ]


def test_anonymous_tokens_differing_only_in_case_stay_separate(ops):
    """Two visitors whose tokens differ only in case own two sessions. The
    lowercasing lookup handed the mixed-case visitor the OTHER visitor's
    conversation."""
    mixed = ops.get_or_create_session(LINK, "AbCdEf123", "anonymous")
    lower = ops.get_or_create_session(LINK, "abcdef123", "anonymous")
    assert mixed.id != lower.id

    assert ops.get_session_by_identifier(LINK, "AbCdEf123").id == mixed.id
    assert ops.get_session_by_identifier(LINK, "abcdef123").id == lower.id


def test_anonymous_lookup_is_not_case_insensitive(ops):
    """A token is a bearer secret: only the exact string resolves it."""
    ops.get_or_create_session(LINK, "abcdef123", "anonymous")

    assert ops.get_session_by_identifier(LINK, "ABCDEF123") is None
    assert ops.get_session_by_identifier(LINK, "AbCdEf123") is None


def test_anonymous_lookup_is_scoped_to_the_link(ops):
    ops.get_or_create_session(LINK, ANON_TOKEN, "anonymous")

    assert ops.get_session_by_identifier("another-link", ANON_TOKEN) is None


# ---------------------------------------------------------------------------
# Email identifiers stay case-insensitive
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "lookup",
    ["visitor@example.com", "Visitor@Example.com", "VISITOR@EXAMPLE.COM"],
)
def test_email_lookup_stays_case_insensitive(ops, lookup):
    created = ops.get_or_create_session(LINK, "Visitor@Example.COM", "email")
    assert created.session_identifier == "visitor@example.com"

    found = ops.get_session_by_identifier(LINK, lookup)

    assert found is not None
    assert found.id == created.id
    assert found.identifier_type == "email"
