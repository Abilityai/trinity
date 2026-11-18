"""An owner-tier person takes over a share holder's schedule in place (#4281, slice D).

A share holder's schedule runs as a shared person: no shell, no owner-added
tools. The remedy is that the agent's owner (or an admin) adopts it, so its
runs get owner tier from then on. That is a grant (a prompt someone else wrote
gains the owner's access), so:

* only a person may do it (`require_person`), at owner tier (`OwnedAgent`);
* the owner adopts exactly what they reviewed: the request echoes
  `expected_updated_at` (compared as an instant) and the reviewed prompt
  fields, and any change since that read is refused;
* it is refused while a run is in flight, and on a schedule that already runs
  at owner tier;
* the webhook is revoked in the same statement;
* the former creator's write authorized just before it cannot land after it
  (review R5/R6);
* a delivery to another person's Workspace is cleared unless the request
  repeats the address (decision D8);
* "Run now" on such a schedule is refused for agent keys (decision D9).

Real routes, the real `get_current_user`, real JWTs and keys, on the unit
conftest's SQLite. Only the scheduler hop and the audit sink are replaced.
"""

from __future__ import annotations

import re
import secrets
import sys
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient
from sqlalchemy import text

from database import db
from db.engine import get_engine
from db_models import McpApiKeyCreate, UserCreate
from dependencies import create_access_token
from routers import schedules as schedules_router
from services import turn_tier_service

pytestmark = pytest.mark.unit

BODY = {
    "name": "brief",
    "cron_expression": "0 9 * * 1",
    "message": "Summarise my week",
    "timezone": "UTC",
}
REVIEWED = (
    "message",
    "allowed_tools",
    "model",
    "validation_enabled",
    "validation_prompt",
)


class _Scheduler:
    """The scheduler hop behind "Run now"; records that it was reached."""

    calls: list = []

    def __init__(self, *a, **k): ...
    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    async def post(self, url, **k):
        _Scheduler.calls.append(url)
        return SimpleNamespace(
            status_code=200, json=lambda: {"execution_id": "e-run-now"}, text="{}"
        )


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    # The webhook signing secret goes through the real cipher.
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    yield


@pytest.fixture
def audit(monkeypatch):
    rows = []

    async def record(**kw):
        rows.append(kw)

    monkeypatch.setattr(
        schedules_router.platform_audit_service,
        "log",
        lambda event_type, event_action, source, **kw: record(
            event_type=event_type, event_action=event_action, **kw
        ),
    )
    return rows


@pytest.fixture
def client(monkeypatch, audit):
    _Scheduler.calls = []
    monkeypatch.setattr(schedules_router.httpx, "AsyncClient", _Scheduler)
    app = FastAPI()
    app.include_router(schedules_router.router)
    return TestClient(app)


def _person(role, *, name=None):
    username = f"t4281-{uuid.uuid4().hex[:8]}@example.com"
    db.create_user(
        UserCreate(
            username=username, email=username, role=role, password="x", name=name
        )
    )
    user = db.get_user_by_username(username)
    return SimpleNamespace(
        username=username,
        email=username,
        id=user["id"],
        jwt=create_access_token({"sub": username}),
    )


@pytest.fixture
def world():
    owner = _person("creator")
    mate = _person("operator", name="Mate Shared")
    admin = _person("admin")
    agent = f"t4281-{uuid.uuid4().hex[:8]}"
    db.register_agent_owner(agent, owner.username)
    db.share_agent(agent, owner.username, mate.username)
    key = db.create_agent_mcp_api_key(agent, owner.username).api_key
    user_key = db.create_mcp_api_key(
        owner.username, McpApiKeyCreate(name="owner's own key")
    ).api_key
    return SimpleNamespace(
        agent=agent, owner=owner, mate=mate, admin=admin, key=key, user_key=user_key
    )


def _token(world, who):
    if who == "key":
        return world.key
    if who == "user_key":
        return world.user_key
    return getattr(world, who).jwt


