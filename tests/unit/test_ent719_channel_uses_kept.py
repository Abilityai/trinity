"""trinity-enterprise#719 — the channel uses an agent keeps, beside the human-only gates.

Two things stay agent-callable on the channel routers. This file proves both
through the real routers:

* `PUT /api/agents/{name}/telegram/groups/{id}` (trinity-enterprise#265 / #600).
  The agent's own key still sets a group's trigger mode and welcome. The
  completion-report consent (`allow_proactive`) and the group-context switch
  (`context_enabled`) are a person's decision: `assert_person` inside the
  handler, an allowlist. It refuses the agent's key, the system key and every
  other machine scope, and the stored value does not move. A person passes. The
  route is listed `AGENT_CALLABLE_ANY` in the census, so the system key also
  keeps trigger mode and welcome; a test pins that too.
* The channel sends and lists (still in the frozen baseline; their listing is a
  separate change) keep admitting the agent's own key. Each reaches its handler
  and gets the handler's own answer.

Real keys through the real `get_current_user`, and an override with a stated
principal for the scopes the real path fences earlier. The same harness shape as
`test_ent719_channel_bindings_human_only.py`.
"""

from __future__ import annotations

import os
import secrets
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

os.environ.setdefault("REDIS_URL", "redis://u:p@localhost:6379")
os.environ.setdefault("SECRET_KEY", "test-secret")

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

from db_harness import db_backend, run as _hrun  # noqa: E402,F401

import dependencies  # noqa: E402
import routers.slack as _SL  # noqa: E402
import routers.telegram as _TG  # noqa: E402
import routers.voip as _VP  # noqa: E402
from database import db  # noqa: E402
from db_models import McpApiKeyCreate, UserCreate  # noqa: E402
from models import User  # noqa: E402

pytestmark = pytest.mark.unit

OWNER = "use719-owner"
ADMIN = "use719-admin"
AGENT = "use719-agent"
SIB = "use719-sib"  # owner-owned, spawned AGENT: AGENT is inside its manage reach
OTHER = "use719-other"  # owner-owned, unrelated: AGENT is outside its reach
SYSTEM = "use719-system"
CHAT = "-100719"

HUMAN_ONLY_DETAIL = dependencies.HUMAN_ONLY_DETAIL
LOOPBACK_FENCE_DETAIL = "Event-loopback tokens may only dispatch a subscriber task"


@pytest.fixture
def world(db_backend, monkeypatch):  # noqa: F811 — the db_harness fixture, by name
    monkeypatch.setattr(dependencies, "get_breaker_redis", lambda: None)
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    monkeypatch.setattr(_VP.voip_service, "is_available", lambda: True)
    db.create_user(
        UserCreate(username=OWNER, role="operator", email="use719-owner@example.com")
    )
    db.create_user(
        UserCreate(username=ADMIN, role="admin", email="use719-admin@example.com")
    )
    for agent in (AGENT, SIB, OTHER):
        db.register_agent_owner(agent, OWNER)
    owner_id = db.get_user_by_username(OWNER)["id"]

    # A bound bot with one group, at a known, non-default starting point.
    db.create_telegram_binding(AGENT, "123:tok", bot_username="bot719", bot_id="719")
    binding = db.get_telegram_binding(AGENT)
    cfg = db.get_or_create_telegram_group_config(binding["id"], CHAT, "g", "supergroup")
    db.update_telegram_group_config(
        cfg["id"],
        trigger_mode="mention",
        welcome_enabled=False,
        allow_proactive=True,
        context_enabled=True,
    )

    app = FastAPI()
    app.include_router(_TG.auth_router)
    app.include_router(_VP.auth_router)
    app.include_router(_SL.auth_router)
    client = TestClient(app)

    def as_(**fields):
        base = dict(
            id=owner_id,
            username=OWNER,
            email="use719-owner@example.com",
            role="operator",
        )
        base.update(fields)
        app.dependency_overrides[dependencies.get_current_user] = lambda: User(**base)

    def stored():
        return db.get_telegram_group_config(binding["id"], CHAT)

    yield SimpleNamespace(
        client=client,
        app=app,
        as_=as_,
        group_url=f"/api/agents/{AGENT}/telegram/groups/{cfg['id']}",
        stored=stored,
    )
    app.dependency_overrides.clear()


def _auth(token: str) -> dict:
    return {"Authorization": f"Bearer {token}"}


def _jwt(username: str) -> dict:
    return _auth(dependencies.create_access_token({"sub": username}))


def _agent_key(agent: str = AGENT) -> dict:
    return _auth(db.create_agent_mcp_api_key(agent, OWNER).api_key)


def _sibling_in_reach_key() -> dict:
    key = db.create_agent_mcp_api_key(SIB, OWNER)
    _hrun(
        "UPDATE agent_ownership SET spawned_by_agent = :s, spawned_by_key_id = :k "
        "WHERE agent_name = :a",
        s=SIB,
        k=key.id,
        a=AGENT,
    )
    return _auth(key.api_key)


