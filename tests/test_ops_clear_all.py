"""
Operations Clear All Tests (test_ops_clear_all.py)

Tests for the bulk clear endpoints behind the Operations page Clear All
button (#1017):
  - POST /api/operator-queue/bulk-cancel
  - POST /api/operator-queue/clear-resolved
  - POST /api/notifications/dismiss-all

Also pins the respond-vs-bulk-cancel race fix (DB-layer conflict marker)
and the tri-state accessible-set contract (None=admin, empty set=no-op).

Feature Flow: operating-room.md
"""

import os
import subprocess
import pytest
import uuid
from datetime import datetime, timezone

from testkit.api_client import TrinityApiClient, ApiConfig
from testkit.assertions import assert_status


# ============================================================================
# Helpers
# ============================================================================

_BACKEND_CONTAINER = os.getenv("TRINITY_BACKEND_CONTAINER", "trinity-backend")
_RUN = uuid.uuid4().hex[:6]
_AGENT = f"test-clr-agent-{_RUN}"
_ISO_USERNAME = f"testuser-clr-{_RUN}"
_ISO_PASSWORD = "test-clr-password-1017"
_ISO_EMAIL = f"{_ISO_USERNAME}@test.example.com"


def _exec_backend(python_code: str) -> str:
    """Run python inside the backend container and return its output.

    Scripts that `import database` trigger an import-time ADMIN_PASSWORD
    warning on stdout; such scripts must print their value as `RESULT:<v>`
    and we extract it here. Plain sqlite3 scripts return raw stdout.
    """
    result = subprocess.run(
        ["docker", "exec", _BACKEND_CONTAINER, "python3", "-c", python_code],
        capture_output=True, text=True, timeout=20,
    )
    if result.returncode != 0:
        raise RuntimeError(f"Backend exec failed: {result.stderr}")
    out = result.stdout.strip()
    for line in reversed(out.splitlines()):
        if line.startswith("RESULT:"):
            return line[len("RESULT:"):]
    return out


def _seed_queue_item(item_id: str, status: str = "pending", agent: str = _AGENT):
    """Insert an operator_queue row directly in the DB."""
    now = datetime.now(timezone.utc).isoformat()
    _exec_backend(f"""
import sqlite3, os
from pathlib import Path
db = os.getenv("TRINITY_DB_PATH", str(Path.home() / "trinity-data" / "trinity.db"))
conn = sqlite3.connect(db)
conn.execute(
    "INSERT OR IGNORE INTO operator_queue "
    "(id, agent_name, type, status, priority, title, question, created_at) "
    "VALUES (?, ?, 'question', ?, 'medium', 'Clear-all test', 'Proceed?', ?)",
    ("{item_id}", "{agent}", "{status}", "{now}"),
)
conn.commit()
conn.close()
print("OK")
""")


def _seed_notification(notif_id: str, status: str = "pending", agent: str = _AGENT):
    """Insert an agent_notifications row directly in the DB."""
    now = datetime.now(timezone.utc).isoformat()
    _exec_backend(f"""
import sqlite3, os
from pathlib import Path
db = os.getenv("TRINITY_DB_PATH", str(Path.home() / "trinity-data" / "trinity.db"))
conn = sqlite3.connect(db)
conn.execute(
    "INSERT OR IGNORE INTO agent_notifications "
    "(id, agent_name, notification_type, title, priority, status, created_at) "
    "VALUES (?, ?, 'info', 'Clear-all test notif', 'normal', ?, ?)",
    ("{notif_id}", "{agent}", "{status}", "{now}"),
)
conn.commit()
conn.close()
print("OK")
""")


def _queue_item_status(item_id: str) -> str:
    """Read an operator_queue row's status straight from the DB ('' if gone)."""
    return _exec_backend(f"""
import sqlite3, os
from pathlib import Path
db = os.getenv("TRINITY_DB_PATH", str(Path.home() / "trinity-data" / "trinity.db"))
conn = sqlite3.connect(db)
row = conn.execute("SELECT status FROM operator_queue WHERE id = ?", ("{item_id}",)).fetchone()
conn.close()
print(row[0] if row else "")
""")


