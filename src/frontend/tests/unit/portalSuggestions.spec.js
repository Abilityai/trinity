// @vitest-environment jsdom
/**
 * trinity-enterprise#465 — Workspace suggestions, mounted.
 *
 * What only a mount proves: each Accept does its OWN thing and never sends
 * (prefill emits text, a section emits its name, a configure item opens the
 * operator path in a new tab and nothing else), Dismiss hides at once and
 * restores with the error beside it when the write fails, the four states look
 * different (loading / failed / nothing-to-suggest / list), and the compact
 * placement renders no chrome at all unless it has something to show.
 * Mounted against the real store with HTTP mocked (#2918).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
})
vi.mock('@/stores/auth', async () => {
  const { ref } = await import('vue')
  const authed = ref(true)
  return { useAuthStore: () => ({ get isAuthenticated() { return authed.value }, get authHeader() { return { Authorization: 'Bearer jwt' } } }) }
})
vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})
vi.mock('@/api', () => ({ default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }))

import axios from 'axios'
import { useClientPortalStore } from '@/stores/clientPortal'
import PortalSuggestions from '@/components/portal/PortalSuggestions.vue'

const AGENT = 'sales-companion'
const URL = `/api/enterprise/client-portal/agents/${AGENT}/suggestions`
const FEEDBACK = `${URL}/feedback`

function item(over = {}) {
  return {
    key: 'unused_playbook:weekly-report', kind: 'invoke', source: 'capability', door: 'platform',
    title: 'Weekly report', signal: "You haven't run /weekly-report yet", description: 'Summarise the week',
    action: { type: 'prefill', value: '/weekly-report ' },
    ...over,
  }
}

function payload(suggestions, over = {}) {
  return { agent_name: AGENT, suggestions, total: suggestions.length, capabilities: 'available', basis: 'history', ...over }
}

function mountIt(props = {}) {
  return mount(PortalSuggestions, { props: { agentName: AGENT, ...props } })
}

beforeEach(() => {
  setActivePinia(createPinia())
  axios.get.mockReset()
  axios.post.mockReset()
})

describe('PortalSuggestions', () => {
  it('shows the evidence and Accept on a playbook prefills, never sends', async () => {
    axios.get.mockResolvedValue({ data: payload([item()]) })
    axios.post.mockResolvedValue({ data: { ok: true } })
    const w = mountIt()
    await flushPromises()
    expect(axios.get).toHaveBeenCalledWith(URL, expect.anything())
    expect(w.get('[data-testid="portal-suggestion-signal"]').text()).toBe("You haven't run /weekly-report yet")
    await w.get('[data-testid="portal-suggestion-accept"]').trigger('click')
    expect(w.emitted('use-playbook')).toEqual([['/weekly-report ']])
    expect(axios.post).toHaveBeenCalledWith(FEEDBACK, { key: 'unused_playbook:weekly-report', action: 'accept' }, expect.anything())
    // Still listed: accept records usefulness, it does not hide the item.
    expect(w.findAll('[data-testid="portal-suggestion"]')).toHaveLength(1)
  })

  it('a section item emits its name; open_chat emits open-chat', async () => {
    axios.get.mockResolvedValue({ data: payload([
      item({ key: 'asks', source: 'asks', title: 'Answer what this agent asked you', signal: '2 questions waiting on you', action: { type: 'open_section', value: 'asks' } }),
      item({ key: 'dormant', source: 'usage', title: 'Pick up where you left off', signal: 'Your last conversation was Sep 3', action: { type: 'open_chat', value: null } }),
    ]) })
    axios.post.mockResolvedValue({ data: { ok: true } })
    const w = mountIt()
    await flushPromises()
    const accepts = w.findAll('[data-testid="portal-suggestion-accept"]')
    expect(accepts[0].text()).toBe('Show questions')
    await accepts[0].trigger('click')
    await accepts[1].trigger('click')
    expect(w.emitted('open-section')).toEqual([['asks']])
    expect(w.emitted('open-chat')).toHaveLength(1)
    expect(w.emitted('use-playbook')).toBeUndefined()
  })

  it('a configure item opens the operator path in a new tab, and only an operator path', async () => {
    const open = vi.spyOn(window, 'open').mockImplementation(() => null)
    axios.get.mockResolvedValue({ data: payload([
      item({ key: 'autonomy_held', kind: 'configure', source: 'schedules', door: 'owner_or_admin',
             title: 'Turn on autonomy, or pause these schedules', signal: "2 schedules won't run — autonomy is off",
             action: { type: 'link', value: `/agents/${AGENT}?tab=schedules` } }),
      item({ key: 'schedule_failing:x', kind: 'configure', source: 'schedules', door: 'owner_or_admin',
             title: 'Check it', signal: 'Failed 3 runs in a row', action: { type: 'link', value: 'https://evil.example.com' } }),
    ]) })
    axios.post.mockResolvedValue({ data: { ok: true } })
    const w = mountIt()
    await flushPromises()
    const accepts = w.findAll('[data-testid="portal-suggestion-accept"]')
    expect(accepts[0].text()).toBe('Open schedules')
    await accepts[0].trigger('click')
    await accepts[1].trigger('click')
    expect(open).toHaveBeenCalledTimes(1)
    expect(open).toHaveBeenCalledWith(`/agents/${AGENT}?tab=schedules`, '_blank', 'noopener')
    expect(w.emitted('use-playbook')).toBeUndefined()
    open.mockRestore()
  })

  it('dismiss hides at once; a failed write restores it with the error beside it', async () => {
    axios.get.mockResolvedValue({ data: payload([item(), item({ key: 'unused_playbook:triage', title: 'Triage', signal: "You haven't run /triage yet" })]) })
    let reject
    axios.post.mockImplementation(() => new Promise((_, r) => { reject = r }))
    const w = mountIt()
    await flushPromises()
    await w.findAll('[data-testid="portal-suggestion-dismiss"]')[0].trigger('click')
    expect(w.findAll('[data-testid="portal-suggestion"]').map((n) => n.attributes('data-key'))).toEqual(['unused_playbook:triage'])
    reject(new Error('boom'))
    await flushPromises()
    const rows = w.findAll('[data-testid="portal-suggestion"]')
    expect(rows.map((n) => n.attributes('data-key'))).toEqual(['unused_playbook:weekly-report', 'unused_playbook:triage'])
    expect(rows[0].text()).toContain("Couldn't dismiss that")
  })

  it('a successful dismiss stays gone and records the dismiss', async () => {
    axios.get.mockResolvedValue({ data: payload([item()]) })
    axios.post.mockResolvedValue({ data: { ok: true } })
    const w = mountIt()
    await flushPromises()
    await w.get('[data-testid="portal-suggestion-dismiss"]').trigger('click')
    await flushPromises()
    expect(w.findAll('[data-testid="portal-suggestion"]')).toHaveLength(0)
    expect(w.find('[data-testid="portal-suggestions-empty"]').exists()).toBe(true)
    expect(axios.post).toHaveBeenCalledWith(FEEDBACK, { key: 'unused_playbook:weekly-report', action: 'dismiss' }, expect.anything())
  })

  it('loading, failed and nothing-to-suggest are three different renders', async () => {
    let resolve
    axios.get.mockImplementationOnce(() => new Promise((r) => { resolve = r }))
    const w = mountIt()
    expect(w.find('[aria-busy="true"]').exists()).toBe(true)
    resolve({ data: payload([]) })
    await flushPromises()
    expect(w.find('[aria-busy="true"]').exists()).toBe(false)
    expect(w.get('[data-testid="portal-suggestions-empty"]').text()).toBe('Nothing to suggest right now.')

    setActivePinia(createPinia())
    axios.get.mockRejectedValueOnce(new Error('down'))
    const f = mountIt()
    await flushPromises()
    expect(f.text()).toContain("Couldn't load suggestions")
    expect(f.find('[data-testid="portal-suggestions-empty"]').exists()).toBe(false)
  })

  it('says when the list can only be capabilities, and when capabilities are unknown', async () => {
    axios.get.mockResolvedValue({ data: payload([item()], { basis: 'capabilities_only', capabilities: 'unavailable' }) })
    const w = mountIt()
    await flushPromises()
    expect(w.find('[data-testid="portal-suggestions-basis"]').exists()).toBe(true)
    expect(w.find('[data-testid="portal-suggestions-capabilities"]').exists()).toBe(true)
  })

  it('never claims "capabilities only" over a list with no capability item (found live on ops-watcher)', async () => {
    axios.get.mockResolvedValue({ data: payload([
      item({ key: 'autonomy_held', kind: 'configure', source: 'schedules', door: 'owner_or_admin',
             title: 'Turn on autonomy, or pause these schedules', signal: "2 schedules won't run — autonomy is off",
             action: { type: 'link', value: `/agents/${AGENT}?tab=schedules` } }),
    ], { basis: 'capabilities_only', capabilities: 'none' }) })
    const w = mountIt()
    await flushPromises()
    expect(w.find('[data-testid="portal-suggestions-basis"]').exists()).toBe(false)
  })

  it('compact renders nothing unless there is something to show, and respects the limit', async () => {
    axios.get.mockResolvedValueOnce({ data: payload([]) })
    const empty = mountIt({ compact: true, limit: 3 })
    await flushPromises()
    expect(empty.find('[data-testid="portal-suggestions"]').exists()).toBe(false)

    setActivePinia(createPinia())
    axios.get.mockRejectedValueOnce(new Error('down'))
    const failed = mountIt({ compact: true, limit: 3 })
    await flushPromises()
    expect(failed.find('[data-testid="portal-suggestions"]').exists()).toBe(false)

    setActivePinia(createPinia())
    const many = ['a', 'b', 'c', 'd'].map((n) => item({ key: `unused_playbook:${n}`, title: n.toUpperCase() }))
    axios.get.mockResolvedValueOnce({ data: payload(many) })
    const full = mountIt({ compact: true, limit: 3 })
    await flushPromises()
    expect(full.findAll('[data-testid="portal-suggestion"]')).toHaveLength(3)
  })

  it('two placements share one fetch while it is fresh', async () => {
    axios.get.mockResolvedValue({ data: payload([item()]) })
    mountIt()
    await flushPromises()
    mountIt({ compact: true, limit: 3 })
    await flushPromises()
    expect(axios.get).toHaveBeenCalledTimes(1)
    const store = useClientPortalStore()
    await store.loadAgentSuggestions(AGENT, { force: true })
    expect(axios.get).toHaveBeenCalledTimes(2)
  })
})
