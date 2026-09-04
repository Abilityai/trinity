"""ent#279 static parity guard — every terminal free-text writer scrubs.

**The scrub-chokepoint set is DISCOVERED, not derived.** Trinity has re-learned
that exact class three times — #45 (tool-call activities), #767 (CB probes), and
#1804 ("the emit set is not the close set"). A runtime-secret-scrub seam that is
wired at eight sites today and silently NOT wired at the ninth a future PR adds is
the same failure: the write succeeds, the row/log/snapshot is persisted, and the
staged value leaks into a durable sink with nothing red.

So, exactly like ``test_1804_terminal_activity_parity.py``, this guard anchors on
the WRITE — the moment agent-authored free text reaches a durable sink — and
requires the enclosing function to have called the scrub seam, or to be on an
explicit allowlist WITH a justification proving why the value it writes is not an
unscrubbed staged secret (user-typed input / static string / admission-path /
transitively scrubbed upstream by the appliers).

Anchors, all against ``services/ + routers/ + client_portal/``:

1. **Negative — the execution-terminal sink.** A function calling
   ``db.update_execution_status(...)`` with an agent-free-text keyword
   (``response`` / ``error`` / ``execution_log`` / ``tool_calls`` /
   ``execution_log_simplified``) — the ``schedule_executions`` sink the appliers
   own — must call a scrub function or be allowlisted. This catches the exact
   "a new terminal applier bypasses the appliers" bug the feature exists for.

2. **Negative — the message-content sinks.** A function calling
   ``db.add_chat_message`` / ``db.add_session_message`` /
   ``db.add_public_chat_message`` (``chat_messages`` / ``agent_session_messages``
   / ``public_chat_messages``) must scrub or be allowlisted. Today every such
   caller is either user-typed input, or writes the applier's already-scrubbed
   ``result.response`` (transitively covered), or is one of the two direct sinks
   that scrub (voice transcript, group broadcast) — but a NEW direct writer of
   raw agent text here would be a fresh leak.

3. **Positive — the direct agent-text sinks.** Four functions persist agent free
   text (or write it to the platform log) WITHOUT going through the appliers, and
   two of them do not write any of the anchored tables directly. They are pinned
   by name with NO allowlist escape: each MUST still call a scrub function, so
   removing the scrub turns CI red rather than silently re-opening the leak.

**Why agent_activities / agent_events / idempotency_keys are NOT anchored
(deliberate, per the plan).** The plan lists seven agent-text tables; this guard
anchors on ``schedule_executions`` + the three message tables, and NOT on the
activity / event / idempotency-snapshot writers, because:

  * Activity / event / idempotency-snapshot rows are written with the appliers'
    ALREADY-SCRUBBED output (``result.response`` / ``result.error`` /
    ``result.execution_log`` / ``raw_response``). Anchoring on e.g.
    ``create_activity`` or ``idempotency_service.complete`` broadly would flag
    hundreds of non-agent-text and transitively-covered call sites — a low-signal
    guard that rots into a giant allowlist.
  * ``emit_event`` payloads (``agent_events`` via the agent's own LLM call) are a
    DOCUMENTED residual, not a chokepoint (see the feature-flow + design docs);
    the live tool result is plaintext BY DESIGN (locked decision).

The transitive coverage is REAL and strong: the appliers are the only producer of
``TaskExecutionResult``, and they scrub at the source.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

_BACKEND = Path(__file__).resolve().parents[2] / "src" / "backend"

# The db-facade writer that persists agent free text into schedule_executions.
_TERMINAL_WRITER = "update_execution_status"

# The keyword arguments to that writer that carry agent-authored free text. A
# status-only write (``status='running'``) carries none and is not anchored.
_FREE_TEXT_KWARGS = {
    "response",
    "error",
    "execution_log",
    "tool_calls",
    "execution_log_simplified",
}

# db-facade writers that persist agent turn text into a durable message table.
_MESSAGE_WRITERS = {
    "add_chat_message",  # chat_messages
    "add_session_message",  # agent_session_messages
    "add_public_chat_message",  # public_chat_messages
}

# Any of these discharges the obligation (all imported bare from the seam).
_SCRUBBERS = {"get_staged_values", "scrub_text", "scrub_obj"}

_SCAN_DIRS = ("services", "routers", "client_portal")

# (module path relative to src/backend, enclosing function) -> why it is exempt.
# The bar is high: the value written is provably NOT an unscrubbed staged secret.
_ALLOWLIST = {
    # -- schedule_executions (update_execution_status) writers --------------
    (
        "services/task_execution_service.py",
        "execute_task",
    ): (
        "The single remaining direct update_execution_status(error=...) write here "
        "is the STATIC backend-shutdown string. #2314 moved the three admission "
        "fast-fail writes (capacity-full, dispatch-breaker, ephemeral-exhausted) "
        "into _admission_gate, allowlisted directly below on the same grounds. The "
        "agent-authored SUCCESS/FAILURE terminals are delegated to apply_result / "
        "_write_terminal_and_gate, which both scrub."
    ),
    (
        "services/task_execution_service.py",
        "_admission_gate",
    ): (
        "#2314 extracted this helper out of execute_task. Its three "
        "update_execution_status(error=...) writes are the SAME static "
        "admission-refusal strings that entry covered before the decomposition: "
        "capacity-full (an int from agent config), the dispatch-breaker literal, "
        "and ephemeral-exhausted (a platform-enumerated e.reason). The gate runs "
        "BEFORE any agent call, so no agent-authored text and no staged secret can "
        "exist on this path yet."
    ),
    (
        "services/chat_execution_service.py",
        "_cancel_inflight_if_parked",
    ): (
        "#2433 parked-queue cancel. The single update_execution_status(error=...) "
        "write is a STATIC literal ('Execution cancelled by user while queued...'); "
        "the execution never dispatched, so no agent-authored text exists to leak."
    ),
    (
        "services/chat_execution_service.py",
        "_finalize_http_failure",
    ): (
        "Writes error=error_msg, but error_msg is produced (and scrubbed) by "
        "_parse_agent_http_error in this same file BEFORE it is returned — pinned "
        "positively in _REQUIRED_SCRUBBERS. Scrubbing again here would be redundant."
    ),
    (
        "services/chat_execution_service.py",
        "_circuit_open_dispatch_error",
    ): (
        "Admission-path terminal (#526 fast-fail): the error is the static "
        "circuit-breaker message, no agent text. Mirror of routers.chat."
        "_raise_circuit_open_503."
    ),
    (
        "services/chat_execution_service.py",
        "_ephemeral_dispatch_error",
    ): (
        "Admission-path terminal: the error is the static ephemeral-exhausted "
        "message, no agent text. Mirror of routers.chat._raise_ephemeral_exhausted_410."
    ),
    (
        "services/chat_execution_service.py",
        "_acquire_task_capacity",
    ): (
        "Admission-path terminal: the CapacityFull branch fails the pre-created "
        "row with the static capacity-full message before any agent turn runs."
    ),
    (
        "services/chat_execution_service.py",
        "_proxy_terminate_and_finalize",
    ): (
        "Writes the static 'Execution terminated by user' CANCELLED error — an "
        "operator-terminate terminal, never agent-authored text."
    ),
    (
        "services/backlog_service.py",
        "drain_next",
    ): (
        "The error writes are backend-diagnostic strings ('Backlog drain failed: "
        "corrupt metadata (...)', 'Backlog drain spawn failed: {e}') where {e} is a "
        "JSONDecodeError / spawn Exception, not agent free text. The agent turn it "
        "drains is spawned via execute_task -> apply_result, which scrubs."
    ),
    (
        "routers/chat.py",
        "_raise_ephemeral_exhausted_410",
    ): (
        "Admission-path terminal: static ephemeral-exhausted string written before "
        "any agent turn is dispatched."
    ),
    (
        "routers/chat.py",
        "_raise_circuit_open_503",
    ): (
        "Admission-path terminal (#526 fast-fail): static circuit-breaker string, "
        "no agent text."
    ),
    (
        "routers/internal.py",
        "_execute_task_internal_background",
    ): (
        "Backstop error catch on the internal /task background path: a static "
        "backend-shutdown string and a f'Background execution failed: {e}' wrapper "
        "around a BACKEND exception. The agent-authored terminal already went "
        "through execute_task -> apply_result before this except runs."
    ),
    (
        "client_portal/service.py",
        "_fail_unstarted_execution",
    ): (
        "ent#286 pre-created streaming row failed with a static string when the "
        "background turn raised before execute_task saw its id. No agent text; if "
        "execute_task DID run, its own scrubbed terminal wins the CAS and this "
        "write is a no-op."
    ),
    # -- message-content writers (chat/session/public) ----------------------
    (
        "services/chat_execution_service.py",
        "prepare_chat_execution",
    ): (
        "Logs the USER's inbound turn (role='user', content=request.message) — "
        "user-typed input, never agent output, so there is no staged secret to "
        "scrub. The assistant reply is written by _finalize_chat_success, which "
        "scrubs."
    ),
    (
        "services/chat_execution_service.py",
        "finalize_self_task",
    ): (
        "Injects content=result.response — the applier's ALREADY-SCRUBBED output "
        "(apply_result scrubs envelope.response before building the result). "
        "Transitively covered."
    ),
    (
        "services/chat_persistence_service.py",
        "persist_chat_session",
    ): (
        "Persists the authenticated /task chat turn with content=result.response, "
        "the applier's already-scrubbed output. Transitively covered."
    ),
    (
        "services/proactive_message_service.py",
        "_persist_outbound",
    ): (
        "Persists the delivered proactive DM body, which send_message (same file) "
        "already scrubbed at the TOP before delivery AND persist — pinned "
        "positively in _REQUIRED_SCRUBBERS. Transitively covered."
    ),
    (
        "routers/public.py",
        "public_chat",
    ): (
        "Writes the USER's inbound message (role='user', content=chat_request."
        "message) and, on the assistant side, content=assistant_response derived "
        "from the applier result. User input needs no scrub; the assistant text is "
        "the applier's scrubbed output."
    ),
    (
        "routers/public.py",
        "_execute_public_chat_background",
    ): (
        "Writes content=result.response — the applier's already-scrubbed output. "
        "Transitively covered."
    ),
    (
        "routers/sessions.py",
        "send_session_message",
    ): (
        "Writes content=result.response / tool_calls=result.execution_log — the "
        "applier's already-scrubbed outputs (the plan's explicitly-noted transitive "
        "coverage). No direct wiring by design."
    ),
}

# Direct agent-free-text sinks pinned positively — each MUST call a scrub
# function (NO allowlist escape). Two do not write an anchored table directly, so
# only a positive check reaches them; the other two also pass the negative
# message-writer anchor, and are pinned here too so a scrub-removal-plus-allowlist
# edit can never quietly exempt them. Value = the sink it protects.
_REQUIRED_SCRUBBERS = {
    (
        "routers/voice.py",
        "_save_transcript",
    ): "voice transcript entries -> chat_messages.content",
    (
        "services/channel_history.py",
        "persist_outbound_group_message",
    ): "proactive group broadcast -> public_chat_messages.content",
    (
        "services/proactive_message_service.py",
        "send_message",
    ): "proactive DM body -> channel delivery + public_chat_messages (D19)",
    (
        "services/chat_execution_service.py",
        "_parse_agent_http_error",
    ): "agent HTTP error body -> platform ERROR log + schedule_executions.error",
}


def _call_names(node: ast.AST) -> set:
    """Names of every call in this subtree — ``obj.method()`` (Attribute) and bare
    ``helper()`` (Name). EXCLUDES nested function bodies (their own units)."""
    found = set()

    def walk(n: ast.AST, is_root: bool) -> None:
        for child in ast.iter_child_nodes(n):
            if (
                isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef))
                and not is_root
            ):
                continue
            if isinstance(child, ast.Call):
                if isinstance(child.func, ast.Attribute):
                    found.add(child.func.attr)
                elif isinstance(child.func, ast.Name):
                    found.add(child.func.id)
            walk(child, False)

    walk(node, True)
    return found


def _writes_free_text_terminal(fn: ast.AST) -> bool:
    """True when ``fn`` calls ``update_execution_status`` with an agent-free-text
    keyword — the moment the scrub obligation attaches. A status-only write does
    not qualify (no free text to scrub)."""
    for c in ast.walk(fn):
        if (
            isinstance(c, ast.Call)
            and isinstance(c.func, ast.Attribute)
            and c.func.attr == _TERMINAL_WRITER
            and {k.arg for k in c.keywords if k.arg} & _FREE_TEXT_KWARGS
        ):
            return True
    return False


def _iter_functions(tree: ast.AST):
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            yield node


def _negative_violations():
    out = []
    for directory in _SCAN_DIRS:
        for path in sorted((_BACKEND / directory).rglob("*.py")):
            rel = path.relative_to(_BACKEND).as_posix()
            tree = ast.parse(path.read_text())
            for fn in _iter_functions(tree):
                calls = _call_names(fn)
                writes_msg = bool(calls & _MESSAGE_WRITERS)
                if not (_writes_free_text_terminal(fn) or writes_msg):
                    continue
                if calls & _SCRUBBERS:
                    continue
                if (rel, fn.name) in _ALLOWLIST:
                    continue
                out.append((rel, fn.name, fn.lineno))
    return out


@pytest.mark.unit
class TestScrubParity:
    def test_every_free_text_writer_scrubs(self):
        """The core contract: a new function that persists agent free text to
        schedule_executions or a message table without scrubbing turns CI red."""
        found = _negative_violations()
        assert not found, (
            "ent#279: these functions persist agent-authored free text to "
            "schedule_executions (update_execution_status) or a message table "
            "(add_chat_message / add_session_message / add_public_chat_message) but "
            "never call the runtime_secret_scrub seam. Call get_staged_values() + "
            "scrub_text/scrub_obj on the RAW fields BEFORE the write, or add an "
            "_ALLOWLIST entry WITH a justification proving the value is not an "
            "unscrubbed staged secret:\n"
            + "\n".join(f"  {f}::{fn} (line {ln})" for f, fn, ln in found)
        )

    def test_direct_sinks_still_scrub(self):
        """The four direct agent-text sinks must each still call a scrub function
        — removing it must turn CI red, with no allowlist escape."""
        missing = []
        for (rel, fn_name), sink in _REQUIRED_SCRUBBERS.items():
            path = _BACKEND / rel
            tree = ast.parse(path.read_text())
            fns = {fn.name: fn for fn in _iter_functions(tree)}
            fn = fns.get(fn_name)
            if fn is None:
                missing.append(f"{rel}::{fn_name} (function missing) — {sink}")
            elif not (_call_names(fn) & _SCRUBBERS):
                missing.append(f"{rel}::{fn_name} no longer scrubs — {sink}")
        assert not missing, (
            "ent#279: a direct agent-text sink lost its scrub (or was renamed). "
            "Re-wire runtime_secret_scrub, or update _REQUIRED_SCRUBBERS if the "
            "sink genuinely moved:\n  " + "\n  ".join(missing)
        )

    def test_allowlist_entries_all_still_exist(self):
        """A stale allowlist entry exempts nothing — but it hides that the guard's
        coverage assumption drifted (a renamed/removed function)."""
        stale = []
        for rel, fn_name in _ALLOWLIST:
            path = _BACKEND / rel
            if not path.exists():
                stale.append(f"{rel}::{fn_name} (file missing)")
                continue
            names = {fn.name for fn in _iter_functions(ast.parse(path.read_text()))}
            if fn_name not in names:
                stale.append(f"{rel}::{fn_name} (function missing)")
        assert not stale, "Stale ent#279 parity allowlist entries: " + ", ".join(stale)

    def test_allowlist_entries_carry_a_justification(self):
        thin = [k for k, v in _ALLOWLIST.items() if len(v.strip()) < 60]
        assert not thin, f"ent#279 allowlist entries need a real justification: {thin}"

    def test_the_five_wired_appliers_are_seen_as_scrubbing(self):
        """Positive control: the five appliers we DID wire must be recognised as
        scrubbing by the same detector — otherwise the negative scan proves nothing
        (a detector that never sees a scrub call would pass vacuously)."""
        expected = {
            ("services/task_execution_service.py", "apply_result"),
            ("services/task_execution_service.py", "_write_terminal_and_gate"),
            ("services/chat_execution_service.py", "_finalize_chat_success"),
            ("services/chat_execution_service.py", "_finalize_budget_exhausted"),
            ("services/pull_coordination_service.py", "apply_task_result"),
        }
        seen = set()
        for rel, fn_name in expected:
            tree = ast.parse((_BACKEND / rel).read_text())
            for fn in _iter_functions(tree):
                if fn.name == fn_name and (_call_names(fn) & _SCRUBBERS):
                    seen.add((rel, fn_name))
        assert seen == expected, (
            "ent#279: a wired applier is no longer recognised as scrubbing "
            f"(detector broke or the scrub was removed): missing {expected - seen}"
        )

    def test_the_guard_actually_fires(self):
        """A guard nobody has seen fail is a guard nobody knows works."""
        src = (
            "class X:\n"
            "    async def apply(self):\n"
            "        db.update_execution_status(execution_id='e', status='failed',\n"
            "                                    error=agent_text)\n"
        )
        fn = next(_iter_functions(ast.parse(src)))
        assert _writes_free_text_terminal(fn)
        assert not (_call_names(fn) & _SCRUBBERS)

    def test_the_message_writer_anchor_fires(self):
        """The second negative anchor must also actually fire."""
        src = (
            "async def leak():\n"
            "    db.add_public_chat_message(sid, 'assistant', agent_text)\n"
        )
        fn = next(_iter_functions(ast.parse(src)))
        assert _call_names(fn) & _MESSAGE_WRITERS
        assert not (_call_names(fn) & _SCRUBBERS)
