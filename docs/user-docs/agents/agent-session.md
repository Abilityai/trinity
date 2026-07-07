# Agent Session

**Session mode** is the default surface of the unified **Chat** tab in Agent Detail (toggle at the top-right; the former separate Session tab merged into Chat). Each new message reattaches to the same Claude Code session, so the agent retains tool-result memory, mid-skill state, and reasoning state across turns — strictly more capable than classic chat's stateless text-replay model. Old `?tab=session` links open the Chat tab in session mode.

> 📺 **Watch:** [The Multi-Agent Platform I Run My Company On](https://youtu.be/8j6q-kABRqc) *(May 2026)* · [all videos](../videos.md)

## Concepts

- **Session** -- A conversation thread with a Claude Code working memory attached. The visible message log is stored separately from the working memory and survives Reset.
- **Cached Claude session** -- A Claude Code session UUID stored on the session row. Each turn calls `claude --print --resume <uuid>` so working memory persists.
- **Auto-compact** -- Built-in Claude Code behaviour. When the underlying conversation reaches ~85% of the model's context window, Claude Code summarizes ~170k tokens of history into a ~10k summary mid-turn. Working memory survives in compressed form.
- **Reset memory** -- Clear the cached Claude session UUID. The visible message log stays; the agent starts the next turn cold.

### Reset memory vs. New Session

Both buttons sit in the Session toolbar. They look similar; they aren't:

| Action | Visible message log | Claude working memory | Cost tracking | When to use |
|--------|--------------------|----------------------|---------------|-------------|
| **Reset memory** | Kept | Cleared (next turn is cold) | Continues on the same session | You want a fresh line of thought but want to keep the conversation transcript with the agent |
| **+ New Session** | New (empty) | New (cold) | New (zero) | You're starting a different conversation entirely — different topic, different cost bucket |

## When to use Session mode vs classic chat

| Use Session mode for... | Use classic chat for... |
|---|---|
| Long, multi-turn reasoning or planning | One-shot questions |
| Multi-step skills where mid-skill state matters | Voice conversations |
| Cases where you want the agent to remember tool outputs across turns | Quick file-bearing questions where memory across turns isn't important |

The two surfaces share nothing — separate tables, separate sessions, separate cost tracking. Flipping the toggle does not transfer state.

Session mode is unavailable (the toggle hides and classic chat is used) when the platform `session_tab_enabled` flag is off or the agent runs a runtime without `--resume` support (Codex).

## How It Works

1. Open an agent's detail page and click the **Chat** tab; make sure **Session mode** is on (the default).
2. Click **+ New Session** or pick an existing session from the dropdown.
3. Type a message and press Enter. The agent processes the message; subsequent turns reattach to the same Claude Code session UUID.
4. Watch the session subtitle for `X% last cache` — the size of the most recent assistant turn's cache as a fraction of the model window.
5. To start a clean line of thought, click **Reset memory**.

### File Attachments

Same as Chat — paperclip button or drag-and-drop on the input.

**Supported types:** images (JPEG, PNG, GIF, WebP), plain text, CSV, JSON. Images are passed as vision content blocks. Text files are written to `/home/developer/uploads/` inside the agent container.

**Unsupported:** PDF, ZIP, archives, video, audio.

**Limits per message:**

| | |
|---|---|
| Max files | 3 |
| Max size per file | 5 MB |
| Max total image size | 10 MB |

### The "last cache" metric

The `X% last cache` value in the session subtitle is the size of the **most recent assistant turn's cache** (cache_read + cache_creation tokens) as a fraction of the model's context window.

It is **not** a session-wide memory pressure indicator. Claude Code auto-compacts mid-turn, which resets the cache to ~10k tokens — so this number bounces:

- Heavy turn that doesn't compact → climbs (e.g. 65%).
- Auto-compact mid-turn → next reading drops sharply (e.g. 28%).
- Light prompt-generation turn after a compact → small (e.g. 6%).

A high value tells you the last turn loaded a lot into Claude's cache, not that the session is "full."

### Auto-compact

When Claude Code's internal conversation history approaches ~85% of the model window (preTokens around 170k of 200k for Sonnet/Opus), it automatically:

1. Summarizes the conversation history into a ~10k-token summary.
2. Replaces the in-memory history with the summary.
3. Continues the current turn.

Effects users will notice:
- The turn appears to take longer than expected (~2 minutes added per compact).
- The `% last cache` reading drops sharply on the next turn.
- The visible message log is **untouched**. Working memory survives in compressed form (the summary).
- After several compacts in one session, response quality may diminish — the summary loses fidelity each time it's compressed further. Consider Reset memory and starting a fresh session.

### The 50-turn agentic-loop cap

Each Session-tab turn can use up to 50 internal Claude agentic-loop iterations (read file, edit file, run tests, retry, etc.) before failing with:

> Task exceeded turn limit: Reached maximum number of turns (50). Consider increasing max_turns_task in guardrails or breaking into smaller subtasks.

This is **not** the number of messages in your session. It's the per-turn iteration budget Claude Code uses internally for one user request. Heavy 12-step tasks with retries can blow past it.

To raise it on a per-agent basis:

```bash
TOKEN=$(curl -s -X POST http://localhost:8000/api/token \
  -d "username=admin&password=$ADMIN_PASSWORD" \
  | python3 -c "import json,sys; print(json.load(sys.stdin)['access_token'])")

curl -X PUT http://localhost:8000/api/agents/<agent-name>/guardrails \
  -H "Authorization: Bearer $TOKEN" \
  -H "Content-Type: application/json" \
  -d '{"max_turns_task": 200}'
```

Default is 50. Allowed range is 1-500. Higher values reduce false-positive failures on heavy tasks but lengthen the worst-case execution time.

### Reset memory

The Reset memory button:

1. Clears the session's `cached_claude_session_id`.
2. Best-effort deletes the Claude Code JSONL file in the agent container.
3. The visible message log in the database stays — you can see the prior conversation, but the agent will not.

The next turn becomes a "cold turn" — Claude Code creates a new session UUID and writes a fresh JSONL.

When to reset:
- You want to start a focused line of thought without the context of prior turns.
- The session has auto-compacted enough times that response quality has degraded.
- You're switching to a different topic and don't want bleed-over.

### Session Management

- Sessions persist across container restarts.
- Cost tracking is cumulative across the conversation.
- Per-user scope: owners cannot see other users' sessions on the same agent.
- Delete a session: removes both the message log and the session row. Best-effort deletes the underlying JSONL.

## Known limitations

| Limitation | Detail |
|---|---|
| **Voice mic not wired into Session mode** | Voice writes to the classic chat tables, not the Session tables. The mic is hidden in session mode. Switch Session mode off for voice. |
| **Agent restore from backup may force fresh sessions** | The platform backup covers SQLite (session rows + messages) but not the Docker volumes that hold Claude Code's JSONL files. After a DR restore, every session's first turn will trigger a memory-loss fallback (one cold turn under a new UUID). The visible message log is preserved. |
| **Long turns survive a severed browser connection** | The turn endpoint is synchronous; if the browser tab is suspended mid-turn (laptop sleep, tab freeze), the UI reconciles against server state when it wakes instead of showing a false "failed to send" — the assistant message appears once the turn lands. Very long turns may still take a moment to reconcile after the tab resumes. |
| **Stdout pipe race recovery is best-effort** | When a child subprocess inherits Claude Code's stdout, the final result event line can occasionally be lost. The system soft-recovers (treats accumulated assistant text as success), but cost and duration columns will be NULL for these recovered turns. The reply is correct; the metrics are missing. |

## For Agents

### API Endpoints

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/agents/{name}/session` | POST | Create a new session row |
| `/api/agents/{name}/sessions` | GET | List sessions (caller-scoped) |
| `/api/agents/{name}/sessions/{id}` | GET | Get session with messages |
| `/api/agents/{name}/sessions/{id}/message` | POST | Send a turn (the synchronous endpoint) |
| `/api/agents/{name}/sessions/{id}/reset` | POST | Clear cached Claude session UUID |
| `/api/agents/{name}/sessions/{id}` | DELETE | Delete the session |
| `/api/agents/{name}/guardrails` | GET / PUT | Read or change `max_turns_task`, `max_turns_chat`, `execution_timeout_sec` |

All Session endpoints return 404 when the `session_tab_enabled` feature flag is off.

## See Also

- [Agent Chat](agent-chat.md) — the classic (stateless) surface of the same tab
- [Agent Configuration](agent-configuration.md)
- [Creating Agents](creating-agents.md)
