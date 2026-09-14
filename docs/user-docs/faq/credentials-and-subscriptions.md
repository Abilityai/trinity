# Trinity FAQ — Credentials & Subscriptions

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## How do credentials work inside a Trinity agent?

Every agent keeps its credentials as files in its own container, following a simple pattern: `.env` is the source of truth (plain `KEY=VALUE` pairs), `.mcp.json.template` declares which credentials the agent needs using `${VAR}` placeholders, and `.mcp.json` is generated at runtime from the template plus `.env`. Trinity writes these files directly into the agent — credentials are injected, not passed around as loose environment variables. The Credentials tab reads the template to show you each required credential as configured or missing. See [Credential Management](../credentials/credential-management.md).

## How do I add or edit credentials on an agent?

Open the agent's detail page and click the **Credentials** tab. You'll see the credentials the agent requires, each marked configured (green) or missing (red). Add values one of four ways: the setup checklist (per-variable inputs with descriptions and setup links), manual entry (name, value, and service), bulk import (paste `.env`-style `KEY=VALUE` pairs), or import from an encrypted `.credentials.enc` backup. See [Credential Management](../credentials/credential-management.md).

## Can I change credentials on a running agent without restarting it?

Yes — credential updates hot-reload. When you paste or edit credentials on a running agent, Trinity updates the `.env` file and regenerates `.mcp.json` immediately; no restart is needed. See [Credential Management](../credentials/credential-management.md).

## What is the .credentials.enc file, and is it safe to commit to git?

`.credentials.enc` is an encrypted backup of an agent's credentials, produced by the export function and encrypted with AES-256-GCM using the platform's encryption key. Because only the ciphertext is stored, the file is safe to keep in the agent's git repository. Export captures the full injected credential set — every allow-listed credential file present in the agent, text and binary alike — not just `.env` and `.mcp.json`. Import decrypts the archive and re-validates every path against the same injection policy on the way in. See [Credential Management](../credentials/credential-management.md).

## Do I need to re-enter credentials every time an agent restarts?

No. Credential files live in the agent's home directory, which sits on a persistent volume that survives restarts and container recreation. There is also an auto-import path: if an agent starts up with a `.credentials.enc` file but no `.env` — for example, a fresh agent created from a repository that has the encrypted backup committed — Trinity automatically decrypts the backup and injects the credentials on startup. See [Credential Management](../credentials/credential-management.md).

## What kinds of credential files can I inject into an agent?

Injection accepts a curated allowlist of credential file types, not just `.env`. Allowed: the core files (`.env`, `.credentials.enc`, `.mcp.json` — the last is also content-validated), Google Cloud SDK credentials under `.config/gcloud/`, a Kubernetes `.kube/config`, TLS certificate and key material (`*.pem`, `*.key`, `*.crt`, `*.cert`, `*.p12`, `*.pfx`), and SSH key pairs (`.ssh/id_*` only). Anything outside the allowlist is rejected. See [Credential Management](../credentials/credential-management.md).

## Why does Trinity reject some files when I try to inject them?

A deny-list takes precedence over the allowlist, and it blocks anything that gets executed or sourced when the agent starts: shell startup files (`.bashrc`, `.profile`, `.zshrc`, and friends), agent instruction files (`CLAUDE.md`, `AGENTS.md`, anything under `.claude/`), `.mcp.json.template`, `.ssh/authorized_keys` and `.ssh/config`, anything under `.git/` or `bin/`, plus absolute paths and `..` traversal. This is deliberate — it keeps credential injection from becoming a way to run arbitrary code in the container. If a legitimate credential file is rejected, place it at one of the allowed paths instead. See [Credential Management](../credentials/credential-management.md).

## Can I inject binary credentials like certificates or keystores?

