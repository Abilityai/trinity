"""The agent's own skills list: read live from its container, remembered for
when it is stopped (trinity-enterprise#754).

Three surfaces read the agent's `GET /api/skills`: the agent page
(`GET /api/agents/{name}/playbooks`, which the Skills tab, the chat `/` menu,
the dashboard and the exposed-skills panel all use), the public link and the
MCP connector. They had three copies of the same proxy, and this is now the one
proxy behind those three. (The Workspace's own skill read in
`client_portal/service.py` still asks the agent directly: a known gap.)

The **last-known copy** answers the Skills tab's "never an empty tab" rule for
a stopped agent. Every successful live read keeps the listing in Redis under
`agent:skills_list:{name}`, with no TTL (the user's ruling: derivable cache data,
no schema). The copy is:

* written only from a real listing: a 200 whose body carries a `skills` list.
  A live EMPTY list replaces it, because the agent really has none now. A failed
  read, or a body that is not a listing, never overwrites it;
* bounded: a listing over ``MAX_CACHED_BYTES`` is not kept, and it DROPS the
  older copy rather than leaving a stale smaller list to be served as current;
* served only on the caller's explicit opt-in, and only when the agent is
  stopped or unreachable. A running agent that answers with an error is not
  masked;
* display-only: nothing that runs a skill reads it;
* cleared with the agent's other name-keyed state on delete / rename / purge /
  ghost teardown (`agent_runtime_state.CLEARED_KEYSPACES`) and on the create
  path, never on a stop or a start.

Redis is fail-open everywhere here: down, slow or raising, the live read still
answers, and the stopped path falls back to the original error.
"""
from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import httpx

from services.agent_auth import agent_httpx_client
from services.docker_service import agent_container_state
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

# Written exactly as the literal: `test_1560_agent_redis_key_parity` greps the
# backend for `"agent:<segment>` literals and checks each is registered in
# `agent_runtime_state` (this one is in CLEARED_KEYSPACES).
KEY_PREFIX = "agent:skills_list:"

# An agent writes its own skill files, so the copy is bounded: a listing larger
# than this is served live but not kept.
MAX_CACHED_BYTES = 256 * 1024

# The per-skill fields the public link carried before #754. The public route is
# unauthenticated, so it keeps to this list (fail-closed): a field the agent
# server adds later never reaches an anonymous visitor by default.
PUBLIC_SKILL_FIELDS = (
    "name", "description", "path", "user_invocable", "automation",
    "allowed_tools", "argument_hint", "has_schedule",
)
# The top-level keys of that answer, held to the same rule.
PUBLIC_LISTING_FIELDS = ("skills", "count", "skill_paths")


class SkillsListUnavailable(Exception):
    """The live list could not be read. ``reason`` is one of `not_found`
    (no container), `not_running`, `unreachable` (timeout / connect error)
    or `agent_error` (the agent answered non-200). ``status_code`` / ``detail``
    are the HTTP answer the agent page has always given; the other callers
    keep their own wording for the reasons they word differently
    (``agent_text`` is the agent's own error body, for `agent_error`)."""

    def __init__(self, reason: str, status_code: int, detail: str, agent_text: str = ""):
        super().__init__(detail)
        self.reason = reason
        self.status_code = status_code
        self.detail = detail
        self.agent_text = agent_text


def _key(agent_name: str) -> str:
    return f"{KEY_PREFIX}{agent_name}"


def _redis():
    # Looked up at call time through the module, so a test (or a Redis that
    # comes back) is seen without a re-import.
    import redis_breaker_util

    return redis_breaker_util.get_breaker_redis()


async def fetch_live(agent_name: str) -> Any:
    """The running agent's `GET /api/skills` body, keeping a copy of it.

    Raises :class:`SkillsListUnavailable`. A body that is not valid JSON
    raises as before; each caller maps that to its own 500.
    """
    # Tri-state, never collapsed (#2196's class): a container that is not there
    # is `not_found`, but a Docker daemon that could not be asked leaves the
    # agent's state unknown, so it is `unreachable` and a kept copy is served
    # for it, as for an agent that is not answering. The read is fresh, and
    # Docker's own text (its URL, the container id) never leaves it.
    state = agent_container_state(agent_name)
    if state is None:
        raise SkillsListUnavailable("unreachable", 503, "Could not read the agent's state")
    if state == "missing":
        raise SkillsListUnavailable("not_found", 404, "Agent not found")
    if state != "running":
        raise SkillsListUnavailable(
            "not_running", 503, "Agent is not running. Start the agent to view its skills.")
    try:
        async with agent_httpx_client(agent_name, timeout=10.0) as client:
            response = await client.get(f"http://agent-{agent_name}:8000/api/skills")
    except httpx.TimeoutException:
        raise SkillsListUnavailable("unreachable", 504, "Agent is starting up, please try again")
    except httpx.TransportError:
        # Connect, read or protocol failure: the agent did not answer — a
        # connection dropped mid-answer is as unreachable as one refused.
        raise SkillsListUnavailable("unreachable", 503, "Could not connect to agent")
    if response.status_code != 200:
        raise SkillsListUnavailable(
            "agent_error", response.status_code, f"Agent returned error: {response.text}",
            agent_text=response.text)
    body = response.json()
    remember(agent_name, body)
    return body