def call(client, world, who, method, path="", **kw):
    return client.request(
        method,
        f"/api/agents/{world.agent}/schedules{path}",
        headers={"Authorization": f"Bearer {_token(world, who)}"},
        **kw,
    )


def created(client, world, who="mate", **extra):
    resp = call(client, world, who, "POST", json={**BODY, **extra})
    assert resp.status_code == 201, resp.text
    return resp.json()["id"]


def read(client, world, sid, who="owner"):
    resp = call(client, world, who, "GET", f"/{sid}")
    assert resp.status_code == 200, resp.text
    return resp.json()


def review(row, **over):
    """What the owner saw, echoed back verbatim."""
    body = {
        "expected_updated_at": row["updated_at"],
        "reviewed": {k: row[k] for k in REVIEWED},
    }
    body.update(over)
    return body


def take_over(client, world, sid, who="owner", body=None):
    if body is None:
        body = review(read(client, world, sid))
    return call(client, world, who, "POST", f"/{sid}/take-over", json=body)


def stored(sid):
    with get_engine().connect() as conn:
        return dict(
            conn.execute(
                text("SELECT * FROM agent_schedules WHERE id = :sid"), {"sid": sid}
            )
            .mappings()
            .one()
        )


def sql(statement, **params):
    with get_engine().begin() as conn:
        conn.execute(text(statement), params)


def code_of(resp):
    detail = resp.json()["detail"]
    return detail.get("code") if isinstance(detail, dict) else detail


# ---- who may take over -----------------------------------------------------------------


@pytest.mark.parametrize("who", ["owner", "admin", "user_key"])
def test_an_owner_tier_person_takes_over_a_share_holders_schedule(client, world, who):
    sid = created(client, world)
    before = read(client, world, sid)
    assert (before["runs_as"], before["can_take_over"]) == ("shared_person", True)

    resp = take_over(client, world, sid, who=who)

    assert resp.status_code == 200, resp.text
    taker = world.admin if who == "admin" else world.owner
    assert stored(sid)["owner_id"] == taker.id
    body = resp.json()
    assert (body["runs_as"], body["runs_as_reason"]["code"]) == (
        "owner",
        "creator_owner_tier",
    )
    assert (body["created_by_you"], body["can_take_over"]) == (True, False)
    assert body["message"] == BODY["message"]


def test_the_owners_agent_key_is_refused(client, world):
    """A grant: an agent must not re-home a share holder's prompt to owner tier."""
    sid = created(client, world)
    resp = take_over(
        client, world, sid, who="key", body=review(read(client, world, sid))
    )
    assert resp.status_code == 403, resp.text
    assert code_of(resp) == "person_required"
    assert stored(sid)["owner_id"] == world.mate.id


def test_a_share_holder_gets_the_uniform_404(client, world):
    sid = created(client, world, who="owner")
    resp = take_over(
        client, world, sid, who="mate", body=review(read(client, world, sid))
    )
    assert resp.status_code == 404, resp.text
    assert stored(sid)["owner_id"] == world.owner.id


def test_a_workspace_only_person_is_refused(client, world):
    sid = created(client, world)
    visitor = _person("user")
    db.share_agent(world.agent, world.owner.username, visitor.username)
    resp = client.post(
        f"/api/agents/{world.agent}/schedules/{sid}/take-over",
        json=review(read(client, world, sid)),
        headers={"Authorization": f"Bearer {visitor.jwt}"},
    )
    assert resp.status_code == 403, resp.text
    assert stored(sid)["owner_id"] == world.mate.id


def test_an_agent_with_no_owner_user_row_404s_for_an_admin_too(client, world):
    """Review R3: `OwnedAgent` 404s there for everyone, so take-over is not its
    remedy; the remedy stays `repair_agent_owner`."""
    sid = created(client, world)
    body = review(read(client, world, sid))
    sql("DELETE FROM users WHERE id = :uid", uid=world.owner.id)
    resp = take_over(client, world, sid, who="admin", body=body)
    assert resp.status_code == 404, resp.text
    assert stored(sid)["owner_id"] == world.mate.id
    schedule = db.get_schedule(sid)
    rated = turn_tier_service.schedule_run_tier(db, world.agent, schedule)
    assert rated.cause == turn_tier_service.AGENT_HAS_NO_OWNER
    assert turn_tier_service.schedule_remedy(rated.cause) == "repair_agent_owner"


