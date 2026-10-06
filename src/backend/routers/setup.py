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

#2381 narrows that window to where ent#49's reasoning actually holds. ent#49
priced the tradeoff on the premise *there is no admin yet, so there is nothing
to hijack* — true for a blank-`ADMIN_PASSWORD` install, false for every install
that boots with `ADMIN_PASSWORD` set, where `_ensure_admin_user` has already
created a real admin. This endpoint now refuses whenever a usable admin account
exists, so it provisions the first account and can never overwrite an existing
one. The wizard therefore still renders for installs that genuinely have no way
in, and disappears for installs where it had nothing left to do.

trinity-enterprise#580 makes that first case a product path, not a leftover: a
marketplace one-click image boots with `ADMIN_PASSWORD` deliberately blank
(`ADMIN_PASSWORD_SOURCE=browser`, set only by the image's first boot), so the
first person to open the instance creates the admin here — no terminal. #2381's
invariant is untouched: this still never renders over an existing admin; the
marketplace path simply no longer pre-provisions one. The residual — the window
between instance creation and that first visit, in which anyone who finds the
address can claim it — was accepted on 2026-09-10 as the operator's
responsibility (the instance is empty then, and a squatted one can be destroyed).
See `docs/DEPLOYMENT.md` → Security Recommendations.

That acceptance covers the marketplace only, so nothing else may become
claimable by it. Only `docker-compose.hosted.yml` renders a blank
`ADMIN_PASSWORD` (prod keeps `:?`), and it forwards
`ADMIN_PASSWORD_SOURCE=${ADMIN_PASSWORD_SOURCE:-unset}`. A blank password with
`ADMIN_PASSWORD_SOURCE=unset` is therefore a HAND-RUN hosted compose — not the
marketplace (start.sh writes `browser`) — and this endpoint refuses it (403),
after the #2381 existing-admin check and before any password work. An ABSENT
variable (the dev compose, ent#49's blank dev install) and `browser` still pass.

#3004 (PROV-018) adds `ADMIN_PASSWORD_SOURCE=instance-id` for the AWS image, where
Marketplace review rejects an open first-visitor claim: the first admin must prove
control of the instance with its EC2 instance ID. Provisioning writes that ID to
`/data/setup-claim` (0600, owner 1000:1000); this router reads it and never
queries IMDS. The check runs after the existing-admin refusal and before any
password work, is rate-limited per client IP, fails closed on a missing or empty
claim file, and answers every failure with one generic 403. The file is deleted
once setup succeeds. Email is optional on this path only (no PII required).
"""
import hmac
import logging
import os
import re
import uuid
from pathlib import Path

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request

from models import SetAdminPasswordRequest
from database import db
from dependencies import hash_password
from services.system_seed_service import ensure_first_run_seeded
from services.system_agent_service import system_agent_service
from services.operator_intake_service import submit_operator_intake
from utils.password_validation import validate_password_strength, PASSWORD_REQUIREMENTS_MESSAGE
from utils.admin_identity import admin_username, is_usable_password_hash
from routers.auth import check_login_rate_limit, record_login_attempt
from routers.public import _get_client_ip

logger = logging.getLogger(__name__)

# Lightweight email shape check — deliberately permissive (one @, a dot in the
# domain, no spaces). We only need to reject obvious typos; we never send a
# verification mail here (a fresh install has no email provider configured).
_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s.]+$")

# #3004 (PROV-018): the instance-ID claim. Written by provisioning, deleted here
# after the first admin is created.
_CLAIM_MODE = "instance-id"
_SETUP_CLAIM_PATH = Path("/data/setup-claim")
# The one answer for every claim failure (wrong, missing, or no claim on file),
# so a response never says which it was.
_CLAIM_MISMATCH = "That instance ID does not match this server."

router = APIRouter(prefix="/api/setup", tags=["setup"])


def _claim_required():
    """`"instance-id"` when this install's first admin needs the claim, else None."""
    return _CLAIM_MODE if os.getenv("ADMIN_PASSWORD_SOURCE") == _CLAIM_MODE else None


def _normalise_claim(value: str) -> bytes:
    # Bytes for hmac.compare_digest, which raises TypeError on non-ASCII str.
    return value.strip().lower().encode()


def _check_claim(claim_code, request: Request) -> None:
    """Refuse (403) unless `claim_code` matches the stored instance ID.

    Per-IP only, on the login bucket: an account-style bucket would be one
    global key, letting anyone lock the owner out of their own claim.
    """
    client_ip = _get_client_ip(request)
    check_login_rate_limit(client_ip)
    try:
        expected = _normalise_claim(_SETUP_CLAIM_PATH.read_text())
    except (OSError, UnicodeDecodeError):
        expected = b""
    if not expected:
        # Warning, not error: an unauthenticated caller can trigger it at will.
        logger.warning("Setup claim file missing, empty or unreadable — refusing first-run setup")
    given = _normalise_claim(claim_code or "")
    if not expected or not hmac.compare_digest(given, expected):
        record_login_attempt(client_ip, success=False)
        raise HTTPException(status_code=403, detail=_CLAIM_MISMATCH)
    record_login_attempt(client_ip, success=True)


def _take_claim() -> Path:
    """Atomically take the claim file so one request, and only one, proceeds.

    Two requests with the right ID both pass `_check_claim`; `rename` succeeds
    for exactly one of them. The loser gets the same generic 403.
    """
    taken = _SETUP_CLAIM_PATH.with_name(f"{_SETUP_CLAIM_PATH.name}.{uuid.uuid4().hex}")
    try:
        _SETUP_CLAIM_PATH.rename(taken)
    except OSError:
        raise HTTPException(status_code=403, detail=_CLAIM_MISMATCH)
    return taken


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
        - claim_required: `"instance-id"` when the first admin must also give
          the EC2 instance ID (#3004), else null. Never carries the value.
    """
    setup_completed = db.get_setting_value('setup_completed', 'false') == 'true'
    return {
        "setup_completed": setup_completed,
        "setup_available": True,
        "claim_required": _claim_required(),
    }


async def _deploy_system_agent() -> None:
    """Deploy the system agent after setup (#3237). Never raises: setup has
    already succeeded, and the next backend start retries."""
    try:
        result = await system_agent_service.ensure_deployed()
        logger.info("System agent after setup: %s - %s", result.get("action"), result.get("message"))
    except Exception as e:  # noqa: BLE001
        logger.error("System agent deploy after setup failed: %s", e)


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
    # Refuse if this install already HAS a usable admin account (#2381).
    #
    # This runs FIRST, before the flag and before any password work, and it is
    # the actual security boundary. The flag below is derived state that can be
    # — and on every fresh install was — wrong: `setup_completed` stayed false
    # while `_ensure_admin_user` had already created a real admin from
    # `ADMIN_PASSWORD`, so this endpoint would happily overwrite that admin's
    # password hash and bind a stranger's email to it. The endpoint's own
    # precondition ("nobody can log in yet") is now authoritative.
    #
    # Ordering matters for two more reasons: password hashing below is bcrypt
    # (deliberately expensive) on an unauthenticated, unrate-limited route, and
    # a refusal must not be distinguishable by how long it took.
    #
    # Fails CLOSED: a DB read error refuses rather than falling through to the
    # flag. The opposite direction would restore the vulnerability on exactly
    # the transient conditions an attacker can retry against.
    try:
        existing_admin = db.get_user_by_username(admin_username())
        admin_provisioned = existing_admin is not None and is_usable_password_hash(
            existing_admin.get("password")
        )
    except Exception as e:
        logger.error(
            "Setup admin-existence check failed (%s) — refusing first-run setup",
            type(e).__name__,
        )
        admin_provisioned = True

    if admin_provisioned:
        raise HTTPException(
            status_code=403,
            detail=(
                "This instance already has an administrator account. "
                "Sign in with its password; this endpoint only provisions the "
                "very first account."
            ),
        )

    # ent#580 backstop: a blank password is claimable only where it was meant to
    # be. `unset` is what the hosted compose renders when nothing opted in (see
    # module docstring); absent (dev compose) and `browser` fall through.
    if not os.getenv("ADMIN_PASSWORD") and os.getenv("ADMIN_PASSWORD_SOURCE") == "unset":
        raise HTTPException(
            status_code=403,
            detail=(
                "This instance has no admin password and was not set up to be "
                "claimed in the browser. Set ADMIN_PASSWORD in .env and restart "
                "Trinity, or run ./scripts/deploy/start.sh --hosted."
            ),
        )

    # #3004: the instance-ID claim, before any password work (bcrypt is
    # deliberately expensive and this route is unauthenticated).
    claim_mode = _claim_required()
    if claim_mode:
        _check_claim(data.claim_code, request)

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
    # #3004: on the instance-id path a blank email is allowed (AWS review: no PII
    # required); the admin then signs in with the username + password. A given
    # email is still shape-checked.
    normalized_email = (data.email or "").strip().lower()
    if (normalized_email or not claim_mode) and not _EMAIL_RE.match(normalized_email):
        raise HTTPException(status_code=400, detail="A valid admin email is required")

    # #3004: take the claim only now, after every 400 above, so a rejected
    # password or email leaves it in place for the owner's retry. If the writes
    # below raise, it is put back for the same reason.
    taken_claim = _take_claim() if claim_mode else None
    try:
        # Hash the password and update admin user.
        hashed_password = hash_password(data.password)

        # Update admin user's password in database (creates the admin row if absent).
        # `admin_username()`, not a hardcoded "admin" (#2381): `_ensure_admin_user`
        # honours ADMIN_USERNAME, so on an `ADMIN_USERNAME=root` install the literal
        # made this call miss the real admin and INSERT a *second* role='admin'
        # account (update_user_password upserts) instead of updating the first.
        db.update_user_password(admin_username(), hashed_password)
    except BaseException:
        if taken_claim is not None:
            try:
                taken_claim.rename(_SETUP_CLAIM_PATH)
            except OSError as e:
                logger.warning("Could not restore the setup claim file: %s", type(e).__name__)
        raise

    # Register the operator email as the admin's sign-in identity (#82 Phase 1).
    # No verification email is sent: a fresh install has no email provider
    # configured (no Resend key), so we cannot deliver a code here. We simply
    # bind the email to the admin account — the operator can then sign in with
    # email + password instead of the fixed 'admin' username.
    email_registered = False
    if normalized_email:
        try:
            db.update_user(admin_username(), {"email": normalized_email})
            email_registered = True
        except Exception as e:  # never block setup on a profile write
            logger.warning("Failed to register admin email at setup: %s", type(e).__name__)

    # Mark setup as completed.
    db.set_setting('setup_completed', 'true')

    # #3004: the instance ID stops being a credential once the admin exists.
    # A failed delete is harmless (the endpoint now refuses before reading it,
    # and the taken copy is never read again), so it is logged and never fails
    # setup.
    if taken_claim is not None:
        try:
            taken_claim.unlink(missing_ok=True)
        except OSError as e:
            logger.warning("Could not delete the setup claim file: %s", type(e).__name__)

    # First-run seeding: the default Cornelius agent (ent#107) plus the default
    # system manifest (trinity-enterprise#124), sequenced under ONE persisted
    # freshness verdict. Now that the admin account exists, provision the bundled
    # Brain-Orb-enabled Cornelius and the starter fleet so a fresh install comes up
    # working — zero manual steps. Scheduled as a background task so it runs AFTER
    # the response is sent: container creates must never delay or break setup. Both
    # seeders are idempotent, first-run-only, and fresh-install-scoped, so this can
    # never double-provision or surprise an established fleet.
    background_tasks.add_task(ensure_first_run_seeded)

    # #3237: the system agent needs the admin as its owner. Its only other
    # deploy attempt runs at backend startup, which on a fresh install is before
    # this endpoint, so without this it stayed missing until the next restart.
    # Same background-task rule as the seed pass above; ensure_deployed is
    # idempotent, so a later restart stays a no-op.
    background_tasks.add_task(_deploy_system_agent)

    # Operator intake (trinity-enterprise#38): only on affirmative consent.
    # Scheduled as a background task so it runs AFTER the response is sent — it
    # can never delay or break setup. The service is idempotent (once-per-install)
    # and swallows all errors (air-gapped / blocked / offline).
    # The intake sends the email, so it never runs without one (#3004).
    if data.consent_updates and normalized_email:
        background_tasks.add_task(
            submit_operator_intake,
            email=normalized_email,
            company=(data.company or "").strip() or None,
            name=(data.name or "").strip() or None,
            role=(data.role or "").strip() or None,
            use_case=(data.use_case or "").strip() or None,
        )

    # `username` lets the page sign in when no email was given (#3004).
    return {"success": True, "email_registered": email_registered, "username": admin_username()}
