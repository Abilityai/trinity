"""
Operator intake (trinity-enterprise#38).

At first-run setup the operator may opt in to "occasionally receive important
security & product updates". On that affirmative consent, their email + company
(+ a few optional basics) are submitted ONCE to an Ability.ai-operated hosted
intake endpoint — a sibling endpoint on the same Cloudflare-fronted intake app
as #1116's in-app bug reporter (`/v1/report-bug` → here `/v1/operator-intake`).

This is identifiable, explicit opt-in contact capture — NOT anonymous telemetry
(that is #758 / trinity-enterprise#12). It therefore fires only on an
affirmative consent checkbox, and never silently.

Discipline:
  * Fire-and-forget — a blocked/failed/air-gapped POST never delays or breaks
    setup. `submit_operator_intake` never raises.
  * At-most-once per install — the `operator_intake_submitted` marker is claimed
    BEFORE the POST, so restarts / re-runs / concurrent uvicorn workers can't
    double-submit. Delivery itself is best-effort; we prefer at-most-once over
    at-least-once (a duplicate lead is worse than a missed one).
  * No PII in logs — the email is the payload, never logged. The credential
    sanitizer (Vector) would mask tokens but not arbitrary emails, so we simply
    never write it to a log line.
"""
import logging
import os
import uuid
from typing import Optional

import httpx

from config import OPERATOR_INTAKE_ENABLED, OPERATOR_INTAKE_URL
from database import db
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# system_settings keys
_INSTALLATION_ID_KEY = "installation_id"
_INTAKE_SUBMITTED_KEY = "operator_intake_submitted"

_HTTP_TIMEOUT_SECONDS = 5.0


def get_or_create_installation_id() -> str:
    """Return this instance's stable installation id, creating it once.

    A random UUID persisted in `system_settings` — not tied to any user, never
    regenerated. It is the once-per-install correlation key for the intake
    submission and the natural seed for future installation telemetry (#758).
    """
    existing = db.get_setting_value(_INSTALLATION_ID_KEY, "")
    if existing:
        return existing
    new_id = str(uuid.uuid4())
    db.set_setting(_INSTALLATION_ID_KEY, new_id)
    return new_id


async def submit_operator_intake(
    *,
    email: str,
    company: Optional[str] = None,
    name: Optional[str] = None,
    role: Optional[str] = None,
    use_case: Optional[str] = None,
) -> None:
    """One-shot, fire-and-forget submission of operator contact info on consent.

    Guarded three ways and NEVER raises (callers schedule it as a background
    task and must not be affected by its outcome):
      1. disabled when OPERATOR_INTAKE_ENABLED is false / DO_NOT_TRACK is set
      2. once-per-install: the submitted marker is claimed before the POST
      3. no email → nothing to submit
    """
    try:
        if not OPERATOR_INTAKE_ENABLED:
            logger.info(
                "Operator intake disabled (OPERATOR_INTAKE_ENABLED / DO_NOT_TRACK)"
                " — nothing submitted."
            )
            return
        if not email:
            return

        # At-most-once claim. Set the marker FIRST so a transient POST failure
        # can't trigger a re-send on a later setup attempt, and two uvicorn
        # workers can't both fire. (get/set is non-atomic, but first-run setup is
        # effectively single-shot, so this is sufficient.)
        if db.get_setting_value(_INTAKE_SUBMITTED_KEY, "false") == "true":
            return
        db.set_setting(_INTAKE_SUBMITTED_KEY, "true")

        payload = {
            "installation_id": get_or_create_installation_id(),
            "email": email,
            "company": company or None,
            "name": name or None,
            "role": role or None,
            "use_case": use_case or None,
            "consent": "security_and_product_updates",
            "trinity_version": os.getenv("GIT_COMMIT_SHORT", "unknown"),
            "submitted_at": utc_now_iso(),
        }

        async with httpx.AsyncClient(timeout=_HTTP_TIMEOUT_SECONDS) as client:
            resp = await client.post(OPERATOR_INTAKE_URL, json=payload)

        # Best-effort delivery: a non-2xx is logged at debug and otherwise
        # ignored. We do NOT roll back the submitted marker — re-sending on the
        # next restart would risk duplicate leads.
        if resp.status_code >= 400:
            logger.debug("Operator intake POST returned HTTP %s", resp.status_code)
        else:
            # Log only the install-id prefix — never the operator's email.
            logger.info(
                "Operator intake submitted (install %s…)",
                payload["installation_id"][:8],
            )
    except Exception as e:  # noqa: BLE001 — fire-and-forget, swallow everything
        logger.debug("Operator intake submission skipped (ignored): %s", type(e).__name__)
