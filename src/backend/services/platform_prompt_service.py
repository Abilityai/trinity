"""
Platform Prompt Service — Single source of truth for platform instructions.

Builds the system prompt that is injected into every Claude Code invocation
via --append-system-prompt. Replaces the old file-based CLAUDE.local.md injection.
"""
import logging
import re
from dataclasses import dataclass, replace
from datetime import datetime, timezone
from typing import List, Optional

import httpx

from database import db
from models import REPORT_PAYLOAD_MAX_BYTES
from services.prompt_tier import PromptTier, resolve_prompt_tier

logger = logging.getLogger(__name__)

# Max number of collaborators to render in the context block.
MAX_COLLABORATORS = 20
# Max chars for user-controlled strings before truncation (prompt-injection mitigation).
MAX_FIELD_LEN = 80
# Narrower caps for specific field types.
MAX_COLLAB_NAME_LEN = 60
MAX_TIMESTAMP_LEN = 40
MAX_PLATFORM_URL_LEN = 200

# Static platform instructions — moved from agent-side trinity.py
PLATFORM_INSTRUCTIONS = """# Trinity Platform Instructions

## Trinity Agent System

This agent is part of the Trinity Deep Agent Orchestration Platform.

### Agent Collaboration

You can collaborate with other agents using the Trinity MCP tools:

- `mcp__trinity__list_agents()` - See agents you can communicate with
- `mcp__trinity__chat_with_agent(agent_name, message)` - Delegate tasks to other agents

**Note**: You can only communicate with agents you have been granted permission to access.
Use `list_agents` to discover your available collaborators.

### Sharing Files with Users

When the user asks for a file (image, PDF, document, generated asset) or when your answer is best delivered as a file instead of inline text — but see **Publishing Reports** below first: rows-and-columns results belong in a report, which the user can already export to Excel or PDF, and which works even when file sharing is off:

1. Write the file to `/home/developer/public/` (NOT `/home/developer/` or any other path).
2. Call the `mcp__trinity__share_file` MCP tool with the relative filename.
3. Include the returned `url` in your reply as-is.

The platform returns a time-limited download URL that works across every channel (web, Slack, Telegram, WhatsApp, email). If the owner has not enabled file sharing for you, the tool returns `FEATURE_DISABLED` — ask the operator to turn it on in the agent's Sharing tab.

### Publishing Reports

Any result that is rows-and-columns, or that a human will re-read later — a table you just produced (10 rows or 10,000), findings from a scheduled run, batch summaries, KPI snapshots, numbers someone compares against next period — belongs in a **report**, not only in chat. If you are about to paste a table into chat, or to hand-write a CSV and share it as a file, publish a report instead: the user gets the same data with Excel and PDF export built in. Chat is read once; a report is persisted on your Reports tab and the fleet Reports view. Reports are one-way: when you need a *decision*, use the operator queue below instead.

```
mcp__trinity__report(
    report_type="recon.weekly_summary",    # namespaced, lower_snake
    title="Week 30: 14 leads, 3 qualified",
    payload={...},                         # max __REPORT_PAYLOAD_MAX__ serialized
    display_hint="table",                  # optional, see below
    audience_email="ada@client.com",       # optional, see below
    period_start="...", period_end="...")  # optional ISO-8601
```

Reports are operator-facing by default. When the work was done **for** a person the operator shared you with, pass `audience_email`: it also reaches their Workspace — their deliverables, and a card in the chat it came from. Address what a client asked for; leave it off for fleet telemetry. An address that cannot reach you is refused, never silently dropped.

Match the payload to the `display_hint` or it renders as raw JSON:

- `table` — `{"columns": ["Name","Status"], "rows": [["Acme","qualified"]]}` (a row may instead be an object keyed by column)
- `kpi` — `{"tiles": [{"label": "Leads", "value": 14, "unit": "new"}]}`
- `markdown` — `{"markdown": "## Findings\\n..."}`
- `timeline` — `{"events": [{"ts": "2026-07-27T09:00:00Z", "label": "Deal closed", "detail": "..."}]}`
- `json` — any shape, when none of the above fit

Aggregate before publishing: the 20 rows that matter, not 5,000 raw ones. Oversized payloads are rejected and reports are rate-limited.

Before filing a recurring report, read back what you already filed — `mcp__trinity__list_reports` (metadata; filter by `report_type`) then `mcp__trinity__get_report(report_id)` for a payload. That is how you continue a series instead of duplicating or contradicting last period's numbers.

### Operator Communication

You can ask your human operator for input — approvals, answers to questions, or alerts — through a file-based queue protocol.

**Queue File**: `~/.trinity/operator-queue.json`

The platform monitors this file and presents requests to the operator in the Operating Room UI. The operator's responses are written back to the same file.

#### The contract: fire-and-park, never block-and-wait

All operator communication is **asynchronous**. A human may answer in minutes or in days, so:

1. **Park** your request by appending an entry to the queue file.
2. **End your turn.** Never wait, poll, or sleep for a response inside the current turn — a turn that blocks on a human burns its whole timeout budget and delivers nothing.
3. **Process responses in a later turn.** At the start of each autonomous run (scheduled task, loop iteration), check the queue file for items with `status: "responded"`, act on them, then set their status to `"acknowledged"`.

The operator's answer reaches your queue file within seconds of them responding, but only a future turn can act on it. If nothing will wake you (you have no schedule or heartbeat), say so in the request itself — include resume instructions in the `question`, e.g. "after approving, re-trigger schedule X" or "send me a chat message with your decision".

#### Ask before irreversible actions

Before performing an action that cannot be undone or verified afterwards — payments or money movement, emails/messages sent through your own credentials, public posts, destructive deletions — park an `approval` request and end your turn if you are uncertain it should happen. Be especially careful when the task looks like a repeat of work you may have already done (check your own records and the queue file first). Do the reversible parts of the task now; gate only the irreversible step.

#### How to Use

**Write a request** by adding an entry to the `requests` array:

```json
{
  "$schema": "operator-queue-v1",
  "requests": [
    {
      "id": "approval-<execution_id>-deploy",
      "type": "approval",
      "status": "pending",
      "priority": "high",
      "title": "Short summary of what you need",
      "question": "Full description with context. Markdown supported.",
      "options": ["approve", "reject"],
      "context": { "relevant_key": "relevant_value" },
      "created_at": "2026-03-07T10:00:00Z",
      "expires_at": "2026-03-09T10:00:00Z"
    }
  ]
}
```

**Request IDs must be globally unique.** Derive the `id` from your current execution ID (see the Execution Context block), e.g. `approval-{execution_id}-{short-slug}`. Never use date-serial IDs like `req-20260307-001` — another agent choosing the same ID silently swallows your request. Re-using your own derived ID when the same task runs again is safe and intentional: it prevents duplicate requests.

**Request types:**
- `approval` — You need a yes/no or multi-choice decision. Provide `options` array. State the exact action and its parameters in `context` so the operator can verify what they are approving.
- `question` — You need freeform guidance. No `options` needed.
- `alert` — You're reporting a situation. No decision needed, just acknowledgement.

**Priority levels:** `critical`, `high`, `medium`, `low`

**Set `expires_at`** on requests that gate an action. If it passes without a response the platform marks the item `expired` — treat that as "not approved; do not proceed."

**Check for responses** at the start of a later turn: items with `status: "responded"` carry `response`, `responded_by`, and `responded_at` fields.

**After processing a response**, update the item's status to `"acknowledged"`.

**File hygiene**: Keep only `pending` and `responded` items plus up to 3 recent `acknowledged` items.

#### When to Use

This is entirely your judgment. Some situations where it may be appropriate:
- Actions with significant consequences (deployments, purchases, deletions)
- Ambiguous requirements where you need clarification
- Situations requiring domain knowledge you don't have
- Important alerts the operator should be aware of

### Repeating Work and Deferred Ticks

Each turn here is a **one-shot, headless process**: it exits when you stop writing, and anything you scheduled inside it dies with it. So "run a loop", "do this every N minutes" and "check back later" mean the Trinity primitives, which outlive the turn:

- `mcp__trinity__run_agent_loop(message, max_runs, ...)` — bounded, budgeted repetition. Visible in the Loops panel and stoppable from there.
- `mcp__trinity__set_reminder(message, delay_seconds=...)` — one-shot deferred tick that re-invokes you later as a real execution.

Both default to yourself; pass `agent_name` only to drive another agent you have permission for.

**Never** reach for the Claude Code `/loop` skill or the `ScheduleWakeup` tool. They belong to a persistent interactive harness that does not exist in this runtime: `ScheduleWakeup` **returns success** and then nothing ever fires, so telling the user "loop armed, next tick in 60s" after calling it is a false claim.

The platform denies the whole family for that reason — `ScheduleWakeup`, `Workflow`, `Monitor`, `TaskOutput`, `CronCreate`, `CronList`, `CronDelete`, `SendMessage`, `PushNotification` and `RemoteTrigger`. If one of those is missing, that is this rule and not a permissions problem.

### Nothing survives the end of your turn

Your turn is a process. When you stop writing it exits, and **anything still running is killed a few seconds later** — a backgrounded command, a `sleep`, a build, a download. There is no second turn, and no notification will ever reach you.

This matters because the tools say otherwise. Backgrounding a command answers *"You will be notified when it completes"*, and a command promoted to the background at its timeout says the same. **That sentence is false here** and cannot be changed from Trinity's side, so it is on you not to believe it. Treat every "you'll be notified" as "this will be killed".

So:

- Run work in the **foreground**, to completion, inside your execution timeout. A long command that finishes is worth more than a backgrounded one that does not.
- Never end a turn with work outstanding, and never report success for something you only started.
- If it genuinely cannot fit in one turn, split it: do a bounded piece now, persist what you have (a file, a report, a note), and arm `mcp__trinity__set_reminder` to continue — that fires a real new execution. Say plainly what is done and what is not.
- Checking on your own background job later is not available to you at all. Design around that rather than around a notification.

### Package Persistence

When installing system packages (apt-get, npm -g, etc.), add them to your setup script so they persist across container updates:

```bash
# Install package
sudo apt-get install -y ffmpeg

# Add to persistent setup script
mkdir -p ~/.trinity
echo "sudo apt-get install -y ffmpeg" >> ~/.trinity/setup.sh
```

This script runs automatically on container start. Always update it when installing system-level packages.

### Remembering Things About Users (Public & Channel Sessions)

When serving users through a public link, WhatsApp, Telegram, or Slack session, the user's memory is **isolated per person** — what you know about one user is never shown to another.

**Do NOT** write user-identifying information (names, emails, contact details, personal preferences) to the agent memory directory (`~/.claude/projects/memory/`). That location is **shared across all users** of this agent — writing personal data there leaks it to everyone.

**Instead**, use the `mcp__trinity__write_user_memory` tool to persist facts about this specific user:

```
mcp__trinity__write_user_memory(
    execution_id="<your execution_id from Execution Context>",
    memory_text="User's name is Alice. Prefers concise answers. Works in PST timezone."
)
```

The `execution_id` is in the **Execution Context** block below. The platform stores the memory text in an isolated, per-user store and injects it back at the start of every future session with this user.

- Write the complete updated memory blob each time (read → update → write).
- The current memory for this user (if any) appears in the **"What you know about this user"** block above.
- Only available during user-facing sessions (public link, Slack, Telegram, WhatsApp). The tool returns an error if called from a scheduled task or agent-to-agent call."""

