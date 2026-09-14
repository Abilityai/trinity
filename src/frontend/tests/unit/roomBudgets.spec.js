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
import { budgetNotice, buildBudgetUpdate, isBudgetDirty, ROOM_COST_KEY } from '@/utils/roomBudgets'

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

// ---------------------------------------------------------------------------
// #2620 — the in-room notice
// ---------------------------------------------------------------------------

describe('#2620 budgetNotice', () => {
  const room = (over = {}) => ({ status: 'open', message_count: 0, max_messages: 200, ...over })

  it('says nothing while there is nothing worth saying', () => {
    // A permanent gauge is how a warning gets ignored.
    expect(budgetNotice(room({ message_count: 10 }))).toBeNull()
    expect(budgetNotice(room({ message_count: 159 }))).toBeNull()
  })

  it('warns from 80% and names what remains', () => {
    const n = budgetNotice(room({ message_count: 160 }))
    expect(n.level).toBe('warn')
    expect(n.remaining).toBe(40)
    expect(n.headline).toBe('40 messages left in this room')
  })

  it('states the CONSEQUENCE, not just the ratio', () => {
    // The whole point of the issue: "59/60" told the operator nothing.
    const n = budgetNotice(room({ message_count: 160 }))
    expect(n.detail).toContain('closes permanently')
  })

  it('explains that an agent reply spends a message too', () => {
    const n = budgetNotice(room({ message_count: 160 }))
    expect(n.detail.toLowerCase()).toContain('agent reply counts')
  })

  it('escalates to critical in the last few, and says what to do', () => {
    const n = budgetNotice(room({ message_count: 197 }))
    expect(n.level).toBe('critical')
    expect(n.remaining).toBe(3)
    expect(n.detail).toContain('Start a new chat')
  })

  it('is singular at one left', () => {
    expect(budgetNotice(room({ message_count: 199 })).headline).toBe('1 message left in this room')
  })

  it('at the limit says so without claiming a message is left', () => {
    const n = budgetNotice(room({ message_count: 200 }))
    expect(n.remaining).toBe(0)
    expect(n.headline).toContain('reached its message limit')
  })

  it('says nothing once the room is closed — the room itself says that', () => {
    expect(budgetNotice(room({ message_count: 200, status: 'closed' }))).toBeNull()
  })

  it('falls through to cost when a cost cap is the one being reached', () => {
    const n = budgetNotice(room({ message_count: 1, max_cost_usd: 10, cost: 9 }))
    expect(n.kind).toBe('cost')
    expect(n.headline).toContain('$9.00 of $10.00')
  })

  it('ignores an absent or zero budget rather than dividing by it', () => {
    expect(budgetNotice(room({ max_messages: 0, message_count: 50 }))).toBeNull()
    expect(budgetNotice(room({ max_messages: null, message_count: 50 }))).toBeNull()
  })

  it('survives junk', () => {
    expect(budgetNotice(null)).toBeNull()
    expect(budgetNotice({})).toBeNull()
  })
})
