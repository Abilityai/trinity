"""An agent's ask may address the project its turn is on — `to: project`
(abilityai/trinity-enterprise#1134, ruling 2 of 2026-11-07).

The OSS sink learns the WORD. Who a project reaches (its steward, then its
creator) is enterprise knowledge, so it answers through a registry-shaped seam
on `role_addressing` — `register_target_resolver("project", fn)` — exactly as
the assignments provider and the turn-context providers plug in. The sink:

* accepts `project` beside the four kinds and `role:<id>`; the `invalid_to`
  refusal teaches it; a `credential` ask is still `primary` only;
* with no resolver registered (an OSS-only install) refuses
  `project_unavailable`, filing nothing; a resolver that raises is the same
  refusal, never a 500; a resolver's own `RoleRefused` maps 1:1 to a 422;
* hands the resolver the agent and the PLATFORM's turn id (the validated
  `X-Trinity-Execution-Id`), never anything the agent wrote;
* writes `to_role = 'project'`, the one addressee, and the project id under
  the platform context key ent#981 already uses (`workspace_project_id`),
  which an agent-authored context can never carry;
* attaches the Workspace chat ONLY when the addressee's own running turn
  raised it — never a Main for a person who may be off the agent's roster;
* fences the ANSWER to `resolved_to` (ruling T4; the ent#1042 class), while a
  cancel keeps every ask's rule; the receipt never names the person;
* `readdress_item(attach_chat=False)` moves a row without creating a Main,
  and `project_steward_changed` is a platform re-address reason.

Harness: the unit island's per-process SQLite and the real sink (the ent#1007
shape); the target resolver is a fake registered per test.
"""
from __future__ import annotations

import json
import os
import sys
import uuid
from types import SimpleNamespace

import pytest

pytest.importorskip("sqlalchemy")

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", "src", "backend"))
if _BACKEND not in sys.path:
    sys.path.insert(0, _BACKEND)

pytestmark = pytest.mark.unit

# Bound at collection time — see test_ent747_748_ask_discuss_dismiss.py.
import services.ask_service  # noqa: E402,F401
import services.operator_queue_service  # noqa: E402,F401
import services.operator_resume_service  # noqa: E402,F401
import services.role_addressing  # noqa: E402,F401
import client_portal.service  # noqa: E402,F401

PROJECT = "proj-1134"
PROJECT_KEY = "workspace_project_id"
THREAD = "workspace_session_id"
TURN = "workspace_raised_in_turn"


def _uniq(prefix):
    return f"{prefix}-{uuid.uuid4().hex[:8]}@example.com"


@pytest.fixture
def real_db():
    from database import db as real
    return real


@pytest.fixture
def agent():
    return f"agent-1134-{uuid.uuid4().hex[:6]}"


@pytest.fixture
def people():
    return {"steward": _uniq("steward"), "creator": _uniq("creator"), "other": _uniq("other"),
            "owner": _uniq("owner"), "admin": _uniq("admin")}


@pytest.fixture(autouse=True)
def sink(monkeypatch, real_db):
    """The real sink over the real SQLite; its side effects recorded. The
    Main attachment is a recorder so a test can prove it was NOT taken."""
    from services import ask_service as svc
    from services import assignment_provider
    from services import operator_queue_service as oqs
    from services import operator_resume_service as ors
    from services import role_addressing
    from services.rate_limiter import RateLimitResult

    class _Audit:
        async def log(self, **kw):
            return "evt"

    class _WS:
        async def broadcast(self, message):
            json.loads(message)

    mains = []

    def _attachment(agent_name, email, **_):
        mains.append((agent_name, email))
        return f"main-{email}", False

    monkeypatch.setattr(ors, "spawn_on_loop", lambda factory: None)
    monkeypatch.setattr(ors, "spawn_ending_dispatch", lambda rows, **kw: None)
    monkeypatch.setattr(ors, "spawn_resume_dispatch", lambda item, **kw: None)
    monkeypatch.setattr(svc, "_opted_in", lambda agent_name: True)
    monkeypatch.setattr(svc, "_observers", [])
    monkeypatch.setattr(svc, "platform_audit_service", _Audit())
    monkeypatch.setattr(svc, "_websocket_manager", _WS())
    monkeypatch.setattr(svc, "_owner_email", lambda agent: None)
    monkeypatch.setattr(svc, "_platform_turn", lambda agent, execution_id: execution_id)
    monkeypatch.setattr(oqs, "_workspace_attachment", _attachment)
    monkeypatch.setattr(oqs.rate_limiter, "check",
                        lambda *a, **k: RateLimitResult(True, 10, 0, 60))
    assignment_provider.clear_provider()
    role_addressing.clear_target_resolvers()
    yield SimpleNamespace(mains=mains)
    assignment_provider.clear_provider()
    role_addressing.clear_target_resolvers()


