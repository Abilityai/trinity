"""
A2A Agent Card generator (#737 card; version pinned to 0.3.0 by ent#157).

The A2A (Agent-to-Agent) protocol's discovery primitive is a JSON
document — the "Agent Card" — that describes an agent's identity,
capabilities, skills, and auth requirements. External orchestrators
(AWS Bedrock, Azure Copilot, Google ADK, …) consume this to discover
and call agents without knowing the host platform's internal API.

Phase 1 scope: minimum viable card generated from `template.yaml`
data + agent ownership metadata. The card is returned as a Python
dict; the router decides how to serve it (JSON response,
`/.well-known/agent-card.json` proxying, etc.).

Out of scope (subsequent phases):
- Redis caching (template.yaml is already loaded at agent-server
  level; the backend's only job here is mapping)
- Extended card variant with auth-gated fields (agent-private URLs,
  full skill schemas) — Phase 1 always returns the public card
- The A2A JSON-RPC endpoint that the card's `url` field would
  ideally point to — issue text acknowledges this is a follow-up
- MCP `get_agent_card` tool — surface for agents to introspect each
  other; lives in the MCP server, not here
- Schema validation against a published JSON Schema (the A2A spec
  doesn't publish a stable schema bundle yet)

A2A spec reference: https://google.github.io/A2A/ (targeting v0.3.x —
see `protocolVersion` below)
(A2A is Google's open protocol; field names mirror the spec verbatim.)
"""

from __future__ import annotations

from typing import Any, Dict, List, Optional


def _skills_from_capabilities(
    capabilities: List[str],
    use_cases: List[str],
    description: str,
) -> List[Dict[str, Any]]:
    """Build the A2A `skills[]` array from Trinity's `capabilities[]`.

    Trinity templates use `capabilities` (short tag strings) rather
    than A2A's structured skill objects. We map each capability to a
    skill record, attach `use_cases` as `examples`, and reuse the
    agent's top-level description. The `id` and `name` are the
    capability string itself — A2A allows arbitrary strings for both.
    """
    if not capabilities:
        return []

    # Distribute use_cases across capabilities as examples. Cheap
    # heuristic: every capability gets the full use_cases list — A2A
    # has no rule against duplication and orchestrators that surface
    # examples will benefit from the broader context per skill.
    examples = use_cases or []

    skills = []
    for cap in capabilities:
        if not isinstance(cap, str) or not cap.strip():
            continue
        skill_id = cap.strip()
        skills.append({
            "id": skill_id,
            "name": skill_id,
            "description": description or skill_id,
            "tags": [skill_id],
            "examples": examples,
        })
    return skills


def generate_a2a_card(
    agent_name: str,
    template_data: Dict[str, Any],
    base_url: Optional[str] = None,
) -> Dict[str, Any]:
    """Build an A2A Agent Card (protocol `0.3.0`) for `agent_name` from template data.

    Args:
        agent_name: Trinity agent name (used for URL construction and
            as the card's `name` if the template doesn't override).
        template_data: Parsed `template.yaml` dict as returned by the
            agent-server's `/api/template/info` endpoint. May be
            partial if the agent is stopped (fields default in the
            map below).
        base_url: External base URL of this Trinity instance (e.g.
            "https://trinity.example.com"). Used to construct the
            card's `url` (where A2A clients call the agent) and
            `documentationUrl`. None ⇒ omit those fields; clients
            either fail closed or fall back to the discovery URL.

    Returns:
        A2A `0.3.0`-compliant card dict ready for JSON serialisation.
    """
    # template.yaml shape (Trinity-internal):
    #   name, display_name, description, tagline, version, author,
    #   capabilities[], use_cases[], resources, mcp_servers[], …
    display_name = template_data.get("display_name") or template_data.get("name") or agent_name
    description = (
        template_data.get("description")
        or template_data.get("tagline")
        or f"Trinity agent: {display_name}"
    )
    version = str(template_data.get("version") or "1.0.0")
    capabilities_list = template_data.get("capabilities") or []
    use_cases = template_data.get("use_cases") or []

    skills = _skills_from_capabilities(capabilities_list, use_cases, description)

    card: Dict[str, Any] = {
        # ent#157: pin the targeted A2A spec version. The prior "1.0" was a
        # placeholder that never had an endpoint behind it; the JSON-RPC server
        # this card now points at speaks the v0.3.x method set (message/send,
        # message/stream, tasks/get, tasks/cancel), lowerCamel method casing.
        "protocolVersion": "0.3.0",
        "name": display_name,
        "description": description,
        "version": version,
        # `provider` identifies the host platform — useful for clients
        # that route differently per host. Hard-coded "Trinity" here;
        # the agent's owner could be exposed via the extended card but
        # is intentionally not on the public card (PII).
        "provider": {
            "organization": "Trinity",
            "url": base_url or "",
        },
        # Streaming/SSE: the agent-server supports SSE on /api/chat/stream,
        # so every Trinity agent advertises streaming=true. Push
        # notifications + state-transition history aren't part of the
        # current agent-server surface; set false explicitly so clients
        # don't probe for them.
        "capabilities": {
            "streaming": True,
            "pushNotifications": False,
            "stateTransitionHistory": False,
        },
        "defaultInputModes": ["text"],
        "defaultOutputModes": ["text"],
        "skills": skills,
        # Trinity agents are reachable only via authenticated calls.
        # The public card declares the auth scheme so orchestrators
        # know to attach a bearer token; the actual MCP key issuance
        # happens out-of-band via the Trinity UI.
        "securitySchemes": {
            "bearerAuth": {
                "type": "http",
                "scheme": "bearer",
                "description": "Trinity MCP API key (issued via Settings → MCP Keys)",
            },
        },
        "security": [{"bearerAuth": []}],
        # ent#157: JSON-RPC 2.0 over HTTP is the transport the `url` endpoint
        # speaks (message/send + message/stream SSE + tasks/get + tasks/cancel).
        "preferredTransport": "JSONRPC",
    }

    # ent#157: `url` points at the real A2A JSON-RPC endpoint served by
    # `routers/a2a.py` (`POST {base}/a2a/{name}`), NOT the old chat placeholder.
    # The well-known discovery doc lives at `{base}/a2a/{name}/.well-known/
    # agent-card.json`. base_url == "" ⇒ omit (relative resolution by the client).
    if base_url:
        b = base_url.rstrip("/")
        card["url"] = f"{b}/a2a/{agent_name}"
        card["documentationUrl"] = f"{b}/a2a/{agent_name}/.well-known/agent-card.json"

    return card


