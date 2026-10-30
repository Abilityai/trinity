"""The backend's one model door (trinity-enterprise#1027).

Every platform service that needs a small model call — retrieval-memory
extraction and reconcile, the MEM-001 conversation summarizer, Workspace thread
titles, the compatibility AI checks — comes through here. Before this each had
its own ``httpx`` call, its own credential lookup and its own request shape;
one of them (thread titles, #2766) had already learned to use the agent's
subscription, the others only ever saw the platform key.

**The credential follows the agent** (#2766, #471's one derivation): a job
attributed to an agent that runs on a Claude subscription uses that agent's
own setup token — same credential, same bill as its chat — through the
Messages API's OAuth beta; an agent on API-key auth, or a job with no agent
behind it, uses the instance's platform key. A subscription-mode agent whose
token cannot be read answers *nobody* rather than borrowing the instance key
(the shadowing #2766 fixed). The knob is ``trinity_model_credential``:
``auto`` (the rule above; a subscription that is rate-limited, or answers
429 mid-call, steps aside to the platform key when there is one), ``subscription``
(the agent's subscription or nobody), ``platform_key`` (the key or nobody).

**The model follows the credential.** Measured 2026-10-30 on a real setup
token, plain Bearer with the OAuth beta: ``claude-haiku-4-5`` answers 200;
Haiku 5.5, Sonnet 4.6 / 5.5 and Opus 4.6 / 5.5 answer 429 ``Error`` unless the
request claims to be Claude Code. So a subscription credential always runs
``MODEL_ON_SUBSCRIPTION`` and never impersonates the client the token was
issued for; the platform key runs ``MODEL_ON_API_KEY`` (thinking off, low
effort — one-shot answers, ``max_tokens`` is the text budget). A caller's
``model`` override applies to the platform key only.

Never raises on the request path, never logs a secret or a prompt. Every
answered call leaves one bounded usage line (purpose, credential kind, model,
tokens, latency) for Vector.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional

import httpx

logger = logging.getLogger(__name__)

SETTING_CREDENTIAL = "trinity_model_credential"
MODES = ("auto", "subscription", "platform_key")
DEFAULT_MODE = "auto"

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
OAUTH_BETA = "oauth-2025-04-20"        # the header Claude Code itself sends with a setup token

MODEL_ON_API_KEY = "claude-haiku-5-5"
MODEL_ON_SUBSCRIPTION = "claude-haiku-4-5-20251001"

KIND_SUBSCRIPTION = "subscription"
KIND_API_KEY = "api_key"
KIND_NONE = "none"


@dataclass(frozen=True)
class Credential:
    kind: str                            # subscription | api_key | none
    mode: str                            # the knob that produced it
    model: Optional[str] = None
    secret: Optional[str] = field(default=None, repr=False)   # never logged, never in as_dict(), never in repr
    subscription_id: Optional[str] = None
    reason: Optional[str] = None         # why nobody, when kind == none

    @property
    def usable(self) -> bool:
        return self.kind != KIND_NONE and bool(self.secret)

    def headers(self) -> Dict[str, str]:
        base = {"anthropic-version": API_VERSION, "content-type": "application/json"}
        if self.kind == KIND_SUBSCRIPTION:
            return {**base, "authorization": f"Bearer {self.secret}", "anthropic-beta": OAUTH_BETA}
        if self.kind == KIND_API_KEY:
            return {**base, "x-api-key": self.secret or ""}
        return base

    def body(self, *, prompt: Optional[str] = None, messages: Optional[List[Dict[str, Any]]] = None,
             max_tokens: int = 512, model: Optional[str] = None,
             tools: Optional[list] = None, tool_choice: Optional[dict] = None) -> Dict[str, Any]:
        """The request body for this credential. ``model`` overrides only on
        the platform key — a subscription runs the one model the gateway serves."""
        chosen = (model or MODEL_ON_API_KEY) if self.kind == KIND_API_KEY else MODEL_ON_SUBSCRIPTION
        body: Dict[str, Any] = {
            "model": chosen,
            "max_tokens": int(max_tokens),
            "messages": messages if messages is not None else [{"role": "user", "content": prompt or ""}],
        }
        if self.kind == KIND_API_KEY and chosen.split("-")[2:3] and chosen.split("-")[2].startswith("5"):
            # This generation thinks by default; these are one-shot answers.
            body["thinking"] = {"type": "disabled"}
            body["output_config"] = {"effort": "low"}
        if tools:
            body["tools"] = tools
        if tool_choice:
            body["tool_choice"] = tool_choice
        return body

    def as_dict(self) -> Dict[str, Any]:
        return {"kind": self.kind, "mode": self.mode, "model": self.model,
                "subscription_id": self.subscription_id, "reason": self.reason}


# --------------------------------------------------------------------------- #
# Resolution
# --------------------------------------------------------------------------- #

def _db():
    import database
    return database.db


def has_platform_key() -> bool:
    from services import settings_service
    try:
        return bool(settings_service.get_anthropic_api_key())
    except Exception:  # noqa: BLE001 — a settings read failure reads as "no key"
        return False


def mode() -> str:
    """The knob, normalised; anything unreadable or unknown reads as ``auto``."""
    try:
        value = str(_db().get_setting_value(SETTING_CREDENTIAL, default=DEFAULT_MODE) or "").strip().lower()
    except Exception:  # noqa: BLE001
        return DEFAULT_MODE
    return value if value in MODES else DEFAULT_MODE


def resolve(agent_name: Optional[str] = None) -> Credential:
    """The credential a call attributed to ``agent_name`` (None: the platform
    itself) runs on right now. Never raises: a lookup failure reads as nobody."""
    from services import settings_service
    from services.subscription_service import derive_auth_mode

    m = mode()
    try:
        api_key = settings_service.get_anthropic_api_key() or ""
    except Exception:  # noqa: BLE001
        api_key = ""
    sub_reason: Optional[str] = None
    if m in ("auto", "subscription") and agent_name:
        try:
            db = _db()
            sub_id = db.get_agent_subscription_id(agent_name)
            if sub_id:
                limited = bool(db.is_subscription_rate_limited(sub_id))
                if limited:
                    sub_reason = "the agent's subscription is rate-limited right now"
                else:
                    token = db.get_subscription_token(sub_id)
                    if token:
                        return Credential(KIND_SUBSCRIPTION, m, MODEL_ON_SUBSCRIPTION, token, sub_id)
                    # #2766: an unreadable token is a fault to surface, never a
                    # reason to bill an instance key the agent was not assigned.
                    return Credential(KIND_NONE, m, None, None, sub_id,
                                      "the agent's subscription token is unreadable")
            else:
                sub_reason = "the agent has no subscription assigned"
        except Exception as e:  # noqa: BLE001 — fail-soft, the caller keeps its fallback
            logger.warning("[model] subscription lookup failed for %s: %s", agent_name, type(e).__name__)
            return Credential(KIND_NONE, m, None, None, None, "the subscription lookup failed")
    elif m in ("auto", "subscription"):
        sub_reason = "the call is not attributed to an agent"

    # The platform's one auth-mode derivation (#471) names what is left.
    auth_mode = derive_auth_mode(False, bool(api_key))
    if m in ("auto", "platform_key") and auth_mode == "api_key":
        if sub_reason and m == "auto" and agent_name:
            logger.debug("[model] %s: %s — using the platform key", agent_name, sub_reason)
        return Credential(KIND_API_KEY, m, MODEL_ON_API_KEY, api_key)
    if m == "platform_key":
        reason = "no platform Anthropic key"
    elif m == "subscription":
        reason = sub_reason or "no subscription"
    else:
        reason = f"{sub_reason or 'no subscription'} and no platform Anthropic key"
    return Credential(KIND_NONE, m, None, None, None, reason)


def plan(agent_name: Optional[str] = None) -> Dict[str, Any]:
    """What the status cards show: who would answer next and on which model —
    the credential without its secret, plus the subscription's name."""
    cred = resolve(agent_name)
    name = None
    if cred.subscription_id:
        try:
            sub = _db().get_agent_subscription(agent_name) if agent_name else None
            name = getattr(sub, "name", None)
        except Exception:  # noqa: BLE001
            name = None
    return {"mode": cred.mode, "effective": cred.kind, "model": cred.model,
            "subscription": name, "reason": cred.reason}


