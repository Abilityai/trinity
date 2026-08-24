"""#1310 — behavioral characterization of the inline auth gates consolidated
behind the shared imperative-guard family (INV-8).

This suite LOCKS the CURRENT behavior of every router site the refactor touches
BEFORE anything moves, and must stay green AFTER migration — that identity is the
behavior-preserving proof. For each site it asserts the exact denial the site
raises today: status code (403) + the exact ``detail`` string + the admitted
principal set (owner / admin / stranger), driving the REAL ``db.can_user_*``
predicates against a real (temp) schema via ``db_harness`` (never a mocked auth
predicate — a wholesale-Mock ``db`` returns truthy and would false-green a broken
gate, learnings #762/#1446/#1533). Only non-auth resource lookups (a notification
/ session / subscription row) are stubbed, so the gate under test always runs its
real access check.

Structure:
  * A1 admin gates          — non-admin → 403 "Admin access required" (+ custom)
  * A2 access gates         — stranger → 403 + exact detail (assert_agent_access)
  * A3 owner gates          — stranger → 403 + exact detail (assert_agent_owner)
  * A4 Shape-F (owns-or-admin) — stranger→403, admin ADMITTED, anti-IDOR binding
  * A5 public.py (owns, no-admin) — stranger→403 AND admin-non-owner→403 (the
        access-WIDENING regression guard) + anti-IDOR binding
  * A6 anti-exfil sibling   — Agent B's key sharing Agent A's files → 403
  * A7 schedules ordering   — low-priv on a nonexistent agent → 403 (not 404)
  * A8 loops composite gate — admin/initiator ADMITTED, stranger fallback → 403
  * A9 slack.py (#1710)     — 3 read + 8 owner gates migrated onto the shared
        helpers: stranger→403 exact-detail; shared reader ADMITTED on reads,
        DENIED on owner gates; site #10 human-only reject + #11 send asymmetry

Helper-level truth tables (connector/ephemeral fence, owns_or_admin/owns type
parity) live in this file's Part B, added with the helpers in the same PR.
"""

from __future__ import annotations

import asyncio
import sys
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

# --- import surface (mirror the other real-db unit tests) --------------------
_BACKEND = str(Path(__file__).resolve().parents[2] / "src" / "backend")
while _BACKEND in sys.path:
    sys.path.remove(_BACKEND)
sys.path.insert(0, _BACKEND)

_TESTS = str(Path(__file__).resolve().parents[1])
if _TESTS not in sys.path:
    sys.path.insert(0, _TESTS)

from db_harness import db_backend, run, seed_agent, seed_user  # noqa: E402,F401

# Capture the REAL service modules at collection time so a sibling unit test's
# sys.modules stub can't leak into the handler bodies this suite drives
# (learnings #762/#1446/#1533). Two concrete leaks this guards against:
#   * a Mock ``services.docker_service`` leaking a truthy container probe (#186);
#   * ``test_inject_assigned_credentials`` permanently installing a fake
#     ``services.agent_service`` package (``agent_service_pkg_under_test_612``,
#     lacking ``start_agent_internal``) at ITS import — which the owner-admit
#     path (``subscriptions.clear_agent_subscription`` /
#     ``assign_subscription_to_agent`` lazy-import ``start_agent_internal`` at
#     call time) would otherwise trip with a bare ``ImportError`` that escapes
#     ``_raised``. Collection imports every selected file before any test runs,
#     so that fake is present regardless of execution order — the ``_pin_real_service_modules``
#     autouse fixture below re-pins these per test (monkeypatch auto-restores the
#     sibling's stub afterward). This file sorts first in collection order, so
#     the captures here bind the genuine modules.
import services  # noqa: E402

# If a sibling already installed a fake ``services.agent_service`` stub package
# that persists for the session (``test_inject_assigned_credentials`` does this
# at its own import), evict the subtree and import the genuine package from disk
# BEFORE the router imports below — ``routers/agents.py`` binds
# ``start_agent_internal`` from it at ITS module top, so a cached fake would
# abort THIS module's collection with an ImportError. In natural sorted
# collection order this file sorts ahead of every persistent stubber, so the
# eviction is a no-op there; the guard just makes the capture order-independent.
_cached_agent_service = sys.modules.get("services.agent_service")
if _cached_agent_service is not None and getattr(
    _cached_agent_service, "__name__", ""
) != "services.agent_service":
    for _k in [
        k for k in list(sys.modules)
        if k == "services.agent_service" or k.startswith("services.agent_service.")
    ]:
        sys.modules.pop(_k, None)
import services.agent_service  # noqa: E402
import services.docker_service  # noqa: E402
_REAL_SERVICES_PKG = sys.modules["services"]
_REAL_AGENT_SERVICE = sys.modules["services.agent_service"]
_REAL_DOCKER_SERVICE = sys.modules["services.docker_service"]

# The service-package names this file re-pins to their genuine modules per test.
# Declaring this list + the ``_restore_sys_modules`` autouse fixture below is the
# linter-recognized escape hatch (`tests/lint_sys_modules.py`) for the import-time
# eviction above — a module-level ``sys.modules.pop`` that no monkeypatch fixture
# can reach (precedent: tests/unit/test_telegram_webhook_backfill.py).
_STUBBED_MODULE_NAMES = [
    "services",
    "services.agent_service",
    "services.docker_service",
]
_REAL_MODULES = {
    "services": _REAL_SERVICES_PKG,
    "services.agent_service": _REAL_AGENT_SERVICE,
    "services.docker_service": _REAL_DOCKER_SERVICE,
}

