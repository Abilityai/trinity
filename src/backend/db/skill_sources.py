"""Skill source database operations (ent#237).

A *source* is one git repo the skills library syncs from. This replaces the
single ``skills_library_url`` system setting with a table, so an install can
carry the bundled community catalog **and** its own repo(s) at once.

Resolution on a name collision is ``priority`` ASC then ``created_at`` ASC:
custom sources default to 100 and the bundled community source to 1000, so
**custom wins** (ent#237 AC#4). Names stay bare — the agent-side identity is
the directory ``.claude/skills/<name>/``, and both the ent#139 runner and the
ent#178 A2A card resolve by bare name, so prefixing would change every agent's
invocation string.

SQLAlchemy Core throughout (#300) so it runs unchanged on SQLite and
PostgreSQL. This module owns no HTTP concerns and no git/filesystem work —
cloning and syncing live in ``services/skill_service.py``.
"""

import secrets
from datetime import datetime
from typing import Any, Dict, List, Optional

from sqlalchemy import select, insert, update, delete
from sqlalchemy.exc import IntegrityError

from .engine import get_engine
from .tables import skill_sources
from db_models import SkillSource
from utils.helpers import utc_now_iso

# Custom sources outrank the bundled default purely by these numbers; nothing
# else encodes the precedence, so the merge in skill_service stays a plain
# ORDER BY rather than a special case for `is_default`.
DEFAULT_SOURCE_PRIORITY = 1000
CUSTOM_SOURCE_PRIORITY = 100


class DuplicateSkillSource(Exception):
    """A source with this (url, ref) already exists."""


class DefaultSourceExists(Exception):
    """A bundled default source is already registered (at most one)."""


