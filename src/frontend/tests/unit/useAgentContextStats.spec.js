/**
 * trinity-enterprise#950 — the agent page's context poll for the header chart.
 *
 * AgentDetail is KeepAlive-cached (App.vue), so `onUnmounted` never fires on
 * navigation: the poll must be started and stopped by the page's own
 * start/stopAllPolling lifecycle, and stopping must really stop it. A slow
 * fleet-wide `GET /api/agents/context-stats` must not stack requests either.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { ref } from 'vue'
import { useAgentContextStats, CONTEXT_POLL_MS } from '../../src/composables/useAgentContextStats'

function fakeStore() {
  const store = { contextStats: {}, calls: 0, resolve: null }
  store.fetchContextStats = vi.fn(() => {
    store.calls += 1
    return new Promise((r) => { store.resolve = r })
  })
  return store
}

beforeEach(() => {
  vi.useFakeTimers()
  globalThis.document = { hidden: false }
})
afterEach(() => { vi.useRealTimers(); delete globalThis.document })

describe('useAgentContextStats', () => {
  it('reads this agent\'s entry, or null', () => {
    const store = fakeStore()
    const agent = ref({ name: 'alpha' })
    const { stats } = useAgentContextStats(agent, store)
    expect(stats.value).toBeNull()
    store.contextStats = { alpha: { contextPercent: 12 } }
    // computed over a plain object: re-read through a fresh call
    expect(useAgentContextStats(agent, store).stats.value).toEqual({ contextPercent: 12 })
  })

  it('fetches immediately on start, then on the interval, and stop ends it', async () => {
    const store = fakeStore()
    const { start, stop } = useAgentContextStats(ref({ name: 'alpha' }), store)
    start()
    expect(store.calls).toBe(1)
    store.resolve(); await Promise.resolve(); await Promise.resolve()
    vi.advanceTimersByTime(CONTEXT_POLL_MS)
    expect(store.calls).toBe(2)
    store.resolve(); await Promise.resolve(); await Promise.resolve()
    stop()
    vi.advanceTimersByTime(CONTEXT_POLL_MS * 3)
    expect(store.calls).toBe(2)
  })

  it('never stacks a request on one still in flight', () => {
    const store = fakeStore()
    const { start } = useAgentContextStats(ref({ name: 'alpha' }), store)
    start()
    vi.advanceTimersByTime(CONTEXT_POLL_MS * 3)
    expect(store.calls).toBe(1)
  })

  it('skips ticks while the browser tab is hidden', async () => {
    const store = fakeStore()
    const { start } = useAgentContextStats(ref({ name: 'alpha' }), store)
    start()
    store.resolve(); await Promise.resolve(); await Promise.resolve()
    document.hidden = true
    vi.advanceTimersByTime(CONTEXT_POLL_MS * 2)
    expect(store.calls).toBe(1)
  })

  it('a second start does not run two intervals', async () => {
    const store = fakeStore()
    const { start } = useAgentContextStats(ref({ name: 'alpha' }), store)
    start()
    store.resolve(); await Promise.resolve(); await Promise.resolve()
    start()
    store.resolve(); await Promise.resolve(); await Promise.resolve()
    const before = store.calls
    vi.advanceTimersByTime(CONTEXT_POLL_MS)
    expect(store.calls).toBe(before + 1)
  })
})
