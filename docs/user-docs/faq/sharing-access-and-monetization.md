# Trinity FAQ — Sharing, Access & Monetization

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## How do I share an agent with a teammate?

Open the agent detail page, click the **Access** tab (visible to the owner), enter your teammate's email, and click **Add operator**. The email is also added to the platform login whitelist, so they can sign in with an email verification code. Their row shows **Active** if they already have a Trinity account, or **Pending** ("Invited — no account yet") until their first login. Click **Remove** on a row to revoke access at any time. See [Agent Sharing](../sharing-and-access/agent-sharing.md).

## What can my teammate do with an agent I've shared with them?

Shared operators get interact-level access: they can chat with the agent, run tasks, and view its files and logs. They cannot modify the agent's configuration, credentials, schedules, or permissions, and they cannot delete or re-share it — those actions stay with the owner (and admins, who have full access to all agents). Every API endpoint checks ownership or sharing status before granting access. See [Agent Sharing](../sharing-and-access/agent-sharing.md).

## Why can't my teammate see my past conversations with a shared agent?

That's by design. Shared users see only their own chat messages with an agent — each person's conversation history is private to them. Only admins can see all messages across users. If your teammate needs the content of a past conversation, share it with them directly. See [Agent Sharing](../sharing-and-access/agent-sharing.md).

## What's the difference between the Access tab and the Sharing tab?

The **Access** tab manages Trinity operators — platform users (your teammates) who log into the Trinity UI and get interact-level access. The **Sharing** tab manages external clients — people without a Trinity account who reach the agent through Slack, Telegram, WhatsApp, voice calls, or public links, identified by verified email. The Sharing tab also holds the Restricted/Open access switch, pending access requests, channel configuration, a read-only client roster, and the public links and file sharing panels. See [Agent Sharing](../sharing-and-access/agent-sharing.md).

## What user roles does Trinity have and what can each one do?

Trinity has four hierarchical roles: **admin** > **creator** > **operator** > **user**. Admins can create agents and manage every agent on the platform; creators can create agents and manage their own; operators cannot create agents but can work with agents assigned to them; users cannot create or manage agents but can chat with agents shared with them. Higher roles inherit all permissions of lower ones. See [Roles and Permissions](../getting-started/roles-and-permissions.md).

## Who can create agents?

Creating agents requires the **creator** role or above. If a teammate needs to create agents, an admin can promote them: go to **Settings** → **User Management**, find the user in the table, and pick a new role from the dropdown — the change takes effect on their next request. You cannot change your own role, and admins cannot demote themselves. See [Roles and Permissions](../getting-started/roles-and-permissions.md).

## Why am I getting an agent quota error when I create an agent?

Each role has a limit on how many agents it can own: by default creators get 10, operators 3, and users 1 (admins are always unlimited). When you're at your limit, agent creation is rejected with HTTP 429 and a "Agent quota exceeded" message. Admins can raise the per-role limits under **Settings** → **Agent Quotas** (enter `0` for unlimited). Redeploying an agent you already own doesn't count against your quota, and system agents are excluded from the count. See [Agent Quotas](../operations/agent-quotas.md).

## What role does a new user get the first time they log in?

The role comes from their email whitelist entry: each whitelisted email carries a default role that is assigned on first login, and it falls back to the basic **user** role if none was set. Adding someone through an agent's Access tab or approving their access request whitelists them as a **user** — a chat-only grant that never silently promotes anyone. An admin can promote them afterwards via **Settings** → **User Management**. See [Roles and Permissions](../getting-started/roles-and-permissions.md).

## Can people sign up for my Trinity instance on their own?

Not unless you explicitly allow it — public self-signup is off by default, and the email whitelist stays the real access gate. The unauthenticated access-request endpoint returns 403 and tells the person to ask an administrator to whitelist their email. An admin can opt in via the `PUBLIC_ACCESS_REQUESTS_ENABLED` environment variable or the matching system setting; when enabled, self-signups are auto-whitelisted with the basic **user** role. See [Roles and Permissions](../getting-started/roles-and-permissions.md).

## How does someone request access to my agent, and what happens when I approve it?

