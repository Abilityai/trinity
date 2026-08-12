"""Outbound A2A target resolution — the fail-CLOSED seam (#736).

Answers exactly one question: *given a calling agent and an operator-chosen
endpoint reference, what URL and credential does Trinity use?* Nothing else in
the outbound path is allowed to answer it, and in particular the **calling agent
never supplies a URL** (requirements mcp.md §32.5 FR-1). That single constraint
is what turns a server-side fetcher — the classic SSRF primitive — into a
bounded integration: an agent's parameters are LLM-generated and
prompt-injectable, so a URL parameter would make any document the agent reads a
lever on a credentialed request from inside the platform network.

────────────────────────────────────────────────────────────────────────────
FAIL-CLOSED. Read this before copying `a2a_gate.py`.
────────────────────────────────────────────────────────────────────────────
This module mirrors `services/a2a_gate.py`'s registration shape (Protocol,
module-level `_provider`, register/get/clear) and **inverts its failure
semantics**. `a2a_gate` fails OPEN and its own docstring says why that is
acceptable: *"this is not a security boundary — it would not be if it were."*
This one **is** one. It decides where a credential is sent. Therefore:

* no provider  → **no target** (the call is refused);
* provider raises → **refuse**, log at ERROR;
* provider returns a malformed object → **refuse**, log at ERROR.

The `isinstance(ResolvedEndpoint)` check on the return value is not defensive
padding. It is what makes the refusal hold under a test harness that stubs
`sys.modules["services.a2a_outbound"]` with a `MagicMock`: a mock's
`resolve_endpoint()` returns a truthy mock whose `.url` is also a mock, which
would silently convert this module from fail-closed to fail-open *inside the
suite that is supposed to be proving it closed*.

────────────────────────────────────────────────────────────────────────────
Two providers, one seam (§32.5 FR-2)
────────────────────────────────────────────────────────────────────────────
* **OSS (shipped, below):** admin-managed named endpoints in `system_settings`,
  each credential in an AES-256-GCM envelope — the location Invariant #12
  already blesses for `elevenlabs_api_key_encrypted`. No new table, no SQLite
  migration, no Alembic revision. Platform-scope: a named endpoint is available
  to every agent on the instance.
* **Enterprise (future):** a registered provider takes precedence and may scope
  endpoints per agent. The enterprise A2A module today ships an endpoint
  registry with **no decrypt path and no provider**, which is exactly why OSS
  owns a working source: a seam alone would have shipped a tool that answers
  "no targets configured" on 100% of installs, including entitled ones.

The epic's owner ruling is *"Outbound = OSS"*, so there is no
`requires_entitlement` anywhere on this path.
"""
from __future__ import annotations

import json
import logging
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Protocol

logger = logging.getLogger(__name__)

#: `system_settings` key holding the OSS named-endpoint list (an AES-256-GCM
#: envelope over a JSON document, so the per-endpoint credentials are never at
#: rest in plaintext — Invariant #12).
A2A_ENDPOINTS_SETTING = "a2a_outbound_endpoints_encrypted"

#: Bound the list. Not a security control — a bound on operator error and on the
#: size of a single settings row.
MAX_ENDPOINTS = 50
MAX_ENDPOINT_NAME_LEN = 200
MAX_ENDPOINT_URL_LEN = 2048
MAX_ENDPOINT_CREDENTIAL_LEN = 8192


@dataclass(frozen=True)
class ResolvedEndpoint:
    """A resolved outbound target. **Carries a plaintext credential.**

    `credential` is decrypted here and must never reach a log line, an exception
    `repr`, an audit row, or a Pydantic 422 `input` field. `__repr__` is
    overridden rather than trusted to be uninteresting: the default dataclass
    repr prints every field, and `error_handlers.validation_error_without_input`
    exists in this codebase precisely because rejecting a bad secret at the
    Pydantic boundary was found to *echo* it.
    """

    id: str
    name: str
    url: str
    credential: Optional[str] = field(default=None, repr=False)

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return (
            f"ResolvedEndpoint(id={self.id!r}, name={self.name!r}, url={self.url!r}, "
            f"credential={'<set>' if self.credential else None})"
        )

    __str__ = __repr__


class A2AEndpointProvider(Protocol):
    """Resolve `ref` (an endpoint id or operator-facing name) for `agent_name`.

    Returns `None` when the reference is unknown — which the caller reports as
    "endpoint not found", never as "call anything you like".
    """

    def resolve_endpoint(self, agent_name: str, ref: str) -> Optional[ResolvedEndpoint]:
        ...

    def list_endpoints(self, agent_name: str) -> List[Dict[str, Any]]:
        """Metadata only — id/name/url/has_credentials. NEVER the credential."""
        ...


_provider: Optional[A2AEndpointProvider] = None


def register_provider(provider: A2AEndpointProvider) -> None:
    """Register an outbound endpoint provider. Idempotent (last wins)."""
    global _provider
    _provider = provider
    logger.info("[a2a_outbound] provider registered: %s", type(provider).__name__)


