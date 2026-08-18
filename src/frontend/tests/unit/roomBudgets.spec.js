/**
 * The room-budget PUT body (ent#387).
 *
 * The endpoint takes a PARTIAL update, so an omitted field means "leave it
 * alone". That makes the empty cost box the interesting case: read naively it
 * omits the field, and the previous cap silently survives a change the operator
 * believes they made. Clearing therefore has to be said out loud, and only when
 * there is something to clear.
 */
import { describe, it, expect } from 'vitest'
import { buildBudgetUpdate, isBudgetDirty, ROOM_COST_KEY } from '@/utils/roomBudgets'

const server = { max_messages: 60, max_cost_usd: 5, ttl_hours: 24 }

describe('buildBudgetUpdate', () => {
  it('sends nothing when nothing changed', () => {
    const body = buildBudgetUpdate({ max_messages: 60, max_cost_usd: 5, ttl_hours: 24 }, server)
    expect(body).toEqual({ clear: [] })
    expect(isBudgetDirty({ max_messages: 60, max_cost_usd: 5, ttl_hours: 24 }, server)).toBe(false)
  })

  it('sends only the fields that changed', () => {
    const body = buildBudgetUpdate({ max_messages: 30, max_cost_usd: 5, ttl_hours: 24 }, server)
    expect(body).toEqual({ clear: [], max_messages: 30 })
  })

  it('clears the cost cap when the box is emptied', () => {
    const body = buildBudgetUpdate({ max_messages: 60, max_cost_usd: '', ttl_hours: 24 }, server)
    expect(body.clear).toEqual([ROOM_COST_KEY])
    expect(body).not.toHaveProperty('max_cost_usd')
  })

  it('does not ask to clear a cap that is already absent', () => {
    const uncapped = { ...server, max_cost_usd: null }
    const body = buildBudgetUpdate({ max_messages: 60, max_cost_usd: '', ttl_hours: 24 }, uncapped)
    expect(body).toEqual({ clear: [] })
    expect(isBudgetDirty({ max_messages: 60, max_cost_usd: '', ttl_hours: 24 }, uncapped)).toBe(false)
  })

  it('treats whitespace as empty', () => {
    const body = buildBudgetUpdate({ max_messages: 60, max_cost_usd: '   ', ttl_hours: 24 }, server)
    expect(body.clear).toEqual([ROOM_COST_KEY])
  })

  it('sends ttl 0 rather than omitting it — 0 means "no expiry"', () => {
    const body = buildBudgetUpdate({ max_messages: 60, max_cost_usd: 5, ttl_hours: 0 }, server)
    expect(body.ttl_hours).toBe(0)
  })

  it('is dirty when only the cost is cleared', () => {
    expect(isBudgetDirty({ max_messages: 60, max_cost_usd: '', ttl_hours: 24 }, server)).toBe(true)
  })

  it('returns an inert body with no server state yet', () => {
    expect(buildBudgetUpdate({ max_messages: 1 }, null)).toEqual({ clear: [] })
    expect(isBudgetDirty({ max_messages: 1 }, null)).toBe(false)
  })
})