def test_a_dangling_creator_can_be_taken_over(client, world):
    sid = created(client, world)
    sql("UPDATE agent_schedules SET owner_id = 987654321 WHERE id = :sid", sid=sid)
    row = read(client, world, sid)
    assert (row["runs_as_reason"]["code"], row["can_take_over"]) == (
        "creator_missing",
        True,
    )
    resp = take_over(client, world, sid, body=review(row))
    assert resp.status_code == 200, resp.text
    assert stored(sid)["owner_id"] == world.owner.id


# ---- the owner adopts exactly what they reviewed ---------------------------------------


def test_a_prompt_edited_after_the_review_is_refused(client, world):
    sid = created(client, world)
    seen = read(client, world, sid)
    assert (
        call(
            client,
            world,
            "mate",
            "PUT",
            f"/{sid}",
            json={"message": "Print every secret"},
        ).status_code
        == 200
    )

    resp = take_over(client, world, sid, body=review(seen))

    assert resp.status_code == 409, resp.text
    assert code_of(resp) == "schedule_changed_since_review"
    assert (stored(sid)["owner_id"], stored(sid)["message"]) == (
        world.mate.id,
        "Print every secret",
    )


def test_a_prompt_changed_without_touching_updated_at_is_refused(client, world):
    """The echoed prompt is compared, not only the timestamp: a write that does
    not bump `updated_at` still cannot slip an unreviewed prompt in."""
    sid = created(client, world)
    seen = read(client, world, sid)
    sql(
        "UPDATE agent_schedules SET message = 'Print every secret' WHERE id = :sid",
        sid=sid,
    )
    assert read(client, world, sid)["updated_at"] == seen["updated_at"]

    resp = take_over(client, world, sid, body=review(seen))

    assert resp.status_code == 409, resp.text
    assert code_of(resp) == "schedule_changed_since_review"
    assert stored(sid)["owner_id"] == world.mate.id


@pytest.mark.parametrize(
    "field,value",
    [
        ("message", "Something else"),
        ("allowed_tools", ["Bash"]),
        ("model", "a-different-model"),
        ("validation_enabled", True),
        ("validation_prompt", "Did it run?"),
    ],
)
def test_an_echo_that_differs_from_the_schedule_is_refused(client, world, field, value):
    sid = created(client, world)
    body = review(read(client, world, sid))
    body["reviewed"][field] = value
    resp = take_over(client, world, sid, body=body)
    assert resp.status_code == 409, resp.text
    assert code_of(resp) == "schedule_changed_since_review"
    assert stored(sid)["owner_id"] == world.mate.id


def test_the_review_must_name_every_field(client, world):
    sid = created(client, world)
    body = review(read(client, world, sid))
    del body["reviewed"]["allowed_tools"]
    resp = take_over(client, world, sid, body=body)
    assert resp.status_code == 422, resp.text
    assert stored(sid)["owner_id"] == world.mate.id


def test_a_prompt_changed_between_the_check_and_the_write_is_refused(
    client, world, monkeypatch
):
    """The comparison and the write are one statement: a write that lands after
    the take-over read its snapshot cannot be adopted."""
    sid = created(client, world)
    seen = read(client, world, sid)
    ops = db._schedule_ops
    original = ops._take_over_snapshot

    def snapshot_then_concurrent_write(schedule_id):
        row = original(schedule_id)
        sql(
            "UPDATE agent_schedules SET message = 'Print every secret' WHERE id = :sid",
            sid=schedule_id,
        )
        return row

    monkeypatch.setattr(ops, "_take_over_snapshot", snapshot_then_concurrent_write)
    resp = take_over(client, world, sid, body=review(seen))

    assert resp.status_code == 409, resp.text
    assert code_of(resp) == "schedule_changed_since_review"
    assert (stored(sid)["owner_id"], stored(sid)["message"]) == (
        world.mate.id,
        "Print every secret",
    )