# Routers that import cleanly in the bare unit env (framework python). schedules
# needs apscheduler and is imported lazily inside its own test (skip if absent).
import routers.agent_config as agent_config  # noqa: E402
import routers.agent_files as agent_files  # noqa: E402
import routers.avatar as avatar  # noqa: E402
import routers.chat as chat  # noqa: E402
import routers.event_subscriptions as event_subscriptions  # noqa: E402
import routers.loops as loops  # noqa: E402
import routers.messages as messages  # noqa: E402
import routers.nevermined as nevermined  # noqa: E402
import routers.notifications as notifications  # noqa: E402
import routers.ops as ops  # noqa: E402
import routers.public as public  # noqa: E402
import routers.public_memory as public_memory  # noqa: E402
import routers.settings as settings  # noqa: E402
import routers.slack as slack  # noqa: E402
import routers.subscriptions as subscriptions  # noqa: E402
import routers.system_agent as system_agent  # noqa: E402
import routers.voice as voice  # noqa: E402

from models import (  # noqa: E402
    AgentCapacityUpdate,
    PublicChannelModelUpdate,
    ShareFileMcpRequest,
    VoiceStopRequest,
    WriteUserMemoryRequest,
)
from db_models import EventSubscriptionUpdate  # noqa: E402


# --- principals + seeding ----------------------------------------------------
_OWNER_ID, _STRANGER_ID, _ADMIN_ID = 1, 2, 3
_OWNER, _STRANGER, _ADMIN = "owner-a", "stranger-b", "admin-c"
_AGENT = "agent-a"
_MISSING = "does-not-exist-xyz"


@pytest.fixture(autouse=True)
def _restore_sys_modules(monkeypatch):
    """Re-pin the genuine service modules for the duration of every test so a
    sibling unit file's persistent sys.modules stub (notably the fake
    ``services.agent_service`` package ``test_inject_assigned_credentials``
    installs at import) can't leak into the handler bodies this suite drives.
    ``monkeypatch.setitem`` auto-restores the sibling's stub afterward, so this
    file stays a well-behaved sys.modules citizen. Named ``_restore_sys_modules``
    (paired with ``_STUBBED_MODULE_NAMES`` above) so the sys.modules lint
    recognizes the snapshot/restore helper (`tests/lint_sys_modules.py`)."""
    for _name in _STUBBED_MODULE_NAMES:
        monkeypatch.setitem(sys.modules, _name, _REAL_MODULES[_name])


@pytest.fixture
def seeded(db_backend):
    """Two regular users, an admin, and one agent owned by user A."""
    seed_user(_OWNER_ID, _OWNER, "user")
    seed_user(_STRANGER_ID, _STRANGER, "user")
    seed_user(_ADMIN_ID, _ADMIN, "admin")
    seed_agent(_AGENT, owner_id=_OWNER_ID)
    return db_backend


def _user(username: str, role: str = "user", *, uid: int = 0, agent_name=None, connector_agent=None, mcp_scope=None):
    from models import User

    return User(id=uid, username=username, role=role, agent_name=agent_name, connector_agent=connector_agent)


def _stranger():
    return _user(_STRANGER, uid=_STRANGER_ID)


def _owner():
    return _user(_OWNER, uid=_OWNER_ID)


def _admin():
    return _user(_ADMIN, role="admin", uid=_ADMIN_ID)


def _raised(coro_or_call):
    """Run an async handler / sync call and return the HTTPException it raised
    (or None if it returned without raising one)."""
    try:
        if asyncio.iscoroutine(coro_or_call):
            asyncio.run(coro_or_call)
        else:
            coro_or_call()
        return None
    except HTTPException as exc:
        return exc


def _assert_403(exc, detail):
    assert exc is not None, "expected an HTTPException, got a clean return"
    assert exc.status_code == 403, f"expected 403, got {exc.status_code}"
    assert exc.detail == detail, f"detail drift: {exc.detail!r} != {detail!r}"


# =============================================================================
# A1 — admin gates: a non-admin is denied with the exact admin-gate detail.
#     Imperative `require_admin(current_user)` and inline `role != "admin"`
#     raises alike. The four router-local require_admin dupes are exercised via
#     a representative handler each.
# =============================================================================

def _invoke_admin_site(mod_call):
    return _raised(mod_call())


_ADMIN_SITES = [
    # (id, invoker returning a coroutine, expected detail)
    ("avatar.generate_default_avatars",
     lambda u: avatar.generate_default_avatars(current_user=u),
     "Only admins can generate default avatars"),
    ("nevermined.get_settlement_failures",
     lambda u: nevermined.get_settlement_failures(current_user=u),
     "Admin access required"),
    ("subscriptions.get_encryption_status(local require_admin)",
     lambda u: subscriptions.get_encryption_status(current_user=u),
     "Admin access required"),
    ("ops.get_fleet_status(local require_admin)",
     lambda u: ops.get_fleet_status(request=None, current_user=u),
     "Admin access required"),
    ("settings.get_all_settings(local require_admin)",
     lambda u: settings.get_all_settings(request=None, current_user=u),
     "Admin access required"),
    ("system_agent.get_system_agent_status(local require_admin)",
     lambda u: system_agent.get_system_agent_status(request=None, current_user=u),
     "Admin access required"),
]