def _system_key() -> dict:
    key = db.create_agent_mcp_api_key(SYSTEM, ADMIN)
    _hrun("UPDATE mcp_api_keys SET scope = 'system' WHERE id = :i", i=key.id)
    return _auth(key.api_key)


def _owner_user_key() -> dict:
    return _auth(
        db.create_mcp_api_key(OWNER, McpApiKeyCreate(name="owner key")).api_key
    )


# The person's fields, each moved away from the seeded value.
PERSON_FIELDS = {
    "allow_proactive": False,
    "context_enabled": False,
}


# --------------------------------------------------------------------------- #
# update_telegram_group: the agent keeps trigger mode and welcome
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "key",
    [
        pytest.param(_agent_key, id="agent-own-key"),
        pytest.param(_sibling_in_reach_key, id="agent-sibling-in-reach"),
    ],
)
def test_an_agent_key_still_sets_trigger_mode_and_welcome(world, key):
    res = world.client.put(
        world.group_url,
        json={"trigger_mode": "all", "welcome_enabled": True, "welcome_text": "hi"},
        headers=key(),
    )
    assert res.status_code == 200, res.text
    after = world.stored()
    assert after["trigger_mode"] == "all"
    assert after["welcome_enabled"] is True
    assert after["welcome_text"] == "hi"
    # The person's fields were not touched.
    assert after["allow_proactive"] is True
    assert after["context_enabled"] is True


def test_the_system_key_still_sets_trigger_mode_and_welcome(world):
    """The route is listed agent-callable (ANY), and the system key resolves to
    an admin with no agent identity, so it keeps trigger mode and welcome on an
    agent another person owns. Closing that is a ruling of its own and changes
    this test; the consent and context fields stay refused to it
    (test_a_machine_key_cannot_set_consent_or_context)."""
    res = world.client.put(
        world.group_url,
        json={"trigger_mode": "observe", "welcome_enabled": True, "welcome_text": "hi"},
        headers=_system_key(),
    )
    assert res.status_code == 200, res.text
    after = world.stored()
    assert after["trigger_mode"] == "observe"
    assert after["welcome_enabled"] is True
    assert after["welcome_text"] == "hi"
    # The person's fields were not touched.
    assert after["allow_proactive"] is True
    assert after["context_enabled"] is True


def test_a_sibling_outside_its_reach_gets_the_uniform_404(world):
    """The listed sibling answer: only an agent it spawned, never one its owner
    merely also owns."""
    res = world.client.put(
        world.group_url, json={"trigger_mode": "all"}, headers=_agent_key(OTHER)
    )
    assert res.status_code == 404, res.text
    assert res.json()["detail"] == "Agent not found"
    assert world.stored()["trigger_mode"] == "mention"


# --------------------------------------------------------------------------- #
# update_telegram_group: consent and context are a person's
# --------------------------------------------------------------------------- #

MACHINE_KEYS = {
    "agent-own-key": _agent_key,
    "agent-sibling-in-reach": _sibling_in_reach_key,
    "system-key": _system_key,
}

OVERRIDE_PRINCIPALS = {
    "ops-key": {"mcp_scope": "ops"},
    "portal-delegate-key": {"mcp_scope": "portal_delegate", "portal_delegate": True},
    "unknown-future-scope": {"mcp_scope": "a-scope-shipped-tomorrow"},
    "user-scope-carrying-an-agent": {"mcp_scope": "user", "agent_name": AGENT},
    "vouched-loopback-jwt": {
        "mcp_scope": None,
        "is_event_loopback": True,
        "vouched_source_agent": SIB,
    },
}


@pytest.mark.parametrize("field", list(PERSON_FIELDS))
@pytest.mark.parametrize("key", list(MACHINE_KEYS))
def test_a_machine_key_cannot_set_consent_or_context(world, key, field):
    before = world.stored()
    res = world.client.put(
        world.group_url, json={field: PERSON_FIELDS[field]}, headers=MACHINE_KEYS[key]()
    )
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == HUMAN_ONLY_DETAIL
    assert world.stored() == before


@pytest.mark.parametrize("field", list(PERSON_FIELDS))
@pytest.mark.parametrize("principal", list(OVERRIDE_PRINCIPALS))
def test_every_other_machine_principal_cannot_set_consent_or_context(
    world, principal, field
):
    before = world.stored()
    world.as_(**OVERRIDE_PRINCIPALS[principal])
    res = world.client.put(world.group_url, json={field: PERSON_FIELDS[field]})
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == HUMAN_ONLY_DETAIL
    assert world.stored() == before


def test_a_connector_key_is_refused_by_the_owner_fence_first(world):
    """A connector key never reaches the handler: the ownership dependency
    refuses an owner operation for it before the consent check runs."""
    before = world.stored()
    world.as_(mcp_scope="connector", connector_agent=AGENT)
    res = world.client.put(world.group_url, json={"allow_proactive": False})
    assert res.status_code == 403, res.text
    assert world.stored() == before


