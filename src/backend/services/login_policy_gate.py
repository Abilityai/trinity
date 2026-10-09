"""Login policy gate — open-core seam for email-code sign-in (ent#849).

Decides whether emailed 6-digit codes may sign a person in. OSS-only builds
register no provider and keep email-code sign-in exactly as before. A
registered provider can turn it off for an organisation whose people sign in
through an external identity provider only.

The provider holds the policy; this module only knows the protocol:

    provider.email_code_allowed() -> bool

Two questions, because the sign-in surfaces serve two audiences:

* :func:`email_code_allowed` — platform sign-in (web login, MCP inline auth).
  Those paths create or resolve a platform account, so the policy applies to
  every address.
* :func:`email_code_allowed_for` — surfaces that also serve outsiders
  (Workspace guest sign-in, Telegram / WhatsApp identity binding, public-link
  email verification). People with
  no platform account and no whitelist entry are not in the organisation's
  identity provider and keep email-code sign-in; platform users are refused.

``/token`` (admin password) never consults this gate: it is the break-glass
path when the identity provider is down or misconfigured.

Every function that mints or redeems an emailed code (``create_login_code``,
``verify_login_code``, ``create_verification``, ``verify_code``) must ask this
gate. ``tests/unit/test_ent849_email_code_policy.py`` fails the build when a new
one does not. The codes share one table, so the
verify step is the real control; the request step only stops the email.
"""
from __future__ import annotations

import logging
from typing import Optional, Protocol

logger = logging.getLogger(__name__)


class LoginPolicyProvider(Protocol):
    def email_code_allowed(self) -> bool:
        ...


_provider: Optional[LoginPolicyProvider] = None


def register_provider(provider: LoginPolicyProvider) -> None:
    """Register the enterprise login-policy provider. Idempotent (last wins)."""
    global _provider
    _provider = provider
    logger.info("[login_policy_gate] provider registered: %s", type(provider).__name__)


def clear_provider() -> None:
    """Drop the provider — used by tests to restore the OSS no-op path."""
    global _provider
    _provider = None


def email_code_allowed() -> bool:
    """True when emailed codes may sign a platform user in.

    Fail-open: a provider error keeps email-code sign-in available, the same
    availability bias as ``mfa_gate.gate_login``. A policy read failure must
    not lock out every user of an install that never chose SSO-only.
    """
    provider = _provider
    if provider is None:
        return True
    try:
        return bool(provider.email_code_allowed())
    except Exception:  # noqa: BLE001 — a policy bug must not block all logins
        logger.exception("[login_policy_gate] provider.email_code_allowed failed; failing open")
        return True


def email_code_allowed_for(email: Optional[str]) -> bool:
    """True when ``email`` may use an emailed code on a surface that also
    serves outsiders. Refused only when the policy is off AND the address is a
    platform user or on the email whitelist."""
    if email_code_allowed():
        return True
    from database import db

    addr = (email or "").strip().lower()
    if not addr:
        return True
    try:
        return not (db.get_user_by_email(addr) or db.is_email_whitelisted(addr))
    except Exception:  # noqa: BLE001 — same fail-open rule as above
        logger.exception("[login_policy_gate] member lookup failed; failing open")
        return True
