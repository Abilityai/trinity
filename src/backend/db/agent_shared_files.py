"""
agent_shared_files database operations (amazing-file-outbound, FILES-001).

Thin CRUD over the table. No business logic here — that belongs in
services/agent_shared_files_service.py.

MVP scope (Step 3):
- create        — insert a new shared-file row
- get_by_token  — token-based lookup (Step 4 download endpoint will use this)
- total_bytes_for_agent — quota enforcement helper

Methods needed by later steps (list_by_agent, revoke, mark_downloaded,
delete_expired) are added later.

One-time link semantics are deferred (see amazing-file-outbound.md §9).
The `one_time` / `consumed_at` columns exist in the schema but are not
written or read by this layer — kept so the feature can be re-enabled
without another migration.

Converted from raw sqlite3 to SQLAlchemy Core (#300) so it runs unchanged
on both SQLite and PostgreSQL. Time-window filters use Python-computed
ISO-Z cutoffs (`utc_now_iso()` / `iso_cutoff()`) bound as values instead
of SQLite's `datetime('now', ...)`, per architecture Invariant #16.
"""

from typing import Optional, Dict, Any

from sqlalchemy import select, insert, update, delete, func, or_, and_

from .engine import get_engine
from .tables import agent_shared_files, portal_file_dismissals
from utils.helpers import utc_now_iso, iso_cutoff