# The payload ceiling is INTERPOLATED, never typed twice (#1838 review). The
# block shipped `256 KB` while `REPORT_PAYLOAD_MAX_BYTES` was already 5 MiB in
# the same PR — so every agent would have been told a ceiling 20x below the real
# one, and would pre-aggregate away exactly the payloads #1537 raised the cap to
# accept. A literal here is a second source of truth for a number the platform
# already owns; `test_1535_report_prompt_guidance.py` pins the substitution too.
PLATFORM_INSTRUCTIONS = PLATFORM_INSTRUCTIONS.replace(
    "__REPORT_PAYLOAD_MAX__", f"{REPORT_PAYLOAD_MAX_BYTES // (1024 * 1024)} MB"
)


# ---------------------------------------------------------------------------
# Model-conditional prompt tiers (ent#243)
# ---------------------------------------------------------------------------
#
# The prompt stays ONE authored literal above. Sections are *derived* from it by
# splitting on its top-level ``###`` headings, never re-typed into a parallel
# list — maintaining two full prompt strings guarantees drift, and the drift that
# matters is a security instruction added to one variant and not the other.
#
# The delimiter is ``\n\n###`` + space, which cannot match the ``####``
# subsections inside Operator Communication (they carry a fourth ``#`` where this
# pattern requires a space). ``split``/``join`` on the same delimiter round-trips
# byte-exactly, which is what makes the VERBOSE render provably identical to the
# pre-ent#243 constant.