class SkillSourcesOperations:
    """Skill source CRUD + sync bookkeeping."""

    @staticmethod
    def _row_to_source(row) -> SkillSource:
        def _dt(value: Optional[str]) -> Optional[datetime]:
            if not value:
                return None
            try:
                return datetime.fromisoformat(value.replace("Z", "+00:00"))
            except ValueError:
                # A hand-edited or legacy timestamp must not take down the
                # whole listing — the row is still usable for syncing.
                return None

        return SkillSource(
            id=row["id"],
            name=row["name"],
            url=row["url"],
            ref=row["ref"],
            ref_type=row["ref_type"],
            is_default=bool(row["is_default"]),
            enabled=bool(row["enabled"]),
            priority=row["priority"],
            last_sync_at=_dt(row["last_sync_at"]),
            last_sync_status=row["last_sync_status"],
            last_commit_sha=row["last_commit_sha"],
            last_error=row["last_error"],
            created_by=row["created_by"],
            created_at=_dt(row["created_at"]),
            updated_at=_dt(row["updated_at"]),
        )

    # =========================================================================
    # Read
    # =========================================================================

    def list_sources(self, enabled_only: bool = False) -> List[SkillSource]:
        """All sources in RESOLUTION order (first wins a name collision)."""
        stmt = select(skill_sources)
        if enabled_only:
            stmt = stmt.where(skill_sources.c.enabled == 1)
        stmt = stmt.order_by(
            skill_sources.c.priority.asc(),
            skill_sources.c.created_at.asc(),
        )
        with get_engine().connect() as conn:
            return [self._row_to_source(r) for r in conn.execute(stmt).mappings()]

    def get_source(self, source_id: str) -> Optional[SkillSource]:
        stmt = select(skill_sources).where(skill_sources.c.id == source_id)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_source(row) if row else None

    def get_default_source(self) -> Optional[SkillSource]:
        """The bundled community source, if one is registered."""
        stmt = select(skill_sources).where(skill_sources.c.is_default == 1)
        with get_engine().connect() as conn:
            row = conn.execute(stmt).mappings().first()
        return self._row_to_source(row) if row else None

    def count_sources(self) -> int:
        with get_engine().connect() as conn:
            return len(conn.execute(select(skill_sources.c.id)).fetchall())

    # =========================================================================
    # Write
    # =========================================================================

    def create_source(
        self,
        name: str,
        url: str,
        ref: str = "main",
        ref_type: str = "branch",
        is_default: bool = False,
        created_by: Optional[str] = None,
        priority: Optional[int] = None,
        enabled: bool = True,
    ) -> SkillSource:
        """Register a source.

        Raises DuplicateSkillSource on a repeated (url, ref) and
        DefaultSourceExists on a second bundled default — both are enforced by
        DB constraints (a partial-unique index for the default), so a
        concurrent second worker loses at the DB rather than at a read-then-write
        check that has already gone stale.
        """
        now = utc_now_iso()
        source_id = f"src_{secrets.token_hex(8)}"
        if priority is None:
            priority = DEFAULT_SOURCE_PRIORITY if is_default else CUSTOM_SOURCE_PRIORITY

        stmt = insert(skill_sources).values(
            id=source_id,
            name=name,
            url=url,
            ref=ref,
            ref_type=ref_type,
            is_default=1 if is_default else 0,
            enabled=1 if enabled else 0,
            priority=priority,
            last_sync_status="never",
            created_by=created_by,
            created_at=now,
            updated_at=now,
        )
        try:
            with get_engine().begin() as conn:
                conn.execute(stmt)
        except IntegrityError as exc:
            # Distinguish the two constraints so the router can map them to
            # different, actionable messages instead of one flat 409.
            if is_default and self.get_default_source() is not None:
                raise DefaultSourceExists(
                    "a bundled default source is already registered"
                ) from exc
            raise DuplicateSkillSource(
                f"a source for {url} @ {ref} already exists"
            ) from exc

        created = self.get_source(source_id)
        if created is None:  # pragma: no cover — insert succeeded above
            raise RuntimeError(f"skill source {source_id} vanished after insert")
        return created

    _MUTABLE_FIELDS = {"name", "url", "ref", "ref_type", "enabled", "priority"}

    # Changing any of these makes the source point somewhere else, which voids
    # every piece of sync bookkeeping recorded against the old target.
    _IDENTITY_FIELDS = ("url", "ref", "ref_type")

    def update_source(self, source_id: str, **fields: Any) -> Optional[SkillSource]:
        """Patch a source. Unknown/immutable fields are ignored.

        `is_default` is deliberately NOT mutable: promoting a custom source to
        the bundled default would change its trust posture (tag-pinned, ours to
        bump) without changing where it points. Delete and re-add instead.

        Changing `url`/`ref`/`ref_type` CLEARS the sync bookkeeping
        (`last_commit_sha`, status, timestamp, error). `last_commit_sha` is the
        load-bearing one: it is the tag pin's baseline, and a baseline is only
        meaningful for the ref it was recorded against. Left in place, bumping a
        tag `v1` → `v2` compared v2 against v1's SHA and was refused as
        `moved_tag` — with the error telling the operator to do the thing they
        had just done — and on the fresh-clone path the refusal `rmtree`s the
        checkout first, so a source that was merely being bumped ended up empty.
        The other three go with it because "Synced <date>" against a repo this
        source has never fetched is a claim the row cannot support.
        """
        values: Dict[str, Any] = {
            k: (int(v) if k == "enabled" else v)
            for k, v in fields.items()
            if k in self._MUTABLE_FIELDS and v is not None
        }
        if not values:
            return self.get_source(source_id)

        current = self.get_source(source_id)
        if current is None:
            return None
        if any(
            k in values and values[k] != getattr(current, k)
            for k in self._IDENTITY_FIELDS
        ):
            values.update(
                last_commit_sha=None,
                last_sync_status="never",
                last_sync_at=None,
                last_error=None,
            )
        values["updated_at"] = utc_now_iso()

        stmt = (
            update(skill_sources)
            .where(skill_sources.c.id == source_id)
            .values(**values)
        )
        try:
            with get_engine().begin() as conn:
                if conn.execute(stmt).rowcount == 0:
                    return None
        except IntegrityError as exc:
            raise DuplicateSkillSource(
                "another source already points at that url and ref"
            ) from exc
        return self.get_source(source_id)

    def delete_source(self, source_id: str) -> bool:
        """Remove a source row.

        Assignments referencing it are left alone: `agent_skills.source_id`
        becomes a dangling record of where the skill CAME from, and the skill
        itself keeps resolving by bare name through whatever source still
        provides it. Cascading the delete would silently unassign skills that
        are still perfectly available from another source.
        """
        stmt = delete(skill_sources).where(skill_sources.c.id == source_id)
        with get_engine().begin() as conn:
            return conn.execute(stmt).rowcount > 0

    def record_sync(
        self,
        source_id: str,
        *,
        success: bool,
        commit_sha: Optional[str] = None,
        error: Optional[str] = None,
    ) -> None:
        """Stamp the outcome of a sync attempt.

        Never raises — this runs on the tail of a sync, and losing the
        bookkeeping must not turn a successful clone into a failed operation.
        `last_error` is cleared on success so a stale message can't outlive the
        failure it described.
        """
        now = utc_now_iso()
        values: Dict[str, Any] = {
            "last_sync_at": now,
            "last_sync_status": "success" if success else "failed",
            "last_error": None if success else (error or "")[:2000],
            "updated_at": now,
        }
        if success and commit_sha:
            values["last_commit_sha"] = commit_sha
        try:
            with get_engine().begin() as conn:
                conn.execute(
                    update(skill_sources)
                    .where(skill_sources.c.id == source_id)
                    .values(**values)
                )
        except Exception:  # noqa: BLE001 — bookkeeping is never load-bearing
            import logging

            logging.getLogger(__name__).warning(
                "failed to record sync outcome for skill source %s", source_id,
                exc_info=True,
            )
