"""Hub onboarding: a fixed welcome in the member's Main at hand-over (abilityai/trinity-enterprise#1170).

Two halves:

* **OSS seam** (always runs): ``client_portal.db.add_portal_message_once`` —
  a deterministic-id insert that counts once, with no IntegrityError catch —
  and the terms door's generic landing: an inline acceptance hook may return a
  same-origin path, ``POST /api/terms/accept`` carries it as ``landing``.
* **Enterprise** (skips without the private submodule): the welcome text and
  its two placeholders, the once-per-member write into Main, the hello skip
  claimed as ``welcome:<id>``, the backstop write before the finish, the
  admin route's validation and audit, and the inline hook's landing.
"""
from __future__ import annotations

import asyncio
import importlib.util
import types
import uuid

import pytest

pytestmark = pytest.mark.unit

HAS_ENTERPRISE = importlib.util.find_spec("enterprise.backend.member_agents") is not None
needs_enterprise = pytest.mark.skipif(not HAS_ENTERPRISE,
                                      reason="private enterprise submodule not mounted")


def _uniq(prefix: str) -> str:
    return f"{prefix}-{uuid.uuid4().hex[:8]}"


def _rows(msg_id: str):
    from db.engine import get_engine

    with get_engine().connect() as conn:
        return [dict(r._mapping) for r in conn.exec_driver_sql(
            "SELECT * FROM enterprise_portal_messages WHERE id = ?", (msg_id,))]


def _message_count(session_id: str) -> int:
    from db.engine import get_engine

    with get_engine().connect() as conn:
        return conn.exec_driver_sql(
            "SELECT message_count FROM enterprise_portal_sessions WHERE id = ?",
            (session_id,)).scalar()


# --------------------------------------------------------------------------- #
# OSS: the insert-once helper
# --------------------------------------------------------------------------- #

class TestAddPortalMessageOnce:
    def test_a_second_write_inserts_nothing_and_counts_nothing(self):
        from client_portal import db as portal_db
        from client_portal import service as portal_service

        agent, email = _uniq("agent"), "member@example.com"
        session = portal_service.ensure_main_session(agent, email)
        msg_id = _uniq("member-welcome-7")

        first = portal_db.add_portal_message_once(msg_id, agent, email, "assistant", "hi",
                                                  "2026-11-08T00:00:00Z", session_id=session,
                                                  source="welcome")
        second = portal_db.add_portal_message_once(msg_id, agent, email, "assistant", "hi again",
                                                   "2026-11-08T00:00:01Z", session_id=session,
                                                   source="welcome")

        assert (first, second) == (True, False)
        rows = _rows(msg_id)
        assert len(rows) == 1 and rows[0]["content"] == "hi"
        assert rows[0]["role"] == "assistant" and rows[0]["source"] == "welcome"
        assert rows[0]["execution_id"] is None
        assert _message_count(session) == 1
        assert portal_db.portal_message_exists(msg_id) is True
        assert portal_db.portal_message_exists(_uniq("nope")) is False

    def test_it_does_not_rely_on_catching_an_integrity_error(self):
        """Decision #18: the duplicate is decided in SQL (ON CONFLICT DO
        NOTHING); a raised IntegrityError would surface here."""
        from client_portal import db as portal_db
        from client_portal import service as portal_service

        agent, email = _uniq("agent"), "member@example.com"
        session = portal_service.ensure_main_session(agent, email)
        msg_id = _uniq("dup")
        for _ in range(3):
            portal_db.add_portal_message_once(msg_id, agent, email, "assistant", "x",
                                              "2026-11-08T00:00:00Z", session_id=session)
        assert _message_count(session) == 1


# --------------------------------------------------------------------------- #
# OSS: the terms door's landing seam
# --------------------------------------------------------------------------- #

@pytest.fixture
def clean_hooks():
    from services import terms_service

    terms_service.clear_acceptance_hooks()
    yield
    terms_service.clear_acceptance_hooks()


