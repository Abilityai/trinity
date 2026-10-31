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

# The value the fresh-install seed writes to `created_by` (ent#237); the boot
# follow (trinity-enterprise#529) moves only a row that still carries it.
SEED_CREATED_BY = "seed:ent237"

# Everything recorded against a source's previous TARGET, voided when it points
# somewhere else — by an admin edit (`update_source`) or by the boot follow
# (`follow_bundled_pin`), which must not drift apart. `last_commit_sha` is the
# load-bearing one: it is the tag pin's baseline (see `update_source`), and the
# follow line describes how the CURRENT ref got there, so an admin retarget
# makes it stale too.
_RETARGET_RESET: Dict[str, Any] = {
    "last_commit_sha": None,
    "last_sync_status": "never",
    "last_sync_at": None,
    "last_error": None,
    "followed_from_ref": None,
    "followed_at": None,
}


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
            follows_bundled_pin=bool(row["follows_bundled_pin"]),
            followed_from_ref=row["followed_from_ref"],
            followed_at=_dt(row["followed_at"]),
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

        Naming `url`/`ref`/`ref_type` ends `follows_bundled_pin` for good
        (trinity-enterprise#529), even when the value is unchanged: an admin who
        sets the source's target, including to the tag it is already on, owns
        its version from then on. Only the marker goes on such a save; the
        bookkeeping above resets on an actual change alone, because a sync with
        no recorded baseline trusts its first fetch. Here, not in the routers,
        so the PUT route, the `apply` route behind the MCP tools (which passes
        only the fields that differ) and any later caller are all covered.
        Rename, priority and `enabled` leave it alone — a disabled row is simply
        not moved while it is off.

        "Did the target change?" is decided from a read, so the write only lands
        on the target that read saw; if the row was retargeted in between (the
        boot follow is a second writer of `ref`), the change is re-decided
        against the row as it now is. Unscoped, a save naming the ref it last
        saw put the old tag back while keeping the follow's marker and record.
        """
        requested: Dict[str, Any] = {
            k: (int(v) if k == "enabled" else v)
            for k, v in fields.items()
            if k in self._MUTABLE_FIELDS and v is not None
        }
        if not requested:
            return self.get_source(source_id)

        # Bounded: a retry needs another writer to retarget the row between our
        # read and our write, and the only other writer of the target (the boot
        # follow) moves it once per boot.
        for _ in range(3):
            current = self.get_source(source_id)
            if current is None:
                return None
            values = dict(requested)
            if any(
                k in values and values[k] != getattr(current, k)
                for k in self._IDENTITY_FIELDS
            ):
                values.update(_RETARGET_RESET)
            # NAMED, not only changed: putting the tag the row is already on is
            # how an admin holds it there, so it must end following too. The
            # bookkeeping above still resets only on a real change. `apply`
            # passes only the fields whose value differs, so re-applying a
            # declaration is not a pin.
            if any(k in values for k in self._IDENTITY_FIELDS):
                values["follows_bundled_pin"] = 0
            values["updated_at"] = utc_now_iso()

            stmt = (
                update(skill_sources)
                .where(
                    skill_sources.c.id == source_id,
                    *(skill_sources.c[k] == getattr(current, k)
                      for k in self._IDENTITY_FIELDS),
                )
                .values(**values)
            )
            try:
                with get_engine().begin() as conn:
                    landed = conn.execute(stmt).rowcount == 1
            except IntegrityError as exc:
                raise DuplicateSkillSource(
                    "another source already points at that url and ref"
                ) from exc
            if landed:
                # Read after the commit, never inside it: another connection
                # would still see the row as it was.
                return self.get_source(source_id)
            # 0 rows: deleted (the next read says so) or retargeted since the read.
        raise DuplicateSkillSource(
            "the source changed concurrently while it was being updated; retry"
        )

    def follow_bundled_pin(
        self, source_id: str, *, from_ref: str, to_ref: str, url: str
    ) -> Optional[SkillSource]:
        """Move the untouched seeded default from `from_ref` to `to_ref`
        (trinity-enterprise#529). Returns the moved row, or None.

        A compare-and-set: every condition that makes the row eligible is in the
        WHERE, so a stale read cannot move a row an admin just retargeted, and
        when two workers boot together exactly one UPDATE matches — the caller
        writes the audit row only on a win. The version check (forward only)
        is the caller's; this method only refuses to move what it no longer
        recognises.

        Goes through `_RETARGET_RESET` like an admin ref edit: a stale
        `last_commit_sha` would be compared against the new tag and refused as
        `moved_tag`. `follows_bundled_pin` stays 1, so the next pin moves it
        too. None, never a raise, when a custom source already holds
        (url, `to_ref`).
        """
        now = utc_now_iso()
        stmt = (
            update(skill_sources)
            .where(
                skill_sources.c.id == source_id,
                skill_sources.c.ref == from_ref,
                skill_sources.c.url == url,
                skill_sources.c.ref_type == "tag",
                skill_sources.c.is_default == 1,
                skill_sources.c.created_by == SEED_CREATED_BY,
                skill_sources.c.enabled == 1,
                skill_sources.c.follows_bundled_pin == 1,
            )
            .values({
                **_RETARGET_RESET,
                "ref": to_ref,
                "followed_from_ref": from_ref,
                "followed_at": now,
                "updated_at": now,
            })
        )
        try:
            with get_engine().begin() as conn:
                if conn.execute(stmt).rowcount != 1:
                    return None
        except IntegrityError:
            import logging

            logging.getLogger(__name__).warning(
                "skill source %s not moved to %s: another source already "
                "points at that url and ref", source_id, to_ref,
            )
            return None
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
        url: Optional[str] = None,
        ref: Optional[str] = None,
        ref_type: Optional[str] = None,
    ) -> None:
        """Stamp the outcome of a sync attempt.

        Never raises — this runs on the tail of a sync, and losing the
        bookkeeping must not turn a successful clone into a failed operation.
        `last_error` is cleared on success so a stale message can't outlive the
        failure it described.

        `url` / `ref` / `ref_type` are what the sync actually fetched. When
        given, the outcome lands only if the row still points there
        (trinity-enterprise#529). A sync is admitted on a snapshot of the rows,
        so one that started on tag v0.3.0 and finished after the source was
        moved to v0.5.0 — by the boot follow, or by an admin edit — used to
        write v0.3.0's commit onto the v0.5.0 row, and every later sync then
        compared v0.5.0 against it and was refused as `moved_tag`, with no way
        out short of another retarget. A dropped outcome leaves the reset
        bookkeeping in place, so the next sync fetches the new target.
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
        stmt = update(skill_sources).where(skill_sources.c.id == source_id)
        for column, synced in (("url", url), ("ref", ref), ("ref_type", ref_type)):
            if synced is not None:
                stmt = stmt.where(skill_sources.c[column] == synced)
        try:
            with get_engine().begin() as conn:
                if conn.execute(stmt.values(**values)).rowcount == 0:
                    import logging

                    logging.getLogger(__name__).info(
                        "sync outcome for skill source %s dropped: the source "
                        "was retargeted or removed while it synced", source_id,
                    )
        except Exception:  # noqa: BLE001 — bookkeeping is never load-bearing
            import logging

            logging.getLogger(__name__).warning(
                "failed to record sync outcome for skill source %s", source_id,
                exc_info=True,
            )
