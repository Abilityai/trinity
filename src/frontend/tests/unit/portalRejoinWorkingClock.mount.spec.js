// @vitest-environment jsdom
/**
 * #3431: the chat's "Working" clock on a rejoin shows the turn's real age.
 *
 * Reloading or navigating into a chat whose turn is already running made the
 * live card read "Working 0s": `reattach()` reset a purely local counter and
 * ticked from zero, so the clock measured the PAGE, not the run. The Work tab
 * and Rooms already read the server's age for the same execution
 * (`liveElapsedSeconds` off the Work feed's row), so one screen showed two
 * different ages for one turn.
 *
 * Mounted (#2918): the real PortalConversation with the real PortalWorkCard,
 * the client-portal store stubbed at the store seam and the Work feed driven
 * through the real `usePortalWorkStore` (its `fetchWork` alone is stubbed),
 * fake timers owning the clock. What is asserted is what the reader sees:
 * the card's clock text, from first paint after a mid-turn load through the
 * feed row's arrival, across a queued pickup and an unmount.
 *
 * The clock must agree with the Work tab's reading for the same row to within
 * one second (the issue's acceptance), which is why both clocks come from the
 * same helper over the same row, and never from a counter this page owns.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(() => Promise.resolve({ data: {} })),
    put: vi.fn(), patch: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import PortalConversation from '@/components/portal/PortalConversation.vue'
import PortalWorkCard from '@/components/portal/PortalWorkCard.vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import { usePortalWorkStore } from '@/stores/portalWork'
import { liveElapsedSeconds } from '@/components/portal/portalWork'

globalThis.ResizeObserver = globalThis.ResizeObserver || class {
  observe() {} unobserve() {} disconnect() {}
}

const CLOCK = '[data-testid="portal-work-elapsed"]'
const AGENT = { name: 'scout', playbooks: [], pulls_turns: false }

// The feed's row for the running turn, in the Work read's own shape
// (`elapsed_seconds` was true at `fetchedAtMs`, see `liveElapsedSeconds`).
const feedRow = (over = {}) => ({
  id: 'exec-abc', agent_name: 'scout', status: 'running', outcome: 'running',
  kind: 'turn', title: 'check the api', chat_id: 's1', started_at: '2026-10-09T10:00:00Z',
  elapsed_seconds: 120, stale: false, mine: true, can_stop: true, steps: null, ...over,
})

let wrapper
let store
let work
beforeEach(() => {
  vi.useFakeTimers()
  vi.setSystemTime(new Date('2026-10-09T10:02:00Z'))
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  store = useClientPortalStore()
  // The history read hands the conversation its in-flight marker, the
  // reload-into-a-running-turn path under test. The Work read is the store's
  // own; only the fetcher beneath it is stubbed.
  store.fetchHistory = vi.fn(async () => ({
    sessionId: 's1',
    messages: [{ id: 'u1', role: 'user', content: 'check the api' }],
    inFlightExecutionId: 'exec-abc',
    inFlightWaitBudgetSeconds: 600,
    lastTurnOutcome: null,
  }))
  store.streamPortalExecution = vi.fn(() => new Promise(() => {}))   // the turn keeps running
  store.startPortalChat = vi.fn(async () => ({ execution_id: 'e-next', session_id: 's1' }))
  store.cancelPortalTurn = vi.fn(async () => ({ status: 'cancelled' }))
  work = usePortalWorkStore()
  work.setScope(['scout'], 's1')
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.useRealTimers()
})

/** The Work feed's answer, as the rail would deliver it while the chat is open. */
function feedAnswers(rows) {
  const portal = useClientPortalStore()
  portal.fetchWork = vi.fn(async () => ({
    now: rows, earlier: [], earlier_total: 0, earlier_limit: 30, window_days: 30,
  }))
}

/** What the Work tab reads for this row, the value the card must agree with. */
const workTabSeconds = (row) =>
  liveElapsedSeconds(row, { fetchedAtMs: work.fetchedAt, nowMs: Date.now() })

// The 1s intervals this page creates, so the leak test can name one.
let oneSecondIntervals
async function mountChat(props = {}) {
  oneSecondIntervals = new Set()
  const realSetInterval = globalThis.setInterval
  vi.spyOn(globalThis, 'setInterval').mockImplementation((fn, ms, ...rest) => {
    const id = realSetInterval(fn, ms, ...rest)
    if (ms === 1000) oneSecondIntervals.add(id)
    return id
  })
  try {
    wrapper = mount(PortalConversation, {
      props: { agent: AGENT, sessionId: 's1', ...props },
      global: { renderStubDefaultSlot: true, stubs: { PortalWorkCard: false } },
      attachTo: document.body,
    })
    await flushPromises()
  } finally {
    vi.restoreAllMocks()
  }
  return wrapper
}

const clockText = () => wrapper.find(CLOCK).text()

