"""Typed parameter objects for the widest database writers (#1482).

`update_execution_status`, `create_task_execution`, `create_schedule_execution`
and `add_chat_message` each took 10–20 parameters, declared twice (the
`database.py` facade and the db module) and — for `update_execution_status` —
passed between the two POSITIONALLY, so a reorder in either signature would
have written one field into another column with nothing failing. Each now takes
one object from here.

Persistence models, not API models: they live in the db layer, out of
Invariant #14's scope, like `db_models.py`.

Every class is `frozen` (a caller cannot mutate a shared instance after handing
it over) and `kw_only` (a positional value is exactly the bug class removed).
Field names are the column names, so a field maps to its column with no
renaming step to drift.
"""
from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional


@dataclass(frozen=True, kw_only=True)
class ExecutionSource:
    """Who or what started an execution — the `source_*` columns, plus the
    model and the subscription active when the row was recorded (SUB-004)."""
    source_user_id: Optional[int] = None
    source_user_email: Optional[str] = None
    source_agent_name: Optional[str] = None
    source_mcp_key_id: Optional[str] = None
    source_mcp_key_name: Optional[str] = None
    model_used: Optional[str] = None
    subscription_id: Optional[str] = None


@dataclass(frozen=True, kw_only=True)
class TaskExecutionFields(ExecutionSource):
    """Everything optional on a manual/API-triggered execution row
    (`create_task_execution`). See that method's docstring for each field."""
    fan_out_id: Optional[str] = None
    fan_out_task_id: Optional[str] = None
    loop_id: Optional[str] = None
    source_channel: Optional[str] = None
    source_channel_chat_id: Optional[str] = None
    source_channel_thread: Optional[str] = None
    source_channel_agent: Optional[str] = None
    source_channel_client: Optional[str] = None
    open_canvas_id: Optional[str] = None
    chain_depth: Optional[int] = None


@dataclass(frozen=True, kw_only=True)
class ExecutionResult:
    """What a terminal write records (`update_execution_status`).

    Every field is written as given — `None` NULLs the column — EXCEPT
    `retry_count` (#678) and `turn_integrity` (#2467): for those two, `None`
    means "leave the column as it is", so a writer that does not derive them
    never zeroes them; and EXCEPT `completed_at` (#3589): `None` means "the
    writer stamps now" (every caller that finalizes as the turn ends), and a
    value is the instant the PRODUCER says the turn ended — the #2944 watchdog
    claim passes the agent's `retained_at`, minutes before the write. The
    writer normalizes it (`utils.helpers.terminal_completed_at`: naive → UTC,
    never in the future) and measures `duration_ms` to it. The lease
    `claim_token` is deliberately NOT here: it is a CAS precondition on the
    write, not a value the write records.
    """
    response: Optional[str] = None
    error: Optional[str] = None
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    cost: Optional[float] = None
    tool_calls: Optional[str] = None
    execution_log: Optional[str] = None
    claude_session_id: Optional[str] = None
    compact_metadata: Optional[str] = None
    retry_count: Optional[int] = None
    turn_integrity: Optional[str] = None
    completed_at: Optional[datetime] = None


@dataclass(frozen=True, kw_only=True)
class ChatMessageFields:
    """Everything optional on a chat message row (`add_chat_message`)."""
    cost: Optional[float] = None
    context_used: Optional[int] = None
    context_max: Optional[int] = None
    tool_calls: Optional[str] = None
    execution_time_ms: Optional[int] = None
    source: Optional[str] = "text"
    subscription_id: Optional[str] = None
    output_tokens: Optional[int] = None
