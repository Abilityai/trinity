# Trinity FAQ — Security

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## Can agents see each other's files?

No. Each agent runs in its own Docker container with its own workspace volume — there is no shared filesystem between agents by default. If you want two agents to exchange files, that is an explicit opt-in: an agent can expose a shared folder, and only agents you have granted permission to can mount it. Everything else in one agent's workspace, including its credentials, stays invisible to every other agent. See [Agent Files](../agents/agent-files.md).

## Can an agent reach the platform database or Redis?

No — this is enforced at the network level, not by policy. Trinity runs two separate Docker networks: agents live on the agent network (172.28.0.0/16) while Redis, the scheduler, and the log aggregator live on the platform network (172.29.0.0/16). Agents have no route to the platform network, so they physically cannot connect to Redis or other internal services. Only the backend and MCP server bridge both networks, and every call through them is authenticated and access-checked.

## Do Trinity containers run as root?

No. The backend, scheduler, and MCP server run as unprivileged UID 1000, the frontend as UID 101, and every agent as the non-root `developer` user. Agent containers additionally drop all Linux capabilities except network binding, run with `no-new-privileges`, and get a non-executable temporary filesystem. This limits what any compromised process can do to its own container.

## Are my credentials stored in Trinity's database?

Never as plaintext. Agent credentials are injected as files (`.env`, `.mcp.json`, and other allow-listed credential files) directly into the agent's container and never land in the database at all. What the database does hold is always wrapped in AES-256-GCM encryption: channel bot tokens (Slack, Telegram, WhatsApp), shared subscription tokens, GitHub PATs, vault entries, and the platform's own credentials — the Anthropic API key, the platform GitHub PAT, the Slack app token, client secret, and signing secret, and the Google API key — which are stored under `<key>_encrypted` names. The generic settings route refuses to store a secret in the clear (`PUT /api/settings/{key}` answers 422 for any credential-shaped key), and an install upgraded from an older release re-encrypts a leftover cleartext row on first read. Because backups taken before that upgrade still hold plaintext, rotate those platform tokens after upgrading — runbook: [Secret settings encryption](../../migrations/SECRET_SETTINGS_ENCRYPTION_2026-08.md). See [Credential Management](../credentials/credential-management.md).

## Can my API keys leak into logs?

Credential values are never logged — all credential operations use structured logging with values masked. On top of that, a guardrail hook scans agent command output for known credential patterns (API keys, GitHub tokens, cloud access keys) and records only the pattern name when it finds a match, never the value itself, so you can review potential leaks without the log becoming one. A value an agent fetches from the Credential Vault is additionally scrubbed out of everything Trinity persists from that turn — transcript, execution log, response, notifications — and replaced with `***REDACTED***`. See [Agent Guardrails](../agents/agent-guardrails.md).

## Is it safe to commit the `.credentials.enc` file to git?

Yes — that is what it exists for. It is an AES-256-GCM encrypted archive of the agent's full credential set, safe to store in version control as an encrypted backup; on agent startup Trinity decrypts and re-injects it automatically. The encryption key lives only in your platform's environment, and it can be rotated online without downtime or data loss. See [Credential Management](../credentials/credential-management.md).

## What is recorded in the audit log?

Administrative and security-relevant actions across the platform: agent lifecycle (create, start, stop, delete, rename, recover), logins and logouts, permission grants and denials, settings changes, credential inject/export/import, git operations, and every MCP tool call. Each entry records who acted (user, agent, MCP client, or system), what was affected, when, and where the request originated. When the call was authenticated with an MCP API key, the entry also names the key — its id, name, and scope — beside the accountable owner, so "what did that leaked key touch?" is answered by filtering the list on the key id; a browser session records no key fields. Admins can search, filter, and export it from the dashboard. See [Audit Trail](../operations/audit-trail.md).

## Can audit log entries be edited or deleted?

No. The audit log is append-only at the database level: entries can never be modified, and a database trigger refuses deletion of any entry younger than the 365-day retention floor. You can also enable an optional SHA-256 hash chain, where each entry stores a hash linked to the previous one — the verify endpoint walks the chain and reports the first broken link, proving the log wasn't tampered with between two checkpoints. See [Audit Trail](../operations/audit-trail.md).

## How long does my login session last?

Login tokens (JWTs) are valid for 7 days, and all tokens are invalidated whenever the backend restarts, so you re-login after an upgrade. Logging out revokes your token immediately on the server side — a stolen token dies with your session instead of living out its remaining days. MCP API keys don't expire on restart; you revoke them explicitly from key management. See [Authentication](../api-reference/authentication.md).