def get_provider() -> Optional[A2AEndpointProvider]:
    return _provider


def clear_provider() -> None:
    """Drop the registered provider — used by tests to restore the OSS default."""
    global _provider
    _provider = None


# ---------------------------------------------------------------------------
# OSS provider — admin-managed named endpoints in `system_settings`
# ---------------------------------------------------------------------------

def _load_endpoint_records() -> List[Dict[str, Any]]:
    """Decrypt + parse the stored endpoint list. `[]` on anything unusable.

    Returning `[]` rather than raising is the fail-CLOSED direction for this
    module: an unreadable list resolves nothing, so every call is refused with
    "endpoint not found". It is deliberately NOT the same as fail-open, which
    would be resolving *something*.
    """
    from database import db

    envelope = db.get_setting_value(A2A_ENDPOINTS_SETTING, None)
    if not envelope:
        return []
    try:
        from services.credential_encryption import CredentialEncryptionService

        payload = CredentialEncryptionService().decrypt(envelope)
        raw = payload.get("endpoints") if isinstance(payload, dict) else None
        if isinstance(raw, str):
            raw = json.loads(raw)
        if not isinstance(raw, list):
            return []
        return [r for r in raw if isinstance(r, dict)]
    except Exception:  # noqa: BLE001 — an unreadable list must not 500 a call
        logger.error(
            "[a2a_outbound] stored endpoint list is unreadable; resolving nothing",
            exc_info=True,
        )
        return []


def _store_endpoint_records(records: List[Dict[str, Any]]) -> None:
    """Encrypt + persist the endpoint list (AES-256-GCM, Invariant #12)."""
    from database import db
    from services.credential_encryption import CredentialEncryptionService

    envelope = CredentialEncryptionService().encrypt(
        {"endpoints": json.dumps(records, ensure_ascii=False)}
    )
    db.set_setting(A2A_ENDPOINTS_SETTING, envelope)


def _public_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """The read shape: metadata plus whether a credential exists, never its value."""
    return {
        "id": str(record.get("id") or ""),
        "name": str(record.get("name") or ""),
        "url": str(record.get("url") or ""),
        "has_credentials": bool(record.get("credential")),
    }


class SystemSettingsEndpointProvider:
    """The OSS provider: platform-scope named endpoints from `system_settings`.

    `agent_name` is accepted and deliberately unused — OSS endpoints are
    platform-scope, and per-agent scoping is the enterprise delta. Taking the
    parameter keeps the Protocol identical for both, so the enterprise provider
    is a drop-in rather than a signature change.
    """

    def resolve_endpoint(self, agent_name: str, ref: str) -> Optional[ResolvedEndpoint]:
        wanted = (ref or "").strip()
        if not wanted:
            return None
        lowered = wanted.lower()
        for record in _load_endpoint_records():
            rid = str(record.get("id") or "")
            rname = str(record.get("name") or "")
            if rid == wanted or rname.lower() == lowered:
                url = str(record.get("url") or "")
                if not url:
                    return None
                credential = record.get("credential")
                return ResolvedEndpoint(
                    id=rid,
                    name=rname,
                    url=url,
                    credential=str(credential) if credential else None,
                )
        return None

    def list_endpoints(self, agent_name: str) -> List[Dict[str, Any]]:
        return [_public_record(r) for r in _load_endpoint_records()]


_OSS_PROVIDER = SystemSettingsEndpointProvider()


def _effective_provider() -> A2AEndpointProvider:
    """A registered (enterprise) provider wins; otherwise the OSS one."""
    return _provider or _OSS_PROVIDER


def resolve_endpoint(agent_name: str, ref: str) -> Optional[ResolvedEndpoint]:
    """Resolve an endpoint reference to a target, or `None`. **Never raises.**

    Fail-closed on every unhappy path (see the module docstring): a provider
    error and a malformed return are both `None`, and `None` means the caller
    refuses the call.
    """
    provider = _effective_provider()
    try:
        resolved = provider.resolve_endpoint(agent_name, ref)
    except Exception:  # noqa: BLE001 — a resolver error must never open the gate
        logger.error(
            "[a2a_outbound] provider error resolving %r for %s; refusing",
            ref, agent_name, exc_info=True,
        )
        return None
    if resolved is None:
        return None
    if not isinstance(resolved, ResolvedEndpoint):
        # See the module docstring: a MagicMock passes a truthiness test and a
        # `.url` attribute access, so "is it the real type" is the only check
        # that survives a stubbed sys.modules.
        logger.error(
            "[a2a_outbound] provider returned %s (expected ResolvedEndpoint); refusing",
            type(resolved).__name__,
        )
        return None
    if not isinstance(resolved.url, str) or not resolved.url.strip():
        logger.error("[a2a_outbound] provider returned an endpoint with no URL; refusing")
        return None
    return resolved


