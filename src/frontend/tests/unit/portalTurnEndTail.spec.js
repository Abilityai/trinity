/**
 * #4671 — the turn-end reads that used to happen twice, below the shell.
 *
 * `portalTurnEndRequests.mount.spec.js` pins the lists the shell refreshes.
 * Two more reads doubled on every turn end and live elsewhere:
 *
 *   • HISTORY. The reply poll finds the reply in the thread's newest rows;
 *     then the shell's list refresh moved the thread's row and the tail
 *     watcher read those same rows again. `tailHeld` is the rule that lets
 *     the watcher see it already holds them; the conversation's own half is
 *     mounted in `portalTurnEndHistory.mount.spec.js`.
 *   • WORK / CANVASES / FILES. Read directly when the turn ended, and again
 *     2 s later from the debounced push handler the direct read never
 *     cancelled.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('@/stores/clientPortal', () => {
  const portal = {
    fetchWork: vi.fn(async () => ({ now: [], earlier: [] })),
    fetchWorkActivity: vi.fn(async () => ({ items: {} })),
    fetchAgentCanvases: vi.fn(async () => []),
    fetchDocuments: vi.fn(async () => []),
    fetchUploads: vi.fn(async () => []),
  }
  return { useClientPortalStore: () => portal, __portal: portal }
})

import { __portal as portal } from '@/stores/clientPortal'
import { tailHeld } from '@/components/portal/portalThreadTail'
import { usePortalWorkStore } from '@/stores/portalWork'
import { usePortalRailFeedsStore } from '@/stores/portalRailFeeds'
import { joinFresh, resetInFlight } from '@/utils/inflight'

// ---------------------------------------------------------------------------
// History
// ---------------------------------------------------------------------------

const REPLY_AT = '2026-11-20T10:01:00.123456Z'
const transcript = (...ats) => ats.map((at, i) => ({ id: `m${i}`, role: i % 2 ? 'assistant' : 'user', at }))

describe('tailHeld — the transcript already holds what the row describes', () => {
  it('the row\'s newest instant IS the newest message on screen: nothing to fetch', () => {
    // After the person's own turn: `last_message_at` is the reply's own
    // `created_at` (one `now` writes both), and the reply is on screen.
    const row = { message_count: 4, last_message_at: REPLY_AT }
    expect(tailHeld(row, transcript('2026-11-20T10:00:00Z', REPLY_AT))).toBe(true)
  })

  it('the row is AHEAD of the transcript: something arrived on its own — fetch', () => {
    const row = { last_message_at: '2026-11-20T10:02:00Z' }
    expect(tailHeld(row, transcript('2026-11-20T10:00:00Z', REPLY_AT))).toBe(false)
  })

  it('a later row in the SAME millisecond is still ahead', () => {
    // `Date.parse` rounds both to .123 — compared as strings first, so a row
    // written 400 µs after the reply is not mistaken for the reply.
    const row = { last_message_at: '2026-11-20T10:01:00.123856Z' }
    expect(tailHeld(row, transcript(REPLY_AT))).toBe(false)
  })

  it('the row is behind the transcript (a list read from before the reply): held', () => {
    const row = { last_message_at: '2026-11-20T10:00:30Z' }
    expect(tailHeld(row, transcript('2026-11-20T10:00:00Z', REPLY_AT))).toBe(true)
  })

  it.each([
    ['no row', null, transcript(REPLY_AT)],
    ['a row with no instant', { last_message_at: null }, transcript(REPLY_AT)],
    ['an empty transcript', { last_message_at: REPLY_AT }, []],
    // The synchronous fallback's reply carries no time: the client cannot
    // name what it holds, so it must not claim to hold the row's newest.
    ['a newest message with no instant', { last_message_at: REPLY_AT }, [{ id: 'm', role: 'assistant', at: null }]],
    ['an unparseable row instant', { last_message_at: 'yesterday' }, transcript(REPLY_AT)],
  ])('any doubt answers false, and the read happens: %s', (_name, row, messages) => {
    expect(tailHeld(row, messages)).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Work, canvases, files
// ---------------------------------------------------------------------------

const PUSH = { type: 'agent_activity', agent_name: 'scout', activity_state: 'completed' }

describe('a direct rail refresh cancels the pending push-driven one', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.useFakeTimers()
    Object.values(portal).forEach((fn) => fn.mockClear())
  })
  afterEach(() => vi.useRealTimers())

  it('work: push, then the turn ends — ONE read, none 2 s later', async () => {
    const work = usePortalWorkStore()
    work.setScope(['scout'], 'sess-a')

    work.handleWebSocketEvent(PUSH)                 // the execution finished: debounce armed
    await work.refresh()                            // …the turn ended: the shell reads now
    expect(portal.fetchWork).toHaveBeenCalledTimes(1)

    await vi.advanceTimersByTimeAsync(5000)
    expect(portal.fetchWork).toHaveBeenCalledTimes(1)
  })

  it('work: a push AFTER the direct read is a new change and is still read', async () => {
    const work = usePortalWorkStore()
    work.setScope(['scout'], 'sess-a')

    await work.refresh()
    work.handleWebSocketEvent(PUSH)
    await vi.advanceTimersByTimeAsync(5000)

    expect(portal.fetchWork).toHaveBeenCalledTimes(2)
  })

  it('work: a burst of pushes is still one debounced read', async () => {
    const work = usePortalWorkStore()
    work.setScope(['scout'], 'sess-a')

    for (let i = 0; i < 5; i++) work.handleWebSocketEvent(PUSH)
    await vi.advanceTimersByTimeAsync(5000)

    expect(portal.fetchWork).toHaveBeenCalledTimes(1)
  })

  it('canvases and files: push, then the turn ends — one read of each', async () => {
    const feeds = usePortalRailFeedsStore()
    feeds.setParticipants(['scout'])
    feeds.setFeeds({ canvas: true, files: true })

    feeds.handleWebSocketEvent(PUSH)
    await feeds.refresh()
    await vi.advanceTimersByTimeAsync(5000)

    expect(portal.fetchAgentCanvases).toHaveBeenCalledTimes(1)
    expect(portal.fetchDocuments).toHaveBeenCalledTimes(1)
  })

  it('canvases and files: a push after the direct read is still read', async () => {
    const feeds = usePortalRailFeedsStore()
    feeds.setParticipants(['scout'])
    feeds.setFeeds({ canvas: true, files: true })

    await feeds.refresh()
    feeds.handleWebSocketEvent(PUSH)
    await vi.advanceTimersByTimeAsync(5000)

    expect(portal.fetchAgentCanvases).toHaveBeenCalledTimes(2)
    expect(portal.fetchDocuments).toHaveBeenCalledTimes(2)
  })
})

// ---------------------------------------------------------------------------
// A push is a change
// ---------------------------------------------------------------------------

/**
 * Is a read that left BEFORE `act` still handed to a caller that asks AFTER it?
 * The real `joinFresh`: `joined` is true when the later caller got the earlier
 * flight, and `value` is what the later caller was answered with.
 */
