"""
Nevermined card delegation (trinity-enterprise#965/#966).

The buyer side of Nevermined, built beside the provider-side paywall in
``nevermined_payment_service`` and sharing none of its gates. Nevermined holds
the card, holds the mandate and settles every purchase; Trinity keeps only the
organization config and the delegation id a person bound to an agent.

Upstream calls are plain ``httpx`` (payments-py has no widget-session, revoke
or transactions call), each inside :func:`delegation_slot` — its own gate, so a
panel can never starve the paid-chat verify/settle path. The API base is a
fixed two-entry map keyed by the configured environment, never built from
input.

Secrets (widget secret, agent API key, a minted session token) are never logged
and never returned except the session itself, to the person who asked for it.
Upstream failures are logged by status code only — never the response body.
"""

import asyncio
import contextlib
import logging
import os
import re
import weakref
from typing import Any, Dict, List, Optional

import httpx
from sqlalchemy.exc import IntegrityError

from database import db
from services import agent_env_patch
from services.agent_auth import agent_httpx_client
from services.credential_paths import disallowed_paths
from services.docker_service import get_agent_container, get_agent_status_from_container
from utils.helpers import utc_now_iso

logger = logging.getLogger(__name__)

#: Environment → Nevermined API base. A table lookup on a two-value enum, so
#: the outbound host can never be steered by a request.
NEVERMINED_API_BASES: Dict[str, str] = {
    "sandbox": "https://api.sandbox.nevermined.app",
    "live": "https://api.live.nevermined.app",
}

SECRET_FIELDS = ("widget_secret", "agent_api_key")

#: Delegation traffic gets its OWN gate (the module precedent is
#: `nevermined_payment_service.plan_lookup_slot`): a polling panel must never
#: hold a slot the paid-chat verify/settle path needs.
NEVERMINED_DELEGATION_MAX_INFLIGHT = int(
    os.getenv("NEVERMINED_DELEGATION_MAX_INFLIGHT", "4")
)
DELEGATION_SLOT_WAIT_SECONDS = 5.0
UPSTREAM_TIMEOUT_SECONDS = 10.0

_DELEGATION_GATES: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


class DelegationError(Exception):
    """A named refusal: rendered as ``{"detail": {"code", "message"}}``."""

    def __init__(
        self,
        status_code: int,
        code: str,
        message: str,
        *,
        headers: Optional[Dict[str, str]] = None,
        extra: Optional[Dict[str, Any]] = None,
    ):
        super().__init__(code)
        self.status_code = status_code
        self.code = code
        self.message = message
        self.headers = headers
        self.extra = extra or {}

    def detail(self) -> Dict[str, Any]:
        return {"code": self.code, "message": self.message, **self.extra}


class DelegationBusy(Exception):
    """No delegation slot became free within the wait budget."""


def _delegation_gate() -> asyncio.Semaphore:
    loop = asyncio.get_running_loop()
    gate = _DELEGATION_GATES.get(loop)
    if gate is None:
        gate = asyncio.Semaphore(NEVERMINED_DELEGATION_MAX_INFLIGHT)
        _DELEGATION_GATES[loop] = gate
    return gate


@contextlib.asynccontextmanager
async def delegation_slot():
    """Hold one delegation slot, or raise `DelegationBusy`."""
    gate = _delegation_gate()
    try:
        await asyncio.wait_for(gate.acquire(), timeout=DELEGATION_SLOT_WAIT_SECONDS)
    except asyncio.TimeoutError:
        raise DelegationBusy("delegation concurrency limit reached") from None
    try:
        yield
    finally:
        gate.release()


def _not_configured() -> DelegationError:
    return DelegationError(
        409,
        "nevermined_not_configured",
        "No Nevermined organization is configured on this instance. "
        "An admin sets it under Settings before an agent can be funded through Nevermined.",
    )


def _delegations_active(count: int, what: str) -> DelegationError:
    return DelegationError(
        409,
        "delegations_active",
        f"{count} agent(s) hold an active Nevermined mandate; {what}. "
        "Revoke those mandates first.",
        extra={"active_delegations": count},
    )


# ---------------------------------------------------------------------------
# Organization (admin)
# ---------------------------------------------------------------------------

