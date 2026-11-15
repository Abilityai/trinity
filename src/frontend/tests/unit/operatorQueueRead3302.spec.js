/**
 * #3302 — the operator queue is read so a count is never a window.
 *
 * `GET /api/operator-queue` is complete within `limit` and says how much more
 * there is (`total`, `has_more`, `next_cursor`; trinity-enterprise#815). Every
 * surface read one capped page and derived counts and lists from it, so past
 * the cap the numbers stopped being true and nothing said so — and because the
 * route lists pending rows FIRST, 200 pending asks pushed every ended ask out of
 * the desktop window (the Resolved tab read empty).
 *
 * These specs drive `utils/operatorQueueRead.js` against an in-memory route
 * (`helpers/fakeOperatorQueue.js`) that orders, pages and walks the way the real
 * one does, including what it does to a reader between two reads.
 */
import { describe, it, expect } from 'vitest'
import {
  QUEUE_PAGE_MAX,
  readPendingQueue,
  readQueueSnapshot,
  pendingCountOf,
  pendingResponsesPhrase,
  resolvedPhrase,
  pendingPartialLine,
} from '../../src/utils/operatorQueueRead'
import { makeQueue, rowsOf } from './helpers/fakeOperatorQueue'

const ids = (rows) => rows.map((r) => r.id)
const unique = (rows) => new Set(ids(rows)).size === rows.length

// ------------------------------------------------------------ readPendingQueue

describe('readPendingQueue — every pending ask, or a read that says it is partial', () => {
  it('a queue that fits one page costs one offset-mode request (no walk, no Redis)', async () => {
    const q = makeQueue([...rowsOf(7), ...rowsOf(4, { prefix: 'e', status: 'responded' })])
    const read = await readPendingQueue(q.get, { pageLimit: 10 })
    expect(q.calls).toEqual([{ status: 'pending', limit: 10 }])
    expect(read.complete).toBe(true)
    expect(read.items).toHaveLength(7)
    expect(read.total).toBe(7)
  })

  it('defaults to the route ceiling (500) per page', async () => {
    const q = makeQueue(rowsOf(2))
    await readPendingQueue(q.get)
    expect(q.calls[0]).toEqual({ status: 'pending', limit: QUEUE_PAGE_MAX })
    expect(QUEUE_PAGE_MAX).toBe(500)
  })

  it('past one page it walks by cursor until has_more is false — every row once, in route order', async () => {
    const q = makeQueue(rowsOf(11, { agents: 3 }))
    const read = await readPendingQueue(q.get, { pageLimit: 4 })
    expect(read.complete).toBe(true)
    expect(read.items).toHaveLength(11)
    expect(unique(read.items)).toBe(true)
    expect(read.total).toBe(11)
    // page 1 in offset mode, then a walk from `start`, then its cursors
    expect(q.calls[0]).toEqual({ status: 'pending', limit: 4 })
    expect(q.calls[1]).toEqual({ status: 'pending', limit: 4, cursor: 'start' })
    expect(q.calls.slice(2).every((c) => c.cursor && c.cursor !== 'start')).toBe(true)
  })

  it('passes the caller\'s filters through on every page', async () => {
    const q = makeQueue([...rowsOf(6, { agents: 1 }), ...rowsOf(5, { prefix: 'x', agent_name: 'agent-other' })])
    const read = await readPendingQueue(q.get, { params: { agent_name: 'agent-1' }, pageLimit: 2 })
    expect(read.items.every((r) => r.agent_name === 'agent-1')).toBe(true)
    expect(read.items).toHaveLength(6)
    expect(q.calls.every((c) => c.agent_name === 'agent-1' && c.status === 'pending')).toBe(true)
  })

  it('the walk REPLACES page 1: a row answered between the two reads is not reported pending', async () => {
    const q = makeQueue(rowsOf(9), {
      between: (n, rows) => { if (n === 1) rows.find((r) => r.id === 'p-0009').status = 'responded' },
    })
    const read = await readPendingQueue(q.get, { pageLimit: 4 })
    expect(ids(read.items)).not.toContain('p-0009')
    expect(read.items).toHaveLength(8)
    expect(read.complete).toBe(true)
  })

  it('stops at maxPages and says the read is partial, with the route total as the count', async () => {
    const q = makeQueue(rowsOf(30))
    const read = await readPendingQueue(q.get, { pageLimit: 4, maxPages: 3 })
    expect(read.complete).toBe(false)
    expect(read.items).toHaveLength(12)
    expect(read.total).toBe(30)
  })

  it('a page with more but no cursor (an uncarriable key) ends the walk as partial, never silently complete', async () => {
    const q = makeQueue(rowsOf(10), { uncarriable: true })
    const read = await readPendingQueue(q.get, { pageLimit: 4 })
    expect(read.complete).toBe(false)
    expect(read.total).toBe(10)
    expect(read.items).toHaveLength(4)
  })

  it.each([
    ['503 when the walk starts (no Redis)', { status: 503, on: 'start' }],
    ['410 on a later page (walk expired)', { status: 410, on: 'next' }],
    ['503 on a later page (Redis failed mid-walk)', { status: 503, on: 'next' }],
  ])('a walk that fails — %s — returns page 1 as a stated partial instead of failing', async (_name, walkError) => {
    const q = makeQueue(rowsOf(10), { walkError })
    const read = await readPendingQueue(q.get, { pageLimit: 4 })
    expect(read.complete).toBe(false)
    expect(ids(read.items)).toEqual(ids(rowsOf(10)).reverse().slice(0, 4)) // newest filed first
    expect(read.total).toBe(10)
  })

  it('a failed FIRST page rejects — the caller keeps what it had and says the refresh failed', async () => {
    const q = makeQueue(rowsOf(3), { fail: (_p, n) => n === 1 })
    await expect(readPendingQueue(q.get)).rejects.toThrow()
  })

  it('an unverified total (null) stays null on a partial read — never shown as 0', async () => {
    const q = makeQueue(rowsOf(10), { totalNull: true, uncarriable: true })
    const read = await readPendingQueue(q.get, { pageLimit: 4 })
    expect(read.complete).toBe(false)
    expect(read.total).toBeNull()
  })

  it('a complete read is its own count even when the route could not verify `total`', async () => {
    const q = makeQueue(rowsOf(3), { totalNull: true })
    const read = await readPendingQueue(q.get)
    expect(read.complete).toBe(true)
    expect(read.total).toBe(3)
  })
})

