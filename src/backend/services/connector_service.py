"""Pure helpers for the per-agent MCP connector (ent#46; OSS-core since #118).

(a) build copy-paste-ready connector configuration per AI client with the scoped
key pre-embedded, and (b) resolve an agent's exposed playbooks against its
allow-list + the ``user_invocable`` exclusion.

No DB or HTTP — the router owns persistence and the MCP-URL lookup. Relocated
verbatim from the private ``enterprise/backend/mcp_connector/service.py`` (#118).
"""
from __future__ import annotations

import json
import re
from typing import List, Optional

from models import ConnectorClientSnippet, ConnectorPlaybook


def connector_name(agent_name: str) -> str:
    """A safe MCP-server name for ``claude mcp add <name>`` / config keys."""
    slug = re.sub(r"[^a-z0-9]+", "-", agent_name.lower()).strip("-")
    return f"trinity-{slug or 'agent'}"


def build_snippets(agent_name: str, mcp_url: str, api_key: str) -> List[ConnectorClientSnippet]:
    """Per-client connector config blocks with the key pre-embedded."""
    name = connector_name(agent_name)
    auth = f"Bearer {api_key}"

    claude_code_cli = (
        f'claude mcp add --transport http {name} {mcp_url} '
        f'--header "Authorization: {auth}"'
    )
    mcp_json = {
        "mcpServers": {
            name: {"type": "http", "url": mcp_url, "headers": {"Authorization": auth}}
        }
    }
    mcp_json_block = json.dumps(mcp_json, indent=2)
    desktop_block = json.dumps({"url": mcp_url, "headers": {"Authorization": auth}}, indent=2)

    return [
        ConnectorClientSnippet(
            client="claude-code", label="Claude Code", format="shell",
            content=claude_code_cli,
            note="Run in your terminal, then restart Claude Code (or run /mcp).",
        ),
        ConnectorClientSnippet(
            client="claude-code-json", label="Claude Code (.mcp.json)", format="json",
            content=mcp_json_block,
            note="Add to .mcp.json under mcpServers if you prefer a config file.",
        ),
        ConnectorClientSnippet(
            client="cursor", label="Cursor", format="json",
            content=mcp_json_block,
            note="Add to .cursor/mcp.json (project) or ~/.cursor/mcp.json (global).",
        ),
        ConnectorClientSnippet(
            client="claude-desktop", label="Claude Desktop / claude.ai", format="json",
            content=desktop_block,
            note="Add as a Connector: paste the URL and Authorization header.",
        ),
    ]


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