def organization_state() -> Dict[str, Any]:
    """The GET body: always a state, never a 404 and never a secret."""
    org = db.get_nevermined_organization()
    active = db.count_active_agent_delegations()
    if not org:
        return {
            "configured": False,
            "org_id": None,
            "environment": None,
            "updated_at": None,
            "updated_by": None,
            "active_delegations": active,
        }
    return {
        "configured": True,
        "org_id": org["org_id"],
        "environment": org["environment"],
        "updated_at": org["updated_at"],
        "updated_by": org["updated_by"],
        "active_delegations": active,
    }


def _key_environment_prefix(api_key: str) -> Optional[str]:
    """`sandbox:` / `live:` prefix the Nevermined SDK understands, if present."""
    head, sep, _ = api_key.partition(":")
    if sep and head in NEVERMINED_API_BASES:
        return head
    return None


def save_organization(
    org_id: str,
    environment: str,
    widget_secret: Optional[str],
    agent_api_key: Optional[str],
    updated_by: Optional[str],
) -> Dict[str, Any]:
    """Create or update the organization. Returns ``{state, rotated, created}``.

    A secret left out keeps the stored one; on first create both are required.
    """
    if agent_api_key:
        prefix = _key_environment_prefix(agent_api_key)
        if prefix and prefix != environment:
            raise DelegationError(
                422,
                "environment_mismatch",
                f"The Nevermined agent API key is a '{prefix}' key but the "
                f"environment is '{environment}'. Use a key for the same Nevermined environment.",
            )

    current = db.get_nevermined_organization()
    stored = db.get_nevermined_organization_secrets() if current else None
    if not current or not stored:
        missing = [
            name for name, value in (("widget_secret", widget_secret), ("agent_api_key", agent_api_key))
            if not value
        ]
        if missing:
            raise DelegationError(
                422,
                "secret_required",
                "The first Nevermined organization setup needs both secrets: "
                + ", ".join(missing) + " missing.",
                extra={"missing": missing},
            )
        stored = {}

    rotated: List[str] = [
        name for name, value in (("widget_secret", widget_secret), ("agent_api_key", agent_api_key))
        if value and value != stored.get(name)
    ]

    if current:
        changes_env = environment != current["environment"]
        rotates_key = "agent_api_key" in rotated
        if changes_env or rotates_key:
            active = db.count_active_agent_delegations()
            if active:
                what = (
                    "they hold keys for the current Nevermined environment"
                    if changes_env
                    else "they hold the current Nevermined agent API key"
                )
                raise _delegations_active(active, what)

    secrets = {
        "widget_secret": widget_secret or stored.get("widget_secret"),
        "agent_api_key": agent_api_key or stored.get("agent_api_key"),
    }
    db.save_nevermined_organization(org_id, environment, secrets, updated_by)
    return {"state": organization_state(), "rotated": rotated, "created": not current}


def delete_organization() -> bool:
    """Remove the organization; refused while any live agent holds a mandate."""
    active = db.count_active_agent_delegations()
    if active:
        raise _delegations_active(active, "deleting the organization would strand them")
    return db.delete_nevermined_organization()


# ---------------------------------------------------------------------------
# Upstream (Nevermined) — one call per request, inside delegation_slot()
# ---------------------------------------------------------------------------

def _http_client() -> httpx.AsyncClient:
    """The outbound client; a seam tests replace with a mock transport."""
    return httpx.AsyncClient(timeout=UPSTREAM_TIMEOUT_SECONDS)


def _busy() -> DelegationError:
    return DelegationError(
        503,
        "nevermined_busy",
        "Too many Nevermined requests are in flight on this instance. Try again in a few seconds.",
        headers={"Retry-After": "5"},
    )


def _unreachable() -> DelegationError:
    return DelegationError(
        503,
        "nevermined_unreachable",
        "Nevermined did not answer. Nothing was changed; try again shortly.",
    )


def _rejected(upstream_status: Optional[int], message: str) -> DelegationError:
    return DelegationError(
        502, "nevermined_rejected", message, extra={"upstream_status": upstream_status}
    )


def _configured_organization() -> Dict[str, Any]:
    """Organization row + decrypted secrets, or 409 `nevermined_not_configured`."""
    org = db.get_nevermined_organization()
    secrets = db.get_nevermined_organization_secrets() if org else None
    if not org or not secrets:
        raise _not_configured()
    return {**org, **secrets}