// ----------------------------------------------------------- readQueueSnapshot

describe('readQueueSnapshot — complete pending, the newest ended window, and how many ended in all', () => {
  const opts = { pageLimit: 10, endedLimit: 10, walkPageLimit: 4 }

  it('A: a queue that fits page 1 is ONE request — exactly today\'s read', async () => {
    const q = makeQueue([...rowsOf(3), ...rowsOf(4, { prefix: 'e', status: 'cancelled' })], { undelivered: 2, closedByFiler: 1 })
    const snap = await readQueueSnapshot(q.get, opts)
    expect(q.calls).toEqual([{ limit: 10 }])
    expect(snap.pending).toHaveLength(3)
    expect(snap.pendingTotal).toBe(3)
    expect(snap.pendingComplete).toBe(true)
    expect(snap.ended).toHaveLength(4)
    expect(snap.endedTotal).toBe(4)
    expect(snap.flags).toEqual({ undelivered: 2, closedByFiler: 1 })
  })

  it('B: page 1 reached an ended row, so every pending ask is in it; the ended window is topped up to its size', async () => {
    const q = makeQueue([...rowsOf(4), ...rowsOf(25, { prefix: 'e', status: 'responded' })])
    const snap = await readQueueSnapshot(q.get, opts)
    expect(snap.pending).toHaveLength(4)
    expect(snap.pendingComplete).toBe(true)
    expect(snap.pendingTotal).toBe(4)
    // page 1 held 6 ended rows; the top-up reads the 4 that follow it
    expect(q.calls).toEqual([{ limit: 10 }, { limit: 4, offset: 10 }])
    expect(snap.ended).toHaveLength(10)
    expect(ids(snap.ended)).toEqual(ids(rowsOf(25, { prefix: 'e', status: 'responded' })).reverse().slice(0, 10))
    expect(snap.endedTotal).toBe(25)
  })

  it('B: a full ended window needs no top-up', async () => {
    const q = makeQueue(rowsOf(30, { prefix: 'e', status: 'expired' }))
    const snap = await readQueueSnapshot(q.get, opts)
    expect(q.calls).toHaveLength(1)
    expect(snap.ended).toHaveLength(10)
    expect(snap.endedTotal).toBe(30)
    expect(snap.pending).toEqual([])
  })

  it('B: a row raised between page 1 and the top-up shifts the offsets — the window carries no duplicate', async () => {
    const q = makeQueue([...rowsOf(4), ...rowsOf(25, { prefix: 'e', status: 'responded' })], {
      between: (n, rows) => { if (n === 1) rows.push(rowsOf(1, { prefix: 'late', at: '2026-12-01T00:00:00Z' })[0]) },
    })
    const snap = await readQueueSnapshot(q.get, opts)
    expect(unique(snap.ended)).toBe(true)
    expect(unique([...snap.pending, ...snap.ended])).toBe(true)
    expect(snap.ended.every((r) => r.status !== 'pending')).toBe(true)
  })

  it('C: page 1 is all pending — walk the pending asks, then read the ended ones after them', async () => {
    const pending = rowsOf(23, { agents: 4 })
    const ended = rowsOf(15, { prefix: 'e', status: 'cancelled' })
    const q = makeQueue([...pending, ...ended])
    const snap = await readQueueSnapshot(q.get, opts)
    expect(snap.pendingComplete).toBe(true)
    expect(snap.pending).toHaveLength(23)
    expect(unique(snap.pending)).toBe(true)
    expect(snap.pendingTotal).toBe(23)
    expect(snap.ended).toHaveLength(10)
    expect(ids(snap.ended)).toEqual(ids(ended).reverse().slice(0, 10))
    expect(snap.endedTotal).toBe(15)
    // the ended read never carries `status` (one status filter cannot say "ended") and never a null offset
    const endedRead = q.calls[q.calls.length - 1]
    expect(endedRead.status).toBeUndefined()
    expect(Number.isInteger(endedRead.offset)).toBe(true)
  })

  it('C: an ask answered between the pending walk and the ended read is reported ended, not pending (the later read wins)', async () => {
    // p-0023 is the newest pending ask, so the walk collects it on its first
    // page; it is answered after the walk's LAST page (23 rows / 4 per page = 6),
    // just before the ended read.
    let answered = false
    const q = makeQueue([...rowsOf(23), ...rowsOf(15, { prefix: 'e', status: 'cancelled' })], {
      between: (_n, rows) => {
        if (answered || q.calls.filter((c) => c.cursor).length < 6) return
        answered = true
        Object.assign(rows.find((r) => r.id === 'p-0023'), { status: 'responded', disposed_at: '2026-12-01T00:00:00Z' })
      },
    })
    const snap = await readQueueSnapshot(q.get, opts)
    expect(ids(snap.pending)).not.toContain('p-0023')
    expect(ids(snap.ended)[0]).toBe('p-0023')
    expect(unique([...snap.pending, ...snap.ended])).toBe(true)
  })

  it('C: a partial walk with an unverified total still sends a real offset, and the ended total stays unknown', async () => {
    const q = makeQueue([...rowsOf(23), ...rowsOf(6, { prefix: 'e', status: 'expired' })], { totalNull: true, uncarriable: true })
    const snap = await readQueueSnapshot(q.get, opts)
    expect(snap.pendingComplete).toBe(false)
    expect(snap.pendingTotal).toBeNull()
    const endedRead = q.calls[q.calls.length - 1]
    expect(Number.isInteger(endedRead.offset)).toBe(true)
    expect(snap.endedTotal).toBeNull()
    expect(snap.ended.every((r) => r.status !== 'pending')).toBe(true)
  })

  it('C: a partial walk with an unverified total asks the one-row count where the pending section ends — the Resolved window is not falsely empty', async () => {
    const q = makeQueue([...rowsOf(23), ...rowsOf(6, { prefix: 'e', status: 'expired' })], {
      uncarriable: true,
      totalNullWhen: (params) => params.limit !== 1, // the belt fired on the walk's pages, not on a one-row read
    })
    const snap = await readQueueSnapshot(q.get, opts)
    expect(snap.pendingComplete).toBe(false)
    expect(q.calls).toContainEqual({ status: 'pending', limit: 1 })
    expect(snap.pendingTotal).toBe(23)
    expect(snap.ended).toHaveLength(6)
  })

  it('an unverified page-1 total never becomes a negative or invented ended total', async () => {
    const q = makeQueue([...rowsOf(4), ...rowsOf(25, { prefix: 'e', status: 'responded' })], { totalNull: true })
    const snap = await readQueueSnapshot(q.get, opts)
    expect(snap.endedTotal).toBeNull()
    expect(snap.pendingTotal).toBe(4) // page 1 still holds every pending ask
  })

  it('A with an unverified total: everything is on the page, so the ended count is the rows themselves', async () => {
    const q = makeQueue([...rowsOf(2), ...rowsOf(3, { prefix: 'e', status: 'acknowledged' })], { totalNull: true })
    const snap = await readQueueSnapshot(q.get, opts)
    expect(snap.endedTotal).toBe(3)
  })

  it('a failed top-up fails the whole read (one failure path for the caller)', async () => {
    const q = makeQueue([...rowsOf(4), ...rowsOf(25, { prefix: 'e', status: 'responded' })], { fail: (_p, n) => n === 2 })
    await expect(readQueueSnapshot(q.get, opts)).rejects.toThrow()
  })
})

