/**
 * trinity-enterprise#610 PR A2, §3g L6 C1 — the Action tab's order is urgency,
 * not recency (Andrii 2026-09-29: a 24h bucket plus the A10 badge).
 *
 * The round-1 seed had critical asks sitting at #10 and #20 of 21 because the
 * tab sorted newest-first. The order now:
 *   1. asks that expire within 24h, soonest first (the A10 "Expires in …" badge
 *      is the reason shown on the row);
 *   2. then priority — critical > high > medium > low, an unknown one as medium;
 *   3. then the OLDEST first: the ask that has waited longest is next.
 */
import { describe, it, expect } from 'vitest'
import { actionItems, askUrgencyCompare, PRIORITY_RANK } from '@/components/portal/portalInbox'

const NOW = Date.parse('2026-09-29T12:00:00Z')
const H = 3600 * 1000
const at = (msFromNow) => new Date(NOW + msFromNow).toISOString()
const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', title: id, question: 'q?',
  priority: 'medium', created_at: at(-2 * H), expires_at: null, status: 'pending', ...over,
})

describe('C1 — Action is ordered by urgency', () => {
  it('expiring within 24h, then priority, then the oldest', () => {
    const rows = actionItems([
      ask('medium-new', { created_at: at(-1 * H) }),
      ask('high', { priority: 'high', created_at: at(-1 * H) }),
      ask('medium-old', { created_at: at(-9 * H) }),
      ask('critical', { priority: 'critical', created_at: at(-1 * H) }),
      ask('low-1h', { priority: 'low', expires_at: at(1 * H), created_at: at(-1 * H) }),
    ], NOW)
    expect(rows.map((r) => r.id)).toEqual(['low-1h', 'critical', 'high', 'medium-old', 'medium-new'])
  })

  it('inside the bucket the soonest expiry leads; past 24h an expiry does not jump the queue', () => {
    const rows = actionItems([
      ask('in-20h', { expires_at: at(20 * H) }),
      ask('in-30h', { priority: 'critical', expires_at: at(30 * H) }),
      ask('in-2h', { priority: 'low', expires_at: at(2 * H) }),
    ], NOW)
    expect(rows.map((r) => r.id)).toEqual(['in-2h', 'in-20h', 'in-30h'])
  })

  it('an unknown or missing priority ranks as medium', () => {
    expect(PRIORITY_RANK.critical).toBeLessThan(PRIORITY_RANK.high)
    const rows = actionItems([
      ask('low', { priority: 'low', created_at: at(-9 * H) }),
      ask('weird', { priority: 'urgent-ish', created_at: at(-1 * H) }),
      ask('none', { priority: undefined, created_at: at(-2 * H) }),
    ], NOW)
    expect(rows.map((r) => r.id)).toEqual(['none', 'weird', 'low'])
  })

  it('is a total order: ties break on id, so a poll never swaps two rows', () => {
    const a = ask('a'); const b = ask('b')
    expect(askUrgencyCompare(a, b, NOW)).toBeLessThan(0)
    expect(askUrgencyCompare(b, a, NOW)).toBeGreaterThan(0)
  })
})
