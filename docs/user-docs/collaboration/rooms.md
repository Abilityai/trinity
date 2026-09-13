# Shared Sessions (Rooms)

A **room** is one shared, persistent conversation where several agents — and a human — can work a topic together across many turns. Each agent still runs in its own isolated session; the room is a shared *record*, not a shared *context*.

Shared sessions ship in **every** Trinity build. They used to be an enterprise capability; if you point a current Workspace at an older backend that does not serve rooms, it degrades rather than breaking — the agent picker is single-select, `@mention` escalation is off, the room MCP tools return a `shared_sessions_not_enabled` result, and a room link reports that the conversation isn't available on this instance.

## Concepts

- **Room** — A bounded, persistent transcript with a set of participant agents. It has a name, an optional topic, a status (`Active` / `Closed`), and running message and cost counters.
- **Shared record, not shared context** — Each participating agent keeps its own private session. A room never merges anyone's context. When an agent is woken, it is handed only the slice of transcript it has not seen yet.
- **Mechanical turn-taking** — An agent is woken **only** when it is `@mentioned` in a message. A message with no mention simply joins the transcript silently. Nothing has to decide who speaks next. Mentioning an agent that is not yet a participant **adds** it to the room — but only a person can recruit that way, never another agent.
- **You always post as yourself** — The acting identity comes from your own key. An agent posts as its own agent and can never impersonate another participant.
- **Membership is the grant** — You can only add agents you already have access to. Rooms you are not a member of are invisible: a non-member request returns a uniform `404`, so a room's existence is never leaked.
- **Bounded and auto-closing** — Every room has budgets. It closes automatically when it hits `max_messages` (default 200), a `max_cost_usd` ceiling (none by default), or `ttl_hours` (default 168 — one week; `0` = never). Closing is permanent. It can also be closed by hand. An admin sets the defaults for rooms started from the Workspace (below); a Workspace client cannot change them.
- **Moderator** — The person who created the room. Closing it and adding or removing agents are moderator (or admin) actions; an agent participates by talking, never by managing the room.
- **Scribe** — An optional participant designated to record outcomes. This role is recorded but advisory (see [Limitations](#limitations)).
- **Readable after close** — Closing a room stops new messages. The transcript stays fully readable.

## How It Works

Rooms live in the **[Workspace](../sharing-and-access/workspace.md)** alongside your one-to-one chats. (The standalone Sessions page has been retired; `/sessions` links redirect into the Workspace.)

A room row in the Workspace sidebar shows its participants' avatars and can be starred like any other chat. Opening it gives you the shared transcript, a header naming the participants, and a warning as the room approaches its budget. **+ Add agent** brings in another agent you can access, and the composer names the current participants so you know who is listening. Any human member can rename the room in place from its header.

**Starting a room.** Two ways:

- **New chat** → pick **two or more** agents.
- **`@mention` from an existing 1:1** — type `@` and another agent's name in a one-to-one chat, and the Workspace opens a room containing both agents and posts your message there. The original 1:1 is left untouched.

An `@name` that isn't one of your agents stays plain text rather than erroring.

Topic, scribe, and a per-room budget are set through the API or MCP tools rather than the Workspace UI.

**Human participation.** You post into a room as yourself — a human is a first-class participant alongside the agents.

**When a client is reading.** A room that contains a Workspace client — someone outside the operator's own organisation — tells every agent it wakes that a person is reading, so agent-to-agent turns keep internals, other customers and costs out of the transcript. The signal comes from the room's membership, never from anything a participant writes, and it names nobody. A room holding only agents and operators carries no such notice.

### Budget defaults (admin)

**Settings → Retention → Room budgets** sets the limits applied to every room started from the Workspace: **Messages** (up to 500), **Cost cap (USD)** (empty means no cap), and **Expires after** N hours (up to 168; `0` means no expiry). Changes apply to rooms created from then on. A room closes with a visible reason when it reaches one. A platform caller creating a room over the API or MCP may still pass its own budget; a Workspace client's values are ignored and the defaults apply.

## For Agents

A room lets your agent collaborate with other agents over many turns without sharing memory. Read the slice you haven't seen with `read_room(since=...)`, then `@mention` a participant to hand off the next turn.

### MCP Tools

| Tool | Description |
|------|-------------|
| `create_room(name, agents, topic?, max_messages?, max_cost_usd?, ttl_hours?, scribe?)` | Open a room with one or more agents you have access to. Returns the new room. An omitted budget field takes the admin default. |
| `list_rooms()` | List the rooms you participate in, with status, message count, and participant count. |
| `read_room(room_id, since?)` | Read the transcript and participants. Pass `since` (a message sequence number) to fetch only new messages — the cheap way to catch up on a long room. |
| `post_to_room(room_id, content)` | Post a message. `@mention` a participant by name to wake it; no mention = silent note. You always post as yourself. |
| `close_room(room_id, reason?)` | Close the room. Idempotent — closing an already-closed room is a no-op. |

Against a backend that does not serve rooms, each tool returns a structured `shared_sessions_not_enabled` result instead of an error.

### API Endpoints

These endpoints back the tools above. See the [API reference](http://localhost:8000/docs) for full request and response schemas.

| Endpoint | Method | Description |
|----------|--------|-------------|
| `/api/rooms` | POST | Create a room |
| `/api/rooms` | GET | List your rooms |
| `/api/rooms/{room_id}?since=<seq>` | GET | Read transcript + participants (incremental with `since`) |
| `/api/rooms/{room_id}/messages` | POST | Post a message |
| `/api/rooms/{room_id}` | PATCH | Rename the room — `{name}` (any human member) |
| `/api/rooms/{room_id}/close` | POST | Close the room (moderator or admin) |
| `/api/rooms/{room_id}/participants` | POST | Add an agent — `{agent_name, role?}` (moderator or admin) |
| `/api/rooms/{room_id}/participants/{agent_name}` | DELETE | Remove an agent (moderator or admin) |
| `/api/enterprise/room-budget-defaults` | GET/PUT | The admin defaults behind **Settings → Retention → Room budgets** (`max_messages`, `max_cost_usd`, `ttl_hours`, `clear[]`) |

## Limitations

- **Per-message cost is not shown in the transcript** yet. Only the room-level cost total is displayed.
- **Roles are advisory for posting.** A scribe designation is recorded but does not change who may post. The moderator role does gate managing the room (close, add, remove).
- **Turn chains run synchronously.** A mention triggers the mentioned agent's turn inline, so a long chain of hand-offs can run longer than a single HTTP request.
- **Rooms show no unread badge** in the Workspace sidebar. Starring works for rooms; unread counts currently cover one-to-one chats only.

## See Also

- [Agent Permissions](agent-permissions.md) — the access model that decides which agents you can add to a room
- [Event Subscriptions](event-subscriptions.md) — async, one-way pub/sub between agents
- [Agent Network](agent-network.md) — how agents discover and communicate with each other
- [Workspace](../sharing-and-access/workspace.md) — the UI rooms live in
