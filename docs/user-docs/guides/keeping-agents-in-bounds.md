# How Trinity Keeps Agents in Bounds

Trinity's approach to guardrails, for operators, security reviewers and buyers. The short version: **Trinity bounds agents by what they hold, not what they're told.**

The first half of this page is the approach: the bet, the principles behind it, and what we deliberately don't do. The second half, [Where it stands](#where-it-stands), says what ships in the current release, what is coming in the next one, and what is planned. Read the principles as the direction and the table as the facts.

## The bet

Companies don't hold back real work from agents because the agents lack capability. They hold it back because they can't bound them. The usual answers don't scale:

- a prompt that says "please don't"
- a human approving every step
- a rigid workflow that throws away the agent's judgment

We think delegation becomes routine when it is bounded the way a job is.

## The vision

Give an agent a job the way you'd give a person one: a desk, the keys that job needs, someone who signs off on what matters, and a record of what they did. It earns more as it proves itself.

## Five principles

1. **Keys, not rules.** An agent can do only what its credentials allow. A payment belongs to the one agent that holds the payment key, and every other agent has to ask it. A rule can be talked around. A missing key can't.
2. **Limits live outside the agent.** Inside its own workspace an agent is a power user: it installs packages and runs code. The in-container [guardrail hooks](../agents/agent-guardrails.md) catch accidents. The boundary that holds sits in the platform: the credentials the agent was granted, the platform calls its key may make, and the settings only a person can change. Prompts shape behaviour; they never set bounds.
3. **Sign-off before the work, never during.** You mark an action on an agent as "needs a yes from <role>". The request goes to the person in that role before the agent starts. Nothing pauses halfway and resumes on stale state. The asking agent never approves its own request, and silence is never consent.
4. **Trust is earned per kind of work.** A new agent starts on a short leash. A kind of task graduates from "ask first" to "just do it" on evidence, and drops back after a bad call. Earned trust changes *when* an agent acts, never *what it may touch*. Only a person removes a gate.
5. **Everything leaves a receipt.** Grants, refusals and approvals are recorded in a tamper-evident audit log, along with which key acted. You can always answer who allowed this, who did it, and how to stop it.

## What it feels like

This is where the principles lead. Some of it ships today; the table below says which.

- **An operator** opens one agent and sees what it can touch, what needs a yes, and what it did. One click stops it, revokes its credentials, or switches it to read-only.
- **An approver** gets one card: *"Pay Acme $4,200, requested by the ops agent."* She approves. The finance agent pays, and the ops agent is told.
- **An agent** that reaches for something it wasn't given is told plainly what is missing, and it asks.

## What we deliberately don't do

- Sell prompt-based guardrails as safety.
- Block individual functions inside an agent. It routes around them.
- Turn agents back into scripts to make them safe.
- Put a human on every step. That isn't delegation.

## Where it stands

**Available now** means shipped in the latest release (v0.9.5). **Next release** means built and merged, not yet released. **Planned** means not built. Features marked *requires an entitlement* are part of the Enterprise edition.