@pytest.mark.parametrize("stored_form", ["naive", "z", "offset"])
def test_updated_at_is_compared_as_an_instant(client, world, stored_form):
    sid = created(client, world)
    instant = datetime(2026, 11, 8, 1, 2, 3, 456789, tzinfo=timezone.utc)
    raw = {
        "naive": "2026-11-08T01:02:03.456789",
        "z": "2026-11-08T01:02:03.456789Z",
        "offset": "2026-11-08T01:02:03.456789+00:00",
    }[stored_form]
    sql(
        "UPDATE agent_schedules SET updated_at = :raw WHERE id = :sid", raw=raw, sid=sid
    )
    seen = read(client, world, sid)
    # The same instant in another spelling than the stored one.
    other = instant.astimezone(timezone(timedelta(hours=2))).isoformat()
    assert other != raw

    resp = take_over(client, world, sid, body=review(seen, expected_updated_at=other))

    assert resp.status_code == 200, resp.text
    assert stored(sid)["owner_id"] == world.owner.id


def test_its_own_serialization_round_trips(client, world):
    sid = created(client, world)
    assert take_over(client, world, sid).status_code == 200


def test_one_microsecond_off_is_a_different_version(client, world):
    sid = created(client, world)
    seen = read(client, world, sid)
    off = (
        datetime.fromisoformat(seen["updated_at"].replace("Z", "+00:00"))
        + timedelta(microseconds=1)
    ).isoformat()
    resp = take_over(client, world, sid, body=review(seen, expected_updated_at=off))
    assert resp.status_code == 409, resp.text
    assert code_of(resp) == "schedule_changed_since_review"


# ---- refused when there is nothing to adopt, or while a run is in flight ---------------


def _execution(sid, agent, status):
    sql(
        "INSERT INTO schedule_executions (id, schedule_id, agent_name, status, started_at, message, triggered_by) "
        "VALUES (:id, :sid, :agent, :status, '2026-11-08T00:00:00Z', 'm', 'schedule')",
        id=f"e-{uuid.uuid4().hex[:8]}",
        sid=sid,
        agent=agent,
        status=status,
    )


@pytest.mark.parametrize("status", ["queued", "running", "pending_retry"])
def test_refused_while_a_run_is_in_flight(client, world, status):
    sid = created(client, world)
    _execution(sid, world.agent, status)
    resp = take_over(client, world, sid)
    assert resp.status_code == 409, resp.text
    assert code_of(resp) == "schedule_run_in_progress"
    assert stored(sid)["owner_id"] == world.mate.id


def test_a_finished_run_does_not_block_it(client, world):
    sid = created(client, world)
    _execution(sid, world.agent, "success")
    _execution(sid, world.agent, "failed")
    assert take_over(client, world, sid).status_code == 200


def test_an_owner_tier_schedule_is_not_taken_over(client, world):
    """Review R10: one admin cannot strip another owner-tier creator's schedule
    (and its webhook)."""
    sid = created(client, world, who="owner")
    assert call(client, world, "owner", "POST", f"/{sid}/webhook").status_code == 200
    row = read(client, world, sid, who="admin")
    assert row["can_take_over"] is False
    resp = take_over(client, world, sid, who="admin", body=review(row))
    assert resp.status_code == 409, resp.text
    assert code_of(resp) == "schedule_not_restricted"
    assert stored(sid)["owner_id"] == world.owner.id
    assert stored(sid)["webhook_token"] is not None


