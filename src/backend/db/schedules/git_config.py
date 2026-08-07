"""Per-agent Git configuration + sync flags (Phase 7 GitHub bidirectional sync)."""

import json
from datetime import datetime
from typing import Optional, List, Dict

from sqlalchemy import select, insert, update, delete, and_, func, text
from sqlalchemy.exc import IntegrityError

from ..engine import get_engine
from ..tables import (
    agent_git_config,
    agent_ownership,
)
from db_models import AgentGitConfig
from utils.helpers import utc_now_iso, parse_iso_timestamp

# #73: chunk size for scoped `IN (...)` lookups. SQLite caps host parameters at
# SQLITE_MAX_VARIABLE_NUMBER (999 before SQLite 3.32). Keep a safe margin below
# that so a large accessible-agent set can't blow the limit. Read as a module
# global so tests can monkeypatch it to exercise the multi-chunk path.
_SQLITE_MAX_IN_VARS = 900

class ScheduleGitConfigMixin:
    """Git-config CRUD, sync flags, duplicate-binding audit."""

    @staticmethod
    def _row_to_git_config(row) -> AgentGitConfig:
        """Convert an agent_git_config row to an AgentGitConfig model."""
        row_keys = row.keys() if hasattr(row, 'keys') else []
        return AgentGitConfig(
            id=row["id"],
            agent_name=row["agent_name"],
            github_repo=row["github_repo"],
            working_branch=row["working_branch"],
            instance_id=row["instance_id"],
            source_branch=row["source_branch"] if "source_branch" in row_keys else "main",
            source_mode=bool(row["source_mode"]) if "source_mode" in row_keys else False,
            # Use parse_iso_timestamp to handle both 'Z' and non-'Z' timestamps
            created_at=parse_iso_timestamp(row["created_at"]),
            last_sync_at=parse_iso_timestamp(row["last_sync_at"]) if row["last_sync_at"] else None,
            last_commit_sha=row["last_commit_sha"],
            sync_enabled=bool(row["sync_enabled"]),
            sync_paths=row["sync_paths"],
            # #389 sync health fields — absent on DBs predating the migration.
            auto_sync_enabled=bool(row["auto_sync_enabled"])
                if "auto_sync_enabled" in row_keys else False,
            freeze_schedules_if_sync_failing=bool(row["freeze_schedules_if_sync_failing"])
                if "freeze_schedules_if_sync_failing" in row_keys else False,
        )

    # =========================================================================
    # Git Configuration Management (Phase 7: GitHub Bidirectional Sync)
    # =========================================================================

    def create_git_config(
        self,
        agent_name: str,
        github_repo: str,
        working_branch: str,
        instance_id: str,
        sync_paths: List[str] = None,
        source_branch: str = "main",
        source_mode: bool = False
    ) -> Optional[AgentGitConfig]:
        """Create git configuration for an agent.

        Args:
            agent_name: Name of the agent
            github_repo: GitHub repository (e.g., "owner/repo")
            working_branch: Branch for Trinity to work on (legacy mode) or same as source_branch
            instance_id: Unique instance identifier
            sync_paths: Paths to sync (default: memory/, outputs/, etc.)
            source_branch: Branch to pull updates from (default: "main")
            source_mode: If True, track source_branch directly without creating a working branch
        """
        config_id = self._generate_id()
        now = utc_now_iso()
        sync_paths_json = json.dumps(sync_paths) if sync_paths else json.dumps(["memory/", "outputs/", "CLAUDE.md", ".claude/"])

        try:
            with get_engine().begin() as conn:
                conn.execute(
                    insert(agent_git_config).values(
                        id=config_id,
                        agent_name=agent_name,
                        github_repo=github_repo,
                        working_branch=working_branch,
                        instance_id=instance_id,
                        source_branch=source_branch,
                        source_mode=1 if source_mode else 0,
                        created_at=now,
                        sync_enabled=1,
                        sync_paths=sync_paths_json,
                    )
                )

            return AgentGitConfig(
                id=config_id,
                agent_name=agent_name,
                github_repo=github_repo,
                working_branch=working_branch,
                instance_id=instance_id,
                source_branch=source_branch,
                source_mode=source_mode,
                created_at=datetime.fromisoformat(now),
                sync_enabled=True,
                sync_paths=sync_paths_json
            )
        except IntegrityError:
            # Already exists
            return None

    def get_git_config(self, agent_name: str) -> Optional[AgentGitConfig]:
        """Get git configuration for an agent."""
        stmt = select(agent_git_config).where(
            agent_git_config.c.agent_name == agent_name
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_git_config(row) if row else None

    def get_git_config_agent_names_for_repo(self, github_repo: str) -> List[str]:
        """Agent names whose git config binds ``github_repo`` (any branch/mode).

        Fork-to-own creation guard (trinity-enterprise#93): source-mode rows
        bypass the partial UNIQUE(github_repo, working_branch) index, so two
        auto-pushing agents could otherwise bind the same destination repo.
        NB: agent delete is a SOFT delete — the git-config row is cascaded only
        at purge (retention default 180d), so a hit may name a soft-deleted
        agent. That block is intentional: admin recovery (#834) would resurrect
        the binding. Comparison is case-insensitive — GitHub repo slugs are.
        """
        stmt = select(agent_git_config.c.agent_name).where(
            func.lower(agent_git_config.c.github_repo) == github_repo.lower()
        )
        with get_engine().connect() as conn:
            rows = conn.execute(stmt).scalars().all()
        return list(rows)

    def update_git_sync(self, agent_name: str, commit_sha: str) -> bool:
        """Update git sync timestamp and commit SHA after successful sync."""
        now = utc_now_iso()
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_git_config)
                .where(agent_git_config.c.agent_name == agent_name)
                .values(last_sync_at=now, last_commit_sha=commit_sha)
            )
            return result.rowcount > 0

    def set_git_sync_enabled(self, agent_name: str, enabled: bool) -> bool:
        """Enable or disable git sync for an agent."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_git_config)
                .where(agent_git_config.c.agent_name == agent_name)
                .values(sync_enabled=1 if enabled else 0)
            )
            return result.rowcount > 0

    def set_git_auto_sync_enabled(self, agent_name: str, enabled: bool) -> bool:
        """#389: toggle the 15-min auto-sync heartbeat for an agent."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_git_config)
                .where(agent_git_config.c.agent_name == agent_name)
                .values(auto_sync_enabled=1 if enabled else 0)
            )
            return result.rowcount > 0

    def set_freeze_schedules_if_sync_failing(self, agent_name: str, enabled: bool) -> bool:
        """#389: toggle scheduler freeze-on-failure opt-in."""
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_git_config)
                .where(agent_git_config.c.agent_name == agent_name)
                .values(freeze_schedules_if_sync_failing=1 if enabled else 0)
            )
            return result.rowcount > 0

    def get_git_auto_sync_enabled(self, agent_name: str) -> bool:
        """#389: read the auto-sync flag. False if config missing."""
        stmt = select(agent_git_config.c.auto_sync_enabled).where(
            agent_git_config.c.agent_name == agent_name
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
        return bool(row[0]) if row else False

    def get_all_git_auto_sync_enabled(
        self, agent_names: Optional[set] = None
    ) -> Dict[str, bool]:
        """#73: bulk read of the auto-sync flag in one query, optionally scoped
        to `agent_names` (mirrors get_all_permission_edges). Removes the N+1 in
        the /sync-health dashboard endpoint. Agents without a config row are
        absent from the result (caller defaults to False).

        `None` = whole fleet; an empty set = empty result (an empty scope must
        NOT fall through to the whole-fleet query, and `IN ()` is invalid SQL).

        Scoped lookups chunk the `IN (...)` list at `_SQLITE_MAX_IN_VARS` so a
        large accessible-agent set can't exceed SQLite's host-parameter cap.
        """
        if agent_names is not None and not agent_names:
            return {}
        with get_engine().connect() as conn:
            if agent_names is None:
                rows = conn.execute(
                    select(
                        agent_git_config.c.agent_name,
                        agent_git_config.c.auto_sync_enabled,
                    )
                ).all()
                return {row[0]: bool(row[1]) for row in rows}

            result: Dict[str, bool] = {}
            names = list(agent_names)
            for start in range(0, len(names), _SQLITE_MAX_IN_VARS):
                chunk = names[start:start + _SQLITE_MAX_IN_VARS]
                rows = conn.execute(
                    select(
                        agent_git_config.c.agent_name,
                        agent_git_config.c.auto_sync_enabled,
                    ).where(agent_git_config.c.agent_name.in_(chunk))
                ).all()
                result.update({row[0]: bool(row[1]) for row in rows})
            return result

    def get_freeze_schedules_if_sync_failing(self, agent_name: str) -> bool:
        """#389: read the freeze-schedules flag. False if config missing."""
        stmt = select(agent_git_config.c.freeze_schedules_if_sync_failing).where(
            agent_git_config.c.agent_name == agent_name
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
        return bool(row[0]) if row else False

    def find_duplicate_bindings(self) -> set:
        """#390 S6: return the set of agent_names whose (github_repo, working_branch)
        pair is shared with another row where source_mode = 0.

        Source-mode agents intentionally share branches (e.g. all legacy-mode
        siblings track `main`) and are excluded by the partial filter, mirroring
        the spec's §P5 query verbatim.
        """
        # Row-value tuple IN-subquery — kept as text(); valid on both SQLite
        # and PostgreSQL. No bind params, no sqlite-only constructs.
        query = text("""
            SELECT agent_name FROM agent_git_config
            WHERE source_mode = 0
              AND (github_repo, working_branch) IN (
                  SELECT github_repo, working_branch
                  FROM agent_git_config
                  WHERE source_mode = 0
                  GROUP BY github_repo, working_branch
                  HAVING COUNT(*) > 1
              )
        """)
        with get_engine().connect() as conn:
            rows = conn.execute(query).all()
            return {row[0] for row in rows}

    def rebind_git_config(
        self,
        agent_name: str,
        *,
        new_github_repo: str,
        expected_github_repo: str,
        source_branch: str,
    ) -> bool:
        """Compare-and-swap an agent's git binding onto a repo the user owns
        (trinity-enterprise#109).

        **The CAS predicate, stated explicitly:**

            WHERE agent_name = :agent_name
              AND github_repo = :expected_github_repo

        ``expected_github_repo`` is the value the caller READ before it started
        creating GitHub state. Returning ``False`` (rowcount 0) therefore means
        "the row moved under us" — a concurrent rebind, a delete, or an unknown
        agent — and the caller must surface 409 ``BIND_CONCURRENT_MODIFICATION``
        rather than retry: **nothing partial is written**, because this single
        statement is the whole commit point.

        The comparison is exact, NOT case-folded. GitHub slugs are
        case-insensitive, so a same-repo-different-case value would be the
        "same" repo to GitHub — but the point of the predicate is detecting
        that *the row changed*, and any write is a change worth losing the race
        over. Folding case here would let a concurrent writer that only
        re-cased the value slip through undetected.

        ``auto_sync_enabled`` is set to 1: the user now owns the destination,
        and auto-pushing captures to their own default branch is the whole
        point of the operation (the same carve-out ent#93's create path makes
        for fork-to-own agents).

        Deliberately NOT written: ``source_mode`` (stays 1 — see requirements
        §11.12 FR-2; flipping it would carve a ``trinity/<agent>/<id>`` branch
        and move the row *into* ``idx_git_config_repo_branch_unique``) and
        ``working_branch`` (untouched for the same reason).

        Returns True when exactly the intended row was updated.
        """
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_git_config)
                .where(
                    and_(
                        agent_git_config.c.agent_name == agent_name,
                        agent_git_config.c.github_repo == expected_github_repo,
                    )
                )
                .values(
                    github_repo=new_github_repo,
                    source_branch=source_branch,
                    auto_sync_enabled=1,
                )
            )
            return result.rowcount == 1

    def restore_git_config_binding(
        self,
        agent_name: str,
        *,
        github_repo: str,
        source_branch: str,
        auto_sync_enabled: bool,
    ) -> bool:
        """Compensating UPDATE that puts a rebind's captured previous values back
        (trinity-enterprise#109).

        The loser path of a post-commit guard, and deliberately **not**
        ``delete_git_config``. ent#93's create-path rollback can delete because
        the row was INSERTed microseconds earlier and the whole agent creation
        aborts with it. Here the row **pre-exists** a live agent: deleting it
        strips that agent's binding entirely, so its next container recreate
        finds no row, drops ``GITHUB_REPO`` from the baked env, and the agent
        comes back with no repo at all — the silently-empty-agent class
        (#843/#1439). Restoring the captured values is the only shape that is
        a rollback rather than a second, worse mutation.

        Unconditional by design: it runs only when the caller has already
        established it lost, and the values it writes are the ones it read.
        """
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_git_config)
                .where(agent_git_config.c.agent_name == agent_name)
                .values(
                    github_repo=github_repo,
                    source_branch=source_branch,
                    auto_sync_enabled=1 if auto_sync_enabled else 0,
                )
            )
            return result.rowcount > 0

    def delete_git_config(self, agent_name: str) -> bool:
        """Delete git configuration for an agent (when agent is deleted)."""
        with get_engine().begin() as conn:
            result = conn.execute(
                delete(agent_git_config).where(
                    agent_git_config.c.agent_name == agent_name
                )
            )
            return result.rowcount > 0

    def list_git_enabled_agents(self) -> List[AgentGitConfig]:
        """List all agents with git sync enabled.

        Joins `agent_ownership` and excludes soft-deleted agents (#1561).
        `agent_git_config` rows survive soft delete by design, so without this
        filter the 60s `SyncHealthService` poller (and `GET /api/fleet/sync-audit`)
        keeps hitting removed containers forever — each `httpx.ConnectError`
        poisons the transport circuit breaker and eventually drives it to
        DORMANT + a bogus `circuit_breaker_dormant` alert for an agent that no
        longer exists. Mirrors the #834 hardening on `list_all_enabled_schedules()`.
        """
        stmt = (
            select(agent_git_config)
            .select_from(
                agent_git_config.join(
                    agent_ownership,
                    agent_ownership.c.agent_name == agent_git_config.c.agent_name,
                )
            )
            .where(
                and_(
                    agent_git_config.c.sync_enabled == 1,
                    agent_ownership.c.deleted_at.is_(None),
                )
            )
            .order_by(agent_git_config.c.agent_name)
        )
        with get_engine().connect() as conn:
            return [
                self._row_to_git_config(row) for row in conn.execute(stmt).mappings()
            ]
