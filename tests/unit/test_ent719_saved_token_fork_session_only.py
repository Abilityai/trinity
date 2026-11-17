"""trinity-enterprise#719 — the saved token's fork arm of agent creation is session-only.

`POST /api/agents` with a `fork_to_own` block and no token in the body forks the
template into the creator's GitHub account with the creator's SAVED personal
token (#3164), and that token becomes the new agent's per-agent token. Binding
a credential is INTERACTIVE (`docs/memory/requirements/auth.md` §2.8): a
signed-in session only. So that arm, and only that arm, refuses every key, the
person's own `user` key included, with the named 400 `FORK_PAT_REQUIRED` it
already gave an agent key; a vouched loopback JWT is refused the same way, as
`require_interactive` refuses it. Every other creation is unchanged: a fork
with a token in the body, a create with no fork, and agent-spawned creation.

Driven through the real `POST /api/agents` route (`routers.agents`, its
`require_role("creator")` and `agents.manage` gates, then the real
`create_agent_internal` → `_resolve_template` → `_apply_fork_to_own`), with
`get_current_user` overridden by stated principals (every `models.User` field
set; an admin owner, as on a default install). Name, volume and quota checks,
the template read and the GitHub fork are stubbed, and the create is stopped by
a sentinel at the step right after the fork arm (`_apply_agent_kind_default`),
before any GitHub call, so the answer shows how far a caller got: 400
`FORK_PAT_REQUIRED` with no fork attempted, or the sentinel 418 with the fork
made under the token the arm chose.

Sync throughout (`tests/unit/pytest.ini` runs pytest-asyncio in strict mode).
"""

from __future__ import annotations

import types

import pytest
from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

import routers.agents as agents_router
import services.agent_service.crud as crud
from models import User

pytestmark = pytest.mark.unit

SAVED = "ghp_saved_personal_s14"
FORM = "ghp_form_token_s14"
TEMPLATE_REPO = "acme/second-brain"
DEST = "alice/my-brain"
PAST_ARM = 418  # the sentinel: creation got past the fork arm


def _admin(**kw) -> User:
    base = dict(id=1, username="admin", email="admin@example.com", role="admin")
    base.update(kw)
    return User(**base)


SESSION = _admin()  # a signed-in session: `mcp_scope=None`
KEYS = {
    "agent": lambda: _admin(mcp_scope="agent", agent_name="s14-parent"),
    "system": lambda: _admin(mcp_scope="system"),
    "user": lambda: _admin(mcp_scope="user"),
}


@pytest.fixture
def create(monkeypatch):
    """POST /api/agents as `principal`; returns (status, detail, forks)."""
    forks = []

    async def fake_fork(**kw):
        forks.append(kw)
        return types.SimpleNamespace(destination_repo=kw["destination_repo"],
                                     default_branch="main")

    async def no_volume(*_a, **_k):
        return None

    async def source_read(*_a, **_k):
        return types.SimpleNamespace(metadata={}, reason=None, attempts=[],
                                     read_tier="per_user", preferred_reason=None,
                                     read_pat=SAVED)

    async def stop(*_a, **_k):
        raise HTTPException(status_code=PAST_ARM, detail="past the fork arm")

    monkeypatch.setattr(crud, "_check_name_availability", lambda config: None)
    monkeypatch.setattr(crud, "_guard_leftover_workspace_volume", no_volume)
    monkeypatch.setattr(crud, "clear_agent_breakers", lambda name: None)
    monkeypatch.setattr(crud, "_enforce_role_quota", lambda config, user: None)
    monkeypatch.setattr(crud, "validate_base_image", lambda image: None)
    # The creator's saved personal token is what the resolver finds (#3164).
    monkeypatch.setattr(crud, "_resolve_github_repo_and_pat",
                        lambda config, user, lookup, repo: (None, TEMPLATE_REPO, SAVED, "per_user"))
    monkeypatch.setattr(crud, "_read_source_template", source_read)
    monkeypatch.setattr(crud, "fork_template_to_own_repo", fake_fork)
    monkeypatch.setattr(crud.db, "get_git_config_agent_names_for_repo", lambda repo: [])
    monkeypatch.setattr(crud, "_apply_agent_kind_default", stop)
    import services.agent_skills_listing as listing
    monkeypatch.setattr(listing, "forget", lambda name: None)
    # The agent key holds `agents.manage`, so the real capability gate admits
    # it and it reaches the arm (the grant itself is trinity-enterprise#164's,
    # pinned by test_ent164_self_change_grants).
    monkeypatch.setattr(crud.db, "agent_has_capability",
                        lambda agent, capability: (agent, capability)
                        == ("s14-parent", "agents.manage"))

    app = FastAPI()
    app.include_router(agents_router.router)

    def run(principal, body):
        forks.clear()
        app.dependency_overrides[agents_router.get_current_user] = lambda: principal
        with TestClient(app, raise_server_exceptions=False) as client:
            r = client.post("/api/agents", json=body)
        return r.status_code, r.json().get("detail"), list(forks)

    return run


def _body(name="s14-fork", token=None, fork=True):
    body = {"name": name, "template": f"github:{TEMPLATE_REPO}"}
    if fork:
        body["fork_to_own"] = {"destination_repo": DEST, "private": True}
        if token:
            body["fork_to_own"]["github_pat"] = token
    return body


@pytest.mark.parametrize("scope", sorted(KEYS))
def test_a_key_cannot_fork_with_the_saved_token(create, scope):
    """The agent key, the system key and the person's own `user` key: the
    INTERACTIVE rule refuses each, before anything reaches GitHub."""
    status, detail, forks = create(KEYS[scope](), _body())
    assert status == 400, detail
    assert detail["code"] == "FORK_PAT_REQUIRED"
    assert "signed-in session" in detail["error"]
    assert forks == []


def test_a_vouched_loopback_cannot_fork_with_the_saved_token(create):
    status, detail, forks = create(_admin(vouched_source_agent="s14-parent"), _body())
    assert (status, detail["code"], forks) == (400, "FORK_PAT_REQUIRED", [])


def test_a_session_forks_with_the_saved_token(create):
    status, detail, forks = create(SESSION, _body())
    assert status == PAST_ARM, detail
    assert [(f["user_pat"], f["destination_repo"]) for f in forks] == [(SAVED, DEST)]


@pytest.mark.parametrize("who", ["session", *sorted(KEYS)])
def test_a_fork_with_a_token_in_the_body_is_unchanged(create, who):
    principal = SESSION if who == "session" else KEYS[who]()
    status, detail, forks = create(principal, _body(token=FORM))
    assert status == PAST_ARM, detail
    assert [f["user_pat"] for f in forks] == [FORM]


@pytest.mark.parametrize("who", ["session", *sorted(KEYS)])
def test_a_create_without_a_fork_is_unchanged(create, who):
    """No fork, so the arm never runs: every principal gets past it exactly
    as before, agent-spawned creation included."""
    principal = SESSION if who == "session" else KEYS[who]()
    status, detail, forks = create(principal, _body(fork=False))
    assert status == PAST_ARM, detail
    assert forks == []
