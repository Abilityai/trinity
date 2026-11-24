"""Connect a Claude subscription from a browser, with no terminal (trinity-enterprise#1354).

`claude setup-token` is an OAuth sign-in with PKCE and a "manual" redirect: it
opens Claude's sign-in page, the person signs in, the page shows a code
(`<code>#<state>`) to copy, and the code is exchanged with the kept verifier
for a long-lived (one year) `sk-ant-oat01-` token, scoped to inference only.

This module runs that same sign-in with the INSTANCE as the client, so a
person who only has a browser can do it:

* `start(user_id)` makes the link. The PKCE verifier stays on the instance, in
  Redis under the link's `state`, bound to the person who asked, for
  `LINK_TTL_SECONDS`.
* `exchange(user_id, pasted)` takes what the page showed, spends the link
  (single use, whatever the outcome) and asks Anthropic for the token.

The parameters are the ones the Claude Code CLI itself sends (its public OAuth
client id, the manual redirect, `user:inference`, `expires_in` one year). The
client id is public by design: it is printed in every sign-in URL.

Nothing here logs, returns in an error, or stores the pasted code or the
token; the caller registers the token through the subscription path, which
encrypts it.
"""
from __future__ import annotations

import base64
import hashlib
import json
import logging
import secrets
from typing import Optional
from urllib.parse import urlencode

from routers.auth import get_redis_client

logger = logging.getLogger(__name__)

AUTHORIZE_URL = "https://claude.com/cai/oauth/authorize"
TOKEN_URL = "https://platform.claude.com/v1/oauth/token"
REDIRECT_URI = "https://platform.claude.com/oauth/code/callback"
CLIENT_ID = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"   # Claude Code's public OAuth client
SCOPE = "user:inference"
TOKEN_LIFETIME_SECONDS = 31536000                    # what `claude setup-token` asks for
LINK_TTL_SECONDS = 15 * 60
# The provider call. Well under the assistant's loopback wait for this route
# (enterprise `assistant/credential.py::TIMEOUT_SECONDS`, 30 s): the code is
# spent the moment the exchange starts, so a slow provider must end here, in a
# named refusal, before that wait ends in "may or may not have been stored".
TIMEOUT_SECONDS = 8.0
PASTE_MAX_CHARS = 2000

_KEY_PREFIX = "claude_sign_in:"


class SignInError(Exception):
    """A named refusal. `message` is plain words for the person and never
    carries the pasted code or a token."""

    def __init__(self, status: int, code: str, message: str):
        super().__init__(code)
        self.status = status
        self.code = code
        self.message = message


_FRESH = " Open the sign-in link again for a fresh code."


def _key(state: str) -> str:
    return f"{_KEY_PREFIX}{state}"


def _challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii").rstrip("=")


def _store():
    r = get_redis_client()
    if r is None:
        raise SignInError(503, "store_unavailable",
                          "This instance could not start the sign-in right now. Try again in a moment.")
    return r


def start(user_id) -> dict:
    """A fresh sign-in link for this person: `{url, expires_in}`."""
    verifier = secrets.token_urlsafe(48)
    state = secrets.token_urlsafe(32)
    try:
        _store().setex(_key(state), LINK_TTL_SECONDS, json.dumps({"user_id": user_id, "verifier": verifier}))
    except SignInError:
        raise
    except Exception:
        logger.warning("claude sign-in: the link could not be kept")
        raise SignInError(503, "store_unavailable",
                          "This instance could not start the sign-in right now. Try again in a moment.") from None
    query = urlencode({
        "code": "true",
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": REDIRECT_URI,
        "scope": SCOPE,
        "code_challenge": _challenge(verifier),
        "code_challenge_method": "S256",
        "state": state,
    })
    return {"url": f"{AUTHORIZE_URL}?{query}", "expires_in": LINK_TTL_SECONDS}


def _load_for_test(store, state: str) -> Optional[dict]:
    """Read a kept link without spending it (tests only)."""
    raw = store.data.get(_key(state)) if hasattr(store, "data") else None
    return json.loads(raw) if raw else None


