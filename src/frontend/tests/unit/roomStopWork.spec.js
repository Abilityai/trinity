/**
 * #2795 — running executions in a room can be stopped.
 *
 * Two independent gaps stacked up, so the fix has two halves and this spec
 * proves each of them separately:
 *
 *   1. the room's live tiles were never handed `can-stop` / `@stop`, so the
 *      Stop button `PortalWorkCard` already renders was simply never turned on
 *      there — a SOURCE guard, because there is no mount harness;
 *   2. Escape gets a rule of its own (`soleStoppableItem`), because a room
 *      fans out to several agents and "stop the turn" has to name which one.
 *
 * The server half (`can_stop` admitting `kind: "room"`) lives in
 * `tests/unit/test_2795_room_stop.py` — the button being wired is worth
 * nothing while the server says the row is unstoppable.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { parse } from '@vue/compiler-sfc'
import { baseParse } from '@vue/compiler-core'
import { stripComments } from './helpers/stripComments'
import { soleStoppableItem } from '@/components/portal/portalWork'

const ROOM = stripComments(
  readFileSync(fileURLToPath(new URL('../../src/components/portal/PortalRoom.vue', import.meta.url)), 'utf8'),
)

const live = (over = {}) => ({ id: 'e1', agent_name: 'a', status: 'running', can_stop: true, ...over })

describe('#2795 soleStoppableItem', () => {
  it('returns the one stoppable live row', () => {
    const it0 = live()
    expect(soleStoppableItem([it0])).toBe(it0)
  })

  it('is null when two rows are stoppable — Escape must not pick one by position', () => {
    // THE property. Guessing destroys work somebody is still waiting for.
    expect(soleStoppableItem([live({ id: 'e1' }), live({ id: 'e2' })])).toBeNull()
  })

  it('ignores rows the server says cannot be stopped', () => {
    const mine = live({ id: 'e1' })
    expect(soleStoppableItem([mine, live({ id: 'e2', can_stop: false })])).toBe(mine)
    expect(soleStoppableItem([live({ can_stop: false })])).toBeNull()
  })

  it('ignores rows that are not live — a stale row is not stoppable', () => {
    expect(soleStoppableItem([live({ stale: true })])).toBeNull()
    expect(soleStoppableItem([live({ status: 'success' })])).toBeNull()
  })

  it('ignores a row whose cancel is already in flight', () => {
    // Otherwise the row being stopped keeps holding the "sole" slot and a
    // second press acts on it again.
    expect(soleStoppableItem([live({ id: 'e1' })], ['e1'])).toBeNull()
    const other = live({ id: 'e2' })
    expect(soleStoppableItem([live({ id: 'e1' }), other], ['e1'])).toBe(other)
  })

  it('survives junk without throwing', () => {
    expect(soleStoppableItem(null)).toBeNull()
    expect(soleStoppableItem(undefined, undefined)).toBeNull()
    expect(soleStoppableItem([null, undefined])).toBeNull()
  })
})

describe('#2795 the room wires the card it already renders', () => {
  it('passes the server verdict to the tile, never a local guess', () => {
    // `can_stop` mirrors what the terminate route will accept; a client-side
    // re-derivation is how the button becomes a lie.
    expect(ROOM).toMatch(/:can-stop="it\.can_stop"/)
  })

  it('shows the Stopping… state from the shared store', () => {
    expect(ROOM).toMatch(/:stopping="workStore\.stoppingIds\.includes\(it\.id\)"/)
  })

  it('stops through the Work tab’s own store action — one cancel path', () => {
    expect(ROOM).toMatch(/@stop="onStopWork"/)
    expect(ROOM).toMatch(/workStore\.stopItem\(item\)/)
  })

  it('surfaces a refused cancel, which is the outcome a person must act on', () => {
    expect(ROOM).toMatch(/data-testid="portal-room-stop-error"/)
    expect(ROOM).toMatch(/cancelOutcome\(\{ ok: false \}\)/)
  })

  it('asks the shared Escape rule, with the popups as overlays', () => {
    // ent#155's rule unchanged: anything nearer the keystroke wins.
    expect(ROOM).toMatch(/shouldCancelOnEscape\(/)
    expect(ROOM).toMatch(/overlays: \[typeaheadOpen\.value, addOpen\.value\]/)
    expect(ROOM).toMatch(/soleStoppableItem\(roomLiveItems\.value, workStore\.stoppingIds\)/)
  })
})


describe('#2795 the live-work chain survives the new error line', () => {
  // `roomComposerChain.spec.js` pins the COMPOSER's chain. This is the same
  // hazard one region up, and it is not hypothetical: the first draft of this
  // change inserted the stop-error paragraph between `v-if="roomLiveItems"`
  // and `v-else-if="workingAgents"`, which silently repointed the "…is
  // thinking…" fallback at `stopError`. The SFC compiled and every other test
  // passed.
  const ELEMENT = 1
  const SRC = readFileSync(
    fileURLToPath(new URL('../../src/components/portal/PortalRoom.vue', import.meta.url)), 'utf8',
  )
  const { descriptor } = parse(SRC, { filename: 'PortalRoom.vue' })
  const ast = baseParse(descriptor.template.content)

  const all = []
  ;(function walk(node) {
    for (const child of node.children || []) {
      if (child.type === ELEMENT) { all.push(child); walk(child) }
    }
  })(ast)

  const dir = (node, name) => (node.props || []).find((pr) => pr.type === 7 && pr.name === name)
  const elementChildren = (node) => (node.children || []).filter((c) => c.type === ELEMENT)
  const expr = (node, name) => dir(node, name)?.exp?.content || ''

  function previousSibling(node) {
    const parent = all.find((n) => elementChildren(n).includes(node))
    if (!parent) return null
    const sibs = elementChildren(parent)
    return sibs[sibs.indexOf(node) - 1] || null
  }

  it('the "is thinking…" fallback still chains off the work tiles', () => {
    const fallback = all.find((n) => expr(n, 'else-if').includes('workingAgents.length'))
    expect(fallback, 'the server-derived thinking fallback is gone').toBeTruthy()
    expect(expr(previousSibling(fallback), 'if')).toContain('roomLiveItems.length')
  })

  it('the "sending" fallback still chains off the "is thinking…" one', () => {
    const sending = all.find((n) => expr(n, 'else-if') === 'sending')
    expect(sending, 'the local sending fallback is gone').toBeTruthy()
    expect(expr(previousSibling(sending), 'else-if')).toContain('workingAgents.length')
  })

  it('the stop-error line sits OUTSIDE the chain', () => {
    const line = all.find((n) => (n.props || []).some(
      (pr) => pr.type === 6 && pr.name === 'data-testid'
        && pr.value?.content === 'portal-room-stop-error',
    ))
    expect(line, 'the stop-error line is gone').toBeTruthy()
    // It must not be the element any chain arm binds to.
    const after = all.find((n) => previousSibling(n) === line)
    if (after) {
      expect(dir(after, 'else-if') || dir(after, 'else')).toBeFalsy()
    }
  })
})
