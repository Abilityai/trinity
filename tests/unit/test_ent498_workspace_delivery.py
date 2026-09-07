"""trinity-enterprise#498 — a schedule delivers into a Workspace conversation.

Almost all of this already existed: `channel_completion_report` has resolved,
persisted and effect-guarded a portal-bound completion since ent#457, and
`report_completion` is trigger-agnostic. What was missing was that a scheduled
execution row never carried `source_channel='portal'`. So the tests that matter
are about the STAMP and its refusals, not about a delivery mechanism.

Three properties carry the weight:

* the stamp only ever ADDS a destination — it can never repoint an inbound
  channel turn whose adapter is waiting on that reply;
* an address that cannot reach the agent is a VISIBLE failure, never a silent
  no-op (AC 5);
* both migration tracks carry the column, and the Alembic revision chains off
  the current head (Invariant #3, and the one-head rule).
"""
import ast
import re
from pathlib import Path

import pytest

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


# --- both migration tracks ---------------------------------------------------

def test_the_column_is_in_the_sqlite_ddl_and_the_metadata_mirror():
    schema = (REPO / "src/backend/db/schema.py").read_text()
    tables = (REPO / "src/backend/db/tables.py").read_text()
    assert "deliver_to_workspace_email TEXT" in schema
    assert 'Column("deliver_to_workspace_email", Text)' in tables


def test_the_sqlite_runner_carries_the_migration():
    mig = (REPO / "src/backend/db/migrations.py").read_text()
    assert "def _migrate_schedule_workspace_delivery" in mig
    assert '("schedule_workspace_delivery", _migrate_schedule_workspace_delivery)' in mig


def test_the_alembic_revision_exists_and_chains_off_the_head():
    """Invariant #3's second track. Two revisions sharing a `down_revision` is
    two heads, and `alembic upgrade head` is singular — it resolves its target
    BEFORE applying anything, so a forked graph applies ZERO revisions and every
    revision since the fork silently stops arriving."""
    rev = REPO / "src/backend/migrations/versions/0055_schedule_workspace_delivery.py"
    body = rev.read_text()
    assert 'revision = "0055_schedule_workspace_delivery"' in body
    assert 'down_revision = "0054_portal_session_main_chat"' in body
    # Mandatory, not defensive: a fresh PostgreSQL build runs init_schema_postgres
    # from db/schema.py FIRST and is then stamped through the chain, so this
    # revision routinely meets a column that already exists.
    assert "ADD COLUMN IF NOT EXISTS" in body
    assert "DROP COLUMN IF EXISTS" in body


def test_the_migration_graph_still_has_exactly_one_head():
    import subprocess
    out = subprocess.run(
        ["python3", str(REPO / "scripts/ci/check_alembic_heads.py"),
         str(REPO / "src/backend/migrations/versions")],
        capture_output=True, text=True,
    )
    assert out.returncode == 0, out.stdout + out.stderr


# --- the address, validated at the API boundary ------------------------------

@pytest.mark.parametrize("raw,expected", [
    ("  Person@Example.COM  ", "person@example.com"),
    ("", None),
    ("   ", None),
    (None, None),
])
def test_the_address_is_normalised_the_way_the_roster_stores_it(raw, expected):
    """Lower-cased and stripped because that is how the portal roster compares
    it — a case difference must not become an 'unreachable target' refusal at
    fire time, hours after the schedule was accepted. An empty string becomes
    None: "" is not a target, and arming delivery against it guarantees a
    refusal on the first run."""
    from db_models import ScheduleCreate
    assert ScheduleCreate._normalize_delivery_email(raw) == expected