class TestLandingSeam:
    @pytest.mark.parametrize("value,expected", [
        ("/workspace?agent=member-7", "/workspace?agent=member-7"),
        ("/", "/"),
        ("//evil.example", None),
        ("/\\evil.example", None),
        ("https://evil.example/", None),
        ("workspace", None),
        ("/work\nspace", None),
        ("/" + "a" * 600, None),
        ("", None),
        (None, None),
        (42, None),
    ])
    def test_only_a_same_origin_path_is_a_landing(self, value, expected):
        from services.terms_service import landing_path

        assert landing_path(value) == expected

    @pytest.mark.asyncio
    async def test_the_first_hook_that_names_a_path_wins(self, clean_hooks):
        from services import terms_service as ts

        async def silent(*_a):
            return None

        async def bad(*_a):
            return "//evil.example"

        async def names(*_a):
            return "/workspace?agent=member-7"

        async def later(*_a):
            return "/workspace?agent=other"

        for fn in (silent, bad, names, later):
            ts.register_acceptance_hook(fn, inline=True)
        assert await ts.run_inline_acceptance_hooks(7, "m@example.com", "v1", "t") \
            == "/workspace?agent=member-7"

    @pytest.mark.asyncio
    async def test_no_hooks_no_landing(self, clean_hooks):
        from services import terms_service as ts

        assert await ts.run_inline_acceptance_hooks(7, "m@example.com", "v1", "t") is None

    @pytest.mark.asyncio
    async def test_the_accept_route_carries_the_landing(self, clean_hooks, monkeypatch):
        from fastapi import BackgroundTasks

        from models import TermsAccept, TermsAccepted, User
        from routers import terms as terms_router
        from services import terms_gate, terms_service as ts

        async def inline(*_a):
            return "/workspace?agent=member-7"

        ts.register_acceptance_hook(inline, inline=True)

        async def _accept(*_a, **_k):
            return {"accepted": True, "created": True, "version": "v1",
                    "accepted_at": "2026-11-08T00:00:00Z", "user_id": 7,
                    "email": "m@example.com"}

        monkeypatch.setattr(ts, "accept", _accept)
        monkeypatch.setattr(terms_gate, "assert_door_open", lambda *_a: None)
        monkeypatch.setattr(terms_gate, "provider_door", lambda *_a: None)
        monkeypatch.setattr(terms_router, "reject_non_interactive_principal", lambda *_a: None)
        request = types.SimpleNamespace(client=types.SimpleNamespace(host="127.0.0.1"),
                                        state=types.SimpleNamespace(request_id="r"),
                                        scope={"path": "/api/terms/accept"})
        out = await terms_router.accept_terms(
            TermsAccept(version="v1"), request, BackgroundTasks(),
            current_user=User(id=7, username="m", email="m@example.com", role="user"))
        assert TermsAccepted(**out).landing == "/workspace?agent=member-7"


# --------------------------------------------------------------------------- #
# Enterprise: text, placeholders, settings
# --------------------------------------------------------------------------- #

@pytest.fixture
def welcome_settings():
    from database import db

    def reset():
        for key in ("member_welcome", "member_welcome_replaces_hello"):
            try:
                db.delete_setting(key)
            except Exception:  # noqa: BLE001
                pass

    reset()
    yield db
    reset()


@needs_enterprise
class TestRender:
    def test_both_tokens(self):
        from enterprise.backend.member_agents import welcome as w

        assert w.render("Hi {first_name}, I'm {agent_name}.", first_name="Ada",
                        agent_name="Scout") == "Hi Ada, I'm Scout."

    def test_a_missing_first_name_reads_naturally(self):
        from enterprise.backend.member_agents import welcome as w

        assert w.render("Hi {first_name}, I'm {agent_name}.", first_name="",
                        agent_name="Scout") == "Hi there, I'm Scout."

    def test_other_braces_pass_through_and_nothing_is_formatted(self):
        from enterprise.backend.member_agents import welcome as w

        text = "{0.__class__} {agent_name.__dict__} {} {email} {first_name}"
        assert w.render(text, first_name="Ada", agent_name="Scout") == \
            "{0.__class__} {agent_name.__dict__} {} {email} Ada"

    @pytest.mark.parametrize("member,expected", [
        ({"name": "Ada Lovelace", "email": "x@example.com"}, "Ada"),
        ({"name": "  ", "email": "grace.hopper@example.com"}, "Grace"),
        ({"name": None, "email": "42@example.com"}, ""),
        ({"name": "A" * 80}, "A" * 40),
        ({"name": "Ada\nEvil", "email": "x@example.com"}, "Ada"),
    ])
    def test_first_name_never_the_address(self, member, expected):
        from enterprise.backend.member_agents import welcome as w

        got = w.first_name_for(member)
        assert got == expected and "@" not in got

    def test_agent_name_is_the_label_else_never_the_slug(self, monkeypatch):
        from database import db
        from enterprise.backend.member_agents import welcome as w

        monkeypatch.setattr(db, "get_display_label", lambda name: None)
        assert w.agent_display_name("member-7") == w.AGENT_NAME_FALLBACK
        monkeypatch.setattr(db, "get_display_label", lambda name: "  Scout  ")
        assert w.agent_display_name("member-7") == "Scout"

    def test_the_default_copy_is_plain_and_promises_nothing_about_the_hello(self):
        """AC5 + decision #15."""
        from enterprise.backend.member_agents import welcome as w

        text = w.DEFAULT_MEMBER_WELCOME.lower()
        for token in ("member-", "trinity", "slug", "version", "in a moment", "introduce"):
            assert token not in text
        assert "{first_name}" in w.DEFAULT_MEMBER_WELCOME
        assert "{agent_name}" in w.DEFAULT_MEMBER_WELCOME


