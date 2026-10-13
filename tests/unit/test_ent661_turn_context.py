"""The turn-context seam (ent#661): an edition-agnostic registry through which a
module adds a line to a Workspace chat or room turn.

Pinned here:
- OSS build (no provider) → empty string, zero behaviour change.
- Providers are called in registration order, blank results are dropped, and
  each line ends in a blank line so it composes like the other turn prefixes.
- Fail open: a provider that raises is skipped, never fails the turn.
- The context a provider sees carries `internal_audience`, the platform's own
  verdict on whether anyone outside the organisation can read the reply.
"""
from __future__ import annotations

import pytest

pytestmark = pytest.mark.unit


@pytest.fixture(autouse=True)
def _clean():
    from services import turn_context
    turn_context.clear_providers()
    yield
    turn_context.clear_providers()


def _ctx(**kw):
    from services.turn_context import TurnContext
    base = dict(surface="thread", agent_name="a1", chat_id="c1",
                person_email="p@example.com", internal_audience=True)
    base.update(kw)
    return TurnContext(**base)


def test_no_provider_is_empty():
    from services import turn_context
    assert turn_context.collect(_ctx()) == ""


def test_providers_compose_in_order_and_skip_blank():
    from services import turn_context
    turn_context.register_provider(lambda c: "[A] one")
    turn_context.register_provider(lambda c: "")
    turn_context.register_provider(lambda c: None)
    turn_context.register_provider(lambda c: "[B] two\n")
    assert turn_context.collect(_ctx()) == "[A] one\n\n[B] two\n\n"


def test_raising_provider_is_skipped():
    from services import turn_context

    def boom(c):
        raise RuntimeError("db down")

    turn_context.register_provider(boom)
    turn_context.register_provider(lambda c: "[B] ok")
    assert turn_context.collect(_ctx()) == "[B] ok\n\n"


def test_register_is_idempotent_per_function():
    from services import turn_context

    def p(c):
        return "[A] x"

    turn_context.register_provider(p)
    turn_context.register_provider(p)
    assert turn_context.collect(_ctx()) == "[A] x\n\n"


def test_provider_sees_the_context():
    from services import turn_context
    seen = []
    turn_context.register_provider(lambda c: seen.append(c) or "")
    turn_context.collect(_ctx(surface="room", chat_id="room_1", internal_audience=False))
    assert seen[0].surface == "room"
    assert seen[0].chat_id == "room_1"
    assert seen[0].internal_audience is False


def test_unknown_surface_is_a_programming_error():
    with pytest.raises(ValueError):
        _ctx(surface="voice")