_API_KEY_NOT_CODE = (
    "That looks like an API key, not the code the Claude sign-in page shows. "
    "Anthropic API keys start with sk-ant-api (for example sk-ant-api03-…) — "
    "paste one of those, or open the sign-in link again and copy the code."
)


def _looks_like_api_key(text: str) -> bool:
    """Plainly an API-key attempt: a sign-in code is `<code>#<state>` and never
    starts like a provider key."""
    lowered = text.lower()
    return "#" not in text and (lowered.startswith(("sk-", "ant-api")) or "sk-ant-api" in lowered)


def _split(pasted) -> tuple[str, str]:
    text = "".join(pasted.split()) if isinstance(pasted, str) else ""
    if len(text) > PASTE_MAX_CHARS:
        text = ""
    if _looks_like_api_key(text):
        raise SignInError(422, "api_key_not_code", _API_KEY_NOT_CODE)
    code, _, state = text.partition("#")
    if not code or not state:
        raise SignInError(422, "code_incomplete",
                          "That is not the whole code. Copy everything the Claude page shows, "
                          "including the part after #, and paste it here." + _FRESH)
    return code, state


def _spend(user_id, state: str) -> str:
    """The verifier for `state`, removed so the link works once."""
    try:
        raw = _store().getdel(_key(state))
    except SignInError:
        raise
    except Exception:
        logger.warning("claude sign-in: the kept link could not be read")
        raise SignInError(503, "store_unavailable",
                          "This instance could not finish the sign-in right now. Try again in a moment.") from None
    kept = None
    try:
        kept = json.loads(raw) if raw else None
    except (TypeError, ValueError):
        kept = None
    if not isinstance(kept, dict) or kept.get("user_id") != user_id or not kept.get("verifier"):
        raise SignInError(422, "sign_in_expired",
                          "That code is from a sign-in that has expired or was already used." + _FRESH)
    return kept["verifier"]


async def _post_token(url: str, body: dict):
    import httpx

    async with httpx.AsyncClient(timeout=TIMEOUT_SECONDS) as http:
        return await http.post(url, json=body, headers={"Content-Type": "application/json"})


async def exchange(user_id, pasted) -> str:
    """The subscription token for a pasted `<code>#<state>`. Raises
    `SignInError`; after any refusal nothing was stored."""
    code, state = _split(pasted)
    verifier = _spend(user_id, state)
    body = {
        "grant_type": "authorization_code",
        "code": code,
        "redirect_uri": REDIRECT_URI,
        "client_id": CLIENT_ID,
        "code_verifier": verifier,
        "state": state,
        "expires_in": TOKEN_LIFETIME_SECONDS,
    }
    try:
        response = await _post_token(TOKEN_URL, body)
    except Exception as e:
        logger.warning("claude sign-in: Anthropic did not answer (%s)", type(e).__name__)
        raise SignInError(502, "anthropic_unavailable",
                          "Claude's sign-in service did not answer." + _FRESH) from None
    status = getattr(response, "status_code", 0)
    if status in (400, 401, 403):
        logger.info("claude sign-in: the code was refused (%s)", status)
        raise SignInError(422, "code_rejected",
                          "Claude did not accept that code. It may have expired or been copied in part." + _FRESH)
    if status != 200:
        logger.warning("claude sign-in: Anthropic answered %s", status)
        raise SignInError(502, "anthropic_unavailable",
                          "Claude's sign-in service is busy or unavailable right now." + _FRESH)
    try:
        token = response.json().get("access_token")
    except (ValueError, AttributeError):
        token = None
    if not isinstance(token, str) or not token.startswith("sk-ant-oat01-"):
        logger.warning("claude sign-in: the answer carried no subscription token")
        raise SignInError(502, "unexpected_answer",
                          "Claude answered, but not with a subscription token." + _FRESH)
    logger.info("claude sign-in: a subscription token was issued")
    return token
