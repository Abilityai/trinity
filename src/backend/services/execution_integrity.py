"""Turn-integrity derivation from an execution's transcript (#2467).

When a ``claude --print`` turn ends with a background shell still in flight,
the CLI exits, kills the task ~5s later, and reports the kill in its own
stdout stream (``task_updated {"status": "killed"}`` +
``task_notification {"status": "stopped"}``). Those events reach the backend
verbatim inside the execution's ``execution_log`` (the agent server forwards
``ctx.raw_messages`` wholesale) — but until #2467 nothing structured ever read
them, so the row recorded a clean ``success`` whose response was the model's
announcement while the work was silently lost.

This module derives the missing structured record at TERMINAL-WRITE time,
backend-side, from the transcript the agent already sends. Deriving here
rather than in the agent image is deliberate (the #1741 ``extract_tool_calls``
precedent): it works on every deployed agent image with no base-image rebuild
and no cold recreate, and the agent-side stream handling stays byte-identical
(pinned by ``tests/unit/test_2467_bg_kill_agent_negative_controls.py``).

Why the KILL EVENTS and never the ledger: the CLI emits
``background_tasks_changed tasks: []`` BEFORE exiting, so any counter keyed on
ledger snapshots reads 0 at finalize no matter which task types it counts
(proven by the negative-control fixture in the test file above — widening
``_NON_WAITED_BG_TASK_TYPES`` does not and cannot surface this).

Privacy rule (#2127): only structural fields are ever persisted — task id,
task type, promotion origin, final status, end time. NEVER ``description`` /
``summary`` / ``command`` / ``output_file``, which carry model/prompt text and
filesystem paths. Ids and types are additionally charset-validated before they
enter the JSON column or the notice, so a forged stream line (an stdio MCP
child shares the pipe, #640) cannot smuggle free text into durable
operator-visible state (the canary G-04 class).

Failure direction: every malformed shape degrades to "no entry" — today's
behaviour — never to a false positive on a healthy run (AC #4/#6 of #2467).
"""
from __future__ import annotations

import json
import logging
import re
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# Bounded, structural-only persistence (see module docstring).
_MAX_KILLED_ENTRIES = 20
# Task ids / types are short CLI-minted tokens ("bg1", "local_bash"). Anything
# outside this shape is replaced with a fixed placeholder, never dropped — the
# flag must survive a mangled id, but the mangled id must not reach the column.
_TOKEN_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")

# task_updated ``patch.status`` vocabulary measured on Claude Code 2.1.235.
# Unknown values are logged once per process so the vocabulary is learned from
# production instead of guessed (the ``_NON_WAITED_BG_TASK_TYPES`` convention)
# — and NOT flagged: a status we cannot interpret is not evidence of a kill.
_KNOWN_TASK_STATUSES = frozenset({"killed", "completed"})
_warned_statuses: set = set()

# Waited-path pending count is agent-reported; bound a forged value.
_MAX_PENDING_COUNT = 1000

_KILLED_NOTICE_ISSUE_TAG = "(#2467)"


def _safe_token(value: Any, fallback: str) -> str:
    if isinstance(value, str) and _TOKEN_RE.match(value):
        return value
    return fallback


