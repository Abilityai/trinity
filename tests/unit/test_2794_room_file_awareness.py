"""#2794 — an agent woken in a room is told about the client's files.

The reported session, in full: a client opens a room with `analyst-demo` and
`sidekick`, sends a screenshot, and writes *"@sidekick What is displayed on the
pasted image?"*. sidekick replies *"I don't see any image attached to your
message."* — and it is telling the truth.

Nothing was broken in delivery. The file reached an inbox, the rail listed it,
the transcript carried the question. What did not exist was the *telling*: a
room turn was built from `_build_turn_prompt`, which is a header plus the
transcript, and from nothing else. The sentence that makes a file visible to an
agent — and the vision blocks that make "what is in this picture" answerable at
all — were written inline in `portal_chat`, so the 1:1 conversation was the only
surface in the product that had them.

Two claims are pinned here, and neither is checkable by reading the diff:

1. **A room turn carries the manifest and the images.** Not "calls a function" —
   the composed message that reaches `execute_task` starts with the manifest, and
   `images=` is populated. A wiring that built the prefix and dropped it would
   pass any test that only asserted the collector was called.

2. **There is exactly ONE composer.** The failure mode being fixed is a second
   surface that quietly does not tell the agent anything. A third one is only a
   matter of time unless the sentence lives in one place, so its presence is
   counted across the whole backend rather than trusted to a comment.

Everything here fails CLOSED in the same direction the code does: a file that
cannot be mentioned never costs the client their turn.
"""
import asyncio
import os
import sys
import tempfile
from pathlib import Path
from types import SimpleNamespace

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-2794.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-2794-logs"))

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

EMAIL = "bob@example.com"
AGENT = "sidekick"
OTHER = "analyst-demo"
ROOM = "room-1"


@pytest.fixture
def rooms():
    from shared_sessions import service as mod
    return mod


@pytest.fixture
def portal():
    from client_portal import service as mod
    return mod


# ---------------------------------------------------------------------------
# 1. the composer — one sentence, every branch
# ---------------------------------------------------------------------------

def _collected(monkeypatch, portal, images=(), image_names=(), doc_files=()):
    async def _fake(agent_name, email, message):
        return list(images), list(image_names), list(doc_files)
    monkeypatch.setattr(portal, "_collect_inbox_for_turn", _fake)


def test_an_empty_inbox_composes_nothing(monkeypatch, portal):
    """No files, no sentence — and emphatically not an empty `[Client Portal] `
    banner in front of every turn the client ever sends."""
    _collected(monkeypatch, portal)
    prefix, images = asyncio.run(portal.collect_inbox_context(AGENT, EMAIL, "hello"))
    assert prefix == ""
    assert images == []


def test_an_attached_image_is_announced_as_shown(monkeypatch, portal):
    _collected(monkeypatch, portal,
               images=[{"media_type": "image/png", "data": "AAA"}],
               image_names=["shot.png"])
    prefix, images = asyncio.run(portal.collect_inbox_context(AGENT, EMAIL, "what is in the image?"))
    assert "shown to you directly below" in prefix
    assert "shot.png" in prefix
    assert images == [{"media_type": "image/png", "data": "AAA"}]


def test_an_unrequested_image_is_offered_rather_than_attached(monkeypatch, portal):
    """#78's "only when told": an image the turn does not reference is NAMED so
    the client can ask for it, not pushed into every unrelated turn."""
    _collected(monkeypatch, portal, images=[], image_names=["shot.png"])
    prefix, images = asyncio.run(portal.collect_inbox_context(AGENT, EMAIL, "unrelated"))
    assert "in your inbox" in prefix
    assert "shot.png" in prefix
    assert images == []