Yes. Binary credential files — certificates, keystores, service-account bundles — round-trip as base64 via the `files_b64` field on the inject endpoint, and the encrypted export format carries binary and text files alike. So a `.p12` keystore or a PEM bundle survives export, backup, and import intact. See [Credential Management](../credentials/credential-management.md).

## Can I connect an agent to Google, Slack, GitHub, or Notion without pasting API keys?

Not today. Trinity ships a small OAuth helper API for those four providers that reports which ones are configured (from client IDs and secrets set as backend environment variables) and builds the provider's authorization URL, but it does not complete the exchange: there is no callback handler and no OAuth button on the Credentials tab, so approving access at the provider never turns into a stored token. Obtain the provider token yourself and add it to the agent as a `KEY=VALUE` credential; the agent's `.mcp.json.template` picks it up through `${VAR}` placeholders. The one complete OAuth flow is the platform-level Slack workspace install (**Install to Workspace** under Slack Integration settings), which is separate from per-agent credentials. See [OAuth Credentials](../credentials/oauth-credentials.md).

## What are subscription credentials?

A subscription credential is a Claude Max or Pro token (from `claude setup-token`, prefix `sk-ant-oat01-`) registered once with Trinity so that several agents can share it. Register it under **Settings → Integrations → Claude Subscriptions** with a name and type; Trinity stores it AES-256-GCM encrypted and injects it into assigned agents as an environment variable. The table shows each subscription's agent count and a **Pressure** cell, and expanding a row shows its assigned agents with assign and unassign controls plus live usage. Registering a name that already exists replaces its token and hot-reloads every running agent on it. See [Subscription Credentials](../credentials/subscription-credentials.md).

## Do I have to assign a subscription to every new agent manually?

No — every new agent (system agents excepted) is auto-assigned. Trinity skips any subscription that failed in the last 2 hours, then picks the one with the most cached headroom: furthest from whichever of its 5-hour or 7-day limits is nearer. Only when no usable headroom reading exists does it fall back to fewest-agents-first with an alphabetical tie-break. You can reassign at any time by expanding the row under **Settings → Integrations → Claude Subscriptions** or from the auth badge dropdown in the agent's header; only the agent's owner or an admin can change an assignment. See [Subscription Credentials](../credentials/subscription-credentials.md).

## What happens when my agent hits a rate limit on its Claude subscription?

With auto-switch on (the default), the turn is not lost. If the subscription is already known to be refusing before the turn starts — a fresh provider reading says so, or it hit a limit in the last 2 hours with nothing fresher to the contrary — Trinity switches the agent before dispatching, so the first message after a limit does not burn a failed attempt. If the provider refuses mid-turn, Trinity records the failure event, switches the agent, hot-reloads the new token into the running container (no recreate, so in-flight work keeps running), and re-issues the turn once on the new subscription. The destination is chosen by cached headroom: candidates that failed in the last 2 hours are skipped unless they have provably recovered, and the survivors are ranked furthest from the nearer 5h/7d wall in 10-point bands, with fewest agents as the tie-break. Every switch is logged as an activity on the agent and raises a high-priority notification saying what failed and why the destination was chosen. The toggle lives under **Settings → Integrations → Claude Subscriptions**; auto-switch still depends on the refusal surfacing as a rate-limit or auth-class error. See [Subscription Credentials](../credentials/subscription-credentials.md).

## What happens when every subscription is rate-limited?

With **Fall back to the platform API key** on (the default) and a platform Anthropic API key configured, Trinity clears the agent's subscription assignment and restarts it on the API key so the turn completes, with a `Switched to the platform API key` notification. This is a permanent reassignment, not a temporary redirect — reassign a subscription deliberately when you want spend back on it. With the fallback off, or with no platform key configured (the toggle warns when there isn't one), the turn fails and the error names the earliest known reset time. See [Subscription Credentials](../credentials/subscription-credentials.md).

## How do I see how much of a subscription's limit is already used?

