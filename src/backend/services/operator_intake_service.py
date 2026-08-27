"""
Operator intake (trinity-enterprise#38).

The operator may opt in to "occasionally receive important security & product
updates". On that affirmative consent, their email + company (+ a few optional
basics) are submitted ONCE to an Ability.ai-operated hosted intake endpoint — a
sibling endpoint on the same Cloudflare-fronted intake app as #1116's in-app
bug reporter (`/v1/report-bug` → here `/v1/operator-intake`).

This is identifiable, explicit opt-in contact capture — NOT anonymous telemetry
(that is #758 / trinity-enterprise#12). It therefore fires only on an
affirmative consent action, and never silently.

Two consent surfaces:
  1. First-run welcome form (`routers/setup.py`) — the original surface. Since
     abilityai/trinity#2385 refuses that form on any install with a
     pre-provisioned admin, this covers only fresh empty-admin installs.
  2. Settings home (ent#463) — a durable admin-only surface added so an operator
     can opt in (or out) after first-run, on installs where the welcome form
     never renders.

Both routes converge on `submit_operator_intake` and preserve the at-most-once
marker, so the two surfaces cannot double-submit.

Discipline:
  * Fire-and-forget — a blocked/failed/air-gapped POST never delays or breaks
    setup. `submit_operator_intake` never raises.
  * At-most-once per install — the `operator_intake_submitted` marker is claimed
    BEFORE the POST, so restarts / re-runs / concurrent uvicorn workers can't
    double-submit. Delivery itself is best-effort; we prefer at-most-once over
    at-least-once (a duplicate lead is worse than a missed one).
  * Opt-out (ent#463) does NOT roll back the submitted marker. It sets a durable
    consent-off setting; if a future feature ever adds a re-send path, it must
    gate on the marker independently, so an operator who later opts out cannot
    have a fresh record silently re-sent.
  * No PII in logs — the email is the payload, never logged. The credential
    sanitizer (Vector) would mask tokens but not arbitrary emails, so we simply
    never write it to a log line.
"""
import logging
import os
import re
import uuid
from typing import Dict, Optional

import httpx

