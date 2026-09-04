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

**Voice conversation — one conversation, two modalities (ent#440).** The Workspace's
two manual voice controls — hold-to-dictate (#2212) and the speaker toggle (#2157)
— become one hands-free loop: the mic listens, the utterance is submitted as an
**ordinary Workspace turn**, the reply is spoken, the mic reopens. The load-bearing
property is that a spoken turn takes the SAME path a typed one does
(`submitUserText` → `deliver` → `POST .../chat/stream`), so shared context, history
parity and permission parity are true by construction rather than by
synchronisation — same portal session, same resumed Claude session, same
`enterprise_portal_messages` row, no new route and therefore no new gate. Bridging
the platform's Gemini Live session (VOICE-001, `routers/voice.py`) was **rejected**:
it answers with a different model holding a *summarised copy* of the thread and
writes its transcript back afterwards (a parallel conversation wearing the same
page), it cannot reach the agent's own tools/canvas, and its JWT-only WebSocket
cannot authenticate a portal client, which holds no `users` row. All decidable
rules are pure in `components/portal/voiceConversation.js` — transition table
(`start` idempotent; every event inert in `off`, so a `/stt` answer arriving after
teardown cannot restart a loop the user ended; `transcript` sends from exactly one
state; every exit releases the microphone), an RMS level meter for utterance
boundaries (1.2 s silence hold, 30 s cap, 15 s no-speech → stop **with a
sentence**; room tone re-listens rather than spending a transcription), barge-in at
a **higher** threshold than listening plus `echoCancellation` on capture (without
both, the mic hears the agent's narration through the speakers and it interrupts
itself), and `spokenReply()`, which cleans a reply for the ear (code fences → one
spoken sentence, links read as text, sentence-boundary cap) while the rendered
message is untouched. The control renders only where the loop can run; an agent
with no configured voice still converses, in text, and says so. **OSS-core by
decision (ent#440): deliberately ungated** — recorded explicitly because the
default for an enterprise-tracker feature is gated-unless-ruled-otherwise (the
ent#326/ent#384/ent#392 discipline). **No backend change, no new endpoint, no
migration.** See [workspace-voice-conversation.md](../feature-flows/workspace-voice-conversation.md).

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
consumers that want the briefing make the second call. On the client, three
`ScanlineReveal` zones (stage, conversation body, briefing) each key on their own "no
data yet", and `ScanlineReveal` gained an additive `content-class` prop because
`.scan-content` is the primitive's own element and a full-height flex stage had no hook.
See [workspace-roster-briefing.md](../feature-flows/workspace-roster-briefing.md).


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