def test_every_branch_forbids_reading_an_image_as_text():
    """#728 — a binary through the stream-json pipe is the zombie-claude
    deadlock. The prohibition is not decoration on one branch; an agent told
    about an image it was NOT handed is the branch most likely to go and `cat`
    it."""
    from client_portal import service as portal

    async def run(images, names):
        async def _fake(*a, **k):
            return images, names, []
        import unittest.mock as m
        with m.patch.object(portal, "_collect_inbox_for_turn", _fake):
            return await portal.collect_inbox_context(AGENT, EMAIL, "image")

    attached, _ = asyncio.run(run([{"media_type": "image/png", "data": "A"}], ["a.png"]))
    offered, _ = asyncio.run(run([], ["a.png"]))
    assert "do NOT" in attached and "as text" in attached
    assert "do NOT" in offered and "as text" in offered


def test_documents_are_listed_with_the_directory_to_read_them_from(monkeypatch, portal):
    _collected(monkeypatch, portal,
               doc_files=[{"filename": "q3.csv", "size_bytes": 2048}])
    prefix, _ = asyncio.run(portal.collect_inbox_context(AGENT, EMAIL, "summarise it"))
    assert "q3.csv" in prefix
    assert portal._client_inbox(EMAIL) in prefix


# ---------------------------------------------------------------------------
# 2. the room turn actually carries it
# ---------------------------------------------------------------------------

class _WakeHarness:
    """`_wake_agent` driven to the point where `execute_task` has been called.

    `_build_turn_prompt` is deliberately NOT stubbed: the assertion is about the
    composed message, and a stub would hide whether the manifest reached it.
    """

    def __init__(self, monkeypatch, rooms, *, email=EMAIL, context=("", []), raises=False):
        self.kwargs = None
        self.posted = []

        monkeypatch.setattr(rooms.db, "get_participant",
                            lambda *a, **k: {"last_read_seq": 0, "cached_session_id": None})
        monkeypatch.setattr(rooms.db, "get_room",
                            lambda *a, **k: {"status": "open", "id": ROOM, "name": "Q3", "topic": None})
        monkeypatch.setattr(rooms.db, "get_messages", lambda *a, **k: self.delta)
        monkeypatch.setattr(rooms.db, "get_recent_messages", lambda *a, **k: self.delta)
        monkeypatch.setattr(rooms.db, "list_participants", lambda *a, **k: [])
        monkeypatch.setattr(rooms.db, "advance_read_cursor", lambda *a, **k: None)
        monkeypatch.setattr(rooms.db, "clear_cached_session", lambda *a, **k: None)
        monkeypatch.setattr(rooms, "_post_system", lambda *a, **k: None)
        monkeypatch.setattr(rooms, "_mark_agent_working", lambda *a, **k: None)
        monkeypatch.setattr(rooms, "_clear_agent_working", lambda *a, **k: None)
        monkeypatch.setattr(rooms, "_broadcast", lambda *a, **k: None)
        monkeypatch.setattr(rooms, "room_is_user_facing", lambda *a, **k: False)
        monkeypatch.setattr(rooms, "build_user_facing_room_prompt", lambda *a, **k: None)

        self.delta = [{"seq": 4, "content": "@sidekick what is in the image?",
                       "sender_kind": "user", "sender_identity": EMAIL}]
        self.email = email

        async def _post_message(*a, **k):
            self.posted.append(a)
        monkeypatch.setattr(rooms, "post_message", _post_message)

        self.asked = []

        async def _ctx(agent_name, email_, message):
            self.asked.append((agent_name, email_, message))
            if raises:
                raise RuntimeError("inbox unreadable")
            return context

        # Patched where the room REACHES it — the import inside
        # `_room_inbox_context` resolves against the portal module, so patching
        # the room module would be a no-op that still passed.
        from client_portal import service as portal
        monkeypatch.setattr(portal, "collect_inbox_context", _ctx)

        async def _execute_task(**kwargs):
            self.kwargs = kwargs
            return SimpleNamespace(status="success", response="ok",
                                   error="", execution_id="e1", session_id="s1")

        import services.task_execution_service as tes
        monkeypatch.setattr(tes, "get_task_execution_service",
                            lambda: SimpleNamespace(execute_task=_execute_task))

    def run(self, rooms):
        asyncio.run(rooms._wake_agent(SimpleNamespace(email=self.email), ROOM, AGENT, 1))
        return self.kwargs