from config import OPERATOR_INTAKE_ENABLED, OPERATOR_INTAKE_URL
from database import db
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# system_settings keys
_INSTALLATION_ID_KEY = "installation_id"
_INTAKE_SUBMITTED_KEY = "operator_intake_submitted"
# ent#463: additional keys for the Settings surface. Timestamped so the panel
# can show honest state (when it was submitted / when consent was recorded).
# `operator_intake_submitted_at` is written on ANY successful submission path
# from this point forward; legacy markers set before this key existed report as
# NULL and the UI shows "date unknown" rather than lying.
KEY_SUBMITTED_AT = "operator_intake_submitted_at"
# ent#463: durable consent state — true when the operator has opted in via
# Settings (or first-run) and NOT subsequently opted out. Default-off. A
# submitted install with `consent_enabled=false` is the "declined future
# updates" state — the record was sent, but any hypothetical future re-send
# path must gate on this.
KEY_CONSENT_ENABLED = "operator_intake_consent_enabled"
KEY_CONSENT_AT = "operator_intake_consent_at"

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
        submitted_at = utc_now_iso()
        db.set_setting(_INTAKE_SUBMITTED_KEY, "true")
        # ent#463: timestamp the submission so the Settings panel can show honest
        # "opted in on {date}" state. Written beside the marker so the two can't
        # drift for a caller that only reads this file.
        db.set_setting(KEY_SUBMITTED_AT, submitted_at)

        payload = {
            "installation_id": get_or_create_installation_id(),
            "email": email,
            "company": company or None,
            "name": name or None,
            "role": role or None,
            "use_case": use_case or None,
            "consent": "security_and_product_updates",
            "trinity_version": os.getenv("GIT_COMMIT_SHORT", "unknown"),
            "submitted_at": submitted_at,
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


# ---------------------------------------------------------------------------
# ent#463 — Settings surface (durable consent + status readback)
# ---------------------------------------------------------------------------

def is_hard_disabled() -> bool:
    """Config/air-gap kill switch (``OPERATOR_INTAKE_ENABLED`` / ``DO_NOT_TRACK``).

    Same shape as the telemetry-sharing hard-disable so the Settings panels agree
    on how to render an air-gapped install.
    """
    return not OPERATOR_INTAKE_ENABLED


def is_consent_enabled() -> bool:
    """Does the operator currently consent to being contacted? Default-off.

    NOT the same as `is_already_submitted()`. A submitted install with
    `consent_enabled=false` is the "declined future updates" state — the record
    was sent (and cannot be locally recalled), but any hypothetical future
    re-send path must gate on this flag independently.
    """
    try:
        return db.get_setting_value(KEY_CONSENT_ENABLED, "false") == "true"
    except Exception:  # pragma: no cover — read failure ⇒ safe default
        return False


def is_already_submitted() -> bool:
    """Has the at-most-once intake POST already fired for this install?"""
    try:
        return db.get_setting_value(_INTAKE_SUBMITTED_KEY, "false") == "true"
    except Exception:  # pragma: no cover — read failure ⇒ safe default
        return False


def get_status() -> Dict:
    """Operator-facing status for the Settings panel (ent#463).

    Combines the three orthogonal state axes the UI needs to render an honest
    view:
      * `hard_disabled` — the env kill switch overrides everything else
      * `already_submitted` (+ `submitted_at`) — has the at-most-once fired?
      * `enabled` (+ `consent_at`) — durable consent flag

    A legacy install that had the marker set before ent#463 shipped will report
    `already_submitted=true` with `submitted_at=None`; the panel must render
    "date unknown" rather than lie.
    """
    return {
        "enabled": is_consent_enabled(),
        "hard_disabled": is_hard_disabled(),
        "already_submitted": is_already_submitted(),
        "submitted_at": db.get_setting_value(KEY_SUBMITTED_AT, None),
        "consent_at": db.get_setting_value(KEY_CONSENT_AT, None),
        "intake_url": OPERATOR_INTAKE_URL,
    }


def set_consent(enabled: bool) -> Dict:
    """Record (or revoke) the durable consent flag (ent#463).

    Writing consent=true here is intent only: it does NOT itself submit — the
    caller is responsible for scheduling the submission when the operator has
    also provided an email and the install has not already submitted. Writing
    consent=false is a durable decline; it does NOT roll back the
    at-most-once marker, since the record has already been sent externally.
    """
    db.set_setting(KEY_CONSENT_ENABLED, "true" if enabled else "false")
    db.set_setting(KEY_CONSENT_AT, utc_now_iso())
    return get_status()


class SettingsIntakeResult:
    """Enum-ish outcome codes for the Settings submit path (ent#463).

    Kept explicit instead of raising so the router can map outcomes to HTTP
    status codes without a try/except pyramid, and so a test can assert the
    exact code path without stubbing the network.
    """
    SUBMITTED = "submitted"                # POST scheduled successfully
    HARD_DISABLED = "hard_disabled"        # env kill switch
    ALREADY_SUBMITTED = "already_submitted"  # marker already set
    MISSING_EMAIL = "missing_email"        # cannot submit without a contact


async def submit_from_settings(
    *,
    email: str,
    company: Optional[str] = None,
    name: Optional[str] = None,
    role: Optional[str] = None,
    use_case: Optional[str] = None,
) -> str:
    """Settings-surface submit (ent#463) — the second producer, at-most-once.

    Returns a `SettingsIntakeResult` code. Never raises. Delegates the actual
    POST to `submit_operator_intake`, so both surfaces converge on the same
    hosted intake, the same payload shape, and the same marker semantics — no
    second intake client, no forked payload (AC #3).
    """
    if is_hard_disabled():
        return SettingsIntakeResult.HARD_DISABLED
    if is_already_submitted():
        return SettingsIntakeResult.ALREADY_SUBMITTED
    if not (email or "").strip():
        return SettingsIntakeResult.MISSING_EMAIL

    await submit_operator_intake(
        email=email.strip(),
        company=(company or "").strip() or None,
        name=(name or "").strip() or None,
        role=(role or "").strip() or None,
        use_case=(use_case or "").strip() or None,
    )
    return SettingsIntakeResult.SUBMITTED
