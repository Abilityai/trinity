import { describe, it, expect } from 'vitest'
import {
  PAGE_SIZE, visibleKeys, keyCounts, filterKeys, pageOf, emptyReason,
} from '../../src/utils/mcpKeyList.js'

/** #2202 — the MCP Keys panel's list rules, driven for real. */
const key = (over = {}) => ({
  id: over.id || 'k1', name: 'Key', key_prefix: 'trinity_mcp_ab', scope: 'user',
  is_active: true, agent_name: null, ...over,
})

const FLEET = [
  key({ id: 'u1', name: 'Laptop', scope: 'user' }),
  key({ id: 'u2', name: 'Old laptop', scope: 'user', is_active: false }),
  key({ id: 'a1', name: 'scout key', scope: 'agent', agent_name: 'scout' }),
  key({ id: 'a2', name: 'ghost key', scope: 'agent', agent_name: 'ghost', is_active: false }),
  key({ id: 's1', name: 'system', scope: 'system' }),
]

describe('visibleKeys', () => {
  it('hides agent-scoped keys from a non-admin', () => {
    // An access rule, not a convenience filter: agent-scoped rows carry the
    // fleet's agent names.
    expect(visibleKeys(FLEET, { isAdmin: false }).map((k) => k.id)).toEqual(['u1', 'u2', 's1'])
  })

  it('shows everything to an admin', () => {
    expect(visibleKeys(FLEET, { isAdmin: true })).toHaveLength(5)
  })

  it('treats a missing list as empty rather than throwing', () => {
    expect(visibleKeys(undefined, { isAdmin: true })).toEqual([])
    expect(visibleKeys(null)).toEqual([])
  })
})

describe('keyCounts', () => {
  it('counts within what the viewer can see, not the whole fleet', () => {
    expect(keyCounts(FLEET, { isAdmin: true })).toEqual({ total: 5, active: 3, revoked: 2 })
    expect(keyCounts(FLEET, { isAdmin: false })).toEqual({ total: 3, active: 2, revoked: 1 })
  })
})

describe('filterKeys', () => {
  it('hides revoked keys by default', () => {
    expect(filterKeys(FLEET, { isAdmin: true }).map((k) => k.id)).toEqual(['u1', 'a1', 's1'])
  })

  it('shows them behind the explicit toggle', () => {
    expect(filterKeys(FLEET, { isAdmin: true, showRevoked: true })).toHaveLength(5)
  })

  it('searches the fields a person knows a key by', () => {
    const byName = filterKeys(FLEET, { isAdmin: true, query: 'scout' })
    expect(byName.map((k) => k.id)).toEqual(['a1'])
    expect(filterKeys(FLEET, { isAdmin: true, query: 'TRINITY_MCP' })).toHaveLength(3)
    expect(filterKeys(FLEET, { isAdmin: true, query: 'system' }).map((k) => k.id)).toEqual(['s1'])
  })

  it('never matches on a secret-shaped field', () => {
    const withHash = [key({ id: 'h', name: 'x', key_hash: 'deadbeef' })]
    expect(filterKeys(withHash, { isAdmin: true, query: 'deadbeef' })).toEqual([])
  })

  it('keeps the access rule under every other filter', () => {
    // A non-admin searching an agent name still gets nothing.
    expect(filterKeys(FLEET, { isAdmin: false, showRevoked: true, query: 'scout' })).toEqual([])
  })

  it('ignores surrounding whitespace in the query', () => {
    expect(filterKeys(FLEET, { isAdmin: true, query: '  laptop  ' }).map((k) => k.id)).toEqual(['u1'])
  })
})

describe('pageOf', () => {
  const many = Array.from({ length: 306 }, (_, i) => key({ id: `k${i}` }))

  it('bounds the rendered rows so page weight does not scale with instance age', () => {
    const page = pageOf(many)
    expect(page.rows).toHaveLength(PAGE_SIZE)
    expect(page).toMatchObject({ shown: PAGE_SIZE, total: 306, hasMore: true })
  })

  it('reports honestly when everything fits', () => {
    const page = pageOf(many.slice(0, 4))
    expect(page).toMatchObject({ shown: 4, total: 4, hasMore: false })
  })

  it('grows when the panel asks for more', () => {
    expect(pageOf(many, { limit: 50 }).rows).toHaveLength(50)
    expect(pageOf(many, { limit: 1000 })).toMatchObject({ shown: 306, hasMore: false })
  })

  it('never renders zero rows because of a bad limit', () => {
    expect(pageOf(many, { limit: 0 }).rows).toHaveLength(1)
  })
})

describe('emptyReason — three empties, three next actions', () => {
  it('distinguishes a fresh instance from a filtered-out list', () => {
    expect(emptyReason({ total: 0, filtered: 0, query: '', showRevoked: false })).toBe('none')
    expect(emptyReason({ total: 306, filtered: 0, query: 'nope', showRevoked: true })).toBe('no-match')
    // The state that mattered: 294 revoked keys and nothing active. "No API
    // keys" would be a lie.
    expect(emptyReason({ total: 294, filtered: 0, query: '', showRevoked: false })).toBe('all-revoked')
  })

  it('returns null when there is something to show', () => {
    expect(emptyReason({ total: 5, filtered: 3, query: '', showRevoked: false })).toBeNull()
  })
})