def test_taking_over_twice_is_a_quiet_no_op(client, world, audit):
    sid = created(client, world)
    assert take_over(client, world, sid).status_code == 200
    updated_at = stored(sid)["updated_at"]
    again = take_over(client, world, sid)
    assert again.status_code == 200, again.text
    assert stored(sid)["updated_at"] == updated_at
    # Only this route's own action: the take-over also ends the former creator's
    # restricted-schedule alert, which audits `platform_cleared`.
    assert [a["event_action"] for a in audit if a["event_action"] == "schedule_taken_over"] == [
        "schedule_taken_over"
    ]


# ---- the webhook and the former creator -------------------------------------------------


def test_the_webhook_is_revoked_with_it(client, world):
    sid = created(client, world)
    minted = call(client, world, "mate", "POST", f"/{sid}/webhook")
    assert minted.status_code == 200, minted.text
    assert (
        call(client, world, "mate", "POST", f"/{sid}/webhook/secret").status_code == 200
    ), "signing secret"
    token = stored(sid)["webhook_token"]
    assert token and db.get_schedule_by_webhook_token(token) is not None

    assert take_over(client, world, sid).status_code == 200

    row = stored(sid)
    assert (
        row["webhook_token"],
        row["webhook_enabled"],
        row["webhook_secret_encrypted"],
        row["webhook_auth_enabled"],
    ) == (None, 0, None, 0)
    assert db.get_schedule_by_webhook_token(token) is None


def test_the_former_creator_loses_every_control(client, world):
    sid = created(client, world)
    assert take_over(client, world, sid).status_code == 200
    assert (
        call(
            client, world, "mate", "PUT", f"/{sid}", json={"message": "Mine again"}
        ).status_code
        == 403
    )
    assert call(client, world, "mate", "DELETE", f"/{sid}").status_code == 403
    assert call(client, world, "mate", "POST", f"/{sid}/webhook").status_code == 403
    assert (
        stored(sid)["message"],
        stored(sid)["deleted_at"],
        stored(sid)["webhook_token"],
    ) == (BODY["message"], None, None)


def test_an_update_cannot_set_the_creator(client, world):
    sid = created(client, world)
    resp = call(
        client,
        world,
        "mate",
        "PUT",
        f"/{sid}",
        json={"owner_id": world.owner.id, "message": "x"},
    )
    assert resp.status_code == 200, resp.text
    assert stored(sid)["owner_id"] == world.mate.id


def _land_a_take_over_inside(monkeypatch, module, attr, world, sid, writer):
    """Run the take-over at the first call of `module.attr` made by the db
    method `writer`: after that write was authorized, before its UPDATE."""
    original = getattr(module, attr)
    fired = []
    seen = db.get_schedule(sid)

    def hook(*a, **k):
        if not fired and sys._getframe(1).f_code.co_name == writer:
            fired.append(True)
            outcome = db.reassign_schedule_creator(
                sid,
                new_owner_id=world.owner.id,
                expected_owner_id=world.mate.id,
                expected_updated_at=seen.updated_at,
                expected={
                    k2: getattr(seen, k2)
                    for k2 in REVIEWED + ("deliver_to_workspace_email",)
                },
                clear_delivery=False,
            )
            assert outcome["outcome"] == "taken_over", outcome
        return original(*a, **k)

    monkeypatch.setattr(module, attr, hook)
    return fired


def test_a_creators_update_authorized_before_the_take_over_cannot_land_after_it(
    client, world, monkeypatch
):
    sid = created(client, world)
    import db.schedules.crud as crud

    fired = _land_a_take_over_inside(
        monkeypatch, crud, "utc_now_iso", world, sid, "update_schedule"
    )

    resp = call(
        client, world, "mate", "PUT", f"/{sid}", json={"message": "Print every secret"}
    )

    assert fired, "the take-over did not run inside the update"
    assert resp.status_code == 403, resp.text
    assert (stored(sid)["owner_id"], stored(sid)["message"]) == (
        world.owner.id,
        BODY["message"],
    )