// ------------------------------------------------------------------- counts/copy

describe('counts and copy', () => {
  it('pendingCountOf reads a verified total and nothing else', () => {
    expect(pendingCountOf({ total: 7, count: 1 })).toBe(7)
    expect(pendingCountOf({ total: 0 })).toBe(0)
    expect(pendingCountOf({ total: null, count: 1 })).toBeNull()
    expect(pendingCountOf({ count: 1 })).toBeNull()
    expect(pendingCountOf(null)).toBeNull()
  })

  it('pendingResponsesPhrase: complete reads as today; a partial read says how many are shown', () => {
    expect(pendingResponsesPhrase({ count: 1, shown: 1 })).toBe('1 pending response')
    expect(pendingResponsesPhrase({ count: 3, shown: 3 })).toBe('3 pending responses')
    expect(pendingResponsesPhrase({ count: 812, shown: 500, complete: false })).toBe('812 pending responses (500 shown)')
    expect(pendingResponsesPhrase({ count: 500, shown: 500, complete: false, exact: false })).toBe('500+ pending responses (500 shown)')
  })

  it('resolvedPhrase: the principle-28 line, and nothing to state when nothing resolved', () => {
    expect(resolvedPhrase({ total: 340, shown: 200 })).toBe('340 resolved · latest 200 shown')
    expect(resolvedPhrase({ total: 12, shown: 12 })).toBe('12 resolved')
    // an optimistic answer adds a row before the refetch: never "12 resolved · latest 13 shown"
    expect(resolvedPhrase({ total: 12, shown: 13 })).toBe('13 resolved')
    expect(resolvedPhrase({ total: null, shown: 200 })).toBe('Latest 200 resolved')
    expect(resolvedPhrase({ total: 0, shown: 0 })).toBeNull()
    expect(resolvedPhrase({ total: null, shown: 0 })).toBeNull()
  })

  it('pendingPartialLine: the phone states what it is showing', () => {
    expect(pendingPartialLine({ shown: 100, total: 640 })).toBe('Showing 100 of 640 pending')
    expect(pendingPartialLine({ shown: 100, total: null })).toBe('Showing the first 100 pending — the total could not be read')
  })
})
