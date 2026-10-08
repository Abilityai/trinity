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

Each role has a limit on how many agents it can own: by default creators get 10, operators 3, and users 1 (admins are always unlimited). When you're at your limit, agent creation is rejected with HTTP 429 and a message of the form *Agent quota exceeded. You have 10/10 agents. Delete an agent to create a new one.* (code `QUOTA_EXCEEDED`, with your current count and limit). Admins can raise the per-role limits under **Settings** → **Agent Quotas** (enter `0` for unlimited). Redeploying an agent you already own doesn't count against your quota, and system agents are excluded from the count. See [Agent Quotas](../operations/agent-quotas.md).

## What role does a new user get the first time they log in?

The role comes from their email whitelist entry: each whitelisted email carries a default role that is assigned on first login, and it falls back to the basic **user** role if none was set. Adding someone through an agent's Access tab or approving their access request whitelists them as a **user** — a chat-only grant that never silently promotes anyone. An admin can promote them afterwards via **Settings** → **Access** → **User Management**. See [Roles and Permissions](../getting-started/roles-and-permissions.md).

## Can people sign up for my Trinity instance on their own?

Not unless you explicitly allow it — public self-signup is off by default, and the email whitelist stays the real access gate. The unauthenticated access-request endpoint returns 403 and tells the person to ask an administrator to whitelist their email. An admin can opt in via the `PUBLIC_ACCESS_REQUESTS_ENABLED` environment variable or the matching system setting; when enabled, self-signups are auto-whitelisted with the basic **user** role. See [Roles and Permissions](../getting-started/roles-and-permissions.md).

## How does someone request access to my agent, and what happens when I approve it?

When an agent is in **Restricted** mode and an unknown user with a verified email messages it — from any channel — they see "Your access request is pending approval" and a request appears under the Restricted/Open switch in the agent's **Sharing** tab. Click **Approve** to add their email to the share list, which admits them on every channel at once; click **Deny** to reject silently (the agent's existence is not confirmed). If the request came in over Telegram, Slack, or WhatsApp, Trinity automatically messages the requester on that same channel to confirm access; a delivery failure never rolls back the approval. See [Access Control](../sharing-and-access/access-control.md).

## What's the difference between Restricted and Open access on an agent?

It's a single per-agent switch under "Who can chat with this agent?" on the Sharing tab. **Restricted** (the default) means only the owner, admins, and explicitly approved emails can chat — everyone else generates a pending access request. **Open** means anyone with a verified email can chat immediately. Email verification is not a separate switch on that tab: both positions of the control require a verified email (it writes `require_email: true` either way), and turning verification off entirely (`require_email: false`) is possible only through the access-policy API (`PUT /api/agents/{name}/access-policy`). The fleet-wide default that newly created agents start with lives under **Settings → General** and applies at creation time only — existing agents are never rewritten. See [Access Control](../sharing-and-access/access-control.md).

## What's the difference between sharing an agent and creating a public link?

Sharing grants a Trinity account holder interact access through the logged-in UI. A public link is a shareable URL that lets anyone chat with the agent without logging in. Whether it asks for a verified email comes from the agent's access policy, not the link; a link itself carries only an optional name and expiry. Public-link conversations persist across page refreshes, and logged-in Trinity users get a history dropdown to read their past sessions. Create links under **Sharing** → **Distribution** → **Public links** on the agent detail page. See [Public Links](../sharing-and-access/public-links.md).

## Can I connect a public link to Slack?