@pytest.mark.parametrize("site_id,invoke,detail", _ADMIN_SITES, ids=[s[0] for s in _ADMIN_SITES])
def test_admin_sites_deny_non_admin(seeded, site_id, invoke, detail):
    exc = _raised(invoke(_stranger()))
    _assert_403(exc, detail)


@pytest.mark.parametrize("site_id,invoke,detail", _ADMIN_SITES, ids=[s[0] for s in _ADMIN_SITES])
def test_admin_sites_admit_admin(seeded, site_id, invoke, detail):
    """An admin never trips the admin gate — it may fail later for another
    reason, but never with the admin-gate detail."""
    exc = _raised(invoke(_admin()))
    if exc is not None:
        assert exc.detail != detail, f"admin wrongly denied at {site_id}"


# =============================================================================
# A2 — access gates (assert_agent_access): stranger → 403 + exact detail.
# =============================================================================

def _stub_notification(monkeypatch, agent_name=_AGENT):
    monkeypatch.setattr(
        notifications.db, "get_notification",
        lambda _id: SimpleNamespace(agent_name=agent_name), raising=False,
    )


def _access_sites(monkeypatch):
    """(id, invoker(user), detail) for every assert_agent_access target."""
    _stub_notification(monkeypatch)
    return [
        ("agent_config.get_agent_resources",
         lambda u: agent_config.get_agent_resources(agent_name=_AGENT, current_user=u),
         "Access denied"),
        ("agent_config.get_agent_capacity",
         lambda u: agent_config.get_agent_capacity(agent_name=_AGENT, current_user=u),
         "Access denied"),
        ("avatar.get_avatar_identity",
         lambda u: avatar.get_avatar_identity(agent_name=_AGENT, current_user=u),
         "Access denied"),
        ("public_memory.write_user_memory",
         lambda u: public_memory.write_user_memory(
             agent_name=_AGENT,
             body=WriteUserMemoryRequest(execution_id="e1", memory_text="m"),
             current_user=u),
         "Not authorized"),
        ("subscriptions.get_agent_auth_status",
         lambda u: subscriptions.get_agent_auth_status(agent_name=_AGENT, current_user=u),
         "Access denied to this agent"),
        ("notifications.get_notification",
         lambda u: notifications.get_notification(notification_id="n1", current_user=u),
         "Access denied"),
        ("notifications.acknowledge_notification",
         lambda u: notifications.acknowledge_notification(notification_id="n1", current_user=u),
         "Access denied"),
        ("notifications.dismiss_notification",
         lambda u: notifications.dismiss_notification(notification_id="n1", current_user=u),
         "Access denied"),
    ]


def test_access_sites_deny_stranger(seeded, monkeypatch):
    for site_id, invoke, detail in _access_sites(monkeypatch):
        exc = _raised(invoke(_stranger()))
        _assert_403(exc, detail), site_id
        assert exc.detail == detail, f"{site_id}: {exc.detail!r} != {detail!r}"


def test_access_sites_admit_owner(seeded, monkeypatch):
    """The owner clears every access gate (may fail later for a non-auth reason,
    never with the access-gate detail)."""
    for site_id, invoke, detail in _access_sites(monkeypatch):
        exc = _raised(invoke(_owner()))
        if exc is not None:
            assert exc.detail != detail, f"owner wrongly denied at {site_id}"


# =============================================================================
# A3 — owner gates (assert_agent_owner): stranger → 403 + exact detail.
# =============================================================================

def _stub_event_sub(monkeypatch, agent_name=_AGENT):
    monkeypatch.setattr(
        event_subscriptions.db, "get_event_subscription",
        lambda _id: SimpleNamespace(subscriber_agent=agent_name), raising=False,
    )


