# Continuous Conversations

Trinity has two conversation surfaces, and the difference is memory.

| Surface | Where | What the agent remembers |
|---------|-------|--------------------------|
| **Chat tab** on Agent Detail | `/agents/{name}?tab=chat` | Nothing. Each message starts fresh. |
| **[Workspace](../sharing-and-access/workspace.md)** | `/workspace` | Everything — tool results, mid-task state, reasoning — carried across turns. |

> **The Session tab is retired.** It was folded into the Chat tab as a mode, and that mode has now been removed in favour of the Workspace. Old `?tab=session` links redirect to `/workspace?agent=<name>`, and the Chat tab carries a **Continue in Workspace →** link. The underlying session API still exists (see [For Agents](#for-agents)); what went away is the second UI for it.

This page covers the behavior of a resuming conversation — what carries over, what compaction does to it, and the limits a long task can hit. For the Workspace UI itself, see [Workspace](../sharing-and-access/workspace.md).

## Concepts

**Resume** — Each turn reattaches to the same underlying agent session rather than replaying the transcript as text. That preserves strictly more than what was *said*: files the agent read, commands it ran, where it was in a multi-step skill, and its reasoning state.

**Cold turn** — A turn with no session to resume: the first message in a chat, the first turn after working memory is cleared, or a recovery turn after the underlying session file is gone. A cold turn re-sends the visible history as text so the agent still has the thread of the conversation.

**Auto-compact** — Claude Code's own mid-turn summarization of its history when it approaches the model's context limit.

## How It Works

### What resuming preserves

A resumed turn keeps the agent's working memory. A stateless turn keeps only the words. That is why long, multi-step work belongs in the Workspace: an agent that read three files in turn two still has them in turn six, instead of re-reading them.

Turns on one chat are **serialized**. Two simultaneous resumes of the same session could corrupt it, so a second message while one is in flight is refused rather than queued. Start another chat if you need parallel work against the same agent.

If the underlying session file has gone missing, the platform recovers automatically: it retries once as a cold turn, re-attaching the conversation history. You get an answer; the agent has the transcript but not its prior working state.

### Auto-compact

When Claude Code's internal history approaches roughly 85% of the model's context window, it:

1. Summarizes the history into a compact summary.
2. Replaces its in-memory history with that summary.
3. Continues the current turn.

The window is **model-specific**, not a flat 200K — a 1M-token model compacts at ~85% of 1M, a 200K model at ~85% of 200K.

What you'll notice: the turn takes a couple of minutes longer than expected, the visible message log is untouched, and working memory survives in compressed form. After several compacts in one conversation the summary loses fidelity and answers get vaguer — that's the point to start a fresh chat.

### The 50-turn agentic-loop cap

One turn can use up to 50 internal Claude agentic-loop iterations — read a file, edit it, run tests, retry — before failing with:

> Task exceeded turn limit: Reached maximum number of turns (50). Consider increasing max_turns_task in guardrails or breaking into smaller subtasks.

This is **not** the number of messages in your conversation. It is the per-turn iteration budget for a single request, and a heavy twelve-step task with retries can exhaust it.

To raise it for one agent:

```bash
TOKEN=$(curl -s --fail-with-body -X POST http://localhost:8000/api/token \
  -d "username=admin&password=$ADMIN_PASSWORD" \
  | python3 -c "import json,sys; print(json.load(sys.stdin).get('access_token') or '')")
# 403 = correct password, second factor required (no session). Use an MCP API
# key for automation — see api-reference/authentication.md.
[ -n "$TOKEN" ] || { echo "login issued no session" >&2; exit 1; }

curl -X PUT http://localhost:8000/api/agents/<agent-name>/guardrails \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"max_turns_task": 200}'
```

Default is 50, allowed range 1–500. Higher values reduce false failures on heavy tasks and lengthen the worst-case execution time.

### Clearing working memory

Starting a new chat gives you a fresh conversation and a fresh cost bucket. Clearing an existing conversation's working memory — keeping the visible log while the agent starts cold — drops the cached session and best-effort deletes the underlying session file in the container; the next turn is a cold turn.

Reach for a clean slate when the agent is going in circles, when you're switching topic and don't want bleed-over, or when repeated compaction has degraded its answers.

Conversations survive container restarts, cost tracking is cumulative across a conversation, and scope is per person: an agent's owner cannot read another user's conversations with it.

## Known limitations

| Limitation | Detail |
|---|---|
| **Runtimes without resume fall back to replay** | Codex agents have no resume primitive, so every turn replays the visible history as text. Continuity of *conversation* is preserved; working memory is not. |
| **Restore from backup forces one cold turn** | Platform backups cover the database (conversations and messages) but not the Docker volumes holding the agent's session files. After a restore, each conversation's first turn falls back to a cold turn. The visible log is preserved. |
| **Long turns survive a severed connection** | If the browser sleeps mid-turn, the turn keeps running server-side and the reply appears when the tab reconnects — no false failure. Very long turns may take a moment to reconcile. |
| **Recovered turns lose their metrics** | When a subprocess swallows the final result event, the platform recovers the reply but records no cost or duration for that turn. The answer is correct; the numbers are missing. |

## For Agents

The session API is unchanged and remains available; it simply has no dedicated UI any more. Workspace conversations run on the same engine.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/session` | POST | Create a new session row |
| `/api/agents/{name}/sessions` | GET | List sessions (caller-scoped) |
| `/api/agents/{name}/sessions/{id}` | GET | Get session with messages |
| `/api/agents/{name}/sessions/{id}/message` | POST | Send a turn (synchronous) |
| `/api/agents/{name}/sessions/{id}/reset` | POST | Clear the cached session so the next turn is cold |
| `/api/agents/{name}/sessions/{id}` | DELETE | Delete the session |
| `/api/agents/{name}/guardrails` | GET / PUT | Read or change `max_turns_task`, `max_turns_chat`, `execution_timeout_sec` |

All session endpoints return 404 when the `session_tab_enabled` feature flag is off. The Workspace does **not** consult that flag — it has its own chat surface.

## See Also

- [Workspace](../sharing-and-access/workspace.md) — the UI where continuous conversations live
- [Agent Chat](agent-chat.md) — the stateless Chat tab on Agent Detail
- [Agent Runtimes](agent-runtimes.md) — which runtimes support resume
- [Agent Configuration](agent-configuration.md)
