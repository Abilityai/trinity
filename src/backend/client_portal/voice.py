"""Workspace voice mode (trinity-enterprise#534) — the orb takes the conversation.

The Workspace's real-time voice call is the platform's existing realtime voice
session (`services/gemini_voice.py` today; ent#354 may add a second provider)
started from, and written back into, a **Workspace thread** instead of the
Agent Detail chat. This module is everything that is Workspace-specific about
that:

  * `realtime_voice_capability` — the roster's capability field. Resolved once
    per roster load, platform principals only, fail-closed. Named for the
    capability, never the provider, and deliberately distinct from the
    per-agent `voice_available` (which means "this agent has a TTS voice").
  * `start_workspace_voice` — the start path: roster + thread ownership as ONE
    uniform 404, the voice prompt, the thread's recent turns as context, and a
    session created with the Workspace's own cap and canvas audience.
  * `persist_voice_turn` / `persist_voice_call_end` — **write-as-you-go**. Each
    spoken turn is inserted the moment the provider reports it complete, by the
    worker that holds the live session. The Agent Detail path saves at the end
    of the call; here that shape was rejected twice over (both independent
    plan reviews): under two uvicorn workers a `/stop` that lands off-worker
    reconstructs an EMPTY session from Redis and would write a phantom
    "Voice call · 0 min", and a backend restart mid-call would lose the whole
    transcript. Writing per turn makes the row order the wall clock's, makes
    the double-write impossible (the reconstructed session never receives a
    turn), and bounds the loss on a crash to the turn in flight.

Provider-neutral on purpose: this module talks to the voice service through
`create_session` and a session object's plain fields, never through
`google.genai` types.
"""
from __future__ import annotations

import asyncio
import logging
import uuid
from datetime import datetime, timedelta, timezone
from typing import Optional

from utils.helpers import utc_now_iso

from . import db
from .models import PortalRealtimeVoice

logger = logging.getLogger(__name__)

# Spoken rows are marked with this `source`; typed rows carry NULL.
VOICE_SOURCE = "voice"

# How many chars of the thread ride into the voice system prompt — the same
# budget the Agent Detail path uses (`routers/voice.py::_build_context_summary`).
_CONTEXT_MAX_CHARS = 3000
_CONTEXT_ENTRY_MAX_CHARS = 500
_CONTEXT_ROWS = 20

# Copy for the capability field. Generic on purpose (ent#354): the KEY is
# today's provider's; the sentence is about voice.
REASON_DISABLED = "Voice is turned off on this instance."
REASON_NO_KEY = "No voice provider key is configured on this instance."


# ---- The live-call lease (#2694, #2700) --------------------------------------
#
# The other half of "no typed reply lands mid-call". `start_workspace_voice`
# refuses over a reply in flight; this is what lets the TURN side refuse over a
# call in progress — the owning tab's composer is inert, but a second tab or
# the headless `/chat` surface is not.
#
# #2700: the marker is a **lease**, and the audio bridge owns it. The bridge
# arms it at connect, inside the same `try` whose `finally` releases it
# (`routers/voice.py::voice_websocket`), and renews it while it lives. It is
# NOT armed by `start_workspace_voice` any more: every path able to clear it
# lives downstream of a socket that may never open, so a start whose socket
# never opened stranded a cap-sized (~32 min) `409 voice_call_active` on every
# typed turn in that thread — naming a call the person could not end.
#
# Best-effort Redis throughout; the read stays fail-OPEN, because a Redis
# outage must not silence every typed turn.