def _owner_sites(monkeypatch):
    _stub_event_sub(monkeypatch)
    return [
        ("agent_config.set_agent_resources",
         lambda u: agent_config.set_agent_resources(
             agent_name=_AGENT, body={}, request=None, current_user=u),
         "Only owners can change resource limits"),
        ("agent_config.set_agent_capabilities",
         lambda u: agent_config.set_agent_capabilities(
             agent_name=_AGENT, body={}, current_user=u),
         "Only owners can change capabilities"),
        ("agent_config.set_agent_capacity",
         lambda u: agent_config.set_agent_capacity(
             agent_name=_AGENT, body=AgentCapacityUpdate(max_parallel_tasks=1), current_user=u),
         "Only owners can change capacity settings"),
        ("agent_config.set_agent_timeout",
         lambda u: agent_config.set_agent_timeout(
             agent_name=_AGENT, body={}, current_user=u),
         "Only owners can change timeout settings"),
        ("agent_config.set_public_channel_model",
         lambda u: agent_config.set_public_channel_model(
             agent_name=_AGENT, body=PublicChannelModelUpdate(model=None),
             request=None, current_user=u),
         "Only owners can change the public-channel model"),
        ("agent_config.set_agent_guardrails",
         lambda u: agent_config.set_agent_guardrails(
             agent_name=_AGENT, body={}, current_user=u),
         "Only agent owners can change guardrails settings"),
        ("agent_files.share_agent_file",
         lambda u: agent_files.share_agent_file(
             agent_name=_AGENT,
             body=ShareFileMcpRequest(filename="f.txt"), current_user=u),
         "Only the owner or admin can share files from this agent."),
        ("agent_files.list_agent_shared_files",
         lambda u: agent_files.list_agent_shared_files(agent_name=_AGENT, current_user=u),
         "Only the owner or admin can view shared files"),
        ("agent_files.revoke_agent_shared_file",
         lambda u: agent_files.revoke_agent_shared_file(
             agent_name=_AGENT, file_id="f1", current_user=u),
         "Only the owner can revoke shares"),
        ("subscriptions.assign_subscription_to_agent",
         lambda u: subscriptions.assign_subscription_to_agent(
             agent_name=_AGENT, subscription_name="s", current_user=u),
         "Only the agent owner or an admin can manage subscriptions"),
        ("subscriptions.clear_agent_subscription",
         lambda u: subscriptions.clear_agent_subscription(agent_name=_AGENT, current_user=u),
         "Only the agent owner or an admin can manage subscriptions"),
        ("messages.get_proactive_shares",
         lambda u: messages.get_proactive_shares(agent_name=_AGENT, current_user=u),
         "Not authorized to view shares"),
        ("event_subscriptions.update_event_subscription",
         lambda u: event_subscriptions.update_event_subscription(
             subscription_id="es1", data=EventSubscriptionUpdate(), current_user=u),
         "Only the owner can modify event subscriptions"),
        ("event_subscriptions.delete_event_subscription",
         lambda u: event_subscriptions.delete_event_subscription(
             subscription_id="es1", current_user=u),
         "Only the owner can delete event subscriptions"),
    ]


def test_owner_sites_deny_stranger(seeded, monkeypatch):
    for site_id, invoke, detail in _owner_sites(monkeypatch):
        exc = _raised(invoke(_stranger()))
        assert exc is not None, f"{site_id}: expected 403, got clean return"
        assert exc.status_code == 403, f"{site_id}: status {exc.status_code}"
        assert exc.detail == detail, f"{site_id}: {exc.detail!r} != {detail!r}"


def test_owner_sites_admit_owner(seeded, monkeypatch):
    for site_id, invoke, detail in _owner_sites(monkeypatch):
        exc = _raised(invoke(_owner()))
        if exc is not None:
            assert exc.detail != detail, f"owner wrongly denied at {site_id}"


def test_owner_sites_deny_shared_reader(seeded, monkeypatch):
    """A shared (non-owner) reader is denied at every owner gate — owner-only is
    stricter than access, and the refactor must preserve that."""
    # Give the stranger a share row so can_user_access is True but share is False.
    run("UPDATE users SET email = :e WHERE id = :id", e="stranger@example.com", id=_STRANGER_ID)
    run(
        "INSERT INTO agent_sharing (agent_name, shared_with_email, shared_by_id, created_at) "
        "VALUES (:a, :e, :o, :n)",
        a=_AGENT, e="stranger@example.com", o=_OWNER_ID, n="2026-01-01T00:00:00Z",
    )
    shared = _stranger()
    for site_id, invoke, detail in _owner_sites(monkeypatch):
        exc = _raised(invoke(shared))
        assert exc is not None and exc.status_code == 403, f"{site_id}: shared reader not denied"
        assert exc.detail == detail, f"{site_id}: {exc.detail!r} != {detail!r}"


# =============================================================================
# A4 — Shape-F (assert_owns_or_admin): strict-self-OR-admin.
#     Locks: stranger → 403 owner-gate detail; admin ADMITTED (bypass preserved);
#     anti-IDOR binding (session of a DIFFERENT agent → the binding 403).
# =============================================================================

def _stub_voice_session(monkeypatch, agent_name=_AGENT, user_id=_OWNER_ID):
    async def _get(_sid):
        # panel_state present so the admin/owner admitted path returns cleanly.
        return SimpleNamespace(agent_name=agent_name, user_id=user_id, panel_state={})
    monkeypatch.setattr(voice.voice_service, "get_session", _get, raising=False)


def _session_stub(agent_name, user_id):
    # model_dump present so an admitted principal falls through to a clean return.
    return SimpleNamespace(agent_name=agent_name, user_id=user_id, model_dump=lambda: {})


def _stub_chat_session(monkeypatch, mod, agent_name=_AGENT, user_id=_OWNER_ID):
    monkeypatch.setattr(mod, "get_agent_container", lambda name: object(), raising=False)
    monkeypatch.setattr(
        mod.db, "get_chat_session",
        lambda _sid: _session_stub(agent_name, user_id), raising=False,
    )
    monkeypatch.setattr(mod.db, "get_chat_messages", lambda _sid, limit=100: [], raising=False)
    monkeypatch.setattr(mod.db, "close_chat_session", lambda _sid: True, raising=False)


