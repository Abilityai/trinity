"""Post-voice-session processing dispatch for the Brain Orb (trinity-enterprise#102).

The post-session run used to be a DETACHED ``claude -p`` forked by the agent's
``~/.trinity/brain-orb/action`` hook. That run was invisible to the platform —
no execution row, no cost tracking, no failure surface (an auth/billing error
became the "processed" note's content) — and the agent-server's periodic cgroup
orphan sweep (#817) SIGKILLs exactly this shape of process: an unregistered
claude reparented to PID 1. Net effect: "nothing executes after voice mode
ends" (#102).

This service replaces the detached fork with the standard execution path (the
VoIP post-call precedent, ``voip_service._dispatch_post_call``): the action
route saves the transcript through the hook as before, then calls
``dispatch_postprocess`` here, which reads the agent's own ``{enabled, prompt}``
config (agent-server ``GET /api/brain-orb/postprocess`` — the agent still owns
the semantics, Invariant #8) and pre-creates a real execution row before
backgrounding ``task_execution_service.execute_task(triggered_by="voice")``.
The run is registered in the agent's process registry (sweep-safe), shows up in
the Executions UI, and a failure lands as a FAILED execution — surfaced, never
swallowed. Never raises: a dispatch problem is reported as
``{"dispatched": False, "reason": ...}`` so a saved transcript is never lost to
a postprocess hiccup.
"""
import asyncio
import json
import logging
from typing import Optional

from services.agent_auth import agent_httpx_client

logger = logging.getLogger(__name__)

_AGENT_PORT = 8000
_CONFIG_TIMEOUT = 15.0

# Strong refs for fire-and-forget dispatch tasks — a bare create_task result is
# only weakly referenced and can be GC'd mid-run (the #526 footgun).
_background_tasks: set = set()


def _spawn_bg(coro) -> None:
    task = asyncio.create_task(coro)
    _background_tasks.add(task)
    task.add_done_callback(_background_tasks.discard)


async def read_postprocess_config(agent_name: str) -> dict:
    """The agent's ``{enabled, prompt}`` post-voice-processing config (#73), read
    via the agent-server (which resolves the JSON config + legacy .md fallback).
    Fail-closed: any transport/parse problem reads as disabled."""
    url = f"http://agent-{agent_name}:{_AGENT_PORT}/api/brain-orb/postprocess"
    try:
        async with agent_httpx_client(agent_name, timeout=_CONFIG_TIMEOUT) as client:
            response = await client.request("GET", url)
        if response.status_code != 200:
            return {"enabled": False, "prompt": ""}
        data = json.loads(response.content)
        return {
            "enabled": bool(data.get("enabled")),
            "prompt": str(data.get("prompt") or "").strip(),
        }
    except Exception as e:  # noqa: BLE001 — disabled is the safe read
        logger.warning("brain-orb postprocess config read failed for %s: %s", agent_name, e)
        return {"enabled": False, "prompt": ""}


def _compose_message(
    prompt: str,
    *,
    transcript_path: Optional[str],
    transcript_text: Optional[str],
) -> str:
    """Frame the owner-configured prompt around the transcript reference. The
    framing header marks the platform-injected part so the agent can tell the
    trigger context from the owner's instructions."""
    parts = ["[Trinity Brain Orb: post-voice-session processing]"]
    if transcript_path:
        stem = transcript_path.rsplit("/", 1)[-1]
        if stem.endswith(".md"):
            stem = stem[:-3]
        parts.append(
            f"A voice conversation just ended; its transcript was saved to `{transcript_path}` "
            "in your workspace. Read that file first."
        )
        output_hint = (
            f"Unless the instructions say otherwise, write your primary output as a note next to "
            f"the transcript named `{stem} - processed.md`, then briefly summarize what you produced."
        )
    else:
        parts.append("A voice conversation just ended. Its full transcript is included below.")
        output_hint = "When done, briefly summarize what you produced."
    parts.append("Follow these owner-configured instructions over that transcript:\n\n" + prompt)
    parts.append(output_hint)
    if transcript_text:
        parts.append("## Transcript\n" + transcript_text)
    return "\n\n".join(parts)


async def dispatch_postprocess(
    agent_name: str,
    *,
    transcript_path: Optional[str] = None,
    transcript_text: Optional[str] = None,
    source_user_id: Optional[int] = None,
    source_user_email: Optional[str] = None,
) -> dict:
    """Dispatch the configured post-session prompt over a transcript as a standard
    platform task. Returns ``{dispatched: True, execution_id}`` or
    ``{dispatched: False, reason}``. Never raises."""
    try:
        cfg = await read_postprocess_config(agent_name)
        if not cfg["enabled"] or not cfg["prompt"]:
            return {
                "dispatched": False,
                "reason": "post-processing disabled or no prompt configured",
            }
        message = _compose_message(
            cfg["prompt"],
            transcript_path=transcript_path,
            transcript_text=transcript_text,
        )

        # Pre-create the execution row (the scheduler pattern) so the caller gets
        # a real, immediately-observable execution id; the backgrounded
        # execute_task then runs against it and owns the terminal status.
        from database import db
        from services.task_execution_service import get_task_execution_service

        execution = db.create_task_execution(
            agent_name=agent_name,
            message=message,
            triggered_by="voice",
            source_user_id=source_user_id,
            source_user_email=source_user_email,
        )
        if not execution:
            return {"dispatched": False, "reason": "could not create execution record"}

        svc = get_task_execution_service()

        async def _run() -> None:
            try:
                await svc.execute_task(
                    agent_name=agent_name,
                    message=message,
                    triggered_by="voice",
                    source_user_id=source_user_id,
                    source_user_email=source_user_email,
                    execution_id=execution.id,
                )
            except Exception as e:  # noqa: BLE001 — terminal status is on the row
                logger.error(
                    "brain-orb postprocess execution failed for %s (execution %s): %s",
                    agent_name, execution.id, e,
                )

        _spawn_bg(_run())
        logger.info(
            "brain-orb postprocess dispatched for %s (execution %s)", agent_name, execution.id
        )
        return {"dispatched": True, "execution_id": execution.id}
    except Exception as e:  # noqa: BLE001 — fail-soft: the transcript save must survive
        logger.error("brain-orb postprocess dispatch failed for %s: %s", agent_name, e)
        return {"dispatched": False, "reason": f"dispatch failed: {e}"}