@needs_enterprise
class TestSettings:
    def test_absent_is_the_default_and_replace_is_off(self, welcome_settings):
        from enterprise.backend.member_agents import welcome as w

        assert w.resolve_welcome() == {"text": w.DEFAULT_MEMBER_WELCOME, "is_default": True,
                                       "replaces_hello": False}

    def test_set_empty_is_off_and_null_restores_the_default(self, welcome_settings):
        from enterprise.backend.member_agents import welcome as w

        assert w.set_welcome("  ", True) == {"text": "", "is_default": False,
                                             "replaces_hello": True}
        assert w.set_welcome(None, False)["is_default"] is True

    def test_too_long_is_a_named_refusal(self, welcome_settings):
        from enterprise.backend.member_agents import welcome as w

        w.set_welcome("x" * 4000, False)  # the cap itself is allowed
        with pytest.raises(w.WelcomeInvalid) as e:
            w.set_welcome("x" * 4001, False)
        assert e.value.code == "member_welcome_too_long"
        assert w.resolve_welcome()["text"] == "x" * 4000

    @pytest.mark.asyncio
    async def test_the_route_refuses_422_and_audits_lengths_only(self, welcome_settings,
                                                                 monkeypatch):
        from fastapi import HTTPException

        from enterprise.backend.member_agents import router as r
        from enterprise.backend.member_agents.models import MemberWelcomeUpdate
        from models import User

        audits = []

        async def _audit(action, **kw):
            audits.append((action, kw))

        monkeypatch.setattr(r.service, "_audit", _audit)
        admin = User(id=1, username="admin", email="admin@example.com", role="admin")

        with pytest.raises(HTTPException) as e:
            await r.set_member_welcome(MemberWelcomeUpdate(text="x" * 4001), current_user=admin)
        assert e.value.status_code == 422
        assert e.value.detail["code"] == "member_welcome_too_long"
        assert audits == []

        out = await r.set_member_welcome(
            MemberWelcomeUpdate(text="Secret plan {first_name}", replaces_hello=True),
            current_user=admin)
        assert out.text == "Secret plan {first_name}" and out.replaces_hello is True
        (action, kw), = audits
        assert action == "member_agent_welcome_set"
        assert "Secret plan" not in repr(kw)
        assert kw["details"]["length"] == len("Secret plan {first_name}")

    def test_the_route_is_behind_the_member_agent_admin_gate(self):
        """Pin on top of the executed tests above: the dependency that refuses
        agent keys and non-interactive sessions guards the write."""
        from enterprise.backend.member_agents import router as r

        route = next(x for x in r.router.routes
                     if getattr(x, "path", "") == "/api/enterprise/member-agents/settings/welcome")
        assert route.methods == {"PUT"}
        calls = {d.call for d in route.dependant.dependencies}
        assert r.require_member_agent_admin in calls


# --------------------------------------------------------------------------- #
# Enterprise: the write
# --------------------------------------------------------------------------- #

MEMBER = {"id": 7, "name": "Ada Lovelace", "email": "ada@example.com"}