@pytest.fixture(scope="module")
def clr_setup(api_client: TrinityApiClient):
    """Sentinel agent owned by admin + a zero-agent non-admin user."""
    # Non-admin user with no agents (exercises the empty-accessible-set path)
    _exec_backend(f"""
import sqlite3, os
from pathlib import Path
from passlib.context import CryptContext
ctx = CryptContext(schemes=["bcrypt"], deprecated="auto")
pw = ctx.hash("{_ISO_PASSWORD}")
db = os.getenv("TRINITY_DB_PATH", str(Path.home() / "trinity-data" / "trinity.db"))
conn = sqlite3.connect(db)
conn.execute(
    "INSERT OR IGNORE INTO users (username, password_hash, role, email, created_at, updated_at) "
    "VALUES (?, ?, 'user', ?, datetime('now'), datetime('now'))",
    ("{_ISO_USERNAME}", pw, "{_ISO_EMAIL}"),
)
admin_id = conn.execute("SELECT id FROM users WHERE username='admin'").fetchone()[0]
conn.execute(
    "INSERT OR IGNORE INTO agent_ownership (agent_name, owner_id, created_at) "
    "VALUES (?, ?, datetime('now'))",
    ("{_AGENT}", admin_id),
)
conn.commit()
conn.close()
print("OK")
""")

    cfg = ApiConfig(
        base_url=os.getenv("TRINITY_API_URL", "http://localhost:8000"),
        username=_ISO_USERNAME,
        password=_ISO_PASSWORD,
    )
    non_admin = TrinityApiClient(cfg)
    non_admin.authenticate()

    yield {"admin": api_client, "non_admin": non_admin, "agent": _AGENT}

    non_admin.close()

    _exec_backend(f"""
import sqlite3, os
from pathlib import Path
db = os.getenv("TRINITY_DB_PATH", str(Path.home() / "trinity-data" / "trinity.db"))
conn = sqlite3.connect(db)
conn.execute("DELETE FROM operator_queue WHERE agent_name = ?", ("{_AGENT}",))
conn.execute("DELETE FROM agent_notifications WHERE agent_name = ?", ("{_AGENT}",))
conn.execute("DELETE FROM agent_ownership WHERE agent_name = ?", ("{_AGENT}",))
conn.execute("DELETE FROM users WHERE username = ?", ("{_ISO_USERNAME}",))
conn.commit()
conn.close()
print("OK")
""")


# ============================================================================
# Authentication
# ============================================================================

class TestClearAllAuthentication:
    pytestmark = pytest.mark.smoke

    def test_bulk_cancel_requires_auth(self, unauthenticated_client: TrinityApiClient):
        response = unauthenticated_client.post(
            "/api/operator-queue/bulk-cancel", json={"ids": ["x"]}, auth=False
        )
        assert_status(response, 401)

    def test_clear_resolved_requires_auth(self, unauthenticated_client: TrinityApiClient):
        response = unauthenticated_client.post(
            "/api/operator-queue/clear-resolved", json={}, auth=False
        )
        assert_status(response, 401)

    def test_dismiss_all_requires_auth(self, unauthenticated_client: TrinityApiClient):
        response = unauthenticated_client.post(
            "/api/notifications/dismiss-all", json={}, auth=False
        )
        assert_status(response, 401)


# ============================================================================
# Bulk cancel (Needs Response tab)
# ============================================================================

