/**
 * trinity-enterprise#491 — the sidebar's agent order is most-recent-collaboration.
 *
 * Two halves that pull against each other, which is the whole difficulty:
 * sending must move an agent to the top AT ONCE, and a reply arriving must NOT
 * re-sort. Derived-from-threads alone cannot do both, because a reply moves
 * `last_message_at` exactly like a send — so a brief landing for another agent
 * would reshuffle the list under the reader's cursor.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import {
  orderRosterAgents,
  collaborationRecency,
} from '../../src/components/portal/portalUtils.js'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

const AGENTS = [{ name: 'alpha' }, { name: 'beta' }, { name: 'zeta' }]
const names = (list) => list.map((a) => a.name)

describe('a room counts as collaboration with every agent in it', () => {
  it('ranks an agent you only ever meet in a room', () => {
    // The bug this fixes: `orderRosterAgents` skipped `is_room` rows outright, so
    // an agent you work with daily in a room read as never-used and sat at the
    // bottom under the alphabetical tiebreak.
    const threads = [
      { agent_name: 'alpha', last_message_at: '2026-09-01T10:00:00Z' },
      { is_room: true, agent_names: ['zeta', 'beta'], last_message_at: '2026-09-07T10:00:00Z' },
    ]
    // zeta and beta share the room's instant, so the alphabetical tiebreak
    // decides between them — deterministic, never arbitrary.
    expect(names(orderRosterAgents(AGENTS, threads))).toEqual(['beta', 'zeta', 'alpha'])
  })

  it('credits every participant, mirroring the unread fan-out', () => {
    const rec = collaborationRecency([
      { is_room: true, agent_names: ['a', 'b', 'c'], last_message_at: '2026-09-07T10:00:00Z' },
    ])
    expect([...rec.keys()].sort()).toEqual(['a', 'b', 'c'])
  })

  it('takes the max across a room and a 1:1, not whichever came last in the array', () => {
    const rec = collaborationRecency([
      { is_room: true, agent_names: ['alpha'], last_message_at: '2026-09-07T10:00:00Z' },
      { agent_name: 'alpha', last_message_at: '2026-01-01T10:00:00Z' },
    ])
    expect(rec.get('alpha')).toBe(new Date('2026-09-07T10:00:00Z').getTime())
  })

  it('falls back to created_at for a room nobody has spoken in', () => {
    const rec = collaborationRecency([
      { is_room: true, agent_names: ['alpha'], created_at: '2026-09-05T10:00:00Z' },
    ])
    expect(rec.get('alpha')).toBe(new Date('2026-09-05T10:00:00Z').getTime())
  })

  it('ignores rows with no usable timestamp rather than ranking them as epoch', () => {
    const rec = collaborationRecency([
      { agent_name: 'alpha' },
      { agent_name: 'beta', last_message_at: 'not-a-date' },
      null,
    ])
    expect(rec.size).toBe(0)
  })
})

describe('agents with no history sort last, alphabetically', () => {
  it('keeps a freshly shared agent findable', () => {
    const threads = [{ agent_name: 'zeta', last_message_at: '2026-09-07T10:00:00Z' }]
    expect(names(orderRosterAgents(AGENTS, threads))).toEqual(['zeta', 'alpha', 'beta'])
  })

  it('is the plain alphabetical roster when nobody has any history', () => {
    expect(names(orderRosterAgents(AGENTS, []))).toEqual(['alpha', 'beta', 'zeta'])
  })
})

describe('the session snapshot: sends re-sort, replies do not', () => {
  it('a pinned value wins over derived thread recency', () => {
    // The reply case: threads say alpha is newest, but this session already
    // ranked beta above it because the user SENT to beta.
    const threads = [{ agent_name: 'alpha', last_message_at: '2026-09-07T12:00:00Z' }]
    const pinned = { alpha: 1, beta: 2 }
    expect(names(orderRosterAgents(AGENTS, threads, null, pinned)))
      .toEqual(['beta', 'alpha', 'zeta'])
  })

  it('falls through to derived recency for an agent the snapshot does not know', () => {
    const threads = [{ agent_name: 'zeta', last_message_at: '2026-09-07T12:00:00Z' }]
    expect(names(orderRosterAgents(AGENTS, threads, null, { alpha: 1 })))
      .toEqual(['zeta', 'alpha', 'beta'])
  })

  it('ignores a non-numeric pinned value instead of ranking it as NaN', () => {
    const threads = [{ agent_name: 'zeta', last_message_at: '2026-09-07T12:00:00Z' }]
    const out = orderRosterAgents(AGENTS, threads, null, { zeta: 'soon' })
    expect(names(out)).toEqual(['zeta', 'alpha', 'beta'])
  })

  it('an absent snapshot behaves exactly as before — every existing caller', () => {
    const threads = [{ agent_name: 'beta', last_message_at: '2026-09-07T12:00:00Z' }]
    expect(names(orderRosterAgents(AGENTS, threads)))
      .toEqual(names(orderRosterAgents(AGENTS, threads, null, null)))
  })
})

describe('the primary-companion seam stays a seam', () => {
  it('still ranks a named primary first', () => {
    const threads = [{ agent_name: 'zeta', last_message_at: '2026-09-07T12:00:00Z' }]
    expect(names(orderRosterAgents(AGENTS, threads, 'alpha'))[0]).toBe('alpha')
  })

  it('the sidebar passes null, because ent#500 does not exist yet', () => {
    // Guessing a primary would be worse than the seam: nothing server-side can
    // say who it is, so any guess would be a confident wrong answer.
    const src = read('../../src/components/portal/PortalSidebar.vue')
    expect(src).toMatch(/orderRosterAgents\(\s*props\.roster,\s*props\.threads,\s*null,/)
  })
})

describe('wiring', () => {
  it('the store seeds only missing agents, so a refresh cannot undo a send', () => {
    const src = read('../../src/stores/clientPortal.js')
    const fn = src.slice(src.indexOf('seedAgentRecency('), src.indexOf('noteAgentInteraction('))
    expect(fn).toMatch(/=== undefined/)
  })

  it('the bump happens on SEND, not when a reply lands', () => {
    const src = read('../../src/components/portal/PortalConversation.vue')
    const at = src.indexOf('noteAgentInteraction')
    expect(at).toBeGreaterThan(-1)
    // Inside submitUserText — the user's own action — and not in the reply path.
    const fnStart = src.lastIndexOf('async function submitUserText', at)
    expect(fnStart).toBeGreaterThan(-1)
    expect(at - fnStart).toBeLessThan(800)
  })

  it('the store derives from the shared rule rather than a second copy', () => {
    const src = read('../../src/stores/clientPortal.js')
    expect(src).toMatch(/collaborationRecency/)
  })
})
