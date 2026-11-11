"""Writers of an agent's own model, ``agent_ownership.default_model`` (#4442).

``services/agent_model_service`` reads that column for every backend turn that
names no model (wakes, ask endings, events, unpinned reminders and schedules).
Before #4442 only a private module wrote it, so on every other agent those
turns ran on the platform default while chat ran on the agent's model. The OSS
writers are:

* ``PUT /api/agents/{name}/model`` (Settings → Model), which stores what the
  agent accepted (``routers/chat.py``);
* the create seed, ``seed_default_model``, from the template's declared model;
* the boot fill, ``fill_blank_default_models``, for agents created before this:
  its container env first, then its template.yaml, and only when neither
  declares one, the running agent's own ``GET /api/model`` (whatever the last
  chat turn sent, so it is never preferred over a declared model).

The seed and the fill write only into a NULL column (``set_default_model_if``
with ``expected=None``), so they never overwrite a value another writer set,
and several workers running the fill together make one write per agent.
A value is written only when this instance can run it on the agent's runtime
(``unrunnable_reason``): it reaches the CLI's ``--model`` argument.
"""
from __future__ import annotations

import asyncio
import logging
import urllib.parse
from typing import Any, Awaitable, Callable, Dict, Iterable, Optional

from services.model_catalog import unrunnable_reason

logger = logging.getLogger(__name__)

TEMPLATE_PATH = "/home/developer/template.yaml"
_BOOT_DELAY_S = 17
_boot_task: Optional[asyncio.Task] = None


def declared_model(template_data: Any) -> Optional[str]:
    """The model a parsed ``template.yaml`` declares: ``runtime.model``, else a
    top-level ``model:``. None when it declares neither (or is not a mapping)."""
    if not isinstance(template_data, dict):
        return None
    runtime = template_data.get("runtime")
    if isinstance(runtime, dict):
        value = runtime.get("model")
        if isinstance(value, str) and value.strip():
            return value.strip()
    value = template_data.get("model")
    if isinstance(value, str) and value.strip():
        return value.strip()
    return None


def _claude(runtime: Optional[str]) -> bool:
    return (runtime or "claude-code").lower() == "claude-code"


def seed_default_model(agent_name: str, candidate: Optional[str],
                       runtime: Optional[str] = None, *, db_ops=None) -> bool:
    """Write ``candidate`` into the agent's NULL column. True when this call
    wrote it. A non-Claude agent, a blank or an unrunnable value writes
    nothing; a column that already holds a value is left alone."""
    value = (candidate or "").strip() or None
    if value is None or not _claude(runtime) or unrunnable_reason(value, "claude-code"):
        return False
    if db_ops is None:
        from database import db as db_ops
    return bool(db_ops.set_default_model_if(agent_name, None, value))


async def _read_live_template(agent_name: str) -> Optional[dict]:
    """The running agent's own ``template.yaml``, parsed; None on any failure."""
    from services.agent_client import get_agent_client
    from utils.safe_yaml import load_template_yaml

    try:
        path = urllib.parse.quote(TEMPLATE_PATH, safe="")
        response = await get_agent_client(agent_name).get(
            f"/api/files/download?path={path}", timeout=10.0)
        if getattr(response, "status_code", 0) != 200:
            return None
        data = load_template_yaml(response.text)
    except Exception as e:  # noqa: BLE001
        logger.info("[#4442] %s's template.yaml not read: %s", agent_name, type(e).__name__)
        return None
    return data if isinstance(data, dict) else None


async def _read_live_model(agent_name: str) -> Optional[str]:
    """The model the running agent runs now (its ``GET /api/model``): the
    Settings pick it holds in memory, which starts from ``AGENT_RUNTIME_MODEL``.
    None on any failure."""
    from services.agent_client import get_agent_client

    try:
        response = await get_agent_client(agent_name).get("/api/model", timeout=10.0)
        if getattr(response, "status_code", 0) != 200:
            return None
        value = response.json().get("model")
    except Exception as e:  # noqa: BLE001
        logger.info("[#4442] %s's live model not read: %s", agent_name, type(e).__name__)
        return None
    return value.strip() if isinstance(value, str) and value.strip() else None


def _live_agents() -> Iterable[tuple]:
    from services.docker_service import list_all_agents_fast

    return [(a.name, a.runtime, a.status) for a in list_all_agents_fast()]


async def fill_blank_default_models(
    *,
    db_ops=None,
    list_agents: Callable[[], Iterable[tuple]] = None,
    read_env_model: Callable[[str], Optional[str]] = None,
    read_template: Callable[[str], Awaitable[Optional[dict]]] = None,
    read_live_model: Callable[[str], Awaitable[Optional[str]]] = None,
) -> Dict[str, int]:
    """The boot pass: give every Claude Code agent whose column is NULL the
    model it runs now: its container env (``AGENT_RUNTIME_MODEL``) first; then
    the model its own ``template.yaml`` declares (running agents only); and only
    when neither declares one, the running agent's live ``GET /api/model``.
    The live value is last because it is whatever the last ``/api/chat`` turn
    sent, which before #4442 could be the platform default, and storing that
    would make the cheaper model permanent (merge-train decision on #4466).
    Idempotent, unclaimed: every worker may run it. Returns counts only (no
    names, no values)."""
    if db_ops is None:
        from database import db as db_ops
    if list_agents is None:
        list_agents = _live_agents
    if read_env_model is None:
        from services.docker_service import read_runtime_model as read_env_model
    if read_template is None:
        read_template = _read_live_template
    if read_live_model is None:
        read_live_model = _read_live_model

    counts = {"blank": 0, "filled": 0, "nothing_declared": 0, "refused": 0, "failed": 0}
    agents = [a for a in await asyncio.to_thread(lambda: list(list_agents())) if _claude(a[1])]
    stored = await asyncio.to_thread(db_ops.get_default_models_raw, [a[0] for a in agents])
    for name, runtime, status in agents:
        if stored.get(name):
            continue
        counts["blank"] += 1
        try:
            candidate = await asyncio.to_thread(read_env_model, name)
            if not candidate and status == "running":
                candidate = declared_model(await read_template(name))
            if not candidate and status == "running":
                candidate = await read_live_model(name)
            if not candidate:
                counts["nothing_declared"] += 1
            elif unrunnable_reason(candidate, "claude-code"):
                counts["refused"] += 1
            elif await asyncio.to_thread(seed_default_model, name, candidate, runtime,
                                         db_ops=db_ops):
                counts["filled"] += 1
        except Exception as e:  # noqa: BLE001 — one agent never stops the pass
            counts["failed"] += 1
            logger.warning("[#4442] model fill of %s failed: %s", name, type(e).__name__)
    if counts["blank"]:
        logger.info("[#4442] agent model boot fill: %s", counts)
    return counts


def schedule_boot_fill() -> None:
    """Start the boot pass after a stagger. Never raises; with no running loop
    it is skipped (the next boot runs it)."""

    async def _run() -> None:
        await asyncio.sleep(_BOOT_DELAY_S)
        try:
            await fill_blank_default_models()
        except Exception as e:  # noqa: BLE001 — a boot task must not kill boot
            logger.error("[#4442] agent model boot fill failed: %s", type(e).__name__)

    global _boot_task
    coro = _run()
    try:
        _boot_task = asyncio.create_task(coro)
    except RuntimeError as e:
        coro.close()
        logger.debug("[#4442] agent model boot fill not scheduled (no loop): %s", e)
