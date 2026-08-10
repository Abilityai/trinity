"""
trinity-enterprise#305 — org-overlay tag integrity.

The Grid org overlay stores reporting lines as `reports-to-<agent>` tag
VALUES on OTHER agents' rows. Row-level cascades (AGENT_REFS) never touch
values, so rename and hard-purge each need a value-level sweep:

  * rename_reports_to_refs — rewrite refs inside the rename transaction,
    surviving the `(agent_name, tag)` PK collision case (a holder already
    carrying `reports-to-<new>`), which would otherwise abort the WHOLE
    rename with an IntegrityError.
  * delete_reports_to_refs — purge dangling refs fleet-wide so a reused
    name can never silently re-attach a predecessor's org chart.

Also covers the router-level reserved-namespace guard: org tags are
human-only (mirrors the #1578 reserved event namespace).
"""

import sys
from pathlib import Path

import pytest

_project_root = Path(__file__).resolve().parents[2]
_backend = str(_project_root / "src" / "backend")
while _backend in sys.path:
    sys.path.remove(_backend)
sys.path.insert(0, _backend)

from db_harness import db_backend, run as _hrun  # noqa: E402,F401


def _engine():
    from db.engine import get_engine

    return get_engine()


def _seed_agent(name, deleted_at=None):
    _hrun(
        "INSERT INTO agent_ownership (agent_name, owner_id, created_at, deleted_at) "
        "VALUES (:n, 1, :ts, :d)",
        n=name,
        ts="2026-01-01T00:00:00Z",
        d=deleted_at,
    )


def _seed_tag(agent, tag):
    _hrun(
        "INSERT INTO agent_tags (agent_name, tag, created_at) VALUES (:a, :t, :ts)",
        a=agent,
        t=tag,
        ts="2026-01-01T00:00:00Z",
    )


def _tags_of(agent):
    from sqlalchemy import text

    with _engine().connect() as conn:
        rows = conn.execute(
            text("SELECT tag FROM agent_tags WHERE agent_name = :a ORDER BY tag"),
            {"a": agent},
        ).all()
    return [r[0] for r in rows]


class TestRenameReportsToRefs:
    def test_rewrites_refs_fleet_wide(self, db_backend):
        from db.tags import rename_reports_to_refs

        _seed_tag("report-a", "reports-to-old-boss")
        _seed_tag("report-b", "reports-to-old-boss")
        _seed_tag("report-b", "plain")

        with _engine().begin() as conn:
            count = rename_reports_to_refs(conn, "old-boss", "new-boss")

        assert count == 2
        assert _tags_of("report-a") == ["reports-to-new-boss"]
        assert _tags_of("report-b") == ["plain", "reports-to-new-boss"]

    def test_pk_collision_holder_has_both_old_and_new(self, db_backend):
        """A holder already carrying reports-to-<new> must not abort the
        rename transaction (the 2am-Friday case: rename 500s fleet-wide)."""
        from db.tags import rename_reports_to_refs

        _seed_tag("report-a", "reports-to-old-boss")
        _seed_tag("report-a", "reports-to-new-boss")  # stale duplicate

        with _engine().begin() as conn:
            rename_reports_to_refs(conn, "old-boss", "new-boss")

        # Exactly one surviving ref — no IntegrityError, no duplicate row.
        assert _tags_of("report-a") == ["reports-to-new-boss"]

    def test_no_refs_is_a_noop(self, db_backend):
        from db.tags import rename_reports_to_refs

        with _engine().begin() as conn:
            assert rename_reports_to_refs(conn, "nobody", "somebody") == 0


class TestDeleteReportsToRefs:
    def test_purges_dangling_refs_fleet_wide(self, db_backend):
        from db.tags import delete_reports_to_refs

        _seed_tag("report-a", "reports-to-ghost")
        _seed_tag("report-b", "reports-to-ghost")
        _seed_tag("report-b", "reports-to-other")

        with _engine().begin() as conn:
            assert delete_reports_to_refs(conn, "ghost") == 2

        assert _tags_of("report-a") == []
        assert _tags_of("report-b") == ["reports-to-other"]


class TestRenameAgentEndToEnd:
    @pytest.fixture
    def agent_ops(self, db_backend):
        try:
            from db.agents import AgentOperations
            from db.users import UserOperations
        except ImportError:  # pragma: no cover - env guard
            pytest.skip("backend venv required (no `db.agents` import)")
        return AgentOperations(UserOperations())

    def test_rename_rewrites_org_refs_in_the_same_transaction(self, agent_ops):
        _seed_agent("old-boss")
        _seed_agent("report-a")
        _seed_tag("report-a", "reports-to-old-boss")

        assert agent_ops.rename_agent("old-boss", "new-boss") is True
        assert _tags_of("report-a") == ["reports-to-new-boss"]

    def test_rename_survives_ref_collision(self, agent_ops):
        _seed_agent("old-boss")
        _seed_agent("report-a")
        _seed_tag("report-a", "reports-to-old-boss")
        _seed_tag("report-a", "reports-to-new-boss")

        assert agent_ops.rename_agent("old-boss", "new-boss") is True
        assert _tags_of("report-a") == ["reports-to-new-boss"]


class TestCascadeDeletePurgesRefs:
    def test_cascade_removes_refs_on_other_agents(self, db_backend):
        from db.agent_cleanup import cascade_delete

        _seed_agent("doomed")
        _seed_tag("doomed", "plain")
        _seed_tag("survivor", "reports-to-doomed")

        with _engine().begin() as conn:
            deleted = cascade_delete(conn, "doomed")

        assert deleted.get("agent_tags:reports_to_refs") == 1
        assert _tags_of("survivor") == []
        assert _tags_of("doomed") == []


class TestOrgNamespaceGuard:
    """Router guard: org tags are human-only (agent principals rejected)."""

    def _user(self, agent_name=None):
        # models.User is the runtime principal get_current_user returns —
        # `agent_name` is set only for agent-scoped MCP keys.
        from models import User

        return User(id=1, username="u", role="admin", agent_name=agent_name)

    def test_agent_principal_rejected_for_reserved_tags(self):
        from fastapi import HTTPException

        from routers.tags import _guard_org_namespace

        with pytest.raises(HTTPException) as exc:
            _guard_org_namespace(self._user(agent_name="bot"), ["dept-marketing"])
        assert exc.value.status_code == 403

        with pytest.raises(HTTPException):
            _guard_org_namespace(self._user(agent_name="bot"), ["reports-to-boss"])

    def test_agent_principal_allowed_for_plain_tags(self):
        from routers.tags import _guard_org_namespace

        _guard_org_namespace(self._user(agent_name="bot"), ["marketing", "prod"])

    def test_human_principal_allowed_for_reserved_tags(self):
        from routers.tags import _guard_org_namespace

        _guard_org_namespace(self._user(), ["dept-marketing", "reports-to-boss"])