_SECTION_DELIMITER = "\n\n### "

# Sections DROPPED at PromptTier.MINIMAL. Each is tool-usage guidance whose
# real home is the corresponding MCP tool description — the "single source of
# truth" rule. Dropping them is inert until _MINIMAL_PREFIXES is non-empty.
_MINIMAL_DROP_SECTIONS = frozenset({
    "Agent Collaboration",              # → list_agents / chat_with_agent descriptions
    "Sharing Files with Users",         # → share_file description
    "Publishing Reports",               # → report description (+ #1535 display_hint enum)
})

# Every top-level section, CI-pinned (tests/unit/test_ent243_prompt_tier.py).
# A renamed heading must fail loudly: an unmapped section falls back to
# always-render (see _iter_sections), which is safe but silent, and "silent" is
# how a section intended to be dropped quietly stops being dropped — or worse,
# how a rename makes a drop-list entry dead without anyone noticing.
_KNOWN_SECTION_HEADINGS = frozenset({
    "Agent Collaboration",
    "Sharing Files with Users",
    "Publishing Reports",
    "Operator Communication",
    "Repeating Work and Deferred Ticks",
    "Nothing survives the end of your turn",
    "Package Persistence",
    "Remembering Things About Users (Public & Channel Sessions)",
})

# Sections that render at EVERY tier, stated positively for the reader. These are
# NOT tool-usage guidance and have no tool description to live in:
#   * Operator Communication — the #1402 fire-and-park contract. Sentinel-locked
#     by tests/unit/test_1402_prompt_contract.py and synced to
#     config/trinity-meta-prompt/prompt.md; it documents a FILE protocol
#     (~/.trinity/operator-queue.json), not an MCP tool.
#   * Remembering Things About Users — carries the shared-memory-directory leak
#     warning. A privacy guard, un-gateable by construction.
#   * Package Persistence — a Trinity environment gotcha (~/.trinity/setup.sh)
#     that no model can infer from tool signatures.
#   * Nothing survives the end of your turn — #2468. The counterweight to a
#     tool result the platform cannot edit: backgrounding a command answers
#     "You will be notified when it completes", which is true interactively
#     and false in a one-shot `claude --print` run where the task is killed
#     seconds after the turn ends. No tool description can carry a correction
#     to a DIFFERENT tool's description, so this cannot be gated.
#   * Repeating Work and Deferred Ticks — #2454. Its load-bearing half is a
#     NEGATIVE rule about the harness's own `/loop` skill and `ScheduleWakeup`,
#     which no Trinity tool description can carry (they are not our tools). The
#     wrong path reports success and silently never fires, so dropping this at
#     MINIMAL would restore exactly the false-success the section exists to
#     stop.
# Derived, not hand-listed, so it cannot disagree with the drop set.
_ALWAYS_SECTIONS = _KNOWN_SECTION_HEADINGS - _MINIMAL_DROP_SECTIONS


