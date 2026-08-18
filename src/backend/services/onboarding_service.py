"""First-run state for the front-desk surface (ent#319, epic ent#54).

Answers one question for the calling user: **is this still the first run** — i.e.
has anyone actually put an agent of their own on this install, or is everything
here something Trinity seeded?

Why this needs a service at all
-------------------------------
Before ent#124, "fresh install" was trivially observable from the browser: zero
non-system agents. Seeding (ent#122/#124 — reaffirmed when ent#322 was closed
not-planned) made that predicate permanently false: a fresh install now comes up
with the bundled system's agents plus Cornelius already running, so the
first-run surface it was gating never appeared again.

The seed is deployed **as the admin user** (``system_seed_service.SEED_OWNER``),
so it is indistinguishable from a human admin's own work in ``audit_log`` — an
actor-based predicate cannot answer this. What CAN answer it is the seed's own
naming contract, which is deterministic: the manifest deploys ``{system}-{short}``
for each declared agent, and Cornelius is seeded under a fixed name. Deriving the
set at read time (rather than recording it at seed time) keeps this a pure reader
— no new persisted state, no migration, and it stays correct for an operator who
points ``TRINITY_DEFAULT_SYSTEM_MANIFEST`` at their own manifest.

Everything here is best-effort and **fails toward "not first run"**: if the
manifest can't be resolved we simply know of no seeded names, and an install with
agents then reads as established. A first-run card that fails to appear is a
missed nudge; one that appears on a mature fleet is a bug the operator cannot
dismiss on behalf of every user.
"""
from __future__ import annotations

import logging
from typing import List, Optional, Set

from database import db
from models import User
from services.cornelius_agent_service import CORNELIUS_AGENT_NAME
from services.system_seed_service import seeded_agent_names as manifest_seeded_agent_names

logger = logging.getLogger(__name__)

# Preferred demonstrator, when it is one of the seeded agents present. Cornelius
# is the second brain the install seeds for exactly this purpose; anything else
# is a fallback so the "Show me" door is never a dead link.
_PREFERRED_DEMO_AGENTS = (CORNELIUS_AGENT_NAME,)


def get_seeded_agent_names() -> Set[str]:
    """Names an out-of-the-box install creates on its own.

    The union of the Cornelius seed name and whatever the default-system
    manifest in force declares (bundled, or the operator's
    `TRINITY_DEFAULT_SYSTEM_MANIFEST`). Each half is owned by the service that
    seeds it, so this composes rather than re-derives. Never raises.
    """
    return {CORNELIUS_AGENT_NAME} | manifest_seeded_agent_names()


def _visible_agents(current_user: User) -> List[dict]:
    """Live, non-system agents this user can see. DB-only (no Docker).

    Deliberately not routed through ``get_accessible_agents``: that helper is
    Docker-backed, and the first-run surface must render truthfully even when the
    Docker socket is slow or an agent container is down — the question is about
    ownership rows, not running processes.
    """
    user_row = db.get_user_by_username(current_user.username) or {}
    is_admin = user_row.get("role") == "admin"
    metadata = db.get_all_agent_metadata(user_row.get("email"))

    visible = []
    for name, meta in (metadata or {}).items():
        if meta.get("is_system"):
            continue
        if is_admin or meta.get("owner_id") == user_row.get("id") or meta.get("is_shared_with_user"):
            visible.append({"name": name, **meta})
    return visible


def _pick_demo_agent(seeded_present: List[str]) -> Optional[str]:
    for preferred in _PREFERRED_DEMO_AGENTS:
        if preferred in seeded_present:
            return preferred
    return sorted(seeded_present)[0] if seeded_present else None


def get_first_run_state(current_user: User) -> dict:
    """Assemble the caller's first-run state. Read-only, DB-only, never raises.

    ``first_run`` is true while every agent the caller can see is one Trinity
    seeded (including the case of no agents at all — an install with seeding
    disabled, which is the pre-ent#124 shape this predicate must keep serving).
    The moment someone creates an agent of their own, the install has been made
    theirs and the front desk stands down for good.
    """
    try:
        seeded = get_seeded_agent_names()
        visible = _visible_agents(current_user)
        visible_names = [a["name"] for a in visible]
        own_agents = [n for n in visible_names if n not in seeded]
        seeded_present = [n for n in visible_names if n in seeded]

        return {
            "first_run": not own_agents,
            "seeded_agents": sorted(seeded_present),
            "own_agent_count": len(own_agents),
            "demo_agent": _pick_demo_agent(seeded_present),
        }
    except Exception:  # pragma: no cover - the surface is a nudge, never a gate
        logger.warning("[onboarding] first-run state unavailable", exc_info=True)
        return {
            "first_run": False,
            "seeded_agents": [],
            "own_agent_count": 0,
            "demo_agent": None,
        }
