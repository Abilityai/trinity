# Feature: Workspace transcripts stay where the reader put them

> **Status**: ✅ Implemented (2026-09-09)
> **Issue**: abilityai/trinity#2624
> **Related**: [workspace-chat-tabs-and-titles.md](workspace-chat-tabs-and-titles.md) (the thread strip above the same transcript), [workspace-composer-typeahead.md](workspace-composer-typeahead.md) (the other "one rule, both surfaces" composable), #1927 (the same family one surface over: a background poll must not reset loaded UI state)

## Overview

Both Workspace chat surfaces scrolled to the bottom on **every** arrival.
`scrollDown()` was an unconditional `scrollTop = scrollHeight`, and every path
called it: the room's 3s poll, an agent reply settling, a cancelled turn
settling, the history load, the send. Scroll up to re-read an earlier answer and
the next message yanked you back down — in a room, from any participant, every
three seconds, so reading anything but the tail was close to impossible.

Design-system principle 5 already said it: *updates preserve scroll, selection,
focus, and expansion*. What was missing was the distinction between an **arrival**
and an **intent**.

* An **arrival** — a poll fetching somebody else's message, a reply settling, a
  stream ending — follows only if the reader was already following.
* An **intent** — sending, opening a thread, clicking jump-to-latest — always
  pins and re-arms, whatever the prior scroll position.

## Where the rule lives

`src/frontend/src/composables/useStickToBottom.js`. One module, because
`PortalConversation.vue` and `PortalRoom.vue` both own a `scrollEl` on the same
`flex-1 min-h-0 overflow-y-auto` container and had **two copies of the same
bug** — the AC's "implemented once, not twice". It exposes:

| Name | Used by | Behaviour |
|------|---------|-----------|
| `onScroll` | the container's `@scroll.passive` | recomputes `following` from where the reader is; re-arming clears `unread` |
| `onArrive(count)` | every arrival path | follows, or counts toward the affordance |
| `pinToBottom()` | send, thread/room load | always goes to the bottom and re-arms |
| `scrollToLatest()` | the jump-to-latest control | the same thing, named for the reader |
| `reset()` | a thread/room switch | re-arms **without** touching the DOM |
| `following` / `unread` / `showJumpToLatest` | the control | state |

`chat/ChatMessages.vue` still carries the unconditional pattern behind its
`autoScroll` prop. It is a different surface (Agent Detail, not the Workspace)
and out of this issue's scope; it can adopt the composable unchanged.

## The decisions worth keeping

**The threshold is 64px, and it is not cosmetic.** A pixel-exact "at the bottom"
test fails two ways that have nothing to do with the reader: `scrollHeight`
EXCLUDES the border under `box-sizing: border-box` (the gotcha `PortalRoom.vue`
already documents for its composer), and fractional layout — zoom, device pixel
ratio, a fractional line-height — leaves a sub-pixel gap when the reader *is* at
the bottom. 64px is also roughly "a line off the bottom", which is what a reader
would call still following.

**A missing element answers "following".** Before first paint, and on a thread
too short to overflow, the reader IS at the bottom. Answering false would open
every thread detached and badge its first reply as unread.

**`reset()` deliberately does not scroll.** On a thread switch the outgoing
transcript's element is about to be replaced; scrolling it would move a surface
being torn down. The incoming thread's own load pins it.

**The affordance requires detached AND behind.** A reader scrolled up with
nothing new below them has missed nothing, and a control claiming otherwise
would be the dishonest kind. It says *what* was missed ("3 new messages"), not
just that something was — a bare arrow cannot tell a reader whether it is worth
going.

**Pinning takes TWO passes, one frame apart.** This is the one thing the unit
tests could not have found. `nextTick` covers the component's own patch, but a
child patching on a later tick — the loading skeleton swapping out, markdown
rendering a long thread — grows the transcript *after* the first measurement,
and the browser clamps the assignment to the height it had then. Measured in a
real browser: a 40-message thread opened **20px above** its newest message and
stayed there, stably, every time. The e2e found it; the second pass fixes it.

## Call-site map

| File | Path | Kind |
|------|------|------|
| `PortalRoom.vue` | `load({ full: true })` — opening a room | intent → `pinToBottom` |
| `PortalRoom.vue` | `load()` — the 3s poll | arrival → `onArrive(incoming.length)` |
| `PortalRoom.vue` | `send()` | intent → `pinToBottom` |
| `PortalRoom.vue` | the `roomId` watcher | `reset()` (no DOM) |
| `PortalConversation.vue` | `loadThread()` | intent → `pinToBottom` |
| `PortalConversation.vue` | `reattach()` settle | arrival → `onArrive(1)` |
| `PortalConversation.vue` | `deliver()` settle | arrival → `onArrive(1)` |
| `PortalConversation.vue` | `send()` | intent → `pinToBottom` |
| `PortalConversation.vue` | the agent/session watcher | `reset()` (no DOM) |

A **streaming** reply needs nothing of its own: nothing moves the viewport while
the reply grows, and the settle above is the only scroll it can cause.

## Coverage

* `tests/unit/stickToBottom.spec.js` — 21 cases over the composable. `vitest`
  runs `environment: 'node'` with no mount harness, so the element is a plain
  object carrying the three numbers the decision reads, which is also the honest
  scope of the decision.
* `e2e/workspace-stick-to-bottom.spec.js` — `@smoke`, `page.route()` mocks. The
  room is the surface under test for the arrival case because its arrivals are
  the deterministic ones: the poll fetches, so a mock answering the second call
  with one more message IS an arrival, with no LLM and no timing guesswork.
  Every assertion is `scrollTop` — a real layout engine is the only thing that
  can state where the viewport actually is, and it is what caught the two-pass
  defect above.
