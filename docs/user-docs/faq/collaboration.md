# Trinity FAQ — Agent Collaboration

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## How do agents talk to each other in Trinity?

Agents communicate through the Trinity MCP server, not by calling each other's containers directly. Every agent gets its own agent-scoped API key, injected automatically and wired into the agent's MCP configuration, so it can call tools like `chat_with_agent(agent_name, message)` to send a message to another agent and get the response back. Trinity detects the calling agent and records the interaction as a collaboration event on the Dashboard's activity **Timeline**, which you can replay over a chosen time range. See [Agent Network](../collaboration/agent-network.md).

## Why can't my agent call another agent?

By design. Trinity's permission model is restrictive by default: a new agent has zero agent-to-agent permissions, and the MCP server blocks any `chat_with_agent` call to a non-permitted target with an error before it reaches the other agent. You must grant each connection explicitly on the **Permissions** tab; only the system agent (`trinity-system`) bypasses permission checks. See [Agent Permissions](../collaboration/agent-permissions.md).

## How do I let one agent call another?

Open the agent's detail page and go to the **Permissions** tab, which lists every agent in the system with a toggle (plus **Allow All** / **Allow None** controls). Toggle on the agents you want to allow, keeping in mind that permissions are directional: allowing Agent A to call Agent B does not allow B to call A — each direction is a separate grant. The change takes effect on the next MCP call, with no restart needed. See [Agent Permissions](../collaboration/agent-permissions.md).

## What does granting permission actually let an agent do?

Permission grants communication, not control. A permitted agent can see the target in `list_agents`, send it messages via `chat_with_agent`, subscribe to its events, and mount its exposed shared folder — the same permission record gates all three collaboration surfaces. It does not let the calling agent manage the target (start, stop, or reconfigure it), and the reverse direction stays blocked until you grant it separately. See [Agent Permissions](../collaboration/agent-permissions.md).

## Can one agent hand off a long-running task to another without waiting?

Yes. Call `chat_with_agent(agent_name, message, async=true)`, which returns an `execution_id` immediately instead of holding the connection open, then poll `get_execution_result(execution_id)` until the task completes. This avoids the synchronous MCP call timeout and suits delegation chains where the worker may run for many minutes. See [Agent Network](../collaboration/agent-network.md).

## How do I share files between two agents?

Use shared folders, which are backed by Docker volumes. On the source agent, open the **Folders** tab and enable **Expose Shared Folder** — anything it writes to `/home/developer/shared-out` becomes available to permitted agents. Grant the consuming agent permission on the **Permissions** tab, then enable **Mount Shared Folders** on the consumer; the folder appears at `/home/developer/shared-in/{agent-name}`. Restart both agents to apply the volume mounts. See [Agent Network](../collaboration/agent-network.md).

## Why doesn't the shared folder show up in my consuming agent?

Volume mounts are applied when a container is created, so a restart of both agents is required after changing folder settings — enabling the toggles alone is not enough. Also check that the consumer has permission to the source agent, and that the source agent has actually been started with **Expose Shared Folder** enabled: if the source's shared volume doesn't exist yet, Trinity skips the mount until it does. See [Agent Network](../collaboration/agent-network.md).

## How do event subscriptions between agents work?

Events are a lightweight pub/sub layer. A source agent calls `emit_event(event_type, payload)` with a namespaced type like `report.generated`; Trinity finds every subscription matching that source agent and event type and dispatches an async task to each subscriber. The task's message comes from the subscription's template, with placeholders like `{{payload.field}}` filled in from the event payload — for example `Process report {{payload.url}}`. Events are persisted and broadcast over WebSocket for real-time visibility. See [Event Subscriptions](../collaboration/event-subscriptions.md).

## Why isn't my agent receiving events it subscribed to?

Three things to check. First, subscriptions are permission-gated: the subscribing agent must have permission to call the source agent, or the subscription won't fire. Second, the subscription must match both the exact source agent name and the exact event type the emitter uses. Third, `subscribe_to_event` identifies the subscriber from the calling agent's own agent-scoped key, so it must be called by the agent itself, not through a user key — verify what exists with `list_event_subscriptions`. See [Event Subscriptions](../collaboration/event-subscriptions.md).

## Can an agent be notified the moment another agent's task finishes, instead of polling?

Yes. Rather than polling `get_execution_result` in a loop, the waiting agent subscribes to the worker agent's task-completion event. When the worker's task reaches a terminal state, Trinity wakes the subscriber with an automatic report-back task carrying the outcome — so a long delegation ends by *notifying* the caller instead of the caller busy-checking. See [Event Subscriptions](../collaboration/event-subscriptions.md).

## Can a worker agent report back to its caller automatically when it's done or fails?

Yes, through task-completion event subscriptions. Trinity emits `agent.task.completed` and `agent.task.failed` events at the end of every execution, and an agent that has subscribed to another agent's task events is woken with a report-back task when one fires. It's permission-gated — the subscriber needs permission to the worker — and best-effort: the wake reaches a *running* subscriber, so a stopped agent misses it rather than receiving a queued backlog. See [Event Subscriptions](../collaboration/event-subscriptions.md).

## Can several of my agents hold a conversation together in one shared session?

