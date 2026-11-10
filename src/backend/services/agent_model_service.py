"""The one resolver for "what does a turn that names no model run on" (ent#1202).

Precedence, the same on every path::

    explicit model on the request (reminder pin, schedule, loop, picker)
      > public-channel override         (public-facing paths only; their own rung)
      > agent_ownership.default_model   (this module: runnable on read, else skipped by name)
      > platform default model          (#831)

A non-Claude agent gets ``None`` and runs on its own model (``AGENT_RUNTIME_MODEL``
or its CLI default): the platform default is a Claude id.

The stored value is checked on every read with ``model_catalog.unrunnable_reason``
— an allowlist — so an id this instance cannot run never reaches the CLI. It is
skipped, logged once per (agent, value) per process, and reported through
``refused_reason`` so every surface can name it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Optional

from services.model_catalog import unrunnable_reason

logger = logging.getLogger(__name__)

SOURCE_AGENT = "agent"
SOURCE_PLATFORM_DEFAULT = "platform_default"
SOURCE_RUNTIME = "runtime"

_warned: set[tuple[str, str]] = set()


@dataclass(frozen=True)
class ResolvedModel:
    model: Optional[str]           # what the turn runs on; None = the runtime's own
    source: str                    # agent | platform_default | runtime
    stored: Optional[str]          # the agent's stored default_model, as written
    refused_reason: Optional[str]  # why `stored` was skipped, or None


def choose(stored: Optional[str], runtime: str, platform_default: Optional[str]) -> ResolvedModel:
    """Pure precedence over already-read facts (no I/O)."""
    stored = (stored.strip() or None) if isinstance(stored, str) else None
    if (runtime or "claude-code") != "claude-code":
        return ResolvedModel(None, SOURCE_RUNTIME, stored, unrunnable_reason(stored, runtime))
    reason = unrunnable_reason(stored, runtime)
    if stored and reason is None:
        return ResolvedModel(stored, SOURCE_AGENT, stored, None)
    return ResolvedModel(platform_default or None, SOURCE_PLATFORM_DEFAULT, stored, reason)


def _runtime_of(agent_name: str) -> str:
    """The agent's runtime, Claude Code on any failure (never blocks dispatch)."""
    try:
        from services.docker_service import get_agent_runtime

        return get_agent_runtime(agent_name) or "claude-code"
    except Exception:  # noqa: BLE001
        return "claude-code"


def resolve_default_model(agent_name: str, *, runtime: Optional[str] = None,
                          strict: bool = False, db_ops=None, settings=None) -> ResolvedModel:
    """Resolve the model a turn naming none runs on, for one agent.

    Fail-open by default: a failed read of the agent's own model degrades to the
    platform default, so a turn is never refused over the stamp. ``strict=True``
    re-raises instead, for a caller that must not create a row it cannot finish.
    ``db_ops`` / ``settings`` default to the platform singletons; a router passes
    its own module references so they stay its one seam.
    """
    if settings is None:
        from services import settings_service as settings

    runtime = runtime or _runtime_of(agent_name)
    if runtime != "claude-code":
        return choose(None, runtime, None)
    try:
        if db_ops is None:
            from database import db as db_ops

        stored = db_ops.get_default_model_raw(agent_name)
    except Exception as e:  # noqa: BLE001
        if strict:
            raise
        logger.warning("[ent#1202] default_model read failed for %s: %s", agent_name, type(e).__name__)
        stored = None
    resolved = choose(stored, runtime, settings.get_platform_default_model())
    if resolved.refused_reason and (agent_name, resolved.stored) not in _warned:
        _warned.add((agent_name, resolved.stored))
        shown = resolved.stored if len(resolved.stored) <= 64 else resolved.stored[:64] + "…"
        logger.warning(
            "[ent#1202] %s's own model %r cannot run here (%s); its turns run on %s",
            agent_name, shown, resolved.refused_reason, resolved.model,
        )
    return resolved