def test_the_room_turn_leads_with_the_manifest_and_carries_the_images(monkeypatch, rooms):
    """The whole fix, in one assertion.

    Order matters and is asserted as order: the manifest is a *prefix*. An agent
    that meets "@sidekick what is in the image?" before it has been told an image
    exists is the agent that answers "I don't see any image attached".
    """
    h = _WakeHarness(monkeypatch, rooms,
                     context=("[Client Portal] image here\n\n",
                              [{"media_type": "image/png", "data": "AAA"}]))
    kw = h.run(rooms)
    assert kw["message"].startswith("[Client Portal] image here")
    assert "what is in the image?" in kw["message"]
    assert kw["images"] == [{"media_type": "image/png", "data": "AAA"}]


def test_an_empty_inbox_leaves_the_room_prompt_exactly_as_it_was(monkeypatch, rooms):
    """The no-files path must be a byte-for-byte no-op. Rooms without files are
    the overwhelming majority of rooms, and `images=None` (never `[]`) is what
    `execute_task` already expects for "no vision input"."""
    h = _WakeHarness(monkeypatch, rooms, context=("", []))
    kw = h.run(rooms)
    assert kw["message"].startswith("You are participating in the Trinity room")
    assert kw["images"] is None


def test_the_intent_test_sees_the_whole_delta_including_an_agents_relay(monkeypatch, rooms):
    """"@sidekick can you look at the screenshot the client sent?" is an ordinary
    room move. Scoping the image-intent test to human lines would make exactly
    that relay arrive image-less — this bug, one hop along."""
    h = _WakeHarness(monkeypatch, rooms, context=("", []))
    h.delta = [{"seq": 5, "content": "@sidekick look at the screenshot the client sent",
                "sender_kind": "agent", "sender_identity": OTHER}]
    h.run(rooms)
    assert h.asked, "the room never asked about the inbox at all"
    _, _, message = h.asked[0]
    assert "screenshot" in message


def test_a_turn_with_no_client_email_still_runs(monkeypatch, rooms):
    """An agent-only room has no client inbox to read. It must cost nothing —
    not a docker exec, not an exception, not a lost turn."""
    h = _WakeHarness(monkeypatch, rooms, email=None, context=("", []))
    kw = h.run(rooms)
    assert h.asked == []
    assert kw["images"] is None
    assert kw["message"].startswith("You are participating in the Trinity room")


def test_an_unreadable_inbox_never_costs_the_turn(monkeypatch, rooms):
    """Fail-safe, and in the direction that keeps the conversation working: the
    agent is simply not told about the files, and still answers."""
    h = _WakeHarness(monkeypatch, rooms, raises=True)
    kw = h.run(rooms)
    assert kw is not None, "the turn was never dispatched"
    assert kw["images"] is None
    assert "[Client Portal]" not in kw["message"]


# ---------------------------------------------------------------------------
# 3. one composer, counted
# ---------------------------------------------------------------------------

def test_the_manifest_sentence_exists_in_exactly_one_place():
    """The bug was a surface that composed nothing because the composition lived
    somewhere else. A second copy re-opens it silently — both surfaces work on
    the day it is written, and then one of them is edited.

    Counted across the backend rather than asserted about two known files, so a
    THIRD surface inventing its own sentence fails here too.
    """
    backend = _REPO / "src" / "backend"
    needle = "shown to you directly below as images"
    hits = [p for p in backend.rglob("*.py")
            if "__pycache__" not in p.parts and needle in p.read_text(errors="ignore")]
    assert [p.name for p in hits] == ["service.py"], hits
    assert hits[0].parent.name == "client_portal", hits


def test_the_room_reaches_that_composer_rather_than_its_own():
    """Source-level, because the behavioural tests above stub the collector and
    would pass against a room that had grown a private copy."""
    src = (_REPO / "src" / "backend" / "shared_sessions" / "service.py").read_text()
    assert "from client_portal.service import collect_inbox_context" in src
    assert "[Client Portal]" not in src, "the room is composing its own manifest"
