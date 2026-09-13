# Trinity FAQ — Sharing, Access & Monetization

> Part of the [Trinity FAQ](README.md). Short, grounded answers with links to the full documentation.

## How do I share an agent with a teammate?

Open the agent detail page, click the **Access** tab (visible to the owner), enter your teammate's email, and click **Add operator**. The email is also added to the platform login whitelist, so they can sign in with an email verification code. Their row shows **Active** if they already have a Trinity account, or **Pending** ("Invited — no account yet") until their first login; each row also carries an off-by-default toggle that lets the agent message that person proactively. Sharing puts the agent on their Workspace roster as well. Click **Remove** on a row to revoke access at any time. See [Agent Sharing](../sharing-and-access/agent-sharing.md).

## What can my teammate do with an agent I've shared with them?

Shared operators get interact-level access: they can chat with the agent, run tasks, and view its files and logs. They cannot modify the agent's configuration, credentials, schedules, or permissions, and they cannot delete or re-share it — those actions stay with the owner (and admins, who have full access to all agents). Every API endpoint checks ownership or sharing status before granting access. See [Agent Sharing](../sharing-and-access/agent-sharing.md).

## Why can't my teammate see my past conversations with a shared agent?

That's by design. Shared users see only their own chat messages with an agent — each person's conversation history is private to them. Only admins can see all messages. If your teammate needs the content of a past conversation, share it with them directly. See [Agent Sharing](../sharing-and-access/agent-sharing.md).

## What's the difference between the Access tab and the Sharing tab?

The **Access** tab manages Trinity operators — platform users (your teammates) who log into the Trinity UI and get interact-level access. The **Sharing** tab manages external clients — people without a Trinity account who reach the agent through Slack, Telegram, WhatsApp, voice calls, public links, or the Workspace, identified by verified email. The Sharing tab also holds the Restricted/Open access switch with pending access requests, the **Public chat model** and **Additional instructions** that apply only to outside audiences (public links, channels, paid chat — never your own chats or schedules), channel configuration, a read-only client roster, and the public links and file sharing panels. See [Agent Sharing](../sharing-and-access/agent-sharing.md).

## What user roles does Trinity have and what can each one do?

Trinity has four hierarchical roles: **admin** > **creator** > **operator** > **user**. Admins can create agents and manage every agent on the platform; creators can create agents and manage their own; operators cannot create agents but can work with agents assigned to them; users cannot create or manage agents but can chat with agents shared with them. Higher roles inherit all permissions of lower ones. See [Roles and Permissions](../getting-started/roles-and-permissions.md).

## Who can create agents?

Creating agents requires the **creator** role or above. If a teammate needs to create agents, an admin can promote them: go to **Settings** → **Access** → **User Management**, find the user in the table, and pick a new role from the dropdown — the change takes effect on their next request. You cannot change your own role, and admins cannot demote themselves. See [Roles and Permissions](../getting-started/roles-and-permissions.md).

## Why am I getting an agent quota error when I create an agent?

Each role has a limit on how many agents it can own: by default creators get 10, operators 3, and users 1 (admins are always unlimited). When you're at your limit, agent creation is rejected with HTTP 429 and a "Agent quota exceeded" message. Admins can raise the per-role limits under **Settings** → **Agent Quotas** (enter `0` for unlimited). Redeploying an agent you already own doesn't count against your quota, and system agents are excluded from the count. See [Agent Quotas](../operations/agent-quotas.md).

## What role does a new user get the first time they log in?

The role comes from their email whitelist entry: each whitelisted email carries a default role that is assigned on first login, and it falls back to the basic **user** role if none was set. Adding someone through an agent's Access tab or approving their access request whitelists them as a **user** — a chat-only grant that never silently promotes anyone. An admin can promote them afterwards via **Settings** → **Access** → **User Management**. See [Roles and Permissions](../getting-started/roles-and-permissions.md).

## Can people sign up for my Trinity instance on their own?

Not unless you explicitly allow it — public self-signup is off by default, and the email whitelist stays the real access gate. The unauthenticated access-request endpoint returns 403 and tells the person to ask an administrator to whitelist their email. An admin can opt in via the `PUBLIC_ACCESS_REQUESTS_ENABLED` environment variable or the matching system setting; when enabled, self-signups are auto-whitelisted with the basic **user** role. See [Roles and Permissions](../getting-started/roles-and-permissions.md).

