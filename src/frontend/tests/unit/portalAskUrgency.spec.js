/**
 * trinity-enterprise#610 §3g A10 — the ask urgency helpers, one module for the
 * row anatomy (A10), the Action sort (C1, L6) and the ask context line (E1, L7).
 *
 * A row said "Waiting on you" on every Action item — true of all of them, so it
 * told the reader nothing — while a Critical ask sat at #10 of 21 looking like
 * the rest. The row now says what differs: a priority only when it is high or
 * critical, and how soon it expires only when that is within a day.
 */
import { describe, it, expect } from 'vitest'
import { priorityBadge, expiresSoonLabel, needsExpiryTick, askKindIcon } from '@/components/portal/portalAskUrgency'

const NOW = Date.parse('2026-09-29T12:00:00Z')
const inMin = (m) => new Date(NOW + m * 60_000).toISOString()

describe('priorityBadge', () => {
  it('critical is a danger badge, high an urgent one; medium, low and junk say nothing (T9)', () => {
    expect(priorityBadge('critical')).toEqual({ variant: 'danger', label: 'Critical' })
    expect(priorityBadge('high')).toEqual({ variant: 'urgent', label: 'High' })
    for (const p of ['medium', 'low', null, undefined, 'nope', 3]) expect(priorityBadge(p)).toBeNull()
  })
})

describe('expiresSoonLabel', () => {
  it('under an hour: a warning, in minutes', () => {
    expect(expiresSoonLabel(inMin(18), NOW)).toEqual({ label: 'Expires in 18m', variant: 'warning' })
    expect(expiresSoonLabel(inMin(0.4), NOW)).toEqual({ label: 'Expires in 1m', variant: 'warning' })
  })
  it('one to 24 hours: neutral, in hours — the reason for the Action order stays visible', () => {
    expect(expiresSoonLabel(inMin(60), NOW)).toEqual({ label: 'Expires in 1h', variant: 'neutral' })
    expect(expiresSoonLabel(inMin(5 * 60 + 20), NOW)).toEqual({ label: 'Expires in 5h', variant: 'neutral' })
    expect(expiresSoonLabel(inMin(24 * 60), NOW)).toEqual({ label: 'Expires in 24h', variant: 'neutral' })
  })
  it('nothing past a day, in the past, or unreadable', () => {
    for (const v of [inMin(24 * 60 + 1), inMin(-5), null, '', 'garbage']) expect(expiresSoonLabel(v, NOW)).toBeNull()
  })
})

describe('needsExpiryTick — the 30 s clock runs only while it has something to move', () => {
  it('true only for a PENDING ask expiring within a day', () => {
    expect(needsExpiryTick([{ status: 'pending', expires_at: inMin(90) }], NOW)).toBe(true)
    expect(needsExpiryTick([{ status: 'answered', expires_at: inMin(90) }], NOW)).toBe(false)
    expect(needsExpiryTick([{ status: 'pending', expires_at: inMin(3 * 24 * 60) }], NOW)).toBe(false)
    expect(needsExpiryTick([{ status: 'pending', expires_at: null }], NOW)).toBe(false)
    expect(needsExpiryTick(null, NOW)).toBe(false)
  })
})

describe('askKindIcon — identity by shape, not hue (principle 24)', () => {
  it('three distinct shapes, and an unknown kind gets the alert one', () => {
    const a = askKindIcon('approval'); const q = askKindIcon('question'); const n = askKindIcon('notification')
    expect(new Set([a.path, q.path, n.path]).size).toBe(3)
    expect(a.name).toBe('shield-check')
    expect(q.name).toBe('question-mark-circle')
    expect(n.name).toBe('bell')
    expect(askKindIcon(undefined).name).toBe('bell')
  })
})