async def list_skills(agent_name: str, *, last_known: bool = False) -> Any:
    """The agent page's listing: live, or with ``last_known`` the kept copy
    when the agent is stopped or unreachable.

    The copy is answered as the live shape plus
    ``last_known: {captured_at, reason: "stopped" | "unreachable"}``. With no
    copy, or for any other failure, the original error is raised.
    """
    try:
        return await fetch_live(agent_name)
    except SkillsListUnavailable as e:
        if not last_known or e.reason not in ("not_running", "unreachable"):
            raise
        copy = recall(agent_name)
        if copy is None:
            raise
        return {
            "skills": copy["skills"],
            "count": len(copy["skills"]),
            "skill_paths": copy["skill_paths"],
            "last_known": {
                "captured_at": copy["captured_at"],
                "reason": "stopped" if e.reason == "not_running" else "unreachable",
            },
        }


def remember(agent_name: str, body: Any) -> None:
    """Keep a real listing as the agent's last-known copy. Never raises."""
    if not isinstance(body, dict) or not isinstance(body.get("skills"), list):
        return
    paths = body.get("skill_paths")
    record = {
        "skills": body["skills"],
        "skill_paths": paths if isinstance(paths, list) else [],
        "captured_at": utc_now_iso(),
    }
    try:
        payload = json.dumps(record, separators=(",", ":"), default=str)
    except (TypeError, ValueError):
        return
    if len(payload.encode("utf-8")) > MAX_CACHED_BYTES:
        logger.info("[skills_list] %s's listing is over %d bytes; not kept", agent_name, MAX_CACHED_BYTES)
        forget(agent_name)
        return
    r = _redis()
    if r is None:
        return
    try:
        r.set(_key(agent_name), payload)
    except Exception as e:  # noqa: BLE001 — a cache write never fails the live read
        logger.warning("[skills_list] keeping %s's listing failed: %s", agent_name, e)


def recall(agent_name: str) -> Optional[Dict[str, Any]]:
    """The kept copy ``{skills, skill_paths, captured_at}``, or None when there
    is none, Redis is unreachable, or the stored value is malformed."""
    r = _redis()
    if r is None:
        return None
    try:
        raw = r.get(_key(agent_name))
    except Exception as e:  # noqa: BLE001
        logger.warning("[skills_list] reading %s's listing failed: %s", agent_name, e)
        return None
    if raw is None:
        return None
    try:
        record = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if (not isinstance(record, dict) or not isinstance(record.get("skills"), list)
            or not isinstance(record.get("skill_paths"), list)
            or not isinstance(record.get("captured_at"), str)):
        return None
    return record


def forget(agent_name: str) -> None:
    """Drop the kept copy. Never raises."""
    r = _redis()
    if r is None:
        return
    try:
        r.delete(_key(agent_name))
    except Exception as e:  # noqa: BLE001
        logger.warning("[skills_list] clearing %s's listing failed: %s", agent_name, e)


def public_view(body: Any) -> Optional[Dict[str, Any]]:
    """The listing as the unauthenticated public link may see it, or None when
    the agent's answer is not a listing. Only the top-level keys and the
    per-skill fields that link has always carried survive (fail-closed: a key or
    field the agent server adds later never reaches an anonymous visitor)."""
    if not isinstance(body, dict) or not isinstance(body.get("skills"), list):
        return None
    skills = [{k: s[k] for k in PUBLIC_SKILL_FIELDS if k in s} for s in body["skills"] if isinstance(s, dict)]
    paths = body.get("skill_paths")
    view: Dict[str, Any] = {"skills": skills, "count": len(skills)}
    if isinstance(paths, list):
        view["skill_paths"] = [p for p in paths if isinstance(p, str)]
    return view