@pytest.mark.parametrize("bad", [
    "not-an-address", "two@at@signs.com", "@nolocal.com", "nodomain@",
    "has space@example.com", "control\nchar@example.com", "x" * 400 + "@e.com",
])
def test_a_shape_that_cannot_be_an_address_is_a_named_422_not_a_dead_schedule(bad):
    """The point is not to validate email — real authorization is
    `agent_on_roster` at fire time. It is to fail at WRITE time instead of
    shipping a schedule that looks configured and dies on its first run."""
    import pydantic
    from db_models import ScheduleCreate
    with pytest.raises(pydantic.ValidationError):
        ScheduleCreate(name="n", cron_expression="0 9 * * *", message="m",
                       deliver_to_workspace_email=bad)


def test_the_create_and_update_models_cannot_diverge():
    """`models.py` is the API contract layer and `db_models.py` the persistence
    layer, so the rule is re-stated rather than imported. Driving BOTH over one
    table is what stops the update path from being laxer than the create path."""
    from db_models import ScheduleCreate
    from models import ScheduleUpdateRequest
    for raw in ("  A@B.com ", "", None, "x@y.z"):
        assert (ScheduleCreate._normalize_delivery_email(raw)
                == ScheduleUpdateRequest._normalize_delivery_email(raw))


def test_the_field_survives_the_response_model():
    """`ScheduleResponse` is built with `**schedule.model_dump()` and pydantic
    ignores extra keys — so a field missing from the response model is silently
    dropped from every read, with no error anywhere."""
    from models import ScheduleResponse
    assert "deliver_to_workspace_email" in ScheduleResponse.model_fields


def test_the_update_allow_list_lets_the_field_through():
    """`update_schedule` filters against an explicit allow-list, so a field
    absent from it is accepted by the API and then silently discarded."""
    crud = (REPO / "src/backend/db/schedules/crud.py").read_text()
    block = crud[crud.index("allowed_fields = ["):]
    assert '"deliver_to_workspace_email"' in block[:block.index("]")]


# --- the scheduler carries the address, and only the address -----------------

def test_the_scheduler_puts_the_address_on_the_dispatch_payload():
    svc = (REPO / "src/scheduler/service.py").read_text()
    assert 'payload["deliver_to_workspace_email"] = deliver_to_workspace_email' in svc
    assert "deliver_to_workspace_email=schedule.deliver_to_workspace_email" in svc


def test_the_scheduler_reads_the_column_defensively():
    """`SELECT *` everywhere, so a pre-migration row simply has no key. The
    mapper must not KeyError the whole cron loop over a column that has not
    landed yet."""
    dbm = (REPO / "src/scheduler/database.py").read_text()
    assert '"deliver_to_workspace_email" in row_keys' in dbm


def test_the_scheduler_does_not_try_to_resolve_a_session():
    """It is a separate process that cannot import the portal package, and it
    always sends `execution_id` — so `execute_task`'s channel-persisting branch
    can never run for a cron fire and channel columns passed as kwargs would be
    silently inert (#2426). Carrying the address is the whole of its job."""
    svc = (REPO / "src/scheduler/service.py").read_text()
    assert "ensure_main_session" not in svc
    assert "source_channel" not in svc


# --- the stamp ---------------------------------------------------------------

def test_the_stamp_only_ever_adds_a_destination():
    """Guarded on `source_channel IS NULL`. A row that already carries one
    belongs to an inbound channel turn whose adapter is waiting to send that
    reply, and repointing it would deliver the answer to the wrong place."""
    src = (REPO / "src/backend/db/schedules/executions.py").read_text()
    fn = src[src.index("def stamp_execution_channel_context"):]
    assert "source_channel.is_(None)" in fn
    assert "rowcount > 0" in fn, "a caller cannot refuse if the stamp reports nothing"


def test_the_stamp_is_reachable_through_the_facade():
    from database import DatabaseManager
    assert hasattr(DatabaseManager, "stamp_execution_channel_context")


# --- refusals are visible ----------------------------------------------------