## How does someone request access to my agent, and what happens when I approve it?

When an agent is in **Restricted** mode and an unknown user with a verified email messages it — from any channel — they see "Your access request is pending approval" and a request appears under the Restricted/Open switch in the agent's **Sharing** tab. Click **Approve** to add their email to the share list, which admits them on every channel at once; click **Deny** to reject silently (the agent's existence is not confirmed). If the request came in over Telegram, Slack, or WhatsApp, Trinity automatically messages the requester on that same channel to confirm access; a delivery failure never rolls back the approval. See [Access Control](../sharing-and-access/access-control.md).

## What's the difference between Restricted and Open access on an agent?

It's a single per-agent switch under "Who can chat with this agent?" on the Sharing tab. **Restricted** (the default) means only the owner, admins, and explicitly approved emails can chat — everyone else generates a pending access request. **Open** means anyone with a verified email can chat immediately. Separately, admins can set whether new agents require verified email at all (**Settings** → **General** → "Require verified email for new agents") — it's on out of the box, applies only at creation time, and owners can still override it per agent. See [Access Control](../sharing-and-access/access-control.md).

## What's the difference between sharing an agent and creating a public link?

Sharing grants a Trinity account holder interact access through the logged-in UI. A public link is a shareable URL that lets anyone chat with the agent without logging in — optionally with email verification, rate limits, and a custom welcome message. Public-link conversations persist across page refreshes, and logged-in Trinity users get a history dropdown to resume past sessions. Create links under **Sharing** → **Distribution** → **Public links** on the agent detail page. See [Public Links](../sharing-and-access/public-links.md).

## What is the Workspace?

The Workspace at `/workspace` is the signed-in chat app for the people you share agents with — and for you. It ships in every build (the old `/portal` path redirects there), and it is the one surface where a conversation keeps the agent's working memory between turns, where every (you, agent) pair has a pinned **Main** chat plus as many named chats as you like, and where several agents can work one thread together. Platform users open it from the nav — it opens in its own browser tab, and your platform session is your Workspace session; external clients sign in with a 6-digit email code, get no platform account, and see only the agents shared with their address. Clicking an agent opens the chat you were last in with it — Main when there is no other. See [Workspace](../sharing-and-access/workspace.md).

## How do I bring a second agent into a conversation?

Type `@` and the other agent's name in an existing one-to-one chat. The Workspace opens a **room** containing both agents and posts your message there, leaving the original chat untouched. Inside a room, **+ Add agent** (or mentioning an agent that isn't yet a participant) recruits it — only a person can do that, never another agent — and `@` keeps working there while the `/` playbook picker is not offered in rooms. An `@name` that isn't one of your agents stays plain text. Multi-agent chat is part of the open-source platform — it used to be an enterprise capability, and against an older backend that lacks it the picker is single-select and mentions stay ordinary text. See [Workspace](../sharing-and-access/workspace.md#bringing-in-another-agent).

## What can a client see about an agent in the Workspace?

Clicking an agent opens the conversation, not a report about it. A band under the header of every chat with that agent shows its tasks in the last 7 days (a fixed window), the share completed, the share that succeeded first try, a **Helpful / Not helpful** tally, and a small activity chart. Everything else sits in the rail's **Info** tab: name and description with health and availability as separate facts, the client's own chats with unread counts, **What it can do** capability cards that pre-fill the composer, and the reports addressed to them. Beyond Info, a client gets **Files** (files exchanged in the chat) and **Canvas** (only canvases the agent published to its roster); the **Work** and **Loops** tabs, the model dropdown, and voice calls are platform-user-only, and a client never sees loop runs it couldn't open. Nothing configures — no schedules, skills, logs, costs, or model details — and the live updates a client receives are scoped to the agents on their roster. Because it reads from stored data, a stopped agent still renders. See [Workspace](../sharing-and-access/workspace.md#the-rail).

## What's the difference between a public link, sharing, and the Workspace?

