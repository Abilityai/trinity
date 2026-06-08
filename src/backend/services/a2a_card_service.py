"""
A2A v1.0 Agent Card generator (Issue #737 Phase 1).

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

A2A v1.0 spec reference: https://google.github.io/A2A/
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
    """Build an A2A v1.0 Agent Card for `agent_name` from template data.

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
        A2A v1.0-compliant card dict ready for JSON serialisation.
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
        "protocolVersion": "1.0",
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
    }

    # URL points to where A2A clients would call the agent's JSON-RPC
    # endpoint. The actual A2A JSON-RPC server is a separate ticket
    # (issue #737 explicitly defers it). Placeholder URL: the existing
    # public-chat endpoint, which IS callable today even though it's
    # not strictly A2A protocol. Clients can use this to test
    # reachability; full A2A semantics arrive with the JSON-RPC server.
    if base_url:
        card["url"] = f"{base_url.rstrip('/')}/api/agents/{agent_name}/chat"

    return card