## Why does my login script get a 403 even though the password is right?

Because the account requires a second factor, and a correct password alone does not complete the login. `POST /api/token` answers 403 with `mfa_required` and a short-lived `challenge_token` — there is no `access_token` and no session was issued; only completing the second-factor step returns the real token. A 401 means the credentials were rejected; 200 means a session was issued. Two-factor authentication (which requires an entitlement) can start applying the moment an administrator enables the role policy, before anyone has enrolled, so an unattended credential can begin receiving 403s without its own configuration changing. Scripts should check both the status code and the presence of the token, never echo the challenge body into logs, and for automation prefer an MCP API key (`trinity_mcp_*`), which is not subject to the second-factor flow. See [Authentication](../api-reference/authentication.md#second-factor-pending).

## Why doesn't Trinity put my login token in WebSocket URLs?

URLs end up in places you don't control — reverse-proxy logs, browser history, and intermediate systems — so a long-lived token in a URL is a leak waiting to happen. Instead, the UI first requests a single-use ticket over a normal authenticated call, then opens the WebSocket with that ticket. The ticket expires in about 30 seconds and is consumed on first use, so even if one is captured it is worthless moments later. See [Authentication](../api-reference/authentication.md).

## Can a user receive WebSocket events for agents they can't access?

No. The browser's `/ws` connection is scoped to the agents its ticket's user can access: an event that names agents is delivered only when every agent it names is one that user may access, and both live delivery and the reconnect replay are filtered — admins see everything. An unknown or suspended account is closed with code 4001. The MCP-key endpoint `/ws/events` is scoped the same way to the key owner's agents, and only `user`, `agent` (non-ephemeral agents), and `system` keys may open it; `ops`, `connector`, and `portal_delegate` keys are closed with code 4003. See [Authentication](../api-reference/authentication.md#websocket-authentication).

## What do guardrails actually block?

Guardrails are deterministic, infrastructure-level rules that agents cannot bypass or edit. They block dangerous shell commands against a deny-list (recursive deletion of root or home, world-writable permissions, piping remote scripts to a shell, force pushes, filesystem formatting, host shutdown), block writes to credential files and hook configuration, scan output for leaked credentials, and cap the number of turns per execution to stop runaway loops. If a guardrail hook itself errors, the tool call is blocked — the system fails closed. Owners can tighten the baseline per agent but never loosen it. See [Agent Guardrails](../agents/agent-guardrails.md).

## Can an agent — or a git push to its repository — remove its own guardrail hooks?

No. Hook registration lives in Claude Code's admin-controlled managed settings (`/etc/claude-code/managed-settings.json`), which is root-owned and read-only, takes precedence over user and project settings, and sits outside the git-synced working tree — so neither an edit inside the container nor a push to the agent's repository can remove it, and the hook scripts under `/opt/trinity/` are root-owned too. Both paths are also on the guardrails' own write-protection list. On every boot the container checks that the registration is present and unwritable and logs `GUARDRAILS: ERROR` if it is not, so a broken registration shows up in the agent's logs rather than going silently missing. See [Agent Guardrails](../agents/agent-guardrails.md).

## Why are some Claude Code tools missing during a scheduled or headless run?

Because a task, schedule, loop, or MCP call runs the agent as a one-shot turn, and nothing it starts survives the end of that turn. Claude Code's built-in tools that promise a *later* event — a scheduled wake-up, a cron entry, a workflow or task-output notification, a message to another local session, a push notification, a remote trigger — would let the agent plan around an event that will never arrive, so Trinity withholds that whole tool family from headless runs, platform-wide, merged with any per-agent disallowed tools. The agent is told so in its platform prompt and pointed at Trinity's own mechanisms instead: `run_agent_loop` for repetition, `set_reminder` for a deferred self-trigger, and `chat_with_agent` for talking to another agent. Subagents are unaffected, and a background shell command still running when the turn ends is killed with the execution recording that it was. See [Agent Runtimes](../agents/agent-runtimes.md#headless-runs-on-claude-code).

## What is read-only mode?

A per-agent toggle that prevents the agent from modifying source files (`*.py`, `*.js`, and similar) inside its own container, while still allowing writes to designated output folders like `output/` and `content/`. It works through the same hook mechanism as guardrails, intercepting file-editing tool calls before they run. Use it for agents that should analyze and report but never change their own code. See [Agent Configuration](../agents/agent-configuration.md).

## Is it safe to expose a fresh Trinity install to the internet before setup?

Only an install with **no admin account** has an open window. On a normal install `start.sh` refuses to start while `ADMIN_PASSWORD` is blank, so the admin exists before you ever open a browser and the first-run form never appears. The unauthenticated "Create your admin account" form shows only when the backend came up with no admin — a bare `docker compose up` or a hand-rolled backend — and in that state whoever submits it first owns the instance, so keep such an instance behind a VPN, tunnel, or firewall until the account exists. The endpoint refuses with 403 whenever a usable admin already exists, whatever the setup flag says, so it cannot be used to take over an install that is already configured. See [Setup](../getting-started/setup.md).

## Is a webhook URL secure enough on its own?

The URL's embedded token is the whole credential, so anyone who obtains the URL can trigger that schedule (rate-limited, and every call is audit-logged). For anything sensitive, enable signature authentication: Trinity issues a signing secret exactly once (stored only encrypted after that), and every request must then carry an HMAC-SHA256 signature of the request body. Requests with a missing or invalid signature are rejected, so a leaked URL alone is no longer enough. You can rotate the secret or the URL at any time. See [Webhook Triggers](../api-reference/webhook-triggers.md).

## How do agents and the backend authenticate to each other?

In both directions, with per-agent credentials. Every backend call into an agent container carries a token derived specifically for that agent from a master secret that never leaves the backend — so even a fully compromised agent cannot compute a sibling's token or impersonate it. In the other direction, each agent authenticates to the platform with its own agent-scoped API key, which is restricted to that agent's identity and revocable independently. See [Authentication](../api-reference/authentication.md).

## Does Trinity phone home?

Nothing leaves your server without an explicit, admin-level opt-in — and never silently. By default Trinity records only anonymous product events (a small fixed set of onboarding/setup steps) **locally**, in its own database, with zero network egress. There are exactly two outbound paths, both opt-in and independent of each other: the **Security & product updates** contact form (**Settings → General**, or the optional checkbox on the first-run form), which sends exactly the details you typed at most once per install and can be disabled outright with `OPERATOR_INTAKE_ENABLED=false`; and anonymous **Usage sharing**, a reversible admin toggle that sends coarse aggregates (never PII, message content, emails, or agent names) under a random share id that is never your install id. The standard `DO_NOT_TRACK=1` convention hard-disables both, and a blocked or failed send never affects the platform. See [Product Telemetry](../operations/telemetry.md).

## What does Trinity record about my usage, and does any of it leave my server?

By default Trinity records only anonymous local product events — a small fixed allow-list of onboarding and setup step events, stored in its own database — and they never leave your server; there is no toggle and nothing to configure. Separately, an admin can opt in to share coarse anonymized aggregates (release version, edition, install lane, counts of agents, executions and funnel steps, and an outcome mix of how runs ended) with a hosted benchmark service in exchange for fleet benchmarks; this is off by default, keyed by a random share id that is never your install id, and validated against a documented schema before every send. The shared aggregates never include PII, message content or prompts, emails, or agent names, and they can't be used to reconstruct individual activity. See [Product Telemetry](../operations/telemetry.md).

## How do I turn usage-data sharing on or off?

Two admin-only surfaces, both off by default. After login the Dashboard shows a **Finish setup** card whose **Help improve Trinity** section offers **Share anonymous usage**, **Not now** (hides the ask in this browser for 14 days; nothing is sent), and **Don't ask again** (a once-per-install marker that silences it on every device; audit-logged). The permanent home is **Settings → General → Usage sharing**: the reversible toggle, the backfill choice (7, 30, or 90 days of local history, or none), the share id, an "Exactly what would be shared" preview of the exact payload, and **Recent sends** — the last five attempts with the receiver each went to and its status. Egress needs two independent gates — your stored consent and the `TELEMETRY_SHARING_ENABLED` config switch, which honors `DO_NOT_TRACK` — so if either is off nothing leaves the box; opting out deletes the share id locally and the next heartbeat stops sending. Consent is a human-only decision: an API key of any scope cannot toggle it or dismiss the ask. See [Product Telemetry](../operations/telemetry.md).

## What should I do before exposing Trinity to the internet?

Complete first-run setup while still behind a VPN or firewall, and set a strong admin password. Prefer the Cloudflare Tunnel approach — it opens no inbound firewall ports and only forwards the path prefixes you list, so unlisted routes are rejected at the edge before reaching your server. Then harden the surfaces you actually expose: enable signature authentication on webhooks, review the email whitelist so only intended addresses can log in, and keep the tunnel token in your gitignored `.env`. If everyone who needs access can reach the server over a private network like Tailscale, you may not need public exposure at all. See [Public Access](../guides/deploying/public-access.md).

## Who controls which users can access an agent?

Access is governed by Trinity's role and sharing model: owners share agents with specific users, admins see everything, and roles gate who can create agents at all. That model is covered in its own documentation rather than here. See [Roles and Permissions](../getting-started/roles-and-permissions.md) and [Access Control](../sharing-and-access/access-control.md).

## An admin API key can do anything an admin can — is that a problem?

Not any more — the admin gate is an allowlist over key scopes, not a list of known exceptions. An MCP key resolves to the user who created it, carrying that user's role, so on a default admin-owned install an agent's own injected key would otherwise pass any plain "is this caller an admin?" check. Admin-only endpoints now admit only a browser session, a standard `user` key, and the system agent's key: an agent-scoped key never satisfies an admin gate, and neither does a `connector`, `portal_delegate`, or `ops` key or any scope invented later (the individual ops read routes opt the `ops` scope in explicitly). On top of that, endpoints whose blast radius is operator-scale require a **human** caller and reject keys of any scope: approving an oversized retention deletion, turning usage sharing on or off, restarting or reinitializing the system agent, managing skill sources, reading an agent's credential checklist, reading or rotating an agent's MCP key, minting an ops or portal-delegate key, binding an agent to a GitHub repository, writing an evaluation, and editing organizational tags. A 403 from an automation on one of those is the gate working as intended. See [Authentication](../api-reference/authentication.md).

## What is an Ops (read-only) API key, and when should I use one?

It is the bounded machine credential for a monitoring integration — a status dashboard or an ops agent — that must keep working when two-factor authentication is enforced, since key validation never passes through the second-factor flow. An ops key reaches a fixed set of read endpoints (version, fleet status, health, schedules, alerts and costs, monitoring status, host and container telemetry, the agent roster, execution stats, history and the live log stream, subscription usage) and nothing else: every write is refused, it gets no MCP tools, and it cannot open the event stream. Only an admin in an interactive browser session can mint one (**Settings → MCP Keys**, the **Ops (read-only)** scope choice); no key of any scope can. It remains a narrowing of its owner — if that admin is demoted or suspended the key stops working — so mint it under a dedicated service admin account and re-mint any ops key held by a departing admin. Every call it makes is attributed to the key in the audit log. The other bounded scope, `portal_delegate`, reaches exactly one route (exchanging an end-user email for a Workspace session, which requires an entitlement) and refuses everything else. See [Authentication](../api-reference/authentication.md#mcp-key-scopes).

## How do I know my agent is authenticating to Trinity as itself?

Check the MCP key panel on the agent's **Settings** tab. It reports a health state and offers a **Verify** action that probes the container's actual configuration and tells you whether it is carrying its own key, a foreign user key, another agent's key, an unknown key, or a duplicate entry. This matters because the agent-to-agent permission matrix only applies to agent-scoped keys — a container holding a *user*-scoped key operates with the owner's identity and bypasses the matrix. If something is wrong, **Regenerate** rotates and delivers a fresh key; Trinity also self-heals a missing or mismatched key on the agent's next start. See [MCP Server](../integrations/mcp-server.md).

## Can a skills repository push executable code to my whole fleet without me noticing?

Only if you enable fleet re-inject, and even then the supply chain is pinned. Skills carry executable scripts, so the bundled community source — which accepts public contributions — is pinned to a **tag**, not a branch head, and a tag that later resolves to a different commit is **refused** rather than adopted. Custom sources, whose write access you control, track a branch. Both automation settings (auto-sync, fleet re-inject) default to off, source URLs are locked to github.com, and registering or syncing a source is admin-and-human-only because it decides which repository your fleet executes code from. See [Skills and Playbooks](../automation/skills-and-playbooks.md).

## Is anything stopping a retention setting from wiping my history?

Yes, two independent controls. Retention windows have exactly one write path, which type- and range-validates every value all-or-nothing and audit-logs the change; the generic settings endpoint refuses those keys. And any sweep that would delete more than a fixed safety threshold (1,000 rows) of a single table **refuses** to run, logs an error, and raises an operator alarm — an admin must then approve it explicitly, bound to the exact window in force and single-use. Note the counter-intuitive part: garbage input always failed safe (retain forever); a *small valid number* is the dangerous input, which is why the approval gate, not the validation, is what actually protects you. "Reset to defaults" deliberately skips retention windows. See [Monitoring](../operations/monitoring.md).