def _refusal(monkeypatch, *, on_roster=True, blocked=False, roster_raises=False,
             stamped=True, email="client@example.com"):
    from services import schedule_workspace_delivery as swd

    class _PortalDb:
        @staticmethod
        def is_client_blocked(_e):
            return blocked

    def _on_roster(agent, addr, include_owned):
        if roster_raises:
            raise RuntimeError("db down")
        return on_roster

    import client_portal.db as portal_db
    import client_portal.service as portal_service
    import database as database_mod

    monkeypatch.setattr(portal_db, "is_client_blocked", _PortalDb.is_client_blocked)
    monkeypatch.setattr(portal_service, "agent_on_roster", _on_roster)
    monkeypatch.setattr(portal_service, "ensure_main_session",
                        lambda a, e: "session-1")
    monkeypatch.setattr(database_mod.db, "stamp_execution_channel_context",
                        lambda *a, **k: stamped)
    return swd


def test_an_address_that_cannot_reach_the_agent_is_refused(monkeypatch):
    """AC 5. Running the turn anyway would spend the tokens and put the answer
    where nobody can read it."""
    swd = _refusal(monkeypatch, on_roster=False)
    with pytest.raises(swd.WorkspaceDeliveryRefused) as ei:
        swd.resolve_and_stamp("e1", "analyst", "stranger@example.com")
    assert ei.value.reason == "workspace_delivery_target_unreachable"


def test_a_blocked_client_is_refused(monkeypatch):
    swd = _refusal(monkeypatch, blocked=True)
    with pytest.raises(swd.WorkspaceDeliveryRefused) as ei:
        swd.resolve_and_stamp("e1", "analyst", "client@example.com")
    assert ei.value.reason == "workspace_delivery_target_blocked"


def test_an_unreadable_roster_refuses_rather_than_delivering_blind(monkeypatch):
    """Fail CLOSED. The alternative is delivering a brief to an address whose
    access we could not confirm."""
    swd = _refusal(monkeypatch, roster_raises=True)
    with pytest.raises(swd.WorkspaceDeliveryRefused) as ei:
        swd.resolve_and_stamp("e1", "analyst", "client@example.com")
    assert ei.value.reason == "workspace_delivery_target_unverifiable"


def test_a_row_that_already_has_a_destination_is_refused(monkeypatch):
    swd = _refusal(monkeypatch, stamped=False)
    with pytest.raises(swd.WorkspaceDeliveryRefused) as ei:
        swd.resolve_and_stamp("e1", "analyst", "client@example.com")
    assert ei.value.reason == "workspace_delivery_row_not_stampable"


def test_an_empty_target_is_refused_rather_than_arming_delivery(monkeypatch):
    swd = _refusal(monkeypatch)
    with pytest.raises(swd.WorkspaceDeliveryRefused) as ei:
        swd.resolve_and_stamp("e1", "analyst", "   ")
    assert ei.value.reason == "workspace_delivery_no_target"


def test_a_reachable_target_stamps_the_row_with_portal_and_the_session(monkeypatch):
    from services import schedule_workspace_delivery as swd

    seen = {}
    import client_portal.db as portal_db
    import client_portal.service as portal_service
    import database as database_mod
    monkeypatch.setattr(portal_db, "is_client_blocked", lambda _e: False)
    monkeypatch.setattr(portal_service, "agent_on_roster", lambda *a: True)
    monkeypatch.setattr(portal_service, "ensure_main_session", lambda a, e: "sess-9")

    def _stamp(execution_id, **kwargs):
        seen["execution_id"] = execution_id
        seen.update(kwargs)
        return True
    monkeypatch.setattr(database_mod.db, "stamp_execution_channel_context", _stamp)

    assert swd.resolve_and_stamp("e1", "analyst", " Client@Example.com ") == "sess-9"
    assert seen["source_channel"] == "portal"
    assert seen["source_channel_chat_id"] == "sess-9"
    # The portal resolver matches this against the SESSION's own client_email and
    # fails closed when they differ — so it is the recipient check's other half,
    # not decoration.
    assert seen["source_channel_client"] == "client@example.com"


