"""`room` gets a first-class analytics bucket; it stays out of the alert set (ent#220).

Two cross-repo residuals from the ent#169 review, decided in opposite directions
because they answer different questions:

* **`_TRIGGER_BUCKETS`** — a room turn was unmapped, so every one landed in
  `Other`. That bucket means "a trigger nobody has classified yet"; letting the
  Workspace's whole traffic live there quietly redefines it as "rooms", and hides
  a distinct spend shape (one human message can bill N agent turns). Fixed —
  `Rooms`, the same treatment `loop` (#1150) and `reminder` (#1296) got.

* **`_AUTONOMOUS_TRIGGERS`** — deliberately NOT added, which is the part that
  needs a test, since an absence looks like an oversight. That set means "no
  human will see the reply, so an unresolved slash command earns an Operating
  Room alert". A room turn's reply lands in the room's own transcript, where the
  reader already is; and `room` is the one trigger an untrusted external
  Workspace client can drive, so adding it would turn a client's typo into an
  operator alert.
"""
from __future__ import annotations

import os
import tempfile
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")
os.environ.setdefault("AGENT_AUTH_SECRET", "0" * 64)
os.environ.setdefault("SECRET_KEY", "x" * 32)
os.environ.setdefault("INTERNAL_API_SECRET", "y" * 32)
os.environ.setdefault("TRINITY_DB_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent220.db"))
os.environ.setdefault("LOG_ARCHIVE_PATH", str(Path(tempfile.gettempdir()) / "trinity-ent220-logs"))

import pytest

from db.schedules.analytics import _BUCKET_ORDER, _TRIGGER_BUCKETS, _bucket_for_trigger

pytestmark = pytest.mark.unit


def test_a_room_turn_is_bucketed_as_rooms_not_other():
    assert _bucket_for_trigger("room") == "Rooms"
    assert _TRIGGER_BUCKETS["room"] == "Rooms"


def test_rooms_is_in_the_stack_order_before_the_catch_all():
    """A bucket missing from the order sorts after everything real (the map's
    own fallback), which would put Rooms past `Other` — the position reserved
    for "unclassified"."""
    assert "Rooms" in _BUCKET_ORDER
    assert _BUCKET_ORDER.index("Rooms") < _BUCKET_ORDER.index("Other")
    assert _BUCKET_ORDER[-1] == "Other"


def test_other_still_means_unclassified():
    """The bucket this change reclaims: `Other` must go back to meaning a
    trigger nobody has mapped yet."""
    assert _bucket_for_trigger("some_future_trigger") == "Other"
    assert _bucket_for_trigger(None) == "Other"
    assert _bucket_for_trigger("") == "Other"


def test_every_mapped_bucket_has_a_place_in_the_order():
    """The invariant that makes the chart and its legend agree — a bucket in the
    map but not the order renders in a different position everywhere it is
    drawn (the #1107 lesson `_fold_trigger_buckets` records)."""
    missing = sorted(set(_TRIGGER_BUCKETS.values()) - set(_BUCKET_ORDER))
    assert missing == [], f"buckets with no stack position: {missing}"


def test_room_is_not_an_autonomous_trigger():
    """Decided, not overlooked. A room turn's reply — and its failure line — is
    posted into the transcript the reader is already watching, so it is the
    `chat`/`public` case. `room` is also the one trigger an untrusted external
    Workspace client can drive: alerting on it would route a client's typo into
    the operator queue, one per unresolved command."""
    from services.task_execution_service import _AUTONOMOUS_TRIGGERS

    assert "room" not in _AUTONOMOUS_TRIGGERS
    # The interactive-ish set it belongs with, spelled out so a future edit that
    # adds `room` has to explain why it disagrees with these.
    for interactive in ("chat", "public", "manual", "mcp", "session"):
        assert interactive not in _AUTONOMOUS_TRIGGERS


def test_the_alerting_set_still_holds_the_unwatched_triggers():
    """The other half: this change must not quietly drop anything FROM the set."""
    from services.task_execution_service import _AUTONOMOUS_TRIGGERS

    for unwatched in ("schedule", "webhook", "loop", "event", "fan_out",
                      "agent", "reminder", "a2a"):
        assert unwatched in _AUTONOMOUS_TRIGGERS


def test_room_is_a_recognised_execution_filter():
    """The third place a trigger name has to be listed — the executions filter
    allow-list, where an unknown value degrades to "no filter" and silently
    returns everything."""
    from routers.executions import _VALID_TRIGGERS

    assert "room" in _VALID_TRIGGERS
