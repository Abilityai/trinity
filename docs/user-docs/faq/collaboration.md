# Trinity FAQ — Agent Collaboration

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## How do agents talk to each other in Trinity?

Agents communicate through the Trinity MCP server, not by calling each other's containers directly. Every agent gets its own agent-scoped API key, injected automatically and wired into the agent's MCP configuration, so it can call tools like `chat_with_agent(agent_name, message)` to send a message to another agent and get the response back. Trinity detects the calling agent and shows the interaction live on the Dashboard network graph. See [Agent Network](../collaboration/agent-network.md).

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

## What do the edges and activity feed on the Dashboard graph mean?

The network graph shows each agent as a node, color-coded by status (green running, gray stopped). When one agent calls another via MCP, an animated edge appears between them for about three seconds, and the real-time activity feed logs the collaboration and activity events (started, completed, failed) as they happen; replay lets you re-watch a chosen time range. Click a node to open that agent's detail page — node positions are saved in your browser, so you can drag them into a layout that sticks. See [Agent Network](../collaboration/agent-network.md).

## Can I deploy a whole team of agents in one step?

Yes, with a system manifest: a YAML file listing the agents (name, template, configuration) plus permission presets, shared folder wiring, schedules, and auto-start settings. Deploy it with the `deploy_system` MCP tool or the REST API (creating agents requires the creator role), and a dry-run mode validates the manifest without creating anything. It's a recipe deployment — once created, the agents are independent and you manage them like any others. See [System Manifest](../collaboration/system-manifest.md).

## How do I restart all agents in a system at once?

Use the `restart_system(name)` MCP tool or the matching REST endpoint. Trinity finds every agent you can access whose name starts with the system prefix, stops the running ones, starts each again, and returns which agents were restarted and which failed. It's handy after configuration changes that need a container restart, such as shared folder updates. See [System Manifest](../collaboration/system-manifest.md).

## Can external orchestrators discover and call my Trinity agents?

Yes. Every agent publishes an A2A v1.0 Agent Card at `GET /api/agents/{name}/a2a/agent-card` — a standard JSON document (built from the agent's `template.yaml` and container labels) advertising its name, description, capabilities, skills, and URL, so external orchestrators can discover it without knowing Trinity's internal API. The endpoint requires authentication (owner, admin, or shared user, via JWT or MCP key), and it still returns a partial card when the agent is stopped. There is no public unauthenticated `/.well-known` route yet, and you should set `PUBLIC_CHAT_URL` or `FRONTEND_URL` so the card advertises an externally reachable URL. See [A2A Agent Card](../integrations/a2a-protocol.md).

## Can my agent message me proactively instead of waiting for me to ask?

Yes, with the `send_message` MCP tool, which delivers a message to a user identified by their verified email address. It's consent-based: the recipient must be the agent's owner or have the agent shared with them with the allow-proactive flag enabled, otherwise the send is rejected. Delivery goes over Telegram, Slack, or web — `auto` tries Telegram, then Slack, then web — and sends are rate-limited to 10 messages per recipient per hour, with a 4096-character limit per message. See [MCP Server](../integrations/mcp-server.md).

## What's the difference between an agent loop and agent collaboration?

A loop runs the same task against one agent repeatedly with a bounded run count — it's single-agent automation, not communication. Collaboration is different agents calling each other via MCP: delegation, orchestrator-worker patterns, events, and shared folders. Use a loop to grind through a backlog on one agent; use collaboration when the work needs to move between agents. See [Agent Loops](../automation/agent-loops.md).
