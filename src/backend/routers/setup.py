"""
First-time setup routes for the Trinity backend.

Provides endpoints for initial admin account creation on first launch. These
endpoints require NO authentication and only work before setup is completed
(the `setup_completed` flag self-disables them after the first success).

Setup-token note (trinity-enterprise#49): the earlier flow required the operator
to copy a one-time setup token from the server logs (#1165 / SEC #177) before
setting the admin password. That guarded the first-run window against admin
hijack on an instance reachable by a stranger before setup completes. It has been
**removed** to streamline the common self-hosted, single-operator bring-up. The
tradeoff is explicit and documented as an operator responsibility: an
internet-reachable instance MUST be deployed behind a tunnel/VPN (or otherwise
network-restricted) until first-time setup completes — see
`docs/DEPLOYMENT.md` → Security Recommendations. The endpoint still self-disables
after the first success, so the exposure is limited to the pre-setup window only.
"""
import logging
import re

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from models import SetAdminPasswordRequest
from database import db
from dependencies import hash_password
from services.cornelius_agent_service import cornelius_agent_service
from services.operator_intake_service import submit_operator_intake
from utils.password_validation import validate_password_strength, PASSWORD_REQUIREMENTS_MESSAGE

logger = logging.getLogger(__name__)

# The fixed admin username this endpoint provisions (matches update_user_password
# below). The email captured here is bound to THIS account as its sign-in identity.
_ADMIN_USERNAME = "admin"

# Lightweight email shape check — deliberately permissive (one @, a dot in the
# domain, no spaces). We only need to reject obvious typos; we never send a
# verification mail here (a fresh install has no email provider configured).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")

router = APIRouter(prefix="/api/setup", tags=["setup"])


@router.get("/status")
async def get_setup_status():
    """
    Check if initial setup is complete. No auth required.

    Returns:
        - setup_completed: Whether the admin account has been created.
        - setup_available: Whether setup can be completed right now. Always true
          now that setup writes only to SQLite (the Redis-backed setup token was
          removed in trinity-enterprise#49); kept in the response for backward
          compatibility with older frontends.
    """
    setup_completed = db.get_setting_value('setup_completed', 'false') == 'true'
    return {
        "setup_completed": setup_completed,
        "setup_available": True,
    }


@router.post("/admin-password")
async def set_admin_password(
    data: SetAdminPasswordRequest, request: Request, background_tasks: BackgroundTasks
):
    """
    Create the admin account on first launch. No auth required, only works once.

    Once `setup_completed=true` is set, this endpoint returns 403 forever.

    Security (trinity-enterprise#49): there is no setup token. On an
    internet-reachable instance the operator is responsible for restricting
    network access (tunnel/VPN) until setup completes — see
    `docs/DEPLOYMENT.md` → Security Recommendations.

    Requirements:
    - A valid admin email (becomes the sign-in identity).
    - Password must meet OWASP ASVS 2.1 complexity requirements.
    - Password and confirm_password must match.
    """
    # Check setup not already completed.
    if db.get_setting_value('setup_completed', 'false') == 'true':
        raise HTTPException(
            status_code=403,
            detail="Setup already completed. Password cannot be changed through this endpoint."
        )

    # Validate password complexity (OWASP ASVS 2.1).
    errors = validate_password_strength(data.password)
    if errors:
        # Return generic message — don't reveal which specific rules failed
        # on this unauthenticated endpoint (CSO review finding #1).
        raise HTTPException(
            status_code=400,
            detail=PASSWORD_REQUIREMENTS_MESSAGE,
        )

    if data.password != data.confirm_password:
        raise HTTPException(
            status_code=400,
            detail="Passwords do not match"
        )

    # Admin email is required (trinity-enterprise#49). Validate the shape up-front
    # (before any writes) so a blank/typo'd value surfaces as a clean 400 rather
    # than half-completing setup. A missing field already 422s at the model layer.
    normalized_email = (data.email or "").strip().lower()
    if not normalized_email or not _EMAIL_RE.match(normalized_email):
        raise HTTPException(status_code=400, detail="A valid admin email is required")

    # Hash the password and update admin user.
    hashed_password = hash_password(data.password)

    # Update admin user's password in database (creates the admin row if absent).
    db.update_user_password(_ADMIN_USERNAME, hashed_password)

    # Register the operator email as the admin's sign-in identity (#82 Phase 1).
    # No verification email is sent: a fresh install has no email provider
    # configured (no Resend key), so we cannot deliver a code here. We simply
    # bind the email to the admin account — the operator can then sign in with
    # email + password instead of the fixed 'admin' username.
    email_registered = False
    try:
        db.update_user(_ADMIN_USERNAME, {"email": normalized_email})
        email_registered = True
    except Exception as e:  # never block setup on a profile write
        logger.warning("Failed to register admin email at setup: %s", type(e).__name__)

    # Mark setup as completed.
    db.set_setting('setup_completed', 'true')

    # Seed the default Cornelius agent (ent#107). Now that the admin account
    # exists, provision the bundled Brain-Orb-enabled Cornelius so a fresh install
    # comes up with it present — zero manual steps. Scheduled as a background task
    # so it runs AFTER the response is sent: a container create must never delay or
    # break setup. The service is idempotent, first-run-only, and fresh-install-
    # scoped, so this can never double-provision or surprise an established fleet.
    background_tasks.add_task(cornelius_agent_service.ensure_seeded)

    # Operator intake (trinity-enterprise#38): only on affirmative consent.
    # Scheduled as a background task so it runs AFTER the response is sent — it
    # can never delay or break setup. The service is idempotent (once-per-install)
    # and swallows all errors (air-gapped / blocked / offline).
    if data.consent_updates:
        background_tasks.add_task(
            submit_operator_intake,
            email=normalized_email,
            company=(data.company or "").strip() or None,
            name=(data.name or "").strip() or None,
            role=(data.role or "").strip() or None,
            use_case=(data.use_case or "").strip() or None,
        )

    return {"success": True, "email_registered": email_registered}