async function readAcross(act) {
  let release
  const before = joinFresh('probe', () => new Promise((resolve) => { release = () => resolve('before') }))
  await Promise.resolve()
  await act()
  const after = joinFresh('probe', async () => 'after')
  const joined = after === before
  release()
  return { joined, value: await after }
}

describe('a push makes every read that left before it stale', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    resetInFlight()
  })

  it('control: with nothing in between, the later caller joins the earlier read', async () => {
    expect(await readAcross(() => {})).toEqual({ joined: true, value: 'before' })
  })

  it('work: a participant\'s push — the read after it is a new one', async () => {
    const work = usePortalWorkStore()
    work.setScope(['scout'], 'sess-a')
    expect(await readAcross(() => work.handleWebSocketEvent(PUSH))).toEqual({ joined: false, value: 'after' })
    work.clear()
  })

  it('work: a push for an agent outside this chat changes nothing here', async () => {
    const work = usePortalWorkStore()
    work.setScope(['scout'], 'sess-a')
    const stranger = { ...PUSH, agent_name: 'someone-else' }
    expect(await readAcross(() => work.handleWebSocketEvent(stranger))).toEqual({ joined: true, value: 'before' })
    work.clear()
  })

  it('canvases and files: a participant\'s push — the read after it is a new one', async () => {
    const feeds = usePortalRailFeedsStore()
    feeds.setParticipants(['scout'])
    feeds.setFeeds({ canvas: true, files: true })
    expect(await readAcross(() => feeds.handleWebSocketEvent(PUSH))).toEqual({ joined: false, value: 'after' })
    feeds.clear?.()
  })

  it('canvases and files: a STARTED activity is not a change to them', async () => {
    const feeds = usePortalRailFeedsStore()
    feeds.setParticipants(['scout'])
    feeds.setFeeds({ canvas: true, files: true })
    const startedPush = { ...PUSH, activity_state: 'started' }
    expect(await readAcross(() => feeds.handleWebSocketEvent(startedPush))).toEqual({ joined: true, value: 'before' })
    feeds.clear?.()
  })
})
