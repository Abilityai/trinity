"""trinity-enterprise#450: suggesting registered accounts in the Access tab's add field.

The REAL sharing router, dependency chain and SQL over the unit DB. Only the agent
container is absent, and this route doesn't look for one.

  1. who may read: owner (non-admin too) and admin; a shared user and a stranger get
     the uniform 404; every key is refused;
  2. who's offered: matches by email, username and a word of the name; never the
     roster, owner, the reader, suspended accounts or accounts with no email;
  3. the bounds: a minimum query, a row cap, a rate limit, wildcards as text;
  4. a row carries only what it shows, and role only for an admin.
"""
from __future__ import annotations

import time
import uuid
from types import SimpleNamespace

import pytest
from starlette.testclient import TestClient

pytestmark = pytest.mark.unit


def _user(db, username, role="creator", *, name=None, email=None):
    from db_models import UserCreate
    db.create_user(UserCreate(username=username, email=email or f"{username}@example.com", role=role, name=name))
    return username


@pytest.fixture
def world(monkeypatch):
    from fastapi import FastAPI
    from jose import jwt

    from config import ALGORITHM, SECRET_KEY
    from database import db
    from routers import sharing
    from services import rate_limiter

    db.set_setting("terms_required", "false")
    tag = uuid.uuid4().hex[:8]
    agent = f"ent450-{tag}"
    owner = _user(db, f"o{tag}")
    admin = _user(db, f"a{tag}", "admin")
    shared = _user(db, f"s{tag}")
    stranger = _user(db, f"x{tag}", name=f"Xena {tag}")
    db.register_agent_owner(agent, owner)
    db.share_agent(agent, owner, f"{shared}@example.com")

    monkeypatch.setattr(rate_limiter, "_get_redis", lambda: None)
    rate_limiter.clear_inprocess()
    app = FastAPI()
    app.include_router(sharing.router)
    client = TestClient(app, raise_server_exceptions=False)

    def headers(username):
        token = jwt.encode({"sub": username, "exp": int(time.time()) + 300}, SECRET_KEY, algorithm=ALGORITHM)
        return {"Authorization": f"Bearer {token}"}

    def ask(q, as_=owner):
        return client.get(f"/api/agents/{agent}/access/candidates", params={"q": q}, headers=headers(as_))

    def emails(q, as_=owner):
        r = ask(q, as_)
        assert r.status_code == 200, r.text
        return [row["email"] for row in r.json()]

    yield SimpleNamespace(db=db, tag=tag, agent=agent, owner=owner, admin=admin, shared=shared, stranger=stranger,
                          ask=ask, emails=emails, client=client, headers=headers, rate_limiter=rate_limiter)
    rate_limiter.clear_inprocess()
    db.delete_setting("terms_required")


class TestWhoMayRead:
    def test_a_non_admin_owner_can(self, world):
        assert world.ask(world.stranger).status_code == 200

    def test_an_admin_can(self, world):
        assert world.ask(world.stranger, as_=world.admin).status_code == 200

    @pytest.mark.parametrize("who", ["shared", "stranger"])
    def test_anyone_else_gets_the_uniform_404(self, world, who):
        assert world.ask(world.tag, as_=getattr(world, who)).status_code == 404

    def test_an_unknown_agent_is_the_same_404(self, world):
        r = world.client.get("/api/agents/no-such-agent/access/candidates", params={"q": "ab"},
                             headers=world.headers(world.owner))
        assert r.status_code == 404

    def test_no_credential_is_refused(self, world):
        r = world.client.get(f"/api/agents/{world.agent}/access/candidates", params={"q": "ab"})
        assert r.status_code == 401

    @pytest.mark.parametrize("scope", ["agent", "user"])
    def test_a_key_is_refused_even_the_owners_own(self, world, monkeypatch, scope):
        monkeypatch.setattr(world.db, "validate_mcp_api_key", lambda *_a, **_k: {
            "scope": scope, "agent_name": world.agent if scope == "agent" else None,
            "key_id": "k1", "key_name": scope, "user_id": world.owner,
        })
        r = world.client.get(f"/api/agents/{world.agent}/access/candidates", params={"q": world.stranger},
                             headers={"Authorization": "Bearer trinity_mcp_test"})
        assert r.status_code in (401, 403), r.text
        assert world.stranger not in r.text


