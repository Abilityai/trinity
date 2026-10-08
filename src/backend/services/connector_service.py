"""Helpers for the per-agent MCP connector (ent#46; OSS-core since #118).

(a) build copy-paste-ready connector configuration per AI client with the scoped
key pre-embedded, (b) resolve an agent's exposed playbooks against its
allow-list + the ``user_invocable`` exclusion, and (c) fetch an agent's live
playbook list from its container.

(a) and (b) are pure. (c) — ``fetch_live_playbooks`` — talks to the agent
container; it lives here rather than in a router because BOTH the owner-facing
connector route and the #848 inline-auth route need it, and a second copy would
be a place for the two surfaces to drift. No DB access either way: the caller
owns persistence and the MCP-URL lookup. Relocated from the private
``enterprise/backend/mcp_connector/service.py`` (#118).
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from fastapi import HTTPException

from models import ConnectorClientSnippet, ConnectorPlaybook


def connector_name(agent_name: str) -> str:
    """A safe MCP-server name for ``claude mcp add <name>`` / config keys."""
    slug = re.sub(r"[^a-z0-9]+", "-", agent_name.lower()).strip("-")
    return f"trinity-{slug or 'agent'}"


def _client_snippets(
    name: str, mcp_url: str, api_key: Optional[str]
) -> List[ConnectorClientSnippet]:
    """The four per-client config blocks. ``api_key=None`` builds the KEYLESS
    variant (#848): no Authorization header, so the client connects as an
    anonymous MCP session that then signs in with ``request_login`` /
    ``verify_login``. Keeping both variants in one shape stops the keyed and
    keyless setups from drifting apart.
    """
    keyless = api_key is None
    server: dict = {"type": "http", "url": mcp_url}
    desktop: dict = {"url": mcp_url}
    if not keyless:
        auth = f"Bearer {api_key}"
        server["headers"] = {"Authorization": auth}
        desktop["headers"] = {"Authorization": auth}

    if keyless:
        cli = f"claude mcp add --transport http {name} {mcp_url}"
    else:
        cli = (
            f'claude mcp add --transport http {name} {mcp_url} '
            f'--header "Authorization: Bearer {api_key}"'
        )

    mcp_json_block = json.dumps({"mcpServers": {name: server}}, indent=2)
    desktop_block = json.dumps(desktop, indent=2)

    login_hint = " Then call request_login with your email, and verify_login with the code."
    return [
        ConnectorClientSnippet(
            client="claude-code", label="Claude Code", format="shell",
            content=cli,
            note="Run in your terminal, then restart Claude Code (or run /mcp)."
            + (login_hint if keyless else ""),
        ),
        ConnectorClientSnippet(
            client="claude-code-json", label="Claude Code (.mcp.json)", format="json",
            content=mcp_json_block,
            note="Add to .mcp.json under mcpServers if you prefer a config file."
            + (login_hint if keyless else ""),
        ),
        ConnectorClientSnippet(
            client="cursor", label="Cursor", format="json",
            content=mcp_json_block,
            note="Add to .cursor/mcp.json (project) or ~/.cursor/mcp.json (global)."
            + (login_hint if keyless else ""),
        ),
        ConnectorClientSnippet(
            client="claude-desktop", label="Claude Desktop / claude.ai", format="json",
            content=desktop_block,
            note=(
                "Add as a Connector: paste the URL, then sign in with request_login / verify_login."
                if keyless
                else "Add as a Connector: paste the URL and Authorization header."
            ),
        ),
    ]


def build_snippets(agent_name: str, mcp_url: str, api_key: str) -> List[ConnectorClientSnippet]:
    """Per-client connector config blocks with the key pre-embedded."""
    return _client_snippets(connector_name(agent_name), mcp_url, api_key)


def build_keyless_snippets(agent_name: str, mcp_url: str) -> List[ConnectorClientSnippet]:
    """Per-client connector config with NO key (#848 inline email auth).

    The collaborator adds this, connects as an anonymous session, and signs in
    with ``request_login`` / ``verify_login`` — no pre-minted key changes hands.
    Only meaningful when ``MCP_INLINE_AUTH_ENABLED`` is on (an anonymous MCP
    session is rejected otherwise); the caller gates on the flag.
    """
    return _client_snippets(connector_name(agent_name), mcp_url, None)


async def fetch_live_playbooks(agent_name: str) -> List[dict]:
    """The agent's live playbook list, read from its running container.

    Shared by the owner-facing connector route and the #848 inline-auth route so
    the two surfaces can never disagree about what an agent advertises. Raises
    the same HTTP mapping both callers already relied on: 404 no container, 503
    not running / unreachable, 504 starting up.
    """
    # trinity-enterprise#754: the one proxy (it also refreshes the agent's
    # last-known listing); the HTTP mapping above is unchanged.
    from services import agent_skills_listing

    try:
        body = await agent_skills_listing.fetch_live(agent_name)
    except agent_skills_listing.SkillsListUnavailable as e:
        detail = "Agent is not running." if e.reason == "not_running" else e.detail
        raise HTTPException(status_code=e.status_code, detail=detail)
    return body.get("skills", []) if isinstance(body, dict) else []


def resolve_exposed_playbooks(
    live_playbooks: List[dict],
    exposed_allow_list: Optional[List[str]],
) -> List[ConnectorPlaybook]:
    """Filter the agent's live playbooks to what the connector exposes.

    1. ``user_invocable == False`` playbooks are NEVER exposed.
    2. ``exposed_allow_list is None`` ⇒ expose all remaining; else only listed.
    """
    allow = set(exposed_allow_list) if exposed_allow_list is not None else None
    out: List[ConnectorPlaybook] = []
    for pb in live_playbooks:
        if not pb.get("user_invocable", True):
            continue
        name = pb.get("name")
        if not name:
            continue
        if allow is not None and name not in allow:
            continue
        out.append(
            ConnectorPlaybook(
                name=name,
                description=pb.get("description"),
                argument_hint=pb.get("argument_hint"),
                automation=pb.get("automation"),
            )
        )
    return out
