"""#4050 — two readings that mint the same request id in one instant are two
rows, and `observe` never reports `created` for a row it did not write.

`observe` mints `<kind prefix><key>-<utc_now_iso()>`. `git_bloat_size` /
`git_bloat_maintenance` and `db_backup_failure` / `db_backup_stale` share a
prefix and, at their emitters, a key; an event kind repeats its id outright.
At one instant the ids are equal, `create_platform_item`'s
`ON CONFLICT (agent_name, request_id) DO NOTHING` wrote nothing, and the
re-select returned the first reading's row as `created`. Driven at the seam
against the unit island's real SQLite, with the clock frozen.
"""
import uuid

import pytest

pytestmark = pytest.mark.unit

_FROZEN = "2026-10-29T03:30:00.000000Z"


@pytest.fixture
def db():
    from database import db
    return db


@pytest.fixture
def pa(monkeypatch):
    from services import platform_alerts, operator_resume_service
    from utils import helpers
    monkeypatch.setattr(operator_resume_service, "spawn_on_loop", lambda factory: None)
    monkeypatch.setattr(helpers, "utc_now_iso", lambda: _FROZEN)
    return platform_alerts


@pytest.fixture
def agent():
    return f"agent-4050-{uuid.uuid4().hex[:6]}"


def _pending(db, agent):
    return db.list_operator_queue_items(agent_name=agent, status="pending")


@pytest.mark.parametrize("first,second,key", [
    ("git_bloat_size", "git_bloat_maintenance", "the-agent"),
    ("db_backup_failure", "db_backup_stale", "/data/backups"),
])
def test_a_shared_prefix_pair_at_one_instant_is_two_rows(db, pa, agent, first, second, key):
    assert pa.KINDS[first].prefix == pa.KINDS[second].prefix, "control: the pair shares a prefix"

    outcomes = [pa.observe(agent, kind, key, title=kind, question="q") for kind in (first, second)]

    assert outcomes == [pa.OBSERVED_CREATED, pa.OBSERVED_CREATED]
    rows = _pending(db, agent)
    assert sorted(r["subject"] for r in rows) == sorted(
        [pa.subject_for(first, key), pa.subject_for(second, key)])
    assert len({r["request_id"] for r in rows}) == 2
    by_subject = {r["subject"]: r for r in rows}
    assert by_subject[pa.subject_for(second, key)]["title"] == second, (
        "the second reading's row carries the first reading's fields"
    )


def test_one_event_twice_at_one_instant_is_two_rows(db, pa, agent):
    kind = "gitignore_untracked"
    assert pa.KINDS[kind].event, "control: an event kind, always a new row"

    first = pa.observe(agent, kind, title="first", question="q")
    second = pa.observe(agent, kind, title="second", question="q")

    assert (first, second) == (pa.OBSERVED_CREATED, pa.OBSERVED_CREATED)
    assert sorted(r["title"] for r in _pending(db, agent)) == ["first", "second"]


def test_create_platform_item_returns_the_row_it_inserted(db, agent):
    item = {"id": f"collide-{agent}", "type": "alert", "status": "pending",
            "priority": "high", "question": "q", "created_at": _FROZEN}

    a = db.create_platform_operator_queue_item(agent, {**item, "title": "a"}, subject="k:a")
    b = db.create_platform_operator_queue_item(agent, {**item, "title": "b"}, subject="k:b")

    assert (a["outcome"], b["outcome"]) == ("created", "created")
    assert (a["row"]["title"], b["row"]["title"]) == ("a", "b")
    assert a["row"]["request_id"] == item["id"], "an id that did not collide is kept as is"
    assert b["row"]["request_id"].startswith(item["id"] + "-")
    assert a["row"]["id"] != b["row"]["id"]
