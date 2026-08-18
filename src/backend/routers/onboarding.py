"""First-run state for the front-desk surface (ent#319, epic ent#54).

One read: is this still the first run for the calling user, and which seeded
agent can demonstrate the primitive. OSS-core and un-gated — this is the open
install's front door (the issue's declared gating shape).

Thin by construction (Invariant #1): the predicate and the seeded-name
derivation live in `services/onboarding_service.py`.
"""
from __future__ import annotations

from fastapi import APIRouter, Depends

from dependencies import get_current_user
from models import FirstRunState, User
from services import onboarding_service

router = APIRouter(prefix="/api/onboarding", tags=["onboarding"])


@router.get("/first-run", response_model=FirstRunState)
def get_first_run(current_user: User = Depends(get_current_user)):
    """Whether the caller still sees a seed-only install, plus the agent the
    "Show me" door should open. Read-only; never raises (a failure reads as
    "not first run", so the card stays hidden rather than nagging a live fleet).
    """
    return onboarding_service.get_first_run_state(current_user)