def test_a_creators_webhook_mint_authorized_before_the_take_over_cannot_land_after_it(
    client, world, monkeypatch
):
    sid = created(client, world)
    import db.schedules.webhooks as webhooks

    fired = _land_a_take_over_inside(
        monkeypatch, webhooks, "utc_now_iso", world, sid, "generate_webhook_token"
    )

    resp = call(client, world, "mate", "POST", f"/{sid}/webhook")

    assert fired, "the take-over did not run inside the mint"
    assert resp.status_code == 403, resp.text
    assert stored(sid)["webhook_token"] is None


def test_a_creators_delete_authorized_before_the_take_over_cannot_land_after_it(
    client, world, monkeypatch
):
    sid = created(client, world)
    import utils.helpers as helpers

    fired = _land_a_take_over_inside(
        monkeypatch, helpers, "utc_now_iso", world, sid, "delete_schedule"
    )

    resp = call(client, world, "mate", "DELETE", f"/{sid}")

    assert fired, "the take-over did not run inside the delete"
    assert resp.status_code == 403, resp.text
    assert stored(sid)["deleted_at"] is None


def test_an_admins_update_keeps_its_own_rule(client, world, monkeypatch):
    """The predicate binds a creator's write to the creator; an admin may edit
    any schedule, before or after a take-over."""
    sid = created(client, world)
    import db.schedules.crud as crud

    _land_a_take_over_inside(
        monkeypatch, crud, "utc_now_iso", world, sid, "update_schedule"
    )
    resp = call(
        client, world, "admin", "PUT", f"/{sid}", json={"message": "Admin edit"}
    )
    assert resp.status_code == 200, resp.text
    assert stored(sid)["message"] == "Admin edit"


# ---- delivery to another person's Workspace (D8) ----------------------------------------


def _delivering(client, world):
    return created(client, world, deliver_to_workspace_email=world.mate.email)


def test_a_delivery_to_another_person_must_be_decided(client, world):
    sid = _delivering(client, world)
    resp = take_over(client, world, sid)
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert (detail["code"], detail["deliver_to_workspace_email"]) == (
        "schedule_delivers_to_another_person",
        world.mate.email,
    )
    assert stored(sid)["owner_id"] == world.mate.id


def test_clear_removes_the_delivery(client, world):
    sid = _delivering(client, world)
    resp = take_over(
        client, world, sid, body=review(read(client, world, sid), delivery="clear")
    )
    assert resp.status_code == 200, resp.text
    assert (stored(sid)["owner_id"], stored(sid)["deliver_to_workspace_email"]) == (
        world.owner.id,
        None,
    )


def test_keep_with_the_address_repeated_keeps_it(client, world):
    sid = _delivering(client, world)
    body = review(
        read(client, world, sid),
        delivery="keep",
        keep_delivery_to=f"  {world.mate.email.upper()} ",
    )
    resp = take_over(client, world, sid, body=body)
    assert resp.status_code == 200, resp.text
    assert stored(sid)["deliver_to_workspace_email"] == world.mate.email


@pytest.mark.parametrize("echo", [None, "someone-else@example.com"])
def test_keep_without_the_right_address_is_refused(client, world, echo):
    sid = _delivering(client, world)
    body = review(read(client, world, sid), delivery="keep", keep_delivery_to=echo)
    resp = take_over(client, world, sid, body=body)
    assert resp.status_code == 409, resp.text
    assert code_of(resp) == "schedule_delivers_to_another_person"
    assert stored(sid)["owner_id"] == world.mate.id


def test_a_delivery_to_the_taker_needs_no_decision(client, world):
    sid = created(client, world)
    sql(
        "UPDATE agent_schedules SET deliver_to_workspace_email = :e WHERE id = :sid",
        e=world.owner.email,
        sid=sid,
    )
    resp = take_over(client, world, sid)
    assert resp.status_code == 200, resp.text
    assert stored(sid)["deliver_to_workspace_email"] == world.owner.email


# ---- the record and the reads -----------------------------------------------------------