# ===========================================================================
# ent#679 — the card states the price
# ===========================================================================

#: Nevermined's own payment-extension URI, as emitted by the provider SDK's
#: `payments_py.a2a.agent_card.build_payment_agent_card`. We speak that
#: vocabulary verbatim so a payments-py client reads `agentId` / `planId`
#: straight off our card with no Trinity-specific knowledge.
#:
#: The **official A2A x402 extension URI** (`A2A_X402_EXTENSION_URI`, which the
#: SDK's helper also appends) is deliberately NOT declared. Per the A2A
#: extension spec, declaring an extension advertises the activation handshake
#: for it (`X-A2A-Extensions` negotiation), and Trinity runs no handshake — it
#: reads the in-band payment metadata and answers 402. Declaring the URI would
#: promise a protocol we do not implement, which is worse for a generic client
#: than saying nothing: it would activate the extension and then wait.
NEVERMINED_PAYMENT_EXTENSION_URI = "urn:nevermined:payment"


def _cost_description(credits: int, plan_id: str) -> str:
    """Human-readable price line for the extension's `description`.

    `credits == 0` is a duration/time-based Nevermined plan (ent#679 T9): the
    burn is whatever the plan defines and the per-call amount is not a fixed
    number, so saying "0 credits per call" would read as free. Say what is
    true instead — the plan sets the cost.
    """
    if credits <= 0:
        return f"Cost per call is set by Nevermined plan {plan_id}"
    unit = "credit" if credits == 1 else "credits"
    return f"{credits} {unit} per call via Nevermined plan {plan_id}"


def with_payment_extension(
    card: Dict[str, Any],
    pricing: Any,
    *,
    agent_name: str,
    base_url: str = "",
) -> Dict[str, Any]:
    """Declare the agent's price on its A2A card (ent#679 AC2).

    A stranger that gets a 402 from `POST /a2a/{name}` can act on it, but it
    has to call first to learn there is a price at all. The card is the
    discovery document, so the price belongs on the card: an x402-speaking
    client can mint a token from `agentId` + `planId` and meet the paywall on
    its first request, and a human following `paymentInfoUrl` lands on the
    public `GET /api/paid/{name}/info` document that says what to buy.

    **Pure.** No I/O, no edition awareness — the caller does the config read
    and decides whether this agent is priced. `pricing` is a Nevermined config
    (anything carrying `nvm_agent_id` / `nvm_plan_id` / `credits_per_request` /
    `nvm_environment` / `enabled`); `None`, a disabled config, or one missing
    its plan/agent ids returns **the card object unchanged, by identity**, so
    an unpriced agent's card is byte-identical to before this change. A priced
    agent gets a new dict — the input is never mutated.

    The declared `paymentType` follows the credit amount rather than being
    hardcoded "fixed": a 0-credit duration plan charges by time, and declaring
    `{paymentType: "fixed", credits: 0}` is a contradiction the SDK's own card
    validator rejects for a paid plan.

    Note for an OSS reader: this block says what the agent costs, not that the
    door is open. The paid A2A door also needs A2A exposure to be ON, which is
    the entitled enterprise setter's flag — so in an OSS-only build a
    configured price block points at a door that answers 404 (ent#679 T1).
    """
    if pricing is None or not getattr(pricing, "enabled", False):
        return card

    agent_id = getattr(pricing, "nvm_agent_id", None)
    plan_id = getattr(pricing, "nvm_plan_id", None)
    if not agent_id or not plan_id:
        # A config that cannot tell a client what to buy is worse than silence:
        # the client would activate a payment flow with no plan to pay into.
        return card

    try:
        credits = int(getattr(pricing, "credits_per_request", 0) or 0)
    except (TypeError, ValueError):
        credits = 0

    params: Dict[str, Any] = {
        "agentId": agent_id,
        "planId": plan_id,
        "credits": credits,
        "paymentType": "fixed" if credits > 0 else "dynamic",
        "costDescription": _cost_description(credits, plan_id),
    }
    environment = getattr(pricing, "nvm_environment", None)
    if environment:
        params["environment"] = environment
    if base_url:
        params["paymentInfoUrl"] = f"{base_url.rstrip('/')}/api/paid/{agent_name}/info"

    capabilities = dict(card.get("capabilities") or {})
    extensions = list(capabilities.get("extensions") or [])
    extensions.append({
        "uri": NEVERMINED_PAYMENT_EXTENSION_URI,
        "description": params["costDescription"],
        "required": False,
        "params": params,
    })
    capabilities["extensions"] = extensions
    return {**card, "capabilities": capabilities}