class _Resolver:
    """A fake project resolver: answers one person for `PROJECT`, records
    what it was asked, or refuses / raises as a test says."""

    def __init__(self, person, *, seat="steward", refuse=None, boom=False):
        self.person, self.seat, self.refuse, self.boom = person, seat, refuse, boom
        self.calls = []

    def __call__(self, agent_name, turn):
        from services import role_addressing
        self.calls.append((agent_name, turn))
        if self.boom:
            raise RuntimeError("enterprise down")
        if self.refuse:
            raise role_addressing.RoleRefused(self.refuse, "project", f"refused: {self.refuse}")
        return role_addressing.TargetResolution(
            people=[self.person], single=self.person, project_id=PROJECT, seat=self.seat)


def _register(resolver):
    from services import role_addressing
    role_addressing.register_target_resolver("project", resolver)
    return resolver


def _raise(agent, request_id=None, *, to="project", kind="question", execution_id=None, **over):
    from services import ask_service
    rid = request_id or f"r-{uuid.uuid4().hex[:10]}"
    body = {"request_id": rid, "type": kind, "title": "Access to the drive",
            "question": "May the agent read the shared drive?", "to": to}
    if kind == "approval":
        body["options"] = ["approve", "reject"]
        body["proposal"] = {"grant": f"drive-{rid}"}
    if kind == "credential":
        body["context"] = {"credential_name": "DRIVE_TOKEN"}
    body.update(over)
    return ask_service.raise_ask(agent, body, raised_by="agent", channel="mcp",
                                 platform_execution_id=execution_id)


def _refused(agent, **kw):
    from services import ask_service
    with pytest.raises(ask_service.AskRejected) as exc:
        _raise(agent, **kw)
    return exc.value


def _row(real_db, ask_id):
    row = real_db.get_operator_queue_item(ask_id)
    resolved = row.get("resolved_to")
    if isinstance(resolved, str):
        row["resolved_to"] = json.loads(resolved)
    return row


def _rows(real_db, agent):
    return real_db.list_operator_queue_items(agent_name=agent, limit=50)


def _actor(email, role=None):
    from services import ask_service
    user = SimpleNamespace(role=role) if role else None
    return ask_service.Actor(email=email, user=user)


# ===========================================================================
# The word
# ===========================================================================

class TestTheWord:
    def test_invalid_to_teaches_project_beside_the_kinds_and_the_role_form(self, agent):
        exc = _refused(agent, to="boss")
        assert exc.code == "invalid_to"
        assert "project" in exc.extra["allowed"]
        assert set(exc.extra["allowed"]) >= {"primary", "approver", "viewer", "operator", "role:<id>"}

    def test_a_credential_ask_is_still_primary_only(self, agent, people):
        _register(_Resolver(people["steward"]))
        exc = _refused(agent, to="project", kind="credential")
        assert (exc.code, exc.extra["allowed"]) == ("invalid_to", ["primary"])
        assert _rows(real_db_of(), agent) == []

    def test_project_is_an_ask_target_not_a_role_a_report_or_message_may_address(self):
        from services import role_addressing
        assert "project" in role_addressing.ASK_TARGETS
        assert "project" not in role_addressing.ROLES
        with pytest.raises(role_addressing.RoleRefused) as exc:
            role_addressing.resolve("any", "project")
        assert exc.value.code == "invalid_to"


