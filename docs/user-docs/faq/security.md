# Trinity FAQ — Security

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## Can agents see each other's files?

No. Each agent runs in its own Docker container with its own workspace volume — there is no shared filesystem between agents by default. If you want two agents to exchange files, that is an explicit opt-in: an agent can expose a shared folder, and only agents you have granted permission to can mount it. Everything else in one agent's workspace, including its credentials, stays invisible to every other agent. See [Agent Files](../agents/agent-files.md).

## Can an agent reach the platform database or Redis?

No — this is enforced at the network level, not by policy. Trinity runs two separate Docker networks: agents live on the agent network (172.28.0.0/16) while Redis, the scheduler, and the log aggregator live on the platform network (172.29.0.0/16). Agents have no route to the platform network, so they physically cannot connect to Redis or other internal services. Only the backend and MCP server bridge both networks, and every call through them is authenticated and access-checked.

## Do Trinity containers run as root?

No. The backend, scheduler, and MCP server run as unprivileged UID 1000, the frontend as UID 101, and every agent as the non-root `developer` user. Agent containers additionally drop all Linux capabilities except network binding, run with `no-new-privileges`, and get a non-executable temporary filesystem. This limits what any compromised process can do to its own container.

## Are my credentials stored in Trinity's database?

Not as plaintext, and mostly not at all. Credentials are injected as files (`.env`, `.mcp.json`, and other allow-listed credential files) directly into the agent's container — the database never holds them. The one exception is tokens that must drive long-lived background processes, such as channel bot tokens (Slack, Telegram, WhatsApp) and subscription tokens: these are persisted, but always wrapped in AES-256-GCM encryption — plaintext persistence is forbidden by design. See [Credential Management](../credentials/credential-management.md).

## Can my API keys leak into logs?

Credential values are never logged — all credential operations use structured logging with values masked. On top of that, a guardrail hook scans agent command output for known credential patterns (API keys, GitHub tokens, cloud access keys) and records only the pattern name when it finds a match, never the value itself, so you can review potential leaks without the log becoming one. See [Agent Guardrails](../agents/agent-guardrails.md).

## Is it safe to commit the `.credentials.enc` file to git?

Yes — that is what it exists for. It is an AES-256-GCM encrypted archive of the agent's full credential set, safe to store in version control as an encrypted backup; on agent startup Trinity decrypts and re-injects it automatically. The encryption key lives only in your platform's environment, and it can be rotated online without downtime or data loss. See [Credential Management](../credentials/credential-management.md).

## What is recorded in the audit log?

Administrative and security-relevant actions across the platform: agent lifecycle (create, start, stop, delete, rename), logins and logouts, permission grants and denials, settings changes, credential inject/export/import, git operations, and every MCP tool call. Each entry records who acted (user, agent, or MCP client), what was affected, when, and from where the request originated. Admins can search, filter, and export it from the dashboard. See [Audit Trail](../operations/audit-trail.md).

## Can audit log entries be edited or deleted?

No. The audit log is append-only at the database level: entries can never be modified, and a database trigger refuses deletion of any entry younger than the 365-day retention floor. You can also enable an optional SHA-256 hash chain, where each entry stores a hash linked to the previous one — the verify endpoint walks the chain and reports the first broken link, proving the log wasn't tampered with between two checkpoints. See [Audit Trail](../operations/audit-trail.md).

## How long does my login session last?

Login tokens (JWTs) are valid for 7 days, and all tokens are invalidated whenever the backend restarts, so you re-login after an upgrade. Logging out revokes your token immediately on the server side — a stolen token dies with your session instead of living out its remaining days. MCP API keys don't expire on restart; you revoke them explicitly from key management. See [Authentication](../api-reference/authentication.md).

## Why doesn't Trinity put my login token in WebSocket URLs?

