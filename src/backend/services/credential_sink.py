"""The secret sink — where a credential a person typed into the Workspace
secret field goes (trinity-enterprise#1066).

ONE leaf handles the value. It arrives here from exactly one caller
(`client_portal.asks.service.provide_credential`, behind the person-only
Workspace route), is written to exactly one place — one line of the agent's
`.env`, through the agent's own `GET /api/credentials/read` and
`POST /api/credentials/inject`, the same curated-path write
`routers/credentials.py` and the Nevermined bind use — and reaches nothing a
reader sees: no log line, no audit field, no refusal message, no exception
text. Every name in this module's log and audit output is the agent, the
variable NAME and the ask id. `tests/unit/test_ent1066_credential_sink.py`
holds an AST guard over this file: the name `value` may be measured, staged,
patched into the file or passed on, never formatted, logged, concatenated or
raised.

Order, each step a named refusal (`CredentialSinkError`) that leaves the
agent untouched:

1. the NAME re-checked against the ask policy (a file-channel row validated
   nothing) and the value checked (`invalid_value`: empty, below the scrub's
   identity floor, over `VALUE_MAX_BYTES`, or carrying a line break — the
   agent's reader is line-oriented and a second line would define a second
   key — or NUL);
2. STAGE into the runtime identity scrub FIRST, and fail CLOSED
   (`scrub_unavailable`, 503): the "never in a persisted transcript" guarantee
   is the scrub's, and delivering a value the scrub cannot see is a silent
   downgrade — the same asymmetry the vault chose (ent#279). A refusal in
   steps 3-5 that left the agent's file untouched UNSTAGES what this call
   staged, so its "nothing was stored" holds for the scrub too; a failed
   inject keeps it staged (the agent may have written) and says so;
3. the agent must be running (`agent_not_running`, 409, naming the state):
   inject requires it, and a retry after Start is cheap;
4. one writer per agent at a time: a short Redis lease (`env_busy`, 409 while
   another sink write holds it; a lease the store cannot grant is the same
   503 as step 2 — this is the store step 2 already needed). The write below
   is read-modify-write of the whole file, so two sink writes racing would
   drop each other's key; the lease serialises THIS writer's calls across
   workers. The owner's Credentials-tab inject (`routers/credentials.py`) is a
   whole-file overwrite that does not take the lease and keeps its existing
   last-writer-wins contract — a documented residual, not closed here;
5. read `.env` — only a file the agent proves absent counts as empty; one
   it could not hand back as text is `env_unreadable` (409) and left as it
   is — set ONE line through `agent_env_patch.set_keys` (the #2023
   quoting the agent's reader reverses, in place, duplicates collapsed, every
   other byte kept), refuse a result over `ENV_MAX_BYTES` (`env_too_large`,
   422 — the agent's reader ignores a file over its own 1 MiB cap, so a file
   this sink grew past that would silently drop EVERY credential), inject it
   back (`agent_unreachable`, 503, on either leg);
6. audit `credentials` / `chat_provide` — the NAME, the ask id, the file —
   and drop the requirements report cache (ent#127, so the checklist shows
   the variable set on its next read).

The caller answers the ask afterwards, by name, through the ask sink.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, Optional

import httpx

from redis_breaker_util import SingleFlightLock, get_breaker_redis
from services import agent_env_patch, agent_env_read, credential_ask_policy, credential_requirements_service
from services import runtime_secret_scrub
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container, get_agent_status_from_container
from services.platform_audit_service import AuditEventType, platform_audit_service

logger = logging.getLogger(__name__)

#: The longest value the field accepts. A certificate or a service-account
#: JSON blob fits; anything larger is not a credential a person pastes.
VALUE_MAX_BYTES = 32 * 1024
#: The largest `.env` this sink will write back. Under the agent reader's own
#: 1 MiB cap (`execution_env._ENV_FILE_MAX_BYTES`) by a wide margin.
ENV_MAX_BYTES = 256 * 1024
#: How long one write may hold the agent's lease. The two agent calls are
#: bounded by `AGENT_HTTP_TIMEOUT_SECONDS` each.
LOCK_TTL_SECONDS = 90
AGENT_HTTP_TIMEOUT_SECONDS = 30.0
AUDIT_ACTION = "chat_provide"


class CredentialSinkError(Exception):
    """A named refusal. `status_code` / `code` / `message` are what the route
    answers with; `data` is what a client can act on (the agent's state). The
    message never carries the value — see the AST guard."""

    def __init__(self, status_code: int, code: str, message: str,
                 data: Optional[Dict[str, Any]] = None, *, may_have_written: bool = False):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.data = data or {}
        # True only on the inject leg: the agent may have written the file
        # before failing, so the value stays staged in the scrub.
        self.may_have_written = may_have_written


def _agent_client(agent_name: str) -> httpx.AsyncClient:
    """Pre-authenticated client for `agent-{name}:8000`; a seam tests replace."""
    return agent_httpx_client(agent_name, timeout=AGENT_HTTP_TIMEOUT_SECONDS)


def _check_name(name: object) -> str:
    problem = credential_ask_policy.credential_name_problem(name)
    if problem is not None:
        raise CredentialSinkError(
            422, "invalid_credential_name",
            "This ask names no environment variable the platform can set.",
            {"reason": problem})
    assert isinstance(name, str)
    return name


def _check_value(value: object) -> None:
    """Refuse a value the file or the scrub cannot carry. Only the SHAPE is
    named back; the value itself is never part of the refusal."""
    if not isinstance(value, str) or not value.strip():
        raise CredentialSinkError(422, "invalid_value", "Type the credential's value.")
    if len(value) < runtime_secret_scrub._MIN_VALUE_LEN:
        raise CredentialSinkError(
            422, "invalid_value",
            f"A credential is at least {runtime_secret_scrub._MIN_VALUE_LEN} characters.")
    if len(value.encode("utf-8")) > VALUE_MAX_BYTES:
        raise CredentialSinkError(
            422, "invalid_value",
            f"A credential is at most {VALUE_MAX_BYTES // 1024} KiB.")
    if "\x00" in value or value.splitlines() != [value]:
        raise CredentialSinkError(
            422, "invalid_value",
            "A credential is one line: no line breaks and no NUL characters.")


def _running_state(agent_name: str) -> Optional[str]:
    """None when the agent is running, else the state to name (`missing` for
    no container at all)."""
    container = get_agent_container(agent_name)
    if container is None:
        return "missing"
    state = get_agent_status_from_container(container).status
    return None if state == "running" else (state or "unknown")


def _store_unavailable(what: str) -> CredentialSinkError:
    return CredentialSinkError(
        503, "scrub_unavailable",
        "The credential could not be protected right now; nothing was stored. Try again "
        "in a moment.",
        {"step": what})


def _agent_unreachable(what: str) -> CredentialSinkError:
    if what == "inject":
        return CredentialSinkError(
            503, "agent_unreachable",
            "The agent did not confirm it stored the credential, so it may or may not be in "
            "its file. Check the agent is running and send the value again.",
            {"step": what}, may_have_written=True)
    return CredentialSinkError(
        503, "agent_unreachable",
        "The agent's credential file could not be read, so nothing was stored. Check the "
        "agent is running and try again.",
        {"step": what})


def _env_unreadable(reason: str) -> CredentialSinkError:
    return CredentialSinkError(
        409, "env_unreadable",
        "The agent's credential file could not be read as text, so it was left exactly as "
        "it is and nothing was stored. Ask whoever runs the agent to check its .env file.",
        {"reason": reason})


async def _read_env(client: httpx.AsyncClient, agent_name: str) -> str:
    """The agent's `.env` as text, or "" ONLY when the agent proved there is no
    file. The write that follows replaces the whole file, so a file the agent
    did not hand back as text — non-UTF-8 bytes (`files_b64`), a read error,
    or an agent image too old to say "absent" (all three omit the path from
    `files`) — is refused, never treated as empty: an empty read here would
    delete every other credential on the agent."""
    try:
        response = await client.get(
            f"http://agent-{agent_name}:8000/api/credentials/read", params={"paths": ".env"},
        )
    except httpx.RequestError:
        raise _agent_unreachable("read") from None
    if response.status_code != 200:
        raise _agent_unreachable("read")
    try:
        state, content = agent_env_read.classify_env_read(response.json())
    except ValueError:
        raise _agent_unreachable("read") from None
    if state in (agent_env_read.TEXT, agent_env_read.ABSENT):
        return content
    raise _env_unreadable(state)  # "not_utf8" / "unconfirmed", as before


async def _inject_env(client: httpx.AsyncClient, agent_name: str, env_text: str) -> None:
    try:
        response = await client.post(
            f"http://agent-{agent_name}:8000/api/credentials/inject",
            json={"files": {".env": env_text}},
        )
    except httpx.RequestError:
        raise _agent_unreachable("inject") from None
    if response.status_code != 200:
        raise _agent_unreachable("inject")


async def set_env_value(
    agent_name: str,
    name: str,
    value: str,
    *,
    actor_email: str,
    ask_id: str,
    actor_ip: Optional[str] = None,
    endpoint: Optional[str] = None,
    request_id: Optional[str] = None,
) -> None:
    """Set ``name`` to ``value`` in ``agent_name``'s `.env`, as the person
    ``actor_email`` answering ask ``ask_id``. Raises `CredentialSinkError`;
    returns nothing — the caller answers the ask by name."""
    name = _check_name(name)
    _check_value(value)

    # 2. the scrub, first and fail-closed
    try:
        staged_here = runtime_secret_scrub.stage_secret(agent_name, value)
    except runtime_secret_scrub.StagingUnavailable:
        logger.warning("[CredentialSink] %s for agent %s not stored: scrub store unavailable "
                       "(ask %s)", name, agent_name, ask_id)
        raise _store_unavailable("stage") from None

    try:
        await _deliver(agent_name, name, value)
    except CredentialSinkError as e:
        # A refusal that left the agent's file untouched takes back what this
        # call staged, so "nothing was stored" is true of the scrub as well. The
        # inject leg keeps it: the agent may have written before failing.
        if staged_here and not e.may_have_written:
            if not runtime_secret_scrub.unstage_secret(value):
                logger.warning("[CredentialSink] %s for agent %s refused (%s); the staged value "
                               "could not be removed and expires with the scrub TTL (ask %s)",
                               name, agent_name, e.code, ask_id)
        raise

    # 6. the record — names only — and the checklist cache
    logger.info("[CredentialSink] %s set on agent %s through ask %s", name, agent_name, ask_id)
    try:
        await platform_audit_service.log(
            event_type=AuditEventType.CREDENTIALS,
            event_action=AUDIT_ACTION,
            source="api",
            actor_email=actor_email,
            actor_ip=actor_ip,
            target_type="agent",
            target_id=agent_name,
            endpoint=endpoint,
            request_id=request_id,
            details={"name": name, "ask_id": ask_id, "files": [".env"]},
        )
    except Exception:  # noqa: BLE001 — the write stands; audit is best-effort like every other
        logger.warning("[CredentialSink] audit row skipped for %s on agent %s", name, agent_name,
                       exc_info=True)
    try:
        credential_requirements_service.invalidate_report_cache(agent_name)
    except Exception:  # noqa: BLE001
        logger.warning("[CredentialSink] report cache not invalidated for agent %s", agent_name,
                       exc_info=True)


async def _deliver(agent_name: str, name: str, value: str) -> None:
    """Steps 3-5: the agent running, the lease, read-patch-inject. Every
    `CredentialSinkError` before the inject leaves the agent's file untouched."""
    # 3. the agent must be running
    state = _running_state(agent_name)
    if state is not None:
        raise CredentialSinkError(
            409, "agent_not_running",
            f"The agent is not running (it is {state}), so nothing was stored; start it and "
            "send the credential again.",
            {"status": state})

    # 4. one writer per agent
    lock = SingleFlightLock(f"credential_sink:lock:{agent_name}", LOCK_TTL_SECONDS,
                            client=get_breaker_redis())
    if not lock.acquire():
        raise CredentialSinkError(
            409, "env_busy",
            "Another credential is being stored on this agent right now, so nothing was "
            "stored. Send it again in a moment.")
    if not lock.held:
        # fail-open is the lock's default; this writer fails CLOSED — the same
        # store the scrub needed a moment ago, so it is the same outage.
        raise _store_unavailable("lock")
    try:
        async with _agent_client(agent_name) as client:
            env_text = await _read_env(client, agent_name)
            try:
                patched = agent_env_patch.set_keys(env_text, {name: value})
            except ValueError:
                # belt: `_check_value` already refused every shape the writer refuses
                raise CredentialSinkError(422, "invalid_value",
                                          "A credential is one line; nothing was stored.") from None
            if len(patched.encode("utf-8")) > ENV_MAX_BYTES:
                raise CredentialSinkError(
                    422, "env_too_large",
                    f"The agent's credential file would exceed {ENV_MAX_BYTES // 1024} KiB; "
                    "nothing was stored.")
            await _inject_env(client, agent_name, patched)
    finally:
        lock.release_if_owned()
