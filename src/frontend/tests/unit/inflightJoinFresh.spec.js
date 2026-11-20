/**
 * #4671 — `joinFresh`: a join that never hands back a stale answer.
 *
 * One Workspace turn end sent 17–21 requests, several of them the same read
 * twice. A plain in-flight join (`dedupe`) would fix the count and break the
 * page: the refresh that follows a mark-read, or a turn ending, exists to show
 * that change, and joining a read that left BEFORE it resolves the refresh
 * with the old list.
 *
 * So the count is the easy half here. The assertions that matter are the ones
 * about WHEN a join is refused.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { joinFresh, markChanged, resetInFlight, inFlightCount, MAX_FLIGHT_AGE_MS } from '@/utils/inflight'

function deferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

/** A read whose every call is recorded and resolved by hand. */
function reader() {
  const calls = []
  const fn = () => {
    const d = deferred()
    calls.push(d)
    return d.promise
  }
  return { fn, calls }
}

const tick = () => new Promise((r) => setTimeout(r, 0))

beforeEach(() => resetInFlight())

describe('joinFresh — joining', () => {
  it('two reads with no change between them are ONE request, and both get its answer', async () => {
    const { fn, calls } = reader()
    const a = joinFresh('sessions', fn)
    const b = joinFresh('sessions', fn)
    await tick()
    expect(calls).toHaveLength(1)
    calls[0].resolve('list')
    expect(await a).toBe('list')
    expect(await b).toBe('list')
  })

  it('is a join, not a cache: a read after the first settled is a new request', async () => {
    const { fn, calls } = reader()
    const a = joinFresh('sessions', fn)
    await tick()
    calls[0].resolve(1)
    await a
    const b = joinFresh('sessions', fn)
    await tick()
    expect(calls).toHaveLength(2)
    calls[1].resolve(2)
    expect(await b).toBe(2)
    expect(inFlightCount()).toBe(0)
  })

  it('different keys never join', async () => {
    const { fn, calls } = reader()
    joinFresh('asks|{}', fn)
    joinFresh('asks|{"chat_id":"c1"}', fn)
    await tick()
    expect(calls).toHaveLength(2)
  })

  it('a rejection reaches every joiner and poisons nothing', async () => {
    const { fn, calls } = reader()
    const a = joinFresh('rooms', fn)
    const b = joinFresh('rooms', fn)
    await tick()
    calls[0].reject(new Error('503'))
    await expect(a).rejects.toThrow('503')
    await expect(b).rejects.toThrow('503')
    const c = joinFresh('rooms', fn)
    await tick()
    expect(calls).toHaveLength(2)
    calls[1].resolve('ok')
    expect(await c).toBe('ok')
  })

  it('a synchronous throw is a rejection, not an exception at the call site', async () => {
    const boom = () => { throw new Error('sync') }
    await expect(joinFresh('k', boom)).rejects.toThrow('sync')
    expect(inFlightCount()).toBe(0)
  })
})

describe('joinFresh — never a stale answer', () => {
  it('a read issued AFTER a change does not join one that left before it', async () => {
    // The mark-read case: the 20 s tick's read is out, the mark-read lands,
    // and the refresh behind it must not be answered with the pre-read list.
    const { fn, calls } = reader()
    const before = joinFresh('chat-state', fn)
    await tick()
    markChanged()
    const after = joinFresh('chat-state', fn)
    await tick()
    expect(calls).toHaveLength(1)         // not raced: it waits for the older read…
    calls[0].resolve('unread: 3')
    expect(await before).toBe('unread: 3')
    await tick()
    expect(calls).toHaveLength(2)         // …then reads again
    calls[1].resolve('unread: 0')
    expect(await after).toBe('unread: 0')
  })

  it('every caller waiting behind the same change shares ONE re-read', async () => {
    const { fn, calls } = reader()
    joinFresh('sessions', fn)
    await tick()
    markChanged()
    const a = joinFresh('sessions', fn)
    const b = joinFresh('sessions', fn)
    const c = joinFresh('sessions', fn)
    calls[0].resolve('old')
    await tick(); await tick()
    expect(calls).toHaveLength(2)
    calls[1].resolve('new')
    expect(await Promise.all([a, b, c])).toEqual(['new', 'new', 'new'])
  })

  it('the answers arrive in order: the older read can never land last', async () => {
    const { fn, calls } = reader()
    const seen = []
    joinFresh('asks', fn).then((v) => seen.push(v))
    await tick()
    markChanged()
    joinFresh('asks', fn).then((v) => seen.push(v))
    await tick()
    // Only one request is out, so there is nothing for it to race.
    expect(calls).toHaveLength(1)
    calls[0].resolve('older')
    await tick(); await tick()
    calls[1].resolve('newer')
    await tick()
    expect(seen).toEqual(['older', 'newer'])
  })

  it('a re-read is issued even when the older read FAILS', async () => {
    const { fn, calls } = reader()
    const before = joinFresh('sessions', fn)
    await tick()
    markChanged()
    const after = joinFresh('sessions', fn)
    calls[0].reject(new Error('down'))
    await expect(before).rejects.toThrow('down')
    await tick(); await tick()
    expect(calls).toHaveLength(2)
    calls[1].resolve('up')
    expect(await after).toBe('up')
  })

  it('a second change while waiting still ends in a read issued after it', async () => {
    const { fn, calls } = reader()
    joinFresh('sessions', fn)
    await tick()
    markChanged()
    const a = joinFresh('sessions', fn)
    markChanged()
    const b = joinFresh('sessions', fn)
    calls[0].resolve('v0')
    await tick(); await tick()
    // One re-read serves both: it left after BOTH changes.
    expect(calls).toHaveLength(2)
    calls[1].resolve('v2')
    expect(await a).toBe('v2')
    expect(await b).toBe('v2')
  })
})