class AgentSharedFilesOperations:
    """CRUD for agent_shared_files."""

    # -------------------------------------------------------------------------
    # Write
    # -------------------------------------------------------------------------

    def create(
        self,
        *,
        file_id: str,
        agent_name: str,
        filename: str,
        stored_filename: str,
        size_bytes: int,
        mime_type: Optional[str],
        download_token: str,
        created_by: str,
        created_at: str,
        expires_at: str,
        addressed_to_email: Optional[str] = None,
        addressed_to_channel: Optional[str] = None,
        audience_source: Optional[str] = None,
    ) -> str:
        """Insert a new shared-file row. Returns the file_id.

        The addressee columns (ent#549) default to NULL = the owner only, so a
        caller that says nothing can never widen who sees a file.
        """
        with get_engine().begin() as conn:
            conn.execute(
                insert(agent_shared_files).values(
                    id=file_id,
                    agent_name=agent_name,
                    filename=filename,
                    stored_filename=stored_filename,
                    size_bytes=size_bytes,
                    mime_type=mime_type,
                    download_token=download_token,
                    created_by=created_by,
                    created_at=created_at,
                    expires_at=expires_at,
                    addressed_to_email=addressed_to_email,
                    addressed_to_channel=addressed_to_channel,
                    audience_source=audience_source,
                )
            )
            return file_id

    # -------------------------------------------------------------------------
    # Read
    # -------------------------------------------------------------------------

    def get_by_id(self, file_id: str) -> Optional[Dict[str, Any]]:
        with get_engine().connect() as conn:
            row = conn.execute(
                select(agent_shared_files).where(agent_shared_files.c.id == file_id)
            ).mappings().first()
            return dict(row) if row else None

    def get_by_token(self, download_token: str) -> Optional[Dict[str, Any]]:
        with get_engine().connect() as conn:
            row = conn.execute(
                select(agent_shared_files).where(
                    agent_shared_files.c.download_token == download_token
                )
            ).mappings().first()
            return dict(row) if row else None

    def mark_downloaded(self, file_id: str) -> None:
        """Increment download_count + bump last_downloaded_at."""
        with get_engine().begin() as conn:
            conn.execute(
                update(agent_shared_files)
                .where(agent_shared_files.c.id == file_id)
                .values(
                    download_count=agent_shared_files.c.download_count + 1,
                    last_downloaded_at=utc_now_iso(),
                )
            )

    def revoke(self, file_id: str) -> bool:
        """
        Mark a share as revoked. Idempotent — a second revoke is a no-op.
        Returns True if a row transitioned from active to revoked.
        """
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_shared_files)
                .where(
                    and_(
                        agent_shared_files.c.id == file_id,
                        agent_shared_files.c.revoked_at.is_(None),
                    )
                )
                .values(revoked_at=utc_now_iso())
            )
            return result.rowcount > 0

    def delete_expired_and_revoked(self, revoke_grace_hours: int = 24) -> list:
        """
        Delete rows where the share is:
          - expired (expires_at < now) — any state, OR
          - revoked more than `revoke_grace_hours` ago

        Returns a list of `stored_filename` values whose rows were
        deleted, so the caller can unlink the on-disk files under
        /data/agent-files/. Called by the cleanup service (C4 / Step 7).
        """
        now = utc_now_iso()
        grace_cutoff = iso_cutoff(hours=revoke_grace_hours)
        cond = or_(
            agent_shared_files.c.expires_at < now,
            and_(
                agent_shared_files.c.revoked_at.is_not(None),
                agent_shared_files.c.revoked_at < grace_cutoff,
            ),
        )
        with get_engine().begin() as conn:
            rows = conn.execute(
                select(
                    agent_shared_files.c.id,
                    agent_shared_files.c.stored_filename,
                ).where(cond)
            ).mappings().all()
            if not rows:
                return []
            stored = [row["stored_filename"] for row in rows]
            ids = [row["id"] for row in rows]
            conn.execute(
                delete(agent_shared_files).where(agent_shared_files.c.id.in_(ids))
            )
            # #2582 — the per-viewer dismissals of the shares we just purged, in
            # the SAME transaction (this whole block is one `begin()`), so the
            # rows cannot survive the ids they name.
            conn.execute(
                delete(portal_file_dismissals).where(
                    portal_file_dismissals.c.file_id.in_(ids)
                )
            )
            return stored

    def delete_for_agent(self, agent_name: str) -> list:
        """
        Delete every shared-file row for an agent and return the
        `stored_filename` values so the caller can unlink the files
        on disk.

        Used by the agent-delete handler. We can't rely on the FK
        `ON DELETE CASCADE` because the platform's SQLite connections
        don't `PRAGMA foreign_keys = ON`; deletion of child rows is
        done explicitly everywhere else in the codebase too
        (`rename_agent` in `db/agent_settings/metadata.py` follows the
        same manual-cascade pattern).
        """
        with get_engine().begin() as conn:
            stored = [
                row["stored_filename"]
                for row in conn.execute(
                    select(agent_shared_files.c.stored_filename).where(
                        agent_shared_files.c.agent_name == agent_name
                    )
                ).mappings()
            ]
            conn.execute(
                delete(agent_shared_files).where(
                    agent_shared_files.c.agent_name == agent_name
                )
            )
            # #2582 — the other purge path, and the one the AgentRef
            # registration exists for. Deleting the shares without this leaves
            # every dismissal of them orphaned with nothing to join back to.
            conn.execute(
                delete(portal_file_dismissals).where(
                    portal_file_dismissals.c.agent_name == agent_name
                )
            )
            return stored

    # One projection for both listings below, so the owner's panel and a
    # viewer's tab cannot drift in what a row carries.
    _LISTING_COLUMNS = (
        agent_shared_files.c.id,
        agent_shared_files.c.agent_name,
        agent_shared_files.c.filename,
        agent_shared_files.c.stored_filename,
        agent_shared_files.c.size_bytes,
        agent_shared_files.c.mime_type,
        agent_shared_files.c.download_token,
        agent_shared_files.c.created_by,
        agent_shared_files.c.created_at,
        agent_shared_files.c.expires_at,
        agent_shared_files.c.revoked_at,
        agent_shared_files.c.download_count,
        agent_shared_files.c.last_downloaded_at,
        agent_shared_files.c.addressed_to_email,
        agent_shared_files.c.addressed_to_channel,
        agent_shared_files.c.audience_source,
    )

    def list_active_for_agent(self, agent_name: str) -> list:
        """
        Active (non-revoked, non-expired) shares for an agent,
        newest first. Used by the Sharing panel.

        The OPERATOR question — everything this agent has out. Never the read
        behind a client-facing surface: that is `list_active_for_viewer`.
        """
        now = utc_now_iso()
        stmt = (
            select(*self._LISTING_COLUMNS)
            .where(
                and_(
                    agent_shared_files.c.agent_name == agent_name,
                    agent_shared_files.c.revoked_at.is_(None),
                    agent_shared_files.c.expires_at > now,
                )
            )
            .order_by(agent_shared_files.c.created_at.desc())
        )
        with get_engine().connect() as conn:
            return [dict(row) for row in conn.execute(stmt).mappings()]

    def list_active_for_viewer(
        self,
        agent_name: str,
        viewer_email: Optional[str],
        *,
        include_owner_only: bool = False,
    ) -> list:
        """Active shares of an agent that are FOR this person, newest first (ent#549).

        The Workspace question, and deliberately not `list_active_for_agent`
        with a filter bolted on afterwards: a read that loads every share and
        drops some in Python has already put another person's download token in
        this process one edit away from the response (the ent#365 FR-2 lesson).
        Narrowed in the query.

        ``include_owner_only`` adds the rows addressed to nobody at all — NULL
        email AND NULL channel — which is what a schedule's, an operator chat's
        or a pre-column row's file is. Only the agent's owner passes it. A row
        addressed to a CHANNEL identity with no email is not owner-only: it is
        somebody's, just nobody with a Files tab, and it stays on the owner's
        panel.

        An empty ``viewer_email`` matches nothing rather than everything — a
        caller who cannot be identified must not inherit the unaddressed rows.
        ``viewer_email`` is compared as given: the caller normalises, with the
        same function the writers use (`utils/addressee.py::normalize_addressee_email`,
        re-exported by `turn_audience`).
        """
        audiences = []
        if viewer_email:
            audiences.append(agent_shared_files.c.addressed_to_email == viewer_email)
        if include_owner_only:
            audiences.append(
                and_(
                    agent_shared_files.c.addressed_to_email.is_(None),
                    agent_shared_files.c.addressed_to_channel.is_(None),
                )
            )
        if not audiences:
            return []
        now = utc_now_iso()
        stmt = (
            select(*self._LISTING_COLUMNS)
            .where(
                and_(
                    agent_shared_files.c.agent_name == agent_name,
                    agent_shared_files.c.revoked_at.is_(None),
                    agent_shared_files.c.expires_at > now,
                    or_(*audiences),
                )
            )
            .order_by(agent_shared_files.c.created_at.desc())
        )
        with get_engine().connect() as conn:
            return [dict(row) for row in conn.execute(stmt).mappings()]

    def total_bytes_for_agent(self, agent_name: str) -> int:
        """
        Sum of size_bytes for rows that still count toward the quota:
        not revoked and not expired.
        """
        now = utc_now_iso()
        stmt = select(
            func.coalesce(func.sum(agent_shared_files.c.size_bytes), 0)
        ).where(
            and_(
                agent_shared_files.c.agent_name == agent_name,
                agent_shared_files.c.revoked_at.is_(None),
                agent_shared_files.c.expires_at > now,
            )
        )
        with get_engine().connect() as conn:
            row = conn.execute(stmt).first()
            return int(row[0]) if row else 0
