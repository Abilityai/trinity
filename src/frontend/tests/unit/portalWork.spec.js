/**
 * trinity-enterprise#525 — the live execution card and the rail's Work tab
 * (the visual half of ent#457).
 *
 * Three halves, the house shape (`portalRail.spec.js`, `portalRailFeeds.spec.js`):
 *
 *   1. the pure rules in `portalWork.js` — the status words, the clocks, the
 *      three steps sentences, chat scoping by execution id, the MERGED signal,
 *      the bounded "Earlier" summary and the Ask-about-it prefill;
 *   2. the store under Pinia in node — one request per refresh, a stale
 *      response dropped after a chat switch, a failed first load `failed`
 *      never `empty`, the 12 s poll ONLY while something is live (and a
 *      `stale` row does not count), push events filtered to participants and
 *      debounced, Stop through the existing terminate route; and the owner
 *      composable feeding the Work store off the door gate;
 *   3. source guards for what no unit test can reach — the tab docked in both
 *      rail mounts, the card replacing the dots in both conversations, the
 *      reattach path setting the execution id so Stop works after a reload,
 *      the verdict-memo living beside (not inside) #2320's extracted function,
 *      and no bare loading gate on the new surface.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { computed, nextTick, ref } from 'vue'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'

vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
  globalThis.window = globalThis.window || { location: { pathname: '/workspace' } }
})

const portal = vi.hoisted(() => ({
  fetchWork: vi.fn(),
  cancelPortalTurn: vi.fn(),
  fetchAgentCanvases: vi.fn(async () => []),
  fetchDocuments: vi.fn(async () => []),
  fetchUploads: vi.fn(async () => []),
  uploadDocument: vi.fn(),
  isPlatformSession: true,
  asks: [],
}))
vi.mock('@/stores/clientPortal', () => ({ useClientPortalStore: () => portal }))

const api = vi.hoisted(() => ({ get: vi.fn(async () => ({ data: [] })), post: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))
vi.mock('../../src/api', () => ({ default: api }))

import {
  EARLIER_PREVIEW, askAboutItPrefill, childrenForChat, earlierSlice, earlierSummary, formatElapsed,
  holderLine, isHonestTerminal, isLive, itemById, kindLabel, liveElapsedSeconds, liveItems,
  stageRows, stepsLine, workSignalFromItems, workStatusLabel, workTone, workView,
} from '@/components/portal/portalWork'
import { workSignalFrom } from '@/components/portal/portalRail'
import { usePortalWorkStore } from '@/stores/portalWork'
import { usePortalRailFeeds } from '@/composables/usePortalRailFeeds'
import { RAIL_TABS, visibleTabs } from '@/components/portal/portalRail'

const src = (rel) => stripComments(readFileSync(fileURLToPath(new URL(`../../src/${rel}`, import.meta.url)), 'utf8'))
const flush = () => new Promise((r) => setTimeout(r, 0))

const item = (over = {}) => ({
  id: 'x1', agent_name: 'scout', status: 'running', outcome: 'running', kind: 'turn',
  title: 'Reconcile the invoices', started_at: '2026-09-06T10:00:00Z', elapsed_seconds: 30,
  stale: false, chat_id: 'sess-1', mine: true, can_stop: true, steps: null, ...over,
})

beforeEach(() => {
  setActivePinia(createPinia())
  vi.useRealTimers()
  portal.fetchWork.mockReset()
  portal.cancelPortalTurn.mockReset()
  portal.fetchWork.mockImplementation(async () => ({ now: [], earlier: [], earlier_total: 0, earlier_limit: 30, window_days: 30 }))
})

// ---------------------------------------------------------------------------
// 1. Pure rules
// ---------------------------------------------------------------------------

describe('ent#525 — the words are honest', () => {
  it('names every outcome, and the stale verdict wins over the status', () => {
    expect(workStatusLabel(item({ outcome: 'running' }))).toBe('Working')
    expect(workStatusLabel(item({ outcome: 'queued' }))).toBe('Waiting for a slot')
    expect(workStatusLabel(item({ outcome: 'success' }))).toBe('Done')
    expect(workStatusLabel(item({ outcome: 'failed' }))).toBe('Failed')
    expect(workStatusLabel(item({ outcome: 'timeout' }))).toBe('Timed out')
    expect(workStatusLabel(item({ outcome: 'cancelled' }))).toBe('Stopped by you')
    expect(workStatusLabel(item({ outcome: 'lost' }))).toBe('No longer tracked')
    expect(workStatusLabel(null)).toBe('Unknown')
    expect(isLive(item())).toBe(true)
    expect(isLive(item({ stale: true }))).toBe(false)
    expect(isLive(item({ status: 'success' }))).toBe(false)
    expect(liveItems([item(), item({ id: 'x2', status: 'failed' }), null]).map((i) => i.id)).toEqual(['x1'])
  })

  it('tones by outcome, never by hue name, and only non-success terminals keep a card', () => {
    expect(workTone(item({ outcome: 'running' }))).toBe('active')
    expect(workTone(item({ outcome: 'success' }))).toBe('ok')
    expect(workTone(item({ outcome: 'timeout' }))).toBe('danger')
    expect(workTone(item({ outcome: 'cancelled' }))).toBe('warn')
    expect(['failed', 'timeout', 'cancelled', 'lost'].every(isHonestTerminal)).toBe(true)
    expect(isHonestTerminal('success')).toBe(false)
    expect(isHonestTerminal('running')).toBe(false)
  })

  it('kind words fall back to Background for an unknown kind', () => {
    expect(kindLabel('turn')).toBe('You asked')
    expect(kindLabel('delegated')).toBe('Handed on')
    expect(kindLabel('loop')).toBe('Loop run')
    expect(kindLabel('whatever')).toBe('Background')
  })
})

describe('ent#525 — clocks', () => {
  it('formats elapsed, never negative', () => {
    expect(formatElapsed(4)).toBe('4s')
    expect(formatElapsed(125)).toBe('2m 05s')
    expect(formatElapsed(4320)).toBe('1h 12m')
    expect(formatElapsed(-1)).toBeNull()
    expect(formatElapsed(null)).toBeNull()
  })

  it("advances the SERVER's reading, falls back to started_at as an instant, and stops on terminal/stale", () => {
    const fetchedAtMs = 1_000_000
    expect(liveElapsedSeconds(item({ elapsed_seconds: 30 }), { fetchedAtMs, nowMs: fetchedAtMs + 5000 })).toBe(35)
    const startMs = Date.parse('2026-09-06T10:00:00Z')
    expect(liveElapsedSeconds(item({ elapsed_seconds: null }), { fetchedAtMs: null, nowMs: startMs + 90_000 })).toBe(90)
    expect(liveElapsedSeconds(item({ stale: true }), { fetchedAtMs, nowMs: fetchedAtMs + 5000 })).toBeNull()
    expect(liveElapsedSeconds(item({ status: 'success' }), { fetchedAtMs, nowMs: fetchedAtMs })).toBeNull()
  })
})

describe('ent#525 — three steps sentences (ruling 2, reviewed)', () => {
  it('reported → stages; none → "doesn\'t report steps"; unknown → "could not be read"; not-yet → nothing', () => {
    const reported = { state: 'reported', stages: [{ id: 'a', name: 'Collect', state: 'done', holder: 'scout' }] }
    expect(stepsLine(reported, 'scout').kind).toBe('stages')
    expect(stepsLine({ state: 'none' }, 'scout')).toEqual({ kind: 'none', text: "scout doesn't report steps." })
    expect(stepsLine({ state: 'unknown' }, 'scout')).toEqual({ kind: 'unknown', text: 'Steps could not be read right now.' })
    expect(stepsLine(null, 'scout').kind).toBe('unknown')
    expect(stepsLine(undefined, 'scout')).toEqual({ kind: 'pending', text: '' })
    // A reported pipeline with no stages is still "doesn't report steps".
    expect(stepsLine({ state: 'reported', stages: [] }, null).text).toBe("This agent doesn't report steps.")
  })

  it('stage rows normalize state and holder; a masked holder reads "another agent"', () => {
    const rows = stageRows({ stages: [{ id: 'a', state: 'weird' }, { id: 'b', name: 'B', state: 'current', holder: 'sage' }] })
    expect(rows).toEqual([
      { id: 'a', name: 'a', state: 'pending', holder: null },
      { id: 'b', name: 'B', state: 'current', holder: 'sage' },
    ])
    expect(holderLine('sage', { agentName: 'scout' })).toBe('held by sage')
    expect(holderLine('scout', { agentName: 'scout' })).toBeNull()
    expect(holderLine(null, { agentName: 'scout', masked: true })).toBe('held by another agent')
  })
})

describe('ent#525 — chat scoping is by execution id, never "latest running"', () => {
  it('finds the turn by id and its children by chat', () => {
    const items = [item(), item({ id: 'c1', kind: 'delegated', agent_name: 'sage' }),
      item({ id: 'o1', chat_id: 'sess-2' }), item({ id: 'done', status: 'success' })]
    expect(itemById(items, 'x1').id).toBe('x1')
    expect(itemById(items, null)).toBeNull()
    expect(childrenForChat(items, 'sess-1', 'x1').map((i) => i.id)).toEqual(['c1'])
    expect(childrenForChat(items, null)).toEqual([])
  })
})

describe('ent#525 — ONE merged signal (review A1)', () => {
  it('counts a turn both sources see exactly once, and an emit with no id once', () => {
    const items = [item(), item({ id: 'c1', agent_name: 'sage' })]
    expect(workSignalFromItems(items, { emit: { executionId: 'x1', agent: 'scout' } }))
      .toEqual({ live: 2, updated: false, agents: ['scout', 'sage'] })
    expect(workSignalFromItems(items, { emit: { executionId: 'new', agent: 'scout' } }).live).toBe(3)
    expect(workSignalFromItems([], { emit: { executionId: null, agent: 'scout' } }))
      .toEqual({ live: 1, updated: false, agents: ['scout'] })
    expect(workSignalFromItems([item({ stale: true })], {})).toEqual({ live: 0, updated: false, agents: [] })
    // A room's server list joins by NAME; a name already live in the feed is not doubled.
    expect(workSignalFromItems(items, { extraAgents: ['sage', 'kin'] }).live).toBe(3)
  })

  it('the conversation emit carries the id only when it has one', () => {
    expect(workSignalFrom({ sending: true, agent: 'scout', executionId: 'x1' }))
      .toEqual({ live: 1, updated: false, agents: ['scout'], executionId: 'x1' })
    expect(workSignalFrom({ sending: true, agent: 'scout' })).toEqual({ live: 1, updated: false, agents: ['scout'] })
    expect(workSignalFrom({ sending: false, executionId: 'x1' })).toEqual({ live: 0, updated: false, agents: [] })
  })
})

describe('ent#525 — Earlier is bounded and says so', () => {
  it('"N in the last 30 days · latest 3 shown", and 30+ when the page is full', () => {
    expect(earlierSummary({ total: 12, shown: 3, windowDays: 30, pageLimit: 30 })).toBe('12 in the last 30 days · latest 3 shown')
    expect(earlierSummary({ total: 2, shown: 2, windowDays: 30, pageLimit: 30 })).toBe('2 in the last 30 days')
    expect(earlierSummary({ total: 0, shown: 0 })).toBe('0 in the last 30 days')
    expect(earlierSummary({ total: 30, shown: 30, windowDays: 30, pageLimit: 30 })).toBe('30+ in the last 30 days')
    expect(earlierSummary({ total: 45, shown: 30, windowDays: 30, pageLimit: 30 })).toBe('45 in the last 30 days · latest 30 shown')
    const five = [1, 2, 3, 4, 5].map((n) => item({ id: `e${n}` }))
    expect(earlierSlice(five, false)).toHaveLength(EARLIER_PREVIEW)
    expect(earlierSlice(five, true)).toHaveLength(5)
  })

  it('the tab reads a VERDICT: loading with no participants, failed before empty', () => {
    expect(workView({ participants: [] }).state).toBe('loading')
    expect(workView({ participants: ['scout'], hasLoaded: false, error: 'x' }).state).toBe('failed')
    expect(workView({ participants: ['scout'], hasLoaded: true, count: 0 }).state).toBe('empty')
    expect(workView({ participants: ['scout'], hasLoaded: true, count: 2, error: 'x' })).toEqual({ state: 'ready', stale: true })
  })
})

describe('ent#525 — Ask about it is a prefill that names the job', () => {
  it('quotes the title, says how it ended, and addresses the agent in a room', () => {
    const text = askAboutItPrefill(item({ outcome: 'timeout' }))
    expect(text).toContain('"Reconcile the invoices"')
    expect(text).toContain('timed out')
    const long = askAboutItPrefill(item({ outcome: 'failed', title: 'x'.repeat(100) }))
    expect(long).toContain('…')
    expect(askAboutItPrefill(item({ outcome: 'lost' }), { participants: ['scout', 'sage'] })).toMatch(/^scout, what happened/)
    expect(askAboutItPrefill({ outcome: 'failed', title: '' })).toContain('the last job')
  })
})

// ---------------------------------------------------------------------------
// 2. The store, and its owner
// ---------------------------------------------------------------------------

describe('ent#525 — the Work store', () => {
  it('reads once for every participant with the chat id, and lands the page', async () => {
    portal.fetchWork.mockImplementation(async () => ({
      now: [item()], earlier: [item({ id: 'e1', status: 'success', outcome: 'success' })],
      earlier_total: 7, earlier_limit: 30, window_days: 30,
    }))
    const s = usePortalWorkStore()
    s.setScope(['scout', 'sage'], 'sess-1')
    await s.refresh()
    expect(portal.fetchWork).toHaveBeenCalledWith(['scout', 'sage'], 'sess-1')
    expect(s.hasLoaded).toBe(true)
    expect(s.live.map((i) => i.id)).toEqual(['x1'])
    expect(s.earlierTotal).toBe(7)
    expect(Number.isFinite(s.fetchedAt)).toBe(true)
    s.stopPolling()
  })

  it('does nothing with no participants', async () => {
    const s = usePortalWorkStore()
    await s.refresh()
    expect(portal.fetchWork).not.toHaveBeenCalled()
    expect(s.hasLoaded).toBe(false)
  })

  it('a failed first load is failed, never empty; a failed refresh keeps the rows', async () => {
    portal.fetchWork.mockRejectedValueOnce(new Error('boom'))
    const s = usePortalWorkStore()
    s.setScope(['scout'])
    await s.refresh()
    expect(s.hasLoaded).toBe(false)
    expect(s.error).toBeTruthy()
    portal.fetchWork.mockImplementationOnce(async () => ({ now: [item()], earlier: [], earlier_total: 0 }))
    await s.refresh()
    expect(s.hasLoaded).toBe(true)
    portal.fetchWork.mockRejectedValueOnce({ response: { data: { detail: 'Couldn\'t load what\'s running. Try again in a moment.' } } })
    await s.refresh()
    expect(s.now).toHaveLength(1)
    expect(s.error).toContain("Couldn't load")
    s.stopPolling()
  })

  it('drops a response that lands after the chat switched', async () => {
    let release
    portal.fetchWork.mockImplementationOnce(() => new Promise((r) => { release = r }))
    const s = usePortalWorkStore()
    s.setScope(['scout'])
    const p = s.refresh()
    s.setScope(['sage'])
    release({ now: [item()], earlier: [], earlier_total: 0 })
    await p
    expect(s.now).toEqual([])
    expect(s.hasLoaded).toBe(false)
  })

  it('polls every 12 s only while something is LIVE — a stale row does not count', async () => {
    vi.useFakeTimers()
    portal.fetchWork.mockImplementation(async () => ({ now: [item()], earlier: [], earlier_total: 0 }))
    const s = usePortalWorkStore()
    s.setScope(['scout'])
    await s.refresh()
    expect(portal.fetchWork).toHaveBeenCalledTimes(1)
    await vi.advanceTimersByTimeAsync(12_000)
    expect(portal.fetchWork).toHaveBeenCalledTimes(2)
    portal.fetchWork.mockImplementation(async () => ({ now: [item({ stale: true })], earlier: [], earlier_total: 0 }))
    await vi.advanceTimersByTimeAsync(12_000)
    expect(portal.fetchWork).toHaveBeenCalledTimes(3)
    await vi.advanceTimersByTimeAsync(30_000)
    expect(portal.fetchWork).toHaveBeenCalledTimes(3)      // the ghost row did not keep the poll alive
    s.clear()
    vi.useRealTimers()
  })

  it("refreshes (debounced) on a participant's activity — started included — and ignores the rest", async () => {
    vi.useFakeTimers()
    const s = usePortalWorkStore()
    s.setScope(['scout'])
    s.handleWebSocketEvent({ type: 'agent_activity', agent_name: 'scout', activity_state: 'started' })
    s.handleWebSocketEvent({ type: 'agent_activity', agent_name: 'scout', activity_state: 'completed' })
    s.handleWebSocketEvent({ type: 'loop_run_completed', agent_name: 'scout' })
    s.handleWebSocketEvent({ type: 'agent_activity', agent_name: 'other', activity_state: 'started' })
    s.handleWebSocketEvent({ type: 'agent_activity' })
    s.handleWebSocketEvent({ type: 'room_message', agent_name: 'scout' })
    expect(portal.fetchWork).not.toHaveBeenCalled()
    await vi.advanceTimersByTimeAsync(2_100)
    expect(portal.fetchWork).toHaveBeenCalledTimes(1)
    s.clear()
    vi.useRealTimers()
  })

  it('Stop goes through the existing terminate route and refetches; a 404 is the lost race', async () => {
    portal.cancelPortalTurn.mockResolvedValueOnce({ status: 'terminated' })
    const s = usePortalWorkStore()
    s.setScope(['scout'])
    const res = await s.stopItem(item())
    expect(res.success).toBe(true)
    expect(portal.cancelPortalTurn).toHaveBeenCalledWith('scout', 'x1')
    expect(portal.fetchWork).toHaveBeenCalledTimes(1)
    expect((await s.stopItem(item({ can_stop: false }))).success).toBe(false)
    portal.cancelPortalTurn.mockRejectedValueOnce({ response: { status: 404 } })
    expect((await s.stopItem(item())).success).toBe(true)
    portal.cancelPortalTurn.mockRejectedValueOnce(new Error('nope'))
    expect((await s.stopItem(item())).success).toBe(false)
    s.stopPolling()
  })
})

describe('ent#525 — the owner feeds the Work store off the door gate', () => {
  function mount({ session, participants, chatId = ref(null), emit = ref(null) }) {
    const visible = ref(true)
    const parts = ref(participants)
    const tabs = computed(() => visibleTabs(RAIL_TABS, { ...session, participants: parts.value }))
    const rail = usePortalRailFeeds({
      visible, tabs, participants: parts, activeTab: ref('work'), open: ref(false), sheetOpen: ref(false),
      storage: () => null, chatId, workEmit: emit,
    })
    return { rail, visible, parts }
  }

  it('a platform session reads the feed with the chat id; a client never does', async () => {
    const chatId = ref('sess-1')
    const { rail } = mount({ session: { isPlatform: true }, participants: ['scout'], chatId })
    await flush()
    expect(portal.fetchWork).toHaveBeenCalledWith(['scout'], 'sess-1')
    expect(rail.work.participants).toEqual(['scout'])
    rail.work.stopPolling()

    portal.fetchWork.mockClear()
    setActivePinia(createPinia())
    mount({ session: { isPlatform: false }, participants: ['scout'] })
    await flush()
    expect(portal.fetchWork).not.toHaveBeenCalled()
  })

  it('a thread switch under the same agent re-scopes ONLY the Work feed; hiding clears it', async () => {
    const chatId = ref('sess-1')
    const { rail, visible } = mount({ session: { isPlatform: true }, participants: ['scout'], chatId })
    await flush()
    const loopsCalls = api.get.mock.calls.length
    chatId.value = 'sess-2'
    await nextTick(); await flush()
    expect(portal.fetchWork).toHaveBeenLastCalledWith(['scout'], 'sess-2')
    expect(api.get.mock.calls.length).toBe(loopsCalls)       // loops did not refetch
    visible.value = false
    await nextTick()
    expect(rail.work.participants).toEqual([])
  })

  it('the signal merges the emit by id — one turn, one count', async () => {
    portal.fetchWork.mockImplementation(async () => ({ now: [item()], earlier: [], earlier_total: 0 }))
    const emit = ref(workSignalFrom({ sending: true, agent: 'scout', executionId: 'x1' }))
    const { rail } = mount({ session: { isPlatform: true }, participants: ['scout'], emit })
    await flush()
    expect(rail.signals.value.work).toEqual({ live: 1, updated: false, agents: ['scout'] })
    emit.value = workSignalFrom({ sending: true, agent: 'scout', executionId: 'x2' })
    expect(rail.signals.value.work.live).toBe(2)
    emit.value = workSignalFrom({ sending: false })
    expect(rail.signals.value.work.live).toBe(1)
    rail.work.stopPolling()
  })
})

// ---------------------------------------------------------------------------
// 3. Source guards
// ---------------------------------------------------------------------------

describe('ent#525 — the tab and the card are wired (source guards)', () => {
  const shell = src('views/Portal.vue')
  const conv = src('components/portal/PortalConversation.vue')
  const room = src('components/portal/PortalRoom.vue')
  const tab = src('components/portal/PortalWork.vue')
  const card = src('components/portal/PortalWorkCard.vue')
  const ws = src('utils/websocket.js')

  it('the Work body docks into BOTH rail mounts, and the shell hands it the chat id and the prefill path', () => {
    expect((shell.match(/<template #tab-work=/g) || []).length).toBe(2)
    expect((shell.match(/<PortalWork /g) || []).length).toBe(2)
    expect(shell).toContain(':chat-id="railChatId"')
    expect(shell).toContain('@ask-about-it="askAboutIt"')
    expect(shell).toMatch(/function askAboutIt\(text\) \{[\s\S]*usePlaybook\(text\)/)
    expect(shell).toContain("@open-work=\"openRailOn('work')\"")
    expect(shell).toContain('chatId: railChatId')
    expect(shell).toContain('workEmit: workSignal')
    // A turn STARTING nudges the feed (the row and its children appear then).
    expect(shell).toMatch(/if \(!wasLive && workSignal\.value\.live\) rail\.work\.scheduleRefresh\(/)
  })

  it('the conversation renders the card while sending and the terminal card after an honest failure', () => {
    expect(conv).toContain('<PortalWorkCard')
    expect(conv).not.toContain('animate-bounce')
    expect(conv).toContain(':item="liveCardItem"')
    expect(conv).toContain(':can-stop="canCancelTurn"')
    expect(conv).toContain('@stop="cancelTurn"')
    expect(conv).toContain('data-testid="portal-work-terminal"')
    expect(conv).toContain('@ask-about-it="askAboutIt"')
    // A retryable verdict (nothing reached the agent) gets no card — the red
    // message with Retry is the honest UI for a job that never ran.
    const termAt = conv.indexOf('const terminalCardItem = computed(')
    expect(conv.slice(termAt, termAt + 400)).toContain('if (o.retryable === true) return null')
    // Matched by id, never "latest running".
    expect(conv).toContain('itemById(workStore.now, activeExecutionId.value)')
    expect(conv).toContain('childrenForChat(workStore.now, currentSessionId.value, activeExecutionId.value)')
  })

  it('a reattached turn can be stopped (review E3), and the id is cleared with the turn', () => {
    const at = conv.indexOf('async function reattach(')
    const body = conv.slice(at, conv.indexOf('\n}', at))
    expect(body).toContain('activeExecutionId.value = executionId || null')
    expect(body).toMatch(/finally \{[\s\S]*activeExecutionId\.value = null/)
  })

  it("the verdict memo lives beside #2320's extracted function, not inside it", () => {
    const at = conv.indexOf('function markLastUserTurnFailed(')
    const body = conv.slice(at, conv.indexOf('\n}', at))
    expect(body).not.toContain('terminalOutcome')
    expect(conv).toContain('if (outcome) rememberVerdict(outcome)')
    expect(conv).toContain('rememberVerdict(data.outcome)')
    // A new turn clears the previous verdict's card; so does a thread switch.
    const deliverAt = conv.indexOf('async function deliver(')
    expect(conv.slice(deliverAt, deliverAt + 200)).toContain('terminalOutcome.value = null')
    const loadAt = conv.indexOf('async function loadThread(')
    expect(conv.slice(loadAt, loadAt + 200)).toContain('terminalOutcome.value = null')
  })

  it('the room renders cards for the agents the SERVER says are working, and keeps its fallback line', () => {
    expect(room).toContain('<PortalWorkCard')
    expect(room).toContain('data-testid="portal-room-work"')
    expect(room).toContain('workingAgents.value.includes(it.agent_name)')
    expect(room).toContain('v-else-if="workingAgents.length"')
    expect(room).toContain("@open-work=\"emit('open-work')\"")
  })

  it('the tab body reads a verdict, never a bare loading flag, and filters asks in a computed', () => {
    expect(tab).toContain("v-if=\"view.state === 'loading'\"")
    expect(tab).not.toMatch(/v-if="store\.loading"/)
    expect(tab).toContain('<LoadFailed')
    expect(tab).toContain('<InlineError')
    expect(tab).toContain('<PortalAsks :agent-names="participants"')
    expect(tab).not.toContain('fetchAsks(')
    expect(tab).toContain('portal.isPlatformSession')
    expect(tab).toContain('groupByParticipant(')
    expect(tab).toContain('data-testid="portal-work-show-all"')
  })

  it('the card animates only where motion is welcome and names the three steps states', () => {
    expect(card).toContain('motion-safe:animate-pulse')
    expect(card).not.toMatch(/[^:]animate-pulse/)
    expect(card).toContain('Ask about it')
    expect(card).toContain('Open in Work')
    expect(card).toContain("steps.kind !== 'pending'")
  })

  it('the WebSocket handler routes activity and loop events to the Work store', () => {
    expect(ws).toContain("import { usePortalWorkStore } from '../stores/portalWork'")
    expect((ws.match(/portalWorkStore\.handleWebSocketEvent\(data\)/g) || []).length).toBe(2)
  })
})