async def mint_widget_session(email: Optional[str], *, agent_name: str, actor: str) -> Dict[str, Any]:
    """Mint a Nevermined widget session for the signed-in person.

    Server-to-server ``POST {api_base}/api/v1/widgets/session``; the session is
    returned verbatim to the caller and never logged.
    """
    org = _configured_organization()
    email = (email or "").strip()
    if not email:
        raise DelegationError(
            422,
            "email_required",
            "Nevermined widget sessions are minted for the signed-in person's email, "
            "and this account has none. Add a sign-in email, then try again.",
        )
    environment = org["environment"]
    url = NEVERMINED_API_BASES[environment] + "/api/v1/widgets/session"
    payload = {"orgId": org["org_id"], "email": email, "rawSecret": org["widget_secret"]}

    try:
        async with delegation_slot():
            async with _http_client() as client:
                response = await client.post(url, json=payload)
    except DelegationBusy:
        logger.warning("Nevermined widget session for %s by %s: delegation gate busy", agent_name, actor)
        raise _busy() from None
    except httpx.HTTPError as exc:
        # The exception class only — its text can carry the request.
        logger.warning(
            "Nevermined widget session for %s by %s: unreachable (%s)",
            agent_name, actor, type(exc).__name__,
        )
        raise _unreachable() from None

    status = response.status_code
    logger.info(
        "Nevermined widget session for %s by %s: org=%s environment=%s upstream_status=%s",
        agent_name, actor, org["org_id"], environment, status,
    )
    if status in (401, 403):
        raise _rejected(
            status,
            "Nevermined refused the widget session. The organization id or widget secret "
            "configured for Nevermined is likely wrong; an admin can update them under Settings.",
        )
    if not 200 <= status < 300:
        raise _rejected(status, f"Nevermined refused the widget session (HTTP {status}).")
    try:
        session = response.json()
    except ValueError:
        raise _rejected(status, "Nevermined answered the widget session request with an unreadable body.") from None
    if not isinstance(session, dict):
        raise _rejected(status, "Nevermined answered the widget session request with an unexpected body.")
    return {"session": session, "environment": environment, "org_id": org["org_id"]}


# ---------------------------------------------------------------------------
# Delegation read-back (GET) — one upstream call per delegation id in flight
# ---------------------------------------------------------------------------

#: A Nevermined delegation / payment-method id is a UUID-like token. Pinned
#: before ANY interpolation into an upstream path (plan §10): no `/`, `?`, `.`.
DELEGATION_ID_RE = re.compile(r"^[A-Za-z0-9_-]{8,128}$")

#: Nevermined field → our field. Every number is COPIED from the named
#: Nevermined field or left null; nothing is derived locally.
LIVE_NUMBER_FIELDS = (
    ("remainingBudgetCents", "remaining_cents"),
    ("spendingLimitCents", "spending_limit_cents"),
    ("amountSpentCents", "spent_cents"),
    ("transactionCount", "transactions_used"),
    ("maxTransactions", "max_transactions"),
)
LIVE_KNOWN_STATUSES = ("active", "exhausted", "expired", "revoked")

_READ_INFLIGHT: "weakref.WeakKeyDictionary" = weakref.WeakKeyDictionary()


class InvalidDelegationId(ValueError):
    """An id that must not reach an upstream URL."""


def validate_delegation_id(value: str, *, what: str = "delegation id") -> str:
    if not isinstance(value, str) or not DELEGATION_ID_RE.match(value):
        raise InvalidDelegationId(f"{what} must match [A-Za-z0-9_-]{{8,128}}")
    return value


def _delegation_url(environment: str, delegation_id: str, suffix: str = "") -> str:
    validate_delegation_id(delegation_id)
    return f"{NEVERMINED_API_BASES[environment]}/api/v1/delegation/{delegation_id}{suffix}"


def _empty_live(source: str, status: str, error: Optional[str]) -> Dict[str, Any]:
    live = {"source": source, "status": status}
    live.update({ours: None for _, ours in LIVE_NUMBER_FIELDS})
    live.update({"currency": None, "expires_at": None, "fetched_at": utc_now_iso(), "error": error})
    return live