class TestWhoIsOffered:
    def test_it_matches_the_email_the_username_and_a_word_of_the_name(self, world):
        t = world.tag
        _user(world.db, f"pat{t}", name=f"Ann Quill{t}", email=f"zed{t}@example.com")
        mail = f"zed{t}@example.com"
        assert world.emails(f"zed{t}") == [mail]
        assert world.emails(f"pat{t}") == [mail]
        assert world.emails(f"quill{t}") == [mail]
        assert world.emails(f"ann quill{t}") == [mail]

    def test_the_middle_of_a_word_does_not_match(self, world):
        assert world.emails(world.stranger[2:]) == []

    def test_it_is_case_insensitive(self, world):
        assert world.emails(world.stranger.upper()) == [f"{world.stranger}@example.com"]

    def test_the_roster_is_not_offered(self, world):
        assert world.emails(world.shared) == []

    def test_a_roster_entry_is_matched_whatever_the_case_of_the_account(self, world):
        late = _user(world.db, f"late{world.tag}")
        world.db.share_agent(world.agent, world.owner, f"{late}@example.com")
        from db.engine import get_engine
        from db.tables import users
        with get_engine().begin() as conn:
            conn.execute(users.update().where(users.c.username == late).values(email=f"{late.upper()}@EXAMPLE.com"))
        assert world.emails(late) == []

    def test_the_owner_is_not_offered_even_to_an_admin(self, world):
        assert world.emails(world.owner, as_=world.admin) == []

    def test_the_reader_is_not_offered(self, world):
        assert world.emails(world.admin, as_=world.admin) == []

    def test_a_suspended_account_is_not_offered(self, world):
        gone = _user(world.db, f"gone{world.tag}")
        assert world.emails(gone) == [f"{gone}@example.com"]
        from db.engine import get_engine
        from db.tables import users
        with get_engine().begin() as conn:
            conn.execute(users.update().where(users.c.username == gone).values(suspended_at="2026-11-01T00:00:00Z"))
        assert world.emails(gone) == []

    @pytest.mark.parametrize("email", [None, ""])
    def test_an_account_with_no_email_is_not_offered(self, world, email):
        # On an agent with an empty roster, where the roster check can't hide it.
        lonely = f"ent450-lonely-{world.tag}"
        world.db.register_agent_owner(lonely, world.owner)
        none = _user(world.db, f"none{world.tag}")
        from db.engine import get_engine
        from db.tables import users
        with get_engine().begin() as conn:
            conn.execute(users.update().where(users.c.username == none).values(email=email))
        r = world.client.get(f"/api/agents/{lonely}/access/candidates", params={"q": none},
                             headers=world.headers(world.owner))
        assert (r.status_code, r.json()) == (200, [])

    def test_a_pending_invite_with_no_account_is_not_offered(self, world):
        other = f"ent450-other-{world.tag}"
        world.db.register_agent_owner(other, world.owner)
        world.db.share_agent(other, world.owner, f"ghost{world.tag}@example.com")
        assert world.emails(f"ghost{world.tag}") == []

    def test_someone_on_another_agents_roster_is_still_offered(self, world):
        other = f"ent450-other-{world.tag}"
        world.db.register_agent_owner(other, world.owner)
        world.db.share_agent(other, world.owner, f"{world.stranger}@example.com")
        assert world.emails(world.stranger) == [f"{world.stranger}@example.com"]


class TestTheBounds:
    @pytest.mark.parametrize("q", ["", " ", "a", " a "])
    def test_a_short_query_lists_no_one_and_costs_nothing(self, world, monkeypatch, q):
        from services import access_candidate_service as svc

        def boom(*_a, **_k):
            raise AssertionError("spent the rate limit")
        monkeypatch.setattr(svc.rate_limiter, "check", boom)
        assert world.emails(q) == []

    def test_two_characters_is_enough(self, world, monkeypatch):
        from services import access_candidate_service as svc
        seen = []
        monkeypatch.setattr(svc.db, "list_share_candidates", lambda *a, **k: seen.append(a[1]) or [])
        assert world.emails("ab") == []
        assert seen == ["ab"]

    def test_a_long_query_is_cut(self, world, monkeypatch):
        from services import access_candidate_service as svc
        seen = []
        monkeypatch.setattr(svc.db, "list_share_candidates", lambda *a, **k: seen.append(a[1]) or [])
        world.emails("a" * 500)
        assert seen == ["a" * svc.MAX_QUERY]

    def test_it_returns_at_most_eight(self, world):
        for i in range(10):
            _user(world.db, f"many{world.tag}{i}")
        assert len(world.emails(f"many{world.tag}")) == 8

    @pytest.mark.parametrize("wild", ["%%", "__", "%a", "_a"])
    def test_wildcards_are_text(self, world, wild):
        assert world.emails(wild) == []

    def test_the_rate_limit_is_a_named_429(self, world, monkeypatch):
        from services import access_candidate_service as svc
        monkeypatch.setattr(svc, "RATE_LIMIT", 2)
        assert world.ask(world.stranger).status_code == 200
        assert world.ask(world.stranger).status_code == 200
        r = world.ask(world.stranger)
        assert r.status_code == 429
        assert r.json()["detail"]["code"] == "rate_limited"
        assert int(r.headers["Retry-After"]) >= 1

    def test_the_limit_is_per_person(self, world, monkeypatch):
        from services import access_candidate_service as svc
        monkeypatch.setattr(svc, "RATE_LIMIT", 1)
        assert world.ask(world.stranger).status_code == 200
        assert world.ask(world.stranger).status_code == 429
        assert world.ask(world.stranger, as_=world.admin).status_code == 200


class TestARow:
    def test_an_owner_gets_only_email_and_label(self, world):
        rows = world.ask(world.stranger).json()
        assert rows == [{"email": f"{world.stranger}@example.com", "label": f"Xena {world.tag}"}]

    def test_an_admin_also_gets_role(self, world):
        rows = world.ask(world.stranger, as_=world.admin).json()
        assert rows == [{"email": f"{world.stranger}@example.com", "label": f"Xena {world.tag}", "role": "creator"}]

    def test_the_label_falls_back_to_the_username(self, world):
        plain = _user(world.db, f"plain{world.tag}")
        assert world.ask(plain).json()[0]["label"] == plain
