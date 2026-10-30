"""Which model answers a retrieval-memory prompt (trinity-enterprise#1027).

Extraction and reconcile are one-shot prompts. Who runs them is ONE decision,
made here, so the capture seam, the status cards and the owner's preview all
read the same answer:

* ``agent``    — the agent's own runtime, headless inside its container, through
  the agent-server door ``POST /api/runtime/complete``: the agent's subscription
  (or its own key) pays, the transcript never leaves the platform network, and
  the backend stays runtime-agnostic — Claude Code, Gemini CLI and Codex each
  implement the door behind the same adapter.
* ``platform`` — the instance's Anthropic key through ``anthropic_messages``.
* ``none``     — nobody; the run is not captured, and the status says why.

The operator knob is ``trinity_memory_extractor``: ``auto`` (the agent when its
container is running and its subscription is not at a limit, the platform key
otherwise — the default, and the only mode that falls back), ``agent`` (the
agent or nobody), ``platform`` (the key or nobody).

Why not call the Messages API from the backend with the stored subscription
token: the gateway only serves a completion on a setup token when the request
claims to be Claude Code (measured 2026-10-30: 429 ``Error`` without the
identity system line, 200 with it), so that path would impersonate the client
the token was issued for and put every install's subscriptions at risk. The
headroom probe (``subscription_headroom_service``) stays the only backend code
that touches a setup token.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from database import db
from services import anthropic_messages
from services.agent_auth import agent_httpx_client

logger = logging.getLogger(__name__)

SETTING_EXTRACTOR = "trinity_memory_extractor"
MODES = ("auto", "agent", "platform")
DEFAULT_MODE = "auto"

AGENT_PORT = 8000
AGENT_CONNECT_TIMEOUT_S = 5.0      # a stopped or busy container must fail fast, not stall the retain
AGENT_CALL_OVERHEAD_S = 10.0       # the CLI's cold start, on top of the model's own budget


@dataclass(frozen=True)
class Plan:
    mode: str                 # the knob: auto | agent | platform
    effective: str            # who answers next: agent | platform | none
    runtime: Optional[str]    # "claude-code" | "gemini-cli" | "codex" when effective == agent
    reason: Optional[str]     # why nobody, when effective == none

    def as_dict(self) -> dict:
        return {"mode": self.mode, "effective": self.effective, "runtime": self.runtime, "reason": self.reason}


def mode() -> str:
    """The knob, normalised; anything unreadable or unknown reads as ``auto``."""
    try:
        value = str(db.get_setting_value(SETTING_EXTRACTOR, default=DEFAULT_MODE) or "").strip().lower()
    except Exception:  # noqa: BLE001 — a settings read failure reads as the default
        return DEFAULT_MODE
    return value if value in MODES else DEFAULT_MODE


# --- the three facts the plan is made of (each one a seam the tests replace) ---

def _container_running(agent_name: str) -> Optional[bool]:
    """True / False, or None when Docker could not be asked — never assume a
    container exists on a daemon fault (the #2196 tri-state)."""
    from services import docker_service
    if getattr(docker_service, "docker_client", None) is None:
        return None
    container = docker_service.get_agent_container(agent_name)
    if container is None:
        return False
    try:
        return str(getattr(container, "status", "") or "").lower() == "running"
    except Exception:  # noqa: BLE001
        return None


def _agent_runtime(agent_name: str) -> str:
    from services import docker_service
    return docker_service.get_agent_runtime(agent_name)


def _subscription_limited(agent_name: str) -> bool:
    """The agent's own subscription is at a limit right now (the 2h db half of
    ``rate_limited_now``): its window is for its work, not for remembering it."""
    try:
        sub = db.get_agent_subscription(agent_name)
        return bool(sub and db.is_subscription_rate_limited(sub.id))
    except Exception:  # noqa: BLE001 — unknown reads as not limited; the door itself answers 429
        return False


def plan(agent_name: str) -> Plan:
    """Who would answer the next memory prompt for ``agent_name`` right now."""
    m = mode()
    has_key = anthropic_messages.has_platform_key()
    agent_reason: Optional[str] = None
    if m in ("auto", "agent"):
        running = _container_running(agent_name)
        if running is True:
            if _subscription_limited(agent_name):
                agent_reason = "the agent's subscription is rate-limited right now"
            else:
                return Plan(m, "agent", _agent_runtime(agent_name), None)
        elif running is False:
            agent_reason = "the agent is not running"
        else:
            agent_reason = "the agent's container state is unknown (Docker unreadable)"
    if m in ("auto", "platform") and has_key:
        return Plan(m, "platform", None, None)
    if m == "platform":
        reason = "no platform Anthropic key"
    elif m == "agent":
        reason = agent_reason or "the agent cannot answer"
    else:
        reason = f"{agent_reason or 'no agent runtime'} and no platform Anthropic key"
    return Plan(m, "none", None, reason)


def available(agent_name: str) -> bool:
    """Whether anything would answer — the capture seam's gate."""
    return plan(agent_name).effective != "none"


# --- the call ---

async def _agent_complete(agent_name: str, prompt: str, *, max_tokens: int, timeout: float, tag: str) -> Optional[str]:
    """One prompt through the agent-server door; None on any refusal, logged
    with the door's code (never the prompt, never the text)."""
    url = f"http://agent-{agent_name}:{AGENT_PORT}/api/runtime/complete"
    body = {"prompt": prompt, "max_tokens": int(max_tokens), "timeout_seconds": int(timeout), "tag": tag}
    try:
        async with agent_httpx_client(agent_name, timeout=timeout + AGENT_CALL_OVERHEAD_S) as client:
            response = await client.post(url, json=body)
    except Exception as e:  # noqa: BLE001 — a stopped container, a DNS miss, a timeout
        logger.info("[%s] agent runtime door unreachable for %s: %s", tag, agent_name, type(e).__name__)
        return None
    if response.status_code != 200:
        code = "?"
        try:
            detail = response.json().get("detail")
            code = detail.get("code") if isinstance(detail, dict) else str(detail)[:40]
        except Exception:  # noqa: BLE001
            pass
        logger.info("[%s] agent runtime door refused for %s: %s %s", tag, agent_name, response.status_code, code)
        return None
    try:
        text = response.json().get("text")
    except Exception as e:  # noqa: BLE001
        logger.info("[%s] agent runtime door answer unreadable for %s: %s", tag, agent_name, type(e).__name__)
        return None
    return text.strip() if isinstance(text, str) else None


async def complete(agent_name: str, prompt: str, *, max_tokens: int = 512, timeout: float = 30.0,
                   tag: str = "memory") -> Optional[str]:
    """The model's text for ``prompt`` by the plan, or None. In ``auto`` an
    agent door that cannot answer (stopped mid-call, rate-limited, no credential)
    falls back to the platform key when there is one; the other modes never
    cross over."""
    p = plan(agent_name)
    if p.effective == "agent":
        text = await _agent_complete(agent_name, prompt, max_tokens=max_tokens, timeout=timeout, tag=tag)
        if text is not None:
            return text
        if p.mode == "auto" and anthropic_messages.has_platform_key():
            logger.info("[%s] falling back to the platform key for %s", tag, agent_name)
            return await anthropic_messages.complete(prompt, max_tokens=max_tokens, timeout=timeout, tag=tag)
        return None
    if p.effective == "platform":
        return await anthropic_messages.complete(prompt, max_tokens=max_tokens, timeout=timeout, tag=tag)
    logger.info("[%s] nobody to answer for %s: %s", tag, agent_name, p.reason)
    return None