def _live_candidates(payload: Any):
    """The dict that carries the delegation's fields: the body itself, or one
    of the envelopes a REST API tends to use. The shape is undocumented."""
    if not isinstance(payload, dict):
        return
    yield payload
    for key in ("delegation", "data", "result"):
        inner = payload.get(key)
        if isinstance(inner, dict):
            yield inner


def parse_live(payload: Any) -> Dict[str, Any]:
    """Tolerant parse of Nevermined's delegation read: fields copied by name,
    `status` lowercased when present (`status_present` says whether it was),
    `unknown` otherwise. Never computes a number."""
    live = _empty_live("nevermined", "unknown", None)
    live["status_present"] = False
    for candidate in _live_candidates(payload):
        hit = False
        status = candidate.get("status")
        if isinstance(status, str) and status.strip():
            live["status"] = status.strip().lower()
            live["status_present"] = True
            hit = True
        for theirs, ours in LIVE_NUMBER_FIELDS:
            value = candidate.get(theirs)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                live[ours] = value
                hit = True
        currency = candidate.get("currency")
        if isinstance(currency, str):
            live["currency"] = currency
            hit = True
        expires = candidate.get("expiresAt")
        if isinstance(expires, str):
            live["expires_at"] = expires
            hit = True
        if hit:
            break
    return live


async def _upstream_delegation_get(org: Dict[str, Any], delegation_id: str) -> httpx.Response:
    """ONE `GET /api/v1/delegation/{id}/transactions?offset=0`, inside the gate.

    Raises `DelegationBusy` / `httpx.HTTPError`; the caller classifies.
    """
    url = _delegation_url(org["environment"], delegation_id, "/transactions")
    headers = {"Authorization": f"Bearer {org['agent_api_key']}"}
    async with delegation_slot():
        async with _http_client() as client:
            return await client.get(url, params={"offset": 0}, headers=headers)


def _read_inflight() -> Dict[str, "asyncio.Future"]:
    loop = asyncio.get_running_loop()
    inflight = _READ_INFLIGHT.get(loop)
    if inflight is None:
        inflight = {}
        _READ_INFLIGHT[loop] = inflight
    return inflight


async def _fetch_live_once(org: Dict[str, Any], delegation_id: str, *, agent_name: str) -> Dict[str, Any]:
    try:
        response = await _upstream_delegation_get(org, delegation_id)
    except DelegationBusy:
        logger.warning("Nevermined delegation read for %s: delegation gate busy", agent_name)
        return _empty_live("unavailable", "unknown", "nevermined_busy")
    except httpx.HTTPError as exc:
        logger.warning(
            "Nevermined delegation read for %s: unreachable (%s)", agent_name, type(exc).__name__,
        )
        return _empty_live("unavailable", "unknown", "nevermined_unreachable")
    except InvalidDelegationId:
        logger.error("Nevermined delegation read for %s: stored delegation id is malformed", agent_name)
        return _empty_live("unavailable", "unknown", "nevermined_rejected")

    status = response.status_code
    logger.info("Nevermined delegation read for %s: upstream_status=%s", agent_name, status)
    if not 200 <= status < 300:
        return _empty_live("unavailable", "unknown", "nevermined_rejected")
    try:
        payload = response.json()
    except ValueError:
        return _empty_live("nevermined", "unknown", None)
    live = parse_live(payload)
    live.pop("status_present", None)
    return live


async def fetch_live(org: Dict[str, Any], delegation_id: str, *, agent_name: str) -> Dict[str, Any]:
    """The GET's `live` block: no cache, but N concurrent reads of one
    delegation id share one upstream call (T3 single-flight)."""
    inflight = _read_inflight()
    future = inflight.get(delegation_id)
    if future is None:
        future = asyncio.ensure_future(_fetch_live_once(org, delegation_id, agent_name=agent_name))
        inflight[delegation_id] = future
        try:
            return dict(await future)
        finally:
            inflight.pop(delegation_id, None)
    return dict(await future)


