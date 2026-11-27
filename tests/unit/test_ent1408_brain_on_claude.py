"""trinity-enterprise#1408 — the seeded Cornelius can run once the person connects Claude.

Seen on a dev self-serve instance (2026-11-27): the person connected Claude in
the first session, the subscription was offered for their own agent only, and
`/first-run` on Cornelius failed with "Authentication failure: Not logged in".

Two backend halves, both driven here at their own layer:

1. ``GET /api/onboarding/first-run`` names the company brain (``brain_agent``)
   when the install seeded it and the caller can see it, so the assistant panel
   can put it on the credential together with the person's own agent. Cornelius
   is part of the product (epic ent#1407), not one of the examples.
2. A run that fails for want of any Claude credential says what to do next, not
   only "Not logged in".
"""
from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

import services.onboarding_service as onboarding_service
import services.system_seed_service as sss
from models import FirstRunState, User

pytestmark = pytest.mark.unit

CORNELIUS = "cornelius"
MANIFEST = "name: acme\nagents:\n  scout:\n    template: local:scout\n"
ADMIN_ROW = {"id": 1, "username": "admin", "email": "admin@example.com", "role": "admin"}
NOT_LOGGED_IN = ("Authentication failure: Not logged in · Please run /login. "
                 "Check subscription token or API key configuration.")


@pytest.fixture()
def admin_user():
    return User(id=1, username="admin", email="admin@example.com", role="admin")


@pytest.fixture(autouse=True)
def bundled_manifest(monkeypatch):
    monkeypatch.delenv(sss.MANIFEST_ENV_VAR, raising=False)
    monkeypatch.setattr(sss.system_seed_service, "_resolve_manifest",
                        lambda override_raw: (MANIFEST, "bundled", None))


def _fleet(monkeypatch, names):
    fake = type("FakeDb", (), {})()
    fake.get_user_by_username = lambda username: ADMIN_ROW
    fake.get_all_agent_metadata = lambda email=None: {
        n: {"owner_id": 1, "is_system": False, "is_shared_with_user": False} for n in names}
    monkeypatch.setattr(onboarding_service, "db", fake)


# --- 1. the first-run state names the brain ----------------------------------

class TestFirstRunNamesTheBrain:
    def test_seeded_cornelius_is_the_brain(self, monkeypatch, admin_user):
        _fleet(monkeypatch, [CORNELIUS, "acme-scout", "my-agent"])
        state = onboarding_service.get_first_run_state(admin_user)
        assert state["brain_agent"] == CORNELIUS

    def test_the_route_model_keeps_it(self, monkeypatch, admin_user):
        """The route's response_model is FirstRunState: a field it does not
        declare is dropped on the way out, so the page would never see it."""
        _fleet(monkeypatch, [CORNELIUS, "acme-scout"])
        state = onboarding_service.get_first_run_state(admin_user)
        assert FirstRunState(**state).model_dump()["brain_agent"] == CORNELIUS

    def test_no_brain_when_cornelius_is_absent(self, monkeypatch, admin_user):
        _fleet(monkeypatch, ["acme-scout", "my-agent"])
        assert onboarding_service.get_first_run_state(admin_user)["brain_agent"] is None

    def test_an_example_is_never_the_brain(self, monkeypatch, admin_user):
        _fleet(monkeypatch, ["acme-scout"])
        state = onboarding_service.get_first_run_state(admin_user)
        assert state["seeded_agents"] == ["acme-scout"]
        assert state["brain_agent"] is None


# --- 2. a run with no credential says what to do next ------------------------

def _creds(monkeypatch, *, subscription_id=None, use_platform_key=True, instance_key=False):
    import services.subscription_service as subs

    fake = MagicMock()
    fake.get_agent_subscription_id.return_value = subscription_id
    fake.get_use_platform_api_key.return_value = use_platform_key
    monkeypatch.setattr(subs, "db", fake)
    monkeypatch.setattr(subs, "instance_has_api_key", lambda: instance_key)


class TestNoCredentialNextStep:
    def test_an_agent_on_no_credential_is_told_the_next_step(self, monkeypatch):
        from services.subscription_service import no_credential_next_step

        _creds(monkeypatch)
        hint = no_credential_next_step(CORNELIUS)
        assert hint is not None
        assert CORNELIUS in hint
        assert "ask the assistant" in hint
        assert "Settings → Integrations → Claude Subscriptions" in hint

    @pytest.mark.parametrize("kwargs", [
        {"subscription_id": "sub-1"},                         # on a subscription
        {"use_platform_key": True, "instance_key": True},     # on the instance's API key
        {"use_platform_key": False},                          # brings its own key: unseen here
    ])
    def test_an_agent_with_a_credential_gets_no_hint(self, monkeypatch, kwargs):
        from services.subscription_service import no_credential_next_step

        _creds(monkeypatch, **kwargs)
        assert no_credential_next_step(CORNELIUS) is None

    def test_an_unreadable_row_gets_no_hint(self, monkeypatch):
        import services.subscription_service as subs
        from services.subscription_service import no_credential_next_step

        _creds(monkeypatch)
        subs.db.get_agent_subscription_id.side_effect = RuntimeError("db down")
        assert no_credential_next_step(CORNELIUS) is None

    def _fail_auth(self, monkeypatch, **creds):
        """Drive execute_task through the real terminal-failure path with the
        agent's own 503 for a run that had no credential."""
        from test_792_subscription_retry import _resp, _run

        _creds(monkeypatch, **creds)
        with patch("services.subscription_auto_switch.handle_subscription_failure", AsyncMock()):
            result, ctx = _run(responses=[_resp(503, {"detail": NOT_LOGGED_IN})], switch_result=None)
        return result

    def test_the_failed_runs_error_carries_the_next_step(self, monkeypatch):
        from services.task_execution_service import TaskExecutionStatus

        result = self._fail_auth(monkeypatch)
        assert result.status == TaskExecutionStatus.FAILED
        assert result.error_code is not None and result.error_code.value == "auth"
        assert result.error.startswith("Authentication failure: Not logged in")
        assert "test-agent is not on a Claude credential" in result.error
        # The assistant's run card shows the first 300 characters of the error.
        assert "Claude Subscriptions" in result.error[:300]

    def test_a_run_on_a_subscription_keeps_its_error(self, monkeypatch):
        result = self._fail_auth(monkeypatch, subscription_id="sub-1")
        assert "not on a Claude credential" not in (result.error or "")
