# Channel Completion Report-Back (ent#224 Slack · ent#265 Telegram)

## Overview

A long-running or **delegated** task whose work started from a channel reports
its terminal — success AND failure — back to the originating chat/thread. The
trigger side always worked; the completion side died silently: a user asks
agent A in Slack/Telegram, A delegates to B (or kicks off background /task
work), B finishes, and nobody tells the user.

Everything is a JOIN of shipped parts: the `source_channel*` destination
columns (ent#117), the #1578 CAS-won terminal chokepoints, `effect_guard`
(#1084) for at-most-once, the ent#223 Slack consent flag, and each channel's
existing send primitive. One service owns it:
`src/backend/services/channel_completion_report.py`.

This doc also pays the ent#224 documentation debt (#1763 shipped the Slack leg
with no flow doc) and records the ent#265 design decisions (D0–D10).

## User Story

> In Telegram I ask my orchestrator to research something. It fans the work to
> a worker agent and answers "on it". Twenty minutes later the worker finishes
> — and a ✅ completion note appears in my chat, threaded to my original
> message, sent by the same bot I talked to. If the worker fails, I get an
> honest ⚠️ instead of silence.

## The no-double-post rule (load-bearing — never weaken)

A direct channel turn is synchronous: the adapter already replies inline, and
the execution is recorded with `triggered_by` == the channel name. Reporting on
those would duplicate every normal channel reply. So the reporter fires ONLY
when the execution *inherited* its channel context, i.e. `triggered_by` ∉
`INLINE_CHANNEL_TRIGGERS` (`slack`/`telegram`/`whatsapp`/`public`). That single
condition keeps a customer workspace/chat from being spammed.

`public` joined the set in ent#457 and is the one entry that does not name a
channel adapter. A Workspace turn is synchronous in the same way — `portal_chat`
persists the assistant reply itself — so without it every chat message would be
followed by a duplicate "Finished". Public links and x402 share `triggered_by:
"public"` and lose nothing: they stamp no `source_channel_chat_id`
(`routers/public.py` writes `triggered_by` and `source_user_email` only), so
they never reach this gate at all.

## Entry Points

| Entry | Where | Notes |
|-------|-------|-------|
| `apply_result` success branch | `services/task_execution_service.py` (~1965) | CAS-won only |
| `apply_result` failure branch | `services/task_execution_service.py` (~2072) | **ent#265 D3** — previously emitted the #1578 event but never the report; the path agent-reported failure envelopes take (HTTP-error terminals, async #1083 callbacks). CANCELLED envelopes (#679) report too |
| `_write_terminal_and_gate` | `services/task_execution_service.py` (~890) | timeout / budget / crash / inline circuit-open class |

All three call `spawn_completion_report(...)` — fire-and-forget with a
strong-ref task set (the #1083 GC footgun), never-raise, CAS-won-gated by the
caller.

## The D0 fix — inherited context is persisted at row creation

**The bug that shipped ent#224 dead on its flagship path:** `run_async_task`
threaded `_inherited_channel_context(request)` into
`execute_task(source_channel=…)`, but `execute_task` writes channel columns
ONLY in its `if not execution_id:` creation branch — and the /task path always
pre-creates the row. The args were dead; every delegated row carried NULL
channel context at terminal time. The 224 test suite fabricated rows with
`SimpleNamespace` and never drove the write path, so it stayed green
(`docs/memory/learnings.md` 2026-07-31).

**Fix (`services/chat_execution_service.py`):**
`create_task_execution_and_activities` — the single row-creation point BOTH the
async and sync /task branches route through (the `_dispatch_async`/
`_dispatch_sync` fork happens after creation) — resolves
`_inherited_channel_context` and passes all four values into
`db.create_task_execution`: `source_channel`, `source_channel_chat_id`,
`source_channel_thread`, `source_channel_agent`. The dead threading in
`run_async_task` was removed (`execute_task` keeps its channel params —
`message_router` uses them for direct turns; that creation branch passes
`source_channel_agent=None`, so direct rows stay NULL by design).

**Provenance guard (security):** `db.get_execution(parent_id)` is a global
lookup, and inheriting hands the child's terminal an outbound destination
(someone else's chat) plus the bot token that reaches it — so the caller must
own the parent context. Arms, selected by the **authenticated principal**:

| Principal | Requirement |
|-----------|-------------|
| agent-scoped key (`current_user.agent_name`) | must BE the parent's executing agent |
| human (JWT / user-scoped key) | must be the parent agent's OWNER (`can_user_share_agent`) or admin |
| connector key (`connector_agent`) | never inherits (consumption-only, ent#46) |

Failure → no inheritance, info log — fail-open to no-context, never to someone
else's chat.

Two properties are load-bearing and must survive refactors:

1. **Arm selection is on the principal, never the raw `X-Source-Agent`
   header.** The SELF-EXEC-001 spoof guard in `derive_source_and_trigger` only
   fires when `current_user.agent_name` is set, so for a human caller that
   header is unvalidated client input (`routers/chat.py` documents the same trap
   for the resume-session IDOR). A header-selected agent arm is satisfiable by
   naming the parent's own agent — which the row itself tells you — collapsing
   the human arm to a no-op. The header is logged, never trusted.
2. **The human arm is owner-or-admin, not any accessor.** Posting into a channel
   chat is a proactive-send capability; every other proactive surface is
   owner-gated (`OwnedAgentByName` for group sends) or per-recipient-consented
   (#321). A share recipient can already read the owner's execution ids
   (`GET /api/executions` is accessor-scoped), so an accessor arm would let them
   push a report into the owner's Telegram DM or group.

## Identity — the binding agent (D1, Option A)

`schedule_executions.source_channel_agent` = "the agent whose channel binding
owns this context": written only at the D0 creation point as
`parent.source_channel_agent or parent.agent_name`, and only when the parent
actually has `source_channel` (never a dangling pointer on a channel-less
child). Transitive across A→B→C — the leaf row carries the ROOT binding agent.
NULL (direct/legacy rows) ⇒ the reporter falls back to `row.agent_name` —
byte-identical pre-#265 behavior.

The reporter resolves `binding_agent = row.source_channel_agent or
row.agent_name` and evaluates **consent + bot token against `binding_agent`
for BOTH channels** — the bot the user actually addressed delivers. On
Telegram this is a platform constraint, not a preference: a bot cannot DM a
user who never messaged it, so only the originating agent's bot CAN deliver
the flagship delegated-DM report.

**Slack behavior changes (D1b, both directions intentional):** *widening* —
delegated reports now deliver via binding agent A's consent even when worker B
is unbound to the channel (previously suppressed); *narrowing* — a channel that
consented to worker B but not to originating A now suppresses A→B delegation
reports (the user addressed A). Slack's displayed identity stays
`username=row.agent_name` (the EXECUTING agent — attribution unchanged).

**Attribution on Telegram (D1c):** no per-message sender name exists, so when
`binding_agent != agent_name` the head line names the worker:
`✅ Task finished — {agent_name}`.

**Rejected alternatives (recorded for the future provenance model):**
- *(B) reverse chat→binding lookup at report time* — ambiguous with multiple
  bots per chat, needs a new indexed reverse lookup + tiebreak, breaks at ≥2
  hops and on tenant hygiene (global scan across other owners' bindings).
- *(C) strict Slack-mirror (consent on the executing agent)* — suppresses the
  flagship delegated-DM case; fails AC#3.
- *(D) persist `parent_execution_id` + walk to the root at report time* —
  couples delivery to parent-row retention (90-day sweep + #1449 scrub) and
  chain length; the denormalized copy-down matches the `source_channel*`
  pattern it rides beside.
- *reuse `source_agent_name`* — records the DIRECT caller; breaks at ≥2 hops
  and conflates "who called me" with "whose binding delivers".
- *immutable id instead of name* — the codebase is name-keyed with a 17-table
  rename cascade as the house pattern; `AgentRef("schedule_executions",
  "source_channel_agent", KEEP)` covers rename (D1a), purge requires the
  180-day soft-delete window while executions live ≤~2h.

## Delivery — resolver dispatch map (D10)

`_CHANNEL_RESOLVERS = {"slack": _resolve_slack, "telegram": _resolve_telegram,
"portal": _resolve_portal}`; `SUPPORTED_CHANNELS` derives from the keys, so a
WhatsApp leg is one added entry. A resolver does consent + destination + token resolution and returns an
async deliver closure (or `None` = suppress, already logged). The shared block
then runs `effect_guard` and the send inside it.

### Slack (`_resolve_slack`)

Consent: ent#223 channel-binding `allow_proactive` looked up for
`binding_agent`; no binding / no consent → suppress (info log). Token:
`db.get_slack_workspace_bot_token(team_id)`. Send:
`slack_service.send_message_detailed(..., username=executing_agent,
thread_ts=thread)`.

### Telegram (`_resolve_telegram`)

Destination discrimination is deterministic (group configs exist only for
group chats, negative ids; DM chat links key on positive user ids — the
ordered check is belt+braces):

1. `db.get_telegram_group_config(binding.id, chat_id)` exists → **group**:
   require `is_active` AND `allow_proactive`
   (`telegram_group_configs.allow_proactive INTEGER DEFAULT 1` — allow for
   existing AND new groups; the ent#223 "don't break existing sends" split
   rationale does not transfer here, and a new-groups-deny default would make
   the flagship @mention-delegation silently dead in every new group). Opt-out
   mute via the **"Completion reports"** checkbox in `TelegramChannelPanel.vue`
   (label names only what the flag governs today — F2).
2. else `db.get_telegram_chat_link(binding.id, chat_id)` exists → **DM**:
   consent-by-construction — the user personally cold-started this bot;
   Telegram's own cold-DM prohibition makes false positives impossible at the
   transport, and a block surfaces as a 403 the send handles gracefully.
   Deliberately NOT gated on `agent_sharing.allow_proactive` (#321): that flag
   cannot distinguish "explicitly revoked" from "never opted in" (default 0) —
   honoring it would kill the flagship case for every verified shared user.
   The two DM consent regimes (user-initiated work here vs agent-initiated
   outreach #321) are documented in requirements §15.1h.
3. else → suppress + info log (unknown destination; mirrors Slack's
   unbound-channel suppression — never fire a send destined to fail).

Consent writes are human-only: the `allow_proactive` arm of
`PUT /api/agents/{name}/telegram/groups/{id}` calls `reject_agent_principal`
(an agent-scoped key resolves to the owner and could self-grant consent —
ent#223's own post-ship pitfall); `trigger_mode`/welcome updates stay
agent-callable.

**Rendering (D5):** `html.escape(summary, quote=False)` BEFORE
`TelegramAdapter().format_response` (`_markdown_to_html` escapes nothing;
unescaped `<class 'ValueError'>` trips "can't parse entities" and the
strip-HTML fallback then DELETES the substring), then re-cap the CONVERTED
string at 4096 (entity expansion can push 2800 source chars past the hard cap,
and "message too long" is a 400 the parse-fallback does not catch → silent
loss). The adapter's parse-failure fallback stays the last-resort net.

**Threading (D6):** `source_channel_thread` = the triggering `message_id`;
passed as `reply_to_message_id` when present and numeric (`thread.isdigit()` —
the `int()` cast in `_send_message` sits outside its try). DMs anchor too;
`allow_sending_without_reply: True` makes a deleted original safe.

### Workspace (`_resolve_portal`, ent#457)

The surface that had the contract stamped on it and no leg to walk on: #2157
marks portal executions with `source_channel = "portal"` but that stamp names a
SURFACE, not a delivery destination, so every portal row died at
`report_completion`'s `if not source_channel_chat_id` gate. `chat_id` is the
portal session id, stamped at both turn-creation sites.

**Consent is by construction, not by flag.** Slack consults ent#223's
`allow_proactive` on the channel binding and Telegram consults ent#265's on the
group config, because both can deliver into a room holding people who never
asked. A portal session belongs to exactly one client, so there is no third
party to protect and no flag to look up — delivering into a person's own
conversation with the agent they are talking to is what the conversation is
for. That is *why* the recipient is read from the SESSION ROW rather than from
the execution's stamp: a stamp is a string that rode an inheritance chain, and
the session row is the platform's own record of whose chat this is.

Suppressed (info-logged, never raised) when the session no longer exists or
carries no client email.

**Delivery is a persisted assistant message**, filed under `session.agent_name`
— so a delegated child executing as a different agent files under the agent the
client is actually talking to, and `_portal_body` names the executing agent in
the text rather than silently attributing its work. Failure is stated
("Didn't finish — <status>"), never a silent vanish, and the detail is capped at
`_MAX_REPORT_CHARS`.

No new transport: the Workspace polls its threads, so "degrades to poll" holds
by construction. The deliver closure also calls `touch_portal_session(...,
added=1)` — every other writer of a portal message does, and `last_message_at`
is what orders the sidebar; a report filed into a thread that never moved is a
notification pointing at the middle of a list, which is the same silence this
contract exists to end.

## At-most-once (D9 — the dedup-disarm pin)

`effect_guard("channel_completion_report", {channel, chat_id, thread},
execution_id=eid, agent_name=row.agent_name)` — identity is the resolved
destination only, never the generated body (#1084).

**CRITICAL:** the guard's `agent_name` is ALWAYS the EXECUTING agent
(`row.agent_name`), never `binding_agent` —
`resolve_and_validate_execution` fail-opens on an agent/row mismatch (returns
None → dedup silently DISABLED) for exactly the delegated rows this feature
exists for. Pinned by `test_telegram_replay_does_not_repost`, which runs the
REAL guard with `source_channel_agent != agent_name`.

Failed send (D4): `_send_message` → None ⇒ warn + `return False` INSIDE the
guard — the claim completes with an empty snapshot. At-most-once bias; never
blind-retry an ambiguous send (duplicate-message class).

Sanitize-before-truncate: `_summarize` credential-sanitises over a 2× window
before the 2800-char cap (the #1578 emit-chokepoint rule — every egress
surface).

## Chokepoint coverage & known v1 boundaries

| Terminal path | Reports? | Notes |
|---------------|----------|-------|
| `apply_result` success (inline sync + #1083 async callback) | ✅ | both converge here |
| `apply_result` failure/cancel | ✅ | ent#265 D3 |
| `_write_terminal_and_gate` (timeout/budget/crash) | ✅ | shipped with ent#224 |
| Lease-reaper `LEASE_EXPIRED` | ❌ v1 | |
| Bulk watchdog sweeps | ❌ v1 | |
| Pull sink (`apply_task_result`) | ❌ v1 | dark until a pull pilot |
| Operator-terminate cancel (Path B) | ❌ v1 | writes CANCELLED before `apply_result` |

Other recorded limits: a restart mid-inline-turn loses the inline reply and
reports nothing (F7); a late token-gated FAILED→SUCCESS resurrection replays
the guard — the corrective ✅ is never sent (correct at-most-once, D4/L5);
fan-out = ONE report per child execution (per-execution identity; no persisted
parent key to dedup on — G3, pinned by test); pre-migration rows suppress on
Telegram (NULL context, G7); forum topics not threaded (inbound never captures
`message_thread_id`); no channel-history persistence of the report (the group
session key needs the original sender, which the reporter doesn't have — D7);
no reporter-side rate caps (one post per terminal, bounded by effect_guard —
D8; the Telegram 429 ≤30s sleep is safe because the reporter only ever runs in
a fire-and-forget spawn).

## Failure modes

Every suppression path returns False with a log (info = policy, warn =
unexpected); sends fail-soft; `report_completion` never raises (outer
try/except → warn) — a reporting failure must not disturb an execution that
already completed and billed.

## Data model

- `schedule_executions.source_channel_agent TEXT` (nullable) — ent#265;
  `AgentRef(..., KEEP)` in `db/agent_cleanup.py` (rename cascades; the #772
  90-day terminal-row sweep is its retention discipline).
- `telegram_group_configs.allow_proactive INTEGER DEFAULT 1` — the DEFAULT
  fills existing rows on both backends; `get_or_create_group_config` inserts
  an explicit 1; `_row_to_group_config` reads NULL as allowed.
- Dual-track migration: SQLite `channel_report_back_columns` + Alembic
  `0031_channel_report_back` (single linear head off 0030).
- Facade: `db.create_task_execution(..., source_channel_agent=)`,
  `db.get_telegram_chat_link`, `db.update_telegram_group_config` (keyword
  passthrough — a positional append risks a silent field swap, eng M3).

## Testing

`tests/unit/test_265_telegram_completion_report.py` (41 tests, three layers):
wired-mock reporter decisions (delivery/threading/consent/suppression/
rendering/attribution/binding-agent incl. the D1b Slack-narrowing pin);
chokepoints (`apply_result` failure/success/CANCELLED spawn on CAS-win only);
real-DB (db_harness) — D0 row READ-BACK inheritance (both provenance-guard
arms, channel-less parent, two-hop transitive), REAL effect_guard replay with
`source_channel_agent != agent_name` (the dedup-disarm tripwire), fan-out
one-report-per-child, live column SELECTs through `db/tables.py`, group-config
default + facade kwargs round-trip, router PUT round-trip incl. the
agent-principal 403. Plus conscious edits in
`tests/unit/test_224_channel_completion_report.py` (unsupported-channel →
whatsapp; consent fixture keyed on binding agent) and the
`test_agent_cleanup_parity.py` locks (D1a). The read-back layer caught a real
facade passthrough gap pre-merge — see `docs/memory/learnings.md` 2026-07-31.

`tests/unit/test_ent457_portal_completion_report.py` (27) covers the Workspace
leg: the `"public"` inline-trigger addition and the proof that public links /
x402 are untouched by it; session-missing and client-less suppression; filing
under the session's agent with the executing agent named in the body when they
differ; the failure wording; the `touch_portal_session(..., added=1)` paired
write (one message, not a user+assistant pair); and the resolver's presence in
the dispatch map. `tests/unit/test_2157_portal_narration.py` gains the cases
where this supersedes that PR's stamp-only behaviour.

**Six of those drive `report_completion` itself** (review finding 8). The rest
reach `_portal_body` / `_resolve_portal` directly, or assert the review fixes by
`inspect.getsource` — which is precisely why two real defects, an unsanitized
body and a raise that released the effect claim, both survived a green suite:
nothing exercised the entry point, so nothing exercised the gate ordering, the
recipient guard and the sanitizer *together*. The end-to-end cases stub only the
two edges the module does not own (the execution row and the portal DB) and let
everything between run for real: a delegated terminal reaching the thread, a
credential-shaped failure string proven absent from what is persisted, a report
whose inherited client does not match the session's refusing before any write,
a NULL client failing closed, the inline turn still refused at the gate, and a
vanished session writing nothing. Each was verified to fail against a mutant
(sanitizer removed; recipient guard removed).

## Related Flows

- [task-completion-events.md](task-completion-events.md) — the #1578 sibling
  spawned at the same chokepoints
- [telegram-integration.md](telegram-integration.md) — inbound Telegram +
  group configs
- [slack-channel-routing.md](slack-channel-routing.md) — the ent#223 consent
  flag's home
- [effect-idempotency.md](effect-idempotency.md) — `effect_guard` (#1084)
- [task-execution-service.md](task-execution-service.md) — `apply_result` /
  `_write_terminal_and_gate`

## Revision History

- 2026-07 (ent#224, #1763): Slack leg shipped (undocumented — debt paid here)
- 2026-07-31 (ent#265): Telegram leg; D0 inheritance persistence + provenance
  guard; D1 binding-agent identity; D3 failure-applier hook; this doc
- 2026-08-24 (ent#457 AC#3): Workspace leg — `_resolve_portal`, consent by
  construction rather than by flag, delivery as a persisted assistant message
  with a paired `touch_portal_session`; `"public"` added to
  `INLINE_CHANNEL_TRIGGERS`
