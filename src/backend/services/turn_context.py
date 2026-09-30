"""Turn-context seam: lines a module adds to a Workspace chat or room turn.

The Workspace composes each agent turn from optional prefixes (history, the
open canvas, the file manifest). This registry lets another module add its own
line without the composers knowing about it. A provider receives a
:class:`TurnContext` and returns one line, or ``""`` / ``None`` for nothing.

* OSS-only build → no provider registered → :func:`collect` returns ``""``.
  Zero behavioural change.
* Enterprise build → a registered provider answers from its own records.

Two rules every provider relies on:

* **The platform decides the context, never the agent or the request.** The
  composer fills ``TurnContext`` from the chat row or room it already resolved.
* **``internal_audience``** is the platform's own verdict on whether anyone
  outside the organisation can read the reply: the caller's ``is_platform`` for
  a 1:1 chat, and ``not room_is_user_facing(...)`` for a room. A provider whose
  line is internal-only must return nothing when it is False.

Fail-open (Trinity's availability bias): a provider that raises is logged and
skipped; it never fails the turn.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Callable, List, Optional

logger = logging.getLogger(__name__)

SURFACES = ("thread", "room")


@dataclass(frozen=True)
class TurnContext:
    surface: str                 # "thread" (a 1:1 Workspace chat) | "room"
    agent_name: str              # the agent taking this turn
    chat_id: str                 # enterprise_portal_sessions.id | room id
    person_email: Optional[str]  # the person whose message woke the turn, if any
    internal_audience: bool      # True only when no outside client can read the reply

    def __post_init__(self) -> None:
        if self.surface not in SURFACES:
            raise ValueError(f"TurnContext: unknown surface {self.surface!r}")


TurnContextProvider = Callable[[TurnContext], Optional[str]]

_providers: List[TurnContextProvider] = []


def register_provider(provider: TurnContextProvider) -> None:
    """Register a provider. Idempotent per function object; order is kept."""
    if provider not in _providers:
        _providers.append(provider)
        logger.info("[turn_context] provider registered: %s",
                    getattr(provider, "__qualname__", type(provider).__name__))


def clear_providers() -> None:
    """Drop every provider — used by tests to restore the OSS no-op path."""
    _providers.clear()


def collect(ctx: TurnContext) -> str:
    """Every provider's line for this turn, each followed by a blank line, or ``""``."""
    lines: List[str] = []
    for provider in list(_providers):
        try:
            line = provider(ctx)
        except Exception:  # noqa: BLE001 — fail open, never fail the turn
            logger.warning("[turn_context] provider %s failed; skipping",
                           getattr(provider, "__qualname__", provider), exc_info=True)
            continue
        line = (line or "").strip()
        if line:
            lines.append(line)
    return "".join(f"{line}\n\n" for line in lines)
