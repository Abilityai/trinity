/**
 * #3439 — the pure half of the open thread's catch-up read.
 *
 * The conversation decides WHEN to re-read its thread from the row the shell's
 * 20 s tick hands it, and WHAT to append from the rows that read returns. Both
 * decisions are plain functions here, so the traps each reviewer named are
 * pinned without a mount: a re-stamped row (new object, same values) must not
 * read; the person's own just-sent question (a local row with no id) must not
 * render twice when the server's copy comes back; a read bounded by the
 * server's 50-row cap must say when it may have missed rows.
 */
import { describe, it, expect } from 'vitest'
import {
  tailSignature, tailLimit, adoptLocalIds, freshRows, tailOverflowed, TAIL_LIMIT_CAP,
} from '@/components/portal/portalThreadTail'

const row = (over = {}) => ({
  id: 's1', agent_name: 'scout', title: 'Plan', message_count: 4,
  last_message_at: '2026-10-26T10:00:00Z', unread: 0, ...over,
})

describe('tailSignature — the value the watcher compares', () => {
  it('is a string over message_count and last_message_at, so two re-stamped objects compare equal', () => {
    expect(tailSignature(row())).toBe(tailSignature({ ...row() }))
    expect(typeof tailSignature(row())).toBe('string')
  })
  it('changes when the thread gains a row (count, time) and ignores the fields the tick re-stamps', () => {
    const before = tailSignature(row())
    expect(tailSignature(row({ message_count: 5, last_message_at: '2026-10-26T10:01:00Z' }))).not.toBe(before)
    // A deliverable touch moves the time with `added=0` — that is a change too.
    expect(tailSignature(row({ last_message_at: '2026-10-26T10:01:00Z' }))).not.toBe(before)
    // `unread`, `starred`, `hasDraft`, `title` move on their own cadence.
    expect(tailSignature(row({ unread: 3, starred: true, hasDraft: true, title: 'Renamed' }))).toBe(before)
  })
  it('is null for no row — a chat the list does not carry yet has nothing to compare', () => {
    expect(tailSignature(null)).toBeNull()
    expect(tailSignature(undefined)).toBeNull()
  })
})

describe('tailLimit — how far back the catch-up read goes', () => {
  it('adds the unread count to the reply poll depth, so a burst is read whole', () => {
    expect(tailLimit(0, 8)).toBe(8)
    expect(tailLimit(3, 8)).toBe(11)
  })
  it(`never exceeds the server's cap (${TAIL_LIMIT_CAP}) — a larger value is a 422, not a longer read`, () => {
    expect(TAIL_LIMIT_CAP).toBe(50)
    expect(tailLimit(49, 8)).toBe(50)
    expect(tailLimit(500, 8)).toBe(50)
  })
  it('treats a missing or negative unread as zero', () => {
    expect(tailLimit(undefined, 8)).toBe(8)
    expect(tailLimit(-2, 8)).toBe(8)
    expect(tailLimit('3', 8)).toBe(11)
  })
})

describe('adoptLocalIds — the person\'s own question comes back with an id', () => {
  it('gives the server id and time to the local row with the same content, in place', () => {
    const local = { role: 'user', content: 'hello', local: true, at: null }
    const existing = [{ role: 'assistant', content: 'hi', id: 'a1' }, local]
    const fetched = [
      { id: 'a1', role: 'assistant', content: 'hi' },
      { id: 'u2', role: 'user', content: 'hello', created_at: '2026-10-26T10:00:00Z' },
    ]
    adoptLocalIds(existing, fetched)
    expect(local.id).toBe('u2')
    expect(local.at).toBe('2026-10-26T10:00:00Z')
    expect(existing[1]).toBe(local)   // mutated, not replaced — the bubble keeps its identity
  })
  it('matches each server row to ONE local row, newest local first, and leaves rows with ids alone', () => {
    const a = { role: 'user', content: 'same', local: true }
    const b = { role: 'user', content: 'same', local: true }
    const kept = { role: 'user', content: 'same', id: 'u0', local: true }
    const existing = [kept, a, b]
    adoptLocalIds(existing, [
      { id: 'u1', role: 'user', content: 'same' },
      { id: 'u2', role: 'user', content: 'same' },
    ])
    expect(kept.id).toBe('u0')
    expect([a.id, b.id].sort()).toEqual(['u1', 'u2'])
    expect(a.id).not.toBe(b.id)
  })
  it('never adopts onto a non-user row or from a non-user row', () => {
    const local = { role: 'user', content: 'x', local: true }
    const sys = { role: 'system', content: 'x' }
    adoptLocalIds([local, sys], [{ id: 's9', role: 'system', content: 'x' }])
    expect(local.id).toBeUndefined()
    expect(sys.id).toBeUndefined()
  })
})

describe('freshRows — what the read adds to the transcript', () => {
  it('returns the rows whose id is not already on screen, in server order', () => {
    const existing = [{ id: 'u1' }, { id: 'a1' }]
    const fetched = [{ id: 'u1' }, { id: 'a1' }, { id: 'a2' }, { id: 's3' }]
    expect(freshRows(existing, fetched).map((m) => m.id)).toEqual(['a2', 's3'])
  })
  it('skips rows without an id — nothing to dedupe them by on the next read', () => {
    expect(freshRows([], [{ role: 'assistant', content: 'no id' }, { id: 'a1' }]).map((m) => m.id)).toEqual(['a1'])
  })
  it('is empty when the read brought nothing new', () => {
    expect(freshRows([{ id: 'a1' }], [{ id: 'a1' }])).toEqual([])
    expect(freshRows([{ id: 'a1' }], [])).toEqual([])
    expect(freshRows([{ id: 'a1' }], null)).toEqual([])
  })
})

describe('tailOverflowed — did the bounded read reach back far enough?', () => {
  it('is true only when a full-length read is entirely unseen (there may be more behind it)', () => {
    const existing = [{ id: 'a0' }]
    const allNew = Array.from({ length: 50 }, (_, i) => ({ id: `n${i}` }))
    expect(tailOverflowed(existing, allNew, 50)).toBe(true)
    expect(tailOverflowed(existing, [{ id: 'a0' }, ...allNew.slice(0, 49)], 50)).toBe(false)
    expect(tailOverflowed(existing, allNew.slice(0, 10), 50)).toBe(false)   // shorter than the limit: that is the whole thread
    expect(tailOverflowed([], allNew, 50)).toBe(false)                         // an empty transcript has nothing to miss
  })
})
