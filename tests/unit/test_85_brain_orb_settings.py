"""Unit tests for admin-configurable Brain Orb flags (trinity-enterprise#85).

Covers the three layers the change touches:

  * resolver     — services/settings_service `_resolve_bool_flag` order
                   (stored wins both directions → env opt-in → OFF), tolerant
                   parse, and the load-bearing fail-open on a DB read failure
  * admin routes — GET/PUT /api/settings/brain-orb (payload shape, per-flag
                   source, partial update, `clear` revert-to-env, validation,
                   403 non-admin, audit old→new, /{key} route ordering)
  * consumers    — the brain-orb route gate honors a REAL DB flip without a
                   restart, and GET /api/settings/feature-flags reflects the
                   resolved composition (base ∧ voice ∧ key)

True unit tests: no Docker, no running backend. The gate-seam behavior of every
individual brain-orb route is covered by test_brain_orb.py (which patches the
resolver names); this file owns the DB-backed resolution itself.
"""
from __future__ import annotations

import types
from unittest.mock import AsyncMock, patch

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

import config
import routers.agent_brain_orb as bo
import routers.settings as sr
from database import db
from dependencies import (
    get_authorized_agent_by_name,
    get_current_user,
    get_owned_agent_by_name,
)
from services.settings_service import BRAIN_ORB_FLAGS, settings_service

_URL = "/api/settings/brain-orb"
_ENV_VARS = tuple(env for _key, env in BRAIN_ORB_FLAGS.values())
_SETTING_KEYS = tuple(key for key, _env in BRAIN_ORB_FLAGS.values())


def _delete_keys():
    for key in _SETTING_KEYS:
        db.delete_setting(key)


@pytest.fixture(autouse=True)
def _isolate_brain_orb_state(monkeypatch):
    """Unit tests share one per-process SQLite (conftest pins TRINITY_DB_PATH),
    so stored flags MUST be cleaned in a finalizer — inline cleanup would skip
    on assertion failure and leak state into every later test in the run."""
    for env in _ENV_VARS:
        monkeypatch.delenv(env, raising=False)
    _delete_keys()
    yield
    _delete_keys()


def _user(role):
    return types.SimpleNamespace(
        id=1, username=role, role=role, email=f"{role}@example.com"
    )


@pytest.fixture
def admin_client():
    app = FastAPI()
    app.include_router(sr.router)
    app.dependency_overrides[get_current_user] = lambda: _user("admin")
    return TestClient(app, raise_server_exceptions=True)


@pytest.fixture
def user_client():
    app = FastAPI()
    app.include_router(sr.router)
    app.dependency_overrides[get_current_user] = lambda: _user("user")
    return TestClient(app, raise_server_exceptions=True)


# --- resolver: stored → env opt-in → default ---------------------------------

def test_resolver_default_off():
    assert settings_service.is_brain_orb_enabled() is False
    assert settings_service.is_brain_orb_voice_enabled() is False
    assert settings_service.is_brain_orb_write_enabled() is False


def test_resolver_env_opt_in(monkeypatch):
    monkeypatch.setenv("BRAIN_ORB_ENABLED", "true")
    assert settings_service.is_brain_orb_enabled() is True
    # env is per-flag — siblings stay off
    assert settings_service.is_brain_orb_voice_enabled() is False


def test_resolver_stored_false_beats_env_true(monkeypatch):
    """The stored value wins in BOTH directions — an admin OFF overrides env ON."""
    monkeypatch.setenv("BRAIN_ORB_ENABLED", "true")
    db.set_setting("brain_orb_enabled", "false")
    assert settings_service.is_brain_orb_enabled() is False


def test_resolver_stored_true_without_env():
    db.set_setting("brain_orb_enabled", "true")
    assert settings_service.is_brain_orb_enabled() is True


def test_resolver_keys_are_independent():
    db.set_setting("brain_orb_voice_enabled", "true")
    assert settings_service.is_brain_orb_voice_enabled() is True
    assert settings_service.is_brain_orb_enabled() is False
    assert settings_service.is_brain_orb_write_enabled() is False


def test_resolver_junk_stored_value_is_off(monkeypatch):
    """A junk stored value parses to False (feature off, fail-safe) and does
    NOT fall through to the env leg — premise P4 (generic PUT stays usable)."""
    monkeypatch.setenv("BRAIN_ORB_ENABLED", "true")
    db.set_setting("brain_orb_enabled", "banana")
    assert settings_service.is_brain_orb_enabled() is False


