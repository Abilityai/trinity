"""Canvas share links — resolution policy (ent#554).

Three questions, answered in one place so no surface can answer them
differently: is this token real, is the link still live, and may THIS viewer
see it.

**The shared canvas is LIVE, not a snapshot** (AC #3, operator ruling). The
link renders the canvas as it is now, carrying its `updated_at` and its stale
mark, and the view says so. That follows ent#438's model — a canvas is a
surface an agent keeps *current* — and it means a share stores no copy. The
cost is that content can change after you share it, and the mitigation is
revocation, not freezing.

**A revoked link says it was revoked; an unknown one says nothing.** AC #2 asks
for the first, and the uniform-404 contract on token surfaces asks for the
second. Both are satisfiable at once because they are different facts to
different people: someone holding a link that used to work has already been
told the canvas exists, so `revoked` discloses nothing new — while a stranger
guessing tokens must not be able to tell a real-but-revoked token from a
fabricated one. So `revoked` is only ever returned for a token that MATCHED a
row; everything else collapses into the same not-found.
"""

import logging
from typing import Dict, Optional

from database import db
from db.canvas_shares import SCOPE_AUTHORIZED, SCOPE_PUBLIC
from services import canvas_service
from utils.helpers import parse_iso_timestamp, utc_now_iso

logger = logging.getLogger(__name__)


class ShareResolution:
    """Why a share view did or did not render.

    A small vocabulary rather than booleans, because the view has to say
    something different for each and a caller that had to infer the reason
    would end up guessing.
    """
    OK = "ok"
    NOT_FOUND = "not_found"       # unknown token, or a canvas that is gone
    REVOKED = "revoked"           # matched a row that was turned off
    EXPIRED = "expired"           # matched a row past its expiry
    SIGN_IN_REQUIRED = "sign_in_required"   # authorized-scope, anonymous viewer
    NOT_AUTHORIZED = "not_authorized"       # signed in, but cannot see this canvas


def _is_expired(share: Dict, now_iso: Optional[str] = None) -> bool:
    expires = share.get("expires_at")
    if not expires:
        return False
    try:
        return parse_iso_timestamp(expires) <= parse_iso_timestamp(now_iso or utc_now_iso())
    except Exception:  # noqa: BLE001
        # An unparseable expiry is treated as EXPIRED: a link whose lifetime we
        # cannot read is one we cannot promise is still live, and the failure
        # direction on a sharing surface is to stop serving, not to keep going.
        logger.warning("canvas share %s: unparseable expires_at", share.get("id"))
        return True


def viewer_may_see(agent_name: str, canvas_id: str, user) -> bool:
    """May this signed-in principal see this canvas outside the share link?

    The share does not grant access on the `authorized` scope — it POINTS at a
    canvas the viewer could already reach. So this asks the ordinary access
    question and nothing about the link.
    """
    if user is None:
        return False
    try:
        if db.can_user_access_agent(user.username, agent_name):
            return True
    except Exception as e:  # noqa: BLE001
        logger.warning("canvas share: access check failed for %s: %s", agent_name, e)
        return False
    return False


def resolve(token: str, user=None) -> Dict:
    """Resolve a share token into a render decision.

    Returns `{"status": ShareResolution.*, "canvas": <canvas or None>,
    "share": <share row or None>}`. Never raises for an ordinary miss.
    """
    share = db.get_canvas_share_by_token(token)
    if not share:
        return {"status": ShareResolution.NOT_FOUND, "canvas": None, "share": None}

    if share.get("revoked_at"):
        return {"status": ShareResolution.REVOKED, "canvas": None, "share": share}

    if _is_expired(share):
        return {"status": ShareResolution.EXPIRED, "canvas": None, "share": share}

    if share["scope"] == SCOPE_AUTHORIZED:
        if user is None:
            return {"status": ShareResolution.SIGN_IN_REQUIRED, "canvas": None, "share": share}
        if not viewer_may_see(share["agent_name"], share["canvas_id"], user):
            return {"status": ShareResolution.NOT_AUTHORIZED, "canvas": None, "share": share}

    canvas = db.get_agent_canvas(share["agent_name"], share["canvas_id"])
    if not canvas:
        # The canvas was deleted after the link was made. Not-found rather than
        # a distinct "deleted" state: the link is now pointing at nothing, and
        # there is no version of it that would work again.
        return {"status": ShareResolution.NOT_FOUND, "canvas": None, "share": share}

    decorated = canvas_service.decorate([canvas], share["agent_name"])[0]
    db.record_canvas_share_view(share["id"])
    return {"status": ShareResolution.OK, "canvas": decorated, "share": share}


def public_view_payload(resolution: Dict) -> Dict:
    """What a share view returns on success — the canvas plus the facts the
    page has to state.

    `live: True` is not decoration: AC #3 requires the page to say which it is,
    and a client that had to assume would eventually assume wrong.
    """
    canvas = resolution["canvas"]
    share = resolution["share"]
    return {
        "status": ShareResolution.OK,
        "agent_name": share["agent_name"],
        "canvas": canvas,
        "scope": share["scope"],
        "live": True,
        "shared_at": share["created_at"],
    }