def test_access_is_checked_against_where_the_message_will_land(monkeypatch):
    """`agent_on_roster(..., include_owned=True)` — the Workspace's own roster.
    `email_has_agent_access` was rejected because it admits any admin, and an
    admin who neither owns the agent nor is shared it cannot open that thread,
    so a brief delivered there would be invisible."""
    from services import schedule_workspace_delivery as swd
    import client_portal.db as portal_db
    import client_portal.service as portal_service
    import database as database_mod

    calls = []
    monkeypatch.setattr(portal_db, "is_client_blocked", lambda _e: False)
    monkeypatch.setattr(portal_service, "agent_on_roster",
                        lambda *a: (calls.append(a), True)[1])
    monkeypatch.setattr(portal_service, "ensure_main_session", lambda a, e: "s")
    monkeypatch.setattr(database_mod.db, "stamp_execution_channel_context",
                        lambda *a, **k: True)

    swd.resolve_and_stamp("e1", "analyst", "c@e.com")
    assert calls[0] == ("analyst", "c@e.com", True)

    # The AST check, not a substring one: the module docstring NAMES the
    # rejected function in order to explain why it was rejected, and a substring
    # guard reads that as the violation.
    src = (REPO / "src/backend/services/schedule_workspace_delivery.py").read_text()
    tree = ast.parse(src)
    called = {n.func.id if isinstance(n.func, ast.Name) else
              getattr(n.func, "attr", "")
              for n in ast.walk(tree) if isinstance(n, ast.Call)}
    assert "email_has_agent_access" not in called


# --- the dispatch path -------------------------------------------------------

def test_the_refusal_runs_before_both_dispatch_branches():
    """A refusal must not depend on whether the caller asked for async — and the
    async branch returns an `accepted` ack the scheduler then polls, so refusing
    after it would leave a row nobody ever fails."""
    src = (REPO / "src/backend/routers/internal.py").read_text()
    resolve = src.index("schedule_workspace_delivery.resolve_and_stamp")
    async_branch = src.index("if request.async_mode:", src.index("async def execute_task_internal"))
    assert resolve < async_branch


def test_a_refusal_writes_a_failed_terminal_on_the_pre_created_row():
    """AC 5 again, at the wiring level: 'never a silent no-op'."""
    src = (REPO / "src/backend/routers/internal.py").read_text()
    block = src[src.index("except schedule_workspace_delivery.WorkspaceDeliveryRefused"):]
    block = block[:block.index("if request.async_mode:")]
    assert "_fail_execution_row" in block
    assert "idempotency_service.fail" in block, (
        "a refused dispatch must release its idempotency claim, or a retry of "
        "the same fire replays the refusal instead of being re-attempted"
    )


def test_the_terminal_write_cannot_overwrite_a_finished_run():
    src = (REPO / "src/backend/routers/internal.py").read_text()
    fn = src[src.index("def _fail_execution_row"):]
    fn = fn[:fn.index("\nasync def ")]
    assert "TaskExecutionStatus.SUCCESS" in fn and "not in" in fn


def test_the_internal_request_model_carries_the_address():
    from models import InternalTaskExecutionRequest
    assert "deliver_to_workspace_email" in InternalTaskExecutionRequest.model_fields


# --- C9: never interleaved with an in-flight turn ----------------------------

def test_the_portal_leg_waits_for_an_in_flight_turn_before_writing():
    """C9. `PortalConversation` detects a reply by an assistant-row count delta
    and renders the LAST assistant row, so a report landing mid-turn can be read
    as that turn's answer."""
    src = (REPO / "src/backend/services/channel_completion_report.py").read_text()
    fn = src[src.index("async def deliver() -> bool:"):]
    assert "get_turn_inflight" in fn


