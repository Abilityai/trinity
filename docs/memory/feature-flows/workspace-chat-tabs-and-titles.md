# Feature: Workspace chats as tabs, New chat hotkey, and renameable titles

> **Status**: ✅ Implemented (2026-09-06)
> **Issues**: abilityai/trinity-enterprise#451 (remaining slice — #2430 shipped the fresh-thread half), abilityai/trinity-enterprise#473
> **Requirement**: `docs/memory/requirements/core-agent.md` §5.21
> **Related**: [workspace-absorbs-session.md](workspace-absorbs-session.md) (the ent#451 `new_thread` half), [workspace-sidebar-ia.md](workspace-sidebar-ia.md) (the row this extends), [workspace-agent-page.md](workspace-agent-page.md) (the full chat list), #523 (the pinned Main chat and the merged page — the seam this leaves open)

## Overview

Two rulings from the same operator session (2026-09-06), one PR. The agent's
chats render as **tabs above the thread**; **New chat** moves into the
conversation header with **⌘J / Ctrl+J**; and a person can **rename** any chat
or room in place — with the generated titles (ent#186) made trustworthy: never
over a person's title, one more pass when the opener was a greeting or the
first attempt never landed, and a failing generator that is visible to the
operator once rather than silent forever.

## The tab strip (ent#451)

```
PortalConversation.vue
  <header>  [agent picker] [title, renameable]        [+ New chat ⌘J] [★] [voice] [files]
  <PortalChatTabs>   Q3 invoices | Onboarding | New chat | 3 more ▾      ← OverflowTabs, dense
  <thread>
```

- `portalUtils.agentChatTabs(threads, agentName)` is the whole rule: this
  user's **threads** with the active agent (rooms are not an agent's tabs;
  another agent's threads are not this agent's), most recent first
  (`last_message_at` falling back to `created_at`), labelled through the same
  `threadTitle` the sidebar uses. It is a **slice of the sidebar's list** —
  `Portal.vue` passes the same `threads` it renders in the sidebar — so the
  two can never disagree and nothing is fetched twice.
- `PortalChatTabs.vue` mounts the design system's `OverflowTabs` with two
  additions to the primitive: `dense` (smaller pad and type for a strip above
  a thread) and `moreLabel` (a function of the hidden count, so the trigger
  reads "N more" as the contract asks; every existing strip keeps "More"). The
  mirror row measures the WIDEST label the strip can need (every tab hidden),
  so a count that grows never reflows the fit. Resize repacking is inherited:
  the primitive re-measures on `ResizeObserver`, which covers the rail (#492)
  and the window alike.
- **An unsaved new chat is not a tab.** The ruling: "a new chat exists — tab
  and sidebar row — once its first message is sent". The strip therefore has
  no phantom entry and no active id while the first message is unsent, and
  renders **no chrome at all** for an empty list.
- **The Main tab is #523's first slot.** Nothing here assumes there is not
  one; when it lands it is the first element of this list.

## New chat in the header, ⌘J / Ctrl+J (ent#451)

- The header button starts a fresh thread **with this agent**
  (`emit('new-chat')` → `Portal.vue::newChatWithAgent(activeAgent.name)`); the
  sidebar's button remains the cross-agent picker. The label shows at `md`,
  the `<kbd>` at `lg`; `title` and `aria-keyshortcuts` carry the chord always.
- `isNewChatHotkey(e)`: `j` with a **plain** ⌘ or Ctrl — never Shift/Alt,
  never both modifiers (those are someone else's shortcuts). ⌘N is the
  browser's; ⌘⇧O was declined.
- Armed on `window` in `Portal.vue`'s `onMounted` **above** `bootstrap()`'s
  `await` (contract #23: handlers are armed at mount, never behind fetched
  data), removed in `onBeforeUnmount`, inert until signed in. It resolves the
  agent in front of the person — the agent page's, or the open conversation's
  — and opens the picker in a room or on the roster root.

## Renaming (ent#473)

### One editor, three homes

`PortalEditableTitle.vue` is mounted by the sidebar row (`PortalChatRow`),
the 1:1 header (`PortalConversation`) and the room header (`PortalRoom`). One
component because the three would otherwise drift on exactly the things that
matter: the pencil's reveal rule, the Enter/Esc/blur contract, the client-side
validation and the failed-verb surface.

- **Read mode**: the title (or the placeholder in tertiary ink) plus a pencil.
  Dense rows reveal the pencil on hover from `sm` and always below it — the
  star's reason (ent#359): a touch screen has no hover.
- **Edit mode**: a plain field (BaseInput owns a label row and form padding —
  the right primitive for a form, the wrong one inside a 40px row), same field
  tokens and focus ring. Enter and blur commit; Esc abandons; an unchanged
  draft on blur is an abandon, not a save.
- **Every click and key stops inside it.** The sidebar row is a `role="button"`
  div that opens the chat on Enter/Space/click; without the stops, a rename
  would open the chat it was renaming.
- `normalizeChatTitle` mirrors the server leaf so the person is told before
  the request; a server refusal renders **verbatim** in an `InlineError`
  beside the field (principle 18) — a named 400 carries the rule's own
  sentence, a 404 says the chat is no longer theirs, anything else still names
  the next action.
- `Portal.vue::renameChat(t, title)` updates the list optimistically (row and
  header redraw at once), **reverts and rethrows** on refusal so the editor
  shows the reason, and re-reads the list after success — so a title the
  generator landed meanwhile, or a rename from another tab, is what shows
  next. The room header keeps itself in step without a refetch and emits
  `rooms-changed`.

### The endpoints

| Surface | Route | Scope | Refusals |
|---|---|---|---|
| Thread | `PATCH /api/enterprise/client-portal/agents/{agent}/sessions/{id}` `{title}` | roster gate, then the UPDATE is (agent, client)-scoped | uniform 404 (unowned id, off-roster agent); named 400 `invalid_title`; per-viewer rate limit |
| Room | `PATCH /api/rooms/{room_id}` `{name}` | membership (uniform 404), then **person-only** | 403 `not_a_person` for a member agent; named 400 `invalid_title` |

- **One validator, one leaf**: `services/chat_title.py::normalize_chat_title`
  — outer trim, inner whitespace collapsed, control characters dropped, an
  **inner** line break refused (a pasted two-line note is not a title, and
  joining it would render a sentence the person never wrote), non-empty,
  ≤ 100 characters (wider than the generator's 60: a model is asked for
  something sidebar-shaped, a person is allowed to be precise). Both services
  import it, so a thread and a room refuse the same titles for the same
  reasons. The refusal is `{code: "invalid_title", reason, message}` where
  the message names the rule, the fix and an example (principle 17). Pydantic
  bounds the body at 4000 only against abuse, so a real over-long title gets
  the named 400 rather than a 422 about a schema.
- **A member agent talks; it does not rename.** An agent in a room is
  reachable through its own MCP key and is a prompt-injection surface; a
  room's name is what every participant reads it by. Membership is checked
  first, so the 403 discloses nothing a member cannot already see. One notch
  below `_require_moderator`, since a rename is neither lifecycle nor roster.
- The room broadcast is a thin `room_renamed` trigger carrying the id only
  (#918): listeners refetch through the membership-scoped read.
- No MCP tool: rename is a person's verb on the UI; the routers' `# mcp:`
  headers stand.

### A person's title stands — `title_source`

`enterprise_portal_sessions.title_source`: NULL (the derived fallback, or any
row that predates the column) · `'generated'` · `'user'`. SQLite
`portal_session_title_source` + Alembic `0052_portal_session_title_source`,
**no backfill** — an existing title keeps working, and NULL is the honest hand
for a row nobody can attribute (it still lets generation land).

The generated write is guarded **in the UPDATE**:

```sql
UPDATE enterprise_portal_sessions SET title = :title, title_source = 'generated'
WHERE id = :id AND (title_source IS NULL OR title_source != 'user')
```

Generation runs off the reply path (`_spawn_title_generation`), so a rename
typed inside the first turn's 15 s window races the model's guess. A
read-then-write in the caller would leave exactly that window; the WHERE
clause leaves none. `set_portal_session_title` returns whether it landed; a
stood-down generation is logged, never retried.

### One more generation pass

`_title_plan(row, history)` runs on the **pre-turn** row (read before
`_persist_user_turn` writes the fallback and bumps the count) once history is
in hand:

| Row | → |
|---|---|
| title empty | `first` (ent#186, unchanged) |
| `title_source == 'user'` | nothing — ever |
| `message_count > 2` | nothing — past the window |
| hand still NULL (the first call failed, produced nothing usable, or the first turn failed) | `retry` |
| hand `'generated'` and the opener `is_greeting` | `retry` |
| hand `'generated'`, opener has a topic | nothing |

`is_greeting`: a short message (≤ 8 words) opening with a salutation, a
check-in or a test word — "hi", "Hello there!", "are you there?", "test".
"Hi, can you pull the Q3 invoices for Acme…" is long enough to have a topic
and is not retried. The retry feeds **this** exchange — the first one with a
topic in it. `message_count <= 2` is what makes it exactly one more: a thread
with a second exchange on record is past the window, whatever happened.

### A failing generator is observable once

Every path in the generator is fail-soft for the client — right — and was
silent for the operator: an install whose generator had never worked once
looked identical to one that worked every time. Now:

- `_record_title_outcome(outcome, detail)` folds each attempt into an
  in-process record: `state` (`unknown` · `ok` · `no_credential` · `failing`),
  consecutive failures, timestamps, a bounded credential-free reason ("HTTP
  401", "request failed: ConnectError"), the model.
- A credential miss is an episode from the first hit; transport/API failures
  need **3 in a row**, so a single upstream blip pages nobody.
- The transition **into** a bad state logs one WARNING (pointing at Settings →
  Workspace sessions); the steady state is quiet; a recovery logs INFO and
  re-arms so the next episode warns again.
- `title_generation_health()` rides `GET /api/settings/portal-session-policy`
  → `title_generation` — the one Workspace settings payload every edition
  renders — and `PortalSessionPolicyPanel.vue` shows it as a warning notice
  through `titleGenerationNotice`: nothing while `ok`/`unknown` (a panel that
  reassures on every load trains people to skip it); the missing credential
  names the next action; a failing episode counts and quotes the reason.
- Per process: sibling workers keep their own view, which is honest — each
  one is the one that made the calls.

## What this deliberately leaves to #523

The pinned **Main** chat (first tab), **Reset**, the merged agent page, and
the sidebar's "a recent-chat row opens the agent page with that chat active".
Until then a row opens the thread view, which now carries the agent's tabs —
so opening a row already lands inside that agent's chat list.

## Tests

- `tests/unit/test_ent473_chat_titles.py` — the validator table, the greeting
  shape, the `_title_plan` decision table, the UPDATE guard against a real
  sqlite, rename scoping, search-by-user-title, the named 400 at the router,
  route registration, the room rules (person lands + thin broadcast,
  workspace client is a person, agent refused, same 400), health episodes
  (once, threshold, recovery, no key material), the settings payload, and
  both migration tracks.
- `tests/unit/test_ent79_portal_exposure.py` — the second pass end to end
  through `portal_chat`: `first` then `retry` when the first never landed,
  nothing on a third turn; nothing on the second turn once a title landed on
  a topic.
- `src/frontend/tests/unit/portalChatTabsAndTitles.spec.js` — the client
  validator mirror, the refusal rendering, `agentChatTabs` (slice, order,
  fallback label, no phantom tab), the hotkey chord table and labels, the
  settings notice, and source pins: the strip IS `OverflowTabs`, every
  existing strip keeps "More", the three homes mount the one editor, the
  hotkey is armed above the await.

## Verification

Backend: the ent#473 file plus the portal/rooms/migration neighbours
(`test_ent79/451/359/358/443`, `test_2198/2133/2320/525`, migrations, Alembic
parity + heads, the auth/enumeration/models static guards). Frontend: full
`npm run test:unit`, `npm run check:tokens`, `vite build`. Live: the Docker
stack (backend `--reload` picked up the migration), the rename endpoints and
the settings payload through curl, and the Workspace in the browser.