class TestBulkCancel:
    pytestmark = pytest.mark.smoke

    def test_empty_ids_rejected(self, api_client: TrinityApiClient):
        """ids is required and must be non-empty."""
        response = api_client.post("/api/operator-queue/bulk-cancel", json={"ids": []})
        assert_status(response, 422)

    def test_missing_body_rejected(self, api_client: TrinityApiClient):
        response = api_client.post("/api/operator-queue/bulk-cancel", json={})
        assert_status(response, 422)

    def test_oversized_ids_rejected(self, api_client: TrinityApiClient):
        """More than 500 ids is rejected before reaching SQL."""
        response = api_client.post(
            "/api/operator-queue/bulk-cancel",
            json={"ids": [f"x-{i}" for i in range(501)]},
        )
        assert_status(response, 422)

    def test_bulk_cancel_pending_items(self, clr_setup):
        """Listed pending items transition to cancelled."""
        admin = clr_setup["admin"]
        ids = [f"clr-bc-{_RUN}-{i}" for i in range(2)]
        for item_id in ids:
            _seed_queue_item(item_id, status="pending")

        response = admin.post("/api/operator-queue/bulk-cancel", json={"ids": ids})
        assert_status(response, 200)
        data = response.json()
        assert data["cancelled"] == 2
        assert data["skipped"] == 0

        for item_id in ids:
            assert _queue_item_status(item_id) == "cancelled"

    def test_bulk_cancel_skips_non_pending(self, clr_setup):
        """Already-resolved ids are skipped, pending ones cancelled."""
        admin = clr_setup["admin"]
        pending_id = f"clr-bc-mix-p-{_RUN}"
        responded_id = f"clr-bc-mix-r-{_RUN}"
        _seed_queue_item(pending_id, status="pending")
        _seed_queue_item(responded_id, status="responded")

        response = admin.post(
            "/api/operator-queue/bulk-cancel",
            json={"ids": [pending_id, responded_id]},
        )
        assert_status(response, 200)
        data = response.json()
        assert data["cancelled"] == 1
        assert data["skipped"] == 1
        assert _queue_item_status(responded_id) == "responded"

    def test_bulk_cancel_idempotent(self, clr_setup):
        """Re-cancelling already-cancelled ids cancels nothing."""
        admin = clr_setup["admin"]
        item_id = f"clr-bc-idem-{_RUN}"
        _seed_queue_item(item_id, status="pending")

        first = admin.post("/api/operator-queue/bulk-cancel", json={"ids": [item_id]})
        assert_status(first, 200)
        assert first.json()["cancelled"] == 1

        second = admin.post("/api/operator-queue/bulk-cancel", json={"ids": [item_id]})
        assert_status(second, 200)
        data = second.json()
        assert data["cancelled"] == 0
        assert data["skipped"] == 1


# ============================================================================
# Respond-vs-cancel race (DB conflict marker → router 409)
# ============================================================================

class TestRespondConflictMarker:
    pytestmark = pytest.mark.smoke

    def test_respond_to_cancelled_item_returns_conflict_marker(self, clr_setup):
        """The DB layer flags 'exists but not pending' instead of swallowing it.

        The router's pre-check 400s on a known non-pending item, so the 409
        path only fires in the true race window — pin the DB seam directly.
        """
        item_id = f"clr-race-{_RUN}"
        _seed_queue_item(item_id, status="cancelled")

        marker = _exec_backend(f"""
from database import db
item = db.respond_to_operator_queue_item(
    item_id="{item_id}", response="approve", response_text=None,
    responded_by_id="1", responded_by_email="admin@test",
)
print("RESULT:" + str(item.get("_status_conflict", False)))
""")
        assert marker == "True"
        # And the response must NOT have been recorded
        assert _queue_item_status(item_id) == "cancelled"


# ============================================================================
# Clear resolved (Resolved tab)
# ============================================================================