def test_the_wait_is_bounded_and_never_drops_the_report():
    """A wait that could refuse would trade a cosmetic misread for a lost brief,
    which is much worse."""
    src = (REPO / "src/backend/services/channel_completion_report.py").read_text()
    assert re.search(r"_INFLIGHT_WAIT_SECONDS\s*=\s*\d", src)
    fn = src[src.index("async def deliver() -> bool:"):]
    body = fn[:fn.index("def _write()")]
    # The loop must have a `while ... < budget` bound and no early `return False`
    # on the timeout path.
    assert "while waited < _INFLIGHT_WAIT_SECONDS" in body
    assert "return False" not in body


def test_schedule_is_still_not_an_inline_trigger():
    """The whole mechanism rests on this: a scheduled run has no surface that
    already answered, so `report_completion` must not skip it."""
    from services.channel_completion_report import INLINE_CHANNEL_TRIGGERS
    assert "schedule" not in INLINE_CHANNEL_TRIGGERS


# --- the third surface -------------------------------------------------------

def test_the_mcp_tools_can_set_and_clear_the_target():
    """Invariant #13. `null` is meaningful and `undefined` is not — the handler
    uses exclude_unset, so a truthiness check would make the field unsettable."""
    ts = (REPO / "src/mcp-server/src/tools/schedules.ts").read_text()
    assert ts.count("deliver_to_workspace_email") >= 5
    assert "if (args.deliver_to_workspace_email !== undefined)" in ts
    types = (REPO / "src/mcp-server/src/types.ts").read_text()
    assert "deliver_to_workspace_email?: string | null;" in types


# --- the delivered brief is rateable like any agent message (AC 7) -----------

def _delivered_row():
    """The row shape `channel_completion_report._resolve_portal` writes.

    Mirrors its `add_portal_message(id, session_agent, client_email, "assistant",
    body, None, now, session_id=chat_id)` call — all three fields the rating
    predicate checks come from the SESSION row, not from the execution stamp.
    """
    return {
        "id": "m-brief",
        "agent_name": "analyst",
        "client_email": "client@example.com",
        "role": "assistant",
    }


def test_the_delivered_brief_is_rateable(monkeypatch):
    """AC 7. Claimed "by construction" in the flow doc — construction is exactly
    what a later edit changes, so it is pinned. `_rating_target_is_visible`
    demands agent match, client match and `role == 'assistant'`; the delivery leg
    satisfies all three, which is why a brief can be thumbed down like anything
    else the agent said."""
    from client_portal import service
    monkeypatch.setattr(service.db, "get_portal_message", lambda _id: _delivered_row())
    assert service._rating_target_is_visible(
        "analyst", "client@example.com", "message", "m-brief") is True


def test_a_brief_written_as_a_system_line_would_not_be_rateable(monkeypatch):
    """The discriminating half. Reset's notice is written with `role='system'`
    and is correctly unrateable — so this test proves the one above is asserting
    the delivery leg's ROLE choice and not merely that the predicate returns
    True for everything."""
    from client_portal import service
    row = _delivered_row() | {"role": "system"}
    monkeypatch.setattr(service.db, "get_portal_message", lambda _id: row)
    assert service._rating_target_is_visible(
        "analyst", "client@example.com", "message", "m-brief") is False


def test_the_delivery_leg_writes_an_assistant_row_from_the_session(monkeypatch):
    """The other end of the same contract: if `_resolve_portal` ever wrote a
    different role, or addressed the row from the execution stamp rather than
    the session, the two tests above would still pass while real briefs stopped
    being rateable."""
    src = (REPO / "src/backend/services/channel_completion_report.py").read_text()
    fn = src[src.index("def _resolve_portal"):]
    write = fn[fn.index("def _write()"):fn.index("try:", fn.index("def _write()"))]
    assert '"assistant"' in write
    assert "session_agent" in write and "client_email" in write


