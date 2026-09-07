"""The agent's voice system prompt (VOICE-005) — one resolver, two front doors.

Lifted out of `routers/voice.py` for ent#534: the Workspace's voice start lives
in `client_portal/`, which must never import a router (Invariant #1 — routers
hold no business logic, and `client_portal` → `routers` would be a layering
edge no test harness expects). Provider-neutral by construction: nothing here
knows which realtime model will speak the prompt (ent#354).

Priority:
  1. Per-agent `voice_system_prompt` from the DB (set via API)
  2. `voice-agent-system-prompt.md` in the agent container's home
  3. Auto-generated from the agent's template info (description + voice
     behaviour hints)
"""
from __future__ import annotations

import logging

from database import db
from services.agent_auth import agent_httpx_client
from services.docker_service import get_agent_container

logger = logging.getLogger(__name__)


async def get_voice_system_prompt(agent_name: str) -> str:
    """Resolve the voice system prompt for ``agent_name`` (see module doc)."""
    # 1. DB override
    prompt = db.get_voice_system_prompt(agent_name)
    if prompt:
        return prompt

    # 2. Agent container file
    container = get_agent_container(agent_name)
    if container:
        try:
            from services.docker_utils import container_exec_run
            result = await container_exec_run(
                container,
                "cat /home/developer/voice-agent-system-prompt.md",
                user="developer",
            )
            output = result.output.decode("utf-8").strip() if hasattr(result, "output") else str(result).strip()
            if output and "No such file" not in output and len(output) > 10:
                return output
        except Exception as e:  # noqa: BLE001 — fall through to the generated prompt
            logger.debug(f"Could not read voice-agent-system-prompt.md from {agent_name}: {e}")

    # 3. Auto-generate from template info
    description = None
    if container:
        try:
            async with agent_httpx_client(agent_name, timeout=5.0) as client:
                resp = await client.get(f"http://agent-{agent_name}:8000/api/template/info")
                if resp.status_code == 200:
                    info = resp.json()
                    description = info.get("description") or info.get("summary")
        except Exception:  # noqa: BLE001
            pass

    display_name = agent_name.replace("-", " ").title()
    lines = [f"You are {display_name}, an AI agent."]
    if description:
        lines.append(f"\n{description}")
    lines.append(
        "\n## Voice Behaviour\n"
        "You are in a voice conversation. Keep responses concise and natural for speech. "
        "No bullet points, markdown formatting, or code blocks — speak as you would in conversation. "
        "One idea at a time. Use the run_task tool when you need to look something up or take an action."
    )
    return "\n".join(lines)