class TestClearResolved:
    pytestmark = pytest.mark.smoke

    def test_clear_hides_terminal_keeps_responded_and_pending(self, clr_setup):
        """acknowledged/cancelled/expired get cleared_at; responded/pending
        stay visible. Rows are hidden, NOT deleted — a DELETE would let the
        sync loop resurrect items whose agent-file entry still says pending.
        """
        admin = clr_setup["admin"]
        agent = clr_setup["agent"]

        # Flush terminal rows left behind by earlier test classes (the
        # bulk-cancel tests share this agent) so the count below is exact.
        admin.post("/api/operator-queue/clear-resolved", json={"agent_name": agent})

        seeded = {
            "acknowledged": f"clr-cr-ack-{_RUN}",
            "cancelled": f"clr-cr-can-{_RUN}",
            "expired": f"clr-cr-exp-{_RUN}",
            "responded": f"clr-cr-res-{_RUN}",
            "pending": f"clr-cr-pen-{_RUN}",
        }
        for status, item_id in seeded.items():
            _seed_queue_item(item_id, status=status)

        response = admin.post(
            "/api/operator-queue/clear-resolved", json={"agent_name": agent}
        )
        assert_status(response, 200)
        assert response.json()["cleared"] == 3

        # Rows still exist with their status intact (hidden, not deleted)
        for key in ("acknowledged", "cancelled", "expired", "responded", "pending"):
            assert _queue_item_status(seeded[key]) == {
                "acknowledged": "acknowledged",
                "cancelled": "cancelled",
                "expired": "expired",
                "responded": "responded",
                "pending": "pending",
            }[key]

        # Cleared rows are excluded from the list; responded/pending remain
        listing = admin.get(f"/api/operator-queue?agent_name={agent}&limit=500")
        assert_status(listing, 200)
        listed_ids = {i["id"] for i in listing.json()["items"]}
        for key in ("acknowledged", "cancelled", "expired"):
            assert seeded[key] not in listed_ids, f"cleared {key} row still listed"
        assert seeded["responded"] in listed_ids
        assert seeded["pending"] in listed_ids

        # But get-by-id still returns a cleared row (audit/debugging path)
        direct = admin.get(f"/api/operator-queue/{seeded['acknowledged']}")
        assert_status(direct, 200)
        assert direct.json()["cleared_at"] is not None

    def test_responded_item_still_deliverable_after_clear(self, clr_setup):
        """The sync write-back accessor still sees responded rows post-clear."""
        admin = clr_setup["admin"]
        agent = clr_setup["agent"]
        item_id = f"clr-cr-wb-{_RUN}"
        _seed_queue_item(item_id, status="responded")

        admin.post("/api/operator-queue/clear-resolved", json={"agent_name": agent})

        found = _exec_backend(f"""
from database import db
items = db.get_operator_queue_responded_for_agent("{agent}")
print("RESULT:" + str(any(i["id"] == "{item_id}" for i in items)))
""")
        assert found == "True"

    def test_clear_resolved_idempotent(self, clr_setup):
        """Second clear with nothing left returns cleared=0 (200, not error)."""
        admin = clr_setup["admin"]
        agent = clr_setup["agent"]
        item_id = f"clr-cr-idem-{_RUN}"
        _seed_queue_item(item_id, status="acknowledged")

        first = admin.post(
            "/api/operator-queue/clear-resolved", json={"agent_name": agent}
        )
        assert_status(first, 200)
        assert first.json()["cleared"] >= 1

        second = admin.post(
            "/api/operator-queue/clear-resolved", json={"agent_name": agent}
        )
        assert_status(second, 200)
        assert second.json()["cleared"] == 0


# ============================================================================
# Dismiss all notifications (Notifications tab)
# ============================================================================