_VOICE_DETAIL = "Not authorized for this voice session"
_CHAT_DETAIL = "You don't have access to this session"


def test_voice_stop_shape_f(seeded, monkeypatch):
    _stub_voice_session(monkeypatch)
    # stranger (not owner, not admin) → owner-gate 403
    exc = _raised(voice.voice_stop(
        request=VoiceStopRequest(voice_session_id="v1"), name=_AGENT, current_user=_stranger()))
    _assert_403(exc, _VOICE_DETAIL)
    # admin (non-owner) ADMITTED — never the owner-gate detail
    exc = _raised(voice.voice_stop(
        request=VoiceStopRequest(voice_session_id="v1"), name=_AGENT, current_user=_admin()))
    if exc is not None:
        assert exc.detail != _VOICE_DETAIL, "admin wrongly denied at voice_stop owner gate"


def test_voice_panel_shape_f(seeded, monkeypatch):
    _stub_voice_session(monkeypatch)
    exc = _raised(voice.get_voice_panel(session_id="v1", name=_AGENT, current_user=_stranger()))
    _assert_403(exc, _VOICE_DETAIL)
    exc = _raised(voice.get_voice_panel(session_id="v1", name=_AGENT, current_user=_admin()))
    if exc is not None:
        assert exc.detail != _VOICE_DETAIL


def test_voice_anti_idor_binding(seeded, monkeypatch):
    """Owner of a session whose agent != path name is refused with the binding
    403 (cross-agent session read via a known 128-bit id) — must survive."""
    _stub_voice_session(monkeypatch, agent_name="other-agent")
    exc = _raised(voice.voice_stop(
        request=VoiceStopRequest(voice_session_id="v1"), name=_AGENT, current_user=_owner()))
    _assert_403(exc, "Voice session does not belong to this agent")
    exc = _raised(voice.get_voice_panel(session_id="v1", name=_AGENT, current_user=_owner()))
    _assert_403(exc, "Session does not belong to this agent")


def test_chat_session_detail_shape_f(seeded, monkeypatch):
    _stub_chat_session(monkeypatch, chat)
    exc = _raised(chat.get_chat_session_detail(session_id="c1", name=_AGENT, current_user=_stranger()))
    _assert_403(exc, _CHAT_DETAIL)
    # admin ADMITTED — returns cleanly (empty messages), no owner-gate 403
    exc = _raised(chat.get_chat_session_detail(session_id="c1", name=_AGENT, current_user=_admin()))
    assert exc is None, f"admin wrongly denied: {exc.detail if exc else ''}"


def test_chat_close_session_shape_f(seeded, monkeypatch):
    _stub_chat_session(monkeypatch, chat)
    exc = _raised(chat.close_chat_session(session_id="c1", name=_AGENT, current_user=_stranger()))
    _assert_403(exc, _CHAT_DETAIL)
    exc = _raised(chat.close_chat_session(session_id="c1", name=_AGENT, current_user=_admin()))
    assert exc is None, f"admin wrongly denied: {exc.detail if exc else ''}"


def test_chat_anti_idor_binding(seeded, monkeypatch):
    _stub_chat_session(monkeypatch, chat, agent_name="other-agent")
    exc = _raised(chat.get_chat_session_detail(session_id="c1", name=_AGENT, current_user=_owner()))
    _assert_403(exc, "Session does not belong to this agent")
    exc = _raised(chat.close_chat_session(session_id="c1", name=_AGENT, current_user=_owner()))
    _assert_403(exc, "Session does not belong to this agent")


# =============================================================================
# A5 — public.py strict-self (assert_owns, NO admin bypass). The critical
#     access-WIDENING guard: an admin who is NOT the session owner must STILL
#     get 403 (mapping this to assert_owns_or_admin would flip it to 200).
# =============================================================================

def _stub_public_link_session(monkeypatch, link_agent=_AGENT, session_agent=None, user_id=_OWNER_ID):
    session_agent = session_agent or link_agent
    monkeypatch.setattr(public, "_validate_public_link", lambda token: {"agent_name": link_agent}, raising=False)
    monkeypatch.setattr(
        public.db, "get_chat_session",
        lambda _sid: _session_stub(session_agent, user_id), raising=False,
    )
    monkeypatch.setattr(public.db, "get_chat_messages", lambda _sid, limit=100: [], raising=False)


def test_public_link_session_denies_stranger(seeded, monkeypatch):
    _stub_public_link_session(monkeypatch)
    exc = _raised(public.get_public_link_session_detail(
        token="t", session_id="c1", current_user=_stranger()))
    _assert_403(exc, "You don't have access to this session")


def test_public_link_session_denies_admin_non_owner(seeded, monkeypatch):
    """WIDENING GUARD: strict-self, no admin bypass. An admin who is not the
    session owner is 403 here — this fails if the site is (wrongly) mapped to
    assert_owns_or_admin."""
    _stub_public_link_session(monkeypatch, user_id=_OWNER_ID)
    exc = _raised(public.get_public_link_session_detail(
        token="t", session_id="c1", current_user=_admin()))
    _assert_403(exc, "You don't have access to this session")