def test_an_audit_row_names_both_creators(client, world, audit):
    sid = _delivering(client, world)
    assert call(client, world, "mate", "POST", f"/{sid}/webhook").status_code == 200
    assert (
        take_over(
            client, world, sid, body=review(read(client, world, sid), delivery="clear")
        ).status_code
        == 200
    )
    (row,) = [a for a in audit if a["event_action"] == "schedule_taken_over"]
    assert row["event_type"].value == "authorization"
    assert (row["target_type"], row["target_id"]) == ("agent", world.agent)
    assert row["details"] == {
        "schedule_id": sid,
        "previous_creator_id": world.mate.id,
        "new_creator_id": world.owner.id,
        "webhook_revoked": True,
        "delivery_cleared": True,
    }


@pytest.mark.parametrize(
    "who,expected",
    [
        ("owner", True),
        ("admin", True),
        ("user_key", True),
        ("mate", False),
        ("key", False),
    ],
)
def test_can_take_over_says_what_the_route_does(client, world, who, expected):
    sid = created(client, world)
    rows = call(client, world, who, "GET").json()
    assert next(r for r in rows if r["id"] == sid)["can_take_over"] is expected


def test_can_take_over_is_false_on_the_owners_own_schedule(client, world):
    sid = created(client, world, who="owner")
    assert read(client, world, sid)["can_take_over"] is False


def test_the_remedy_is_take_over(client, world):
    sid = created(client, world)
    row = read(client, world, sid)
    assert "take it over" in row["runs_as_reason"]["message"]
    for cause in (
        turn_tier_service.CREATOR_NOT_OWNER_TIER,
        turn_tier_service.CREATOR_MISSING,
    ):
        assert turn_tier_service.schedule_remedy(cause) == "take_over"
    assert (
        turn_tier_service.schedule_remedy(turn_tier_service.AGENT_HAS_NO_OWNER)
        == "repair_agent_owner"
    )


# ---- "Run now" (D9) ---------------------------------------------------------------------


def test_an_agent_key_cannot_run_a_shared_persons_schedule_now(client, world):
    sid = created(client, world)
    resp = call(client, world, "key", "POST", f"/{sid}/trigger")
    assert resp.status_code == 409, resp.text
    assert code_of(resp) == "schedule_runs_as_shared_person"
    assert _Scheduler.calls == []


@pytest.mark.parametrize("who", ["owner", "admin", "user_key"])
def test_a_person_can_still_run_it_now(client, world, who):
    sid = created(client, world)
    resp = call(client, world, who, "POST", f"/{sid}/trigger")
    assert resp.status_code == 200, resp.text
    assert _Scheduler.calls == [
        f"{schedules_router.SCHEDULER_URL}/api/schedules/{sid}/trigger"
    ]


def test_an_agent_key_still_runs_an_owner_tier_schedule_now(client, world):
    sid = created(client, world, who="owner")
    resp = call(client, world, "key", "POST", f"/{sid}/trigger")
    assert resp.status_code == 200, resp.text
    assert len(_Scheduler.calls) == 1


def test_after_a_take_over_the_agent_key_may_run_it_now(client, world):
    sid = created(client, world)
    assert take_over(client, world, sid).status_code == 200
    assert call(client, world, "key", "POST", f"/{sid}/trigger").status_code == 200


def _system_key(world):
    """The platform's system key: an admin-owned agent key whose scope is
    `system`. It carries the admin's role without being a person."""
    system_agent = f"t4281-sys-{uuid.uuid4().hex[:6]}"
    db.register_agent_owner(system_agent, world.admin.username)
    key = db.create_agent_mcp_api_key(system_agent, world.admin.username)
    sql("UPDATE mcp_api_keys SET scope = 'system' WHERE id = :i", i=key.id)
    return key.api_key


def test_the_system_key_cannot_run_a_shared_persons_schedule_now(client, world):
    """D9 refuses every non-person principal, not only agent keys: the system
    key reaches this owner-tier route as an admin, and its run would have the
    admin's access."""
    sid = created(client, world)
    headers = {"Authorization": f"Bearer {_system_key(world)}"}
    path = f"/api/agents/{world.agent}/schedules/{sid}"
    resp = client.post(f"{path}/trigger", headers=headers)
    assert resp.status_code == 409, resp.text
    assert code_of(resp) == "schedule_runs_as_shared_person"
    assert _Scheduler.calls == []
    taken = client.post(f"{path}/take-over", headers=headers,
                        json=review(read(client, world, sid)))
    assert taken.status_code == 403, taken.text
    assert stored(sid)["owner_id"] == world.mate.id