@needs_enterprise
class TestWrite:
    def test_written_once_into_main_as_the_agents_message(self, welcome_settings, monkeypatch):
        from client_portal import db as portal_db
        from database import db
        from enterprise.backend.member_agents import welcome as w

        monkeypatch.setattr(db, "get_display_label", lambda name: "Scout")
        agent = _uniq("member")
        uid = int(uuid.uuid4().int % 10**6)

        first = w.write_welcome(uid, agent, member=MEMBER)
        second = w.write_welcome(uid, agent, member=MEMBER)

        assert first == second == f"member-welcome-{uid}-{agent}"
        rows = _rows(first)
        assert len(rows) == 1
        row = rows[0]
        assert row["role"] == "assistant" and row["source"] == "welcome"
        assert row["execution_id"] is None and row["cost"] is None
        assert row["content"] == "Hi Ada, I'm Scout. I'm here to help you — ask me anything, " \
                                 "or tell me what you're working on and we'll start there."
        assert row["client_email"] == "ada@example.com"
        assert row["session_id"] == portal_db.get_main_portal_session_id(agent, "ada@example.com")
        assert _message_count(row["session_id"]) == 1
        assert w.welcome_written(uid, agent) is True

    def test_off_writes_nothing(self, welcome_settings):
        from enterprise.backend.member_agents import welcome as w

        w.set_welcome("", False)
        agent = _uniq("member")
        assert w.write_welcome(8, agent, member=MEMBER) is None
        assert w.welcome_written(8, agent) is False

    def test_a_failure_is_swallowed_and_nothing_is_written(self, welcome_settings, monkeypatch):
        from client_portal import service as portal_service
        from enterprise.backend.member_agents import welcome as w

        def boom(*_a):
            raise RuntimeError("db down")

        monkeypatch.setattr(portal_service, "ensure_main_session", boom)
        agent = _uniq("member")
        assert w.write_welcome(9, agent, member=MEMBER) is None
        assert w.welcome_written(9, agent) is False


# --------------------------------------------------------------------------- #
# Enterprise: the hello skip (ruling T5)
# --------------------------------------------------------------------------- #

@pytest.fixture
def hello_env(monkeypatch):
    """`send_hello` with its row, its claim and its dispatch recorded."""
    from database import db
    from enterprise.backend.assignments import db as assignments_db
    from enterprise.backend.member_agents import db as member_db
    from enterprise.backend.member_agents import welcome as w
    from services import schedule_workspace_delivery, task_execution_service
    from services.agent_service import lifecycle

    state = types.SimpleNamespace(row={"user_id": 7, "agent_name": "member-7",
                                       "hello_execution_id": None, "deleted_at": None},
                                  claims=[], created=[], written=False, replaces=False)

    monkeypatch.setattr(member_db, "get_member_agent", lambda uid: dict(state.row))

    def _claim(uid, value):
        state.claims.append(value)
        state.row["hello_execution_id"] = value
        return True

    monkeypatch.setattr(member_db, "set_hello_execution", _claim)
    monkeypatch.setattr(assignments_db, "get_user", lambda uid: dict(MEMBER))
    monkeypatch.setattr(w, "welcome_written", lambda uid, name: state.written)
    monkeypatch.setattr(w, "resolve_welcome", lambda: {"text": "x", "is_default": False,
                                                       "replaces_hello": state.replaces})

    async def _ready(name):
        return True

    monkeypatch.setattr(lifecycle, "wait_for_agent_ready", _ready)
    monkeypatch.setattr(db, "get_agent_subscription_id", lambda name: None)
    monkeypatch.setattr(db, "create_task_execution",
                        lambda name, prompt, **kw: state.created.append(prompt))
    monkeypatch.setattr(schedule_workspace_delivery, "resolve_and_stamp", lambda *a: None)

    class _Svc:
        async def execute_task(self, **kw):
            return None

    monkeypatch.setattr(task_execution_service, "get_task_execution_service", lambda: _Svc())
    return state