Three ways to give someone access, for three audiences. A **public link** is a single anonymous chat URL for one agent — no sign-in, no saved history, one agent per link. **Sharing** gives another Trinity operator interact-level access to an agent through the logged-in admin UI. The **Workspace** is a signed-in app where a named external client verifies their email, picks among the agents shared with them, holds multiple saved conversations that keep their memory between turns, reads each agent's Info tab and the reports addressed to them, and exchanges files — a client workspace, not an anonymous URL and not the operator UI. Both it and the multi-agent rooms within it ship in every build. See [Workspace](../sharing-and-access/workspace.md).

## How long does a client stay signed in to the Workspace?

A client session slides: it renews while the person uses the Workspace, ends after an idle window, and never outlives a hard cap — by default 7 idle days and 30 days in total. When it times out the sign-in form says *Your session timed out*, and signing in again returns them to where they were. Admins see the policy under **Settings → Retention → Workspace sessions**; changing the numbers requires an entitlement. Platform users have no separate Workspace session — their platform login is the Workspace login. See [Workspace](../sharing-and-access/workspace.md#signing-in-and-out).

## What's the difference between "Sign out" and "Sign out of Trinity" in the Workspace?

The button at the bottom of the Workspace sidebar signs you out. For a platform user it reads **Sign out of Trinity**, because the Workspace session *is* the platform session — ending one ends the other. For an external client it is a plain **Sign out**. Identity never switches on its own: when a client's session expires on a browser that also holds an operator login, the sign-in form offers **Continue as user@example.com** as an explicit click rather than quietly carrying on as the operator. See [Workspace](../sharing-and-access/workspace.md#signing-in-and-out).

## Which model does a client's conversation run on, and can they choose it?

A client's turns run on the model you set as the **Public chat model** on the agent's **Sharing** tab, falling back to the platform default; clients get no model control of their own. Platform users on a Claude-runtime agent do get a dropdown beside **Send** — **Agent's default (…)** plus three plain-language tiers, **Most capable**, **Balanced — fast and smart**, and **Fastest** — remembered per agent on their account. If the agent can't complete a turn on a chosen model, the reply says so and the choice reverts to the agent's default. See [Workspace](../sharing-and-access/workspace.md#the-composer) and [Agent Sharing](../sharing-and-access/agent-sharing.md#public-chat-model).

## Can my agent deliver a report to one specific client, and what happens when they rate it?

Yes. When the agent publishes a report with an `audience_email` — an address it is shared with, checked against its roster — the report becomes a **deliverable**: it appears in that person's Workspace under **Info → Reports**, and as a **Delivered here** card at the end of the chat whose turn produced it (the agent passes its `execution_id` and Trinity resolves the chat, so it can never post into a conversation it wasn't part of). Unaddressed reports never appear in the Workspace. Each deliverable carries **Useful** / **Not what I needed**; the second opens an optional comment box, one rating per person applies (clicking the other option corrects it), and a negative rating also raises a heads-up in your operator queue. The agent learns tallies only — never the words and never who rated it; operators read comments in full. See [Agent Reports](../operations/agent-reports.md#deliverables-in-the-workspace).

## Can my agent ask a client a question, and where does the client answer it?

Yes. An agent addresses an approval or question to one person it is shared with by setting `addressed_to_email` on the queue item; an address that isn't on its roster turns the item into an ordinary operator ask rather than being refused. The client sees it above the composer of the chat it belongs to — an ask raised outside any chat (by a scheduled run, say) attaches to their **Main** chat with that agent — and the sidebar counts open asks separately from unread replies. Answering there records the decision exactly as an operator's would, including waking the agent when that setting is on, and the item stays visible to operators in the queue. The ask's `context` block is never shown to the client, and revoking the share hides the ask. See [Approvals](../automation/approvals.md#asks-addressed-to-a-workspace-user).

## Are there limits on how much a client can send through the Workspace?

Yes, per client. Chat sends are limited per client per agent — 20 per minute and 300 per hour by default — and file sends per client at 20 per minute and 100 per hour, each answered with a 429 that names the reason (*Too many messages to this agent.*, *Too many uploads.*); admins tune them with `PORTAL_CHAT_BURST_LIMIT` / `PORTAL_CHAT_HOURLY_LIMIT` and `PORTAL_UPLOAD_BURST_LIMIT` / `PORTAL_UPLOAD_HOURLY_LIMIT` on the backend. Files are capped at 25 MB each and 20 per drop, turns on one chat are serialized (a second message while one runs is refused), and sign-in codes carry brute-force protection. The agent's own execution timeout and parallel capacity apply on top. See [Workspace](../sharing-and-access/workspace.md) and the [single-server guide](../guides/deploying/single-server.md#rate-limits-and-caps).

## Can my own product sign a customer into the Workspace on their behalf?

Yes, with a **Portal delegate** MCP key. An admin mints it (human-only, via the scope choice on the create-key form); it reaches exactly one route — exchanging an end user's email for a Workspace session — so a trusted backend can act as that person and embed Workspace conversations in your own product, and every other path refuses it. The exchange endpoint requires an entitlement. See [Authentication](../api-reference/authentication.md#mcp-key-scopes).

## How do I keep a growing fleet of agents organized?

Use tags and saved views. Tag agents from the agent detail page (or via API/MCP); tags show as colored badges on agent tiles, group agents into tag clouds on the Dashboard, and drive the shared Dashboard filters that apply across the Timeline, Grid, and List views. For filter combinations you use often, create a **System View** — a saved filter of tags plus other criteria that persists across sessions. See [Tags and Organization](../sharing-and-access/tags-and-organization.md).

## Can I manage Trinity from my phone?

Yes. Trinity ships a mobile-optimized PWA at `/m` (for example `http://your-domain.com/m`) — install it via **Add to Home Screen** for a native-app feel. It has three tabs: **Agents** (list, chat, toggle autonomy, send tasks), **Ops** (answer the operator queue and acknowledge alerts — for an approval you tap an option, optionally add a note, then press an explicit **Send**, so nothing goes out on a single tap), and **System** (fleet health and the fleet-level actions, each confirmed in a bottom sheet). It's built for quick interactions like answering an agent's question or flipping autonomy on and off. See [Mobile Admin](../sharing-and-access/mobile-admin.md).

## Can I charge people to chat with my agent?

Yes, via per-request payments using the Nevermined x402 protocol. In the agent's payment settings, enter your Nevermined API key, Agent ID, and Plan ID, then enable payments — the agent gets a paid endpoint at `POST /api/paid/{agent_name}/chat`. Callers without payment credentials receive HTTP 402 with payment instructions, buy credits on the Nevermined checkout page, and retry with a `payment-signature` header; Trinity verifies the payment, deducts credits, and routes the message to the agent. See [Nevermined x402 Payments](../integrations/nevermined-payments.md).

## How do I set the price for my paid agent, and can I test payments without real money?

Pricing lives in your Nevermined plan (which defines credit pricing and allocation — one plan per agent); on the Trinity side you set how many credits each chat request burns (`credits_per_request`, default 1, minimum 1). For testing, configure the agent with the `sandbox` environment (the default), which runs on the Base Sepolia testnet; switch to the `live` environment for real payments on Base mainnet. Anyone can check an agent's payment requirements without authentication via `GET /api/paid/{agent_name}/info`. See [Nevermined x402 Payments](../integrations/nevermined-payments.md).

## How can I see the payments my agent has received?

Payment history is available per agent via `GET /api/nevermined/agents/{name}/payments`, or through the `get_nevermined_payments` MCP tool. If a payment settles incorrectly, admins can list failed settlements and retry them manually — settlement failures are never retried automatically. See [Nevermined x402 Payments](../integrations/nevermined-payments.md).

## How does my agent share a file with someone outside Trinity, and how do I revoke the link?

First enable file sharing in the agent's **Sharing** tab (a restart mounts the publish volume). The agent then drops a file into `/home/developer/public/` and calls the `share_file` MCP tool, which returns a signed download URL valid for 7 days — it works anywhere a link works, including phone in-app browsers: images, audio, video, and PDF open inline and stream, HTML and SVG are always delivered as a download, and `?download=1` forces a download for any file. Limits: 50 MB per file, 500 MB per agent across active shares, and executables are blocked. The signed URL is the only credential a download needs, so revoke it if it reaches the wrong hands: active shares appear in the File Sharing panel with size, expiry, and download count, and **Revoke** kills a link immediately (downloads then return `410 Gone`). See [Agent Files](../agents/agent-files.md).