# --------------------------------------------------------------------------- #
# The call
# --------------------------------------------------------------------------- #

async def send(credential: Credential, body: Dict[str, Any], *, timeout: float = 30.0) -> Optional[httpx.Response]:
    """One POST on ``credential``; None on a transport failure (logged)."""
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            return await client.post(API_URL, headers=credential.headers(), json=body)
    except Exception as e:  # noqa: BLE001 — network failure is a logged None
        logger.error("[model] request failed (%s): %s", credential.kind, type(e).__name__)
        return None


def _log_usage(purpose: str, credential: Credential, data: Dict[str, Any], started: float) -> None:
    usage = data.get("usage") or {}
    logger.info("[model] %s via %s %s: %s in / %s out, %d ms", purpose, credential.kind, data.get("model"),
                usage.get("input_tokens", "?"), usage.get("output_tokens", "?"), int((time.monotonic() - started) * 1000))


async def request(purpose: str, *, agent_name: Optional[str] = None, credential: Optional[Credential] = None,
                  prompt: Optional[str] = None, messages: Optional[List[Dict[str, Any]]] = None,
                  max_tokens: int = 512, timeout: float = 30.0, model: Optional[str] = None,
                  tools: Optional[list] = None, tool_choice: Optional[dict] = None) -> Optional[Dict[str, Any]]:
    """The Messages API response for one call, or None on any failure. In
    ``auto`` a subscription that answers 429 steps aside to the platform key."""
    cred = credential or resolve(agent_name)
    if not cred.usable:
        logger.info("[model] %s: nobody to answer for %s: %s", purpose, agent_name or "platform", cred.reason)
        return None
    started = time.monotonic()
    body = cred.body(prompt=prompt, messages=messages, max_tokens=max_tokens, model=model, tools=tools, tool_choice=tool_choice)
    response = await send(cred, body, timeout=timeout)
    if response is not None and response.status_code == 429 and cred.kind == KIND_SUBSCRIPTION and cred.mode == "auto":
        fallback = resolve(None)
        if fallback.kind == KIND_API_KEY:
            logger.info("[model] %s: subscription rate-limited for %s — using the platform key", purpose, agent_name)
            cred = fallback
            body = cred.body(prompt=prompt, messages=messages, max_tokens=max_tokens, model=model, tools=tools, tool_choice=tool_choice)
            response = await send(cred, body, timeout=timeout)
    if response is None:
        return None
    if response.status_code != 200:
        logger.error("[model] %s via %s: API %s: %s", purpose, cred.kind, response.status_code, response.text[:200])
        return None
    try:
        data = response.json()
    except Exception as e:  # noqa: BLE001
        logger.error("[model] %s: response unreadable: %s", purpose, type(e).__name__)
        return None
    if not isinstance(data, dict):
        return None
    _log_usage(purpose, cred, data, started)
    return data


def first_text(data: Optional[Dict[str, Any]]) -> Optional[str]:
    """The first TEXT block — never ``content[0]`` blindly, which is a thinking
    block on a model that reasons before it answers. A block that carries text
    but declares no type (older shapes) still counts."""
    if not data:
        return None
    blocks = [b for b in (data.get("content") or []) if isinstance(b, dict)]
    for block in blocks:
        if block.get("type") == "text":
            return (block.get("text") or "").strip()
    for block in blocks:
        if block.get("type") is None and isinstance(block.get("text"), str):
            return block["text"].strip()
    return ""


async def complete(purpose: str, prompt: str, *, agent_name: Optional[str] = None,
                   max_tokens: int = 512, timeout: float = 30.0, model: Optional[str] = None) -> Optional[str]:
    """The model's text for ``prompt`` as a single user message, or None."""
    return first_text(await request(purpose, agent_name=agent_name, prompt=prompt, max_tokens=max_tokens,
                                    timeout=timeout, model=model))


__all__ = ["Credential", "complete", "first_text", "has_platform_key", "mode", "plan", "request", "resolve", "send"]
