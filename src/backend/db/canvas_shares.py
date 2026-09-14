"""Canvas share links (ent#554).

A share link lets one canvas leave the Workspace at a stable URL. Two scopes,
and the difference is the whole security model:

* ``authorized`` (the default) — the link is a DEEP link. Opening it requires
  signing in, and the server re-checks that the viewer could already see this
  canvas. It reaches "the people who could already see it" and nobody else.
* ``public`` — anyone holding the URL. An explicit, separate, audited choice.

Deliberately its OWN table rather than a typed row in ``agent_public_links``:
nothing in that table's read path filters on ``type``, so a canvas row there
would also be a working public-chat token. See the DDL comment in db/schema.py.

The row is never deleted on revoke, only stamped: a revoked link has to be able
to SAY it was revoked, which it cannot do once the row is gone.
"""

import logging
import secrets
import uuid
from typing import Dict, List, Optional

from sqlalchemy import and_, select, update

from .engine import get_engine
from .tables import agent_canvas_shares
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

SCOPE_AUTHORIZED = "authorized"
SCOPE_PUBLIC = "public"
VALID_SCOPES = (SCOPE_AUTHORIZED, SCOPE_PUBLIC)

# 32 bytes → 256 bits of entropy, the `agent_shared_files.download_token` shape.
# A `public` link's only protection is that the URL cannot be guessed.
_TOKEN_BYTES = 32

_COLUMNS = (
    agent_canvas_shares.c.id,
    agent_canvas_shares.c.agent_name,
    agent_canvas_shares.c.canvas_id,
    agent_canvas_shares.c.token,
    agent_canvas_shares.c.scope,
    agent_canvas_shares.c.created_by,
    agent_canvas_shares.c.created_at,
    agent_canvas_shares.c.expires_at,
    agent_canvas_shares.c.revoked_at,
    agent_canvas_shares.c.last_viewed_at,
    agent_canvas_shares.c.view_count,
)


def normalize_scope(value) -> str:
    """Coerce a stored or supplied scope to a known one, defaulting NARROW.

    An allowlist, never a blocklist (#2396's rule) — and it defaults to
    `authorized` because the failure direction here is a link that reaches
    further than the sharer intended.
    """
    return value if value in VALID_SCOPES else SCOPE_AUTHORIZED


def _row(r) -> Dict:
    return {
        "id": r[0],
        "agent_name": r[1],
        "canvas_id": r[2],
        "token": r[3],
        "scope": normalize_scope(r[4]),
        "created_by": r[5],
        "created_at": r[6],
        "expires_at": r[7],
        "revoked_at": r[8],
        "last_viewed_at": r[9],
        "view_count": r[10] or 0,
    }


class CanvasShareOperations:
    """Share-link database operations (ent#554)."""

    def create_share(self, agent_name: str, canvas_id: str, *, scope: str,
                     created_by: Optional[str] = None,
                     expires_at: Optional[str] = None) -> Dict:
        """Mint a share link. The token is generated here, never supplied."""
        row = {
            "id": uuid.uuid4().hex,
            "agent_name": agent_name,
            "canvas_id": canvas_id,
            "token": secrets.token_urlsafe(_TOKEN_BYTES),
            "scope": normalize_scope(scope),
            "created_by": created_by,
            "created_at": utc_now_iso(),
            "expires_at": expires_at,
            "revoked_at": None,
            "last_viewed_at": None,
            "view_count": 0,
        }
        with get_engine().begin() as conn:
            conn.execute(agent_canvas_shares.insert().values(**row))
        return row

    def get_share_by_token(self, token: str) -> Optional[Dict]:
        """Resolve a token. Returns revoked and expired rows too — the caller
        decides, because "revoked" and "never existed" must read differently to
        the holder of a link while staying indistinguishable to a stranger."""
        if not token:
            return None
        stmt = select(*_COLUMNS).where(agent_canvas_shares.c.token == token)
        with get_engine().connect() as conn:
            r = conn.execute(stmt).first()
        return _row(r) if r else None

    def list_shares(self, agent_name: str, canvas_id: Optional[str] = None,
                    include_revoked: bool = False) -> List[Dict]:
        """Share links for an agent, or for one of its canvases."""
        stmt = select(*_COLUMNS).where(agent_canvas_shares.c.agent_name == agent_name)
        if canvas_id is not None:
            stmt = stmt.where(agent_canvas_shares.c.canvas_id == canvas_id)
        if not include_revoked:
            stmt = stmt.where(agent_canvas_shares.c.revoked_at.is_(None))
        stmt = stmt.order_by(agent_canvas_shares.c.created_at.desc())
        with get_engine().connect() as conn:
            return [_row(r) for r in conn.execute(stmt)]

    def revoke_share(self, agent_name: str, share_id: str) -> bool:
        """Stamp a link revoked. Scoped to the agent, so a caller authorized for
        one agent cannot revoke another's link by id.

        Idempotent-ish: revoking an already-revoked link reports False (nothing
        changed) rather than raising, and the row keeps its FIRST revocation
        time — when it stopped working is a fact, and a second call must not
        rewrite it.
        """
        with get_engine().begin() as conn:
            result = conn.execute(
                update(agent_canvas_shares)
                .where(
                    and_(
                        agent_canvas_shares.c.agent_name == agent_name,
                        agent_canvas_shares.c.id == share_id,
                        agent_canvas_shares.c.revoked_at.is_(None),
                    )
                )
                .values(revoked_at=utc_now_iso())
            )
        return bool(result.rowcount)

    def record_view(self, share_id: str) -> None:
        """Best-effort view bookkeeping. Never raises: a counter that cannot be
        written must not stop someone reading the canvas they were sent."""
        try:
            with get_engine().begin() as conn:
                conn.execute(
                    update(agent_canvas_shares)
                    .where(agent_canvas_shares.c.id == share_id)
                    .values(
                        last_viewed_at=utc_now_iso(),
                        view_count=agent_canvas_shares.c.view_count + 1,
                    )
                )
        except Exception as e:  # noqa: BLE001
            logger.warning("canvas share %s: view not recorded: %s", share_id, e)
