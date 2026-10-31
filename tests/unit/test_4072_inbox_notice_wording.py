"""#4072 — the inbox-files notice must read as shared context, not a fresh upload.

The inbox is per person per agent, not per chat. Every file this person ever
shared with the agent is listed on every turn — in the 1:1 Workspace chat and in
every room — and the sentence used to say *"The client has uploaded these files
to your inbox … — read any that are relevant"*. An agent reads that literally: a
file shared three conversations ago was announced as this message's upload, and
the agent opened it to answer a question that had nothing to do with it.

The listing is right (AC #5: the agent must still be able to discover a file to
offer help). The CLAIM was wrong. A future rephrase of the sentence must keep
three claims, and these tests pin the claims rather than any one wording:

1. **Provenance is hedged** — the files are not necessarily from this message,
   and may come from earlier or other conversations (R1).
2. **Use is conditional** — open/look at one only when the latest request is
   about it (R2, and R4 for the attached-image branch).
3. **No fresh-upload claim** — never "has uploaded" / "just sent" / "read any
   that are relevant", in any paraphrase the regex catches (R3).

Plus one fact that lets claim 2 resolve a fresh drop ("summarise this"): each
document carries its age, rendered from the `uploaded_at` every listed item
already has (R8), by a helper whose clock is injected so no test here reads the
wall clock.

R5–R7 are regression guards: they were green before #4072 and pin what the
rewording must NOT change (the listing is always there, it names the directory,
files and sizes, and both image branches keep the #728 "do NOT … as text").
"""
import asyncio
import os
import re
import sys
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-4072.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-4072-logs"))

_REPO = Path(__file__).resolve().parents[2]
_BACKEND = _REPO / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

import pytest  # noqa: E402

pytestmark = pytest.mark.unit

EMAIL = "bob@example.com"
AGENT = "sidekick"
OLD = "2020-01-01T00:00:00Z"
DOCS = [
    {"filename": "q3.csv", "size_bytes": 2048, "uploaded_at": OLD},
    {"filename": "notes.txt", "size_bytes": 10, "uploaded_at": None},
]
IMG = [{"media_type": "image/png", "data": "AAA"}]

# A room passes its unread delta — a multi-line, sender-labelled transcript — as
# the "message"; the 1:1 chat passes the person's text. Both must read right.
ROOM_DELTA = "[bob]: morning all\n[analyst-demo]: hi Bob\n[bob]: @sidekick what's the plan for today?"

PROVENANCE = ("earlier", "other conversation", "previous", "over time")
HEDGE = ("not necessarily", "may", "possibly")
CONDITIONAL = re.compile(r"\b(if|when|only)\b")
FRESH_UPLOAD = re.compile(r"\b(has|have|just)\s+(just\s+)?(uploaded|sent|attached)\b")


@pytest.fixture
def portal():
    from client_portal import service as mod
    return mod


def _compose(monkeypatch, portal, images=(), image_names=(), doc_files=(), message="hello"):
    async def _fake(agent_name, email, msg):
        return list(images), list(image_names), [dict(d) for d in doc_files]
    monkeypatch.setattr(portal, "_collect_inbox_for_turn", _fake)
    prefix, _ = asyncio.run(portal.collect_inbox_context(AGENT, EMAIL, message))
    return prefix


def _branch(monkeypatch, portal, which):
    """The prefix for one branch, with the filenames removed so a token in a
    filename can never satisfy (or break) a claim about the sentence."""
    if which == "documents":
        p = _compose(monkeypatch, portal, doc_files=DOCS, message="what's the weather like?")
        names = [d["filename"] for d in DOCS]
    else:
        p = _compose(monkeypatch, portal, image_names=["shot.png"], message="what's the weather like?")
        names = ["shot.png"]
    assert p, "a non-empty inbox must compose a sentence"
    for n in names:
        p = p.replace(n, "")
    return p.lower()


# ---------------------------------------------------------------------------
# R1–R3: the three claims, on the two branches that list standing files
# ---------------------------------------------------------------------------

@pytest.mark.parametrize("which", ["documents", "offered-images"])
def test_r1_provenance_is_hedged(monkeypatch, portal, which):
    """The files are this person's over time — not necessarily this message's."""
    p = _branch(monkeypatch, portal, which)
    assert any(t in p for t in PROVENANCE), p
    assert any(t in p for t in HEDGE), p


@pytest.mark.parametrize("which", ["documents", "offered-images"])
def test_r2_use_is_conditional_on_the_request(monkeypatch, portal, which):
    """Not "read any that are relevant" — open one when the request is about it."""
    p = _branch(monkeypatch, portal, which)
    assert CONDITIONAL.search(p), p


@pytest.mark.parametrize("which", ["documents", "offered-images"])
def test_r3_no_fresh_upload_claim(monkeypatch, portal, which):
    p = _branch(monkeypatch, portal, which)
    assert not FRESH_UPLOAD.search(p), p
    assert "read any that are relevant" not in p