| | Available now | Next release | Planned |
|---|---|---|---|
| **Hold** — what an agent can reach | Each agent holds only the credentials injected into it ([Credentials](../credentials/credential-management.md)).<br>Agent-to-agent permissions: an agent can call only the agents you allow ([Agent Permissions](../collaboration/agent-permissions.md)).<br>An agent's own API key can never pass an admin check, even when the agent's owner is an admin.<br>Credential vault: an agent fetches a granted secret by name at run time instead of holding it in its `.env`, and grants can be revoked per agent (*requires an entitlement*). | Settings only a person can change: an agent's or the system's key is refused on agent configuration writes (autonomy, read-only, resources, capacity), and creating API keys or changing a GitHub token needs a signed-in browser session. | Outbound network allow-lists per agent.<br>Per-agent spend caps. |
| **Sign-off** — a yes before the work | Agents raise approvals, questions and alerts in the [operator queue](../operations/operating-room.md); a person answers in the **Operating Room**, and Workspace users answer the asks addressed to them. Unanswered asks expire. | Agents raise asks with the `ask_operator` tool, and an expired ask ends as *not approved*.<br>Only a person can answer, cancel or dismiss an ask: agent and system keys are refused, so no agent approves its own request.<br>Asks addressed to a role instead of a name: the agent's primary owner or the operators in every edition; the *approver* and *viewer* roles *require an entitlement*.<br>A Workspace **Inbox** that collects the asks waiting on you, in every edition.<br>An ask can carry the exact action it proposes, frozen with the ask. | Gated skills: mark a skill on an agent as "needs a yes from <role>", and the call is held until a person approves, then runs exactly once. The enforcement is built, but nothing switches it on until the per-agent approval map ships, so today no skill is gated.<br>Approvals bound to the exact effect (amount, payee) and enforced by the platform. |
| **Trust** — more autonomy as it proves itself | A per-agent autonomy switch that a person turns on or off for scheduled work. It is manual, and every change is audited. | Admin-granted capabilities: a person grants a named agent a specific platform capability, such as managing skills. | Earned trust per kind of work: graduating from "ask first" to "just do it" on evidence, and dropping back after a bad call. |
| **See & stop** — a receipt, and an off switch | An append-only [audit trail](../operations/audit-trail.md) of administrative actions (grants and revocations, key changes, logins, credential changes, autonomy changes, emergency stops), with the MCP key that acted. An optional SHA-256 hash chain proves it hasn't been tampered with; the audit dashboard *requires an entitlement*, the API does not.<br>Per-agent execution history.<br>Stop one agent, or stop the fleet with the emergency stop.<br>Switch an agent to read-only. | — | Refusals recorded in the audit trail alongside grants and approvals.<br>One-click credential revocation for an agent's injected credentials.<br>One control page per agent: what it can touch, what needs a yes, and what it did. |

## Coverage by runtime

Trinity runs agents on three [runtimes](../agents/agent-runtimes.md). Everything in the **Hold** and **See & stop** rows sits in the platform, so it applies to all three: an agent can use only the credentials and platform calls it was given, whichever runtime it runs on. The in-container controls differ:

| In-container control | Claude Code | OpenAI Codex | Gemini CLI |
|---|---|---|---|
| Guardrail hooks (Bash deny-list, credential-file protection, leak scan) | Yes | No | No |
| Read-only mode | Yes, through a hook | Yes, through Codex's own read-only sandbox | No |
| Turn limit | Yes | No; the execution timeout bounds the run | No; the execution timeout bounds the run |
| Per-agent disallowed tools | Yes | No | No |

Guardrails are strongest on Claude Code. On Codex and Gemini CLI, rely on the platform boundary: what the agent holds, and the timeout.

## Honest limits

- **The in-container hooks catch accidents, not a determined agent.** The agent user has passwordless `sudo` in its own container, so it could rewrite or get around them. That is why they are defence in depth, and why the boundary we rely on is outside the container.
- **Agents can reach the open internet today.** There is no outbound allow-list yet.
- **A raw credential plus a shell reaches past the skills.** An agent that holds a raw credential and a shell can act outside its skills. That is why "one key, one agent" is the boundary we rely on: give the payment key to the one agent that pays, and have every other agent ask it.

## See Also

- [Agent Guardrails](../agents/agent-guardrails.md) — The in-container hooks: deny-lists, credential-file protection, turn limits
- [Agent Permissions](../collaboration/agent-permissions.md) — Which agents an agent may call
- [Credential Management](../credentials/credential-management.md) — Injecting credentials, and the vault
- [Operating Room](../operations/operating-room.md) — The operator queue: how agents ask a person for a decision
- [Audit Trail](../operations/audit-trail.md) — The record of who did what
- [Agent Runtimes](../agents/agent-runtimes.md) — Claude Code, OpenAI Codex, Gemini CLI
- [Recommended Trinity Prompt](../agents/recommended-fleet-prompt.md) — Fleet rules for behaviour that guardrails can't block