The **Pressure** column under **Settings → Integrations → Claude Subscriptions** gives the one-glance state: `5h: 42% · 7d: 88%` when a provider reading under 30 minutes old exists, `rate-limited` in red, an amber event count when there were failures but no fresh reading, `ok`, or `—` when usage couldn't be read. Expand the row for the **Usage** block: live headroom per window with its reset time, whether the figure is `actual (Anthropic)` from a provider probe or `observed` (Trinity's own estimate from recorded consumption), observed tokens, cost and messages for the last 5h and 7d, the 24h failure events split into rate-limit and auth, and a **Refresh** button that probes now (re-clicking within 60 seconds serves the cached reading). **Show per-agent breakdown** lists which agents burn the quota so you can see who to move or stagger. Admins see the same figures on the Dashboard as the **Subscription pressure** Grid tile and the per-agent **sub limit** / **sub 429s** / **sub auth** chips on tiles and List rows. See [Subscription Credentials](../credentials/subscription-credentials.md) and [Dashboard](../operations/dashboard.md).

## Does reading subscription headroom cost me quota?

A little. The 5h/7d percentages come from a provider probe that spends roughly a dozen tokens of the subscription's own quota (visible in the Anthropic console). The **Check subscription quota automatically** toggle (on by default) governs every probe Trinity runs on its own: the ambient refresh while a dashboard or the Settings page is open (at most one probe per 15 minutes per subscription), the 5-minute re-check of a subscription still marked rate-limited, and the hourly sample behind the weekly-limit alert. Turn it off and headroom only updates when you click **Refresh** — the recovery re-check and the weekly alerts stop too. Every probe is kept as a history row for 30 days, so utilization trends are answerable over the API (`GET /api/subscriptions/{id}/headroom/history`); viewing history never probes, and no page charts it yet. See [Subscription Credentials](../credentials/subscription-credentials.md).

## Can Trinity warn me before a subscription hits its weekly limit?

Yes. **Warn me before the weekly limit** (under **Settings → Integrations → Claude Subscriptions**) raises an operator-queue alert when a subscription passes a set share of its 7-day window — default 75, allowed range 50–99, or 0 to turn it off. Trinity samples each subscription about once an hour and files one alert per subscription per weekly window into Operations → Needs Response; the urgency follows your burn rate (low if you're on track to finish the window under 100%, high if you'll run out before the reset), and a second, escalating alert fires at 90% or at your threshold if that is higher. When every registered subscription is past the threshold, a single fleet-wide high alert replaces the per-subscription ones. The status line under the control says whether the alert is actually running or why it isn't — no subscriptions registered, threshold set to 0, or automatic quota checks off. See [Subscription Credentials](../credentials/subscription-credentials.md).

## How does Trinity know a rate-limited subscription has recovered?

The rate-limited badge clears as soon as a provider probe says the subscription is being served again; while automatic quota checks are on, Trinity re-probes any subscription still wearing the badge every 5 minutes, so it never has to wait out the 2-hour failure window. While the subscription is limited, its row says when the limit returns. Two things are deliberately never shown as a rate limit: a rejected token is recorded as an **auth** event — the fix is to re-register the token — and the provider's own "approaching the limit" warning tier counts as pressure (the `near` chip on the Dashboard), not as a limit. Past auth events alone do not trigger a pre-dispatch switch, since a credential problem may be shared by every subscription; a turn the provider actually rejects still switches and retries. See [Subscription Credentials](../credentials/subscription-credentials.md).

## If I rotate a subscription token, will it interrupt agents mid-task?

No. Re-registering a subscription with a fresh token pushes the new token to every running agent on that subscription via hot-reload, and reassigning an agent to a different subscription swaps the token in place the same way. Turns already in flight finish on the old token; the next turn picks up the new one. Container recreation is only needed for image, template, or auth-*mode* changes (such as switching between subscription and API key), and on older agent base images that lack the hot-reload endpoint the switch falls back to a recreate. See [Subscription Credentials](../credentials/subscription-credentials.md).

