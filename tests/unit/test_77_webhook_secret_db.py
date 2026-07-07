"""DB-layer tests for schedule webhook signature secrets (trinity-enterprise#77).

Covers the `db/schedules.py` secret lifecycle + the `agent_schedules_webhook_auth`
migration columns: mint (plaintext once, AES-256-GCM at rest), status reflection,
trigger-path decrypt round-trip, and the credential-rotation invariants (rotating
the URL clears the secret; revoke clears everything).

Runs on the db_harness backend (SQLite, and PostgreSQL when TEST_POSTGRES_URL is
set), which builds the full schema including the ent#77 columns.
"""

import os
import secrets
import sys
import uuid
from pathlib import Path

os.environ.setdefault("REDIS_URL", "redis://test:test@redis:6379")
os.environ.setdefault("REDIS_PASSWORD", "test")
os.environ.setdefault("REDIS_BACKEND_PASSWORD", "test")

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"
if str(_BACKEND) not in sys.path:
    sys.path.insert(0, str(_BACKEND))

from db_harness import db_backend  # noqa: E402


@pytest.fixture(autouse=True)
def encryption_key(monkeypatch):
    monkeypatch.setenv("CREDENTIAL_ENCRYPTION_KEY", secrets.token_hex(32))
    yield


@pytest.fixture
def sched(db_backend):
    """A live agent_ownership row + one schedule; returns (ops, schedule_id, agent)."""
    from sqlalchemy import insert
    from db.engine import get_engine
    from db.tables import agent_schedules, agent_ownership, users
    from db.schedules import ScheduleOperations
    from utils.helpers import utc_now_iso

    agent = f"agent-wh-{uuid.uuid4().hex[:6]}"
    sid = "sched_" + uuid.uuid4().hex[:8]
    now = utc_now_iso()
    with get_engine().begin() as c:
        # a user row for the FK (owner_id)
        c.execute(insert(users).values(
            id=99123, username="wh-owner", role="user", created_at=now, updated_at=now))
        c.execute(insert(agent_ownership).values(
            agent_name=agent, owner_id=99123, created_at=now))
        c.execute(insert(agent_schedules).values(
            id=sid, agent_name=agent, name="t", cron_expression="0 9 * * *",
            message="hello", enabled=1, timezone="UTC", owner_id=99123,
            created_at=now, updated_at=now))
    # ScheduleOperations needs user/agent ops; None is fine — the webhook methods
    # used here don't touch them.
    return ScheduleOperations(None, None), sid, agent


def _decrypt(encrypted):
    from services.credential_encryption import get_credential_encryption_service
    from services.webhook_signature import SECRET_ENVELOPE_KEY
    return get_credential_encryption_service().decrypt(encrypted).get(SECRET_ENVELOPE_KEY)


class TestWebhookSecretLifecycle:
    pytestmark = pytest.mark.unit

    def test_secret_requires_token(self, sched):
        ops, sid, _ = sched
        # No webhook token yet → set_webhook_secret is a no-op (returns None).
        assert ops.set_webhook_secret(sid) is None

    def test_mint_returns_plaintext_once_and_stores_ciphertext(self, sched):
        ops, sid, _ = sched
        ops.generate_webhook_token(sid)
        secret = ops.set_webhook_secret(sid)
        assert secret and secret.startswith("whsec_")

        st = ops.get_webhook_status(sid)
        assert st["auth_enabled"] is True and st["has_secret"] is True
        # get_webhook_status never leaks the secret material
        assert "signing_secret" not in st and "secret" not in st

    def test_trigger_path_row_decrypts_to_mint(self, sched):
        ops, sid, _ = sched
        token = ops.generate_webhook_token(sid)
        secret = ops.set_webhook_secret(sid)
        row = ops.get_schedule_by_webhook_token(token)
        assert row.webhook_auth_enabled is True
        assert row.webhook_secret_encrypted and row.webhook_secret_encrypted != secret
        assert _decrypt(row.webhook_secret_encrypted) == secret

    def test_rotating_url_clears_secret(self, sched):
        ops, sid, _ = sched
        ops.generate_webhook_token(sid)
        ops.set_webhook_secret(sid)
        # Rotating the URL is a credential-rotation event → secret must be dropped.
        ops.generate_webhook_token(sid)
        st = ops.get_webhook_status(sid)
        assert st["auth_enabled"] is False and st["has_secret"] is False

    def test_disable_signature_keeps_url(self, sched):
        ops, sid, _ = sched
        ops.generate_webhook_token(sid)
        ops.set_webhook_secret(sid)
        ops.clear_webhook_secret(sid)
        st = ops.get_webhook_status(sid)
        assert st["has_token"] is True and st["webhook_enabled"] is True
        assert st["auth_enabled"] is False and st["has_secret"] is False

    def test_revoke_clears_token_and_secret(self, sched):
        ops, sid, _ = sched
        ops.generate_webhook_token(sid)
        ops.set_webhook_secret(sid)
        ops.revoke_webhook_token(sid)
        st = ops.get_webhook_status(sid)
        assert st["has_token"] is False and st["has_secret"] is False
        assert st["webhook_enabled"] is False and st["auth_enabled"] is False

    def test_rotating_secret_changes_ciphertext(self, sched):
        ops, sid, _ = sched
        token = ops.generate_webhook_token(sid)
        first = ops.set_webhook_secret(sid)
        c1 = ops.get_schedule_by_webhook_token(token).webhook_secret_encrypted
        second = ops.set_webhook_secret(sid)
        c2 = ops.get_schedule_by_webhook_token(token).webhook_secret_encrypted
        assert first != second
        assert c1 != c2
        assert _decrypt(c2) == second