def real_db_of():
    from database import db as real
    return real


# ===========================================================================
# The seam
# ===========================================================================

class TestTheSeam:
    def test_no_resolver_registered_is_project_unavailable_and_files_nothing(self, real_db, agent):
        exc = _refused(agent)
        assert (exc.status_code, exc.code) == (422, "project_unavailable")
        assert "primary" in exc.message and "operator" in exc.message
        assert _rows(real_db, agent) == []

    def test_a_resolver_that_raises_is_project_unavailable_never_a_crash(self, real_db, agent, people):
        _register(_Resolver(people["steward"], boom=True))
        exc = _refused(agent)
        assert exc.code == "project_unavailable"
        assert _rows(real_db, agent) == []

    @pytest.mark.parametrize("code", ["not_in_project_turn", "project_unreachable"])
    def test_the_resolvers_own_refusal_maps_one_to_one(self, real_db, agent, people, code):
        _register(_Resolver(people["steward"], refuse=code))
        exc = _refused(agent)
        assert (exc.status_code, exc.code) == (422, code)
        assert exc.extra.get("role") == "project"
        assert _rows(real_db, agent) == []

    def test_the_resolver_is_handed_the_agent_and_the_platforms_turn(self, agent, people):
        resolver = _register(_Resolver(people["steward"]))
        _raise(agent, execution_id="exec-live-1", context={"execution_id": "exec-agent-wrote"})
        assert resolver.calls == [(agent, "exec-live-1")]

    def test_a_turn_the_platform_does_not_know_is_handed_as_none(self, agent, people, monkeypatch):
        from services import ask_service
        monkeypatch.setattr(ask_service, "_platform_turn", lambda agent, execution_id: None)
        resolver = _register(_Resolver(people["steward"]))
        _raise(agent, execution_id="manual")
        assert resolver.calls == [(agent, None)]

    def test_the_registry_holds_one_resolver_per_word_and_clears(self, people):
        from services import role_addressing
        first = _register(_Resolver(people["steward"]))
        second = _register(_Resolver(people["creator"]))
        assert role_addressing.resolve_target("project", "a", None).single == people["creator"]
        assert first.calls == [] and len(second.calls) == 1
        role_addressing.clear_target_resolvers()
        with pytest.raises(role_addressing.RoleRefused) as exc:
            role_addressing.resolve_target("project", "a", None)
        assert exc.value.code == "project_unavailable"

    def test_an_unknown_target_word_is_a_programming_error(self):
        from services import role_addressing
        with pytest.raises(ValueError):
            role_addressing.register_target_resolver("galaxy", lambda a, t: None)


# ===========================================================================
# What the sink writes
# ===========================================================================