# The TTL actually written. A lease, not the call cap: with the cap as the TTL,
# a SIGKILL, an OOM or a routine backend deploy mid-call still strands the
# thread for ~32 minutes, with a symptom identical to #2700's own title.
VOICE_MARKER_LEASE_SECONDS = 60
# The renew interval. TTL >= 4x the tick so one BGSAVE-class stall (the breaker
# client has 1s socket timeouts) cannot expire a LIVE bridge's lease — the
# ratio and its reason are lifted from `agent_call_limiter.py`'s
# INFLIGHT_MARKER_TTL_SECONDS / INFLIGHT_TICK_SECONDS, copied not invented.
VOICE_MARKER_TICK_SECONDS = 15.0
# How far past the call's own cap the renewer may keep ticking before it stops.
# Load-bearing: unbounded, a bridge whose peer died half-open would renew
# forever and hold the thread PAST the old cap-sized TTL — the fix would be a
# regression in exactly the pathological case. With it the absolute worst case
# is cap + 120 + one lease, and every real case is <= one lease.
VOICE_MARKER_SLACK_SECONDS = 120

# What the pre-#2700 `/start` wrote. Releasable by anyone: see the compare in
# `clear_voice_call_active`, so a marker stranded across the deploy is not
# immortal.
_LEGACY_MARKER_VALUE = "1"


def _voice_active_key(portal_session_id: str) -> str:
    return f"portal_voice_active:{portal_session_id}"


def mark_voice_call_active(portal_session_id: str, ttl_seconds: int, *, owner: str) -> None:
    """Arm (or renew) this thread's live-call lease, held by `owner`.

    `owner` is the call's `voice_session_id`, and it is keyword-only and
    REQUIRED: an unowned lease cannot be released safely (below), so a future
    call site must not be able to write one without saying whose it is.
    """
    try:
        from redis_breaker_util import get_breaker_redis
        client = get_breaker_redis()
        if client is not None:
            client.set(_voice_active_key(portal_session_id), str(owner), ex=int(ttl_seconds))
    except Exception as e:  # noqa: BLE001 — a marker failure must not block the call
        logger.warning("workspace voice: live-call mark failed for %s: %s", portal_session_id, e)


def clear_voice_call_active(portal_session_id: Optional[str], *, owner: str) -> None:
    """Release the lease — but only if it is still THIS call's (#2700).

    Compare-and-delete, on the `service.py::clear_turn_inflight` precedent. The
    key is one per THREAD, so a reload or a second tab arms a NEW call's lease
    over it; an unconditional delete would let a closing bridge free the thread
    of a call that is still live — the #2694 defect, re-entered through #2700's
    own fix.

    Not atomic (GET then DEL), and the failure direction is the safe one: if
    the value changed in between, the delete is skipped and the newer owner
    keeps its lease, which its own release (or its expiry) clears. Decode
    defensively — `client.get` returns `bytes` or `str` depending on the
    client's `decode_responses`, and the old code only ever tested for `None`.
    """
    if not portal_session_id:
        return
    try:
        from redis_breaker_util import get_breaker_redis
        client = get_breaker_redis()
        if client is None:
            return
        current = client.get(_voice_active_key(portal_session_id))
        current = current.decode() if isinstance(current, bytes) else current
        if current is None:
            return
        if current != str(owner) and current != _LEGACY_MARKER_VALUE:
            return
        client.delete(_voice_active_key(portal_session_id))
    except Exception as e:  # noqa: BLE001 — the lease expiry is the backstop
        logger.warning("workspace voice: live-call clear failed for %s: %s", portal_session_id, e)


def voice_call_active(portal_session_id: str) -> bool:
    """Whether a Workspace voice call is on in this thread right now. Fail-open."""
    try:
        from redis_breaker_util import get_breaker_redis
        client = get_breaker_redis()
        if client is None:
            return False
        return client.get(_voice_active_key(portal_session_id)) is not None
    except Exception as e:  # noqa: BLE001
        logger.warning("workspace voice: live-call read failed for %s: %s", portal_session_id, e)
        return False