def _iter_sections(text: str) -> list[tuple[str, str]]:
    """Split the platform instructions into ``(heading, chunk)`` pairs.

    The first chunk is the preamble (everything before the first ``###``); it
    carries the empty heading ``""`` and always renders. Each subsequent chunk is
    the raw split fragment — heading line included, WITHOUT the delimiter — so
    that ``_SECTION_DELIMITER.join(chunk for _, chunk in ...)`` reconstructs the
    input byte-for-byte.
    """
    chunks = text.split(_SECTION_DELIMITER)
    sections: list[tuple[str, str]] = [("", chunks[0])]
    for chunk in chunks[1:]:
        sections.append((chunk.split("\n", 1)[0].strip(), chunk))
    return sections


def render_platform_instructions(tier: PromptTier = PromptTier.VERBOSE) -> str:
    """Render PLATFORM_INSTRUCTIONS for a prompt tier (ent#243).

    ``VERBOSE`` returns the constant unchanged — byte-identical, not merely
    equivalent. An unknown/renamed heading renders (fail toward more instruction,
    matching prompt_tier's own unknown→VERBOSE invariant).
    """
    if tier is not PromptTier.MINIMAL:
        return PLATFORM_INSTRUCTIONS
    kept = [
        chunk
        for heading, chunk in _iter_sections(PLATFORM_INSTRUCTIONS)
        if heading not in _MINIMAL_DROP_SECTIONS
    ]
    return _SECTION_DELIMITER.join(kept)


# ---------------------------------------------------------------------------
# Runtime-aware MCP tool naming (#1187 F-MCP)
# ---------------------------------------------------------------------------

# Claude Code exposes Trinity MCP tools as ``mcp__trinity__<tool>``; the
# PLATFORM_INSTRUCTIONS above document them that way. Codex auto-discovers MCP
# tools from the configured ``trinity`` server and invokes them by their bare
# names, so the Claude-only prefix must be stripped for Codex agents — otherwise
# the model emits ``mcp__trinity`` and Codex answers "unknown MCP server".
# Mirrors runtime_adapter._CODEX_RUNTIMES (the only non-Claude-named surface in
# the MVP); Gemini and unknown runtimes keep the canonical Claude naming.
_CODEX_RUNTIMES = frozenset({"codex"})

# Prepended to the Codex variant. Intentionally avoids the literal
# ``mcp__trinity__`` token so the stripped prompt contains it nowhere.
_CODEX_MCP_ORIENTATION = (
    "## MCP Tools (Codex runtime)\n\n"
    "A Trinity MCP server named `trinity` is configured for you. Call its tools "
    "by the bare names documented below — `list_agents`, `chat_with_agent`, "
    "`share_file`, `report`, `list_reports`, `get_report`, `write_user_memory` — "
    "exactly as your client "
    "auto-discovers them. Do not add any vendor-specific tool-name prefix."
    "\n\n---\n\n"
)


def _adapt_instructions_for_runtime(instructions: str, runtime: str) -> str:
    """Rewrite the MCP-tool references in ``instructions`` for ``runtime``.

    Codex → strip the Claude-only ``mcp__trinity__`` prefix and prepend a short
    orientation note. Claude/Gemini/unknown → return the text unchanged (the
    plan's ``default claude-code`` behavior). Pure — never mutates the input.
    """
    if (runtime or "").lower() in _CODEX_RUNTIMES:
        return _CODEX_MCP_ORIENTATION + instructions.replace("mcp__trinity__", "")
    return instructions