def list_endpoints(agent_name: str) -> List[Dict[str, Any]]:
    """Metadata for the endpoints this agent may call. Never raises, never leaks."""
    provider = _effective_provider()
    try:
        rows = provider.list_endpoints(agent_name)
    except Exception:  # noqa: BLE001
        logger.error("[a2a_outbound] provider error listing endpoints", exc_info=True)
        return []
    out: List[Dict[str, Any]] = []
    for row in rows or []:
        if isinstance(row, dict):
            out.append(_public_record(row))
    return out


def list_oss_endpoints() -> List[Dict[str, Any]]:
    """The OSS `system_settings` list, read DIRECTLY — not through the seam.

    Deliberately not `list_endpoints()`: that one answers "what can this agent
    call?", which a registered enterprise provider may legitimately shadow. The
    admin surface manages THIS store, and its GET must show what its own PUT and
    DELETE write — otherwise an entitled install would list one set of endpoints
    and edit another.
    """
    return [_public_record(r) for r in _load_endpoint_records()]


# ---------------------------------------------------------------------------
# Admin mutators for the OSS list. Called ONLY from the admin + human-only
# settings route — they are here rather than in the router because the storage
# shape (envelope, record keys, id minting) belongs with the reader that has to
# understand it (Invariant #1: routers hold no business logic).
# ---------------------------------------------------------------------------

class EndpointValidationError(ValueError):
    """An operator-supplied endpoint the store refuses to hold."""


def upsert_endpoint(
    name: str,
    url: str,
    credential: Optional[str] = None,
    *,
    clear_credential: bool = False,
) -> Dict[str, Any]:
    """Add or update one named endpoint. Returns its public (credential-free) record.

    Update-by-name, matching `register_a2a_endpoint`'s shipped semantics on the
    enterprise side so an operator meets one model, not two.

    The URL is validated **here** with the same call-time gate the caller uses
    (`validate_a2a_endpoint_url`), which is a deliberate departure from the
    enterprise registration path — that one accepts anything starting `http://`
    or `https://`, so an operator registers successfully and then fails at first
    call with no idea why. Validating at write time is strictly better UX and
    changes nothing about security, because the call path re-validates
    regardless: a stored row is not trusted, a DNS record can move, and the
    settings row could be written by some future path that skips this function.

    A credential is **write-only**: omitted leaves an existing one untouched
    (so an operator can rename or repoint without re-typing a secret they may
    not have), and `clear_credential=True` removes it.
    """
    import uuid

    from utils.url_validation import A2AEndpointUrlError, validate_a2a_endpoint_url

    clean_name = (name or "").strip()
    if not clean_name:
        raise EndpointValidationError("Endpoint name is required")
    if len(clean_name) > MAX_ENDPOINT_NAME_LEN:
        raise EndpointValidationError(
            f"Endpoint name is too long (max {MAX_ENDPOINT_NAME_LEN} characters)"
        )
    clean_url = (url or "").strip()
    if len(clean_url) > MAX_ENDPOINT_URL_LEN:
        raise EndpointValidationError(
            f"Endpoint URL is too long (max {MAX_ENDPOINT_URL_LEN} characters)"
        )
    if credential is not None and len(credential) > MAX_ENDPOINT_CREDENTIAL_LEN:
        raise EndpointValidationError(
            f"Endpoint credential is too long (max {MAX_ENDPOINT_CREDENTIAL_LEN} characters)"
        )
    try:
        validate_a2a_endpoint_url(clean_url)
    except A2AEndpointUrlError as exc:
        raise EndpointValidationError(str(exc)) from None

    records = _load_endpoint_records()
    lowered = clean_name.lower()
    for record in records:
        if str(record.get("name") or "").lower() == lowered:
            record["name"] = clean_name
            record["url"] = clean_url
            if clear_credential:
                record.pop("credential", None)
            elif credential:
                record["credential"] = credential
            _store_endpoint_records(records)
            return _public_record(record)

    if len(records) >= MAX_ENDPOINTS:
        raise EndpointValidationError(
            f"Too many registered A2A endpoints (max {MAX_ENDPOINTS})"
        )
    record = {
        "id": f"a2aep_{uuid.uuid4().hex[:12]}",
        "name": clean_name,
        "url": clean_url,
    }
    if credential and not clear_credential:
        record["credential"] = credential
    records.append(record)
    _store_endpoint_records(records)
    return _public_record(record)


def remove_endpoint(ref: str) -> bool:
    """Remove one endpoint by id or name. `False` when nothing matched."""
    wanted = (ref or "").strip()
    if not wanted:
        return False
    lowered = wanted.lower()
    records = _load_endpoint_records()
    keep = [
        r for r in records
        if str(r.get("id") or "") != wanted
        and str(r.get("name") or "").lower() != lowered
    ]
    if len(keep) == len(records):
        return False
    _store_endpoint_records(keep)
    return True