async def renew_voice_call_marker(portal_session_id: str, *, owner: str, max_seconds: float) -> None:
    """Hold the lease for as long as the bridge that owns it lives (#2700).

    A plain timer, deliberately — neither of the two event-driven shapes works:
    re-arming on each spoken TURN expires a live-but-quiet call mid-call (the
    person listening to a long answer fires no turn), and re-arming on inbound
    audio FRAMES expires a MUTED call (`useVoiceSession.js` skips `ws.send`
    while muted). Either resurrects the very #2694 defect the marker exists to
    prevent; a timer is immune to both.

    `max_seconds` is the call's own cap plus `VOICE_MARKER_SLACK_SECONDS`, and
    it is load-bearing: past it the renewer stops even if the bridge is still
    blocked in `receive_text()`, so a peer that died half-open cannot hold the
    thread indefinitely.

    The Redis write goes off-loop (`asyncio.to_thread`) — the breaker client
    has 1s socket timeouts and this loop shares an event loop with a realtime
    audio bridge. Same reason, and the same shape, as the `agent_call_limiter`
    refresher. `mark_voice_call_active` swallows every exception, so a Redis
    outage cannot kill this loop or the call; the lease simply expires, and the
    read is fail-OPEN.
    """
    elapsed = 0.0
    try:
        while elapsed < max_seconds:
            await asyncio.sleep(VOICE_MARKER_TICK_SECONDS)
            elapsed += VOICE_MARKER_TICK_SECONDS
            await asyncio.to_thread(
                mark_voice_call_active,
                portal_session_id,
                VOICE_MARKER_LEASE_SECONDS,
                owner=owner,
            )
    except asyncio.CancelledError:
        pass


def realtime_voice_capability(is_platform: bool) -> PortalRealtimeVoice:
    """The roster's realtime-voice capability for this principal.

    A portal-token client can never use it (the audio WebSocket authenticates
    with the platform JWT), so the answer is `False` with NO reason — the UI
    renders nothing rather than a disabled control explaining a limitation that
    is not theirs to fix. A platform principal gets the honest reason when the
    instance cannot do it (the "never a dead button" AC).
    """
    if not is_platform:
        return PortalRealtimeVoice(available=False, reason=None)
    from config import GEMINI_API_KEY, VOICE_ENABLED
    if not VOICE_ENABLED:
        return PortalRealtimeVoice(available=False, reason=REASON_DISABLED)
    if not GEMINI_API_KEY:
        return PortalRealtimeVoice(available=False, reason=REASON_NO_KEY)
    return PortalRealtimeVoice(available=True, reason=None)


def build_portal_context_summary(agent_name: str, email: str, session_id: str) -> str:
    """The thread's recent turns, as the voice model's opening context.

    Same truncation rule as the Agent Detail summary. Platform `system` rows are
    skipped (the ent#523 lesson: they are the platform speaking about the
    thread, not a party to it), and an earlier call's spoken rows are labelled
    so the model knows which of its own lines were said aloud.
    """
    try:
        rows = db.get_portal_messages(agent_name, email, limit=_CONTEXT_ROWS, session_id=session_id)
    except Exception as e:  # noqa: BLE001 — context is best-effort, the call is not
        logger.warning("workspace voice: context read failed for %s/%s: %s", agent_name, email, e)
        return ""
    lines: list[str] = []
    total = 0
    for m in rows:
        role = m.get("role")
        if role == "system":
            continue
        who = "User" if role == "user" else "Assistant"
        if m.get("source") == VOICE_SOURCE:
            who += " (voice)"
        content = (m.get("content") or "").strip()
        if not content:
            continue
        if len(content) > _CONTEXT_ENTRY_MAX_CHARS:
            content = content[:_CONTEXT_ENTRY_MAX_CHARS] + "..."
        line = f"{who}: {content}"
        if total + len(line) > _CONTEXT_MAX_CHARS:
            break
        lines.append(line)
        total += len(line)
    return "\n".join(lines)