URLs end up in places you don't control — reverse-proxy logs, browser history, and intermediate systems — so a long-lived token in a URL is a leak waiting to happen. Instead, the UI first requests a single-use ticket over a normal authenticated call, then opens the WebSocket with that ticket. The ticket expires in about 30 seconds and is consumed on first use, so even if one is captured it is worthless moments later. See [Authentication](../api-reference/authentication.md).

## What do guardrails actually block?

Guardrails are deterministic, infrastructure-level rules that agents cannot bypass or edit. They block dangerous shell commands against a deny-list (recursive deletion of root or home, world-writable permissions, piping remote scripts to a shell, force pushes, filesystem formatting, host shutdown), block writes to credential files and hook configuration, scan output for leaked credentials, and cap the number of turns per execution to stop runaway loops. If a guardrail hook itself errors, the tool call is blocked — the system fails closed. Owners can tighten the baseline per agent but never loosen it. See [Agent Guardrails](../agents/agent-guardrails.md).

## What is read-only mode?

A per-agent toggle that prevents the agent from modifying source files (`*.py`, `*.js`, and similar) inside its own container, while still allowing writes to designated output folders like `output/` and `content/`. It works through the same hook mechanism as guardrails, intercepting file-editing tool calls before they run. Use it for agents that should analyze and report but never change their own code. See [Agent Configuration](../agents/agent-configuration.md).

## Is it safe to expose a fresh Trinity install to the internet before setup?

No. Until the admin account is created, the first-run setup form is reachable without authentication — whoever completes it first owns the instance. This window is deliberately an operator responsibility: deploy behind a VPN, tunnel, or firewall, complete setup, and only then consider opening public access. If you set `ADMIN_PASSWORD` in `.env` before first boot, the account is created automatically and no open setup window exists. See [Setup](../getting-started/setup.md).

## Is a webhook URL secure enough on its own?

The URL's embedded token is the whole credential, so anyone who obtains the URL can trigger that schedule (rate-limited, and every call is audit-logged). For anything sensitive, enable signature authentication: Trinity issues a signing secret exactly once (stored only encrypted after that), and every request must then carry an HMAC-SHA256 signature of the request body. Requests with a missing or invalid signature are rejected, so a leaked URL alone is no longer enough. You can rotate the secret or the URL at any time. See [Webhook Triggers](../api-reference/webhook-triggers.md).

## How do agents and the backend authenticate to each other?

In both directions, with per-agent credentials. Every backend call into an agent container carries a token derived specifically for that agent from a master secret that never leaves the backend — so even a fully compromised agent cannot compute a sibling's token or impersonate it. In the other direction, each agent authenticates to the platform with its own agent-scoped API key, which is restricted to that agent's identity and revocable independently. See [Authentication](../api-reference/authentication.md).

## Does Trinity phone home?

Nothing leaves your server without an explicit, admin-level opt-in — and never silently. By default Trinity records only anonymous product events (a small fixed set of onboarding/setup steps) **locally**, in its own database; they produce zero network egress. The outbound paths are all opt-in: a one-time contact submission at first-run setup if you check the "security and product updates" box, and a separate, reversible admin toggle that shares coarse anonymized aggregates with a hosted benchmark service (never PII, message content, emails, or agent names). Operators can hard-disable outbound sharing through environment configuration, including the standard `DO_NOT_TRACK` convention, and a blocked or failed submission never affects the platform. See [Product Telemetry](../operations/telemetry.md).

## What does Trinity record about my usage, and does any of it leave my server?

By default Trinity records only anonymous local product events — a small fixed allow-list of onboarding and setup step events, stored in its own database — and they never leave your server; there is no toggle and nothing to configure. Separately, an admin can opt in to share coarse anonymized aggregates (release version, edition, install lane, counts of agents, executions and funnel steps, and an outcome mix of how runs ended) with a hosted benchmark service in exchange for fleet benchmarks; this is off by default, keyed by a random share id that is never your install id, and validated against a documented schema before every send. The shared aggregates never include PII, message content or prompts, emails, or agent names, and they can't be used to reconstruct individual activity. See [Product Telemetry](../operations/telemetry.md).

## How do I turn usage-data sharing on or off?

