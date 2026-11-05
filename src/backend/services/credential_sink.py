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

**A destination (trinity-enterprise#1109).** A credential ask may say the
value is for an agent on ANOTHER Trinity (`context.credential_destination`,
`services/credential_destination.py`). Then steps 3-5 are replaced by
`_deliver_remote`: the asking agent must be running (its `.env` is read, by
NAME, for the key it holds to that instance — `destination_key_missing` when
it holds none), the URL is validated AGAIN at delivery time and the
connection pinned to the address it resolved to (the A2A transport), and one
`POST {instance}/api/agents/{agent}/credentials/inject` with
`{mode: "merge", env: {NAME: value}}` is sent under that key. Nothing is
written on this instance; after a 2xx the staged value is removed — nothing
rests here after delivery. Each failure is a named plain retry
(`destination_unreachable`, `destination_key_refused`,
`destination_agent_missing`, `destination_refused`); a connect failure
unstages, a timeout after the request left keeps the value staged and says
so. The audit row is `credential_delivered` with the destination's host and
agent — never the value, never the key.

**The receiving side.** `merge_env_values` is the same read-patch-inject leaf
(steps 3-5) exposed to the owner inject route's `mode: "merge"`, so a remote
delivery lands as one line set in place, not a whole-file rewrite.
"""

from __future__ import annotations

import json
import logging
from typing import Any, Dict, Optional

import httpx

from redis_breaker_util import SingleFlightLock, get_breaker_redis
from services import agent_env_patch, agent_env_read, credential_ask_policy, credential_requirements_service
from services import a2a_client, a2a_outbound, credential_destination, runtime_secret_scrub
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
#: One pinned request to another Trinity (trinity-enterprise#1109).
DESTINATION_HTTP_TIMEOUT_SECONDS = 30.0
AUDIT_ACTION = "chat_provide"
AUDIT_ACTION_DELIVERED = "credential_delivered"
#: Written BEFORE the request to another Trinity leaves (trinity-enterprise#1109).
AUDIT_ACTION_ATTEMPT = "credential_delivery_attempt"


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


def _destination_client() -> httpx.AsyncClient:
    """The outbound client for another Trinity (trinity-enterprise#1109): the
    A2A transport — no redirects, no proxy env, explicit TLS — reached through
    the module object so one seam serves both; a seam tests replace."""
    return a2a_client._http_client(httpx.Timeout(DESTINATION_HTTP_TIMEOUT_SECONDS))


def _check_name(name: object) -> str:
    problem = credential_ask_policy.credential_name_problem(name)
    if problem is not None:
        raise CredentialSinkError(
            422, "invalid_credential_name",
            "This ask names no environment variable the platform can set.",
            {"reason": problem})
    assert isinstance(name, str)
    return name


def _check_value(value: object, *, scrub_floor: bool = True) -> None:
    """Refuse a value the file or the scrub cannot carry. Only the SHAPE is
    named back; the value itself is never part of the refusal. The scrub's
    identity floor applies to a value this sink stages; the owner route's
    merge (`merge_env_values`) stages nothing and sets any one-line value."""
    if not isinstance(value, str) or not value.strip():
        raise CredentialSinkError(422, "invalid_value", "Type the credential's value.")
    if scrub_floor and len(value) < runtime_secret_scrub._MIN_VALUE_LEN:
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
    destination: Optional[Dict[str, str]] = None,
) -> None:
    """Set ``name`` to ``value`` in ``agent_name``'s `.env`, as the person
    ``actor_email`` answering ask ``ask_id``. Raises `CredentialSinkError`;
    returns nothing — the caller answers the ask by name.

    With a ``destination`` (trinity-enterprise#1109) the value is delivered to
    that agent on that instance instead, under the key ``agent_name`` holds
    for it; this instance keeps nothing."""
    name = _check_name(name)
    _check_value(value)

    # 2. the scrub, first and fail-closed
    try:
        staged_here = runtime_secret_scrub.stage_secret(agent_name, value)
    except runtime_secret_scrub.StagingUnavailable:
        logger.warning("[CredentialSink] %s for agent %s not stored: scrub store unavailable "
                       "(ask %s)", name, agent_name, ask_id)
        raise _store_unavailable("stage") from None

    async def _audit(action: str, extra: Dict[str, Any]) -> None:
        """One `credentials` audit row — names only — best-effort like every
        other audit write: the delivery stands whether or not the row does."""
        try:
            await platform_audit_service.log(
                event_type=AuditEventType.CREDENTIALS,
                event_action=action,
                source="api",
                actor_email=actor_email,
                actor_ip=actor_ip,
                target_type="agent",
                target_id=agent_name,
                endpoint=endpoint,
                request_id=request_id,
                details={"name": name, "ask_id": ask_id, **extra},
            )
        except Exception:  # noqa: BLE001
            logger.warning("[CredentialSink] audit row %s skipped for %s through ask %s",
                           action, name, ask_id, exc_info=True)

    try:
        if destination is not None:
            await _deliver_remote(agent_name, destination, name, value, audit=_audit)
        else:
            await _deliver(agent_name, {name: value})
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

    if destination is not None:
        # Delivered elsewhere: nothing of the value rests on this instance, so
        # the staging that protected the hand-over is taken back too. (A
        # staging someone else holds stays theirs.)
        if staged_here and not runtime_secret_scrub.unstage_secret(value):
            logger.warning("[CredentialSink] %s delivered for ask %s; the staged value could not "
                           "be removed and expires with the scrub TTL", name, ask_id)
        view = credential_destination.public_view(destination) or {}
        logger.info("[CredentialSink] %s delivered to agent %s on %s through ask %s (asked by %s)",
                    name, view.get("agent_name"), view.get("host"), ask_id, agent_name)
        await _audit(AUDIT_ACTION_DELIVERED,
                     {"destination": {"host": view.get("host"),
                                      "agent_name": view.get("agent_name")}})
        return

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


async def merge_env_values(agent_name: str, values: Dict[str, str]) -> None:
    """Set every ``values`` line in ``agent_name``'s `.env` in place
    (trinity-enterprise#1109): the owner inject route's `mode: "merge"`. The
    same leaf as the sink's local write — the agent running, the lease, read,
    `set_keys`, inject — so a remote delivery never rewrites the whole file.
    Names and values are checked first; the caller audits."""
    checked: Dict[str, str] = {}
    for key, item in values.items():
        checked[_check_name(key)] = item
        _check_value(item, scrub_floor=False)
    if not checked:
        raise CredentialSinkError(422, "invalid_value", "Name at least one variable to set.")
    await _deliver(agent_name, checked)


async def _deliver(agent_name: str, values: Dict[str, str]) -> None:
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
                patched = agent_env_patch.set_keys(env_text, values)
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


def _destination_failure(code: str, message: str, data: Optional[Dict[str, Any]] = None, *,
                         status_code: int = 409, may_have_written: bool = False) -> CredentialSinkError:
    return CredentialSinkError(status_code, code, message, data, may_have_written=may_have_written)


async def _read_key_by_name(agent_name: str, key_env: str) -> str:
    """The key ``agent_name`` holds under ``key_env`` in its own `.env`, read
    through its own read endpoint. Absent → `destination_key_missing`; one
    that cannot travel as a header value (whitespace, control or non-ASCII
    characters — h11 would refuse it by ECHOING it, the ent#109 class) →
    `destination_key_invalid`. The key's variable NAME is never in the
    refusal: it describes the asking agent's environment, and the person is
    told what to do, not what it is called."""
    state = _running_state(agent_name)
    if state is not None:
        raise CredentialSinkError(
            409, "agent_not_running",
            f"The agent that asked is not running (it is {state}), so nothing was stored; "
            "start it and send the credential again.",
            {"status": state})
    async with _agent_client(agent_name) as client:
        env_text = await _read_env(client, agent_name)
    found = agent_env_patch.values_of(env_text, key_env)
    key = found[-1] if found else ""
    if not key.strip():
        raise _destination_failure(
            "destination_key_missing",
            "The agent that asked does not hold a key to that Trinity yet, so nothing was "
            "stored. Once it has one, send the credential again.")
    if not a2a_outbound._HEADER_SAFE_CREDENTIAL.match(key):
        raise _destination_failure(
            "destination_key_invalid",
            "The key the agent that asked holds for that Trinity cannot be sent as it is "
            "(it contains whitespace, control or non-ASCII characters), so nothing was stored. "
            "Once the key is fixed, send the credential again.")
    return key


def _not_deliverable(exc: credential_destination.DestinationProblem) -> CredentialSinkError:
    """The switch or the registry refused at delivery time: a 422 by the same
    name the raise would have used, nothing stored."""
    return CredentialSinkError(
        422, exc.code,
        ("Outbound A2A is switched off on this Trinity, so the credential was not sent and "
         "nothing was stored. An admin can allow it under "
         f"{credential_destination.REGISTRY_LOCATION}; then send the value again.")
        if exc.code == "destination_outbound_disabled" else
        ("That Trinity is not one an admin has approved for the agent that asked, so the "
         "credential was not sent and nothing was stored. An admin registers it under "
         f"{credential_destination.REGISTRY_LOCATION}; then send the value again."),
        {"reason": exc.reason})


async def _deliver_remote(agent_name: str, destination: Dict[str, str], name: str,
                          value: str, *, audit) -> None:
    """trinity-enterprise#1109: one merge inject on the destination instance,
    under the key the asking agent holds for it. Every `CredentialSinkError`
    raised before the request leaves says nothing was stored, and means it.

    Order: the outbound A2A switch and the admin's endpoint registry (both
    re-read now — an admin may have flipped or removed either since the ask
    was raised), the asking agent's key by name, the URL resolved OFF the
    loop (`a2a_client.validate_endpoint`), the attempt audited, then ONE
    pinned request through the A2A client's capped reader under its total
    wall-clock deadline — the error body is never read, a redirect is a
    failure, the body ceiling is the client's own.
    """
    instance_url = destination["instance_url"]
    try:
        credential_destination.check_deliverable(agent_name, instance_url)
    except credential_destination.DestinationProblem as exc:
        raise _not_deliverable(exc) from None
    key = await _read_key_by_name(agent_name, destination["key_env"])
    try:
        validated = await a2a_client.validate_endpoint(instance_url)
    except a2a_client.A2ACallError as exc:
        raise CredentialSinkError(
            422, "destination_url_invalid",
            "That Trinity's address no longer passes the platform's checks, so nothing was "
            "stored. Ask the agent to request the credential again.",
            {"reason": exc.reason}) from None
    target_agent = destination["agent_name"]
    route = f"{validated.url.rstrip('/')}/api/agents/{target_agent}/credentials/inject"
    pinned = a2a_client._pinned_url(route, validated.addresses[0])
    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json",
    }
    # Built outside the call, so the value sits in a dict and nowhere else
    # (the AST guard reads every call the name `value` is an argument of).
    body = {"mode": "merge", "env": {name: value}}
    content = json.dumps(body).encode("utf-8")
    view = credential_destination.public_view(destination) or {}
    host = view.get("host")
    # The attempt is recorded BEFORE the request leaves: a destination that
    # stores the value and then answers 503 (its own "may or may not") must
    # leave a trace here either way.
    await audit(AUDIT_ACTION_ATTEMPT, {"destination": {"host": host,
                                                       "agent_name": view.get("agent_name")}})
    try:
        async with _destination_client() as client:
            await a2a_client._with_deadline(a2a_client._read_capped(
                client, "POST", pinned,
                sni=validated.hostname,
                host_header=a2a_client._host_header(validated),
                max_bytes=a2a_client.A2A_ERROR_BODY_MAX_BYTES,
                headers=headers,
                content=content,
                error_prefix="inject",
                secret=key,
            ))
    except a2a_client.A2ACallError as exc:
        raise _remote_failure(exc, host, target_agent) from None


def _remote_failure(exc: "a2a_client.A2ACallError", host: Optional[str],
                    target_agent: str) -> CredentialSinkError:
    """The A2A client's named transport outcome → the person's named retry.
    `exc.message` is never forwarded: it is built from a peer-controlled
    string, and the person needs the host and what to do, not the wire."""
    reason = exc.reason
    status = getattr(exc, "remote_status", None)
    if reason == "inject_unreachable":
        return _destination_failure(
            "destination_unreachable",
            f"The Trinity at {host} could not be reached, so nothing was stored. "
            "Check it is up and send the credential again.",
            {"reason": reason}, status_code=503)
    if reason == "timeout":
        return _destination_failure(
            "destination_unreachable",
            f"The Trinity at {host} did not answer in time, so the credential may "
            "or may not have been stored there. Check the agent there and send it again if "
            "it is missing.",
            {"may_have_written": True, "reason": reason}, status_code=503, may_have_written=True)
    if reason == "inject_http_error" and status in (401, 403):
        return _destination_failure(
            "destination_key_refused",
            f"The Trinity at {host} refused the key the agent that asked holds for "
            "it, so nothing was stored. Once the key is fixed, send the credential again.",
            {"status": status})
    if reason == "inject_http_error" and status == 404:
        return _destination_failure(
            "destination_agent_missing",
            f"The Trinity at {host} reports no agent named {target_agent} that this "
            "key can reach, so nothing was stored. Once the agent exists there, send the "
            "credential again.",
            {"status": status})
    if reason == "inject_http_error" and status is not None and status >= 500:
        # The receiving inject route answers 503 for exactly "the agent did
        # not confirm it stored the credential, so it may or may not be in
        # its file" — a 5xx is that sentence, not a refusal.
        return _destination_failure(
            "destination_unconfirmed",
            f"The Trinity at {host} did not confirm it stored the credential (HTTP {status}), "
            "so it may or may not be in the agent's file there. Check the agent there and "
            "send it again if it is missing.",
            {"status": status, "may_have_written": True}, status_code=503, may_have_written=True)
    if reason in ("inject_encoding", "inject_too_large"):
        # An answer arrived that the capped reader would not take; what it said
        # is unknown, so the value is treated as possibly stored.
        return _destination_failure(
            "destination_unconfirmed",
            f"The Trinity at {host} answered in a form this platform does not read, so the "
            "credential may or may not have been stored there. Check the agent there and "
            "send it again if it is missing.",
            {"reason": reason, "may_have_written": True}, status_code=503, may_have_written=True)
    data: Dict[str, Any] = {"reason": reason}
    if status is not None:
        data["status"] = status
    said = f"HTTP {status}" if status is not None else reason.replace("inject_", "")
    return _destination_failure(
        "destination_refused",
        f"The Trinity at {host} did not accept the credential ({said}), so "
        "nothing was stored there. Check the agent there is running and send it again.",
        data)
