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