def test_public_link_session_admits_owner(seeded, monkeypatch):
    _stub_public_link_session(monkeypatch, user_id=_OWNER_ID)
    exc = _raised(public.get_public_link_session_detail(
        token="t", session_id="c1", current_user=_owner()))
    assert exc is None, f"owner wrongly denied: {exc.detail if exc else ''}"


def test_public_link_anti_idor_binding(seeded, monkeypatch):
    # Link resolves to _AGENT but the session belongs to a different agent →
    # the binding 403 fires before the owner check.
    _stub_public_link_session(monkeypatch, link_agent=_AGENT, session_agent="other-agent")
    exc = _raised(public.get_public_link_session_detail(
        token="t", session_id="c1", current_user=_owner()))
    _assert_403(exc, "Session does not belong to this agent")


# =============================================================================
# A6 — anti-exfil sibling (agent_files.share_agent_file). Agent B's own scoped
#     key (same owner) passes the owner gate but the sibling check refuses it
#     sharing Agent A's files. The refactor migrates only the owner line.
# =============================================================================

def test_agent_files_anti_exfil_sibling(seeded):
    # An agent-scoped key of sibling "agent-b", resolving to the shared owner.
    sibling_key = _user(_OWNER, uid=_OWNER_ID, agent_name="agent-b")
    exc = _raised(agent_files.share_agent_file(
        agent_name=_AGENT, body=ShareFileMcpRequest(filename="f.txt"), current_user=sibling_key))
    _assert_403(exc, "Agent-scoped MCP key cannot share files for a different agent.")


# =============================================================================
# A7 — schedules ordering (#1445): access-403 is evaluated BEFORE the
#     is_agent_live 404, so a low-priv caller on a nonexistent agent gets 403
#     (not a 404 existence oracle).
# =============================================================================

def test_schedules_create_access_before_existence_404(seeded):
    try:
        import routers.schedules as schedules  # noqa: PLC0415
        from db_models import ScheduleCreate  # noqa: PLC0415
    except ImportError:  # pragma: no cover - apscheduler-less env
        pytest.skip("apscheduler required to import routers.schedules")

    payload = ScheduleCreate(name="s", cron_expression="0 0 * * *", message="hi")
    exc = _raised(schedules.create_schedule(
        name=_MISSING, schedule_data=payload, current_user=_stranger()))
    assert exc is not None and exc.status_code == 403, (
        f"expected access-403 before the is_agent_live 404, got "
        f"{exc.status_code if exc else 'clean'}"
    )
    assert exc.detail == "Access denied"


def test_schedules_scheduler_status_admin_gate(seeded):
    try:
        import routers.schedules as schedules  # noqa: PLC0415
    except ImportError:  # pragma: no cover
        pytest.skip("apscheduler required to import routers.schedules")
    exc = _raised(schedules.get_scheduler_status(current_user=_stranger()))
    _assert_403(exc, "Admin access required")


# =============================================================================
# A8 — loops composite gate (_check_loop_access): admin + initiator ADMITTED,
#     otherwise falls back to the agent access check → 403 "Access denied".
# =============================================================================

def _loop(owner_uid=_OWNER_ID):
    return {"agent_name": _AGENT, "started_by_user_id": 999}


def test_loop_access_admin_admitted(seeded):
    assert loops._check_loop_access(_loop(), _admin()) is None


def test_loop_access_initiator_admitted(seeded):
    loop = {"agent_name": _AGENT, "started_by_user_id": _STRANGER_ID}
    assert loops._check_loop_access(loop, _stranger()) is None


def test_loop_access_fallback_denies_stranger(seeded):
    exc = _raised(lambda: loops._check_loop_access(_loop(), _stranger()))
    _assert_403(exc, "Access denied")


def test_loop_access_fallback_admits_owner(seeded):
    # Not the initiator, not admin → falls through to the real agent access check.
    assert loops._check_loop_access(_loop(), _owner()) is None


# =============================================================================
# Part B — helper-level truth tables for the five dependencies.py helpers
#          (added WITH the helpers; locks the contract the migrated sites rely
#          on: connector fence, owner-or-admin, strict-self no-admin, type parity).
# =============================================================================

def _connector(agent="bound-agent"):
    # A connector-scoped principal resolves to a real user but is consumption-only.
    return _user(_OWNER, uid=_OWNER_ID, connector_agent=agent)


def test_assert_admin_contract(seeded):
    import dependencies as dep
    assert dep.assert_admin(_admin()) is None
    exc = _raised(lambda: dep.assert_admin(_stranger()))
    _assert_403(exc, "Admin access required")
    # connector principal is rejected before the role check (consumption-only).
    exc = _raised(lambda: dep.assert_admin(_connector()))
    _assert_403(exc, "Connector keys are consumption-only and cannot perform this operation")
    # custom detail threads through.
    exc = _raised(lambda: dep.assert_admin(_stranger(), detail="nope"))
    _assert_403(exc, "nope")


def test_assert_agent_access_contract(seeded):
    import dependencies as dep
    assert dep.assert_agent_access(_owner(), _AGENT) is None
    exc = _raised(lambda: dep.assert_agent_access(_stranger(), _AGENT))
    _assert_403(exc, "Access denied")
    # connector fence: a connector key scoped to a DIFFERENT agent → 403 before db.
    exc = _raised(lambda: dep.assert_agent_access(_connector("other"), _AGENT))
    _assert_403(exc, "Connector key is scoped to a different agent")