def _delegation_block(row: Dict[str, Any]) -> Dict[str, Any]:
    return {
        "delegation_id": row["delegation_id"],
        "payment_method_id": row["payment_method_id"],
        "provider": row["provider"],
        "environment": row["environment"],
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def delegation_state(agent_name: str, *, configured: bool, row: Optional[Dict[str, Any]],
                     live: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """The one response shape the GET, the bind and the panel share."""
    return {
        "agent_name": agent_name,
        "configured": configured,
        "funded": row is not None,
        "delegation": _delegation_block(row) if row else None,
        "live": live if row else None,
        "history": db.list_agent_delegation_history(agent_name) if configured else [],
    }


async def read_delegation(agent_name: str) -> Dict[str, Any]:
    """GET: the agent's binding and what Nevermined says it can still spend."""
    org = db.get_nevermined_organization()
    secrets = db.get_nevermined_organization_secrets() if org else None
    if not org or not secrets:
        return delegation_state(agent_name, configured=False, row=None, live=None)
    row = db.get_active_agent_delegation(agent_name)
    live = None
    if row:
        live = await fetch_live({**org, **secrets}, row["delegation_id"], agent_name=agent_name)
    return delegation_state(agent_name, configured=True, row=row, live=live)


# ---------------------------------------------------------------------------
# Bind (POST) and revoke (DELETE)
# ---------------------------------------------------------------------------

AGENT_HTTP_TIMEOUT_SECONDS = 30.0
ENV_KEYS = ("NVM_DELEGATION_ID", "NVM_API_KEY", "NVM_ENVIRONMENT")


def require_organization() -> Dict[str, Any]:
    """The configured organization with its secrets, or 409 — the FIRST check
    of every bind/revoke, before the rate limit and before anything upstream."""
    return _configured_organization()


def _agent_running(agent_name: str) -> bool:
    container = get_agent_container(agent_name)
    if not container:
        return False
    return get_agent_status_from_container(container).status == "running"


def _agent_client(agent_name: str) -> httpx.AsyncClient:
    """Pre-authenticated client for `agent-{name}:8000`; a seam tests replace."""
    return agent_httpx_client(agent_name, timeout=AGENT_HTTP_TIMEOUT_SECONDS)


def _agent_unreachable(what: str) -> DelegationError:
    return DelegationError(
        503,
        "agent_unreachable",
        f"The agent did not accept its credential file ({what}). Nothing was bound; "
        "the Nevermined mandate is untouched. Check the agent is running and try again.",
    )


async def _read_agent_env(client: httpx.AsyncClient, agent_name: str) -> str:
    """The agent's current `.env` text (missing file ⇒ ""). Held in memory for
    the patch only — never logged."""
    response = await client.get(
        f"http://agent-{agent_name}:8000/api/credentials/read", params={"paths": ".env"},
    )
    if response.status_code != 200:
        raise DelegationError(
            503, "agent_unreachable",
            f"The agent refused to share its credential file (HTTP {response.status_code}); "
            "the Nevermined mandate is untouched and nothing was bound.",
        )
    try:
        content = response.json().get("files", {}).get(".env")
    except (ValueError, AttributeError):
        raise _agent_unreachable("unreadable read response") from None
    return content if isinstance(content, str) else ""


async def _inject_agent_env(client: httpx.AsyncClient, agent_name: str, env_text: str) -> None:
    """Whole-file rewrite through the agent's curated-path inject — the same
    path and policy `routers/credentials.py` uses (`.env` is curated)."""
    if disallowed_paths([".env"]):  # pragma: no cover — policy pin
        raise _agent_unreachable(".env is not an injectable path")
    response = await client.post(
        f"http://agent-{agent_name}:8000/api/credentials/inject",
        json={"files": {".env": env_text}},
    )
    if response.status_code != 200:
        raise DelegationError(
            503, "agent_unreachable",
            f"The agent rejected the credential write (HTTP {response.status_code}); "
            "the Nevermined mandate is untouched.",
        )


def _active_binding_error(row: Dict[str, Any]) -> DelegationError:
    return DelegationError(
        409,
        "delegation_active",
        "This agent already holds an active Nevermined mandate; revoke the current "
        "mandate first, then bind the new one.",
        extra={"delegation_id": row["delegation_id"]},
    )


async def _verify_upstream(org: Dict[str, Any], delegation_id: str, *, agent_name: str, actor: str) -> Dict[str, Any]:
    """The ONE upstream call a bind makes. Returns the `live` block (reused in
    the response, M1) or raises the named refusal; nothing is written here."""
    try:
        response = await _upstream_delegation_get(org, delegation_id)
    except DelegationBusy:
        logger.warning("Nevermined delegation verify for %s by %s: delegation gate busy", agent_name, actor)
        raise _busy() from None
    except httpx.HTTPError as exc:
        logger.warning(
            "Nevermined delegation verify for %s by %s: unreachable (%s)", agent_name, actor, type(exc).__name__,
        )
        raise _unreachable() from None

    status = response.status_code
    logger.info("Nevermined delegation verify for %s by %s: upstream_status=%s", agent_name, actor, status)
    if status == 404:
        raise DelegationError(
            422, "delegation_not_found",
            "Nevermined knows no delegation with that id. Finish the card step in the "
            "Nevermined widget, then bind the id it returns.",
        )
    if status in (401, 403):
        raise _rejected(
            status,
            "Nevermined refused the organization's agent API key. An admin can update it under Settings.",
        )
    if status >= 500:
        raise _unreachable()
    if not 200 <= status < 300:
        raise _rejected(status, f"Nevermined refused the delegation lookup (HTTP {status}).")
    try:
        payload = response.json()
    except ValueError:
        payload = None
    live = parse_live(payload)
    status_present = live.pop("status_present", False)
    if status_present and live["status"] != "active":
        raise DelegationError(
            422, "delegation_inactive",
            f"Nevermined reports this delegation as '{live['status']}', not active. "
            "Create a new mandate in the Nevermined widget and bind that one.",
            extra={"status": live["status"]},
        )
    return live


async def bind_delegation(
    agent_name: str,
    *,
    delegation_id: str,
    payment_method_id: Optional[str],
    provider: Optional[str],
    actor: str,
    created_by: str,
) -> Dict[str, Any]:
    """Bind a Nevermined delegation to the agent. Order (plan addendum):
    active row → 409 · agent running → 409 · ONE upstream verify · `.env` read ·
    row insert · re-read + inject · compensate (delete the inserted row) on
    inject failure. The org check and the rate limit ran in the router."""
    org = require_organization()
    validate_delegation_id(delegation_id)
    if payment_method_id is not None:
        validate_delegation_id(payment_method_id, what="payment method id")

    current = db.get_active_agent_delegation(agent_name)
    if current:
        raise _active_binding_error(current)
    if not _agent_running(agent_name):
        raise DelegationError(
            409, "agent_not_running",
            "The agent must be running so its credential file can receive the Nevermined "
            "delegation id. Start the agent, then bind.",
        )

    live = await _verify_upstream(org, delegation_id, agent_name=agent_name, actor=actor)

    env_values = {
        "NVM_DELEGATION_ID": delegation_id,
        "NVM_API_KEY": org["agent_api_key"],
        "NVM_ENVIRONMENT": org["environment"],
    }
    try:
        async with _agent_client(agent_name) as client:
            env_text = await _read_agent_env(client, agent_name)
            api_key_injected = (
                not agent_env_patch.has_key(env_text, "NVM_API_KEY")
                or db.latest_prior_agent_delegation_api_key_injected(agent_name)
            )
            try:
                row = db.insert_agent_delegation(
                    agent_name, delegation_id, payment_method_id, provider,
                    org["environment"], api_key_injected, created_by,
                )
            except IntegrityError:
                raise DelegationError(
                    409, "bind_in_progress",
                    "Another bind for this agent landed first. Read the agent's Nevermined "
                    "mandate again before retrying.",
                ) from None
            try:
                # Re-read right before the write so the read-modify-write window
                # is as small as the PAT path's (M4; no agent-side lock, D8).
                env_text = await _read_agent_env(client, agent_name)
                await _inject_agent_env(client, agent_name, agent_env_patch.set_keys(env_text, env_values))
            except (DelegationError, httpx.HTTPError) as exc:
                # Compensation: delete ONLY the row just inserted. Deleting by id
                # never touches the partial unique index, so a newer row that
                # already took the slot is left alone.
                db.delete_agent_delegation_row(row["id"])
                logger.warning(
                    "Nevermined delegation bind for %s by %s: agent inject failed (%s); row removed",
                    agent_name, actor, type(exc).__name__,
                )
                if isinstance(exc, DelegationError):
                    raise
                raise _agent_unreachable(type(exc).__name__) from None
    except httpx.HTTPError as exc:
        logger.warning(
            "Nevermined delegation bind for %s by %s: agent unreachable (%s)", agent_name, actor, type(exc).__name__,
        )
        raise _agent_unreachable(type(exc).__name__) from None

    logger.info(
        "Nevermined delegation bound to %s by %s (environment=%s, api_key_injected=%s)",
        agent_name, actor, org["environment"], api_key_injected,
    )
    state = delegation_state(agent_name, configured=True, row=row, live=live)
    state["effective"] = "next_execution"
    state["api_key_injected"] = bool(api_key_injected)
    return state


REVOKE_NOTE_AGENT_DOWN = (
    "agent_not_running — key is inert (mandate revoked at Nevermined); removed at next bind"
)
REVOKE_NOTE_CONCURRENT = (
    "already_revoked — the Nevermined mandate was already revoked by a concurrent request"
)


async def revoke_delegation(agent_name: str, *, actor: str, revoked_by: str) -> Dict[str, Any]:
    """Revoke the agent's active Nevermined mandate: upstream DELETE first
    (2xx or 404 = revoked; anything else changes nothing locally), then the
    row, then a best-effort removal of the keys from the agent's `.env`."""
    org = require_organization()
    row = db.get_active_agent_delegation(agent_name)
    if not row:
        raise DelegationError(
            404, "no_active_delegation",
            "This agent holds no active Nevermined mandate.",
        )
    delegation_id = row["delegation_id"]

    try:
        url = _delegation_url(org["environment"], delegation_id)
        async with delegation_slot():
            async with _http_client() as client:
                response = await client.delete(
                    url, headers={"Authorization": f"Bearer {org['agent_api_key']}"},
                )
    except DelegationBusy:
        logger.warning("Nevermined delegation revoke for %s by %s: delegation gate busy", agent_name, actor)
        raise _busy() from None
    except httpx.HTTPError as exc:
        logger.warning(
            "Nevermined delegation revoke for %s by %s: unreachable (%s)", agent_name, actor, type(exc).__name__,
        )
        raise _unreachable() from None
    except InvalidDelegationId:
        raise _rejected(None, "The stored Nevermined delegation id is malformed; nothing was changed.") from None

    status = response.status_code
    logger.info("Nevermined delegation revoke for %s by %s: upstream_status=%s", agent_name, actor, status)
    if status in (401, 403):
        raise _rejected(
            status,
            "Nevermined refused the organization's agent API key; the mandate is still active. "
            "An admin can update the key under Settings.",
        )
    if status >= 500:
        raise _unreachable()
    if not (200 <= status < 300 or status == 404):
        raise _rejected(status, f"Nevermined refused the revocation (HTTP {status}); the mandate is still active.")

    if db.revoke_agent_delegation(row["id"], revoked_by) is None:
        logger.info("Nevermined delegation revoke for %s by %s: already revoked concurrently", agent_name, actor)
        return {
            "revoked": True,
            "delegation_id": delegation_id,
            "environment": org["environment"],
            "api_key_injected": bool(row["api_key_injected"]),
            "env_removed": False,
            "env_note": REVOKE_NOTE_CONCURRENT,
            "already_revoked": True,
        }

    keys = ["NVM_DELEGATION_ID"]
    if row["api_key_injected"]:
        keys += ["NVM_API_KEY", "NVM_ENVIRONMENT"]
    env_removed = False
    env_note: Optional[str] = REVOKE_NOTE_AGENT_DOWN
    if _agent_running(agent_name):
        try:
            async with _agent_client(agent_name) as client:
                env_text = await _read_agent_env(client, agent_name)
                await _inject_agent_env(client, agent_name, agent_env_patch.remove_keys(env_text, keys))
            env_removed, env_note = True, None
        except (DelegationError, httpx.HTTPError) as exc:
            logger.warning(
                "Nevermined delegation revoke for %s by %s: env removal failed (%s)",
                agent_name, actor, type(exc).__name__,
            )
            env_note = "agent_unreachable — key is inert (mandate revoked at Nevermined); removed at next bind"

    logger.info(
        "Nevermined delegation revoked for %s by %s (env_removed=%s, api_key_removed=%s)",
        agent_name, actor, env_removed, bool(row["api_key_injected"]) and env_removed,
    )
    return {
        "revoked": True,
        "delegation_id": delegation_id,
        "environment": org["environment"],
        "api_key_injected": bool(row["api_key_injected"]),
        "env_removed": env_removed,
        "env_note": env_note,
    }
