/**
 * trinity-enterprise#610 §3g S4 — a failed read write is rolled back, and only
 * the zero THIS call wrote is rolled back.
 *
 * `markRead` zeroes a chat's badge before the write (so the count does not
 * linger for a round trip). It never restored it: `store.markChatRead`
 * swallows every error, so a failed write left the chat looking read until
 * the next poll — and on the Inbox, gone from Unread for good if no poll came.
 * The identity guard is the point: a refresh that replaced the state, or a
 * second read that wrote its own zero, must not be clobbered by a stale
 * rollback.
 */
import { describe, it, expect } from 'vitest'
import { optimisticRead, rollbackRead } from '@/components/portal/portalUtils'

const K = 'thread:t1'

describe('optimisticRead', () => {
  it('zeroes an unread entry and returns the entry it wrote', () => {
    const before = { [K]: { kind: 'thread', id: 't1', unread: 3, starred: true } }
    const { state, written } = optimisticRead(before, K)
    expect(state[K]).toEqual({ kind: 'thread', id: 't1', unread: 0, starred: true })
    expect(written.entry).toBe(state[K])
    expect(written.before).toBe(before[K])
    expect(before[K].unread).toBe(3) // never mutated
  })
  it('writes nothing for an entry with nothing unread, or none at all', () => {
    const s = { [K]: { unread: 0 } }
    expect(optimisticRead(s, K)).toEqual({ state: s, written: null })
    expect(optimisticRead({}, K).written).toBeNull()
    expect(optimisticRead(null, K).written).toBeNull()
  })
})

describe('rollbackRead — the identity guard', () => {
  it('restores the count when the zero this call wrote is still there', () => {
    const { state, written } = optimisticRead({ [K]: { unread: 3 } }, K)
    expect(rollbackRead(state, K, written)[K].unread).toBe(3)
  })
  it('leaves a refreshed state alone (the server has spoken since)', () => {
    const { written } = optimisticRead({ [K]: { unread: 3 } }, K)
    const refreshed = { [K]: { unread: 0 } }
    expect(rollbackRead(refreshed, K, written)).toBe(refreshed)
  })
  it("leaves another call's zero alone", () => {
    const first = optimisticRead({ [K]: { unread: 3 } }, K)
    // a new arrival, then a second read writes its own zero
    const second = optimisticRead({ ...first.state, [K]: { unread: 1 } }, K)
    expect(rollbackRead(second.state, K, first.written)).toBe(second.state)
  })
  it('a null write is a no-op', () => {
    const s = { [K]: { unread: 0 } }
    expect(rollbackRead(s, K, null)).toBe(s)
  })
})