def test_assert_agent_owner_contract(seeded):
    import dependencies as dep
    assert dep.assert_agent_owner(_owner(), _AGENT) is None
    exc = _raised(lambda: dep.assert_agent_owner(_stranger(), _AGENT))
    _assert_403(exc, "Not authorized")  # default detail
    # connector owner-op fence fires regardless of the bound agent.
    exc = _raised(lambda: dep.assert_agent_owner(_connector(_AGENT), _AGENT))
    _assert_403(exc, "Connector keys are consumption-only and cannot perform owner operations")


def test_assert_owns_or_admin_truth_table(seeded):
    import dependencies as dep
    owner = _user("u", uid=7)
    admin = _user("a", role="admin", uid=9)
    other = _user("o", uid=11)
    assert dep.assert_owns_or_admin(owner, 7) is None       # owner (non-admin) → allow
    assert dep.assert_owns_or_admin(admin, 7) is None       # admin (non-owner) → allow
    exc = _raised(lambda: dep.assert_owns_or_admin(other, 7))  # neither → deny
    _assert_403(exc, "Not authorized")
    # owner_id type parity: an int owner_id vs an int User.id compares by value.
    assert dep.assert_owns_or_admin(_user("u", uid=42), 42) is None


def test_assert_owns_no_admin_bypass(seeded):
    import dependencies as dep
    owner = _user("u", uid=7)
    admin = _user("a", role="admin", uid=9)
    assert dep.assert_owns(owner, 7) is None                # owner → allow
    # NO admin bypass — an admin who is not the owner is DENIED (widening guard).
    exc = _raised(lambda: dep.assert_owns(admin, 7))
    _assert_403(exc, "You don't have access to this session")
    exc = _raised(lambda: dep.assert_owns(_user("o", uid=11), 7))
    _assert_403(exc, "You don't have access to this session")


# =============================================================================
# A9 — slack.py inline auth gates, migrated onto the shared imperative helpers
#      (#1710, retiring the `# noqa: inv8` carve-out). 3 read gates
#      (assert_agent_access) + 8 owner gates (assert_agent_owner); every detail=
#      string preserved verbatim so the 403 body is byte-identical (AC #6). The
#      handlers are access-first, so each gate raises BEFORE any downstream
#      public-link / workspace / channel lookup — no stubs needed for a denial.
#      Locks: stranger → 403 + exact detail at all 11; a shared (non-owner)
#      reader is ADMITTED on the 3 read gates and DENIED on all 8 owner gates
#      (a mis-classified helper is invisible to the AST wiring guard); owner /
#      admin are never denied by the migrated gate; and site #10's ent#223
#      human-only reject survives while site #11 stays agent-callable
#      (the grant-vs-send asymmetry).
# =============================================================================

_SLACK_LINK = "lk-xyz"
_SLACK_CHANNEL = "C-123"


def _slack_read_sites():
    """(id, invoker(user), detail) — the 3 assert_agent_access gates."""
    return [
        ("slack.get_slack_connection_status",
         lambda u: slack.get_slack_connection_status(
             name=_AGENT, link_id=_SLACK_LINK, current_user=u),
         "Access denied"),
        ("slack.get_agent_slack_channel",
         lambda u: slack.get_agent_slack_channel(name=_AGENT, current_user=u),
         "Access denied"),
        ("slack.list_agent_slack_channels",
         lambda u: slack.list_agent_slack_channels(name=_AGENT, current_user=u),
         "Access denied"),
    ]


def _slack_owner_sites():
    """(id, invoker(user), detail) — the 8 assert_agent_owner gates."""
    from models import SlackChannelMessageRequest, SlackChannelProactiveRequest  # noqa: PLC0415

    return [
        ("slack.connect_slack",
         lambda u: slack.connect_slack(name=_AGENT, link_id=_SLACK_LINK, current_user=u),
         "Only owners can connect Slack"),
        ("slack.disconnect_slack",
         lambda u: slack.disconnect_slack(name=_AGENT, link_id=_SLACK_LINK, current_user=u),
         "Only owners can disconnect Slack"),
        ("slack.update_slack_connection",
         lambda u: slack.update_slack_connection(
             name=_AGENT, link_id=_SLACK_LINK, enabled=True, current_user=u),
         "Only owners can modify Slack settings"),
        ("slack.create_agent_slack_channel",
         lambda u: slack.create_agent_slack_channel(name=_AGENT, current_user=u),
         "Only owners can manage Slack channels"),
        ("slack.delete_agent_slack_channel",
         lambda u: slack.delete_agent_slack_channel(name=_AGENT, current_user=u),
         "Only owners can manage Slack channels"),
        ("slack.set_agent_as_slack_dm_default",
         lambda u: slack.set_agent_as_slack_dm_default(name=_AGENT, current_user=u),
         "Only owners can manage Slack channels"),
        ("slack.set_slack_channel_proactive",
         lambda u: slack.set_slack_channel_proactive(
             name=_AGENT, channel_id=_SLACK_CHANNEL,
             request=SlackChannelProactiveRequest(allow_proactive=True), current_user=u),
         "Only owners can change channel settings"),
        ("slack.send_agent_slack_channel_message",
         lambda u: slack.send_agent_slack_channel_message(
             name=_AGENT, channel_id=_SLACK_CHANNEL,
             request=SlackChannelMessageRequest(message="hello"), current_user=u),
         "Only owners can send channel messages"),
    ]