Fleet sharing is an admin-only toggle in Settings, off by default and fully reversible — opting out stops the next cycle from sending. Before you consent, Settings shows a preview of the exact aggregate payload that would be sent, so nothing goes out sight-unseen, and enabling it is audit-logged. Egress requires two independent gates — your stored consent and a config switch that honors the cross-tool `DO_NOT_TRACK` environment variable — so if either is off, nothing leaves the box. The local product events have no toggle because they never egress in the first place. See [Product Telemetry](../operations/telemetry.md).

## What should I do before exposing Trinity to the internet?

Complete first-run setup while still behind a VPN or firewall, and set a strong admin password. Prefer the Cloudflare Tunnel approach — it opens no inbound firewall ports and only forwards the path prefixes you list, so unlisted routes are rejected at the edge before reaching your server. Then harden the surfaces you actually expose: enable signature authentication on webhooks, review the email whitelist so only intended addresses can log in, and keep the tunnel token in your gitignored `.env`. If everyone who needs access can reach the server over a private network like Tailscale, you may not need public exposure at all. See [Public Access](../guides/deploying/public-access.md).

## Who controls which users can access an agent?

Access is governed by Trinity's role and sharing model: owners share agents with specific users, admins see everything, and roles gate who can create agents at all. That model is covered in its own documentation rather than here. See [Roles and Permissions](../getting-started/roles-and-permissions.md) and [Access Control](../sharing-and-access/access-control.md).

## An admin API key can do anything an admin can — is that a problem?

It was, and it is now fenced. An MCP key resolves to the user who created it and carries that user's role, so on a default admin-owned installation an agent's own injected key would satisfy any plain "is this caller an admin?" check. Endpoints whose blast radius is operator-scale now require a **human** caller in addition to the role, and reject API keys of any scope regardless of the owner: approving an oversized retention deletion, restarting or reinitializing the system agent, registering/editing/syncing a skill source, reading an agent's credential checklist, reading or rotating an agent's MCP key, binding an agent to a GitHub repository, writing an evaluation, and editing organizational tags. See [Authentication](../api-reference/authentication.md).

## How do I know my agent is authenticating to Trinity as itself?

Check the MCP key panel on the agent's **Settings** tab. It reports a health state and offers a **Verify** action that probes the container's actual configuration and tells you whether it is carrying its own key, a foreign user key, another agent's key, an unknown key, or a duplicate entry. This matters because the agent-to-agent permission matrix only applies to agent-scoped keys — a container holding a *user*-scoped key operates with the owner's identity and bypasses the matrix. If something is wrong, **Regenerate** rotates and delivers a fresh key; Trinity also self-heals a missing or mismatched key on the agent's next start. See [MCP Server](../integrations/mcp-server.md).

## Can a skills repository push executable code to my whole fleet without me noticing?

Only if you enable fleet re-inject, and even then the supply chain is pinned. Skills carry executable scripts, so the bundled community source — which accepts public contributions — is pinned to a **tag**, not a branch head, and a tag that later resolves to a different commit is **refused** rather than adopted. Custom sources, whose write access you control, track a branch. Both automation settings (auto-sync, fleet re-inject) default to off, source URLs are locked to github.com, and registering or syncing a source is admin-and-human-only because it decides which repository your fleet executes code from. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## Is anything stopping a retention setting from wiping my history?

Yes, two independent controls. Retention windows have exactly one write path, which type- and range-validates every value all-or-nothing and audit-logs the change; the generic settings endpoint refuses those keys. And any sweep that would delete more than a fixed safety threshold (1,000 rows) of a single table **refuses** to run, logs an error, and raises an operator alarm — an admin must then approve it explicitly, bound to the exact window in force and single-use. Note the counter-intuitive part: garbage input always failed safe (retain forever); a *small valid number* is the dangerous input, which is why the approval gate, not the validation, is what actually protects you. "Reset to defaults" deliberately skips retention windows. See [Monitoring](../operations/monitoring.md).
