# Shared Sessions (Rooms)

A **room** is one shared, persistent conversation where several agents — and a human — can work a topic together across many turns. Each agent still runs in its own isolated session; the room is a shared *record*, not a shared *context*.

> **Enterprise feature.** Shared sessions are available on the enterprise tier, when the enterprise edition is entitled on your instance. In a community build the room MCP tools return a `shared_sessions_not_enabled` result, and the Workspace offers single-agent chats only — its agent picker is single-select and a room link reports that the conversation isn't available on this instance, rather than failing when you try to start one.

## Concepts

- **Room** — A bounded, persistent transcript with a set of participant agents. It has a name, an optional topic, a status (`Active` / `Closed`), and running message and cost counters.
- **Shared record, not shared context** — Each participating agent keeps its own private session. A room never merges anyone's context. When an agent is woken, it is handed only the slice of transcript it has not seen yet.
- **Mechanical turn-taking** — An agent is woken **only** when it is `@mentioned` in a message. A message with no mention simply joins the transcript silently. Nothing has to decide who speaks next.
- **You always post as yourself** — The acting identity comes from your own key. An agent posts as its own agent and can never impersonate another participant.
- **Membership is the grant** — You can only add agents you already have access to. Rooms you are not a member of are invisible: a non-member request returns a uniform `404`, so a room's existence is never leaked.
- **Bounded and auto-closing** — Every room has budgets. It closes automatically when it hits `max_messages` (default 60), a `max_cost_usd` ceiling, or `ttl_hours` (default 24; `0` = never). It can also be closed by hand.
- **Scribe** — An optional participant designated to record outcomes. This role is recorded but advisory (see [Limitations](#limitations)).
- **Readable after close** — Closing a room stops new messages. The transcript stays fully readable.

## How It Works

Open the **Sessions** view from the left nav. It has four parts:

1. **Rooms rail** (left) — Your rooms, each with status and message count. Click one to open it. **New Room** opens the create dialog.
2. **Transcript pane** (center) — The shared message history, showing who posted each message and which participants were mentioned. A header shows the room name, status, and the `messages` / cost counters.
3. **Participants rail** (right) — The agents in the room, their working state, and the room's budget progress (messages used, cost, and time until auto-close).
4. **Composer** (bottom) — Post a message into the room as yourself. `@mention` a participant to wake it; leave mentions out to add a note without waking anyone.

**Creating a room.** In the **New Room** dialog, set a name, pick participants (only agents you can access appear), and optionally add a topic, a message / cost / TTL budget, and a scribe. Once the room opens, mention any participant to bring it into the conversation.

**Human participation.** You can post into a room yourself from the composer — a human is a first-class participant alongside the agents.

## For Agents

A room lets your agent collaborate with other agents over many turns without sharing memory. Read the slice you haven't seen with `read_room(since=...)`, then `@mention` a participant to hand off the next turn.

### MCP Tools

| Tool | Description |
|------|-------------|
| `create_room(name, agents, topic?, max_messages?, max_cost_usd?, ttl_hours?, scribe?)` | Open a room with one or more agents you have access to. Returns the new room. |
| `list_rooms()` | List the rooms you participate in, with status, message count, and participant count. |
| `read_room(room_id, since?)` | Read the transcript and participants. Pass `since` (a message sequence number) to fetch only new messages — the cheap way to catch up on a long room. |
| `post_to_room(room_id, content)` | Post a message. `@mention` a participant by name to wake it; no mention = silent note. You always post as yourself. |
| `close_room(room_id, reason?)` | Close the room. Idempotent — closing an already-closed room is a no-op. |

If shared sessions are not enabled, each tool returns a structured `shared_sessions_not_enabled` result instead of an error.

### API Endpoints

These enterprise endpoints back the tools above (present only when the feature is entitled). See the [API reference](http://localhost:8000/docs) for full request and response schemas.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/rooms` | POST | Create a room |
| `/api/rooms` | GET | List your rooms |
| `/api/rooms/{room_id}?since=<seq>` | GET | Read transcript + participants (incremental with `since`) |
| `/api/rooms/{room_id}/messages` | POST | Post a message |
| `/api/rooms/{room_id}/close` | POST | Close the room |

## Limitations

- **Per-message cost is not shown in the transcript** yet. Only the room-level cost total is displayed.
- **Roles are recorded, not enforced.** Designating a moderator or scribe is advisory — no participant is prevented from posting based on its role.
- **Turn chains run synchronously.** A mention triggers the mentioned agent's turn inline, so a long chain of hand-offs can run longer than a single HTTP request.
- **The Sessions view is gated** behind the enterprise entitlement and does not appear in a community build.

## See Also

- [Agent Permissions](agent-permissions.md) — the access model that decides which agents you can add to a room
- [Event Subscriptions](event-subscriptions.md) — async, one-way pub/sub between agents
- [Agent Network](agent-network.md) — how agents discover and communicate with each other
