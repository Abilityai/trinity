"""
Validation for the platform keys the first-run flow configures (trinity-enterprise#582).

Format rules and live checks for the Anthropic API key, the Resend email key
and the Gemini key, so the settings routers stay thin (Invariant #1). Every
message is operator copy — the first-run step shows it as-is — so each names
the problem, the fix and an example (design-system principle 17), and none
ever contains the key.

Twin: `src/frontend/src/components/onboarding/steps/credentialSteps.js` runs
the same format rules client-side before any request; the server re-checks
because it is the boundary.
"""
from __future__ import annotations

import re
from typing import Optional

import httpx

CHECK_TIMEOUT_SECONDS = 10.0

# ent#581 spec comment 2, verbatim (markdown emphasis dropped — plain text).
OAT_IN_API_KEY_TAB = (
    "That key was rejected. API keys start with sk-ant-api — this one starts "
    "with sk-ant-oat, which is a subscription token. Paste it on the "
    "Subscription token tab instead."
)


def anthropic_api_key_error(key: str) -> Optional[str]:
    """Format rule for a platform Anthropic API key; None when it passes."""
    if key.startswith("sk-ant-oat"):
        return OAT_IN_API_KEY_TAB
    if not key.startswith("sk-ant-"):
        return (
            "That doesn't look like an Anthropic API key. API keys start with "
            "sk-ant-api (for example sk-ant-api03-…). Create one at "
            "console.anthropic.com → API Keys."
        )
    return None


# =============================================================================
# Resend (email-code sign-in)
# =============================================================================

# `Name <addr@domain>` or a bare address. Deliberately loose — Resend is the
# judge of what it will send from; this only catches a paste that is not an
# address at all.
_FROM_RE = re.compile(r"^(?:[^<>]*<)?\s*([^@\s<>]+@([^@\s<>]+\.[^@\s<>]+))\s*>?$")

# Resend's shared test domain: sends only to the account owner's own address.
RESEND_TEST_DOMAIN = "resend.dev"


def resend_key_error(key: str) -> Optional[str]:
    if not key.startswith("re_"):
        return (
            "That doesn't look like a Resend API key. Resend keys start with "
            "re_ — create one at resend.com/api-keys."
        )
    return None


def from_address_domain(address: str) -> Optional[str]:
    """The domain an address sends from, lower-cased; None if not an address."""
    m = _FROM_RE.match((address or "").strip())
    return m.group(2).lower() if m else None


def from_address_error(address: str) -> Optional[str]:
    if from_address_domain(address) is None:
        return (
            "Enter the address sign-in codes are sent from, on a domain you "
            "have verified in Resend — for example noreply@your-domain.com."
        )
    return None


async def check_resend_key(key: str, from_address: str) -> dict:
    """Live check: the key works AND Resend will send from ``from_address``.

    ``GET /domains`` answers both without sending anything. A sending-only key
    is refused that read (401 ``restricted_api_key``) — it is still a valid
    key, so it passes with a warning that the domain could not be confirmed.
    """
    domain = from_address_domain(from_address)
    try:
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                "https://api.resend.com/domains",
                headers={"Authorization": f"Bearer {key}"},
            )
    except httpx.TimeoutException:
        return {"valid": False, "error": "Resend didn't answer in time — try again in a moment."}
    except Exception:  # noqa: BLE001 — never echo transport detail that may carry the key
        return {"valid": False, "error": "Couldn't reach Resend to check the key — try again in a moment."}

    try:
        body = resp.json() if resp.content else {}
    except ValueError:
        body = {}
    body = body if isinstance(body, dict) else {}

    if resp.status_code == 401 and body.get("name") == "restricted_api_key":
        return {
            "valid": True,
            "sending_only": True,
            "warning": (
                "This is a sending-only key, so Trinity can't confirm that "
                f"{domain or 'your sending domain'} is verified in Resend. If "
                "sign-in codes don't arrive, check resend.com/domains."
            ),
        }
    if resp.status_code != 200:
        return {
            "valid": False,
            "error": (
                f"Resend rejected this key (HTTP {resp.status_code}). Copy it "
                "again from resend.com/api-keys — keys start with re_."
            ),
        }

    data = body.get("data") if isinstance(body.get("data"), list) else []
    verified = sorted(
        str(d.get("name", "")).lower()
        for d in data
        if isinstance(d, dict) and d.get("status") == "verified" and d.get("name")
    )
    result = {"valid": True, "verified_domains": verified}
    if domain == RESEND_TEST_DOMAIN:
        result["warning"] = (
            "resend.dev can only deliver to your own Resend account address. "
            "Verify your domain at resend.com/domains to send sign-in codes to anyone else."
        )
        return result
    if domain not in verified:
        example = f"noreply@{verified[0]}" if verified else "noreply@your-domain.com"
        where = (
            f"Your verified domains: {', '.join(verified)}." if verified
            else "This Resend account has no verified domain yet."
        )
        return {
            "valid": False,
            "verified_domains": verified,
            "error": (
                f"Resend accepted the key, but it won't send from {domain or 'that address'} — "
                f"the domain isn't verified. {where} Verify it at resend.com/domains, "
                f"or send from an address like {example}."
            ),
        }
    return result


# =============================================================================
# Gemini (voice, generated avatars)
# =============================================================================

def gemini_key_error(key: str) -> Optional[str]:
    if not key.startswith("AIza"):
        return (
            "That doesn't look like a Gemini API key. Keys from Google AI Studio "
            "start with AIza — create one at aistudio.google.com/apikey."
        )
    return None


async def check_gemini_key(key: str) -> dict:
    """Live check: list one model with the key (no generation, no cost)."""
    try:
        async with httpx.AsyncClient(timeout=CHECK_TIMEOUT_SECONDS) as client:
            resp = await client.get(
                "https://generativelanguage.googleapis.com/v1beta/models",
                params={"pageSize": 1},
                # Header, not `?key=` — a query-string key lands in access logs.
                headers={"x-goog-api-key": key},
            )
    except httpx.TimeoutException:
        return {"valid": False, "error": "Google didn't answer in time — try again in a moment."}
    except Exception:  # noqa: BLE001
        return {"valid": False, "error": "Couldn't reach Google to check the key — try again in a moment."}

    if resp.status_code in (200, 429):  # 429: a real key at its quota right now
        return {"valid": True}
    if resp.status_code == 403:
        return {
            "valid": False,
            "error": (
                "Google accepted the key, but it can't call the Gemini API. Enable "
                "the Generative Language API for its project, or create a key at "
                "aistudio.google.com/apikey."
            ),
        }
    return {
        "valid": False,
        "error": (
            f"Google rejected this key (HTTP {resp.status_code}). Copy it again "
            "from aistudio.google.com/apikey — keys start with AIza."
        ),
    }
