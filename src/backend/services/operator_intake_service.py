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
import re
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

# Only a lowercase snake_case error CODE is ever echoed into a log line — an
# echoed email / free text / control chars are dropped by construction, so the
# no-PII invariant stays LOCALLY enforced and is not delegated to the Worker.
_SAFE_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,63}")


def _safe_error_detail(resp: "httpx.Response") -> str:
    """Best-effort ` (<code>)` from the Worker's {ok:false,error:"<code>"} body.

    Hardened so it can NEVER re-hide the WARNING and NEVER leak PII:
      * broad `except Exception` — a non-JSON/empty/malformed body returns "" and
        never raises (a raise would fall through to the outer except and downgrade
        this WARNING to INFO — the #1444 silent-swallow class).
      * `isinstance(body, dict)` — a JSON list/string/number degrades to "".
      * coded-shape whitelist — only a lowercase snake_case code is echoed;
        anything with @/spaces/uppercase/control chars (an echoed email or free
        text) is dropped, keeping the module's no-PII invariant LOCAL.
    """
    try:
        body = resp.json()
    except Exception:  # noqa: BLE001 — non-JSON / empty / malformed
        return ""
    if not isinstance(body, dict):
        return ""
    err = body.get("error")
    if isinstance(err, str) and _SAFE_ERROR_CODE.fullmatch(err):
        return f" ({err})"
    return ""


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

        # Best-effort delivery. Gate success on TRUE 2xx: a 3xx (redirect) or
        # 4xx/5xx means the submission did NOT land — surface it at WARNING so a
        # hosted-endpoint outage can't hide (#1593). The marker is NOT rolled
        # back (re-send risks a duplicate lead; at-most-once is deliberate).
        if 200 <= resp.status_code < 300:
            # Log only the install-id prefix — never the operator's email.
            logger.info(
                "Operator intake submitted (install %s…)",
                payload["installation_id"][:8],
            )
        else:
            logger.warning(
                "Operator intake POST returned HTTP %s%s",
                resp.status_code,
                _safe_error_detail(resp),
            )
    except Exception as e:  # noqa: BLE001 — fire-and-forget, swallow everything
        # Connect/timeout/TLS/DNS failure. An outage CAN raise (Worker removed →
        # connection refused, hung edge → read timeout), not only return a 404 —
        # log at INFO (prod root=INFO) so it's visible once, at-most-once per
        # install, without WARNING's "platform broken" semantics or alarming a
        # deliberately-offline install. Only the exception TYPE, never the email.
        logger.info("Operator intake submission skipped (ignored): %s", type(e).__name__)
