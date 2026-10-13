"""Shared A2A/JSON-RPC vocabulary — used by BOTH directions (#736, F19).

`routers/a2a.py` (the ent#157 inbound server) already owned the JSON-RPC error
codes, the method names and the task-object shape. #736 adds an outbound client
that must speak the identical dialect — most immediately against *Trinity
itself*, since #738 (Trinity-to-Trinity federation) is downstream of this issue
and its peer is a Trinity inbound server.

Two copies of a protocol vocabulary is how a dialect table rots: the inbound
server gains a method, the outbound client keeps sending the old name, and
nothing fails until someone federates. So the constants live here once and both
sides import them.

**Dialect (#736 FR-12).** The A2A spec renamed its methods between v0.3 (slash
names: `message/send`) and v1.0 (PascalCase: `SendMessage`). The issue's filed
technical note said *"Target v1.0 only"*; that is rejected on evidence, not
taste:

* `services/a2a_card_service.py` pins `"protocolVersion": "0.3.0"`.
* `routers/a2a.py` dispatches on slash names.
* Therefore a v1.0-only client **cannot talk to Trinity**, and #738 — the
  primary consumer — would be dead on arrival.
* The spec's own back-compat rule is that an absent version header means v0.3
  semantics, which is what makes federation work with zero configuration.

So the dialect is chosen from the peer's card, defaulting to v0.3. The v1.0 arm
is DOCUMENTED but NOT CLAIMED: `resolve_dialect` refuses a `1.x` card with
`unsupported_protocol_version`. There is no v1.0 peer to test against, and §FR-6
already used "untestable ⇒ do not ship it" to reject SSE — claiming an untested
protocol arm would be the same mistake with a longer blast radius (it sends a
credential). Turning it on is one line plus a peer to test it against.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Dict, Optional

# ---------------------------------------------------------------------------
# JSON-RPC 2.0 + A2A error codes (spec §10). Canonical home; `routers/a2a.py`
# imports these rather than re-declaring them.
# ---------------------------------------------------------------------------
RPC_PARSE_ERROR = -32700
RPC_INVALID_REQUEST = -32600
RPC_METHOD_NOT_FOUND = -32601
RPC_INVALID_PARAMS = -32602
RPC_INTERNAL_ERROR = -32603
A2A_TASK_NOT_FOUND = -32001
A2A_TASK_NOT_CANCELABLE = -32002
A2A_UNSUPPORTED = -32004

#: Cap the JSON-RPC body on the INBOUND side before parsing it. Reused as the
#: outbound response ceiling so the two directions agree on what "too big" is.
MAX_RPC_BODY_BYTES = 1_000_000


# ---------------------------------------------------------------------------
# x402 payment vocabulary (#3185). Here rather than in `a2a_client.py` for the
# reason the module docstring gives: two copies of a protocol vocabulary is how
# a dialect table rots. The outbound client WRITES these keys today; the
# inbound server (abilityai/trinity-enterprise#679) will READ the same ones,
# and it imports from here.
#
# The names are the a2a-x402 extension's, verified in use against payments-py
# 1.18 (`x402Metadata`) — see trinity-enterprise#763. They are DOTTED keys
# inside one flat `metadata` dict, not a nested object: that is the extension's
# own shape, and writing it as nesting would be a protocol of our own.
# ---------------------------------------------------------------------------
X402_STATUS_KEY = "x402.payment.status"
X402_REQUIRED_KEY = "x402.payment.required"
X402_PAYLOAD_KEY = "x402.payment.payload"
X402_ERROR_KEY = "x402.payment.error"
X402_RECEIPTS_KEY = "x402.payment.receipts"

#: What we SEND when a payment token is attached in-band.
X402_STATUS_SUBMITTED = "payment-submitted"
#: What a priced peer sends back. `payment-required` and `payment-failed` are
#: refusals the client raises on; `payment-completed` is recorded (the operator
#: must be able to see money leaving) but never surfaced to the calling agent.
X402_STATUS_REQUIRED = "payment-required"
X402_STATUS_FAILED = "payment-failed"
X402_STATUS_COMPLETED = "payment-completed"
#: Verified but NOT settled — payments-py 1.18's own state, and the honest
#: answer for Trinity's deliver-then-reconcile branch (#1018): the turn ran and
#: the artifact is attached, but no receipt exists yet. It matters that this is
#: neither `payment-completed` (which would be a receipt we do not have) nor
#: `payment-failed` (on which the outbound client DISCARDS the artifact the
#: payer's turn produced). A client that does not know the value parses the task
#: normally, which is exactly the required behaviour.
X402_STATUS_VERIFIED = "payment-verified"
#: What the provider sends when it accepted no payment and ran nothing.
X402_STATUS_REJECTED = "payment-rejected"

#: The HTTP response header a priced peer uses to carry its requirements
#: (base64 JSON `X402PaymentRequired`), and the request header carrying the
#: token. Both are x402 v2 names already in use by `routers/paid.py`.
X402_PAYMENT_REQUIRED_HEADER = "payment-required"
X402_PAYMENT_SIGNATURE_HEADER = "payment-signature"

#: Ceiling on a base64-JSON document we will even attempt to decode. The
#: outbound client applies it to a peer-controlled response header (whose only
#: other bound is h11's); the store applies its own, tighter, credential cap.
X402_JSON_B64_MAX_CHARS = 32 * 1024

#: What kind of secret an outbound endpoint's credential slot holds (#3185).
#: Canonical home — the store (`services/a2a_outbound.py`) and the client
#: (`services/a2a_client.py`) both import these rather than re-declaring them,
#: for the same reason the method names live here: two copies of a vocabulary is
#: how the two sides come to disagree about what a token *is*.
#:
#: `api_key` is the default for EVERY record written before #3185 — the key is
#: simply absent there — and it means today's behaviour exactly: the credential
#: rides `Authorization: Bearer …` and nothing else. `payment_token` additionally
#: attaches the token as x402 payment (in-band metadata + the deprecated
#: `payment-signature` header); the Bearer header still goes out either way.
#:
#: It is a LABEL on the existing credential slot, not a second secret. A
#: separate store, route or MCP tool for payment tokens would be a fourth write
#: path to the same AES-256-GCM envelope.
#:
#: Anything outside this tuple is treated as `api_key` by
#: `a2a_outbound.normalize_credential_kind` — the fail-SAFE direction, argued in
#: full at that function.
CREDENTIAL_KIND_API_KEY = "api_key"
CREDENTIAL_KIND_PAYMENT_TOKEN = "payment_token"
CREDENTIAL_KINDS = (CREDENTIAL_KIND_API_KEY, CREDENTIAL_KIND_PAYMENT_TOKEN)


def json_b64_object(raw: Any, *, max_len: int = X402_JSON_B64_MAX_CHARS) -> Optional[Dict[str, Any]]:
    """A base64-JSON (or plain-JSON) **object**, or `None`. Never raises.

    `max_len` is a bound as much as the parse is a parse: the outbound caller
    runs this on a header whose only other ceiling is h11's, so the length is
    checked BEFORE any decode work. Plain (un-encoded) JSON is accepted too —
    Trinity's own paid door emits the requirements object in a JSON body, and a
    provider that puts it in the header unencoded costs us nothing to read.

    `except Exception` is deliberate and wide: the failure set here is
    `binascii.Error`, `UnicodeDecodeError`, `json.JSONDecodeError`,
    `RecursionError` on a deeply nested document, and whatever a future codec
    adds. Every one of them means the same thing — "this is not a JSON object" —
    and none of them may become a 500.
    """
    import base64
    import json

    if not isinstance(raw, str):
        return None
    value = raw.strip()
    if not value or len(value) > max_len:
        return None
    for candidate in (value, None):
        if candidate is None:
            try:
                padded = value + "=" * (-len(value) % 4)
                decoded = base64.b64decode(padded.replace("-", "+").replace("_", "/"),
                                           validate=False)
                text = decoded.decode("utf-8")
            except Exception:  # noqa: BLE001 — see the docstring
                return None
        else:
            text = candidate
        try:
            parsed = json.loads(text)
        except Exception:  # noqa: BLE001
            continue
        return parsed if isinstance(parsed, dict) else None
    return None


def decode_payment_token(credential: Optional[str], *,
                         max_len: int = X402_JSON_B64_MAX_CHARS) -> Optional[Dict[str, Any]]:
    """The stored credential as an x402 `PaymentPayload`, or `None`.

    One predicate, two callers, because they must agree: the outbound client
    asks it "may I announce this token in-band?" and the endpoint store asks it
    "is this credential a payment token?" (#3185 T6 — the kind is inferred from
    the value when the operator omits it). Two spellings of "is this an x402
    token" would mean a credential the store labels `payment_token` and the
    client then declines to send in-band, which is the one combination that
    reads as a platform bug rather than as a provider's refusal.

    `None` is the **degrade, not a refusal** (decision 23/29): an opaque token
    is still sent as the `payment-signature` header, which is exactly today's
    working x402 path. What `None` prevents is shipping base64 garbage as
    `x402.payment.payload` — the shape check (`x402Version` int + a `payload`
    key, per payments-py's own `PaymentPayload`) is what stops a mislabelled API
    key from being announced in-band as a payment.
    """
    obj = json_b64_object(credential, max_len=max_len)
    if obj is None:
        return None
    version = obj.get("x402Version")
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    if "payload" not in obj:
        return None
    return obj


def payment_payload_from_message(message: Any) -> Optional[Dict[str, Any]]:
    """The in-band x402 payment payload on an A2A `message` param, or `None`.

    The provider-side counterpart of what the outbound client WRITES: ruling 3
    makes task metadata the primary rail (`x402.payment.payload`), with the
    `payment-signature` header a deprecated fallback. This reads only the rail;
    turning the payload into a facilitator-valid access token is the SDK's job
    (`payments_py.x402.token.encode_access_token`) and stays out of this module,
    which is SDK-free by design — the outbound client imports it and must not
    acquire a payments-py dependency.

    Tolerant on purpose: every field here is caller-controlled on an endpoint
    reachable without a Trinity credential, so a missing/odd shape means "no
    in-band payment" (→ header fallback → 402), never an exception. The shape
    check is `PaymentPayload`'s own (`x402Version` int + a `payload` key), the
    same predicate `decode_payment_token` applies to the encoded form — one
    definition of "is this an x402 payment", so the two directions cannot come
    to disagree.
    """
    if not isinstance(message, dict):
        return None
    metadata = message.get("metadata")
    if not isinstance(metadata, dict):
        return None
    payload = metadata.get(X402_PAYLOAD_KEY)
    if not isinstance(payload, dict):
        return None
    version = payload.get("x402Version")
    if not isinstance(version, int) or isinstance(version, bool):
        return None
    if "payload" not in payload:
        return None
    return payload


@dataclass(frozen=True)
class Dialect:
    """One protocol generation's wire vocabulary."""

    version: str
    send_message: str
    get_task: str
    #: Value for the `A2A-Version` request header, or None to omit it. The spec
    #: says an EMPTY/absent header means v0.3, so omitting is not laziness — it
    #: is the documented way to request v0.3 semantics.
    header: Optional[str]


DIALECT_V03 = Dialect(
    version="0.3",
    send_message="message/send",
    get_task="tasks/get",
    header=None,
)

DIALECT_V10 = Dialect(
    version="1.0",
    send_message="SendMessage",
    get_task="GetTask",
    header="1.0",
)


class UnsupportedProtocolVersion(ValueError):
    """The peer's card declares a protocol generation we do not speak."""


def resolve_dialect(protocol_version: Any) -> Dialect:
    """Pick the wire vocabulary from a peer card's `protocolVersion`.

    * absent / non-string / unparseable / `0.3.x` → **v0.3**. Defaulting rather
      than erroring is the spec's back-compat rule, and it is what makes a
      zero-configuration federation call to another Trinity work.
    * `1.x` → raises `UnsupportedProtocolVersion` (see the module docstring:
      documented, deliberately not claimed).
    * anything else → raises.

    The default is the SAFE direction here: guessing "v0.3" against a peer that
    speaks something else produces a clean `method not found` from the peer,
    whereas guessing "v1.0" would send a credential using a vocabulary we have
    never exercised.
    """
    if not isinstance(protocol_version, str) or not protocol_version.strip():
        return DIALECT_V03
    raw = protocol_version.strip()
    major = raw.split(".", 1)[0].strip()
    if major == "0":
        return DIALECT_V03
    raise UnsupportedProtocolVersion(
        f"Peer declares A2A protocolVersion {raw!r}; this client speaks 0.3.x. "
        "The 1.x dialect is defined but not enabled (no peer to verify it "
        "against) — see requirements mcp.md §32.5 FR-12."
    )


# ---------------------------------------------------------------------------
# Envelope helpers — build/parse, shared so the two directions cannot disagree
# about where an error lives.
# ---------------------------------------------------------------------------

def build_request(rpc_id: str, method: str, params: Dict[str, Any]) -> Dict[str, Any]:
    """A JSON-RPC 2.0 request envelope."""
    return {"jsonrpc": "2.0", "id": rpc_id, "method": method, "params": params}


def text_message(text: str, message_id: str,
                 context_id: Optional[str] = None,
                 task_id: Optional[str] = None) -> Dict[str, Any]:
    """An A2A `message` param carrying a single text part (v0.3 `kind`)."""
    message: Dict[str, Any] = {
        "role": "user",
        "parts": [{"kind": "text", "text": text}],
        "messageId": message_id,
    }
    if context_id:
        message["contextId"] = context_id
    if task_id:
        message["taskId"] = task_id
    return message


def text_from_parts(container: Any) -> str:
    """Concatenate the text parts of a message / artifact. Never raises.

    Mirrors `routers/a2a.py::_text_from_message` for the opposite direction, and
    is deliberately tolerant: every field here is peer-controlled, so a
    malformed part must yield "no text", never an exception on the response
    path (which would turn a successful remote call into a 500).
    """
    if not isinstance(container, dict):
        return ""
    parts = container.get("parts")
    if not isinstance(parts, list):
        return ""
    out = []
    for part in parts:
        if isinstance(part, dict) and part.get("kind") == "text" and isinstance(part.get("text"), str):
            out.append(part["text"])
    return "\n".join(out).strip()