def collect_killed_bg_tasks(execution_log: Any) -> List[Dict[str, Any]]:
    """Scan a transcript for background tasks killed at CLI exit.

    Pure function over the parsed stream-json entries the agent server returns
    as ``execution_log``. A task is flagged when EITHER kill signal is seen —
    ``task_updated`` with ``patch.status == "killed"`` (primary), or
    ``task_notification`` with ``status == "stopped"`` (its constant companion
    in every capture; kept as a belt against a dropped ``task_updated`` line).
    The two dedupe by ``task_id``; ``killed`` wins as ``final_status``
    regardless of event order.

    ``was_backgrounded_by`` distinguishes the two incident shapes:
    ``tool_timeout`` when a mid-call ``task_updated`` patch carried
    ``is_backgrounded: true`` (the harness promoted a FOREGROUND command the
    model was counting on), else ``requested`` (the model passed
    ``run_in_background`` itself). ``task_type`` comes from the task's first
    ``background_tasks_changed`` snapshot; a kill for a task never seen in one
    (a dropped/unparseable snapshot line) is still flagged, with type
    ``unknown`` — a false negative is the bug itself, a missing type is not.
    """
    if not isinstance(execution_log, list):
        return []

    first_seen: Dict[str, Any] = {}
    promoted: set = set()
    killed: Dict[str, Dict[str, Any]] = {}  # insertion order = stream order

    for entry in execution_log:
        if not isinstance(entry, dict) or entry.get("type") != "system":
            continue
        subtype = entry.get("subtype")

        if subtype == "background_tasks_changed":
            tasks = entry.get("tasks")
            if isinstance(tasks, list):
                for task in tasks:
                    if isinstance(task, dict) and isinstance(task.get("task_id"), str):
                        first_seen.setdefault(task["task_id"], task.get("task_type"))

        elif subtype == "task_updated":
            task_id = entry.get("task_id")
            patch = entry.get("patch")
            if not isinstance(task_id, str) or not isinstance(patch, dict):
                continue
            if patch.get("is_backgrounded") is True:
                promoted.add(task_id)
            status = patch.get("status")
            if status == "killed":
                record = killed.setdefault(task_id, {})
                record["final_status"] = "killed"
                end_time = patch.get("end_time")
                if isinstance(end_time, (int, float)) and not isinstance(end_time, bool):
                    record["end_time"] = int(end_time)
            elif isinstance(status, str) and status not in _KNOWN_TASK_STATUSES:
                if status[:64] not in _warned_statuses:
                    _warned_statuses.add(status[:64])
                    logger.info(
                        "[ExecutionIntegrity] Unknown task_updated status %r — "
                        "not flagged; vocabulary learned from production (#2467)",
                        status[:64],
                    )

        elif subtype == "task_notification":
            task_id = entry.get("task_id")
            if isinstance(task_id, str) and entry.get("status") == "stopped":
                killed.setdefault(task_id, {}).setdefault("final_status", "stopped")

    entries: List[Dict[str, Any]] = []
    for task_id, record in killed.items():
        entries.append(
            {
                "task_id": _safe_token(task_id, "invalid"),
                "task_type": _safe_token(first_seen.get(task_id), "unknown"),
                "was_backgrounded_by": (
                    "tool_timeout" if task_id in promoted else "requested"
                ),
                "final_status": record.get("final_status", "stopped"),
                "end_time": record.get("end_time"),
            }
        )
        if len(entries) >= _MAX_KILLED_ENTRIES:
            break
    return entries


def killed_notice(entries: List[Dict[str, Any]]) -> Optional[str]:
    """Visible warning prepended to the stored response (the #2127 notice
    pattern). Interpolates ONLY validated tokens and our own enum values —
    never ids, never free text."""
    if not entries:
        return None
    types = sorted({e["task_type"] for e in entries})
    promo = ""
    if any(e["was_backgrounded_by"] == "tool_timeout" for e in entries):
        promo = (
            " At least one was a foreground command auto-promoted to the "
            "background at its tool timeout — work the model was waiting on."
        )
    return (
        f"> ⚠️ Background work lost: {len(entries)} background task(s) "
        f"(types: {', '.join(types)}) were still running when this turn ended "
        "and were killed at CLI exit. That work did not complete, and the "
        f"answer below may describe work that never happened.{promo} "
        f"{_KILLED_NOTICE_ISSUE_TAG}"
    )


def derive_turn_integrity(
    execution_log: Any, metadata: Any
) -> Tuple[Optional[str], Optional[str]]:
    """Build the ``turn_integrity`` column value + response notice for one
    terminal write.

    Returns ``(turn_integrity_json, notice)``. Both are ``None`` on a healthy
    run, so the caller writes nothing and the happy path stays byte-identical.
    The JSON object carries, when present:

    - ``background_tasks_killed`` — the structured kill records above.
    - ``background_tasks_pending_at_exit`` — the #2127 waited-path counter the
      agent already reports in ``metadata`` but which was persisted nowhere
      (issue #2467 root cause 3). Plucked here so the one queryable channel
      covers both background-task integrity flags; the agent-side notice for
      that path is untouched (it is already inside ``response``), which is why
      the notice returned here covers KILLED tasks only — a second pending
      notice would double up.

    NULL column ≡ "no evidence" (old transcript shapes, healthy run), never
    "verified healthy" — the ``clone_status`` convention.
    """
    entries = collect_killed_bg_tasks(execution_log)

    pending = 0
    if isinstance(metadata, dict):
        raw = metadata.get("background_tasks_pending_at_exit")
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            pending = min(raw, _MAX_PENDING_COUNT)

    integrity: Dict[str, Any] = {}
    if entries:
        integrity["background_tasks_killed"] = entries
    if pending:
        integrity["background_tasks_pending_at_exit"] = pending
    if not integrity:
        return None, None

    notice = killed_notice(entries)
    try:
        return json.dumps(integrity), notice
    except (TypeError, ValueError):  # unreachable with the shapes above; belt
        logger.warning(
            "[ExecutionIntegrity] turn_integrity serialization failed — "
            "flag dropped, notice kept (#2467)"
        )
        return None, notice