_HUMAN_ONLY = "This operation is human-only; agent-scoped keys cannot perform it"


def _grant_share(email="stranger@example.com"):
    """Give the stranger a share row → can_user_access True, can_user_share False."""
    run("UPDATE users SET email = :e WHERE id = :id", e=email, id=_STRANGER_ID)
    run(
        "INSERT INTO agent_sharing (agent_name, shared_with_email, shared_by_id, created_at) "
        "VALUES (:a, :e, :o, :n)",
        a=_AGENT, e=email, o=_OWNER_ID, n="2026-01-01T00:00:00Z",
    )


def test_slack_read_sites_deny_stranger(seeded):
    for site_id, invoke, detail in _slack_read_sites():
        exc = _raised(invoke(_stranger()))
        assert exc is not None and exc.status_code == 403, f"{site_id}: expected 403"
        assert exc.detail == detail, f"{site_id}: {exc.detail!r} != {detail!r}"


def test_slack_owner_sites_deny_stranger(seeded):
    for site_id, invoke, detail in _slack_owner_sites():
        exc = _raised(invoke(_stranger()))
        assert exc is not None and exc.status_code == 403, f"{site_id}: expected 403"
        assert exc.detail == detail, f"{site_id}: {exc.detail!r} != {detail!r}"


def test_slack_sites_admit_owner(seeded):
    """Owner clears every migrated gate (may fail later for a non-auth reason,
    never with the gate detail)."""
    for site_id, invoke, detail in _slack_read_sites() + _slack_owner_sites():
        exc = _raised(invoke(_owner()))
        if exc is not None:
            assert exc.detail != detail, f"owner wrongly denied at {site_id}"


def test_slack_owner_sites_admit_admin(seeded):
    """Admin (non-owner) clears the owner gates via can_user_share_agent's admin
    short-circuit — never the owner-gate detail."""
    for site_id, invoke, detail in _slack_owner_sites():
        exc = _raised(invoke(_admin()))
        if exc is not None:
            assert exc.detail != detail, f"admin wrongly denied at {site_id}"


def test_slack_shared_reader_read_vs_owner_matrix(seeded):
    """The core adversarial case: a shared (non-owner) reader is ADMITTED on the
    3 read gates but DENIED at all 8 owner gates. A read gate wrongly wired to
    assert_agent_owner (or an owner gate wired to assert_agent_access) is
    invisible to the AST wiring guard — only this matrix catches it."""
    _grant_share()
    shared = _stranger()  # has access via the share row; is NOT the owner
    for site_id, invoke, detail in _slack_read_sites():
        exc = _raised(invoke(shared))
        if exc is not None:
            assert exc.detail != detail, f"shared reader wrongly denied at read gate {site_id}"
    for site_id, invoke, detail in _slack_owner_sites():
        exc = _raised(invoke(shared))
        assert exc is not None and exc.status_code == 403, (
            f"{site_id}: shared reader not denied at owner gate"
        )
        assert exc.detail == detail, f"{site_id}: {exc.detail!r} != {detail!r}"


def test_slack_proactive_toggle_rejects_agent_principal(seeded):
    """Site #10 keeps its ent#223 human-only guard through the migration: an
    agent-scoped principal (resolving to the owner, carrying the owner role) is
    refused with the reject_agent_principal detail — self-granting proactive
    consent is a human decision. assert_agent_owner alone is agent-permissive,
    so the standalone reject_agent_principal line is what enforces this."""
    from models import SlackChannelProactiveRequest  # noqa: PLC0415

    agent_key = _user(_OWNER, uid=_OWNER_ID, agent_name=_AGENT)  # the agent's own key
    exc = _raised(slack.set_slack_channel_proactive(
        name=_AGENT, channel_id=_SLACK_CHANNEL,
        request=SlackChannelProactiveRequest(allow_proactive=True), current_user=agent_key))
    _assert_403(exc, _HUMAN_ONLY)


def test_slack_send_message_allows_agent_principal(seeded):
    """Site #11 asymmetry (ent#223): SENDING under consent stays agent-callable.
    An agent-scoped principal is NOT rejected by any human-only gate — it clears
    the owner gate as its owner and fails later for a non-auth reason, never with
    the human-only detail (adding a reject here would break AC #6)."""
    from models import SlackChannelMessageRequest  # noqa: PLC0415

    agent_key = _user(_OWNER, uid=_OWNER_ID, agent_name=_AGENT)
    exc = _raised(slack.send_agent_slack_channel_message(
        name=_AGENT, channel_id=_SLACK_CHANNEL,
        request=SlackChannelMessageRequest(message="hi"), current_user=agent_key))
    if exc is not None:
        assert exc.detail != _HUMAN_ONLY, (
            "site #11 wrongly rejected an agent principal (breaks the ent#223 "
            "grant/send asymmetry)"
        )
