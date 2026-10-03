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
* **Private module (future):** a registered provider takes precedence and may
  scope endpoints per agent.

OSS ships a working source and not merely the seam, because a seam with no
registered provider resolves nothing — the tool would answer "no targets
configured" on every install, which is not what the owner's *"Outbound = OSS"*
ruling can mean. There is no `requires_entitlement` anywhere on this path.

This is a public open-core SEAM FILE: its comments describe the MECHANISM only
and must never name a paid catalog or a private module's internals (#1461 —
a comment in a seam is unguarded prose that discloses just as a doc does).
"""
from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass, field, replace
from typing import Any, Dict, List, Optional, Protocol

#: What kind of secret an endpoint's credential slot holds (#3185). Declared
#: ONCE in `services/a2a_protocol.py` — the vocabulary both directions share,
#: because two copies of it is how the store and the client come to disagree
#: about what a token *is* — and re-exported here so every existing
#: `a2a_outbound.CREDENTIAL_KIND_*` reference keeps resolving.
from services.a2a_protocol import (  # noqa: F401  (re-export)
    CREDENTIAL_KIND_API_KEY,
    CREDENTIAL_KIND_PAYMENT_TOKEN,
    CREDENTIAL_KINDS,
)

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

#: Printable ASCII, no whitespace — the same class `models._PAT_SAFE_RE` uses,
#: and a strict subset of what h11 will put on the wire.
_HEADER_SAFE_CREDENTIAL = re.compile(r"^[\x21-\x7E]+$")


def normalize_credential_kind(value: Any) -> str:
    """Any stored/provider-supplied value → a kind we will act on.

    Fail-SAFE direction, deliberately: anything unrecognised becomes `api_key`.
    A payment token sent as a Bearer header is refused by the remote and reaches
    nobody else; the opposite default would announce an ordinary API key in-band
    as a payment because of a typo in a record we do not control (an enterprise
    provider may return one).
    """
    return value if value in CREDENTIAL_KINDS else CREDENTIAL_KIND_API_KEY


def _decode_token(credential: Optional[str]) -> Optional[Dict[str, Any]]:
    """The credential as an x402 `PaymentPayload`, or `None`.

    Delegates to `a2a_protocol.decode_payment_token`, which the outbound client
    uses for the same question — "may this value ride as x402 payment?" — so a
    credential this store labels `payment_token` is one the client will actually
    announce in-band. Two spellings of that predicate would produce a label the
    transport silently disagrees with, which reads as a platform bug rather than
    as the remote's refusal. Bounded by the store's own credential cap.
    """
    from services import a2a_protocol

    return a2a_protocol.decode_payment_token(
        credential, max_len=MAX_ENDPOINT_CREDENTIAL_LEN
    )


def infer_credential_kind(credential: Optional[str]) -> str:
    """The kind of a credential the operator did not label (#3185 T6).

    `payment_token` iff the value IS an x402 payload (decodes to a JSON object
    carrying an int `x402Version` and a `payload`), else `api_key`. An explicit
    kind always wins; this only decides the omitted case.

    Inferring rather than demanding the field removes the failure the human
    relay is most likely to hit: an operator who has just been handed a 402,
    bought a token and pasted it in would otherwise get `api_key` by default,
    the token would ride as `Authorization: Bearer …`, and the remote would
    answer 402 again — with nothing on either side saying why.
    """
    return (
        CREDENTIAL_KIND_PAYMENT_TOKEN
        if _decode_token(credential) is not None
        else CREDENTIAL_KIND_API_KEY
    )


def credential_is_single_use(credential: Optional[str]) -> bool:
    """Does this payment token carry a one-shot authorization? (#3185 T4)

    An x402 v3 token authorises ONE settlement: `payload.authorization.nonce`
    is spent when the remote settles, so the second call with the same stored
    token is refused. The flag is honest status, not a refusal — a provider that
    issues only single-use tokens must stay usable (the operator re-pastes a
    token per call), and refusing the write would make the feature unusable
    against them.
    """
    decoded = _decode_token(credential)
    payload = decoded.get("payload") if isinstance(decoded, dict) else None
    authorization = payload.get("authorization") if isinstance(payload, dict) else None
    nonce = authorization.get("nonce") if isinstance(authorization, dict) else None
    return isinstance(nonce, str) and bool(nonce.strip())


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
    #: `api_key` (default, and what every pre-#3185 record resolves to) or
    #: `payment_token`. The KIND is metadata and stays in the repr — an operator
    #: debugging a 402 needs to know which slot they filled; the VALUE never
    #: appears.
    credential_kind: str = CREDENTIAL_KIND_API_KEY

    def __repr__(self) -> str:  # pragma: no cover - trivial, but load-bearing
        return (
            f"ResolvedEndpoint(id={self.id!r}, name={self.name!r}, url={self.url!r}, "
            f"credential={'<set>' if self.credential else None}, "
            f"credential_kind={self.credential_kind!r})"
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


def _record_matches_ref(record: Dict[str, Any], wanted: str, lowered: str) -> bool:
    """Does `record` answer to this reference? Ids match exactly, names case-insensitively.

    THE one predicate for "this reference means this record" (#2174). Resolve and
    remove used to spell it separately, and the two spellings did not agree:
    resolve stopped at the FIRST match, remove filtered out EVERY match. Nothing
    enforces uniqueness ACROSS the id and name namespaces — ids are visible in the
    admin GET and naming one endpoint after another's id is a permitted operation
    — so a single-target DELETE could destroy two endpoints, and their
    AES-256-GCM credentials, while reporting one success.

    `wanted` is the stripped reference and `lowered` its casefold; both are passed
    in rather than derived here so a caller scanning a list computes them once.
    """
    return (
        str(record.get("id") or "") == wanted
        or str(record.get("name") or "").lower() == lowered
    )


def _public_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """The read shape: metadata plus whether a credential exists, never its value.

    `credential_kind` (and the single-use flag) appear only when a credential
    does. They are properties OF the stored secret: reporting a kind for an
    empty slot would tell an operator their payment token is registered when
    nothing is, which is the one wrong answer this read can give about a 402.
    The kind is a LABEL and always safe to show — an operator debugging a 402
    needs to know which slot they filled.
    """
    out = {
        "id": str(record.get("id") or ""),
        "name": str(record.get("name") or ""),
        "url": str(record.get("url") or ""),
        "has_credentials": bool(record.get("credential")),
    }
    if out["has_credentials"]:
        out["credential_kind"] = normalize_credential_kind(record.get("credential_kind"))
        if record.get("credential_single_use"):
            out["credential_single_use"] = True
    return out


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
            # First match wins; `remove_endpoint` deletes by the same predicate in
            # the same order, so what a ref resolves to is what it deletes (#2174).
            if _record_matches_ref(record, wanted, lowered):
                url = str(record.get("url") or "")
                if not url:
                    return None
                credential = record.get("credential")
                return ResolvedEndpoint(
                    id=rid,
                    name=rname,
                    url=url,
                    credential=str(credential) if credential else None,
                    credential_kind=normalize_credential_kind(
                        record.get("credential_kind")
                    ),
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
    kind = normalize_credential_kind(resolved.credential_kind)
    if kind != resolved.credential_kind:
        # A provider we do not own returned a kind we will not act on. Normalise
        # rather than refuse: the call still works as an `api_key` endpoint, and
        # the remote — not us — decides whether that credential is acceptable.
        # The provider's value is not echoed: it is a field we do not own on a
        # record that also carries the secret, and a provider that put the wrong
        # thing in it would have its credential written to the log.
        logger.warning(
            "[a2a_outbound] provider returned an unrecognised credential_kind; "
            "treating as %s",
            kind,
        )
        resolved = replace(resolved, credential_kind=kind)
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


def _apply_credential_kind(record: Dict[str, Any], kind: Optional[str],
                           credential: str) -> None:
    """Record the kind of `credential` — explicit when given, inferred when not.

    Only `payment_token` is written down. `api_key` is the ABSENCE of the key,
    which is exactly what every record written before #3185 says, so a relabel
    back to `api_key` returns a record byte-identical to a pre-#3185 one rather
    than inventing a second spelling of the default that readers would then have
    to keep in agreement.
    """
    effective = kind or infer_credential_kind(credential)
    if effective == CREDENTIAL_KIND_PAYMENT_TOKEN:
        record["credential_kind"] = CREDENTIAL_KIND_PAYMENT_TOKEN
        if credential_is_single_use(credential):
            record["credential_single_use"] = True
        else:
            record.pop("credential_single_use", None)
    else:
        _forget_credential_kind(record)


def _forget_credential_kind(record: Dict[str, Any]) -> None:
    """Drop the label and its single-use flag — both describe a value that is
    gone (or is now an ordinary API key)."""
    record.pop("credential_kind", None)
    record.pop("credential_single_use", None)


def upsert_endpoint(
    name: str,
    url: str,
    credential: Optional[str] = None,
    *,
    clear_credential: bool = False,
    credential_kind: Optional[str] = None,
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

    A credential is **write-only**, and there are exactly THREE paths (#2175 F5b):
    omitting it leaves an existing one untouched (so an operator can rename or
    repoint without re-typing a secret they may not have), passing one sets it,
    and `clear_credential=True` is the only way to remove it. **Every blank
    spelling — `None`, `""`, `"   "` — means "leave it alone."** A whitespace-only
    value used to be a fourth path that silently destroyed the stored secret: it
    skipped the header-safety check (`if credential.strip() and …`) and then took
    the `elif credential:` branch, because `"   "` is truthy, writing `""`. Not
    reachable over HTTP — `A2AOutboundEndpointUpsert._validate_credential`
    normalises a blank `SecretStr` to `None` — but this is a public module
    function, and "blank clears the secret" is the wrong default for a value the
    caller may hold no other copy of.

    `credential_kind` (#3185) is a LABEL on that same slot, never a second
    secret, and it follows the credential's three paths rather than adding a
    fourth: omitted with a new credential it is **inferred** from the value
    (T6); given explicitly it wins; given with no credential it RE-LABELS the
    stored one (an operator who pasted a payment token before the field existed
    must be able to fix the label without re-typing a secret), which is refused
    when there is no stored credential to label; and `clear_credential` drops
    the label with the value it described. Kind + `clear_credential` together is
    refused — it can only mean the caller believes one of the two is being
    ignored, and silently honouring the clear is how an operator comes to think
    a payment token is registered when the slot is empty.
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
    if credential is not None:
        if len(credential) > MAX_ENDPOINT_CREDENTIAL_LEN:
            raise EndpointValidationError(
                f"Endpoint credential is too long (max {MAX_ENDPOINT_CREDENTIAL_LEN} characters)"
            )
        # Header-safety, checked at the STORE and not only at the request model:
        # this value becomes an `Authorization: Bearer …` header, and h11 rejects
        # an illegal header value by ECHOING it into an exception the calling
        # agent then reads through the 502 body. Same guard and same reason as
        # `models._validate_pat_secret` (ent#109). Never echo the value.
        if credential.strip() and not _HEADER_SAFE_CREDENTIAL.match(credential.strip()):
            raise EndpointValidationError(
                "Endpoint credential contains characters that are not valid in an "
                "HTTP header (whitespace, line breaks or control characters)."
            )
    if credential_kind is not None:
        if credential_kind not in CREDENTIAL_KINDS:
            # Name the domain, never the value — the kind is operator input too.
            raise EndpointValidationError(
                "Unknown credential kind; expected one of: "
                + ", ".join(CREDENTIAL_KINDS)
            )
        if clear_credential:
            raise EndpointValidationError(
                "Pass either credential_kind or clear_credential, not both — "
                "clearing the credential also drops the kind that described it."
            )
    # #2175 F5b: ONE normalisation, immediately after the checks above — every
    # blank spelling collapses to None ("leave it alone") before either write
    # path can see it. Done here rather than at each branch so a future third
    # write site cannot reintroduce the fourth path. The length cap is still
    # measured on the raw value, so the bound an operator is told about does not
    # move when their secret has surrounding whitespace.
    clean_credential = (credential or "").strip() or None
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
                _forget_credential_kind(record)
            elif clean_credential:
                record["credential"] = clean_credential
                _apply_credential_kind(record, credential_kind, clean_credential)
            elif credential_kind is not None:
                # Kind-only: re-label the secret already stored. Refused when
                # there is none, because a kind with nothing to describe would
                # read back as "a payment token is registered here".
                if not record.get("credential"):
                    raise EndpointValidationError(
                        "There is no stored credential to label; send the "
                        "credential together with credential_kind."
                    )
                _apply_credential_kind(
                    record, credential_kind, str(record.get("credential"))
                )
            _store_endpoint_records(records)
            return _public_record(record)

    # #2174, at the source: a NEW endpoint may not take the id of an existing one
    # as its name. Ids are visible in the admin GET, so this was reachable by an
    # ordinary create, and it makes one ref mean two records for every later
    # resolve and delete. Checked only on the create path — an already-stored
    # collision must stay editable and removable, or the guard would strand the
    # operator in exactly the state it exists to prevent.
    for record in records:
        if str(record.get("id") or "").lower() == lowered:
            raise EndpointValidationError(
                "That name is already the id of another registered endpoint; "
                "pick a different name."
            )

    if len(records) >= MAX_ENDPOINTS:
        raise EndpointValidationError(
            f"Too many registered A2A endpoints (max {MAX_ENDPOINTS})"
        )
    record = {
        "id": f"a2aep_{uuid.uuid4().hex[:12]}",
        "name": clean_name,
        "url": clean_url,
    }
    if clean_credential and not clear_credential:
        record["credential"] = clean_credential
        _apply_credential_kind(record, credential_kind, clean_credential)
    elif credential_kind is not None:
        raise EndpointValidationError(
            "There is no stored credential to label; send the credential "
            "together with credential_kind."
        )
    records.append(record)
    _store_endpoint_records(records)
    return _public_record(record)


def remove_endpoint(ref: str) -> bool:
    """Remove **exactly one** endpoint by id or name. `False` when nothing matched.

    #2174: this filtered out every record matching the ref on EITHER id or name.
    Since nothing enforces uniqueness across those two namespaces — and an
    operator can read ids from the admin GET and legally name an endpoint after
    one — a single-target `DELETE /api/settings/a2a-endpoints/{ref}` could
    destroy two endpoints at once, taking a partner credential the operator may
    have no other copy of, and still return one `True`. The follow-on symptom
    misdirects too: the collaterally-deleted endpoint's next call fails
    `endpoint_not_found`, which reads as a registration problem.

    Deletion is **first-match-wins**, deliberately the same rule and the same
    order as `resolve_endpoint` (both via `_record_matches_ref`), so a ref always
    deletes precisely what it resolves to. In the collision case that means the
    record registered EARLIER wins — in the reported repro the id-owning
    endpoint, which is the one the operator was naming.

    A refuse-if-ambiguous shape was the alternative. Rejected: it strands the
    operator in a state they can reach but cannot leave, since there is no rename
    path — and it leaves the destructive default in place for every already-stored
    collision, which is the case actually at risk.
    """
    wanted = (ref or "").strip()
    if not wanted:
        return False
    lowered = wanted.lower()
    records = _load_endpoint_records()
    for index, record in enumerate(records):
        if _record_matches_ref(record, wanted, lowered):
            del records[index]
            _store_endpoint_records(records)
            return True
    return False