Yes. Each public link card carries a **Slack** row with a **Connect Slack** button (owner or admin only). If the platform's Slack workspace is already connected, Trinity creates a Slack channel named after the agent, binds the agent to it, and makes the agent the DM default when it is the first agent bound in that workspace. If no workspace is connected yet, the button opens Slack's authorization page — the platform Slack app must be configured under **Settings → Integrations → Slack** first, otherwise the button reports that Slack is not configured. Once connected, the row shows the workspace name and who connected it, with an enable/disable toggle and **Disconnect Slack**; the Slack conversation follows the same access rules as the link itself. See [Public Links](../sharing-and-access/public-links.md#connect-slack-from-a-public-link).

## What is the Workspace?

The Workspace at `/workspace` is the signed-in chat app for the people you share agents with — and for you. It ships in every build (the old `/portal` path redirects there), and it is the one surface where a conversation keeps the agent's working memory between turns, where every (you, agent) pair has a pinned **Main** chat plus as many named chats as you like, and where several agents can work one thread together. Platform users open it from the nav — it opens in its own browser tab, and your platform session is your Workspace session; external clients sign in with a 6-digit email code, get no platform account, and see only the agents shared with their address. It opens on the **Inbox** — what needs you and what came back across your agents — and clicking an agent starts a new chat with it, with your earlier chats as tabs. See [Workspace](../sharing-and-access/workspace.md).

## How do I bring a second agent into a conversation?

Type `@` and the other agent's name in an existing one-to-one chat. The Workspace opens a **room** containing both agents and posts your message there, leaving the original chat untouched. Inside a room, **+ Add agent** (or mentioning an agent that isn't yet a participant) recruits it — only a person can do that, never another agent — and `@` keeps working there while the `/` playbook picker is not offered in rooms. An `@name` that isn't one of your agents stays plain text. Multi-agent chat is part of the open-source platform — it used to be an enterprise capability, and against an older backend that lacks it the picker is single-select and mentions stay ordinary text. See [Workspace](../sharing-and-access/workspace.md#bringing-in-another-agent).

## What can a client see about an agent in the Workspace?

Clicking an agent opens the conversation, not a report about it. A band under the header of every chat with that agent shows its tasks in the last 7 days (a fixed window), the share completed, the share that succeeded first try, a **Helpful / Not helpful** tally, and a small activity chart. Everything else sits in the rail's **Info** tab: name and description with health and availability as separate facts, the agent's **Role** card when it has one, the client's own chats with unread counts, **What it can do** capability cards that pre-fill the composer, the reports addressed to them, **What it remembers about you** (with **Undo** for a run that changed it), and the **Decisions** they recorded with the agent. A client's band and Info tab count only runs they can see — their own turns, what those turns started, and the agent's shared scheduled runs — never other people's runs of the same agent. Beyond Info, a client gets **Files** (files exchanged in the chat) and **Canvas** (only canvases the agent published to its roster); the **Work** and **Loops** tabs, the model dropdown, and voice calls are platform-user-only, and a client never sees loop runs it couldn't open. Nothing configures — no schedules, skills, logs, costs, or model details — and the live updates a client receives are scoped to the agents on their roster. Because it reads from stored data, a stopped agent still renders. See [Workspace](../sharing-and-access/workspace.md#the-rail).

## What can I do with an agent's canvases from the Workspace rail?