## Why is the Register Subscription button disabled in Settings?

The subscription feature requires `CREDENTIAL_ENCRYPTION_KEY` to be set in the platform's `.env` — without it, tokens can't be encrypted, so a warning banner appears on the Settings page, the Register button is disabled, and the API returns 503. The key is auto-generated by `start.sh` on fresh deployments, so this usually means an upgraded or hand-configured install is missing it. You can check with `GET /api/subscriptions/encryption-status`, then add the key and restart the backend. See [Subscription Credentials](../credentials/subscription-credentials.md).

## Should I use a per-agent GitHub PAT or the platform-wide one?

Trinity stores one platform-wide GitHub PAT that every agent inherits by default — its reach is whatever the token's owner can reach on GitHub. Set a per-agent PAT override when an agent needs to push to a repository the platform token can't see, or when you want to limit blast radius by giving each agent its own narrowly-scoped token. Per-agent PATs are validated when you set them and stored encrypted; clearing the override reverts the agent to the platform PAT. When the platform PAT changes, Trinity propagates the new token to every running agent within seconds — agents with their own override are skipped. See [GitHub PAT Setup](../integrations/github-pat-setup.md).

## Can I use my own GitHub token instead of the platform-wide one?

Yes. Store a personal access token in your own Settings, and Trinity uses it when you create agents — so you're not confined to the admin's platform-wide token and its repo scope. At creation the token resolves in tiers: a per-agent PAT override if one is set, otherwise your personal token, otherwise the platform-wide global PAT. Your personal token is validated when you save it, stored encrypted, and read live at creation time. See [GitHub PAT Setup](../integrations/github-pat-setup.md).

## Do I need a GitHub token to build an agent from a public repo?

No. A `github:owner/repo` template that points at a public repository clones anonymously, with no personal access token required. This is source-mode only — the agent can read and run the template but can't push its changes back or use write-dependent features (the working-branch sync heartbeat, fork-to-own) until you add a token. See [Creating Agents](../agents/creating-agents.md).

## Are my credentials ever stored in Trinity's database?

Agent credentials are not — they are injected as files into the agent's container and never persisted as plaintext rows. What the database does hold is always an AES-256-GCM encrypted envelope: tokens that drive long-lived processes outside any container (Slack, Telegram, and WhatsApp bot tokens, shared subscription tokens, payment credentials, per-agent and per-user GitHub PATs), Credential Vault entries, and the platform's own credentials — the Anthropic API key and platform GitHub PAT, the Slack app token, client secret, and signing secret, and the Google API key — which are stored under `<key>_encrypted` names, never in cleartext. An install upgraded from an older release re-encrypts a leftover cleartext row the first time it is read and deletes the cleartext copy. See [Credential Management](../credentials/credential-management.md).

## I upgraded Trinity — do I need to rotate my platform API keys?

You should. Encryption at rest for platform-level credentials protects the database going forward only; backups taken before the upgrade still hold those values in plaintext, so rotate the Anthropic API key, the platform GitHub PAT, the Slack app token, client secret, and signing secret, and the Google API key once you are on the new release. Save the new values through their dedicated settings sections (**Settings → Integrations → API Keys** and **Settings → Integrations → Slack Integration**): the generic `PUT /api/settings/{key}` route answers 422 for those keys — and for any credential-shaped key (`*_api_key`, `*_token`, `*_secret`, `*_pat`, `*_password`, `*_credentials`) — and names the route to use instead. The Slack client ID is the one reviewed exemption, because it is a public identifier that appears verbatim in the authorize URL. Runbook: [Secret settings encryption](../../migrations/SECRET_SETTINGS_ENCRYPTION_2026-08.md). See [Credential Management](../credentials/credential-management.md).

## What is the Credential Vault, and how does an agent use it?