# ---------------------------------------------------------------------------
# R4: the attached-image branch — the claim that IS true stays, use is hedged
# ---------------------------------------------------------------------------

def test_r4_attached_images_are_still_shown_but_use_is_conditional(monkeypatch, portal):
    """An intent word ("attached", "chart") or an older unread room line attaches
    every inbox image, so "shown below" is true but "this is what they mean" is
    not. The pinned needles stay verbatim (test_2794's one-composer count and
    ent79's #728 phrase)."""
    p = _compose(monkeypatch, portal, images=IMG, image_names=["a.png"], message="the chart I attached")
    assert "shown to you directly below as images" in p
    assert "do NOT open/cat/read image files as text" in p
    assert CONDITIONAL.search(p.replace("a.png", "").lower()), p
    assert not FRESH_UPLOAD.search(p.lower()), p


# ---------------------------------------------------------------------------
# R5–R7: regression guards (green before #4072 — they pin what must not change)
# ---------------------------------------------------------------------------

def test_r5_documents_are_listed_whatever_the_message_says(monkeypatch, portal):
    """Regression guard (AC #5): the listing is not gated on the message, and the
    sentence is byte-identical for an empty turn, an unrelated question and a
    room delta."""
    prefixes = {
        _compose(monkeypatch, portal, doc_files=DOCS, message=m)
        for m in ("", "what's the weather like?", ROOM_DELTA)
    }
    assert len(prefixes) == 1
    assert next(iter(prefixes)).startswith("[Client Portal] ")


def test_r6_directory_filenames_and_sizes_are_kept(monkeypatch, portal):
    """Regression guard: the agent can still find and open each file."""
    p = _compose(monkeypatch, portal, doc_files=DOCS)
    assert f"`{portal._client_inbox(EMAIL)}/`" in p
    assert "q3.csv (2.0 KB" in p
    assert "notes.txt (10 B" in p
    assert p.endswith("\n\n")


@pytest.mark.parametrize("attached", [True, False])
def test_r7_both_image_branches_forbid_reading_an_image_as_text(monkeypatch, portal, attached):
    """Regression guard (#728): a binary through the stream-json pipe is the
    zombie-claude deadlock."""
    p = _compose(monkeypatch, portal, images=IMG if attached else (), image_names=["a.png"])
    assert "do NOT" in p and "as text" in p


def test_mixed_images_and_documents_render_both_sentences(monkeypatch, portal):
    """Regression guard: attached images + standing documents in one turn."""
    p = _compose(monkeypatch, portal, images=IMG, image_names=["a.png"], doc_files=DOCS)
    assert "shown to you directly below as images" in p
    assert "q3.csv" in p and "notes.txt" in p
    assert p.count("[Client Portal]") == 1


# ---------------------------------------------------------------------------
# R8: per-document age, from the existing `uploaded_at`, clock injected
# ---------------------------------------------------------------------------

NOW = datetime(2026, 10, 29, 12, 0, 0, tzinfo=timezone.utc)


def _iso(dt):
    return dt.isoformat().replace("+00:00", "Z")


@pytest.mark.parametrize("delta, expected", [
    (timedelta(seconds=5), "just now"),
    (timedelta(minutes=1), "1 minute ago"),
    (timedelta(minutes=42), "42 minutes ago"),
    (timedelta(hours=1), "1 hour ago"),
    (timedelta(hours=23, minutes=59), "23 hours ago"),
    (timedelta(days=1), "1 day ago"),
    (timedelta(days=3, hours=4), "3 days ago"),
    (timedelta(seconds=-30), "just now"),  # mtime a little ahead of the backend clock
])
def test_r8_age_phrase_with_a_fixed_clock(portal, delta, expected):
    assert portal._age_phrase(_iso(NOW - delta), now=NOW) == expected


@pytest.mark.parametrize("bad", [None, "", "not-a-date", 12345])
def test_r8_age_phrase_fails_open_to_empty(portal, bad):
    """A missing or unreadable mtime never costs the file its line."""
    assert portal._age_phrase(bad, now=NOW) == ""


def test_r8_age_phrase_accepts_offset_and_naive_iso(portal):
    assert portal._age_phrase("2026-10-29T10:00:00+00:00", now=NOW) == "2 hours ago"
    assert portal._age_phrase("2026-10-29T10:00:00", now=NOW) == "2 hours ago"  # naive = UTC


def test_r8_documents_carry_their_age_when_known(monkeypatch, portal):
    """The composed sentence renders the age — the 2020 timestamp is "days ago"
    on any clock — and a file without `uploaded_at` is listed without one."""
    p = _compose(monkeypatch, portal, doc_files=DOCS)
    assert re.search(r"q3\.csv \(2\.0 KB, shared \d+ days ago\)", p), p
    assert "notes.txt (10 B)" in p