# --- who may point a schedule at somebody else's Workspace -------------------
#
# Review finding. Schedule creation is `assert_agent_access` — owner OR shared OR
# admin — and the delivery target may be any other person on the agent's roster.
# So a merely-SHARED user could schedule a recurring message, with a prompt of
# their choosing, into a colleague's Main chat, where it renders as an ordinary
# turn from the agent (`ensure_main_session` will create the thread if needed).
# ent#457's "a portal session belongs to exactly one client, so there is no third
# party for an allow_proactive bit to protect" does not carry over: here the
# schedule's AUTHOR need not be its RECIPIENT.

class _User:
    def __init__(self, uid="7", email="me@example.com"):
        self.id, self.email, self.username = uid, email, "me"


def _authority(monkeypatch, *, can_own, target, caller_email="me@example.com"):
    from routers import schedules
    monkeypatch.setattr(schedules.db, "can_user_share_agent", lambda *a: can_own)
    return schedules._enforce_delivery_target_authority(
        _User(email=caller_email), "analyst", target)


def test_a_shared_user_may_deliver_to_their_own_workspace(monkeypatch):
    """The common case — a person automating their own brief — must stay open to
    a shared user, or the gate costs more than it buys."""
    assert _authority(monkeypatch, can_own=False, target="me@example.com") is None


def test_a_shared_user_may_not_deliver_to_a_colleague(monkeypatch):
    from fastapi import HTTPException
    with pytest.raises(HTTPException) as ei:
        _authority(monkeypatch, can_own=False, target="colleague@example.com")
    assert ei.value.status_code == 403
    assert ei.value.detail["code"] == "delivery_target_requires_owner"


def test_the_owner_may_deliver_to_a_colleague(monkeypatch):
    assert _authority(monkeypatch, can_own=True, target="colleague@example.com") is None


def test_clearing_the_target_needs_no_authority(monkeypatch):
    """Stopping delivery is never a privileged act."""
    assert _authority(monkeypatch, can_own=False, target=None) is None
    assert _authority(monkeypatch, can_own=False, target="   ") is None


def test_an_unreadable_ownership_check_refuses(monkeypatch):
    """Fail closed: the question being answered is 'may this person put words in
    the agent's mouth toward someone else'."""
    from fastapi import HTTPException
    from routers import schedules

    def _boom(*a):
        raise RuntimeError("db down")
    monkeypatch.setattr(schedules.db, "can_user_share_agent", _boom)
    with pytest.raises(HTTPException) as ei:
        schedules._enforce_delivery_target_authority(
            _User(), "analyst", "colleague@example.com")
    assert ei.value.status_code == 403


def test_the_gate_is_on_both_the_create_and_the_update_path():
    """Create-only would be a formality: a shared user could create without the
    field and PUT it a second later."""
    src = (REPO / "src/backend/routers/schedules.py").read_text()
    assert src.count("_enforce_delivery_target_authority(") >= 3  # def + 2 call sites


def test_the_ownership_check_is_called_with_a_USERNAME(monkeypatch):
    """`can_user_share_agent` resolves via `get_user_by_username`, so an id finds
    no user and returns False — which refuses the OWNER too, turning this gate
    from a narrowing into a functional break.

    Every other test here stubs that function and so proves nothing about what is
    passed to it; this one inspects the argument. The first cut of this gate
    passed `str(current_user.id)` and every test still went green.
    """
    from routers import schedules

    seen = {}

    def _spy(user, agent):
        seen["user"], seen["agent"] = user, agent
        return True

    monkeypatch.setattr(schedules.db, "can_user_share_agent", _spy)
    schedules._enforce_delivery_target_authority(
        _User(uid="7", email="me@example.com"), "analyst", "colleague@example.com")
    assert seen["user"] == "me", (
        f"passed {seen['user']!r}; can_user_share_agent takes a username"
    )
    assert seen["agent"] == "analyst"


def test_the_case_comparison_is_normalised(monkeypatch):
    """`current_user.email` and the schedule field are both free text; a case
    difference must not turn 'myself' into 'a colleague'."""
    assert _authority(monkeypatch, can_own=False, target="  ME@Example.COM  ",
                      caller_email="me@example.com") is None