# ---- the MCP surface reads the two 409s apart (Invariant #13) -------------------------
#
# `trigger_agent_schedule` (src/mcp-server/src/tools/schedules.ts) tells the
# calling agent "already running, poll for the run in flight" only when the 409
# is the scheduler's running lock, matched on its exact `detail`; any other 409
# is a refusal carrying the backend's code and message. The TS side is tested in
# its own runner; these pin the bodies it relies on, through the real route, so
# a backend rewording cannot quietly turn the lock into a refusal (or a coded
# refusal into a plain string).

_SCHEDULES_TS = (
    Path(__file__).resolve().parents[2] / "src" / "mcp-server" / "src" / "tools" / "schedules.ts"
)


def _mcp_running_lock_detail():
    found = re.findall(
        r'^const RUNNING_LOCK_DETAIL = "([^"]+)";$',
        _SCHEDULES_TS.read_text(encoding="utf-8"),
        flags=re.M,
    )
    assert len(found) == 1, "schedules.ts must declare RUNNING_LOCK_DETAIL once"
    return found[0]


def _scheduler_409(body):
    class _Conflicted(_Scheduler):
        async def post(self, url, **k):
            _Scheduler.calls.append(url)
            return SimpleNamespace(status_code=409, json=lambda: body, text=str(body))

    return _Conflicted


# The body `src/scheduler/main.py` sends when the schedule's lock is held (#1968).
_SCHEDULER_LOCK_BODY = {
    "status": "already_running",
    "schedule_id": "s",
    "schedule_name": "n",
    "agent_name": "a",
    "message": "Schedule is already executing",
}


def test_the_running_lock_409_is_the_detail_the_mcp_tool_matches(client, world, monkeypatch):
    sid = created(client, world, who="owner")
    monkeypatch.setattr(schedules_router.httpx, "AsyncClient", _scheduler_409(_SCHEDULER_LOCK_BODY))
    resp = call(client, world, "key", "POST", f"/{sid}/trigger")
    assert resp.status_code == 409, resp.text
    assert resp.json()["detail"] == _mcp_running_lock_detail()


@pytest.mark.parametrize(
    "body",
    [
        {"status": "paused", "message": "Schedule is paused for review"},
        {"code": "some_future_conflict", "message": "Not now."},
        {},
    ],
)
def test_a_scheduler_409_that_is_not_the_lock_is_not_relayed_as_the_lock(
    client, world, monkeypatch, body
):
    """Only the scheduler's lock denial is "already executing". Relaying any
    other scheduler 409 as the lock would make the MCP tool tell the agent to
    poll for a run that was never started."""
    sid = created(client, world, who="owner")
    monkeypatch.setattr(schedules_router.httpx, "AsyncClient", _scheduler_409(body))
    resp = call(client, world, "key", "POST", f"/{sid}/trigger")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert detail != _mcp_running_lock_detail()
    assert isinstance(detail, dict), detail
    assert detail["code"] == body.get("code", "scheduler_conflict")
    assert detail["message"] == body.get("message", "The scheduler refused to start this run.")


def test_the_shared_person_409_is_coded_so_the_mcp_tool_reads_it_as_a_refusal(client, world):
    sid = created(client, world)
    resp = call(client, world, "key", "POST", f"/{sid}/trigger")
    assert resp.status_code == 409, resp.text
    detail = resp.json()["detail"]
    assert isinstance(detail, dict), detail
    assert detail["code"] == "schedule_runs_as_shared_person"
    assert isinstance(detail["message"], str) and detail["message"]
    assert detail["message"] != _mcp_running_lock_detail()