def test_resolver_fail_open_falls_back_to_env(monkeypatch):
    """A settings-read failure must fall back to the env default, never raise —
    this is load-bearing for GET /api/settings/feature-flags (a 500 there
    zeroes EVERY flag in the frontend store)."""
    monkeypatch.setenv("BRAIN_ORB_ENABLED", "true")

    def boom(key, default=None):
        raise RuntimeError("db down")

    monkeypatch.setattr(settings_service, "get_setting", boom)
    assert settings_service.is_brain_orb_enabled() is True
    monkeypatch.delenv("BRAIN_ORB_ENABLED")
    assert settings_service.is_brain_orb_enabled() is False


# --- dedicated admin routes: GET ---------------------------------------------

def test_get_payload_shape_and_sources(admin_client, monkeypatch):
    """GET returns per-flag {value, source} — and is NOT captured by the /{key}
    catch-all (which would 404 'Setting brain-orb not found', Invariant #4)."""
    monkeypatch.setenv("BRAIN_ORB_VOICE_ENABLED", "true")
    db.set_setting("brain_orb_enabled", "true")

    r = admin_client.get(_URL)
    assert r.status_code == 200
    flags = r.json()["flags"]
    assert flags["enabled"] == {"value": True, "source": "override"}
    assert flags["voice_enabled"] == {"value": True, "source": "env"}
    assert flags["write_enabled"] == {"value": False, "source": "default"}
    assert isinstance(r.json()["gemini_key_configured"], bool)


def test_get_requires_admin(user_client):
    assert user_client.get(_URL).status_code == 403


# --- dedicated admin routes: PUT -----------------------------------------------

def test_put_partial_update_writes_only_that_key(admin_client):
    r = admin_client.put(_URL, json={"enabled": True})
    assert r.status_code == 200
    body = r.json()
    assert body["updated"] == ["enabled"]
    assert body["cleared"] == []
    assert body["flags"]["enabled"] == {"value": True, "source": "override"}
    assert db.get_setting_value("brain_orb_enabled", None) == "true"
    assert db.get_setting_value("brain_orb_voice_enabled", None) is None
    assert db.get_setting_value("brain_orb_write_enabled", None) is None


def test_put_clear_reverts_to_env(admin_client, monkeypatch):
    """`clear` deletes the stored override so the env var speaks again — the
    revert path both reviewers demanded (env is otherwise dead after a toggle)."""
    monkeypatch.setenv("BRAIN_ORB_VOICE_ENABLED", "true")
    db.set_setting("brain_orb_voice_enabled", "false")
    assert settings_service.is_brain_orb_voice_enabled() is False

    r = admin_client.put(_URL, json={"clear": ["voice_enabled"]})
    assert r.status_code == 200
    assert r.json()["cleared"] == ["voice_enabled"]
    assert r.json()["flags"]["voice_enabled"] == {"value": True, "source": "env"}
    assert settings_service.is_brain_orb_voice_enabled() is True
    assert db.get_setting_value("brain_orb_voice_enabled", None) is None


def test_put_set_and_clear_conflict_400(admin_client):
    r = admin_client.put(_URL, json={"enabled": True, "clear": ["enabled"]})
    assert r.status_code == 400
    assert "both set and cleared" in r.json()["detail"]


def test_put_unknown_clear_name_400(admin_client):
    r = admin_client.put(_URL, json={"clear": ["bogus"]})
    assert r.status_code == 400
    assert "bogus" in r.json()["detail"]


def test_put_noop_succeeds_without_audit(admin_client):
    with patch.object(sr.platform_audit_service, "log", AsyncMock()) as log:
        r = admin_client.put(_URL, json={})
    assert r.status_code == 200
    assert r.json()["updated"] == []
    assert r.json()["cleared"] == []
    assert log.await_count == 0


def test_put_requires_admin(user_client):
    assert user_client.put(_URL, json={"enabled": True}).status_code == 403


def test_put_audit_carries_per_flag_old_new(admin_client):
    """The write flag gates an exec-adjacent surface — every change must leave
    a per-flag old→new trace, not a bare 'update'."""
    with patch.object(sr.platform_audit_service, "log", AsyncMock()) as log:
        r = admin_client.put(_URL, json={"write_enabled": True})
    assert r.status_code == 200
    assert log.await_count == 1
    details = log.await_args.kwargs["details"]
    assert details["setting"] == "brain_orb_flags"
    assert details["changes"]["write_enabled"] == {"old": False, "new": True}