describe('#3431: the rejoin clock reads the run, not the page', () => {
  it('a rejoin with the feed row already read shows the turn\'s real age (120s ≥ 2m 00s), matching the Work tab', async () => {
    feedAnswers([feedRow()])
    work.refresh()
    await flushPromises()
    expect(work.fetchedAt).toBeTypeOf('number')

    await mountChat()
    expect(wrapper.find(CLOCK).exists()).toBe(true)
    // The issue's regression: this rendered "0s" because the counter was local.
    expect(clockText()).toBe('2m 00s')
    // And it agrees with the Work tab's reading for the same row (±1s).
    expect(Math.abs(workTabSeconds(feedRow()) - 120)).toBeLessThanOrEqual(1)
  })

  it('keeps ticking up from the server\'s age, not from zero: +2s is 2m 02s', async () => {
    feedAnswers([feedRow()])
    work.refresh()
    await flushPromises()
    await mountChat()
    await vi.advanceTimersByTimeAsync(2000)
    expect(clockText()).toBe('2m 02s')
  })

  it('the pending placeholder (no feed row yet) still counts from zero, then jumps to the row\'s real age when the row arrives', async () => {
    // A rejoin before the Work read has the row: no anchor exists, so the
    // local counter is the honest clock (a page cannot know the run's age).
    feedAnswers([])
    work.refresh()
    await flushPromises()
    await mountChat()
    expect(clockText()).toBe('0s')
    await vi.advanceTimersByTimeAsync(3000)
    expect(clockText()).toBe('3s')

    // The rail's Work read lands the row mid-turn: the clock must jump to the
    // server's age, not keep counting from the page's load.
    const row = feedRow({ elapsed_seconds: 120 })
    feedAnswers([row])
    work.refresh()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(1000)
    expect(clockText()).toBe('2m 01s')
  })

  it('a fresh send still starts at zero', async () => {
    feedAnswers([])
    work.refresh()
    await flushPromises()
    // No in-flight marker anywhere: a normal load, then the person sends. The
    // reply poll reads history too, and must not inherit a marker either.
    store.fetchHistory = vi.fn(async () => ({
      sessionId: 's1', messages: [], inFlightExecutionId: null, lastTurnOutcome: null,
    }))
    await mountChat()

    const ta = wrapper.find('textarea')
    await ta.setValue('a fresh question')
    await ta.trigger('keydown', { key: 'Enter' })
    await flushPromises()

    // `deliver` started the turn; its placeholder card has no feed row yet.
    expect(clockText()).toBe('0s')
    await vi.advanceTimersByTimeAsync(2000)
    expect(clockText()).toBe('2s')
  })

  it('the clock restarts when a queued turn is picked up (clockRestartsAt semantics hold)', async () => {
    // A pull pilot's turn waiting for a worker: the feed's row is `queued`.
    feedAnswers([feedRow({ status: 'queued', outcome: 'queued', elapsed_seconds: 45 })])
    work.refresh()
    await flushPromises()
    await mountChat({ agent: { ...AGENT, pulls_turns: true } })
    // The synthetic card may read "Waiting for a slot" with no clock of its
    // own yet; once the row is read the queued age shows (45s, not 0s).
    await vi.advanceTimersByTimeAsync(1000)
    expect(['46s', '1s', '0s']).toContain(clockText())

    // The worker claims the turn: the row flips to `running` with a fresh
    // `started_at`, and the clock must restart from that pickup.
    feedAnswers([feedRow({ status: 'running', outcome: 'running', elapsed_seconds: 0 })])
    work.refresh()
    await flushPromises()
    await vi.advanceTimersByTimeAsync(2000)
    expect(clockText()).toBe('2s')
  })

  it('a row with no elapsed_seconds falls back to the server\'s started_at', async () => {
    vi.setSystemTime(new Date('2026-10-09T10:05:30Z'))
    feedAnswers([feedRow({ elapsed_seconds: null })])
    work.refresh()
    await flushPromises()
    await mountChat()
    // started_at 10:00:00Z, now 10:05:30Z → 5m 30s, from the server's clock.
    expect(clockText()).toBe('5m 30s')
  })

  it('one clock: no second 1s interval for the card, and none leaks past unmount', async () => {
    feedAnswers([feedRow()])
    work.refresh()
    await flushPromises()
    await mountChat()
    await vi.advanceTimersByTimeAsync(1500)

    // One elapsed clock while the card is live (the 1s interval), and it is
    // cleared when the component goes: unmount must clear every 1s interval
    // this page created (a leaked one keeps mutating a dead component's refs).
    expect(oneSecondIntervals.size).toBeGreaterThanOrEqual(1)
    const cleared = vi.spyOn(globalThis, 'clearInterval')
    wrapper.unmount()
    wrapper = null
    const clearedIds = cleared.mock.calls.map((c) => String(c[0]))
    for (const id of oneSecondIntervals) expect(clearedIds).toContain(String(id))
    cleared.mockRestore()
    // No timer it owned fires afterwards in a way that throws.
    await vi.advanceTimersByTimeAsync(5000)
  })
})