@needs_enterprise
class TestHelloSkip:
    @pytest.mark.asyncio
    async def test_replaced_when_on_and_written(self, hello_env):
        from enterprise.backend.member_agents import hello

        hello_env.written, hello_env.replaces = True, True
        assert await hello.send_hello(7) is None
        assert hello_env.claims == ["welcome:member-welcome-7-member-7"]
        assert hello_env.created == [], "no execution is created"
        # A later sender finds the claim and greets nobody.
        assert await hello.send_hello(7) == "welcome:member-welcome-7-member-7"
        assert hello_env.created == []

    @pytest.mark.asyncio
    async def test_runs_after_the_welcome_when_off_and_does_not_repeat_it(self, hello_env):
        from enterprise.backend.member_agents import hello

        hello_env.written, hello_env.replaces = True, False
        execution_id = await hello.send_hello(7)
        assert execution_id and not execution_id.startswith("welcome:")
        assert hello_env.created == [hello.HELLO_PROMPT + hello.HELLO_AFTER_WELCOME]

    @pytest.mark.asyncio
    async def test_runs_as_today_when_the_write_failed(self, hello_env):
        from enterprise.backend.member_agents import hello

        hello_env.written, hello_env.replaces = False, True
        assert await hello.send_hello(7)
        assert hello_env.created == [hello.HELLO_PROMPT]


# --------------------------------------------------------------------------- #
# Enterprise: call sites
# --------------------------------------------------------------------------- #

@needs_enterprise
class TestCallSites:
    @pytest.mark.asyncio
    async def test_the_backstop_writes_before_the_finish(self, monkeypatch):
        """A row with no hello claim (a crash between the record and the
        welcome): the re-ensure writes the welcome, then schedules the finish."""
        from enterprise.backend.member_agents import adoption, service
        from enterprise.backend.member_agents import db as member_db
        from enterprise.backend.member_agents import welcome as w

        order = []
        row = {"user_id": 7, "agent_name": "member-7", "template_ref": "t",
               "owner_username": "hub-steward", "hello_execution_id": None,
               "deleted_at": None, "adopted_from_spare": 1}
        monkeypatch.setattr(member_db, "get_member_agent", lambda uid: dict(row))
        monkeypatch.setattr(w, "write_welcome",
                            lambda uid, name, member=None: order.append(("welcome", uid, name)))
        monkeypatch.setattr(adoption, "schedule_finish",
                            lambda uid, name: order.append(("finish", uid, name)))

        out = await service.ensure_member_agent(7)
        assert out.agent_name == "member-7"
        assert order == [("welcome", 7, "member-7"), ("finish", 7, "member-7")]

    @pytest.mark.asyncio
    async def test_a_claimed_row_is_left_alone(self, monkeypatch):
        """After a replaced hello the claim is a marker: re-ensuring neither
        re-writes nor restarts (eng E1)."""
        from enterprise.backend.member_agents import adoption, service
        from enterprise.backend.member_agents import db as member_db
        from enterprise.backend.member_agents import welcome as w

        order = []
        row = {"user_id": 7, "agent_name": "member-7", "template_ref": "t",
               "owner_username": "hub-steward", "deleted_at": None, "adopted_from_spare": 1,
               "hello_execution_id": "welcome:member-welcome-7-member-7"}
        monkeypatch.setattr(member_db, "get_member_agent", lambda uid: dict(row))
        monkeypatch.setattr(w, "write_welcome", lambda *a, **k: order.append("welcome"))
        monkeypatch.setattr(adoption, "schedule_finish", lambda *a: order.append("finish"))

        await service.ensure_member_agent(7)
        assert order == []

    @pytest.mark.asyncio
    async def test_the_inline_hook_names_the_members_conversation(self, monkeypatch):
        from enterprise.backend.member_agents import hook, service
        from services.entitlement_service import entitlement_service

        monkeypatch.setattr(entitlement_service, "is_entitled", lambda fid: True)

        async def _matches(*_a):
            return True

        monkeypatch.setattr(hook, "_address_matches", _matches)

        async def _ensure(uid, **kw):
            assert kw.get("inline") is True
            return service.MemberAgent(user_id=uid, agent_name="member-7", template_ref="t",
                                       owner_username="hub-steward", created=False,
                                       adopted=True)

        monkeypatch.setattr(service, "ensure_member_agent", _ensure)
        assert await hook.on_terms_accepted_inline(7, "ada@example.com", "v1", "t") \
            == "/workspace?agent=member-7"

        async def _none(uid, **kw):
            return None

        monkeypatch.setattr(service, "ensure_member_agent", _none)
        assert await hook.on_terms_accepted_inline(7, "ada@example.com", "v1", "t") is None