async def start_workspace_voice(
    *,
    agent_name: str,
    email: str,
    is_platform: bool,
    portal_session_id: str,
    user_id: int,
    user_label: str,
    voice_name: Optional[str] = None,
) -> dict:
    """Start a voice call bound to a Workspace thread.

    Every refusal before the provider is a `ClientPortalError`. Roster
    membership and thread ownership are evaluated BEFORE either answers, and
    both answer the same 404 — a thread id is a 128-bit secret and "not yours"
    vs "does not exist" must not be tellable apart (Invariant #8).
    """
    from .service import ClientPortalError, agent_on_roster

    on_roster = agent_on_roster(agent_name, email, is_platform)
    thread = db.get_portal_session(portal_session_id, agent_name, email) if on_roster else None
    if not is_platform or not on_roster or not thread:
        raise ClientPortalError(404, "Conversation not found")

    cap = realtime_voice_capability(is_platform)
    if not cap.available:
        raise ClientPortalError(503, cap.reason or REASON_DISABLED)

    # #2694: no typed reply may land mid-call. The tab's own composer is inert
    # during a call, but a reply can still be in flight from another tab, a
    # reload, or the headless `/chat` surface — and a reply that lands between
    # two spoken rows sits AFTER the cursor the next typed turn uses to find
    # "what the live session never heard", hiding the call's first half from
    # it. So the server refuses, in words. After the uniform 404 above: a
    # stranger's thread must not learn that it exists and is busy. Fail-open on
    # a Redis outage (`get_turn_inflight` → None) — a call over a possibly
    # running turn beats no call at all.
    from .service import get_turn_inflight
    if get_turn_inflight(portal_session_id):
        raise ClientPortalError(409, "A reply is still being written — wait for it, then start the call.")

    from config import WORKSPACE_VOICE_MAX_DURATION
    from database import db as core_db
    from services.gemini_voice import WORKSPACE_PANEL_INSTRUCTIONS, canvas_context_section, voice_service
    from services.voice_prompt_service import get_voice_system_prompt

    prompt = await get_voice_system_prompt(agent_name)
    context = build_portal_context_summary(agent_name, email, portal_session_id)
    if context:
        prompt += f"\n\n## Conversation so far:\n{context}"
    prompt += WORKSPACE_PANEL_INSTRUCTIONS
    # ent#551 QA: the model is told what the person is looking at — it said
    # "I don't have a current table on the canvas" with the table on screen.
    prompt += canvas_context_section(agent_name)

    session = await voice_service.create_session(
        agent_name=agent_name,
        chat_session_id=None,
        user_id=user_id,
        user_email=user_label,
        system_prompt=prompt,
        voice_name=voice_name or core_db.get_voice_name(agent_name),
        workspace_mode=True,
        max_duration=WORKSPACE_VOICE_MAX_DURATION,
        portal_session_id=portal_session_id,
        client_email=email,
        # An internal user's call is an operator surface, like Agent Detail's.
        # With the Workspace reading every audience for platform principals
        # (agent_page.canvases), `roster` would only ADD exposure: a call that
        # creates `main` would publish its drawings to every external client on
        # the roster. Never widens (ent#536).
        canvas_audience="operator",
        # ent#535 review — this function is the gate that authorizes the wider
        # roster read (`is_platform` is refused above), so it is the place that
        # records it. The turn path reads it off the session instead of
        # re-asserting `include_owned=True` a long way from here.
        is_platform=True,
    )
    # #2700: the thread is deliberately NOT marked here. Every path able to
    # clear the live-call marker lives downstream of an audio socket that may
    # never open, so arming it at the start stranded a cap-sized 409 on every
    # typed turn in the thread whenever the socket failed to connect. The
    # bridge arms its own lease at connect and releases it in the same
    # `finally` (`routers/voice.py::voice_websocket`) — one owner, one closer.
    return {
        "voice_session_id": session.session_id,
        "websocket_url": f"/ws/voice/{session.session_id}",
        "portal_session_id": portal_session_id,
        "max_duration_seconds": WORKSPACE_VOICE_MAX_DURATION,
    }


# ---- Write-as-you-go persistence ---------------------------------------------