def test_a_principal_without_mcp_scope_cannot_set_consent(world):
    before = world.stored()
    uid = db.get_user_by_username(OWNER)["id"]
    world.app.dependency_overrides[dependencies.get_current_user] = lambda: (
        SimpleNamespace(
            id=uid,
            username=OWNER,
            email="use719-owner@example.com",
            role="operator",
            agent_name=None,
            connector_agent=None,
            portal_delegate=False,
        )
    )
    res = world.client.put(world.group_url, json={"allow_proactive": False})
    assert res.status_code == 403, res.text
    assert world.stored() == before


def test_an_unvouched_loopback_jwt_cannot_set_consent(world):
    """A real, signed loopback token with no source agent, through the real
    `get_current_user`: the entry fence refuses it here too, and the row does
    not move."""
    from jose import jwt

    before = world.stored()
    token = jwt.encode(
        {
            "sub": ADMIN,
            "scope": dependencies.EVENT_LOOPBACK_SCOPE,
            "exp": int(time.time()) + 300,
        },
        dependencies.SECRET_KEY,
        algorithm=dependencies.ALGORITHM,
    )
    res = world.client.put(
        world.group_url,
        json={"allow_proactive": False},
        headers=_auth(token),
    )
    assert res.status_code == 403, res.text
    assert res.json()["detail"] == LOOPBACK_FENCE_DETAIL
    assert world.stored() == before


def test_a_mixed_body_is_refused_whole(world):
    """A body that sets trigger mode AND consent writes nothing: the refusal
    comes before the update, so the agent's half does not land either."""
    res = world.client.put(
        world.group_url,
        json={"trigger_mode": "all", "allow_proactive": False},
        headers=_agent_key(),
    )
    assert res.status_code == 403, res.text
    after = world.stored()
    assert after["trigger_mode"] == "mention"
    assert after["allow_proactive"] is True


PERSONS = {
    "owner-jwt": lambda: _jwt(OWNER),
    "admin-jwt-not-owner": lambda: _jwt(ADMIN),
    "owner-user-key": _owner_user_key,
}


@pytest.mark.parametrize("field", list(PERSON_FIELDS))
@pytest.mark.parametrize("who", list(PERSONS))
def test_a_person_sets_consent_and_context(world, who, field):
    res = world.client.put(
        world.group_url, json={field: PERSON_FIELDS[field]}, headers=PERSONS[who]()
    )
    assert res.status_code == 200, res.text
    assert world.stored()[field] is PERSON_FIELDS[field]


# --------------------------------------------------------------------------- #
# The sends and lists keep admitting the agent's own key
# --------------------------------------------------------------------------- #


def test_the_own_key_still_lists_its_telegram_groups(world):
    res = world.client.get(f"/api/agents/{AGENT}/telegram/groups", headers=_agent_key())
    assert res.status_code == 200, res.text
    assert [g["chat_id"] for g in res.json()] == [CHAT]


def test_the_own_key_still_reaches_the_telegram_group_send(world):
    """The send reaches its handler: an unknown chat gets the handler's own
    answer, not an auth refusal."""
    res = world.client.post(
        f"/api/agents/{AGENT}/telegram/groups/-100000/messages",
        json={"message": "hello"},
        headers=_agent_key(),
    )
    assert res.status_code == 404, res.text
    assert res.json()["detail"] == "Group not found or not active for this agent"


def test_the_own_key_still_reads_its_voip_binding(world):
    res = world.client.get(f"/api/agents/{AGENT}/voip", headers=_agent_key())
    assert res.status_code == 200, res.text
    assert res.json()["configured"] is False


def test_the_own_key_still_reaches_the_voip_call(world):
    """The call reaches its handler: with no binding it is the service's own
    refusal, before anything is dialled."""
    res = world.client.post(
        f"/api/agents/{AGENT}/voip/call",
        json={"to_number": "+14155238886"},
        headers=_agent_key(),
    )
    assert res.status_code == 400, res.text
    assert (
        res.json()["detail"]
        == "No active Twilio voice binding configured for this agent"
    )


def test_the_own_key_still_lists_its_slack_channels(world):
    res = world.client.get(f"/api/agents/{AGENT}/slack/channels", headers=_agent_key())
    assert res.status_code == 200, res.text
    assert res.json() == {"channels": [], "count": 0}


def test_the_own_key_still_reads_its_slack_channel_binding(world):
    res = world.client.get(f"/api/agents/{AGENT}/slack/channel", headers=_agent_key())
    assert res.status_code == 200, res.text
    assert res.json() == {"bound": False}


def test_the_own_key_still_reaches_the_slack_channel_send(world):
    res = world.client.post(
        f"/api/agents/{AGENT}/slack/channels/C719/messages",
        json={"message": "hello"},
        headers=_agent_key(),
    )
    assert res.status_code == 404, res.text
    assert res.json()["detail"] == "Channel not found or not bound to this agent"