class TestTheRow:
    def test_the_row_carries_the_target_the_one_addressee_and_the_project(self, real_db, agent, people, sink):
        _register(_Resolver(people["steward"]))
        receipt = _raise(agent, execution_id="exec-1")
        row = _row(real_db, receipt["id"])
        assert row["to_role"] == "project"
        assert row["resolved_to"] == [people["steward"]]
        assert row["addressed_to_email"] == people["steward"]
        assert row["context"][PROJECT_KEY] == PROJECT
        assert row["context"]["execution_id"] == "exec-1"
        # never a Main: a steward may be off the agent's roster (ent#981's reason)
        assert sink.mains == []
        assert THREAD not in row["context"]

    @pytest.mark.parametrize("seat", ["steward", "creator"])
    def test_the_card_names_the_readers_seat_as_its_destination(self, real_db, agent, people, seat):
        """The `destination` field ent#1135 shares: which rule picked this
        reader, stamped by the platform and projected — never the agent's."""
        from client_portal.asks.service import _project
        _register(_Resolver(people[seat], seat=seat))
        row = _row(real_db, _raise(agent, context={"workspace_project_seat": "creator"})["id"])
        assert row["context"]["workspace_project_seat"] == seat
        assert _project(row, viewer_email=people[seat]).destination == {"kind": "project", "seat": seat}

    def test_a_primary_ask_has_no_destination(self, real_db, agent, people):
        from client_portal.asks.service import _project
        _register(_Resolver(people["steward"]))
        row = _row(real_db, _raise(agent, to="primary",
                                   context={"workspace_project_seat": "steward"})["id"])
        assert "workspace_project_seat" not in (row["context"] or {})
        assert _project(row).destination is None

    def test_the_receipt_names_the_target_and_never_the_person(self, agent, people):
        _register(_Resolver(people["steward"]))
        receipt = _raise(agent)
        assert (receipt["to_role"], receipt["resolved"], receipt["status"]) == ("project", True, "created")
        assert people["steward"] not in json.dumps(receipt)

    def test_the_chat_attaches_only_when_the_addressees_own_turn_raised_it(
            self, real_db, agent, people, sink, monkeypatch):
        import client_portal.service as portal

        def chat_for_execution(agent_name, email, execution_id):
            return "chat-steward" if (email, execution_id) == (people["steward"], "exec-live") else None

        monkeypatch.setattr(portal, "chat_for_execution", chat_for_execution)
        _register(_Resolver(people["steward"]))
        in_turn = _row(real_db, _raise(agent, execution_id="exec-live")["id"])
        assert (in_turn["context"][THREAD], in_turn["context"][TURN]) == ("chat-steward", True)

        background = _row(real_db, _raise(agent, execution_id="exec-other")["id"])
        assert THREAD not in background["context"] and TURN not in background["context"]
        assert sink.mains == []

    def test_a_chat_lookup_that_fails_leaves_the_ask_standing_without_a_chat(
            self, real_db, agent, people, monkeypatch):
        import client_portal.service as portal

        def boom(*a):
            raise RuntimeError("portal down")

        monkeypatch.setattr(portal, "chat_for_execution", boom)
        _register(_Resolver(people["steward"]))
        row = _row(real_db, _raise(agent, execution_id="exec-live")["id"])
        assert row["status"] == "pending" and THREAD not in row["context"]

    def test_an_agent_cannot_author_the_project_key(self, real_db, agent, people):
        """The project key is platform-written: an agent naming one on a
        `primary` ask loses it, and on a `project` ask the resolver's id wins."""
        from services import assignment_provider

        class _P:
            def assignment_for(self, *a):
                return None

            def people_for(self, agent_name, role):
                return {"emails": [people["other"]]}

        assignment_provider.register_provider(_P())
        plain = _row(real_db, _raise(agent, to="primary", context={PROJECT_KEY: "forged"})["id"])
        assert PROJECT_KEY not in plain["context"]

        _register(_Resolver(people["steward"]))
        tagged = _row(real_db, _raise(agent, context={PROJECT_KEY: "forged"})["id"])
        assert tagged["context"][PROJECT_KEY] == PROJECT

    def test_a_retry_replays_and_a_changed_to_is_the_one_difference(self, agent, people):
        _register(_Resolver(people["steward"]))
        first = _raise(agent, "r-replay-1134")
        again = _raise(agent, "r-replay-1134")
        assert (again["status"], again["id"], again["differs"]) == ("replayed", first["id"], [])
        changed = _raise(agent, "r-replay-1134", to="operator")
        assert changed["differs"] == ["to"]

    def test_an_approval_question_and_alert_may_address_the_project(self, real_db, agent, people):
        _register(_Resolver(people["steward"]))
        for kind in ("approval", "question", "alert"):
            row = _row(real_db, _raise(agent, kind=kind)["id"])
            assert (row["type"], row["to_role"]) == (kind, "project")


# ===========================================================================
# Who may end it
# ===========================================================================