def _next_stamp(session) -> str:
    """A strictly increasing ISO-Z stamp for this session's rows.

    `get_portal_messages` orders by `created_at` alone, and two rows of one turn
    (the user's line and the reply) are reported in the same tick, so equal
    stamps would interleave them arbitrarily on read. Microsecond precision
    makes a tie unlikely; this makes it impossible.
    """
    now = datetime.now(timezone.utc)
    last = getattr(session, "_last_persist_at", None)
    if last is not None and now <= last:
        now = last + timedelta(microseconds=1)
    session._last_persist_at = now
    return now.strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def persist_voice_turn(session, role: str, text: str) -> bool:
    """Insert one spoken turn into the call's Workspace thread. Best-effort.

    Called by the WebSocket bridge on the worker that holds the live session,
    for every completed turn. Secrets staged on the platform are scrubbed
    before the text lands (the ent#279 rule for every free-text sink).
    """
    portal_session_id = getattr(session, "portal_session_id", None)
    if not portal_session_id or not text or not text.strip():
        return False
    try:
        from services.runtime_secret_scrub import get_staged_values, scrub_text
        content = scrub_text(get_staged_values(), text.strip())
        at = _next_stamp(session)
        db.add_portal_message(
            uuid.uuid4().hex, session.agent_name, session.client_email, role, content,
            None, at, session_id=portal_session_id,
            source=VOICE_SOURCE, voice_call_id=session.session_id,
        )
        session._turns_saved = getattr(session, "_turns_saved", 0) + 1
        if role == "user" and not getattr(session, "_first_spoken_line", None):
            session._first_spoken_line = content
        return True
    except Exception as e:  # noqa: BLE001 — a bookkeeping failure must not end the call
        logger.warning("workspace voice: turn persist failed for %s: %s", session.session_id, e)
        return False


def call_label(duration_seconds: float, end_reason: Optional[str] = None,
               max_duration: Optional[int] = None, end_message: Optional[str] = None) -> str:
    """The one line the chat keeps about the call: `Voice call · N min`, plus
    how it ended when it did not end by choice."""
    minutes = max(1, int(round((duration_seconds or 0) / 60)))
    label = f"Voice call · {minutes} min"
    if end_reason == "cap":
        cap_min = max(1, int(round((max_duration or duration_seconds or 0) / 60)))
        label += f" · ended at the {cap_min}-minute limit"
    elif end_reason in ("error", "provider_closed"):
        label += f" · ended early: {end_message or 'the voice connection was lost'}"
    return label


def persist_voice_call_end(session, duration_seconds: float, end_reason: Optional[str] = None,
                           end_message: Optional[str] = None) -> int:
    """Close the call in the thread: one `system` row with the label, and the
    session bookkeeping (`touch_portal_session`) every message writer pairs with
    its rows (the ent#457 rule). Returns the number of rows this call wrote.

    A call in which nothing was said writes NOTHING — no row, no count bump —
    so a mic that never worked leaves no "Voice call · 0 min" tombstone behind.
    """
    portal_session_id = getattr(session, "portal_session_id", None)
    turns = getattr(session, "_turns_saved", 0)
    if not portal_session_id or turns == 0:
        return 0
    if getattr(session, "_call_closed", False) is True:
        return turns + 1            # idempotent: the same answer the first close gave
    session._call_closed = True
    try:
        from .service import _derive_title
        label = call_label(duration_seconds, end_reason,
                           getattr(session, "max_duration", None), end_message)
        at = _next_stamp(session)
        db.add_portal_message(
            uuid.uuid4().hex, session.agent_name, session.client_email, "system", label,
            None, at, session_id=portal_session_id,
            source=VOICE_SOURCE, voice_call_id=session.session_id,
        )
        first = getattr(session, "_first_spoken_line", None)
        db.touch_portal_session(
            portal_session_id, utc_now_iso(), added=turns + 1,
            title_if_empty=_derive_title(first) if first else None,
        )
        return turns + 1
    except Exception as e:  # noqa: BLE001
        logger.warning("workspace voice: call-end persist failed for %s: %s", session.session_id, e)
        return turns
