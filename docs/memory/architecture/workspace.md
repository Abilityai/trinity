# Trinity Architecture — Workspace / client portal and rooms

> Part of the Trinity architecture set. Core map, invariants and topology: [architecture.md](../architecture.md). This file is **not** auto-loaded.
>
> **Owns**: `src/backend/client_portal/**`, `src/frontend/src/views/Portal.vue`, `src/frontend/src/stores/clientPortal.js`, `src/frontend/src/components/portal/**`
>
> **Read this before changing the paths above**: The roster payload is the only capability channel a portal principal has. `GET /api/settings/feature-flags` is `get_current_user`-gated and its store returns an empty list for every external client, so a UI gate written against it is dead for exactly the audience it targets (#2128). Per-agent fields on that payload also fail in deliberately opposite directions and must not be normalised to match.
>
> **Write path**: changes to this area land here, not in the core (core editorial rule 4). Keep the core's map row in step if the owned paths change.

---

### Workspace / Client Portal (epic ent#78; OSS core since ent#356)

The client-facing surface, mounted in **every** build (`src/backend/client_portal/`,
prefix `/api/enterprise/client-portal`, Vue at `/workspace`). A caller is either an
**external client** — a verified email with no `users` row, signing in with a 6-digit
code — or a **signed-in platform user**, who reaches the same surface in one click
because their platform session *is* the workspace session (ent#357). Both resolve
through `client_portal/portal_auth.py::get_portal_principal`, which returns
`(email, is_platform)`; the roster is every agent shared with that email, plus — for a
platform session only — the agents they own.

**Sign-out ends whichever credential is live — never a derivation of it (#2258).** The
implicit entry above runs the other way too: `isPlatformSession = !portalToken &&
isAuthenticated`, so clearing only the portal token is what *activates* the platform
fallback, and the Workspace's "Sign out" used to re-enter as the operator on refresh (or
re-authenticate a client as the co-resident operator). `stores/clientPortal.js::
signOutEverywhere()` ends the platform session (`authStore.logout()`) **first**, then
clears portal state, and routes by principal — operator → `/login`, client → the OTP
form. A persisted suppression flag was rejected on evidence: the JWT is an axios
**default** header and per-request headers merge over defaults, so a flag hides the
disclosure while every portal request still carries the operator's credential.
`auth.logout()` clears local state **before** the network revoke, because the global 401
interceptors and the `/login → /` router guard both key on it. `endSession({expired})`
deliberately does not end a platform session (expiry is not a user act). Residuals stated
in [workspace-session-signout.md](../feature-flows/workspace-session-signout.md): a client
session that *expires* on a browser which later gained a platform login, and the portal
token's server-side validity post-sign-out (no self-service revoke; ent#281's primitive
is per-email).

**Membership is a DB fact; container state is a projection onto the card (#2196).** The
roster is built from `agent_ownership` / `agent_sharing` and is **never** filtered by
whether a container exists. A live ownership row with no container is a routine state
(#1747: identity lives in the row, and #834 Phase 1c recovery, a `docker system prune`
or a crash mid-create all reach it), so hiding those rows would make "not shared with
me" indistinguishable from "shared but containerless" on the one surface a client has.
Filtering is also the dangerous direction at scale: every Docker read in the platform
collapses *no container* and *Docker could not be asked* into one falsy value
(`list_all_agents_fast` returns `[]` on any fault), so one daemon restart or one
`DOCKER_GID` change would tell every paying customer they have no agents. Instead each
card carries `availability` (`ready`/`stopped`/`unavailable`/`unknown`), resolved once
per roster load in `get_roster` — **not** in `_roster_rows`, which stays pure SQL so
#2198's batch-sessions gate does not inherit a Docker read, and **not** inside
`_agent_briefing` (which #2163 did: defer + bound; cache deferred). Invariant #11
is untouched: every Docker read still happens inside `docker_service.py`.

**The sidebar's thread list is one viewer-scoped call (#2198).**
`GET /api/enterprise/client-portal/sessions` returns every thread the caller has across
every agent on their roster. It replaced a literal N+1: the sidebar renders a merged,
cross-agent, recency-sorted list, so it asked the per-agent route once per rostered
agent — from six `refreshThreads()` call sites including every thread open and every
completed turn — and each of those re-resolved the roster before reading the session
table. The batch's tenant scope IS the roster: `agent_name IN (…)` populated from
`service.roster_agent_names(email, include_owned)`, the same set `agent_on_roster`
enforces, extracted so the two cannot drift (filtering on `client_email` alone would
re-surface threads for an un-shared agent). No agent parameter, so it is strictly less
enumerable than the route it replaces (Invariant #8); no schema change and no new index
(the existing `(agent_name, client_email, last_message_at)` gives it the same plan each
per-agent query already got); rate-limited per viewer, since it is no longer even
incidentally throttled by a browser connection cap. The one real trade is failure
granularity — one unreadable agent used to degrade alone, and the read is now
all-or-nothing — which is why the store returns its **last good list** on failure rather
than blanking, and `refreshThreads` catches both halves: an uncaught rejection there
aborts `bootstrap()` before `resolveAgentQuery()` and breaks Workspace deep-link landing.
Shipping it also closed the hole it would have amplified: `get_portal_principal`'s
platform branch now runs `reject_agent_principal`, so an agent-scoped MCP key — which
resolves to its owner carrying the owner's role — can no longer traverse the Workspace
as `is_platform=True` (it could previously read the owner's threads with agents the
calling agent holds no `agent_permissions` edge to, and this route would have made that
one call). User-scoped keys, `scope='system'` and portal session tokens are unaffected;
there is no legitimate agent caller of this surface (no MCP tool targets it, no agent
image calls it).

It was an entitled module and returned 404 in community builds; ent#356 moved it into
OSS core (adoption: this is the main surface a non-operator uses to work with agents).
The `/api/enterprise/client-portal` prefix and the `enterprise_portal_sessions` /
`enterprise_portal_messages` / `enterprise_client_blocks` table names are **retained
history, not licensing** — ent#83 shipped that prefix as the documented integration
surface for API-only clients, and renaming the tables would force a data migration on
every existing install. Tables are versioned on the OSS two-track runner
(`client_portal_tables_to_oss` + Alembic `0036_client_portal_oss`), both
`CREATE TABLE IF NOT EXISTS` so adopting them is a no-op where they already exist, and
both `agent_name` columns are registered in `AGENT_REFS` (CASCADE) — which the enterprise
track never was, so an agent rename used to strand a client's portal history.

**A turn may ask for a NEW thread (ent#451).** `PortalChatRequest` carries
`new_thread` on both `POST .../chat` (the ent#83-documented headless surface) and
`POST .../chat/stream`; it defaults `False`, so no existing caller changes behaviour.
It exists because an absent `session_id` meant two different things — *"I don't know
which thread"* and *"I want a fresh one"* — and `_resolve_session_id` resolved it as
the first, which is why the Workspace's **New chat** kept landing in the existing
conversation. An explicit `session_id` WINS over the flag (the id is a fact, the flag
an intent, and abandoning a named thread would strand a turn meant for a conversation
the caller could see), and the ownership check runs first either way, so the flag is
never a route past it. `ensure_thread_for_ask` deliberately does not set it — ent#429's
rule reuses the latest thread so asks do not accumulate beside the conversation.
**OSS-core by decision (ent#451): deliberately ungated — no `requires_entitlement`,
logic stays in the OSS tree.** Recorded explicitly because CLAUDE.md's default for an
enterprise-tracker feature is *gated unless ruled otherwise*, so the ruling must never
be inferred later from the mere fact that it merged; it inherits ent#356's move of the
whole client-portal surface into OSS core.

**The agent's chats are tabs, and a title has three hands (ent#451 remaining slice,
ent#473, #2579).** Above the thread, `PortalChatTabs.vue` renders this user's threads
with the active agent as `OverflowTabs` (`dense`, `fixed-width`, counted `moreLabel` →
"N more"), most recent first — a slice of the sidebar's list, never a second fetch; a
room is not an agent's tab. **New chat** is in the conversation header with ⌘J / Ctrl+J,
armed on `window` at mount above `bootstrap()`'s await and resolving the agent in front
of the person (page or conversation; a room or the root opens the picker).

**#2579 fixed four defects in that strip, and one of them is a recorded reversal.** The
2026-09-06 ruling ("a new chat exists — tab and sidebar row — once its first message is
sent") stays true for the **thread**; the **strip** now draws an unsaved active chat as
a provisional tab labelled `New chat` with `thread: null`, inserted directly after Main
(the slot the real row takes, so adoption causes no jump) and refusing to emit a select.
It is keyed **only** off explicit intent — `Portal.vue::startingNewChat`, ORed with
`PortalConversation`'s `bornHere` — never off "the active id is not in the list", or a
cold deep link to a thread the batch has not listed yet would wear the label.
`bornHere` exists because the shell clears `startingNewChat` on `session-adopted`,
*before* the refreshed list arrives; it is raised in the ONE `adoptSession()` seam all
three adoption sites go through (streaming, the sync `/chat` fallback, the voice path's
`createSession` — setting it at one drops the tab exactly when streaming is
unavailable) and spent when the row lands. **New chat focuses the composer** from
`onMounted`'s else-branch, because the press bumps `convGen` and remounts the
conversation, so focus set before it is thrown away. **Tabs are a fixed width**
(`OverflowTabs` `fixedWidth`, `FIXED_TAB_WIDTH = 'w-40'`, Main included) with clamped
labels and the full title on `title=`; every width class is gated on the prop, and
`shrink-0` + the visible nav's `overflow-hidden` are load-bearing (`inlineCount` starts
at `+Infinity`, so a truncating label would let flex squeeze the pre-measure row while
the `max-content` mirror still reports 160 — the parity class the pinned-glyph comment
is about). Geometry is proven in `e2e/workspace-chat-tabs.spec.js`; the node-env unit
specs can only pin the class strings.

**Main is ensured by the shell, through the read that already mints it (#2579).** The
batch's no-mint ruling is untouched — `Portal.vue::ensureMainListed(name)` calls the
per-agent `list_sessions` (which calls `ensure_main_session`) once per agent per
session when the on-screen list carries no Main for that agent, then re-reads the batch.
It is a **GET that inserts**. Map-deduplicated in flight, capped at two attempts, and
both maps cleared in `onSignOut` — that handler resets state in place (the OTP form is a
branch of the same component), so client B would otherwise inherit client A's resolved
promises. The cap is load-bearing rather than tidy: `fetchAllSessions` **never rejects**
(it flags `sessionsFailed` and returns the last good list), so a resolved entry over a
still-missing Main would make the miss permanent for the session. `landOnAgent`'s repair
branch had in fact never run — it destructured `{ sessions }` off an array.

A title is written
by three hands — the derived fallback, the ent#186 generator, and a person — and
`enterprise_portal_sessions.title_source` (NULL · `generated` · `user`; SQLite
`portal_session_title_source` + Alembic `0052`, no backfill) records which. **The
generated write is guarded in the UPDATE itself** (`set_portal_session_title` →
`title_source != 'user'`, returning whether it landed): generation runs off the reply
path, so a rename typed inside the first turn's window races the model's guess and a
read-then-write in the caller would leave exactly that window open. `_title_plan(row,
history)` earns a turn `first` (empty title), `retry` (the exchange after the opener,
`message_count <= 2`, when the hand is still NULL or the opener `is_greeting`) or
nothing — exactly one more, never a person's title. The validator is one leaf,
`services/chat_title.py`, imported by BOTH `client_portal` and `shared_sessions` so a
thread and a room refuse the same titles with the same named 400 (`invalid_title` +
reason + a sentence with an example); the thread UPDATE is (agent, client)-scoped
(uniform 404), the room rename is membership-then-person (a member agent talks, it does
not rename — 403 `not_a_person`, the ent#220 line) and broadcasts a thin `room_renamed`
trigger with the id only (#918). Generator health is an in-process record
(`title_generation_health()`) that WARNS once on the transition into `no_credential`
or `failing` (3 consecutive), stays quiet in it, re-arms on recovery, and rides
`GET /api/settings/portal-session-policy` → `title_generation` for the Workspace
sessions panel's notice — the operator side of a path that is otherwise fail-soft by
design.

**#2579 — the generator runs concurrently with the turn, and the client keeps a belt.**
The spawn was fired as the turn *returned*, so the client's own turn-done refresh always
lost the race and read the derived fallback; the generated title arrived only on some
later refresh, which is why a chat wore its first message as its name. The ordering is
now `_persist_user_turn` → `_spawn_title_generation(..., reply="")` → the turn, and
**both** halves matter: before the turn so the refresh does not lose the race, after the
persist because the derived fallback must exist first (the generated write is guarded
against a person's rename, not against an empty row). `_title_plan` does not move — it
was already decided pre-turn. With no reply, `_generate_thread_title` picks
`_TITLE_PROMPT_OPENER`, a separate constant rather than the two-block prompt formatted
with an empty `<assistant_reply>` (an empty block in a prompt that names it invites the
model to describe the emptiness); it carries the same never-follow-instructions
hardening. Two deliberate consequences: the title comes from the opening message alone
(the `retry` attempt is the disambiguator that remains) and a **failed** turn still
titles its thread, consistent with `_persist_user_turn`'s own ruling. Client-side, the
shell re-reads the list on `TITLE_SETTLE_DELAYS_MS` after a turn on a thread inside the
two-attempt window (`titleSettling`, post-turn `message_count` 2..4, Main included) and
stops as soon as the title differs. That schedule is a best-effort window and
deliberately **not** a mirror of `PORTAL_TITLE_TIMEOUT_SECONDS` (operator-tunable — the
#2133 class), so exhausting it asks the health record rather than deciding; the cycle
aborts on `store.sessionsFailed`, stops without a verdict on a vanished row, and is
cleared from three sites (the next turn-done, `onBeforeUnmount`, and `watch(convKey)` —
the only one that fires on a thread switch). The notice rides
`PortalConversation`'s `#notice` slot for platform admins only
(`shouldFetchTitleHealth`), through `clientPortal.js::fetchTitleGenerationHealth` on
`portalHttp` — never `@/api`, whose 401 handler hard-navigates to `/login` under
`/workspace` and would bounce an operator out of a conversation over a background
diagnostic. **Known blind spot, accepted:** `_title_health` is a module global and prod
runs `--workers 2`, so a probe can land on a worker that ran no generation and answer
`unknown`, which shows no notice. Honest under-reporting; making it cross-worker means
new Redis-shared state for a diagnostic. `PortalEditableTitle.vue` is the one editor for the row, the 1:1 header and the
room header (its clicks and keys stop inside it, or a rename would open the chat it is
renaming). **OSS-core by decision, deliberately ungated** — the ent#356/ent#451 ruling
for the whole surface, recorded here so it is never inferred from the merge. See
[workspace-chat-tabs-and-titles.md](../feature-flows/workspace-chat-tabs-and-titles.md).

**New-chat briefing hints (ent#138 / ent#380):** each agent has a briefing —
description + capability hint cards `playbooks[]{title,description,starter_prompt}` —
resolved best-effort by `service.py::_agent_briefing` from the agent's
`/api/template/info` + `/api/skills`. ent#138 shipped it ON the roster payload so the
empty-chat screen rendered with zero extra fetches; since #2163 it is **hydrated by
`GET /briefings` after the roster lands** (below), because that fan-out was the
Workspace's latency floor. The hint set is a **ladder**: the operator's exposed playbooks (connector
allow-list ∩ `user_invocable` — the same policy the MCP connector advertises) win
outright; an agent exposing none falls back to its template-declared `use_cases`
("What You Can Ask"), sanitized and capped (6 × 200 chars). Clicking a hint **pre-fills
the composer, never auto-sends** (`PortalBriefing.vue` → `prefill`). The future curated
exposable-skills config (ent#178) slots into this same seam. A chat holds one agent —
or, where the capability below is present, several — and the picker starts a new chat
either way, so hints scope to the active agent by construction. ent#380 also fixed the
briefing's metadata read — #138 called a nonexistent agent `/info` route, so descriptions
were silently always `None`.

**Briefing hint grid is bounded (#2101):** with no connector allow-list configured every
`user_invocable` skill becomes a "Things you can ask" card, so the hint set is belted
server-side (`_agent_briefing` ships ≤24 — one final slice at the return so it binds
whichever tier populated the list) and folded client-side (`PortalBriefing.vue` renders 6
described-cards-first via `portalUtils.planHintDisplay`, the rest behind a counted
in-place "Show all N" toggle — deliberately **no nested scroll region**: the chat pane
stays the single scroll axis, and the toggle counts the shipped list, never claiming the
agent's full skill set). Hint *curation* stays the connector allow-list (ent#178 later).

**Composer typeahead — `/` playbooks, `@` agents (ent#392).** The same briefing payload
now also feeds a composer typeahead, which is what makes it reachable after turn 1 (the
hint cards render on the empty-chat screen only). `/` lists the active agent's
`playbooks[]` and splices the `starter_prompt` in **without sending** — the ent#138
prefill contract; `@` lists reachable agents and inserts a token `mentionedAgents()`
resolves (ent#361). All decidable logic is pure and exported from
`components/portal/portalUtils.js` (`vitest` runs `environment: 'node'` with no
component-mount harness, so a decision inside a component is one no test can reach); the
components are dispatchers over it and `PortalTypeahead.vue` is presentational. Three
properties are load-bearing rather than stylistic: the **trigger rule is stricter than
the parser** (`MENTION_RE` is unanchored, so `user@example.com` parses as `@example` —
the popup must never open on something the parser would not see); **un-mentionable slugs
are excluded**, the predicate *derived* by asking `mentionedAgents` rather than copying
the grammar, because `sanitize_agent_name` keeps `.` and caps nothing while the grammar
allows neither, so listing `data.scout` would manufacture the silent
degrade-to-plain-text this feature exists to close; and **a plain Enter never accepts
without an explicit selection**, since an accidental accept destroys typed work while an
accidental send is what the user was reaching for. `@` is hidden — popup *and*
placeholder — without the rooms capability, which it reads from the roster payload per
the rule below. The **room** composer gets `@` scoped to its **agent participants** —
established by *observing* the running server rather than reading the private rooms
engine (`POST /api/rooms/{id}/messages` answered `woke: ["<participant>"]` for a
participant and `woke: []` for a non-participant), so the list contains only names a
pick is known to wake; whether a non-participant mention still joins someone by the
ent#361 engine-side path (§5.12) is deliberately not claimed either way, and recruiting
stays with the explicit "+ Add agent" control. `/`-in-room is deferred — a room has no
active-agent subject. **No backend change, no new endpoint, no
migration. OSS-core by decision (ent#392): deliberately ungated — no
`requires_entitlement`, logic stays in the OSS tree. Recorded explicitly because
CLAUDE.md's default for an enterprise-tracker feature is gated unless ruled otherwise, so
the ruling must never be inferred later from the mere fact that it merged.** See
[workspace-composer-typeahead.md](../feature-flows/workspace-composer-typeahead.md).

**Voice mode — the orb takes the conversation (ent#534; supersedes ent#440).** The
Workspace's voice is the platform's **real-time voice session** (`routers/voice.py` +
`services/gemini_voice.py`, the platform orb `VoiceOverlay` + `useVoiceSession`,
reused not forked — and **since #2559 the Workspace is its only consumer**), run as a
**modal call** inside the conversation: the orb covers the
thread, the header controls / tabs / composer are visible but inert, the shell swaps the
rail for the agent's canvas (`PortalVoiceCanvas`, 40/60), and End / Escape returns to the
chat. It is bound to the **Workspace thread**: `POST
/api/enterprise/client-portal/agents/{name}/voice/start` under `get_portal_principal`
(platform principals only; off-roster, a foreign thread and a portal token are ONE uniform
404; `_require_roster` → rate limit → 503-with-reason) calls
`client_portal/voice.py::start_workspace_voice`, which builds the context from the thread's
recent rows and creates the session with `portal_session_id` + `client_email`,
`canvas_audience="operator"` and `max_duration=WORKSPACE_VOICE_MAX_DURATION` (1800 s).
The transcript is **written turn by turn on the worker holding the live socket**
(`persist_voice_turn`, called from the bridge's `on_turn`) as `enterprise_portal_messages`
rows carrying `source='voice'` + `voice_call_id`; the call closes with one `system` label
row and `touch_portal_session`; a call with no turns writes nothing; `/stop` never writes
for this surface. Save-at-end was rejected by both independent plan reviews: under two
uvicorn workers a `/stop` landing off-worker reconstructs an EMPTY session from Redis and
would write a phantom call, and a restart mid-call would lose it all. The chat folds one
call's rows into a collapsed "Voice call · N min" block **keyed on the call id, never an
opener row** (`portalVoiceMode.js::groupVoiceBlocks`) — a long call is ~180 rows, and
since trinity#2694 the history window is counted in **typed turns** (the newest 100, the
spoken rows of the calls among them riding along, under a row ceiling that reports
`truncated`), so a call can no longer push the typed turns before it off the screen; the
reply poll reads `?limit=N` (row semantics) and finds the reply by identity, never by
count. The agent's next typed turn is told what was said: a resumed turn is prefixed with
the platform-written rows since its last typed reply (`get_platform_rows_since_last_reply`
→ `_format_voice_delta`), the cold replay carries the same rows in the same form under one
24k spoken-char budget, and the two are mutually exclusive — a call is refused (409) while a
typed reply is in flight and a typed turn is refused (409) while a call is on — see
[workspace-voice-conversation.md → One timeline](../feature-flows/workspace-voice-conversation.md#one-timeline-and-the-agent-knows-what-was-said-trinity2694).
The session must outlive the provider connection: every
session asks for context-window compression + session resumption and `connect_and_stream`
reconnects on `go_away`; the cap speaks a wrap-up at T-30 s and ends with a reason the
`status` and `saved` frames carry, so the surface reloads the thread only after the rows
exist. The roster carries `realtime_voice {available, reason}` (platform principals only,
fail-closed, named for the capability — ent#354 may add a second provider behind the same
field; the per-agent `voice_available` still means "has a TTS voice"). A platform
principal reads **every** canvas audience in the Workspace (`agent_page.canvas_audience_for`;
they already can on Agent Detail), a portal-token client stays `roster`. The ent#440
hands-free STT→turn→TTS loop is **retired** by the same ruling (one voice entry point);
hold-to-dictate (#2212) and spoken replies (#2157) stay as composer affordances. **OSS-core
by the standing ruling, deliberately ungated.** See
[workspace-voice-conversation.md](../feature-flows/workspace-voice-conversation.md).

**Thread readability, copy, new-tab entry, agent search (#2515 / ent#456 / ent#402).**
`components/portal/PortalMarkdown.vue` is the single home of the rendered agent body — the one
`v-html`, the one `.prose-portal` stylesheet, the one delegated code-copy handler — and
`PortalAgentBubble.vue` is the chat chrome around it, mounted by both transcripts. Before this the
stylesheet was applied in two SFCs and defined in both, kept "byte-identical so the two cannot
drift", which is the shape a thing takes when it wants to be one thing; a future surface rendering
agent markdown outside a bubble (the ent#486 Files tab) mounts `PortalMarkdown` and inherits render,
style and copy as a unit instead of re-copying two of the three. **`renderMarkdownWithCodeBlocks` is
a SECOND export, never a `marked` renderer override** — `renderMarkdown` has twelve consumers and a
global override would sprout a Workspace copy control on dashboards, queue cards and reports; its
body is byte-identical. **Order is the security of it:** `marked → stripCodeBlockMarkers →
decorateCodeBlocks → DOMPurify.sanitize`. The markers are stripped from the INPUT first because
marked passes raw HTML through and DOMPurify keeps `data-*`, so an agent could otherwise ship a
forged wrapper whose Copy resolves to a hidden `<pre>` (pastejacking); decoration runs BEFORE
sanitization so every byte reaching `v-html` has passed the one policy (H-005 stays literally true);
and the decorator matches only the shapes marked actually emits — the BARE `<pre><code` opener
carrying nothing but an optional `class`, over a body with no literal `<`. marked escapes fence
contents, so a `<` proves raw-HTML passthrough (a block that could nest a `display:none` element
the copy would silently pick up), and an attribute marked never writes proves the same thing on
the opener: DOMPurify keeps `hidden` and `style`, so a raw `<pre><code hidden>` would otherwise get
a real Copy button over a block that renders empty. The one non-constant byte
injected is the charset-validated language label; the scanner is a linear `indexOf` walk (the lazy
regex it replaced was quadratic on adversarial input, on the render path). `utils/markedConfig.js`
is the ONE marked configuration and exists so a spec can exercise the configured parser —
`markdown.js` cannot be imported in a DOM-less node process (DOMPurify's stub has no `addHook`), so
without the split a future highlighter could change fence output while the spec stayed green and
every Copy button vanished. `utils/clipboard.js::copyText` returns a result, never throws, never
logs the copied text (it may be the credential the operator just asked for), and falls back to
`execCommand` on an insecure origin — plain http on a LAN or Tailscale address is a first-class
Trinity topology — so the controls say "Copy unavailable / blocked / failed" only when copying
genuinely cannot happen; its pre-existing sibling `copyToClipboard` (four settings-panel callers) is
left byte-identical and converging them is a follow-up. Blocks WRAP (`pre-wrap` +
`overflow-wrap: anywhere`, no `overflow-x`), so a bubble never widens its column; the copy reads
`textContent`, so wrapping is display-only and ASCII-table alignment is the accepted cost. Both
console entry links are `_blank` + `rel="noopener"` (Vue Router's `guardEvent` declines to intercept
those and modified clicks, so there is no `window.open`) while the `?tab=session` redirect stays
deliberately same-tab — it rewrites a navigation in flight rather than starting one. Sidebar search
reuses `filterAgentCandidates` with **`requireMentionable: false`** (a row is not a mention, so
`data.scout` stays findable) and bounds results through `visibleAgentRows`, so an ask-bearing match
is never collapsed out of its own result; both empty lines are per-section, and the roster skeleton
outranks them while the roster is still loading. **OSS-core by decision (ent#456 / ent#402):
deliberately ungated** — no `requires_entitlement`, logic stays in the OSS tree. Recorded explicitly
because CLAUDE.md's default for an enterprise-tracker feature is *gated unless ruled otherwise*, so
the ruling must never be inferred later from the mere fact that it merged. See
[workspace-thread-code-blocks.md](../feature-flows/workspace-thread-code-blocks.md).

**The briefing is off the roster's critical path (#2163).** `get_roster` used to fan
`_agent_briefing` across every card and `await asyncio.gather(...)`, which waits for
ALL — so the Workspace's first paint was bounded by the SLOWEST agent in the fleet, for
every user, on every sign-in, regardless of fleet size. "Best-effort and parallel"
bounded the blast radius (a failing agent left defaults) but never the LATENCY. The
roster now awaits no agent HTTP at all, and `GET /api/enterprise/client-portal/briefings`
hydrates the briefings after it — viewer-scoped like `/sessions`, with an optional
`?agents=` filter whose names are only ever tested for SET MEMBERSHIP against the roster
(the string that reaches `agent-{name}:8000` is always a DB row value, so a crafted name
cannot steer the target, and an unknown one is dropped rather than answered). Two forms,
two per-viewer limiter keys: the unfiltered batch is far tighter, because one call to it
costs one bounded agent request per rostered agent. It makes **no Docker read** —
`_agent_briefing` attempts `unknown` by design (it reaches the agent by DNS, so
container state says nothing about whether it answers HTTP), so a stopped container
refuses the connect, no leg of the briefing gets an answer, and it lands `unavailable`
— the same verdict a skip would give, one fleet Docker call cheaper. Every remaining briefing — the batch and the agent
page's single one — runs under ONE bound: `_BRIEFING_HTTP_TIMEOUT_SECONDS` (2.0, httpx,
PER PHASE) inside `_BRIEFING_BUDGET_SECONDS` (3.0, wall clock, `_bounded_briefing`); the
literal `5.0` it replaces was never a ceiling, because the function makes two sequential
GETs. The result rides the card as **`briefing_state`** — `pending | ready |
unavailable`, all three SERVER-owned, defaulting to `"ready"` so an older payload reads
as resolved-inline. A bound trip must never pass for an agent that genuinely has no
hints, and a headless ent#83 client must not have to reinvent the third value from empty
fields; `ready` means THE AGENT ANSWERED inside the budget, not "returned data". **The
verdict follows reachability, never which door the failure exited by** — the first cut
got that wrong and measurement caught it: `_agent_briefing` swallows HTTP failures in a
`try/except` per GET leg AND an outer one, so a wedged agent (httpx `ReadTimeout`) and a
missing container (`ConnectError`) both returned an ordinary empty briefing INSIDE the
budget and read `ready`, i.e. exactly the hint-less card this field exists to prevent —
and since the client retries only `unavailable`, it never asked again that session. Only
the tarpit shape, which trips the wall clock, was correct. So reachability is now
reported separately from content: every exit of `_agent_briefing` that got no answer out
of the agent returns the `_UNREACHED` sentinel, which `_bounded_briefing` reads by
IDENTITY (it compares EQUAL to an empty briefing an agent legitimately produced, and
that one must stay `ready`). One leg answering is enough — the client renders
`unavailable` INSTEAD of the fields, so a half-answered briefing must not throw away the
description it did get. It is a **data-state marker, not a capability** — the #2128 rule
below is untouched. API
consumers that want the briefing make the second call. On the client, the three loading
zones (stage, conversation body, briefing) each key on their own "no data yet" and —
since #2540 — render a **skeleton placeholder** (`components/portal/PortalSkeleton.vue`),
never the scanline beam, which is the CHART-loading motion (design-system principle 12
as amended 2026-09-06; `ScanlineReveal` keeps the additive `content-class` prop #2163
gave it, for its chart consumers). The gates read `stage.state` / `!historyLoaded` /
`zone.state`, never a bare `<x>.loading` path, because that spelling is what the #1927
ratchet counts as a bare loading gate.
See [workspace-roster-briefing.md](../feature-flows/workspace-roster-briefing.md).

**The conversation rail (trinity-enterprise#474, slice 1 of #472).** A collapsible third
column — a **sibling of `<main>`** in `Portal.vue`, so the conversation and the room
render into the same rail and its state (a setup ref, persisted under
`localStorage['trinity-workspace-rail']`) rides no remount: a chat switch remounts the
conversation, never the rail. 48px collapsed / 384px open, collapsed by default, hidden
on the agent page and on every stage that holds no conversation — visibility is keyed on
the route and the stage VERDICT (`railVisibleFor`), never on data still arriving, so a
room whose participants have not landed keeps its rail. `components/portal/portalRail.js`
is the **tab contract**: a tab declares `door` (`platform` / `audience` / `agent`), its
participant `scope`, its `empty` state and its `signal` shape, and `visibleTabs` is the
ONE gate for render AND mount — `PortalRail` never reads the registry, mounts a body for
exactly the active tab, and renders no chrome at all for an empty list, so a tab whose
door the session fails is never asked for its data (an external client sees no rail
until an audience tab docks). The collapsed strip carries two signal shapes in one hue
— *live* (ringed, `motion-safe:animate-pulse`) and *updated* (plain) — DERIVED on every
render from the conversation's in-flight turn or the room's server-reported `working`
list, and reset on every chat switch, so it structurally cannot stick. A room groups
every tab by participant over `portalLoopUtils.byAgent`, absence visible. Below `sm` the
column is a strip above the composer (`PortalRailStrip`, through the two components'
`#rail-strip` slot) that opens the same component as a bottom sheet. `OverflowTabs`
gained an optional per-tab `signal` (measured in its mirror row) for the label dot. The
first docked tab is **Work**, empty by the operator's split — #457's Activity docks into
its `#tab-work` slot; loops / canvas / files re-home in #472's second child; #492 lands
the grid variables the rail's widths then follow. See
[workspace-rail.md](../feature-flows/workspace-rail.md).

**Work — the live card and the rail's first tab (trinity-enterprise#525, the visual half
of ent#457).** `client_portal/work/` is the read (`GET …/client-portal/work?agents=&chat_id=`,
**platform door only** — a portal token gets a uniform 404 before any read, ent#78's
auth-path invariant restated by the 2026-09-06 ruling; the frontend's `visibleTabs` gate is
UX, this line is containment). It projects the executions ledger for the person who asked:
*Now* (in-flight rows), *Earlier* (terminal rows inside a 30-day window, bounded at 30 with the
window total counted server-side) and, given the open thread, that chat's **delegated
children** — found by `source_channel_chat_id`, never by agent, because a child's
`agent_name` is the delegate (ent#265 D0 / #2386 copy the chat binding onto the child row;
`idx_executions_status` drives the in-flight selector, so no migration). The DB queries are the
fleet dashboard's (`get_fleet_executions` / `get_fleet_execution_stats`, which gained
`source_channel`, `source_channel_chat_id`, `loop_id`) under the **portal roster** — never the
operator fleet ACL, which resolves through `list_all_agents_fast()` and answers `[]` on a
Docker fault (the #2196 class). Every name on the payload is roster-masked (a child on an agent
the caller cannot see is a step, unnamed — the ent#467 disclosure class); `title`/`error` are
sanitized and bounded; only a `portal` stamp becomes a `chat_id`. **Honesty rules:** a RUNNING
row past 1.5× the agent's turn bound (floor 30 min) is `stale` — not live, no clock, no
signal, no poll — because the 120-minute sweep leaves ghost rows after a restart; `can_stop`
mirrors exactly what the terminate route accepts; steps are **three-state** (`reported` /
`none` = "doesn't report steps" / `unknown` = stopped, unreachable, unreadable, or two runs on
one agent so no instance can be attributed) — a stopped agent must never be described as one
that does not report. The #919 read (`work/pipeline_state.py`) mirrors `pipelines.ts`'s
hardening: id grammar before any path, `size` from the listing before the download, a streamed
byte budget, `safe_yaml`, no retries, 2 s per call in a 3 s wall budget, a 10 s per-agent
cache. **Stage advances are pushed (ent#533):** the agent server watches its own
`~/.trinity/pipeline-state/` and POSTs `/api/agents/{name}/pipeline-state/changed` with its own
agent-scoped MCP key (the #307 heartbeat's auth, never the internal secret and never an admin
gate); the backend coalesces 20 notices / 10 s per agent and publishes the thin
`pipeline_state_changed` trigger (ids + stage, `agent_name` top-level so ent#467 scopes it).
The notice ALSO bumps a **Redis generation** that the 10 s cache checks alongside its TTL —
`_cache` is per-process and prod runs `uvicorn --workers 2`, so a purely in-memory
invalidation would refresh the receiving worker and leave the other one stale for up to 10 s
while dev's single `--reload` worker passed green. Fail-open: Redis down ⇒ generation `None`
on both sides ⇒ today's TTL-only behaviour. The poll is untouched — a dropped notice is
invisible. Frontend: `stores/portalWork.js` is fed by `usePortalRailFeeds` (the ONE owner), polls
12 s **only while something is live**, refreshes on `agent_activity` (started AND terminal) and
loop events for a participant (debounced 2 s) and on `pipeline_state_changed` (500 ms), where
the **earlier deadline wins** (a later 2 s push must not postpone a pending stage refetch) under
a 1500 ms floor between push-driven refreshes (a burst must never reach the per-viewer
120/60 s read limiter, whose 429 renders as visible error text); the Work signal is **one merged set** — the
feed's live rows plus the conversation's in-flight emit joined **by execution id**
(`workSignalFromItems`), never two signals summed. `PortalWorkCard` is the one card for the chat
(under the message; the stream's last line is the current step while live; the terminal card
renders FROM the durable #2320 verdict, so it survives a reload; **Ask about it** is a prefill,
never a send — the ruled lesser control) and for `PortalWork`, the tab body (Waiting on you =
`PortalAsks` over `store.asks` filtered to participants, the fourth rendering of the ask row;
rooms grouped by participant, absence visible). **OSS-core by decision (ent#525): deliberately
ungated.** See [workspace-work.md](../feature-flows/workspace-work.md).


**The roster payload is *the* portal capability channel (#2128).** A portal principal
cannot read `GET /api/settings/feature-flags` — that endpoint is `get_current_user`-gated
and the frontend store behind it returns `[]` for any caller without a platform JWT, i.e.
for **every** external client, including on an instance where the capability is present.
So any UI gate on this surface takes its signal from `PortalRoster`, which
`get_portal_principal` already serves to both principal kinds and `Portal.vue::bootstrap()`
already awaits first — one field, no new route, no new auth surface, no extra round-trip
(`voice_available` is the per-agent precedent). Reach for this before adding a second
channel: the platform entitlement store is structurally unavailable here.
`multi_agent_chat_available` is the first such field — resolved once per roster load from
the entitlement registry, **fail-closed** (an unreadable registry reports the capability
absent, because promising an affordance that cannot work is the bug it fixes), and named
for the *capability* rather than the module or the edition, since this payload is served
to an operator's customer. When it is false the picker is single-select, all five room
store actions refuse before issuing a request, and `/workspace/r/:roomId` renders an
honest refusal instead of mounting the room; a 404/403 from any of the five self-heals the
flag mid-session, so a capability that lapses between roster load and confirm — or while a
room is open, which nothing else converges, since the sidebar refresh is event-driven — is
observed by the next room call rather than dead-ending. **The status alone is not the
signal**: a serving module authors its own refusals as a structured `detail: {code, …}`
(*you cannot reach that agent* → 403, *you are not in that room* → uniform 404), while
absence is a plain string — the framework's own "Not Found" for an unmounted route, the
entitlement gate's sentence for mounted-but-unlicensed. Only the string form lowers the
flag; a coded refusal is passed through so the server's own words reach the user, because
reading one denied request as absence would turn it into a session-long false claim about
the operator's build. The frontend gate is **UX, not
containment** — a portal token legitimately reaches the room endpoints where they exist,
and the real boundary is the serving module's own roster-scoped access plus
membership-scoped uniform 404s. Room data is untouched by the flag and reappears intact
if the capability returns.

**`availability` is the second per-agent field on this channel, and it fails in the
OPPOSITE direction (#2196).** `voice_available` and `multi_agent_chat_available` default
**False** (fail-closed) because their bug is *promising an affordance that cannot work*.
`availability` defaults **`"unknown"`** (fail-open) because its bug is the mirror image:
*denying a working agent*, and — since a Docker fault would mark every card at once —
*emptying a paying customer's roster over an infrastructure fault*. Same payload channel,
opposite default, for a stated reason; the asymmetry is deliberate and is written into
`PortalAgentCard` itself so it is not later "tidied" into consistency. It is resolved from
the tri-state pair `docker_service.agent_container_states()` (batch, one **sparse**
`containers.list()` per roster load) / `agent_container_state(name)` (single, for the
agent page and each turn — routing one agent through the batch would make it pay a
fleet-scale read, against #2160). Those two exist because no pre-existing Docker helper
can distinguish *no container* from *Docker unreadable*: both return a falsy value, which
is the single fact this design turns on. Both are awaited through a `docker_utils`
executor wrapper (that module's mandatory async contract), and `list_all_agents_fast`'s
`[]`-on-fault contract is deliberately left unchanged — ~60 stub sites depend on it. The
portal seams `_availability_map` / `_agent_availability` are isinstance/enum-guarded (the
`a2a_outbound` precedent, here failing in the safe direction) so a `sys.modules` MagicMock
stub degrades to `unknown` rather than silently inverting the default inside the suite
meant to prove it; `_availability_map` also narrows its result to the requested names,
because the underlying call sees **every** agent container on the host.

**`model_options` + `model_default` — the composer's model choice (ent#403).** The curated
option list rides the **roster**, not the card: it is identical for every agent (the
`realtime_voice` / `multi_agent_chat_available` precedent), so putting it per-card would
ship N copies of it on exactly the path #2159/#2163 exist to keep small. Only the resolved
default varies per agent, so only that is a card field — which also means `get_agent_card`
needs no payload change. Both **fail closed**: `model_default is None` renders no control,
and that is the value for every non-platform principal and for a **non-Claude runtime**
(the platform passes no `--model` to the Codex runtime at all, so a Claude-model list
there promises something and changes nothing). Three things are resolved **once per roster
load** beside `tts_ready` and the default voice — the option list, the platform default,
and its label — and threaded into `_row_to_card` as `model_context`; `is_platform`,
`runtime` and `model_context` are **keyword-only with no default**, because a default
would let the agent-page call site keep compiling while silently serving the wrong card.
The runtime comes from `docker_service.agent_container_runtimes()`, a **second** sparse
`containers.list()` — O(1) in fleet size, not the N+1 #2160 forbids, and a separate leaf
rather than a widening of `agent_container_states()` so #2196's guard suite keeps pinning
what it pins. Read **sequentially**, not with `asyncio.gather`: #2163's guard pins that
`get_roster` contains no fan-out at all, and that blanket shape is the point — two fixed
O(1) reads are not the N-agent fan-out it closed, but loosening a guard to admit one's own
change is how the property stops being true. The trade is ~50-200ms once per roster load. Note the
sparse trap in its other form: under `sparse=True` docker-py's `.labels` **raises** (it
reads `attrs["Config"]["Labels"]`, which only a full inspect populates), so the runtime is
read from `attrs["Labels"]` — the key the `/containers/json` summary actually carries.
An unreadable runtime falls back to `claude-code`, matching `get_agent_runtime`'s own
documented posture and `availability`'s fail-open direction on this same payload.

**The turn's model is resolved once, at a specific line.** `resolve_turn_model` is the ONE
ladder for both portal turn routes — requested → the agent's #894 `public_channel_model` →
the **platform default as a concrete id** — and it takes no principal, which is what makes
"the streaming route and the synchronous ent#83 route cannot disagree" true by
construction. It runs **immediately after the availability gate**, before anything is
created, because `schedule_executions.model_used` is written ONLY at row creation and both
portal paths pre-create the row: resolving where the value is *used* would stamp the
pre-created row `None` and half-fix the requirement on exactly the path #2426 already
burned. **The last rung is a concrete id and not `None` for that same reason** (review,
2026-09-08): `execute_task` resolves the platform default at `:1044` but stamps it inside
`if not execution_id:`, so on a portal turn the resolution happens and the stamp does not —
`None` recorded NULL for the default state of every agent, which is most Workspace turns. It
reads `settings_service.get_platform_default_model()`, the same function `execute_task`
calls, so the two hold one opinion and `execute_task`'s lookup becomes a no-op; `None` still
reaches the row only when that read itself fails. A caller passing `execution_id` must pass the `resolved_model` it stamped —
`resolved_model or resolve(...)` would let the row and the turn disagree, so that path
raises. The requested value cannot simply be re-laundered through the composer's allow-list
either: an inherited `public_channel_model` may legitimately sit outside the curated set.
The router owns normalise-then-authorise-then-allow-list (blank → `None` **before**
validation, or the control's own `""` default 422s every default turn), and the closed
`WORKSPACE_MODELS` set is the security control, since the value reaches the agent as a
`--model` argv element.

### Multi-Agent Rooms (ent#169; OSS core since ent#443)

`src/backend/shared_sessions/` — the substrate behind a Workspace chat that holds
more than one agent. **One idea:** *a room is a shared persistent RECORD, never a
shared CONTEXT.* Each agent keeps its own isolated Claude session and, before it
speaks, is handed only the transcript it has not seen (`participants.last_read_seq`);
that is why a room does not cost N× tokens and why no LLM has to decide who talks
next — turn-taking is mechanical: **you are woken iff you were @mentioned**.

- **Two routers, mounted unconditionally in `main.py`**: `/api/rooms` (membership-scoped;
  any authenticated principal, and a Workspace client via the `get_room_principal`
  fallback, ent#362) and `/api/enterprise/room-budget-defaults` (admin-only operator
  defaults, ent#387). The second is deliberately NOT under `/api/rooms` — a
  `/budget-defaults` path there would sit beside `/{room_id}`, one ordering slip from
  being read as a room id (Invariant #4) on the one surface whose reader must never be
  a client.
- **Turn engine** (`service.py::post_message` → `_wake_agent`): mentions resolve against
  participants; each woken agent runs an **ordinary** `execute_task(triggered_by="room")`,
  so slots, the circuit breaker, cost and observability come for free, and its reply is
  auto-posted back. An agent never re-wakes itself; only a **human** mention recruits a
  non-participant (an agent that could pull agents in is a spend amplifier and a
  prompt-injection lever). Chain depth, a per-participant wake cap, and the ent#220
  cancellation shield bound the cascade; the ent#218 rule keeps an in-flight reply that
  was already billed from being discarded by a budget trip.
- **Three tables** — `enterprise_rooms`, `enterprise_room_participants`,
  `enterprise_room_messages`. The `enterprise_` prefix is **retained history, not a
  licensing claim** (the ent#356 portal precedent): every entitled install already holds
  live transcripts under those names, so renaming them would be exactly the data
  migration the move forbids. DDL in `db/schema.py`, versioned on the OSS two-track
  runner (`db/migrations.py::shared_sessions_tables_to_oss` + Alembic
  `0044_shared_sessions_oss`), both `CREATE TABLE IF NOT EXISTS` so adoption is a no-op
  on an install that already has them. The enterprise Alembic `0011_shared_sessions`
  stays on its own line — deleting it would break that chain — and is idempotent.
  The revision chains off **`0038_portal_chat_state`, `main`'s head** — this landed as a
  hotfix onto `main`, and 0039-0043 exist only on `dev`, so pointing at `dev`'s head
  would name an absent revision and fail boot on the line it ships to. That forks the
  two lines at 0038 by construction; the fork surfaces as two heads at the main→dev
  back-merge, where `check_alembic_heads` fails loudly until an `alembic merge` revision
  (`down_revision = ("0043_subscription_headroom_history", "0044_shared_sessions_oss")`)
  collapses it — the fix is that merge revision, never renumbering a revision already
  applied wherever the hotfix went. The file is numbered 0044 rather than 0039 so its
  prefix does not collide with `dev`'s `0039_operator_queue_addressed_to` after the
  back-merge (ids are strings, but the numeric prefix is the graph's only human ordering
  cue); `test_ent443_rooms_oss_core.py` pins both the parent and prefix-uniqueness.
- **Agent-identity columns are POLYMORPHIC and registered kind-scoped** (ent#443):
  `participants.identity` and `messages.sender_identity` hold an agent name, a platform
  user id, or a workspace client's verified email depending on the sibling `kind` /
  `sender_kind`. Both are in `AGENT_REFS` with an `extra_filter` (`kind = 'agent'`), so
  rename re-keys and purge cascades **only** the agent rows; an unscoped ref would
  rewrite — and on purge delete — a human participant whose id or email happened to
  equal the agent's name. The forward parity regex cannot see either column
  (`identity` is too generic to add to `_AGENT_ID_COLUMNS`), so
  `tests/unit/test_ent443_rooms_oss_core.py` pins them explicitly and
  `test_agent_cleanup_parity.py` carries a documented `_POLYMORPHIC_AGENT_COLUMNS` set
  for the backward direction.
- **Why OSS.** It was the entitled `shared_sessions` module, 404ing in community builds
  — while the frontend that drives it (`components/rooms/`, `stores/rooms.js`, the ent#392
  composer typeahead) and the MCP tools (`src/mcp-server/src/tools/rooms.ts`) shipped in
  **every** build and self-disabled. Three of four surfaces were already public, so gating
  only the backend left an OSS install rendering an affordance it then refused. Workspace
  itself moved for the same adoption reason (ent#356), and rooms are the half that makes
  it the place people work with agents rather than a second 1:1 chat.
- **`PortalRoster.multi_agent_chat_available` stays on the payload** and is now
  unconditionally true. It is the portal's ONLY capability channel (#2128) — a portal
  principal cannot read `/api/settings/feature-flags` — and the shipped bundle gates the
  picker, five room store actions and `/workspace/r/:roomId` on it, so deleting the field
  would make all of them read `undefined` and hide the feature this move exposes.
- **Transition ordering (load-bearing):** the OSS routers are included in `main.py`
  **before** `register_enterprise(app)`, so on an install whose submodule has not yet been
  bumped both routers mount and the **ungated OSS one wins** the match order. Pinned by
  `test_ent443_rooms_oss_core.py`.


## Agents at the centre — Main, Reset, and the one page (ent#523, ent#524)

Clicking an agent opens the **conversation** you were last in. `/workspace/a/:agentName`
keeps its URL and resolves to a chat; `PortalAgentPage.vue` is gone, dismantled into an
always-visible band and an on-demand details panel. ent#360's reasoning is not reverted
— an agent still has a home with its history, what it can do, and a place to ask you
something — it is simply no longer a STOP on the way to the conversation.

**Main.** Every `(user, agent)` pair has one pinned Main chat: the place the agent
reaches you when no conversation named itself. `enterprise_portal_sessions.is_main`
marks it and `archived_at` marks the one Reset retired (both tracks: SQLite
`portal_session_main_chat` + Alembic `0055`, no backfill). Uniqueness is the **partial
unique index** `idx_portal_sessions_main` (`WHERE is_main = 1`), not a check-then-insert:
`ensure_main_session` is reachable from two request paths and runs in every uvicorn
worker, and the loser of the race catches `IntegrityError` and adopts the winner's row.
The predicate is load-bearing rather than an optimisation — an archived row keeps its
`(agent_name, client_email)` pair forever, so an unconditional unique index would refuse
the **second** Reset. Main is minted lazily by `list_sessions` (opening an agent, which
is what renders the pinned tab) and by `_resolve_session_id`, and deliberately **not** by
the cross-agent batch (#2198), which runs on every sidebar refresh and would write a row
per agent the person has never opened.

**The landing rule is one edit.** `_resolve_session_id(agent, email, None)` resolves to
Main rather than the most recent thread, which is the whole of the rule for an
agent-initiated message, an ask raised outside a chat (ent#364/#429) and a scheduled
brief (ent#498) — all three already funnel through it via `ensure_thread_for_ask`. An
explicit session id still wins.

**Reset needs no second reset primitive.** `POST …/sessions/main/reset` archives the
current Main and mints a fresh one in ONE transaction (clear the flag before the insert,
or the index refuses it), then writes one `role = 'system'` line in the new Main naming
the archive. "Starts cold" is a property of the new row — it carries no
`cached_claude_session_id` and `session_turn_service` resumes only on a cached id — so
`routers/sessions.py::reset_session_memory` is untouched and uncalled: clearing a cache
and keeping the thread is a different verb from retiring the thread. The archive keeps
its own cached id (still resumable, still in `session_cleanup_service`'s keep-set) and
its own title; only an untitled one is named, and dated, because these accumulate in one
list. Refused with a named 409 while a turn is in flight (`turn_in_flight`) or when a
concurrent Reset won (`reset_raced`); resetting an untouched Main is a no-op reported as
`archived_session_id: null`. **No confirmation dialog** (operator, 2026-09-06) — nothing
is lost, and `ConfirmDialog` is not a caller here.

**One page.** `PortalAgentBand.vue` carries the stats strip and the Activity chart under
the header, always visible (operator, 2026-09-06), and is the **only** surface on this
page entitled to the scanline (#2540 — it is the chart-loading motion). `PortalAgentDetails.vue`
carries chats, what it can do, and reports. It opened **into the rail's place** as a
SIBLING of the rail, never a rail tab (ruled 2026-09-05); **ent#547 reversed that on
2026-09-07** and it is the rail's **Info** tab — see "The compact header" below for the
door, the room restriction and why the 2026-09-05 objections are answered rather than
dropped. Both read one payload through `composables/usePortalAgentPage.js`.
Canvas, Files and recent work are **not** duplicated here — they have been rail tabs since
ent#475/#525. `portalUtils.js::landingThread` is the one rule for which chat you land in,
and the `?agent=` deep link's `resolveAgentLanding` defers to it so the two entry points
cannot disagree. Main is named by its role in both the tab strip and the header and is
not renameable; an archived chat stays a tab (ruled “becomes the newest tab”), though you never LAND in one by default;
an unused Main is filtered from the **sidebar** only (a projection, not a filter on
`threads`, because the strip must show Main from the first visit).

**Files onto the conversation (ent#524).** `composables/usePortalFileDrop.js` is the ONE
drop/batch implementation, used by the conversation, the room and the rail's Files tab —
the issue forbids a second, and the defect it fixes was exactly that each surface had
written its own `files?.[0]` and reported success while discarding the rest. Both
`<input type="file">` carry `multiple`; every file gets its own chip, progress and
outcome; a refused file names itself and the limit; a 429 batch says which files landed
and when to retry. Uploads run **sequentially** — twenty parallel requests is the surest
way to trip the per-email limiter (ent#287). A room's drop fans out to every
participating agent's inbox and the chip names the recipients (operator decision 13). The
destination is the caller's `upload`, so the ent#484/#486 working folder can take it over
without the gesture changing. **Since #2582 the upload also announces itself to the rail
owner**: `stores/clientPortal.js::uploadDocument` — the single funnel all three surfaces
already call — adds the agent to a pending SET that `usePortalRailFeeds` drains into a
targeted inbox re-read. A set and not a scalar, because the two real gestures both defeat
a scalar: a multi-file batch uploads sequentially without awaiting the re-read (a listing
snapshotted before file 2 landed), and a room's fan-out mutates the signal once per agent
inside one Vue flush window (only the last survives). The drain coalesces leading AND
trailing and is ordered against `refresh()` by a **per-agent inbox epoch** the refresh
snapshots before its awaits, so a refresh issued before the upload but resolving after it
cannot clobber the fresh listing. Per agent and not one shared token, because a room's drop
runs three of these concurrently for three different agents and a shared counter lets each
invalidate the last.

**The Files tab's own verbs (#2582 + ent#548).** Rows render from ONE flat projection
(`components/portal/portalFiles.js::flattenFiles`) that owns both the render order and the
preview index — two orderings would drift and the modal would silently open the wrong
file. Download on either list; a preview modal for images and displayable text with
next/previous over the *previewable* subset; and a delete whose affordance depends on the
case:

| Case | Affordance | Mechanism |
|---|---|---|
| My own upload | **Delete** (real) | `rm -f --` in the container inbox |
| Agent-shared, I am a viewer | **Remove from my list** | a `portal_file_dismissals` row; the share is untouched |
| Agent-shared, I am the owner **in a platform session** | both, "Delete for everyone" offered | `db.revoke_agent_shared_file` (soft; the sweeper reclaims bytes) |

**The matrix is session-type dependent and the UI copy says so.** `PortalPrincipal` is
`(email, is_platform)` and carries no role, so `include_owned` is `principal.is_platform`
at every call site (ent#358). Therefore a **non-owner admin is a viewer here** — stricter
than the platform surface, and correct — and an **owner signed in with a magic-link portal
token also gets the viewer affordance**. `portal_owns_agent` is the same membership the
roster card renders, so the UI and the enforcement cannot disagree: the affordance is
simply not offered rather than offered-and-refused. `portal_file_dismissals` is per-viewer
storage because `agent_shared_files` has no audience column and `user_ui_preferences` is
FK'd to `users.id`, which a portal principal has no row in.

## The compact header — Info as a rail tab, one paperclip, voice at the composer (ent#547, #2580)

The band is **compact**, and the three controls that were not about the conversation have
left the header. Ruled by the operator on 2026-09-07 after testing `dev`.

**What sets the band's height is the stats strip, not the chart.** This is the fact to keep:
once the legend is gone, a stat block (39px: an 18px figure over an 11px caption) plus the
band's 8px padding is a hard floor of 56px, and the chart column is budgeted at exactly one
stat block — title 10 + `mb-1.5` 6 + a 23px row. Inside that budget the chart is free; over
it, every pixel is one the band grows. Measured in Chromium at 1440px: **99px → 56px**,
against the issue's ≤60% target, and pinned by `e2e/workspace-compact-header.spec.js`
because vitest runs `environment: 'node'` and cannot measure anything.
The real height was the **legend**, not the bars — `legend="side"` lays out `flex-col`, so
it grew ~13px per bucket (~29px at one, ~133px at nine) and a busy agent's band ran to
~153px. `StackedBarChart` gains `legend="none"` and `:axis="false"`; the `legend !== 'side'`
guard became `legend === 'below'`, or the new value would have rendered the very legend it
removes. Within the 39px the chart keeps its **title** OR its x-axis labels, not both; the
title stays (board A3: "without it the bars read as another statistic") and the axis goes,
since the tooltip carries each bar's full date.

**Info is a rail tab, with a door that is the whole design.** `RAIL_DOORS.SOLO_AGENT` —
exactly ONE participant, never "at least one". Two properties are load-bearing:
- **Not `PLATFORM`.** The header button it replaces carried no gate, so it rendered for
  external portal clients; a platform door would have silently removed a panel they have
  today — the #2128 class, on the surface this file already warns about.
- **Not `AGENT`** (`> 0`), because in a room that renders one arbitrary participant's panel
  under a strip promising the whole conversation. Info **does not group by agent in a room**,
  which is a recorded deviation from ent#547's AC: `stores/clientPortal.js` holds report
  state as a singleton keyed to one agent (`loadAgentReports` → `resetAgentReports` bumps
  `_reportsGeneration` and invalidates siblings' in-flight requests), so N mounted panels
  leave N−1 in a **permanent loading skeleton**. Grouping is unblocked by keying that store
  per agent — a store change, not a rail change.

Info is the registry's first **static** tab: `signal: RAIL_SIGNAL_NONE` and `empty: null`,
declared rather than filled, because an agent always has a name, a health state and a chat
list (no empty state to teach) and nothing writes an `info` signal (no dot that can light).
`signalFor` already answers `emptySignal()` for an unmentioned tab, so the static form needs
no read-side special case. It is also absent from `feedsFor` by design — its body owns its
own two reads, the one docked tab not fed by the shell. Both `<PortalRail>` mounts (the
column and the mobile sheet) receive `#tab-info`; one alone leaves the phone on the generic
empty state. Mobile is a **gain**: the old panel was `hidden sm:flex`, so the header button
did nothing visible there at all.

**The composer owns attach and voice.** One paperclip (the header's opened the Files tab
with the *attach* glyph); the controls are voice-call · attach · dictate · model · send,
every button a 44px box (#2259) and the model picker the same height (#2662).

**The composer is ONE shell, stacked (#2662).** The field is the first row and takes the
shell's full width; the controls are a second row inside the same box, with the model
picker right-aligned beside Send. The border, fill and focus ring live on the **shell**,
not the textarea, which goes transparent and borderless — that is what makes the controls
read as inside the field. The ring is scoped `has-[textarea:focus]`, deliberately **not**
`focus-within`: the latter lit the whole shell when an icon button was merely tabbed onto,
and drew a second ring concentric with the picker's own. A textarea that regains
`rounded-2xl` or a background nests a second box inside the first.

Moving the chrome off the textarea makes the **visible** box larger than the field, so a
click on the shell's padding band or on the control row's ground lands on `<body>` — before
the shell existed the rounded box *was* the textarea. `focusComposerFromShell` puts the
caret back, and is guarded twice: it returns for a live call, and for any event that already
reached a control (`SHELL_INTERACTIVE`). The second guard is the model picker's — the
typeahead commits on `mousedown` with the default prevented, so the click that follows would
otherwise drag focus straight back out of the row the user just chose.

The shape is load-bearing, not cosmetic. While the buttons shared one row with a growing
field, every 44px box came out of the field's width — 143px of a 351px form at 375px, a
placeholder wrapped over four lines — and it is what left 34px when ent#403 tried to add
the model picker there, hence that control's original own-row placement above the composer
and hence #2662. Stacked, the field is ~333px at 375px and the **picker** is the element
that yields, truncating itself while Send stays 44px.

**The call toggle sits OUTSIDE the composer's inert region** — the form goes
`pointer-events-none` for the call's duration, so the button that ENDS the call would have
rendered pressed and refused the click. Stacking made this **two** inert regions, not one:
the field's wrapper and the control row's wrapper each carry the pair, because "everything
except the toggle" is now two boxes on two rows and one without the other leaves half the
composer live during a call. Each **generates a real box** — the field's wrapper is a block
(`composerWrap`, deliberately not a flex container: the `block w-full` textarea inside it is
what fixes #2259's line box, and making the wrapper flex would turn that textarea into a flex
item and undo it), the control row's is a flex row. What neither may become is
`display: contents`, which generates no box and would have silently dropped the dimming while
`pointer-events` (which inherits) still applied.

The shell is the **parent** of both regions and cannot become a third: the live toggle is
inside it, and `opacity` on a parent is not something a child can undo. So it sheds its
border and fill for the call's duration instead of dimming — the composer recedes to the
page ground and the one live control stays at full contrast. Both arms are **bound**, with
no chrome colour left in the static class: two Tailwind utilities of the same shape do not
agree on which of an equal-specificity pair wins (the sheet emits `.border-transparent`
after `.border-gray-300`, but `.bg-transparent` *before* `.bg-white`), and every `dark:`
variant beats both — so the static-plus-resting spelling renders a borderless **light**
composer that is invisible to anyone checking in dark.

The **same trap caught the ghost select twice more**, one level down: `border-transparent` in
`FIELD_GHOST_CLASS`'s base string beat the error arm's `border-status-danger-500` (emitted
later, equal specificity), and the unvariated `disabled:hover:bg-transparent` only TIES
`dark:hover:bg-gray-750` (`:hover:is(.dark *)`, emitted later), so a **disabled** picker still
lit up under the cursor during a call — in dark only. Both are fixed in `fieldClasses.js` with
the colour on the arms and the reset in both theme arms. The rule for any future field recipe
is in `design-system.md` → BaseSelect → Ordering rule.

**Both composers carry the shape.** `PortalConversation.vue` and `PortalRoom.vue` are the
same markup in two files (the #2211 lesson), so a shell landing in one leaves the room
visibly diverged from the chat beside it; `portalComposerAlignment.spec.js` runs every
structural assertion over both. The room's control row holds Send alone — the model picker
is per-agent and a room has several.

**The band's remount is fixed at its source, not by moving it.** `PortalConversation` is
keyed on `convKey`, whose `convGen` half bumps on every thread switch, and the band renders
through that component's `#band` slot — so slot content inside a keyed subtree was torn down
per switch, refetching numbers that had not moved. The band stays in the slot, because the
operator ruled a band **under the header** and the header lives inside the conversation;
hoisting it to a sibling renders an agent's numbers above its name (built, looked at,
reverted). Instead `usePortalAgentPage` seeds from its cache **during setup** rather than in
`onMounted` (which runs after the first paint, which is exactly why a warm remount still
flashed its skeleton and scanline) and skips the refetch inside `PAGE_FRESH_MS`. The
decision is the exported pure `shouldRefetchPage`, which fails **stale** on an unusable
timestamp — a wasted request beats a band that never updates again. Verified live: 6
same-agent tab switches → 0 `/page` requests and 0 placeholder frames, with a control
proving the probe could see a request at all. Making it literal means lifting the header out
of `PortalConversation`; that is the follow-up, and the conversation root's `h-full` must
become `flex-1 min-h-0` in the same change or the composer is pushed out of an
`overflow-hidden` shell.

**A reply is rateable as it lands.** `awaitPersistedReply` polls history — that is how it
knows the turn ended — and was returning only the row's content and cost, discarding the
`id` and `my_rating` that were already in hand; both live-turn paths then pushed an id-less
message and the rating control correctly hid itself until a reload. One shared mapper
(`portalUtils.js::assistantRow` / `replyFromHistory`) now feeds all three construction
sites. The synchronous fallback genuinely had no id — `portal_chat` minted one inline and
threw it away — so it returns `message_id`, **declared on `PortalChatResponse`** because the
response model strips undeclared keys in silence. The `v-if="item.message.id"` gate stays:
carry the identifier with the flag and let the consumer still refuse an empty one.

**Flow**: [workspace-agents-at-the-centre.md](../feature-flows/workspace-agents-at-the-centre.md) ·
**Requirements**: `requirements/core-agent.md` §5.23, §5.30

## The Tandem layer — a brief lands in Main, a room says who is reading, a complaint reaches the operator (ent#498, ent#363, ent#499)

Three small features that only make sense once Main exists (ent#523).

**A schedule can name one Workspace user** (`agent_schedules.deliver_to_workspace_email`,
nullable, both tracks, Alembic `0056`). Almost all of the delivery already existed:
`report_completion` is trigger-agnostic and `schedule` is deliberately NOT in
`INLINE_CHANNEL_TRIGGERS`, so the only missing fact was that a scheduled execution row never
carried `source_channel='portal'`. This is therefore **a stamp and nothing else** — no new
delivery path, no second applier, no terminal writer changed. The scheduler carries the
ADDRESS only: it is a separate process that cannot import the portal package, and it always
sends `execution_id`, so `execute_task`'s channel-persisting branch can never run for a cron
fire and channel columns passed as kwargs would be silently inert (#2426).
`execute_task_internal` resolves Main via `ensure_main_session` and stamps the pre-created row
BEFORE dispatch, through `db.stamp_execution_channel_context` — the first UPDATE of those
columns (every other writer sets them at INSERT), guarded on `source_channel IS NULL` so it
only ever ADDS a destination and can never repoint an inbound channel turn whose adapter is
waiting on that reply. Access is checked with `agent_on_roster(..., include_owned=True)`, the
Workspace's own roster, NOT `email_has_agent_access`: that admits any admin, and an admin who
neither owns the agent nor is shared it cannot open the thread, so a brief delivered there
would be invisible. Every refusal — blocked client, unreachable address, unreadable roster
(fail closed), unavailable session, already-stamped row — fails the pre-created row with a
named reason and releases the idempotency claim, because running the turn anyway spends the
tokens and puts the answer where nobody can read it. **C9** is honoured by a bounded wait on
the ent#286 in-flight marker inside the portal delivery leg, which then writes regardless: the
report is never dropped, only deferred, and the narrowing applies to every portal report
rather than only scheduled ones. See
[schedule-workspace-delivery.md](../feature-flows/schedule-workspace-delivery.md).

**A room tells its agents when a CLIENT is reading.** Full transcript visibility is the
deliberate choice for Workspace rooms, and it is only safe while the agents know they are
watched. `shared_sessions.service.room_is_user_facing` is the pure rule, derived from
MEMBERSHIP — nothing a participant writes reaches it. `FLEET_INTERNAL_PARTICIPANT_KINDS`
holds `agent`, `system` AND `user`: the platform `user` is the operator, and an ops room
is not client-facing. Getting that wrong is not cosmetic — `create_room` always seats its
creator and the only removal path is `kind="agent"`, so counting `user` makes every room
client-facing and the quiet branch unreachable. The set is otherwise the complement of "reader" because a kind added later (ent#171's external A2A
sender) is likelier to be a person than a machine, and an allow-list of human kinds would
silently classify it as fleet-internal. `platform_prompt_service.build_user_facing_room_prompt`
takes no arguments and names no participant: it is composed into a prompt handed to EVERY
woken agent, so an address would be disclosed sideways to agents that person never addressed.
Derived per wake rather than threaded from `post_message` (which does hold the list), because
`_wake_agent` calls `post_message` back and a threaded value could go stale when a reply
recruits a human. An unreadable roster assumes a person IS reading — the inverse of the usual
capability default, because the mistakes are not symmetrical.

**A negative rating reaches the operator.** ent#366's redaction stands — the rated agent reads
the score and never the words — so the operator's copy goes straight to `operator_queue`,
routed through `create_bounded_alert` (the volume is driven by a client clicking, which is the
agent-influenceable side of the #1677 classification) with its own registered type and a
reserved id prefix, one item per person per target. It fires on EVERY thumbs-down, not only
commented ones: "this was not useful" is the report and the words are the elaboration.
Prerequisite, and a live bug: `operator_queue.type` is free TEXT and both queue cards
hardcoded an `approval → question → alert` chain that rendered no control for anything else,
so `skill_not_found` items have never been closeable and five of them would jam a budgeted
type's pending cap forever. Both cards now consume `utils/operatorQueue.js::queueResponseKind`
and its unknown-type default moves from `question` to `acknowledge`.