class TestTheFence:
    def test_only_the_addressee_answers_an_admin_may_cancel(self, real_db, agent, people):
        from services import ask_service
        _register(_Resolver(people["creator"], seat="creator"))
        row = _row(real_db, _raise(agent, kind="approval")["id"])
        assert ask_service.may_end(row, _actor(people["creator"])) is True
        assert ask_service.may_end(row, _actor(people["creator"].upper())) is True
        assert ask_service.may_end(row, _actor(people["other"])) is False
        admin = _actor(people["admin"], role="admin")
        assert ask_service.may_end(row, admin) is False
        assert ask_service.may_end(row, admin, cancelling=True) is True
        # a cancel keeps every ask's rule: the caller's access check
        assert ask_service.may_end(row, _actor(people["other"]), cancelling=True) is True

    def test_the_card_learns_who_it_waits_on(self, real_db, agent, people):
        from services import ask_service
        _register(_Resolver(people["steward"]))
        row = _row(real_db, _raise(agent)["id"])
        view = ask_service.addressed_view(row, _actor(people["other"]))
        assert view == {"waiting_on": [people["steward"]], "can_decide": False}
        assert ask_service.addressed_view(row, _actor(people["steward"]))["can_decide"] is True

    def test_the_refusal_names_the_project_never_a_person(self, real_db, agent, people):
        from services import ask_service
        _register(_Resolver(people["steward"]))
        row = _row(real_db, _raise(agent)["id"])
        exc = ask_service.AskNotAddressee(row["id"], role=ask_service._fenced_role_of(row))
        message = exc.role_message()
        assert message and "project" in message.lower()
        assert people["steward"] not in message

    def test_the_answer_door_refuses_a_non_addressee(self, real_db, agent, people):
        from services import ask_service
        _register(_Resolver(people["steward"]))
        row = _row(real_db, _raise(agent, kind="approval")["id"])
        with pytest.raises(ask_service.AskNotAddressee):
            ask_service.answer(row, response="approve", response_text=None, actor=_actor(people["other"]))
        assert _row(real_db, row["id"])["status"] == "pending"


# ===========================================================================
# Moving a project row
# ===========================================================================

class TestReaddress:
    def test_project_steward_changed_is_a_platform_readdress_reason(self):
        from services import ask_service
        assert ask_service.PROJECT_STEWARD_CHANGED in ask_service.PLATFORM_READDRESS_REASONS
        assert ask_service.ASSIGNMENT_CHANGED in ask_service.PLATFORM_READDRESS_REASONS

    def test_readdress_without_a_chat_moves_the_row_and_opens_no_main(self, real_db, agent, people, sink):
        from services import ask_service
        _register(_Resolver(people["steward"]))
        ask_id = _raise(agent)["id"]
        moved = ask_service.readdress_item(ask_id, [people["creator"]], people["creator"],
                                           reason=ask_service.PROJECT_STEWARD_CHANGED, attach_chat=False)
        assert moved is True
        row = _row(real_db, ask_id)
        assert row["resolved_to"] == [people["creator"]]
        assert row["addressed_to_email"] == people["creator"]
        assert row["context"][PROJECT_KEY] == PROJECT
        assert THREAD not in row["context"]
        assert sink.mains == []
        # the fence follows the move
        assert ask_service.may_end(row, _actor(people["creator"])) is True
        assert ask_service.may_end(row, _actor(people["steward"])) is False

    def test_readdress_with_a_chat_still_attaches_main_by_default(self, real_db, agent, people, sink):
        """The ent#1095 behaviour for a role row is unchanged."""
        from services import ask_service
        _register(_Resolver(people["steward"]))
        ask_id = _raise(agent)["id"]
        assert ask_service.readdress_item(ask_id, [people["creator"]], people["creator"],
                                          reason=ask_service.PROJECT_STEWARD_CHANGED) is True
        assert sink.mains == [(agent, people["creator"])]
        assert _row(real_db, ask_id)["context"][THREAD] == f"main-{people['creator']}"
