"""The instance autonomy dial (trinity-enterprise#641, P12).

`tandem-06-operations.md` §2.3 declares one level per firm — L0 Continuity,
L1 Companion, L2 Delegated classes, L3 Load-bearing judgment, each a superset
of the one below. The level is a **ceiling**: `L2` is the first level at which
anything runs unprompted at all, and below it every graduated ask class reads
`on_request` without losing what it earned.

Two properties of this route, both deliberate:

* **Validated, and registered BEFORE the `/{key}` catch-all** (Invariant #4).
  The level has a closed vocabulary, and the generic PUT would accept `L9` —
  so the key joins the catch-all's blocklist too, the shape `max_parallel_tasks_ceiling`
  (#506), the retention windows (ent#297) and the registry URL (ent#14) already
  have: a settings key whose value has a safe range gets a route that knows the
  range, and the catch-all refuses to be a way around it.
* **Admin AND interactive.** Raising the ceiling is what lets anything run
  unprompted anywhere on the instance — a GRANT, not a use (Invariant #8), and
  `require_admin` alone admits `trinity-system` (its scope is in
  `ADMIN_GATE_SCOPES` and it sets no `agent_name`). The allow-list form is the
  ent#669 precedent.

Reading the level is not a grant: any authenticated principal may ask what the
ceiling is, because a companion that cannot read it cannot tell a person why it
is on-request.
"""
import logging

from fastapi import APIRouter, Depends, HTTPException, Request

from database import db
from dependencies import get_current_user, reject_non_interactive_principal, require_admin
from models import AutonomyDialUpdate, User
from services import autonomy_dial_service
from services.platform_audit_service import AuditEventType, platform_audit_service

logger = logging.getLogger(__name__)
router = APIRouter()


@router.get("/autonomy-dial")
async def get_autonomy_dial(current_user: User = Depends(get_current_user)):
    """The instance level, its canon label, and whether it permits unprompted work."""
    level = autonomy_dial_service.get_level(db)
    return {
        "level": level,
        "label": autonomy_dial_service.LEVEL_LABELS[level],
        "allows_unprompted": autonomy_dial_service.level_allows_unprompted(level),
        "levels": [
            {"level": l, "label": autonomy_dial_service.LEVEL_LABELS[l],
             "allows_unprompted": autonomy_dial_service.level_allows_unprompted(l)}
            for l in autonomy_dial_service.LEVELS
        ],
        "unprompted_from": autonomy_dial_service.UNPROMPTED_FROM,
        "rule_version": autonomy_dial_service.RULE_VERSION,
    }


@router.put("/autonomy-dial")
async def set_autonomy_dial(
    body: AutonomyDialUpdate,
    request: Request,
    current_user: User = Depends(require_admin),
):
    """Move the ceiling. Audited, because "who raised it, to what, when" is the
    one question a later reader will have.

    No fan-out: the level is a read-time conjunct of every seat's verdict
    (`autonomy_dial_service.live_verdict`), so lowering it demotes every class
    on the next read with zero writes, and raising it restores exactly what each
    class had earned — never more.
    """
    reject_non_interactive_principal(current_user)
    previous = autonomy_dial_service.get_level(db)
    try:
        level = autonomy_dial_service.set_level(db, body.level, changed_by=current_user.username)
    except ValueError as e:
        raise HTTPException(status_code=422, detail=str(e))

    await platform_audit_service.log(
        event_type=AuditEventType.CONFIGURATION,
        event_action="autonomy_dial_change",
        source="api",
        actor_user=current_user,
        actor_ip=request.client.host if request.client else None,
        endpoint=str(request.url.path),
        request_id=getattr(request.state, "request_id", None),
        details={"from": previous, "to": level,
                 "allows_unprompted": autonomy_dial_service.level_allows_unprompted(level)},
    )
    logger.info("[autonomy-dial] %s -> %s by %s", previous, level, current_user.username)
    return {
        "level": level,
        "label": autonomy_dial_service.LEVEL_LABELS[level],
        "allows_unprompted": autonomy_dial_service.level_allows_unprompted(level),
        "previous": previous,
    }
