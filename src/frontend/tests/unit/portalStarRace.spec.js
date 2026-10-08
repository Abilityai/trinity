/**
 * abilityai/trinity#3356 — a star click must survive a chat-state read that was
 * already in flight. The bug: the read left before the PUT landed, answered
 * with the pre-click state, and `refreshThreads` overwrote the optimistic star —
 * so the first click looked lost and the second one "worked".
 */
import { describe, it, expect } from 'vitest'
import { createWriteGuard, applyStarOverrides } from '@/components/portal/portalUtils'
import { inSidebar, sidebarThreadsOf } from '@/components/portal/portalInbox'

const K = 'thread:s1'

describe('#3356 createWriteGuard + applyStarOverrides', () => {
  it('the regression: a read issued BEFORE the click cannot unstar it', () => {
    const g = createWriteGuard()
    const token = g.readStarted()          // poll leaves: server says "not starred"
    const w = g.begin(K, true)             // first click
    g.settle(K, w, true)                   // PUT lands
    const stale = {}                       // …and the stale answer arrives
    expect(applyStarOverrides(stale, g.live(token))[K].starred).toBe(true)
  })

  it('holds the star while the write is still in flight', () => {
    const g = createWriteGuard()
    g.begin(K, true)
    const token = g.readStarted()          // a read issued mid-write is not evidence either
    expect(applyStarOverrides({}, g.live(token))[K]).toMatchObject({ kind: 'thread', id: 's1', starred: true })
  })

  it('retires the override once a read issued AFTER the settle returns', () => {
    const g = createWriteGuard()
    const w = g.begin(K, true)
    g.settle(K, w, true)
    const token = g.readStarted()
    const fresh = { [K]: { kind: 'thread', id: 's1', starred: true } }
    expect(applyStarOverrides(fresh, g.live(token))).toBe(fresh)
    expect(g.size).toBe(0)
    // From here the server is the truth — an unstar elsewhere shows.
    expect(applyStarOverrides({}, g.live(g.readStarted()))).toEqual({})
  })

  it('a failed write drops its override (the caller reverts the screen)', () => {
    const g = createWriteGuard()
    const w = g.begin(K, true)
    expect(g.settle(K, w, false)).toBe(true)
    expect(g.size).toBe(0)
  })

  it('a superseded write (double click) cannot settle or fail the newer one', () => {
    const g = createWriteGuard()
    const first = g.begin(K, true)
    const second = g.begin(K, false)
    expect(g.settle(K, first, false)).toBe(false)
    const token = g.readStarted()
    expect(applyStarOverrides({ [K]: { starred: true } }, g.live(token))[K].starred).toBe(false)
    expect(g.settle(K, second, true)).toBe(true)
  })

  it('keys may themselves contain ":"', () => {
    const g = createWriteGuard()
    g.begin('room:a:b', true)
    expect(applyStarOverrides({}, g.live(g.readStarted()))['room:a:b']).toMatchObject({ kind: 'room', id: 'a:b' })
  })
})

describe('#3356 a starred unused Main is listed', () => {
  it('keeps the unused Main once it is starred, and only then', () => {
    expect(inSidebar({ is_main: true, last_message_at: null })).toBe(false)
    expect(inSidebar({ is_main: true, last_message_at: null, starred: true })).toBe(true)
    expect(sidebarThreadsOf([{ id: 'm', is_main: true, starred: true }]).map((t) => t.id)).toEqual(['m'])
  })
})