describe('joinFresh — scopes', () => {
  it('a scoped change moves only the reads issued under that scope', async () => {
    // Mark-read is sent on every turn end and changes unread counts only: the
    // thread list must not be read again behind it.
    const sessions = reader()
    const chatState = reader()
    joinFresh('sessions', sessions.fn)
    joinFresh('chat-state', chatState.fn, 'chat-state')
    await tick()
    markChanged('chat-state')
    const s2 = joinFresh('sessions', sessions.fn)
    const c2 = joinFresh('chat-state', chatState.fn, 'chat-state')
    await tick()
    expect(sessions.calls).toHaveLength(1)      // joined
    sessions.calls[0].resolve('threads')
    expect(await s2).toBe('threads')
    chatState.calls[0].resolve('stale')
    await tick(); await tick()
    expect(chatState.calls).toHaveLength(2)     // re-read
    chatState.calls[1].resolve('fresh')
    expect(await c2).toBe('fresh')
  })

  it('an unscoped change moves every read, scoped or not', async () => {
    const chatState = reader()
    joinFresh('chat-state', chatState.fn, 'chat-state')
    await tick()
    markChanged()
    joinFresh('chat-state', chatState.fn, 'chat-state')
    chatState.calls[0].resolve('old')
    await tick(); await tick()
    expect(chatState.calls).toHaveLength(2)
  })
})

describe('joinFresh — a hung request cannot freeze a list', () => {
  // The Workspace transport sets no timeout: a request the network swallowed
  // stays "in flight" for minutes. Nothing may join it, or queue behind it,
  // for longer than MAX_FLIGHT_AGE_MS.
  beforeEach(() => vi.useFakeTimers())
  afterEach(() => vi.useRealTimers())

  it('a flight past its age is not joined: the next read goes out', async () => {
    const { fn, calls } = reader()
    joinFresh('sessions', fn)                 // never settles
    await vi.advanceTimersByTimeAsync(MAX_FLIGHT_AGE_MS - 1)
    joinFresh('sessions', fn)
    await vi.advanceTimersByTimeAsync(0)
    expect(calls).toHaveLength(1)             // still young: joined
    await vi.advanceTimersByTimeAsync(1)
    const fresh = joinFresh('sessions', fn)
    await vi.advanceTimersByTimeAsync(0)
    expect(calls).toHaveLength(2)
    calls[1].resolve('list')
    expect(await fresh).toBe('list')
  })

  it('a read waiting behind a hung flight stops waiting when it ages out', async () => {
    const { fn, calls } = reader()
    joinFresh('chat-state', fn)               // never settles
    await vi.advanceTimersByTimeAsync(1000)
    markChanged()
    let answer = null
    joinFresh('chat-state', fn).then((v) => { answer = v })
    await vi.advanceTimersByTimeAsync(MAX_FLIGHT_AGE_MS - 1001)
    expect(calls).toHaveLength(1)             // still waiting
    await vi.advanceTimersByTimeAsync(1)
    expect(calls).toHaveLength(2)             // gave up waiting, read
    calls[1].resolve('fresh')
    await vi.advanceTimersByTimeAsync(0)
    expect(answer).toBe('fresh')
  })

  it('the hung flight settling late does not disturb the newer one', async () => {
    const { fn, calls } = reader()
    const hung = joinFresh('sessions', fn)
    await vi.advanceTimersByTimeAsync(MAX_FLIGHT_AGE_MS)
    const fresh = joinFresh('sessions', fn)
    await vi.advanceTimersByTimeAsync(0)
    calls[0].resolve('late')
    expect(await hung).toBe('late')
    const joined = joinFresh('sessions', fn)  // the NEWER flight is still the one in the map
    await vi.advanceTimersByTimeAsync(0)
    expect(calls).toHaveLength(2)
    calls[1].resolve('new')
    expect(await fresh).toBe('new')
    expect(await joined).toBe('new')
  })
})