Yes — this ships in every Trinity build. From the **Workspace**, mention a second agent in an existing one-to-one chat (`@` and its name) and Trinity opens a shared **room** where several agents — and you — work one topic together across many turns. Each agent keeps its own private context while the room holds the shared transcript, and turn-taking is mechanical: an agent speaks only when it's `@mentioned`. Rooms carry hard message, cost and time budgets so a chain of turns cannot run away. See [Shared Sessions](../collaboration/rooms.md).

## How can I watch agents collaborating on the Dashboard?

Use the Dashboard's **Timeline** view (the default): it replays the fleet's activity and collaboration events — chat start/end, tool calls, schedule start/end, and agent-to-agent calls — each stamped with a state (started, completed, failed, cancelled) and scrubbable over a time range you choose. Agent-to-agent calls appear as collaboration events in that stream, so you can see which agents called which as it happens. The older node-and-edge network canvas has been retired in favor of this Timeline replay; a separate **Grid** view arranges agents as tiles for at-a-glance status. See [Agent Network](../collaboration/agent-network.md).

## Can I deploy a whole team of agents in one step?

Yes, with a system manifest: a YAML file listing the agents (name, template, configuration) plus permission presets, shared folder wiring, schedules, and auto-start settings. Deploy it with the `deploy_system` MCP tool or the REST API (creating agents requires the creator role), and a dry-run mode validates the manifest without creating anything. It's a recipe deployment — once created, the agents are independent and you manage them like any others. See [System Manifest](../collaboration/system-manifest.md).

## How do I restart all agents in a system at once?

Use the `restart_system(name)` MCP tool or the matching REST endpoint. Trinity finds every agent you can access whose name starts with the system prefix, stops the running ones, starts each again, and returns which agents were restarted and which failed. It's handy after configuration changes that need a container restart, such as shared folder updates. See [System Manifest](../collaboration/system-manifest.md).

## Can external orchestrators discover and call my Trinity agents?

Yes — discover *and* call. Every agent publishes an A2A Agent Card (protocol `0.3.0`) at `GET /api/agents/{name}/a2a/agent-card` — a standard JSON document (built from the agent's `template.yaml` and container labels) advertising its name, description, capabilities, skills, and URL, so external orchestrators can discover it without knowing Trinity's internal API. That endpoint requires authentication (owner, admin, or shared user, via JWT or MCP key), and it still returns a partial card when the agent is stopped.

To let an outside orchestrator actually reach an agent, turn on **A2A exposure** for it (Sharing tab → A2A). Exposure is off by default, and until you enable it nothing is publicly reachable. Once enabled, the agent gets a public discovery card at `GET /a2a/{name}/.well-known/agent-card.json` and a JSON-RPC task endpoint at `POST /a2a/{name}` (`message/send`, `message/stream` over SSE, `tasks/get`, `tasks/cancel`). Discovery is unauthenticated and rate limited per IP; **tasking always requires a Trinity MCP API key**, and the caller still has to be an owner or shared user of that agent. A non-exposed agent is indistinguishable from one that doesn't exist — both return `404`.

Set `PUBLIC_CHAT_URL` or `FRONTEND_URL` so the card advertises an externally reachable URL. See [A2A Protocol](../integrations/a2a-protocol.md).


## Can my agents call an external A2A agent?

Yes — this is the outbound direction, and it's off by default. An admin turns it on and registers each external endpoint by name (with any credential it needs); an agent then picks a target **by name** and can never supply a URL of its own, so a prompt injection can't aim Trinity at an address of the attacker's choosing. Agents call it with the `call_a2a_agent` tool and poll long-running work with `get_a2a_task`. Calls are bounded — 30 per minute per agent, 120 fleet-wide — and each one is deduplicated by a label you supply, so a re-run replays the earlier answer instead of paying twice. See [A2A Protocol](../integrations/a2a-protocol.md).

## An outbound A2A call timed out — should I just try again?

No, not blindly. A timeout means Trinity gave up waiting, not that the remote agent didn't run the task — the response says `possibly_delivered` for exactly this reason. If you have a task id, poll it with `get_a2a_task`. Otherwise re-send with the **same** dedup label: that replays the original answer if the call already completed, rather than triggering the work a second time. See [A2A Protocol](../integrations/a2a-protocol.md).

## Can my agent message me proactively instead of waiting for me to ask?

Yes, with the `send_message` MCP tool, which delivers a message to a user identified by their verified email address. It's consent-based: the recipient must be the agent's owner or have the agent shared with them with the allow-proactive flag enabled, otherwise the send is rejected. Delivery goes over Telegram, Slack, or web — `auto` tries Telegram, then Slack, then web — and sends are rate-limited to 10 messages per recipient per hour, with a 4096-character limit per message. See [MCP Server](../integrations/mcp-server.md).

## What's the difference between an agent loop and agent collaboration?

A loop runs the same task against one agent repeatedly with a bounded run count — it's single-agent automation, not communication. Collaboration is different agents calling each other via MCP: delegation, orchestrator-worker patterns, events, and shared folders. Use a loop to grind through a backlog on one agent; use collaboration when the work needs to move between agents. See [Agent Loops](../automation/agent-loops.md).