def test_registry_and_model_fields_stay_in_sync():
    """Drift guard: every BRAIN_ORB_FLAGS registry key must be a
    BrainOrbSettingsUpdate field — the PUT loop iterates the registry against
    the model, so a registry-only flag would otherwise surface at runtime."""
    from models import BrainOrbSettingsUpdate

    assert set(BRAIN_ORB_FLAGS) <= set(BrainOrbSettingsUpdate.model_fields)


# --- generic /{key} routes stay usable (premise P4) ---------------------------

def test_generic_put_junk_value_fails_safe(admin_client):
    r = admin_client.put("/api/settings/brain_orb_enabled", json={"value": "banana"})
    assert r.status_code == 200  # generic KV write succeeds...
    assert settings_service.is_brain_orb_enabled() is False  # ...and parses to OFF


def test_generic_delete_reverts_to_env(admin_client, monkeypatch):
    monkeypatch.setenv("BRAIN_ORB_ENABLED", "true")
    db.set_setting("brain_orb_enabled", "false")
    r = admin_client.delete("/api/settings/brain_orb_enabled")
    assert r.status_code == 200
    assert settings_service.is_brain_orb_enabled() is True


# --- consumers: route gate + feature-flags ------------------------------------

def _brain_orb_client():
    """Brain-orb router WITHOUT resolver patches — gates hit the real DB."""
    app = FastAPI()
    app.include_router(bo.router)
    app.dependency_overrides[get_authorized_agent_by_name] = lambda: "cornelius"
    app.dependency_overrides[get_owned_agent_by_name] = lambda: "cornelius"
    app.dependency_overrides[get_current_user] = lambda: _user("admin")
    return TestClient(app, raise_server_exceptions=True)


def test_route_gate_honors_db_flip_without_restart():
    """The acceptance criterion: flipping the stored setting changes the gate
    outcome at request time — no import-time constant, no restart."""
    client = _brain_orb_client()
    url = "/api/agents/cornelius/brain-orb/data"

    r = client.get(url)
    assert r.status_code == 404
    assert "not enabled" in r.json()["detail"]

    db.set_setting("brain_orb_enabled", "true")
    # Flag now passes; the request proceeds to container lookup (no Docker in
    # unit tests → "Agent not found", a DIFFERENT 404 proving the gate opened).
    with patch.object(bo, "get_agent_container", return_value=None):
        r = client.get(url)
    assert r.status_code == 404
    assert "Agent not found" in r.json()["detail"]

    db.set_setting("brain_orb_enabled", "false")
    r = client.get(url)
    assert "not enabled" in r.json()["detail"]


def test_feature_flags_reflect_resolved_values(admin_client, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")

    r = admin_client.get("/api/settings/feature-flags")
    assert r.status_code == 200
    assert r.json()["brain_orb_available"] is False

    db.set_setting("brain_orb_enabled", "true")
    db.set_setting("brain_orb_voice_enabled", "true")
    db.set_setting("brain_orb_write_enabled", "true")
    body = admin_client.get("/api/settings/feature-flags").json()
    assert body["brain_orb_available"] is True
    assert body["brain_orb_voice_available"] is True
    assert body["brain_orb_write_available"] is True


def test_feature_flags_voice_composes_with_base(admin_client, monkeypatch):
    """base stored-OFF ∧ voice env-ON ∧ key present ⇒ voice reads unavailable —
    the #85 composition fix (a downed orb must not advertise its voice tile)."""
    monkeypatch.setattr(config, "GEMINI_API_KEY", "test-key")
    monkeypatch.setenv("BRAIN_ORB_VOICE_ENABLED", "true")
    db.set_setting("brain_orb_enabled", "false")

    body = admin_client.get("/api/settings/feature-flags").json()
    assert body["brain_orb_available"] is False
    assert body["brain_orb_voice_available"] is False
    assert body["brain_orb_write_available"] is False


def test_feature_flags_survive_brain_orb_db_failure(admin_client, monkeypatch):
    """A brain-orb settings-read failure must NOT 500 feature-flags — the
    frontend store zeroes every flag on a non-200 (fail-open, load-bearing)."""
    real_get = settings_service.get_setting

    def flaky(key, default=None):
        if key.startswith("brain_orb"):
            raise RuntimeError("db down")
        return real_get(key, default)

    monkeypatch.setattr(settings_service, "get_setting", flaky)
    r = admin_client.get("/api/settings/feature-flags")
    assert r.status_code == 200
    assert r.json()["brain_orb_available"] is False