def format_user_memory_block(memory_record: dict) -> Optional[str]:
    """Format a user-memory record into a system-prompt block for injection.

    ``memory_record`` is the dict returned by
    :py:meth:`Database.get_or_create_public_user_memory` — it carries two
    independently-written sections (``agent_notes`` from the
    write_user_memory MCP tool, and ``conversation_summary`` from the
    background summarizer; see #895).

    Both sections are rendered when present; empty sections are omitted.
    Returns ``None`` when both sections are empty so callers can skip the
    ``--append-system-prompt`` injection entirely.
    """
    if not isinstance(memory_record, dict):
        return None
    agent_notes = (memory_record.get("agent_notes") or "").strip()
    summary = (memory_record.get("conversation_summary") or "").strip()
    if not agent_notes and not summary:
        return None

    lines = ["## What you know about this user", ""]
    if agent_notes:
        lines.extend(["### Agent notes", "", agent_notes, ""])
    if summary:
        lines.extend(["### Conversation summary", "", summary, ""])
    lines.append("---")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Background user-memory summarization (#895)
# ---------------------------------------------------------------------------

_SUMMARIZATION_MODEL = "claude-haiku-4-5-20251001"

_SUMMARIZATION_PROMPT = """\
You are a memory system. Given this conversation, extract a concise bullet list of facts \
about the user that would be useful to remember for future conversations.
Be specific: name, preferences, goals, context. Max 300 words.

Existing memory:
{existing_memory}

New conversation:
{conversation}

Output the updated memory text only (bullet points, no headers)."""


async def summarize_user_memory_background(
    agent_name: str, user_email: str, session_id: str
) -> None:
    """Summarize recent conversation and update the ``conversation_summary`` section.

    Fire-and-forget — failures are logged but never surfaced to the user.
    Touches only ``conversation_summary`` so the deliberate agent_notes
    section (written by ``write_user_memory``) is never re-summarized away
    (#895).

    Shared by the web public-chat path and the channel-adapter path so both
    surfaces have the same persistent-memory behavior.
    """
    # Local imports to avoid an import cycle at module load:
    # platform_prompt_service is imported by routers that the settings
    # service may transitively pull in.
    from services.settings_service import get_anthropic_api_key

    try:
        api_key = get_anthropic_api_key()
        if not api_key:
            logger.warning(
                "[MemSummarize] No ANTHROPIC_API_KEY configured, skipping summarization"
            )
            return

        memory_record = db.get_or_create_public_user_memory(agent_name, user_email)
        existing_summary = memory_record.get("conversation_summary", "") or ""

        # #903 (F-MEM): a thread-scoped channel session can hold turns from
        # several users. Filter to the current user's own turns so Alice's
        # conversation never persists into Bob's durable, re-injected memory.
        # Single-participant paths (web + any channel DM: Slack/Telegram/
        # WhatsApp) stamp BOTH the user turn and the assistant reply with the
        # recipient's email, so the filter is a no-op there (full user+assistant
        # conversation, as before #903). Only a multi-participant session (Slack
        # channel thread or group chat) leaves the assistant turn null, so the
        # shared reply is excluded from any one participant's memory.
        messages = db.get_recent_public_chat_messages(
            session_id, limit=20, sender_email=user_email
        )
        if not messages:
            return

        conversation_lines = []
        for msg in messages:
            role_label = "User" if msg.role == "user" else "Assistant"
            conversation_lines.append(f"{role_label}: {msg.content}")
        conversation_text = "\n".join(conversation_lines)

        prompt = _SUMMARIZATION_PROMPT.format(
            existing_memory=existing_summary or "(none yet)",
            conversation=conversation_text,
        )

        async with httpx.AsyncClient(timeout=30.0) as client:
            response = await client.post(
                "https://api.anthropic.com/v1/messages",
                headers={
                    "x-api-key": api_key,
                    "anthropic-version": "2023-06-01",
                    "content-type": "application/json",
                },
                json={
                    "model": _SUMMARIZATION_MODEL,
                    "max_tokens": 512,
                    "messages": [{"role": "user", "content": prompt}],
                },
            )

        if response.status_code != 200:
            logger.error(
                f"[MemSummarize] Anthropic API error {response.status_code}: "
                f"{response.text[:200]}"
            )
            return

        data = response.json()
        new_summary = (data.get("content", [{}])[0].get("text", "") or "").strip()
        if new_summary:
            db.update_public_user_memory_conversation_summary(
                agent_name, user_email, new_summary
            )
            logger.info(
                f"[MemSummarize] Updated conversation_summary for {user_email} "
                f"on {agent_name} ({len(new_summary)} chars)"
            )

    except Exception as e:  # noqa: BLE001 — fire-and-forget background task
        logger.error(
            f"[MemSummarize] Failed to summarize memory for {user_email} "
            f"on {agent_name}: {e}"
        )


def get_platform_system_prompt(
    runtime: str = "claude-code", model: Optional[str] = None
) -> str:
    """
    Build the full platform system prompt.

    Combines static platform instructions with the operator's custom prompt
    from the trinity_prompt database setting.

    Args:
        runtime: the agent's execution runtime (``trinity.agent-runtime`` label).
            Codex gets MCP-tool references without the Claude-only
            ``mcp__trinity__`` prefix; Claude/Gemini/unknown keep the canonical
            naming (#1187 F-MCP).
        model: the model this turn will run on, when the caller knows it. Selects
            the prompt tier (ent#243) — a frontier coding model gets the MINIMAL
            render, everything else (including ``None``) gets VERBOSE. Orthogonal
            to ``runtime``: that decides tool *naming*, this decides how much
            prose. Inert until ``prompt_tier._MINIMAL_PREFIXES`` is non-empty.

    Returns:
        Combined system prompt string
    """
    instructions = render_platform_instructions(resolve_prompt_tier(model))
    parts = [_adapt_instructions_for_runtime(instructions, runtime)]

    # Append custom prompt from database setting (operator-configurable)
    custom_prompt = db.get_setting_value("trinity_prompt", default=None)
    if custom_prompt and custom_prompt.strip():
        parts.append(f"\n\n## Custom Instructions\n\n{custom_prompt.strip()}")
        logger.debug(f"Including custom trinity_prompt ({len(custom_prompt)} chars)")

    return "".join(parts)


# ---------------------------------------------------------------------------
# Execution Context (#171)
# ---------------------------------------------------------------------------

# Characters we strip from user-controlled strings before rendering them
# into the system prompt. Newlines and control chars enable the most
# obvious prompt-injection vectors (a crafted schedule name could otherwise
# inject its own markdown heading).
_CONTROL_CHAR_RE = re.compile(r"[\x00-\x1f\x7f]")


def _sanitize_field(value: Optional[str], max_len: int = MAX_FIELD_LEN) -> Optional[str]:
    """Sanitize a user-controlled string before embedding it in the system prompt.

    Strips control characters (including newlines and tabs), backticks, and
    markdown heading markers; truncates to max_len chars. Returns None for
    empty input so callers can omit the field entirely.
    """
    if value is None:
        return None
    cleaned = _CONTROL_CHAR_RE.sub(" ", str(value))
    cleaned = cleaned.replace("`", "'").replace("##", "#").replace("---", "-")
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    if len(cleaned) > max_len:
        cleaned = cleaned[: max_len - 1] + "…"
    return cleaned


@dataclass
class ExecutionContext:
    """Per-invocation execution metadata injected into the agent system prompt.

    All fields are optional; the renderer omits any field that is None or empty.
    The caller constructs this from whatever it knows — a chat handler won't
    have a timeout, a scheduled task won't have a source user, etc.
    """
    agent_name: Optional[str] = None
    mode: Optional[str] = None                          # "chat" | "task"
    triggered_by: Optional[str] = None                  # raw trigger label
    source_user_email: Optional[str] = None
    source_agent_name: Optional[str] = None
    source_mcp_key_name: Optional[str] = None
    model: Optional[str] = None
    timeout_seconds: Optional[int] = None
    attempt: Optional[int] = None
    schedule_name: Optional[str] = None
    schedule_cron: Optional[str] = None
    schedule_next_run: Optional[str] = None
    collaborators: Optional[List[str]] = None
    platform_url: Optional[str] = None
    timestamp: Optional[str] = None
    execution_id: Optional[str] = None                  # MEM-001: for write_user_memory tool

    @staticmethod
    def derive_mode(triggered_by: Optional[str]) -> str:
        """Map a triggered_by label to a behavioral mode.

        chat mode: user is waiting and can respond in a future turn
        task mode: headless execution, agent should not block on input
        """
        chat_triggers = {"chat", "user", "public", "paid"}
        if triggered_by and triggered_by.lower() in chat_triggers:
            return "chat"
        return "task"


def _render_triggered_by(ctx: ExecutionContext) -> Optional[str]:
    """Build the `Triggered by` line, enriched with source identity when known."""
    raw = _sanitize_field(ctx.triggered_by)
    if not raw:
        return None
    extras = []
    if ctx.source_agent_name:
        agent = _sanitize_field(ctx.source_agent_name)
        if agent:
            extras.append(f"source agent: '{agent}'")
    if ctx.source_mcp_key_name:
        key = _sanitize_field(ctx.source_mcp_key_name)
        if key:
            extras.append(f"mcp key: '{key}'")
    if ctx.source_user_email:
        email = _sanitize_field(ctx.source_user_email)
        if email:
            extras.append(f"user: '{email}'")
    if extras:
        return f"{raw} ({', '.join(extras)})"
    return raw


def _render_schedule_line(ctx: ExecutionContext) -> Optional[str]:
    """Build a compact schedule description line, or None if no schedule."""
    name = _sanitize_field(ctx.schedule_name)
    cron = _sanitize_field(ctx.schedule_cron)
    next_run = _sanitize_field(ctx.schedule_next_run, max_len=MAX_TIMESTAMP_LEN)
    if not (name or cron or next_run):
        return None
    parts = []
    if name:
        parts.append(f"'{name}'")
    meta = []
    if cron:
        meta.append(f"cron: {cron}")
    if next_run:
        meta.append(f"next: {next_run}")
    if meta:
        parts.append(f"({', '.join(meta)})")
    return " ".join(parts)


def _render_collaborators(ctx: ExecutionContext) -> Optional[str]:
    """Render the collaborators list, capped at MAX_COLLABORATORS."""
    if not ctx.collaborators:
        return None
    cleaned: List[str] = []
    for name in ctx.collaborators:
        safe = _sanitize_field(name, max_len=MAX_COLLAB_NAME_LEN)
        if safe:
            cleaned.append(safe)
    if not cleaned:
        return None
    if len(cleaned) > MAX_COLLABORATORS:
        shown = cleaned[:MAX_COLLABORATORS]
        return ", ".join(shown) + f", … ({len(cleaned) - MAX_COLLABORATORS} more)"
    return ", ".join(cleaned)


def _mode_guidance(mode: str) -> str:
    # The task-mode carve-out below is the #1402 async human-gate contract:
    # without it, "execute to completion — do not ask questions" directly
    # contradicts the Operator Communication instruction to park an approval
    # before an irreversible action. Sentinel phrase "fire-and-park" is
    # test-locked (tests/unit/test_1402_prompt_contract.py).
    if mode == "chat":
        return "Interactive session. You may ask clarifying questions if the request is ambiguous."
    return (
        "Autonomous execution. Do not ask clarifying questions — execute to completion "
        "and return your results. Plan your work to finish well within the timeout budget. "
        "One exception: an irreversible action that needs operator approval — park an "
        "approval request in the operator queue and end your turn (fire-and-park); "
        "never block the turn waiting for the response."
    )


def build_execution_context(ctx: ExecutionContext) -> str:
    """Render an ExecutionContext into a markdown block for the system prompt.

    Returns an empty string on failure so the caller can fall back to the
    base platform prompt without breaking the request.
    """
    try:
        mode = ctx.mode or ExecutionContext.derive_mode(ctx.triggered_by)
        mode = _sanitize_field(mode) or "task"

        lines: List[str] = [f"- **Mode**: {mode}"]

        triggered = _render_triggered_by(ctx)
        if triggered:
            lines.append(f"- **Triggered by**: {triggered}")

        schedule_line = _render_schedule_line(ctx)
        if schedule_line:
            lines.append(f"- **Schedule**: {schedule_line}")

        if ctx.attempt and ctx.attempt > 0:
            lines.append(f"- **Attempt**: {ctx.attempt}")

        model = _sanitize_field(ctx.model)
        if model:
            lines.append(f"- **Model**: {model}")

        if mode == "task" and ctx.timeout_seconds and ctx.timeout_seconds > 0:
            lines.append(
                f"- **Timeout**: {ctx.timeout_seconds}s — plan to finish well within this budget"
            )

        agent = _sanitize_field(ctx.agent_name)
        if agent:
            lines.append(f"- **Agent**: {agent}")

        if ctx.execution_id:
            lines.append(f"- **Execution ID**: {ctx.execution_id}")

        collaborators = _render_collaborators(ctx)
        if collaborators:
            lines.append(f"- **Collaborators**: {collaborators}")

        timestamp = _sanitize_field(
            ctx.timestamp, max_len=MAX_TIMESTAMP_LEN
        ) or datetime.now(timezone.utc).isoformat()
        lines.append(f"- **Timestamp**: {timestamp}")

        platform = _sanitize_field(ctx.platform_url, max_len=MAX_PLATFORM_URL_LEN)
        if platform:
            lines.append(f"- **Platform**: {platform}")

        guidance = _mode_guidance(mode)
        body = "\n".join(lines)
        return f"## Execution Context\n\n{body}\n\n{guidance}"
    except Exception as e:
        logger.warning(f"build_execution_context failed: {e}")
        return ""


def _resolve_collaborators(agent_name: Optional[str]) -> List[str]:
    """Look up permitted collaborator names for an agent. Empty list on failure."""
    if not agent_name:
        return []
    try:
        return db.get_permitted_agents(agent_name) or []
    except Exception as e:
        logger.debug(f"_resolve_collaborators({agent_name}) failed: {e}")
        return []


def _resolve_platform_url() -> Optional[str]:
    """Best-effort lookup of the platform's public URL."""
    try:
        value = db.get_setting_value("public_chat_url", default=None)
        if value and str(value).strip():
            return str(value).strip()
    except Exception as e:
        logger.debug(f"_resolve_platform_url failed: {e}")
    return None


def compose_system_prompt(
    execution_context: Optional[ExecutionContext] = None,
    caller_prompt: Optional[str] = None,
    *,
    include_execution_context: bool = True,
    runtime: str = "claude-code",
) -> str:
    """Compose the full system prompt: platform instructions + execution context + caller prompt.

    Single composition entry point. Keeps ordering and defaults in one place
    (invariant #15). Callers should use this instead of concatenating prompt
    fragments themselves.

    ``runtime`` is threaded to :func:`get_platform_system_prompt` so the MCP-tool
    naming matches the agent's harness (Codex vs. Claude/Gemini, #1187 F-MCP).

    ``execution_context.model`` selects the prompt tier (ent#243). It was already
    carried on the context for *rendering* (the Execution Context block); this
    also reads it for *selection*. A caller that does not know the model — the
    chat path, where the container picks one after composition — leaves it
    ``None`` and gets VERBOSE, which is the pre-ent#243 prompt.
    """
    parts: List[str] = [
        get_platform_system_prompt(
            runtime=runtime,
            model=execution_context.model if execution_context is not None else None,
        )
    ]

    if include_execution_context and execution_context is not None:
        # Auto-fill collaborators and platform URL without mutating the caller's
        # object — construct a shallow copy with the resolved fields filled in.
        ctx = execution_context
        if ctx.collaborators is None or ctx.platform_url is None:
            ctx = replace(
                ctx,
                collaborators=(
                    ctx.collaborators
                    if ctx.collaborators is not None
                    else _resolve_collaborators(ctx.agent_name)
                ),
                platform_url=(
                    ctx.platform_url
                    if ctx.platform_url is not None
                    else _resolve_platform_url()
                ),
            )
        block = build_execution_context(ctx)
        if block:
            parts.append(block)

    if caller_prompt and caller_prompt.strip():
        parts.append(caller_prompt.strip())

    return "\n\n".join(parts)


def build_public_channel_caller_prompt(
    agent_name: str, memory_system_prompt: Optional[str] = None
) -> Optional[str]:
    """Caller-prompt fragment for public/channel surfaces (#1205).

    Folds the per-agent public/channel custom instructions
    (``public_channel_system_prompt``) together with the MEM-001 per-user memory
    block into the single string public/channel callers pass as
    ``execute_task(system_prompt=...)`` → ``compose_system_prompt(caller_prompt=...)``.
    Public instructions come first (persona / guardrails / scope), then the
    per-user memory block.

    Strict no-op when the agent has no public prompt set: returns the memory
    block unchanged (or ``None``). Never raises — a lookup failure degrades to
    just the memory block so a chat is never blocked on this. Only the
    public-facing surfaces (channel router, public chat, paid chat) call this;
    authenticated chat, schedules, loops, and agent-to-agent calls do not, which
    is what keeps the fragment scoped to outside audiences.
    """
    try:
        public_prompt = db.get_public_channel_system_prompt(agent_name)
    except Exception as e:  # noqa: BLE001 - never block a chat on this lookup
        logger.warning(
            "public_channel_system_prompt fetch failed for %s: %s", agent_name, e
        )
        public_prompt = None
    parts = [p for p in (public_prompt, memory_system_prompt) if p and p.strip()]
    return "\n\n".join(parts) if parts else None


def build_voice_capability_prompt(agent_name: str, channel: str) -> Optional[str]:
    """Advertise the ``send_voice_reply`` capability (ent#117) — ONLY when voice is
    enabled for the agent AND allowed on ``channel`` AND platform TTS is configured,
    so the agent never attempts voice where it can't be delivered (FR-5).

    Returns an instruction fragment to fold into the channel caller prompt, or None.
    Never raises — a lookup failure degrades to no advertisement (the tool still
    self-gates server-side)."""
    try:
        import services.tts_service as tts_service
        if not tts_service.is_available():
            return None
        cfg = db.get_tts_config(agent_name)
        if not cfg.get("enabled"):
            return None
        if not cfg.get("channels", {}).get(channel, False):
            return None
    except Exception as e:  # noqa: BLE001 — never block a chat on this
        logger.warning("voice-capability prompt check failed for %s: %s", agent_name, e)
        return None
    return (
        "## Speaking (voice replies)\n"
        "You can reply with a spoken voice note on this channel using the "
        "`send_voice_reply` tool (pass your current execution_id). Your replies are "
        "TEXT by default — only use voice when a spoken reply genuinely fits (a short "
        "confirmation, greeting, or answer meant to be heard). Keep spoken text short. "
        "After sending a voice note, end your turn with `[NO_REPLY]` if you do NOT also "
        "want the same content sent as text. If voice can't be delivered the tool tells "
        "you and you should just reply with text."
    )


def build_narrated_surface_prompt(agent_name: str) -> Optional[str]:
    """Tell an agent that THIS surface reads its text aloud when the client asks
    it to (#2157) — the Workspace/Client-Portal counterpart to
    ``build_voice_capability_prompt``.

    The two describe deliberately different things, which is the whole point of
    keeping them apart:

    * ``build_voice_capability_prompt`` advertises a **tool the agent invokes**
      to deliver an audio artifact into a messaging channel.
    * this advertises a **client-controlled surface affordance** — the browser
      speaks the agent's text after it arrives. The agent cannot trigger it,
      cannot hear it, and produces no audio file.

    So the fragment must say the surface narrates WITHOUT implying the agent can
    send audio here (it cannot, and claiming so is the mirror-image bug). Without
    it an agent asked for a spoken reply reasons only from ``send_voice_reply``'s
    refusal and over-generalizes to "this surface is text-only — go to Slack",
    which is false and pushes a client off the surface built for them (#2157).

    Returns None (no advertisement) unless narration would actually work for this
    agent — same gate the speaker toggle renders on, so the fragment can never
    promise a control the client does not have. Never raises."""
    try:
        import services.tts_service as tts_service

        if not tts_service.resolve_voice_id(agent_name):
            return None
    except Exception as e:  # noqa: BLE001 — never block a chat on this
        logger.warning("narrated-surface prompt check failed for %s: %s", agent_name, e)
        return None
    return (
        "## Hearing you on this surface (client-controlled narration)\n"
        "You are talking to a client in the Trinity Workspace (web). This surface is "
        "NOT text-only: the client can switch on the speaker control in this "
        "conversation, and their browser then reads your text replies aloud as they "
        "arrive.\n"
        "- Narration is the CLIENT's switch, not yours. You cannot start, stop, or "
        "hear it, and it leaves no audio file in the conversation — so never claim to "
        "have sent, or offer to send, a voice message or recording here.\n"
        "- `send_voice_reply` delivers voice notes on messaging channels only "
        "(Telegram/Slack/WhatsApp) and will refuse here. That is a limit on YOU, not "
        "on this surface.\n"
        "- If a client asks to be spoken to, point them at the speaker control in this "
        "conversation. Never tell them this surface is text-only, and never send them "
        "to another channel to be heard."
    )


def is_execution_context_enabled() -> bool:
    """Operator kill-switch for the execution context block. Default: enabled."""
    try:
        value = db.get_setting_value(
            "trinity_execution_context_enabled", default="true"
        )
    except Exception:
        return True
    if value is None:
        return True
    return str(value).strip().lower() not in {"false", "0", "no", "off"}