class TestDismissAllNotifications:
    pytestmark = pytest.mark.smoke

    def test_dismiss_all_clears_pending_and_acknowledged(self, clr_setup):
        """DB-layer happy path: pending + acknowledged → dismissed with
        acknowledged_at/by set; already-dismissed rows untouched.

        Exercised at the DB layer because the router's accessible-agents
        accessor is Docker-backed and the seeded agent has no container;
        the router's guard paths are covered by the API tests below.
        """
        agent = clr_setup["agent"]
        ids = {
            "pending1": f"clr-na-{_RUN}",
            "pending2": f"clr-nb-{_RUN}",
            "acknowledged": f"clr-nc-{_RUN}",
            "dismissed": f"clr-nd-{_RUN}",
        }
        _seed_notification(ids["pending1"], status="pending")
        _seed_notification(ids["pending2"], status="pending")
        _seed_notification(ids["acknowledged"], status="acknowledged")
        _seed_notification(ids["dismissed"], status="dismissed")

        dismissed = _exec_backend(f"""
from database import db
n = db.dismiss_all_notifications(
    dismissed_by="1", agent_name="{agent}",
    accessible_agent_names={{"{agent}"}},
)
print("RESULT:" + str(n))
""")
        # >= because the monitoring loop may have generated its own pending
        # notifications for this (container-less) agent in the meantime.
        assert int(dismissed) >= 3

        # The three seeded non-dismissed rows are now dismissed with
        # acknowledged_at set; the pre-dismissed row was left untouched
        # (its acknowledged_at stays NULL).
        check = _exec_backend(f"""
import sqlite3, os, json
from pathlib import Path
db = os.getenv("TRINITY_DB_PATH", str(Path.home() / "trinity-data" / "trinity.db"))
conn = sqlite3.connect(db)
rows = {{}}
for nid in {list(ids.values())!r}:
    r = conn.execute(
        "SELECT status, acknowledged_at FROM agent_notifications WHERE id = ?", (nid,)
    ).fetchone()
    rows[nid] = list(r) if r else None
conn.close()
print("RESULT:" + json.dumps(rows))
""")
        import json as _json
        rows = _json.loads(check)
        for key in ("pending1", "pending2", "acknowledged"):
            status, ack_at = rows[ids[key]]
            assert status == "dismissed", f"{key} not dismissed"
            assert ack_at, f"{key} missing acknowledged_at"
        # Pre-dismissed row untouched
        status, ack_at = rows[ids["dismissed"]]
        assert status == "dismissed"
        assert ack_at is None, "already-dismissed row must not be re-stamped"

    def test_dismiss_all_idempotent(self, clr_setup):
        """A second dismiss-all does not re-stamp already-dismissed rows."""
        agent = clr_setup["agent"]
        nid = ids_marker = f"clr-n-idem-{_RUN}"
        _seed_notification(nid, status="pending")

        first_stamp = _exec_backend(f"""
from database import db
db.dismiss_all_notifications(
    dismissed_by="1", agent_name="{agent}", accessible_agent_names={{"{agent}"}},
)
n = db.get_notification("{nid}")
print("RESULT:" + str(n.acknowledged_at))
""")
        assert first_stamp and first_stamp != "None"

        second_stamp = _exec_backend(f"""
from database import db
db.dismiss_all_notifications(
    dismissed_by="1", agent_name="{agent}", accessible_agent_names={{"{agent}"}},
)
n = db.get_notification("{nid}")
print("RESULT:" + str(n.acknowledged_at))
""")
        assert second_stamp == first_stamp, "second dismiss-all must not touch dismissed rows"

    def test_non_admin_dismiss_all_foreign_agent_403(self, clr_setup):
        """Explicit agent_name outside the caller's accessible set → 403."""
        non_admin = clr_setup["non_admin"]
        agent = clr_setup["agent"]
        response = non_admin.post(
            "/api/notifications/dismiss-all", json={"agent_name": agent}
        )
        assert_status(response, 403)

    def test_zero_agent_user_dismiss_all_noop(self, clr_setup):
        """Empty accessible set short-circuits to dismissed=0 (no SQL error,
        no fleet-wide UPDATE)."""
        non_admin = clr_setup["non_admin"]
        agent = clr_setup["agent"]
        sentinel = f"clr-n-iso-{_RUN}"
        _seed_notification(sentinel, status="pending")

        response = non_admin.post("/api/notifications/dismiss-all", json={})
        assert_status(response, 200)
        assert response.json()["dismissed"] == 0

        # Admin's notification untouched
        status = _exec_backend(f"""
import sqlite3, os
from pathlib import Path
db = os.getenv("TRINITY_DB_PATH", str(Path.home() / "trinity-data" / "trinity.db"))
conn = sqlite3.connect(db)
row = conn.execute("SELECT status FROM agent_notifications WHERE id = ?", ("{sentinel}",)).fetchone()
conn.close()
print(row[0] if row else "")
""")
        assert status == "pending"


# ============================================================================
# Cross-user isolation for the queue bulk endpoints
# ============================================================================

class TestClearAllAccessControl:
    pytestmark = pytest.mark.smoke

    def test_non_admin_bulk_cancel_foreign_items_skipped(self, clr_setup):
        """A non-admin cannot cancel items of agents they don't own."""
        non_admin = clr_setup["non_admin"]
        item_id = f"clr-iso-bc-{_RUN}"
        _seed_queue_item(item_id, status="pending")

        response = non_admin.post(
            "/api/operator-queue/bulk-cancel", json={"ids": [item_id]}
        )
        assert_status(response, 200)
        data = response.json()
        assert data["cancelled"] == 0
        assert data["skipped"] == 1
        assert _queue_item_status(item_id) == "pending"

    def test_non_admin_clear_resolved_noop(self, clr_setup):
        """Zero-agent user clearing resolved touches nothing fleet-wide (C1)."""
        non_admin = clr_setup["non_admin"]
        item_id = f"clr-iso-cr-{_RUN}"
        _seed_queue_item(item_id, status="acknowledged")

        response = non_admin.post("/api/operator-queue/clear-resolved", json={})
        assert_status(response, 200)
        assert response.json()["cleared"] == 0
        assert _queue_item_status(item_id) == "acknowledged"

    def test_non_admin_clear_resolved_foreign_agent_403(self, clr_setup):
        """Explicit agent_name outside the accessible set → 403."""
        non_admin = clr_setup["non_admin"]
        agent = clr_setup["agent"]
        response = non_admin.post(
            "/api/operator-queue/clear-resolved", json={"agent_name": agent}
        )
        assert_status(response, 403)