When an agent is in **Restricted** mode and an unknown user with a verified email messages it — from any channel — they see "Your access request is pending approval" and a request appears under the Restricted/Open switch in the agent's **Sharing** tab. Click **Approve** to add their email to the share list, which admits them on every channel at once; click **Deny** to reject silently (the agent's existence is not confirmed). If the request came in over Telegram, Slack, or WhatsApp, Trinity automatically messages the requester on that same channel to confirm access; a delivery failure never rolls back the approval. See [Access Control](../sharing-and-access/access-control.md).

## What's the difference between Restricted and Open access on an agent?

It's a single per-agent switch under "Who can chat with this agent?" on the Sharing tab. **Restricted** (the default) means only the owner, admins, and explicitly approved emails can chat — everyone else generates a pending access request. **Open** means anyone with a verified email can chat immediately. Separately, admins can set whether new agents require verified email at all (**Settings** → **General** → "Require verified email for new agents") — it's on out of the box, applies only at creation time, and owners can still override it per agent. See [Access Control](../sharing-and-access/access-control.md).

## What's the difference between sharing an agent and creating a public link?

Sharing grants a Trinity account holder interact access through the logged-in UI. A public link is a shareable URL that lets anyone chat with the agent without logging in — optionally with email verification, rate limits, and a custom welcome message. Public-link conversations persist across page refreshes, and logged-in Trinity users get a history dropdown to resume past sessions. Create links under **Sharing** → **Distribution** → **Public links** on the agent detail page. See [Public Links](../sharing-and-access/public-links.md).

## How do I keep a growing fleet of agents organized?

Use tags and saved views. Tag agents from the agent detail page (or via API/MCP); tags show as colored badges on agent tiles, group agents into tag clouds on the Dashboard, and drive filters on the Agents page that persist as you navigate. For filter combinations you use often, create a **System View** — a saved filter of tags plus other criteria that persists across sessions. See [Tags and Organization](../sharing-and-access/tags-and-organization.md).

## Can I manage Trinity from my phone?

Yes. Trinity ships a mobile-optimized PWA at `/m` (for example `http://your-domain.com/m`) — install it via **Add to Home Screen** for a native-app feel. It has three tabs: **Agents** (list, chat, toggle autonomy, send tasks), **Ops** (respond to operator queue items and notifications), and **System** (system-level controls). It's built for quick interactions like answering an agent's question or flipping autonomy on and off. See [Mobile Admin](../sharing-and-access/mobile-admin.md).

## Can I charge people to chat with my agent?

Yes, via per-request payments using the Nevermined x402 protocol. In the agent's payment settings, enter your Nevermined API key, Agent ID, and Plan ID, then enable payments — the agent gets a paid endpoint at `POST /api/paid/{agent_name}/chat`. Callers without payment credentials receive HTTP 402 with payment instructions, buy credits on the Nevermined checkout page, and retry with a `payment-signature` header; Trinity verifies the payment, deducts credits, and routes the message to the agent. See [Nevermined x402 Payments](../integrations/nevermined-payments.md).

## How do I set the price for my paid agent, and can I test payments without real money?

Pricing lives in your Nevermined plan (which defines credit pricing and allocation — one plan per agent); on the Trinity side you set how many credits each chat request burns (`credits_per_request`, default 1, minimum 1). For testing, configure the agent with the `sandbox` environment (the default), which runs on the Base Sepolia testnet; switch to the `live` environment for real payments on Base mainnet. Anyone can check an agent's payment requirements without authentication via `GET /api/paid/{agent_name}/info`. See [Nevermined x402 Payments](../integrations/nevermined-payments.md).

## How can I see the payments my agent has received?

Payment history is available per agent via `GET /api/nevermined/agents/{name}/payments`, or through the `get_nevermined_payments` MCP tool. If a payment settles incorrectly, admins can list failed settlements and retry them manually — settlement failures are never retried automatically. See [Nevermined x402 Payments](../integrations/nevermined-payments.md).

## How does my agent share a file with someone outside Trinity, and how do I revoke the link?

First enable file sharing in the agent's **Sharing** tab (a restart mounts the publish volume). The agent then drops a file into `/home/developer/public/` and calls the `share_file` MCP tool, which returns a signed download URL valid for 7 days — it works anywhere a link works: web, Slack, Telegram, WhatsApp, email. Limits: 50 MB per file, 500 MB per agent across active shares, and executables are blocked. Active shares appear in the File Sharing panel with size, expiry, and download count; click **Revoke** to kill a link immediately (downloads then return `410 Gone`). See [Agent Files](../agents/agent-files.md).
