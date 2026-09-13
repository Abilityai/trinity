/**
 * #2703 — the client half of "assigning a skill delivers it, and the lists follow".
 *
 *   * `stores/skills.js` — `noteSkillsChanged` ticks one agent, debounced, so
 *     a burst of `agent_skills_changed` events costs one refetch per agent;
 *     `saveAssignments` keeps the PUT's `delivery` report.
 *   * `utils/skillDelivery.js` — the three things a user has to be told
 *     (delivered / applies on start / not delivered and why), never the old
 *     fixed "Saved. Sync now…" sentence.
 *   * `stores/clientPortal.js::revalidateBriefing` — stale-while-revalidate:
 *     re-hydrates a HYDRATED card without flipping it back to `pending` (that
 *     re-enters the loading skeleton on a zone with data), coalesces an event
 *     landing mid-flight into one re-run, and honours `maxAge` for the
 *     surfaces that have no `/ws`.
 *
 * All three are executed, not grepped. Same harness shape as
 * clientPortalBatchSessions.spec.js (node env, axios + auth mocked).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

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
vi.mock('@/stores/auth', async () => {
  const { ref } = await import('vue')
  const authed = ref(false)
  return {
    useAuthStore: () => ({
      get isAuthenticated() { return authed.value },
      get authHeader() { return authed.value ? { Authorization: 'Bearer jwt' } : {} },
    }),
  }
})
vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})
vi.mock('@/api', () => ({
  default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))

import axios from 'axios'
import api from '@/api'
import { useSkillsStore, SKILLS_CHANGED_DEBOUNCE_MS } from '@/stores/skills'
import { useClientPortalStore } from '@/stores/clientPortal'
import { deliveryText, DELIVERY_TONE } from '@/utils/skillDelivery'

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.useRealTimers()
})

// ---------------------------------------------------------------------------
// stores/skills.js
// ---------------------------------------------------------------------------
describe('skills store — agent_skills_changed ticks', () => {
  it('ticks the named agent once per burst, after the debounce', async () => {
    vi.useFakeTimers()
    const store = useSkillsStore()
    store.noteSkillsChanged('scout')
    store.noteSkillsChanged('scout')
    store.noteSkillsChanged('scout')
    expect(store.changedAt.scout).toBeUndefined()          // nothing yet — debounced
    vi.advanceTimersByTime(SKILLS_CHANGED_DEBOUNCE_MS + 1)
    expect(typeof store.changedAt.scout).toBe('number')
    expect(store.changedAt.scribe).toBeUndefined()         // per agent, not global
  })

  it('a second burst after the window ticks again (a watcher sees a new value)', () => {
    vi.useFakeTimers()
    const store = useSkillsStore()
    store.noteSkillsChanged('scout')
    vi.advanceTimersByTime(SKILLS_CHANGED_DEBOUNCE_MS + 1)
    const first = store.changedAt.scout
    vi.advanceTimersByTime(50)
    store.noteSkillsChanged('scout')
    vi.advanceTimersByTime(SKILLS_CHANGED_DEBOUNCE_MS + 1)
    expect(store.changedAt.scout).toBeGreaterThan(first)
  })

  it('ignores an empty name', () => {
    const store = useSkillsStore()
    store.noteSkillsChanged('')
    expect(store.changedAt).toEqual({})
  })

  it('saveAssignments keeps the PUT delivery report and sends a timeout above the server budget', async () => {
    const store = useSkillsStore()
    store.setAgent('scout')
    const delivery = { status: 'pending_start', skills: { research: { status: 'pending_start' } } }
    api.put.mockResolvedValueOnce({ data: { success: true, delivery } })
    api.get.mockResolvedValueOnce({ data: [{ skill_name: 'research' }] })
    expect(await store.saveAssignments(['research'])).toBe(true)
    expect(store.lastDelivery).toEqual(delivery)
    const [, , cfg] = api.put.mock.calls[0]
    expect(cfg.timeout).toBeGreaterThan(20000)
  })
})

// ---------------------------------------------------------------------------
// utils/skillDelivery.js
// ---------------------------------------------------------------------------
describe('deliveryText', () => {
  const OLD = 'Saved. Sync now, or the agent picks them up on next start.'

  it('delivered → ok, no Sync needed', () => {
    const v = deliveryText({ status: 'injected', skills: {} })
    expect(v).toEqual({ tone: DELIVERY_TONE.ok, text: 'Saved and delivered — available now.', needsSync: false })
  })

  it('stopped agent → pending, says it applies on start, no Sync needed', () => {
    const v = deliveryText({ status: 'pending_start', skills: {} })
    expect(v.tone).toBe(DELIVERY_TONE.pending)
    expect(v.text).toMatch(/applies on next start/)
    expect(v.needsSync).toBe(false)
  })

  it('in_progress → pending, promises the lists will follow', () => {
    const v = deliveryText({ status: 'in_progress', skills: {} })
    expect(v.tone).toBe(DELIVERY_TONE.pending)
    expect(v.text).toMatch(/still installing/)
  })

  it('not delivered names WHY and points at Sync', () => {
    for (const [reason, why] of [
      ['injection_in_progress', 'mid-sync'],
      ['agent_not_ready', 'still starting'],
      ['docker_unavailable', 'container state'],
      ['injection_error', 'install failed'],
    ]) {
      const v = deliveryText({ status: 'not_delivered', reason, skills: {} })
      expect(v.tone).toBe(DELIVERY_TONE.bad)
      expect(v.text).toContain(why)
      expect(v.needsSync).toBe(true)
    }
  })

  it('partial names the skills that failed', () => {
    const v = deliveryText({ status: 'partial', skills: { a: { status: 'injected' }, b: { status: 'failed', error: 'x' } } })
    expect(v.tone).toBe(DELIVERY_TONE.bad)
    expect(v.text).toContain('(b)')
    expect(v.needsSync).toBe(true)
  })

  it('no report (nothing added, or an older backend) is a plain Saved', () => {
    expect(deliveryText(null)).toEqual({ tone: DELIVERY_TONE.none, text: 'Saved.', needsSync: false })
    expect(deliveryText(undefined, { saved: false }).text).toBe('Assigned.')
  })

  it('the Library form leads with Assigned, and no arm reproduces the old sentence', () => {
    expect(deliveryText({ status: 'injected', skills: {} }, { saved: false }).text).toMatch(/^Assigned and delivered/)
    for (const status of ['injected', 'partial', 'pending_start', 'in_progress', 'not_delivered', 'weird']) {
      expect(deliveryText({ status, skills: {} }).text).not.toBe(OLD)
    }
  })
})

// ---------------------------------------------------------------------------
// stores/clientPortal.js — revalidateBriefing
// ---------------------------------------------------------------------------
const BRIEFINGS = '/api/enterprise/client-portal/briefings'

function readyCard(name, extra = {}) {
  return {
    name, briefing_state: 'ready', description: 'old', playbooks: [{ title: 'old-hint' }],
    searchable_playbooks: [{ name: 'old' }], playbooks_total: 1, briefing_hydrated_at: Date.now() - 120_000,
    ...extra,
  }
}

function portalStore(cards) {
  const store = useClientPortalStore()
  store.portalToken = 'portal-token'
  store.agents = cards
  store.multiAgentChatAvailable = false
  return store
}

const answer = (name, overrides = {}) => ({
  data: { briefings: { [name]: { state: 'ready', description: 'new', playbooks: [{ title: 'new-hint' }],
                                  searchable_playbooks: [{ name: 'new' }], playbooks_total: 1, ...overrides } } },
})

describe('clientPortal.revalidateBriefing (#2703)', () => {
  it('re-hydrates a READY card through the per-agent route, keeping its data until the answer lands', async () => {
    const store = portalStore([readyCard('scout'), readyCard('sage')])
    let resolve
    axios.get.mockReturnValueOnce(new Promise((r) => { resolve = r }))

    const p = store.revalidateBriefing('scout')
    // In flight: NOT pending, hints intact (the p13 rule — no loading skeleton on data).
    expect(store.agents.find((a) => a.name === 'scout').briefing_state).toBe('ready')
    expect(store.agents.find((a) => a.name === 'scout').playbooks[0].title).toBe('old-hint')

    resolve(answer('scout'))
    await p
    const scout = store.agents.find((a) => a.name === 'scout')
    expect(scout.playbooks[0].title).toBe('new-hint')                    // swapped on arrival
    expect(scout.searchable_playbooks[0].name).toBe('new')
    expect(store.agents.find((a) => a.name === 'sage').playbooks[0].title).toBe('old-hint')   // untouched
    expect(axios.get).toHaveBeenCalledTimes(1)
    expect(axios.get.mock.calls[0][0]).toBe(BRIEFINGS)
    expect(axios.get.mock.calls[0][1].params).toEqual({ agents: 'scout' })   // one agent, never the roster
  })

  it('an event landing mid-flight re-runs ONCE after the first answer, so the stale one never wins', async () => {
    const store = portalStore([readyCard('scout')])
    let resolveFirst
    axios.get
      .mockReturnValueOnce(new Promise((r) => { resolveFirst = r }))
      .mockResolvedValueOnce(answer('scout', { description: 'newer' }))

    const first = store.revalidateBriefing('scout')
    await store.revalidateBriefing('scout')            // lands while #1 is in flight → dirty, no 2nd GET yet
    expect(axios.get).toHaveBeenCalledTimes(1)

    resolveFirst(answer('scout', { description: 'new' }))
    await first
    await Promise.resolve(); await Promise.resolve()   // let the finally's re-run settle
    expect(axios.get).toHaveBeenCalledTimes(2)
    expect(store.agents[0].description).toBe('newer')
  })

  it('honours maxAge — a fresh card is not re-asked, a stale one is', async () => {
    const store = portalStore([
      readyCard('fresh', { briefing_hydrated_at: Date.now() - 1_000 }),
      readyCard('stale', { briefing_hydrated_at: Date.now() - 120_000 }),
    ])
    axios.get.mockResolvedValueOnce(answer('stale'))
    await store.revalidateBriefing('fresh', { maxAge: 60_000 })
    expect(axios.get).not.toHaveBeenCalled()
    await store.revalidateBriefing('stale', { maxAge: 60_000 })
    expect(axios.get).toHaveBeenCalledTimes(1)
  })

  it('an agent not on this roster is a no-op (never a fetch for a name the caller cannot see)', async () => {
    const store = portalStore([readyCard('scout')])
    await store.revalidateBriefing('someone-elses-agent')
    expect(axios.get).not.toHaveBeenCalled()
  })

  it('a hydration stamps briefing_hydrated_at', async () => {
    const store = portalStore([readyCard('scout', { briefing_hydrated_at: undefined })])
    axios.get.mockResolvedValueOnce(answer('scout'))
    const before = Date.now()
    await store.revalidateBriefing('scout')
    expect(store.agents[0].briefing_hydrated_at).toBeGreaterThanOrEqual(before)
  })
})
