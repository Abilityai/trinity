# Feature: Workspace drafts — unsent input survives switching, with a Draft mark

> **Status**: ✅ Implemented (2026-09-20)
> **Issue**: abilityai/trinity-enterprise#657
> **Requirement**: `docs/memory/requirements/core-agent.md` §5.35
> **Related**: [workspace-chat-tabs-and-titles.md](workspace-chat-tabs-and-titles.md) (the strip, and the provisional New chat tab a draft now lists) · [workspace-sidebar-ia.md](workspace-sidebar-ia.md) (the per-viewer star/unread state the mark sits beside) · [workspace-session-signout.md](workspace-session-signout.md) (why sign-out clears the bucket and expiry does not)

## Overview

Text typed into a Workspace composer used to die on the next click. The shell
keys `PortalConversation` on `convKey` and `PortalRoom` on the room id, so every
switch — another agent, another chat tab, a room — **remounts the stage**, and
the composer's `input` was a ref of that stage. Start writing to one agent,
check something with another, come back: gone.

Drafts now outlive the switch, come back exactly as left, and are marked
wherever the conversation is listed.

## The thing that decided the design

**The composer IS the draft.** Everything else follows from refusing to keep a
second copy of the text anywhere.

The binding (`composables/useComposerDraft.js`) is write-through: every change
to `input` writes the store, and empty (or whitespace-only) clears it. That one
rule closes the whole acceptance list for free:

| Path | What it already does | What that makes the draft do |
|---|---|---|
| `send()` | `input.value = ''` before dispatch | cleared, in the same tick — a sent message can never come back as a draft |
| a failed turn | red row + Retry, composer untouched | no draft (the text is on the row, not in the field) |
| `cancelTurn()` | `restoreDraft(...)` puts the words back | a draft again, honestly (the AC's own wording) |
| a failed escalation | shell hands the text back via `prefill` | a draft again |
| `PortalRoom.send()` catch | `input.value = text` | a draft again |
| "Ask about it" / a playbook | `prefill` overwrites the composer | the explicit act wins, and becomes the draft |

No `clearDraft()` calls were added to any of them. A new path that hands text
back to a composer inherits the behaviour without knowing this feature exists.

## Shape

```
localStorage['trinity-workspace-drafts:<client_email>']
        ▲ read-merge-write, ONE key at a time │ 'storage' event → re-read
        │                                     ▼
clientPortal.clientEmail ──▶ stores/portalDrafts.js ◀── components/portal/portalDrafts.js (pure)
                                   │ get/set/clear/move/keys/newChatDraftAgents
        ┌──────────────────────────┼─────────────────────────────┐
composables/useComposerDraft.js   views/Portal.vue          (props / store reads)
   ▲ key · input                   │ decorate() stamps `hasDraft`
   │                               ├──▶ PortalSidebar ──▶ PortalChatRow   (row mark)
PortalConversation (thread:|new:)  │        agentsWithDrafts() → agent-row mark
PortalRoom         (room:)         └──▶ PortalConversation ──▶ PortalChatTabs ──▶ OverflowTabs (tab.hasDraft)
```

### Keys

`thread:<session id>` · `room:<room id>` · `new:<agent>` — the first two are
the shell's own `chatKey`, the scheme `enterprise_portal_chat_state` is already
keyed by. That is deliberate: if drafts ever become server-side (the issue puts
cross-device sync out of scope), only the source changes, not the keys.

A conversation whose thread is not known yet (a cold `/workspace` root, still
resolving inside `loadThread(null)`) binds **no** key — there is no honest place
to put what is typed, so it is held in memory until the thread resolves.

### The four in-instance transitions

A switch normally remounts, but `PortalConversation`'s identity can change
underneath a live instance. `reconcileDraftOnKeyChange` is the one rule:

| From → to | Why it happens | What the composer does |
|---|---|---|
| `key` → `null` | `openAgentPage` on the **current** agent nulls `pendingSession` without bumping `convGen`; `landOnAgent` remounts a beat later | keeps its text, writes nothing |
| `null` → `key` | the cold root resolved a thread | fills only an **empty** composer; text typed while it resolved wins and is persisted under the resolved key |
| `new:` → `thread:` | session adoption (`adoptSession`, the one seam all three adoption sites share) | **moves** — what was typed during the turn follows the thread it created |
| `key` → `key` | an in-place switch | shows the destination's draft (the source was already stored by write-through) |

### Identity

The bucket is namespaced by **`clientEmail`** — the principal the roster
reports (`get_roster` → `client_email`), for external clients and platform
users alike. It is known before any composer or row renders, because the stage
gates on `rosterLoaded`.

Deliberately **not** the column-layout rule (`resolveLayoutIdentity`, which
reads `auth0_user` and gives every portal client one shared `client` bucket).
That rule exists because widths must be read *synchronously before first paint*;
drafts have no such constraint, and a shared bucket that is harmless for a
number is not harmless for someone's words. The divergence is recorded in
`architecture/workspace.md` so the next per-viewer cache does not invent a
fourth rule.

An identity change **swaps** the map (never merges). With no identity the map is
memory-only and nothing is written.

### Two tabs

The Workspace opens in its own tab (ent#456), so persistence is **key-granular**:
`persistDraft` re-reads the bucket, applies one key, bounds, writes. A whole-map
write would carry tab B's stale snapshot of tab A's keys — erasing a draft A had
just written, or re-persisting one A had just **sent**, which is the one thing
AC-6 forbids. A `storage` event for the bucket re-reads it, so a send in one tab
clears the mark in the other. Residual: the same conversation edited in two tabs
is last-write-per-key.

### The mark

One component (`components/base/DraftMark.vue`), four homes: the agent row, the
chat tab (`OverflowTabs`' optional per-tab `hasDraft` — inline, in the More
menu, and in the hidden mirror row that measures widths, plus the re-measure
key, because the mark changes a tab's width), the thread/room row, and a
search-result row.

It is the **word** "Draft", not an icon. Every glyph that would read as a draft
is already spoken for on these exact surfaces: the pencil is the rename
affordance (`PortalEditableTitle`, in the header and on hover in the same chat
rows), the dot is the rail's activity signal in the tab strip, the star is the
per-viewer pin. Two facts sharing one shape is the failure principle 24 names.
Tertiary ink in each theme (gray-500 / gray-400), mono-caps overline size: quiet
beside the unread and ask pills, which are obligations — a draft is a state of
the reader's own work. Measured: row and tab geometry is byte-identical with and
without it.

**A room marks its own row only**, never its participants' agent rows.
`unreadByAgent` propagates because the agent row was once the way into an unread
chat (ent#359); a room is always listed in the sidebar, so the room row is the
draft's own door.

**A drafted agent is lifted above the "N more" collapse**, exactly as an asked
one is (`visibleAgentRows`) — "visible without opening it" is false for a row
nobody can see.

**A `new:<agent>` draft lists the provisional New chat tab** even while another
chat is open, and selecting it opens a new chat with that agent (`new-chat`).
Without that the words would have no door at all: the agent row lands on Main
(`landOnAgent`), so the tab is the only way back to them.

### Bounds and degradation

- ≤ 100 drafts per person; the oldest `updatedAt` is evicted.
- An entry over 64 000 characters is session-only — its storage entry is
  **removed**, never truncated. One pasted log must not be serialized on every
  keystroke, and a quota failure on it must not disable persistence for every
  other draft in the bucket.
- Storage unavailable (private mode, blocked site data, quota): drafts still
  survive switches for the session; only reload survival is lost, silently.
- `keys` is derived from a sorted-key **signature string**, so it changes
  identity only when membership changes — a keystroke never re-renders the
  sidebar or the strips.

### Lifecycle edges

- **Reset (ent#523)** moves `thread:<archived>` → `thread:<new Main>`: the reset
  archives the history, not what the person was typing.
- **A closed room** clears its draft when it loads — a mark with no field behind
  it is a dead affordance.
- **Explicit sign-out** (`signOutEverywhere`) removes the bucket, *before*
  `signOut()` nulls `clientEmail` (after it, the store's identity is already
  null and the clear removes nothing). **Expiry keeps it** — expiry is not the
  person's act, and a draft is what they come back for.
- **A deleted thread's** draft lights nothing (the row is gone) and is evicted by
  the cap.

## Residuals

- Draft text rests in the clear in the browser profile, under an email-named
  key. That is the same trust boundary that already holds the portal session
  token and the platform JWT; an attacker with that access can act as the
  person. Explicit sign-out removes it.
- Attached-but-unsent files are session-only (the issue allows this if
  retaining them is not cheap — it is not: the chips carry upload state and
  server-side inbox ids).
- The caret is restored to the **end** of the text, not to where it was; and
  the restore focuses on a fine pointer only, so a phone does not get the soft
  keyboard over the transcript on arrival.

## Tests

- `src/frontend/tests/unit/portalDrafts.spec.js` — keys, whitespace, the codec
  against a fake `Storage` (blocked reads, refused writes, corrupt JSON, wrong
  version), the cap, `agentsWithDrafts`, the four reconcile branches, the store
  (identity swap, memory-only with no identity, oversize, `clearBucket`, and
  **two store instances over one storage** = two tabs), and the tab/collapse/title
  rules.
- `portalComposerDraft.spec.js` (jsdom) — the composable on live refs, a
  `StorageEvent` re-read, and `PortalChatRow` / `PortalChatTabs` / `OverflowTabs`
  **mounted**: the mark renders from data, the provisional tab's click reaches
  `new-chat`, clicking the active one does not, and a mark appearing re-measures
  the strip.
- `workspaceSession.spec.js` — sign-out clears the bucket, expiry keeps it.
- `e2e/workspace-drafts.spec.js` (`@smoke`, hermetic `page.route` mocks) — the
  only thing that executes the wiring: agent round trip with focus, two drafts
  at once, reload survival, the room round trip, tab-to-tab isolation. CI runs
  `@smoke` only, which is why it is not `@interactive`.
- Mutations, each verified red: the write-through watch removed; whitespace
  accepted as a draft; the mark dropped from the mirror row; `hasDraft` dropped
  from the re-measure key; buckets merged on identity change; the `→ null`
  reconcile branch dropped; `clearBucket()` moved after `signOut()`; each
  composer's `useComposerDraft` call deleted (e2e only); `hasDraft` dropped from
  `decorate()` (e2e only).