The rail's **Canvas** tab shows one canvas at a time; pick another from the dropdown in the control row above it, where a pinned canvas carries 📌 and comes first, and once an agent has more than six a **Search canvases…** box filters by title or id. The header states two facts — *Updated 2h ago · agent last ran 40m ago* — so you can judge freshness yourself, and **PDF** prints the open canvas through your browser's own print dialog. The agent's owner (or an admin), signed in as a platform user, also gets **Manage** at the end of that row, which opens a list below it: each row shows its age, a pin toggle and **Delete**, and a checkbox per row feeds **Delete selected** with one confirmation naming the count; everyone else — including every external client — has a read-only panel. The canvas you have open travels with each message you send from that chat, so *add a column to this* names the right one; it is context, not permission. Sharing a canvas by link is done from the agent's Canvas tab on Agent Detail, not from the rail (next question). See [Workspace](../sharing-and-access/workspace.md#the-rail).

## How do I share a canvas with someone by link, and can I take it back?

On the agent's **Canvas** tab on Agent Detail, **Share** mints a link, and you choose who it reaches — the narrower option is preselected. **People who already have access** (the default) requires signing in, and only people who can already see the agent see the canvas; **Anyone with the link** needs no sign-in at all, and the dialog says so plainly because it reaches further than the canvas did before. A shared canvas stays current — whoever opens the link sees it as the agent updates it, with the last-updated time shown — and **Revoke** turns a link off; anyone holding it is told it was turned off rather than getting a broken page. Only the agent's owner or an admin can share, pin, or delete, and creating or revoking a link is recorded in the audit log. See [Agent Canvas](../agents/agent-canvas.md#sharing-a-canvas-and-saving-it-as-a-pdf); what a canvas is and who sees which audience is in the [Advanced Features FAQ](advanced-features.md#who-can-see-an-agents-canvas).

## Why did my copied public link used to be just `/chat/...`, and what URL does it use now?

The backend builds a link as `FRONTEND_URL` + `/chat/{token}`, and `FRONTEND_URL` is empty unless you set it, so older builds showed a bare path. The Sharing tab now completes it with the address you opened Trinity on, and copies a full URL. If you set a **Public URL** under **Settings → General**, the link uses that instead — set it whenever outsiders reach Trinity on a different address than you do. See [Public Links](../sharing-and-access/public-links.md).

## Are public links rate limited?

Yes, with fixed limits rather than per-link settings: 30 messages a minute per IP address and 60 a minute per link, plus a cap on how often one IP can look up links. Email verification codes are limited to 3 requests per email every 10 minutes. See [Public Links](../sharing-and-access/public-links.md#limitations).

## What's the difference between a public link, sharing, and the Workspace?

Three ways to give someone access, for three audiences. A **public link** is a single anonymous chat URL for one agent — no sign-in, no saved history, one agent per link. **Sharing** gives another Trinity operator interact-level access to an agent through the logged-in admin UI. The **Workspace** is a signed-in app where a named external client verifies their email, picks among the agents shared with them, holds multiple saved conversations that keep their memory between turns, reads each agent's Info tab and the reports addressed to them, and exchanges files — a client workspace, not an anonymous URL and not the operator UI. Both it and the multi-agent rooms within it ship in every build. See [Workspace](../sharing-and-access/workspace.md).

## How long does a client stay signed in to the Workspace?

A client session slides: it renews while the person uses the Workspace, ends after an idle window, and never outlives a hard cap — by default 7 idle days and 30 days in total. When it times out the sign-in form says *Your session timed out*, and signing in again returns them to where they were. Admins see the policy under **Settings → Retention → Workspace sessions**; changing the numbers requires an entitlement. Platform users have no separate Workspace session — their platform login is the Workspace login. See [Workspace](../sharing-and-access/workspace.md#signing-in-and-out).

## What's the difference between "Sign out" and "Sign out of Trinity" in the Workspace?

The button at the bottom of the Workspace sidebar signs you out. For a platform user it reads **Sign out of Trinity**, because the Workspace session *is* the platform session — ending one ends the other. For an external client it is a plain **Sign out**. Identity never switches on its own: when a client's session expires on a browser that also holds an operator login, the sign-in form offers **Continue as user@example.com** as an explicit click rather than quietly carrying on as the operator. See [Workspace](../sharing-and-access/workspace.md#signing-in-and-out).

## Which model does a client's conversation run on, and can they choose it?

A client's turns run on the model you set as the **Public chat model** on the agent's **Sharing** tab, falling back to the platform default; clients get no model control of their own. Platform users on a Claude-runtime agent do get a dropdown beside **Send** — **Agent's default (…)** plus three plain-language tiers, **Most capable**, **Balanced — fast and smart**, and **Fastest** — remembered per agent on their account. If the agent can't complete a turn on a chosen model, the reply says so and the choice reverts to the agent's default. See [Workspace](../sharing-and-access/workspace.md#the-composer) and [Agent Sharing](../sharing-and-access/agent-sharing.md#public-chat-model).

## Can my agent deliver a report to one specific client, and what happens when they rate it?

Yes. The agent names the role the report is for with `to` — usually `primary`, the person it serves — and the platform resolves who that is; the older `audience_email` still works but is deprecated. When that person is on the agent's roster and is not its owner, the report becomes a **deliverable**: it appears in that person's Workspace under **Info → Reports**, and as a **Delivered here** card at the end of the chat whose turn produced it (the agent passes its `execution_id` and Trinity resolves the chat, so it can never post into a conversation it wasn't part of). Unaddressed reports never appear in the Workspace. Each deliverable carries **Useful** / **Not what I needed**; the second opens an optional comment box, one rating per person applies (clicking the other option corrects it), and a negative rating also raises a heads-up in your operator queue. The agent learns tallies only — never the words and never who rated it; operators read comments in full. See [Agent Reports](../operations/agent-reports.md#deliverables-in-the-workspace).

## What is the Decisions section in the Workspace?

A record of what you and the agent approved, deferred or killed, and why. Each entry records the outcome, what was decided, the alternatives, the criterion that decided it, what would reverse it, and a review date. You record one with **Record a decision**, and the agent can record one with its `record_decision` tool. An active decision can be reconfirmed, corrected, reversed (with a reason) or closed; nothing is deleted, and a decision past its review date shows as expired. The active ones are read into every turn with that agent, so it reuses the criterion next time. See [Workspace](../sharing-and-access/workspace.md#decisions).

## Can my agent ask a client a question, and where does the client answer it?

Yes. An agent addresses an approval or question to one person it is shared with by setting `addressed_to_email` on the queue item; an address that isn't on its roster turns the item into an ordinary operator ask rather than being refused. The client sees it in their **Inbox**; an ask raised during a chat turn also appears as a tile inside that chat, answerable there, while one raised outside any chat (by a scheduled run, say) appears in no chat. The sidebar counts open asks separately from unread replies. Answering there records the decision exactly as an operator's would, including waking the agent when that setting is on, and the item stays visible to operators in the queue. The ask's `context` block is never shown to the client, and revoking the share hides the ask. See [Approvals](../automation/approvals.md#asks-addressed-to-a-workspace-user).

## What is the Workspace Inbox?

The Workspace's landing page — signing in, or opening bare `/workspace`, takes you there. **Action** lists the asks waiting on you, most urgent first (soonest expiry, then priority, then oldest), and can be narrowed to one agent; **Unread** lists chats where something came back since you last read them; **All** lists every chat plus asks that ended in the last 7 days. Clicking a row opens it in a reading pane: an ask is answered in place, with where it came from shown below it, and a chat shows what arrived and is marked read. A pinned **Inbox** row at the top of the sidebar carries the two counts. Rooms are not in the Inbox yet. See [Workspace](../sharing-and-access/workspace.md#the-inbox).

## Why does clicking an agent open an empty chat instead of my last conversation?

Most visits to an agent start new work, so opening an agent now starts a new chat with the cursor in the message field. Nothing is created until you send, so this leaves no empty chats behind, and if you left unsent text in a chat with that agent you land back on it. Your earlier chats are the tabs above the thread, and **⌥⇧↓** / **⌥⇧↑** (**Alt+Shift+↓** / **↑**) steps through them. Moving between agents with **⌥↓** / **⌥↑** returns you to the chat you last had open with each one, until you reload. See [Workspace](../sharing-and-access/workspace.md#starting-a-chat).

## Does the Workspace have keyboard shortcuts?

Yes, and they work while you are typing in the message field. **⌘J** / **Ctrl+J** starts a new chat, **⌥↓** / **⌥↑** moves between agents, **⌥⇧↓** / **⌥⇧↑** between chats with the current agent, **⌘.** / **Ctrl+.** shows or hides the rail, **⌥.** steps through the rail's tabs, **⌘/** / **Ctrl+/** jumps to the sidebar search, and **⌥/** / **Alt+/** opens the full list (also the **Keyboard shortcuts** button at the bottom of the sidebar). Esc closes the innermost thing first, then stops a running turn. The keys pause while a dialog is open, and during a voice call only ⌘J answers. See [Workspace](../sharing-and-access/workspace.md#keyboard-shortcuts).

## How do I reply to one specific message from the agent?

Click **Reply to this message** in the action row under the agent's message (beside **Copy message** and the thumbs). A *Replying to …* chip appears above the message field, and your next message tells the agent which message you are answering; the chip's **×** or Esc drops it. It works in 1:1 chats, and from a chat opened in the Inbox's reading pane. See [Workspace](../sharing-and-access/workspace.md#reading-replies).

## Why does a Workspace link say "This chat isn't available" or "You don't have access"?

The link names something your account cannot open. A chat link that isn't one of your chats shows *This chat isn't available*, and an agent link for an agent that isn't shared with your email shows *You don't have access to …*. Both offer **Back to your chats** and no message field, so you never type into a chat the link did not name. If you expected access, ask the agent's owner to share it with the email you signed in with. See [Workspace](../sharing-and-access/workspace.md#starting-a-chat).

## How do I sign a client out of the Workspace, or block them?

On the agent's **Sharing** tab, the **Portal clients** list shows everyone the agent is shared with. **Log out** ends that person's live Workspace sessions everywhere — one sign-in covers all their agents — and they can sign straight back in; any owner of an agent shared with them can do it. **Block** is admin-only and keeps them out of the whole platform until an admin unblocks them, keeping their history. To remove someone from one agent only, unshare it. See [Agent Sharing](../sharing-and-access/agent-sharing.md#portal-clients).

## Are there limits on how much a client can send through the Workspace?

Yes, per client. Chat sends are limited per client per agent — 20 per minute and 300 per hour by default — and file sends per client at 20 per minute and 100 per hour, each answered with a 429 that names the reason (*Too many messages to this agent.*, *Too many uploads.*); admins tune them with `PORTAL_CHAT_BURST_LIMIT` / `PORTAL_CHAT_HOURLY_LIMIT` and `PORTAL_UPLOAD_BURST_LIMIT` / `PORTAL_UPLOAD_HOURLY_LIMIT` on the backend. Files are capped at 25 MB each and 20 per drop, turns on one chat are serialized (a second message while one runs is refused), and sign-in codes carry brute-force protection. The agent's own execution timeout and parallel capacity apply on top. See [Workspace](../sharing-and-access/workspace.md) and the [single-server guide](../guides/deploying/single-server.md#rate-limits-and-caps).

## Can my own product sign a customer into the Workspace on their behalf?

Yes, with a **Portal delegate** MCP key. An admin mints it (human-only, via the scope choice on the create-key form); it reaches exactly one route — exchanging an end user's email for a Workspace session — so a trusted backend can act as that person and embed Workspace conversations in your own product, and every other path refuses it. The asserted email must already have an agent shared with it — the key cannot create access for anyone — and every exchange is audited. See [Authentication](../api-reference/authentication.md#mcp-key-scopes).

## How do I keep a growing fleet of agents organized?

Use tags and saved views. Tag agents from the agent detail page (or via API/MCP — `POST /api/agents/{name}/tags/{tag}` adds one, `tag_agent` is the MCP tool); tags show as colored badges on agent tiles and drive the shared Dashboard filters that apply across the Timeline, Grid, and List views and persist across navigation. On the Grid view, `dept-<name>` and `reports-to-<agent>` tags draw the org overlay — department zones and reporting lines — and those two families can only be set by people, never by agent-scoped keys. For filter combinations you use often, create a **System View** — a saved filter of tags plus other criteria that persists across sessions. See [Tags and Organization](../sharing-and-access/tags-and-organization.md).

## Can I manage Trinity from my phone?

Yes. Trinity ships a mobile-optimized PWA at `/m` (for example `http://your-domain.com/m`) — install it via **Add to Home Screen** for a native-app feel. It has three tabs: **Agents** (list, start or stop, chat, toggle autonomy, view logs), **Ops** (answer the operator queue and acknowledge alerts — for an approval you tap an option, optionally add a note, then press an explicit **Send**, so nothing goes out on a single tap), and **System** (fleet health as **Total / Running / Stopped / High Ctx** counts, plus the fleet-level actions — Emergency Stop, Fleet Restart, Pause Schedules, Resume Schedules — each confirmed in a bottom sheet). It's built for quick interactions like answering an agent's question or flipping autonomy on and off. See [Mobile Admin](../sharing-and-access/mobile-admin.md).

## Can I charge people to chat with my agent?

Yes, via per-request payments using the Nevermined x402 protocol. In the agent's payment settings, enter your Nevermined API key, Agent ID, and Plan ID, then enable payments — the agent gets a paid endpoint at `POST /api/paid/{agent_name}/chat`. Callers without payment credentials receive HTTP 402 with payment instructions, buy credits on the Nevermined checkout page, and retry with a `payment-signature` header; Trinity verifies the payment, deducts credits, and routes the message to the agent. If the agent is also exposed over A2A, the same price applies to outside callers on its A2A door, and its A2A card advertises the price. See [Nevermined x402 Payments](../integrations/nevermined-payments.md).

## How do I set the price for my paid agent, and can I test payments without real money?

Pricing lives in your Nevermined plan (which defines credit pricing and allocation — one plan per agent); on the Trinity side you set how many credits each chat request burns (`credits_per_request`, default 1; set `0` for a time-based plan, where the plan decides what a call costs). Crypto plans and card (fiat) plans both work — Trinity reads the plan's type from Nevermined and asks the buyer for the matching payment scheme, with nothing to configure. For testing, configure the agent with the `sandbox` environment (the default), which runs on the Base Sepolia testnet; switch to the `live` environment for real payments on Base mainnet. Anyone can check an agent's payment requirements without authentication via `GET /api/paid/{agent_name}/info`. See [Nevermined x402 Payments](../integrations/nevermined-payments.md).

## How can I see the payments my agent has received?

Payment history is available per agent via `GET /api/nevermined/agents/{name}/payments`, or through the `get_nevermined_payments` MCP tool. If a payment settles incorrectly, admins can list failed settlements and retry them manually — settlement failures are never retried automatically. See [Nevermined x402 Payments](../integrations/nevermined-payments.md).

## My Nevermined plan is paid by card. Does that work?

Yes. Trinity reads the plan's type from Nevermined and advertises the matching x402 scheme in its 402 response — `nvm:card-delegation` for a card plan, `nvm:erc4337` for a crypto one — so the buyer's token is checked against the right requirements. The answer is cached for five minutes, and `GET /api/paid/{agent}/info` shows which scheme a client will be asked for. If Nevermined can't be reached the first time, Trinity uses its last answer or falls back to the crypto scheme, and logs a warning. See [Nevermined x402 Payments](../integrations/nevermined-payments.md#crypto-plans-and-card-fiat-plans).

## Why does a buyer's payment token get rejected for the wrong resource URL?

A 402 carries a `resource.url` that the token is minted and verified against, so it must be the address the buyer actually calls. Trinity uses your configured public URL when it matches the host of the request, otherwise the request's own host, upgraded to `https` only when your proxy sends `X-Forwarded-Proto: https`. Check that your TLS terminator forwards that header, and that the configured public URL is the hostname buyers use. See [Nevermined x402 Payments](../integrations/nevermined-payments.md#the-url-in-a-402-must-be-the-url-you-call).

## Can other systems pay to call my agent over A2A?

Yes, when the agent is both exposed over A2A and has payments enabled. A caller with a Trinity key still runs free; a caller without one gets `402`, pays with a Nevermined token sent in the A2A message, and gets a normal Task back with the payment receipt in its metadata. The agent's A2A card advertises the price so a client can pay on its first request. A price alone does not open the door — A2A exposure must be on. See [A2A Protocol](../integrations/a2a-protocol.md#charge-for-inbound-a2a-calls).

## How does my agent share a file with someone outside Trinity, and how do I revoke the link?

First enable file sharing in the agent's **Sharing** tab (a restart mounts the publish volume). The agent then drops a file into `/home/developer/public/` and calls the `share_file` MCP tool, which returns a signed download URL valid for 7 days — it works anywhere a link works, including phone in-app browsers: images, audio, video, and PDF open inline and stream, HTML and SVG are always delivered as a download, and `?download=1` forces a download for any file. Limits: 50 MB per file, 500 MB per agent across active shares, and executables are blocked. The signed URL is the only credential a download needs, so revoke it if it reaches the wrong hands: active shares appear in the File Sharing panel with size, expiry, and download count, and **Revoke** kills a link immediately (downloads then return `410 Gone`). See [Agent Files](../agents/agent-files.md).
