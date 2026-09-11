/**
 * ent#557 — the wiring: the badge appears while you are elsewhere, and the two
 * obligations stay apart.
 *
 * `refreshThreads()` was event-driven only — a send, a navigation, a turn
 * finishing. A message an AGENT starts (the ent#523 Main case this feature is
 * about) therefore reached the sidebar on the user's next action and not before,
 * which is the same as not at all for someone in another tab.
 *
 * Source-asserted, and the reason is structural rather than laziness: this
 * project has no component-mount harness (`package.json` carries no
 * @vue/test-utils, jsdom or happy-dom; vitest runs `environment: 'node'`), so a
 * poll's wiring cannot be driven. What CAN be executed is the arithmetic, and
 * that lives in `portalUtils.js` and is exercised below against the same helper
 * the tab title uses — which is the actual "honest counts" property.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import { totalUnread, unreadByAgent, asksByAgent } from '../../src/components/portal/portalUtils'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const SHELL = read('../../src/views/Portal.vue')
const SIDEBAR = read('../../src/components/portal/PortalSidebar.vue')
const ROUTER = read('../../src/router/index.js')

describe('ent#557 — the indicator arrives without a navigation', () => {
  it('the existing poll refreshes threads as well as asks', () => {
    // Folded into the ent#364 timer rather than given its own: the Workspace
    // has no WebSocket a portal client is on, and a second cadence for one
    // badge is a second thing to reason about.
    const poll = SHELL.slice(SHELL.indexOf('function startAsksPoll'), SHELL.indexOf('function stopAsksPoll'))
    expect(poll).toContain('store.fetchAsks()')
    expect(poll).toContain('refreshThreads()')
  })

  it('stays visibility-aware — a backgrounded tab polls nothing', () => {
    const poll = SHELL.slice(SHELL.indexOf('function startAsksPoll'), SHELL.indexOf('function stopAsksPoll'))
    expect(poll).toMatch(/document\.visibilityState !== 'visible'\) return/)
  })

  it('adds no second timer', () => {
    // One cadence. A `setInterval` count that grows here is the thing to notice.
    const timers = SHELL.match(/setInterval\(/g) || []
    expect(timers.length, 'a new poll timer appeared').toBeLessThanOrEqual(2)
  })
})

describe('ent#557 — the tab marker is wired to the same number the rows show', () => {
  it('the shell computes the total through the shared helper', () => {
    expect(SHELL).toMatch(/const unreadTotal = computed\(\(\) => totalUnread\(threads\.value\)\)/)
    expect(SIDEBAR).toMatch(/totalUnread\(props\.threads\)/)
  })

  it('pushes it at the tab title, and clears on unmount', () => {
    expect(SHELL).toMatch(/watch\(unreadTotal, \(n\) => setUnreadCount\(n\), \{ immediate: true \}\)/)
    expect(SHELL).toMatch(/onUnmounted\(\(\) => clearUnreadCount\(\)\)/)
  })

  it('the router routes its title THROUGH the module, never writing it directly', () => {
    // Two direct writers would erase each other's half.
    expect(ROUTER).toMatch(/setBaseTitle\(label \? /)
    expect(ROUTER).not.toMatch(/document\.title\s*=/)
  })
})

describe('ent#557 — honest counts, and an ask is not an unread reply', () => {
  it('the total is the sum of the rows, so every unit is clickable', () => {
    const threads = [
      { agent_name: 'a', unread: 2 },
      { agent_name: 'b', unread: 1 },
      { agent_name: 'c', unread: 0 },
    ]
    expect(totalUnread(threads)).toBe(3)
    expect(unreadByAgent(threads)).toEqual({ a: 2, b: 1 })
  })

  it('zero threads is zero, not a badge', () => {
    expect(totalUnread([])).toBe(0)
    expect(totalUnread(null)).toBe(0)
  })

  it('asks are counted separately and never summed in (#2424)', () => {
    // Two different obligations: an ask waits on you to DECIDE, an unread reply
    // on you to READ. This feature adds nothing that merges them.
    const threads = [{ agent_name: 'a', unread: 2 }]
    const asks = [{ agent_name: 'a' }, { agent_name: 'a' }]
    expect(unreadByAgent(threads)).toEqual({ a: 2 })
    expect(asksByAgent(asks)).toEqual({ a: 2 })
    expect(totalUnread(threads)).toBe(2)
  })

  it('the two badges keep their separate renderings', () => {
    expect(SIDEBAR).toMatch(/data-testid="agent-ask-count"/)
    expect(SIDEBAR).toMatch(/v-if="waitingFor\(a\.name\)"/)
    // Different tokens, deliberately — the colours carry the distinction.
    expect(SIDEBAR).toMatch(/bg-status-urgent-500/)
    expect(SIDEBAR).toMatch(/bg-action-primary-600/)
  })
})
