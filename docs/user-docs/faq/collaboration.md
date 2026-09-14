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

## Who sets a room's limits, and who can rename it or change who's in it?

An admin sets the defaults for every room started from the Workspace under **Settings → Retention → Room budgets**: **Messages** (200 by default, up to 500), **Cost cap (USD)** (empty by default, meaning no cap), and **Expires after** N hours (168 by default — one week; up to 168, and `0` means never). A room closes permanently, with a visible reason, when it reaches any of them; changes apply to rooms created from then on, and a Workspace client cannot override them, though a platform caller creating a room over the API or MCP may pass its own budget. The person who created the room is its **moderator**: closing it and adding or removing agents are moderator or admin actions, while any human member can rename the room in place from its header. Agents only ever talk in a room — they never manage it. See [Shared Sessions](../collaboration/rooms.md#budget-defaults-admin).

## What does the notice about a client reading the room mean?

A room that includes a Workspace client — someone outside your own organisation — tells every agent it wakes that a person is reading, so agent-to-agent turns keep internal details, other customers and costs out of the transcript. The signal comes from the room's membership alone, never from anything a participant writes, and it names nobody. A room holding only agents and operators carries no such notice. See [Shared Sessions](../collaboration/rooms.md).

## How can I watch agents collaborating on the Dashboard?

Use the Dashboard's **Timeline** view (the default): it replays the fleet's activity and collaboration events — chat start/end, tool calls, schedule start/end, and agent-to-agent calls — each stamped with a state (started, completed, failed, cancelled) and scrubbable over a time range you choose. Agent-to-agent calls appear as collaboration events in that stream, so you can see which agents called which as it happens. The older node-and-edge network canvas has been retired in favor of this Timeline replay; a separate **Grid** view arranges agents as tiles for at-a-glance status. See [Agent Network](../collaboration/agent-network.md).

## Can I deploy a whole team of agents in one step?

Yes, with a system manifest: a YAML file listing the agents (name, template, configuration) plus permission presets, shared folder wiring, schedules, tags and auto-start settings. Install it from **Library → Systems** — pick a bundled manifest, upload a file, or paste YAML, then **Preview** (validates without creating anything and shows the agent names, who can call whom, and any schedules that would start) and **Deploy** — or use the `deploy_system` MCP tool or the REST API, which offer the same dry run. You need the creator role, because installing a system creates agents. It's a recipe deployment: once created the agents are independent, and deploy is best-effort, so read the result's status and its "things needing attention" list rather than trusting the HTTP code. See [System Manifest](../collaboration/system-manifest.md#installing-a-system-from-the-ui).

## How do I restart all agents in a system at once?

Use the `restart_system(name)` MCP tool or `POST /api/systems/{name}/restart`. Membership is by tag, not by name: deploy tags every member with the system name and the restart resolves members from that tag (only agents deployed before tagging existed fall back to the name prefix), so restarting `acme` never touches a system called `acme-extra`. Trinity stops the running members, starts each again, and reports which restarted and which failed — handy after changes that need a container restart, such as shared folder updates. It requires the creator role and is human-only: an agent-scoped API key is refused, because a bulk restart would let an agent touch agents it didn't spawn. See [System Manifest](../collaboration/system-manifest.md#after-deploy-reading-restarting-and-exporting-a-system).

## Can I export a deployed system as a manifest and deploy it again somewhere else?

Yes. `GET /api/systems/{name}/manifest` (or the `get_system_manifest` MCP tool) writes a manifest from the system's current members — their permissions, shared folders and real schedules — that you can hand to `deploy_system` again. Two things are deliberately left out: permission edges pointing at agents outside the system are dropped rather than exported as broken names, and your instance-wide platform prompt is never embedded as the manifest's `prompt:` key, because deploying that export elsewhere would overwrite the prompt for every agent on the other instance. Keep the redeploy caveat in mind: a manifest deployed where those agent names already exist creates suffixed copies (`_2`, `_3`) rather than converging onto the existing agents. See [System Manifest](../collaboration/system-manifest.md#after-deploy-reading-restarting-and-exporting-a-system).

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

## Can my agent ask a specific person a question and wait for their answer?

Yes. When an agent parks a request in its operator queue it can address it to one person it is shared with (`addressed_to_email`). The ask then renders above the composer of the chat it belongs to in that person's Workspace — and, for platform users, again under **Waiting on you** in the rail's **Work** tab; an ask raised outside any chat, by a scheduled run say, attaches to their **Main** chat with that agent. Operators still see it in the Operations queue. The answer is the decision the agent reads: for an approval that listed options it must be exactly one of them — anything else is refused with a `422` naming the offered options — and a note is optional, never standing alone. Answering removes the ask everywhere at once; an expired ask stays visible, marked expired, and the agent treats expiry as "not approved". Someone the agent isn't shared with can't be addressed — the item becomes an ordinary operator ask instead. See [Approvals](../automation/approvals.md#asks-addressed-to-a-workspace-user).

## I answered my agent's request but nothing happened — why?

By default an answer is only read at the start of the agent's *next* turn — its next scheduled run, loop iteration or message — so an agent with no schedule and nobody chatting to it waits indefinitely even though the decision was recorded. Turn on **Wake this agent when an operator answers** in the agent's **Settings → Reliability** (off by default): answering then starts one turn immediately, the agent acts on the decision in it, and that run shows in Executions with the trigger `operator_response`. It's a per-agent switch the owner controls, never something a request can ask for. The Workspace confirmation tells the answerer which case applies — **Sent.** versus **Sent — {agent} is picking this up.** See [Wake on Operator Answer](../agents/agent-configuration.md#wake-on-operator-answer).

## How does an agent deliver a report to a specific person?

By addressing it: the `report` MCP tool takes an `audience_email`, checked against the agent's own roster — someone the agent isn't shared with is refused. An addressed report is a **deliverable**: it appears in that person's Workspace under the rail's **Info → Reports** for that agent and, if the agent also passed its current `execution_id`, as a **Delivered here** card at the end of the chat that produced it. A report published outside a chat — from a scheduled run, for instance — lists under Info only. Unaddressed reports stay operator-only and never appear in the Workspace, so an agent that hasn't adopted `audience_email` yet simply shows its clients an empty Reports section. See [Agent Reports](../operations/agent-reports.md#deliverables-in-the-workspace).

## What happens when someone rates a deliverable, and what does the agent get to see?

Each deliverable card carries **Useful** / **Not what I needed**; one click records the rating, and a negative one also opens an optional comment box. It's one rating per person per deliverable — clicking the other option corrects it, and there is no un-rate. When the agent reads its ratings back it gets tallies only: that it was rated up or down, never the words and never who, because a comment is untrusted text from someone who is, by construction, annoyed, and handing it to the thing being criticised is a prompt-injection path. Operators read comments in full, and a negative rating raises a heads-up in the operator queue (once per person per deliverable per day). The only route by which the words reach an agent is a `capture-feedback` skill, run in a background turn of its own and never posted into your chat — which is why the acknowledgement reads either *passed on to the agent* or *recorded for the team*. See [Agent Reports](../operations/agent-reports.md#rating-a-deliverable).

## My agent handed work to another agent that finished after I left the conversation — where does the result go?

Back to the conversation it started from, success and failure alike. A job that began in a Workspace chat and finished later — because the agent delegated it or kicked off background work — lands as a message from the agent in that chat, filed under the agent you were talking to and naming the one that did the work when that differs; it opens with **Finished** or **Didn't finish** plus the status, so a failure never vanishes. The same report-back reaches a Slack channel or thread, a Telegram chat, and — for a scheduled run addressed to someone — that person's **Main** chat with the agent. Each finished job reports at most once and never to a conversation it didn't start from, and an ordinary turn that already answered inline isn't reported a second time. The Workspace doesn't push it while you watch; it's there on the next load or chat switch. See [Event Subscriptions](../collaboration/event-subscriptions.md#reporting-back-to-the-person-who-asked).

## What's the difference between an agent loop and agent collaboration?

A loop runs the same task against one agent repeatedly with a bounded run count — it's single-agent automation, not communication. Collaboration is different agents calling each other via MCP: delegation, orchestrator-worker patterns, events, and shared folders. Use a loop to grind through a backlog on one agent; use collaboration when the work needs to move between agents. See [Agent Loops](../automation/agent-loops.md).
