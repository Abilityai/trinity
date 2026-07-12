# Trinity FAQ — Credentials & Subscriptions

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## How do credentials work inside a Trinity agent?

Every agent keeps its credentials as files in its own container, following a simple pattern: `.env` is the source of truth (plain `KEY=VALUE` pairs), `.mcp.json.template` declares which credentials the agent needs using `${VAR}` placeholders, and `.mcp.json` is generated at runtime from the template plus `.env`. Trinity writes these files directly into the agent — credentials are injected, not passed around as loose environment variables. The Credentials tab reads the template to show you each required credential as configured or missing. See [Credential Management](../credentials/credential-management.md).

## How do I add or edit credentials on an agent?

Open the agent's detail page and click the **Credentials** tab. You'll see the credentials the agent requires, each marked configured (green) or missing (red). Add values one of three ways: manual entry (name, value, and service), bulk import (paste `.env`-style `KEY=VALUE` pairs), or import from an encrypted `.credentials.enc` backup. See [Credential Management](../credentials/credential-management.md).

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

Yes — the Credentials tab has OAuth buttons for Google, Slack, GitHub, and Notion. Clicking one redirects your browser to the provider's authorization page; after you approve, the callback returns to Trinity, which normalizes the token into `KEY=VALUE` form, writes it to the agent's `.env`, and regenerates `.mcp.json`. The agent never handles the OAuth flow itself — Trinity manages token acquisition and injection on its behalf. See [OAuth Credentials](../credentials/oauth-credentials.md).

## What are subscription credentials?

A subscription credential is a Claude Max or Pro subscription token registered with Trinity so that multiple agents can share it. You register it once in the Settings page (Subscriptions section); Trinity stores it encrypted with AES-256-GCM and injects it into assigned agents as an environment variable. Expanding a subscription row shows which agents use it, with assign and unassign controls. See [Subscription Credentials](../credentials/subscription-credentials.md).

## Do I have to assign a subscription to every new agent manually?

No — new agents get a subscription automatically via round-robin assignment. Trinity picks the subscription with the fewest assigned agents, breaking ties alphabetically. You can always reassign manually from the Subscriptions section of Settings, and round-robin considers only agent count, not how busy each agent actually is. See [Subscription Credentials](../credentials/subscription-credentials.md).

## What happens when my agent hits a rate limit on its Claude subscription?

If auto-switch is enabled (it is on by default), Trinity detects the rate-limit (429) or auth-class failure and automatically switches the agent to a different subscription. The new token is applied via a hot-reload of the running container — no recreate — so in-flight executions keep running. You can turn auto-switch off in the Subscriptions section of Settings. One caveat: auto-switch depends on failure detection, so if the agent doesn't surface a 429 or auth-class error through standard channels, no switch is triggered. See [Subscription Credentials](../credentials/subscription-credentials.md).

## If I rotate a subscription token, will it interrupt agents mid-task?

No. Re-registering a subscription with a fresh token pushes the new token to every running agent on that subscription via hot-reload, and reassigning an agent to a different subscription swaps the token in place the same way. Turns already in flight finish on the old token; the next turn picks up the new one. Container recreation is only needed for image, template, or auth-*mode* changes (such as switching between subscription and API key), and on older agent base images that lack the hot-reload endpoint the switch falls back to a recreate. See [Subscription Credentials](../credentials/subscription-credentials.md).

## Should I use a per-agent GitHub PAT or the platform-wide one?

Trinity stores one platform-wide GitHub PAT that every agent inherits by default — its reach is whatever the token's owner can reach on GitHub. Set a per-agent PAT override when an agent needs to push to a repository the platform token can't see, or when you want to limit blast radius by giving each agent its own narrowly-scoped token. Per-agent PATs are validated when you set them and stored encrypted; clearing the override reverts the agent to the platform PAT. When the platform PAT changes, Trinity propagates the new token to every running agent within seconds — agents with their own override are skipped. See [GitHub PAT Setup](../integrations/github-pat-setup.md).

## Are my credentials ever stored in Trinity's database?

Agent credentials are not — the model is file injection into the agent's container, never plaintext in the database. There is one deliberate exception: tokens that drive long-lived background processes outside any container, such as Slack, Telegram, and WhatsApp bot tokens, shared subscription tokens, payment credentials, and GitHub PATs. These must be persisted, so they are stored only as AES-256-GCM encrypted envelopes; plaintext persistence is forbidden. OAuth tokens acquired through the provider flows are held in Redis with persistence enabled. See [Credential Management](../credentials/credential-management.md).

## How do I rotate the platform encryption key?

The encryption key (`CREDENTIAL_ENCRYPTION_KEY`) rotates online, with no downtime and no data loss. In short: back up the database, generate a new key, set it as the primary while moving the old key to `CREDENTIAL_ENCRYPTION_KEY_SECONDARY` (a decrypt-only fallback), and restart the backend — existing secrets keep decrypting via the old key while all new writes use the new one. Then run the re-encryption script to sweep every database-persisted token onto the new key, and finally remove the secondary key. Per-agent `.credentials.enc` files re-encrypt on their next credential operation. See [Credential Management](../credentials/credential-management.md).

## Why is the Register Subscription button disabled in Settings?

The subscription feature requires `CREDENTIAL_ENCRYPTION_KEY` to be set in the platform's `.env` — without it, tokens can't be encrypted, so a warning banner appears on the Settings page, the Register button is disabled, and the API returns 503. The key is auto-generated by `start.sh` on fresh deployments, so this usually means an upgraded or hand-configured install is missing it. You can check with `GET /api/subscriptions/encryption-status`, then add the key and restart the backend. See [Subscription Credentials](../credentials/subscription-credentials.md).

## How do I move an agent's credentials to another Trinity instance?

Export the credentials to a `.credentials.enc` file (from the Credentials tab or the `export_credentials` MCP tool), bring that file to the agent on the target instance, and import it there (the "From encrypted backup" option or the `import_credentials` MCP tool). The catch is the encryption key: the file only decrypts if the target instance uses the same `CREDENTIAL_ENCRYPTION_KEY` as the source. An admin can retrieve the key from the source instance (via the encryption-key API endpoint or the `get_credential_encryption_key` MCP tool) and configure it on the target before importing. See [Credential Management](../credentials/credential-management.md).