The vault is a platform-level store of named, encrypted credentials that an admin grants to specific agents, so an agent fetches a shared secret by name at runtime instead of carrying its own injected copy — nothing is written into its `.env`. Entries and grants are managed at **Settings → Vault** (the tab appears only where the vault is enabled; it requires an entitlement) by an administrator signed in interactively — API keys of any scope are refused — and after a platform encryption-key rotation the tab's key-rotation maintenance control re-encrypts every entry. From inside an agent, two MCP tools exist in every build: `list_available_credentials()` returns the names and kinds this agent has been granted (never values), and `fetch_credential(name)` returns one granted value; an ungranted or unknown name comes back as `not_granted`, and where the vault isn't available the tools say so instead of erroring. Once a value has been fetched, Trinity scrubs it out of everything it persists from the turn — transcript, execution log, response and error columns, notifications, channel reports — replacing every occurrence with `***REDACTED***`; that covers values fetched in the last 24 hours and what the platform stores, not the agent's own in-container session files. See [Credential Management](../credentials/credential-management.md#credential-vault).

## How do I rotate the platform encryption key?

The encryption key (`CREDENTIAL_ENCRYPTION_KEY`) rotates online, with no downtime and no data loss. In short: back up the database, generate a new key, set it as the primary while moving the old key to `CREDENTIAL_ENCRYPTION_KEY_SECONDARY` (a decrypt-only fallback), and restart the backend — existing secrets keep decrypting via the old key while all new writes use the new one. Then run the re-encryption script to sweep every database-persisted token onto the new key, and finally remove the secondary key. Per-agent `.credentials.enc` files re-encrypt on their next credential operation. See [Credential Management](../credentials/credential-management.md).

## How do I move an agent's credentials to another Trinity instance?

Export the credentials to a `.credentials.enc` file (from the Credentials tab or the `export_credentials` MCP tool), bring that file to the agent on the target instance, and import it there (the "From encrypted backup" option or the `import_credentials` MCP tool). The catch is the encryption key: the file only decrypts if the target instance uses the same `CREDENTIAL_ENCRYPTION_KEY` as the source. An admin can retrieve the key from the source instance (via the encryption-key API endpoint or the `get_credential_encryption_key` MCP tool) and configure it on the target before importing. See [Credential Management](../credentials/credential-management.md).

## How do I know which credentials an agent actually needs?

The **Credentials** tab shows a per-variable checklist built from the agent's own live `template.yaml` — so a forked or hand-edited agent reports its real requirements, not its original template's. Each row shows what the credential is for, whether it is currently set (probed from the agent's `.env`; **names only**, values are never read or transmitted), and a link to where you obtain it. You can fill values in directly on the checklist. It renders for a stopped agent too, with the live-status column honestly marked unavailable. Reading it is owner-only and human-only. See [Credential Management](../credentials/credential-management.md).

## How does a template declare the credentials it needs?

Two sibling keys in `template.yaml`. `credentials:` lists the variable **names** only and is frozen that way, so older Trinity versions can still read a newer template. The optional `credential_setup:` decorates each one with `title`, `description`, `required`, `secret`, `format`, `setup_url`, and a non-secret `default`. Every `credential_setup:` entry must name a variable that `credentials:` declares — an entry naming anything else is dropped with a named error while its valid siblings survive. `secret` defaults to true and `setup_url` must be HTTPS with no embedded userinfo, so a credential is masked until an author says otherwise and a setup link can't impersonate a vendor domain. See [Credential Management](../credentials/credential-management.md).

## I rotated the platform GitHub token — do I need to restart my agents?

No. Saving a new platform PAT propagates it to every running agent that uses it within seconds: the container environment, the workspace `.env`, and the git credential configuration are all updated, so the rotation takes effect for the agent's next push. Agents carrying their **own** per-agent token are deliberately untouched by a global rotation — update those on the agent's Git tab. See [GitHub PAT Setup](../integrations/github-pat-setup.md).
