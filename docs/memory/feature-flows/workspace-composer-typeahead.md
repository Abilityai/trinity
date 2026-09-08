# Feature: the Workspace composer typeahead (`/` playbooks, `@` agents)

> **Status**: ✅ Implemented (2026-08-13)
> **Issue**: abilityai/trinity-enterprise#392
> **Requirement**: `docs/memory/requirements/core-agent.md` §5.13
> **Related**: `docs/memory/requirements/core-agent.md` §5.12 (ent#361 — the @mention grammar this round-trips through; it has no flow doc of its own), [workspace-agent-page.md](workspace-agent-page.md) (the other ent#380 briefing consumer), [workspace-absorbs-session.md](workspace-absorbs-session.md) (why this is the only composer left)

## Overview

The composer had two invocation syntaxes and no way to find either. A client can
`@`-mention an agent to escalate a 1:1 into a group room (ent#361) and can ask an
agent to run one of its playbooks — but both required knowing the exact name, and
a near-miss parses as ordinary text with no feedback at all. The playbook hints
(ent#138 / ent#380) render as cards on the **empty-chat screen only**, so the
capability becomes invisible the moment a conversation has one turn in it.

Typing `/` or `@` at a token boundary now opens a bounded list. `/` splices a
playbook's `starter_prompt` into the composer without sending; `@` inserts a
token `mentionedAgents()` resolves.

**OSS-core by decision (ent#392): deliberately ungated** — no
`requires_entitlement`, logic stays in the OSS tree. Recorded explicitly because
CLAUDE.md's default for an enterprise-tracker feature is *gated unless ruled
otherwise*, so the ruling must never be inferred later from the mere fact that it
merged (the ent#326 / ent#384 discipline). It extends a surface that is already
OSS-core (ent#356) over data the client already holds.

## Why all the logic is pure, and the components hold none

`vitest.config.js` is `environment: 'node'` and `@vue/test-utils` is not a
dependency, so there is no component-mount harness. That is not merely a testing
constraint — it decides the architecture: **a decision left inside a component is
a decision no test can reach.** So every decidable thing is a pure export in
`portalUtils.js` (adjacent to the grammar it round-trips through), the two
composers are dispatchers, and `PortalTypeahead.vue` is presentational.

The parts no unit test can reach — which handler the textarea binds, where the
popup is anchored, whether the reset is called on every programmatic write — are
covered by source-structure guards, the house idiom, with comments stripped first
so prose about a rule is not scanned as code.

## The trigger rule is stricter than the parser, on purpose

`MENTION_RE` is unanchored: `foo@bar` matches `@bar` and `user@example.com`
matches `@example`. Its safety comes entirely from roster resolution, which is
right for *parsing* and wrong for *offering* — a popup that opened on an email
address would fight the user for the rest of the sentence (AC#6).

So the typeahead fires only on a trigger char at a **token start**, where "start"
means *preceded by a non-word character* — deliberately not *preceded by
whitespace*. A whitespace-only rule cannot fire after CJK (`你好@rec`), after an
emoji, or after punctuation (`(@bob`, `"@bob`), while the non-word rule still
closes every case the AC names: `50/50` (previous char `0`), `and/or` (`d`),
`user@example.com` (`r`).

The asymmetry is safe in the only direction that matters: **the popup can never
open on something the parser would not see**, so it can never offer a token that
silently degrades to plain text.

Two further rules exist because the obvious version of the scan is wrong:

* The backward scan is **floored** at 64 characters, not merely capped in its
  result. An unfloored scan walks the whole composer on every keystroke, and a
  pasted 100 KB blob then costs O(n) per key.
* The scan carries a **forward** pass to the token end. Without it the splice
  ends at the caret, so accepting *alice* with the caret parked at `@bo|b`
  leaves `@alice b` — a correct mention turned into two wrong words.

## Un-mentionable slugs, and why the predicate is derived

`sanitize_agent_name` keeps `.` and imposes no length cap; the mention grammar
allows neither. So `data.scout` is a perfectly ordinary agent whose mention reads
as `@data` and resolves to nothing.

Today nothing offers those names, which is why the failure is invisible. A
typeahead that listed them would **manufacture** the exact silent
degrade-to-plain-text the AC forbids, and make it look like a product bug.

`isMentionable` therefore asks the real parser (`mentionedAgents(@name, [{name}])`
resolves to exactly that name) rather than carrying a second copy of the grammar
twelve lines from the first — in the file whose own header says drift in this
grammar is its own bug class. It also rejects names containing `@` or whitespace,
which a hand-written copy would have handled only by luck.

Widening the grammar or tightening slug creation is the real fix and is
cross-repo (it touches the private rooms engine); excluding is the correct local
one.

## No implicit selection — the asymmetry that decides it

The roving index starts at "nothing chosen". A plain Enter accepts **only** with
an explicit selection; otherwise it sends. Tab always accepts the top row.

Accepting whenever the popup happens to be open destroys typed work three ways:
the source-empty panel swallows the send entirely; a stale index after a roster
refresh inserts the wrong row; and the popup may merely *happen* to be open —
after a paste, or in prose like `Can you check /status of the deploy` — at which
point Enter splices up to 500 characters of an unrelated starter into the middle
of the message.

The harm is asymmetric. An accidental accept destroys work; an accidental send is
the thing the user was reaching for anyway. So Enter is safe by default and
accepting costs one extra keystroke (`↓` then Enter, or just Tab, which nobody
presses to send).

The keymap also fixes something the current binding gets wrong: `@keydown.enter.exact`
has no IME guard, so an IME user's candidate-commit Enter sends the message
mid-word. The pure keymap passes a composing keystroke through.

## The splice decides the separator; the token never carries one

Baking a trailing space into `@name` produces `Hello @alice  there` and turns
`@| x` into `@recon  x`. Omitting it entirely produces `@reconx`, which resolves
to nothing — the AC#4 failure by a different route. So the token is the token,
and the splice appends a space only when the character after the replaced token
is not already whitespace.

That is also why the round-trip property is asserted over the **spliced value**
rather than the bare token: `mentionedAgents(buildMentionToken(n), roster)` is
near-tautological and passes while the double-space bug ships.

## Recomputation, dismissal, and the writes that fire no event

`@input` alone is not enough, and three of these were regressions waiting to
happen:

| Event | Behaviour | Why |
|---|---|---|
| `@input` | recompute from `e.target` | Reading the v-model ref makes correctness depend on Vue's listener ordering — true today, an implementation detail |
| `@input` with `inputType` `insertFromPaste`/`insertFromDrop` | close, do not open | A pasted message ending in `@name` must not open a picker whose next keystroke is Enter |
| `@click` / `@select` | recompute | The caret moves with no input event; accepting against stale bounds splices over the wrong text |
| caret keys while open | close, key passes | Same reason, from the keyboard |
| non-collapsed selection | no trigger | The user is selecting, not composing a token |
| click outside the wrapper | close **without** arming the Esc sentinel | Otherwise "type `@`, click away to read something, click back, keep typing" stays suppressed until the `@` is deleted |
| an accepted pick | close **and settle** the token it inserted | The splice appends its separator only when the next char is not already whitespace, so a mid-sentence pick leaves the caret *inside* the new token — and `setSelectionRange()` fires a `select`, so the popup would reopen over its own successful choice. Editing the token back re-arms it |

`resetTypeahead()` runs on every **programmatic** write to `input.value` —
`send()`, the prefill watcher, the thread switch, and both dictation handlers.
None of them fires an input event, so without it a sentinel armed while composing
message 1 kills the popup for every later message that starts the same way: a
feature dead for the rest of the session.

Esc suppression is keyed on `{kind, start, query-prefix}`. Strict query equality
un-dismisses on the very next keystroke (Esc has to mean *stop offering this*);
keying on `start` alone is wrong in both directions — text inserted before the
token shifts `start` and re-opens, while a full retype at the same offset stays
suppressed forever.

## The room composer: `@` ships, `/` does not

`@`-in-room is live, first-class behaviour and undiscoverable in exactly the way
this issue exists to fix — and a room needs it *more* than a 1:1 does, because a
1:1 has one obvious counterpart and a room has many.

But `mentionedAgents` is **never called on the room path** — room mention-waking
resolves server-side inside `POST /api/rooms/{id}/messages` — so the round-trip
proof above does not transfer. It also cannot be replaced by reading the engine:
the rooms module is a private submodule that OSS clones do not check out, so a
source reading is neither reproducible from this repo nor pinned to the commit
this branch builds against. The evidence is therefore what the **server
answered**, using the fields the endpoint already returns
(`{room_id, seq, mentions, woke}`):

| Probe | Observed |
|---|---|
| `@<participant>` | `{"mentions": ["acme-scout"], "woke": ["acme-scout"]}` |
| `@<non-participant>` | `{"mentions": [], "woke": []}` |

Two things follow, and only two. The grammar matches: a `buildMentionToken()`
token is recognised and wakes the agent, which is the property the `@` affordance
depends on. And the candidate list should be the room's **agent participants**,
because those are the names a pick is *known* to wake — offering the roster would
put names in front of the user with no evidence that choosing one does anything,
the same class of silent no-op as offering an un-mentionable slug.

What is deliberately **not** concluded is that a non-participant mention has no
effect at all. Requirements §5.12 records an engine-side newcomer-join path from
ent#361 (`_join_mentioned_newcomers`), and two empty response fields do not
disprove it — a join would surface as a participant change, which the probe did
not look at. If that path is live, this list is narrower than the engine allows.
That is an acceptable place to be narrow: recruiting a genuinely new agent stays
with the explicit "+ Add agent" control, which is honest about spending money on
another agent, and §5.12's own safety framing ("only a human may recruit") argues
for keeping recruitment an explicit act rather than a side effect of typing.

`/`-in-room is deferred: a room has N participants and no active agent, so
"whose playbooks?" has no answer without inventing a two-step picker or per-row
attribution that the issue does not specify. This is a design gap, not a missing
field — every roster card already carries its own `playbooks`.

## Flow

```
user types "/" or "@" at a token boundary
        │
        ▼
detectTypeaheadTrigger(el.value, selectionStart, selectionEnd)   ← pure
        │  null | {kind, start, end, query}
        ▼
kind === '/'  →  filterPlaybookCandidates(agent.playbooks, query)     ← pure
kind === '@'  →  filterAgentCandidates(source, query, {exclude, enabled})
                     · 1:1  source = props.roster,  exclude = [self]
                     · room source = roomMentionSource(participants, roster)
                     · enabled = store.multiAgentChatAvailable  (#2128)
        │  {items, sourceCount | peerCount, mentionableCount, enabled}
        ▼
boundCandidates(items, 8) → {visible, overflow}                       ← pure
        │
        ├─ visible.length > 0                → PortalTypeahead renders rows
        ├─ visible empty AND query === ''    → one honest line (never eats Enter)
        └─ visible empty AND query !== ''    → popup CLOSES (AC#6)
        │
        ▼
resolveComposerKey({key, modifiers, isComposing, open, hasActive, …})  ← pure
        │  'move-down' | 'move-up' | 'accept' | 'dismiss' | 'close' | 'send' | 'pass'
        ▼
accept → applyTypeaheadInsert(input, trigger, insert)                 ← pure
             insert = '/' ? starterFor(row) : buildMentionToken(row.name)
        │  {value, caret}
        ▼
input.value = value ; nextTick → el.focus() → setSelectionRange(caret)
                                  (focus BEFORE selection — Safari resets it)
```

No network call, no store action, no backend change anywhere on this path.

## Files

| Layer | File | Change |
|---|---|---|
| Logic | `src/frontend/src/components/portal/portalUtils.js` | +22 pure exports (16 functions, 6 constants) beside `MENTION_RE`; `starterFor` lifted here |
| UI | `src/frontend/src/components/portal/PortalTypeahead.vue` | **new** — presentational panel, two consumers |
| UI | `src/frontend/src/components/portal/PortalConversation.vue` | anchored wrapper, dispatcher handlers, placeholder |
| UI | `src/frontend/src/components/portal/PortalRoom.vue` | `@` over the room's wake-set; textarea ref + doc-click listener |
| UI | `src/frontend/src/components/portal/PortalBriefing.vue` | imports `starterFor` instead of defining it |
| Tests | `src/frontend/tests/unit/portalComposerTypeahead.spec.js` | **new** — 111 tests |

Backend, store and router are untouched: **no new endpoint, no new table, no
migration.**

## Rendering and containment

The panel opens **upward** (`absolute bottom-full`) because the composer sits at
the bottom of the pane, scrolls internally, and is bounded at
`max-h-[min(14rem,40vh)]` — `Portal.vue`'s root is `h-screen overflow-hidden`, so
on mobile landscape with the keyboard up a flat `max-h-56` clips against a
composer sitting at ~y=250. Eight rows render, with a counted
`N more — keep typing to filter` footer (principle 28: bounded viewport, stated
total).

`z-30` is the same tier as this component's own agent-picker dropdown and
strictly below the two `z-40` overlays (mobile nav, files panel) that must cover
it; the two `z-30` panels sit at opposite ends of the pane and cannot overlap.
The new wrapper carries **no** z-index, so it creates no stacking context — and
it must carry `flex-1 min-w-0`, because a bare `relative` div collapses the field
to content width.

Rows accept on `mousedown`, not `click`: a click blurs the textarea first and the
caret is lost before the splice reads it.

**ARIA, honestly**: `role="listbox"` on the panel, `role="option"` +
`aria-selected` on rows, and an `aria-live="polite"` count. Deliberately **no**
`aria-expanded` / `aria-activedescendant` on the textarea — those belong to
`role="combobox"`, which is itself out of spec on a multiline control, and
shipping a claim screen readers ignore is worse than shipping less.

Titles and descriptions are agent- and operator-authored text arriving in a new
place, so they render through `{{ }}` interpolation only. The portal's single
`v-html` remains assistant message bodies via `renderMarkdown()` (DOMPurify), and
a guard pins that count at 1 rather than asserting "no `v-html`", which is
unwritable for that file.

## How this differs from `usePlaybookAutocomplete`, and why it is not shared

`src/frontend/src/composables/usePlaybookAutocomplete.js` already implements a
`/`-typeahead for the **operator** chat surface. It was not reused, and the
decisive reason is that **it has no tests at all** — refactoring it to serve a
second consumer is unverifiable by construction. It also matches `p.name` (the
portal shape has no `name`), inserts `/name ` as a *command* (which this issue
puts out of scope), mutates five refs from `parse()`, and has no `@` mode.

The divergences are recorded here as decisions so a future unification inherits
reasons instead of re-deriving them: the boundary rule is **non-word** rather
than whitespace (CJK / emoji / punctuation); the scan carries a **forward** pass
so a mid-token caret does not leave a tail; the separator is decided by the
splice rather than baked into the inserted string; and Enter **never** accepts
implicitly. The dead `components/rooms/RoomComposer.vue` (since deleted in #2492) was read, not revived —
it has no boundary check at all and papers over the token tail with `.trimStart()`.

The one genuine *contract* — the mention grammar — is single-sourced by
**deriving** from `mentionedAgents`, which is the thing that must not drift.

## Tests

`src/frontend/tests/unit/portalComposerTypeahead.spec.js` — 111 tests, node env.

| # | Area | What it pins |
|---|---|---|
| 1 | `detectTypeaheadTrigger` | every worked case; forward scan; non-word boundary (CJK/emoji/punctuation); innermost trigger; the 64-char floor; out-of-range and non-numeric carets; non-collapsed selection |
| 2 | AC#6 exhaustively | `50/50`, `and/or`, `user@example.com`, `a@b.c` yield `null` at **every** caret index (a loop, not a sample) |
| 3 | `isMentionable` | the dotted / over-long / leading-punctuation matrix, plus agreement with `mentionedAgents` in both directions |
| 4 | AC#4 round-trip | over the **spliced value**, across content before/after/both/neither and a next char of space, letter, newline, EOS — plus the negative: every excluded name is one the parser cannot resolve |
| 5 | splice + separator | mid-token tail; the four separator cases through `buildMentionToken` (a baked-in space fails 8 assertions); returned caret; junk input |
| 6 | `filterAgentCandidates` | slug **and** label; substring for shared deployment prefixes; case-insensitivity; self excluded; un-mentionable excluded; `enabled:false → []` (the #2128 gate, tested not grepped) |
| 7 | `filterPlaybookCandidates` | word-start ranking beating source order; no substring free-for-all; source-empty vs filter-empty |
| 8 | `typeaheadEmptyMessage` | three distinct statements; room wording; silence without the capability; copy asserts nothing about operator configuration |
| 9 | `resolveComposerKey` | all 16 Enter modifier combinations (the `.exact` table), open × hasActive × hasCandidates, IME, Tab/Shift+Tab, Escape, caret keys |
| 10 | dismissal | Esc-then-keep-typing stays closed; retype re-arms; a cleared sentinel re-opens (the session-long-dead-feature guard) |
| 10b | post-pick settle | a mid-sentence pick leaves a re-detectable trigger at the new caret; `dismissAfterInsert` suppresses exactly it; nothing to settle when a separator was appended; editing the name back re-arms |
| 11 | active index | wrap both ends; no-op on empty; a stale index is **dropped**, not clamped |
| 12 | bounds + `starterFor` | at / under / over the limit; honest overflow; null-safe starter |
| 13 | `roomMentionSource` | participants not roster; label join; junk |
| 14 | source guards | the `.exact` binding is gone and delegates to the tested keymap; `autoGrow` still on the input path; ≥4 `resetTypeahead()` call sites; `e.target` not the ref; wrapper flex classes; placeholder advertises both triggers; room scopes to participants and has no `/`; briefing imports `starterFor`; popup geometry, `mousedown`, overflow footer, listbox semantics, no new `v-html` |

Seven mutations were run against the suite to prove the guards can fail, four at
implementation and three more at review: baking a space into `buildMentionToken`
(8 failures), accepting Enter without an explicit selection (1), dropping the
mentionable filter (6), dropping the forward scan (3), removing the token-boundary
rule (5 — the AC#6 loop), re-typing `isMentionable` as a hand-copied regex that
drifted to allow dots (10), and dropping the post-pick settle (1).

Commands:

```
cd src/frontend && npx vitest run          # 23 files / 444 tests
cd src/frontend && npm run check:tokens
cd src/frontend && npm run build
node src/frontend/scripts/scan-raw-colors.mjs src/frontend
```

## Known limitations

* **`/` in a room is not offered** — a room has no active-agent subject, so
  "whose playbooks?" has no answer without a picker this issue does not specify.
* Agents whose slug contains `.` or exceeds 100 characters never appear in the
  `@` list. They were never mentionable; offering them would create the silent
  failure AC#4 forbids. The fleet-wide fix (widen the grammar, or tighten slug
  creation, on both sides at once) is cross-repo.
* A mention typed mid-word (`foo@bar`) still resolves on send — the parser is
  unanchored. The typeahead does not offer it; unchanged ent#361 behaviour.
* After Esc — or after a pick that settled the token it inserted — that same
  token cannot be re-opened without editing it back. Appending to a just-picked
  name therefore keeps the list shut; backspacing into it brings the list back.
* No fuzzy matching. Playbook titles match on a word-start prefix (they are prose
  up to 200 chars, so a substring rule makes a one-character query match nearly
  everything); agent names also accept a substring, because they are short
  identifiers that frequently share a deployment prefix.
* The empty line cannot distinguish "no playbooks configured" from "the agent was
  stopped when the roster was built", so it deliberately says neither. **#2196
  makes the underlying fact available** — each roster card now carries
  `availability`, so the distinction is resolvable at this seam; wiring it into
  the empty line (and annotating an `@`-candidate row with its state) is PR 2 of
  that issue. Note the rule it must follow: **annotate, never filter** — an
  unavailable agent stays mentionable, exactly as it stays on the roster.
* The room `@` list is scoped to current participants — the names a pick is known
  to wake. If the engine's ent#361 newcomer-join path does recruit on a
  non-participant mention (not established here; see above), this list is
  narrower than the engine allows and recruiting is reachable only through
  "+ Add agent".
