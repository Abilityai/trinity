// @vitest-environment jsdom
/**
 * trinity-enterprise#637 — what an agent remembers about you, and Undo.
 *
 * A scheduled run addressed to the viewer may now write their memory; the
 * Workspace has to show that it did (which run, when, what it left) and let
 * them undo it. Undo is a VERB on a component, so this spec MOUNTS
 * `PortalAgentMemory.vue` against the real store with the HTTP client mocked
 * (#2918: a regex over the SFC cannot prove a click reaches the store) and
 * drives the store's load / undo / refusal paths directly.
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
  return {
    useAuthStore: () => ({
      get isAuthenticated() { return authed.value },
      get authHeader() { return { Authorization: 'Bearer jwt' } },
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
import { useClientPortalStore } from '@/stores/clientPortal'
import PortalAgentMemory from '@/components/portal/PortalAgentMemory.vue'

const AGENT = 'analyst'
const MEMORY_URL = `/api/enterprise/client-portal/agents/${AGENT}/memory`

function payload(over = {}) {
  return {
    agent_name: AGENT,
    notes: 'Open loop: budget sign-off. Commitment: Friday memo.',
    updated_at: new Date(Date.now() - 3600_000).toISOString(),
    writes: [
      { id: 'w2', kind: 'scheduled_run', execution_id: 'e2', schedule_name: 'Daily brief',
        written_at: new Date(Date.now() - 3600_000).toISOString(), undone_at: null, undoable: true,
        notes: 'Open loop: budget sign-off. Commitment: Friday memo.', previous_notes: 'Open loop: budget sign-off.' },
      { id: 'w1', kind: 'conversation', execution_id: 'e1', schedule_name: null,
        written_at: new Date(Date.now() - 86400_000).toISOString(), undone_at: null, undoable: false,
        notes: 'Open loop: budget sign-off.', previous_notes: '' },
    ],
    ...over,
  }
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('PortalAgentMemory (mounted)', () => {
  it('shows the notes, names the scheduled run that wrote them, and offers Undo only where the server says so', async () => {
    axios.get.mockResolvedValueOnce({ data: payload() })
    const w = mount(PortalAgentMemory, { props: { agentName: AGENT } })
    expect(w.find('[aria-busy="true"]').exists()).toBe(true)          // skeleton first, never optimistic
    await flushPromises()

    expect(axios.get).toHaveBeenCalledWith(MEMORY_URL, expect.anything())
    expect(w.get('[data-testid="portal-agent-memory-notes"]').text()).toContain('Friday memo')
    const scheduled = w.findAll('[data-testid="portal-memory-write-scheduled"]')
    expect(scheduled).toHaveLength(1)
    expect(scheduled[0].text()).toContain('Scheduled run · Daily brief')
    expect(scheduled[0].text()).toContain('Friday memo')                // what it LEFT
    expect(w.findAll('[data-testid="portal-memory-write"]')).toHaveLength(1)
    // One Undo: the latest write. The older one is not undoable and shows none.
    expect(w.findAll('[data-testid="portal-memory-undo"]')).toHaveLength(1)
  })

  it('Undo posts to the write, then re-reads so the notes and the list come from one read', async () => {
    axios.get.mockResolvedValueOnce({ data: payload() })
    const w = mount(PortalAgentMemory, { props: { agentName: AGENT } })
    await flushPromises()

    axios.post.mockResolvedValueOnce({ data: { write_id: 'w2', notes: 'Open loop: budget sign-off.' } })
    axios.get.mockResolvedValueOnce({ data: payload({
      notes: 'Open loop: budget sign-off.',
      writes: [
        { ...payload().writes[0], undone_at: new Date().toISOString(), undoable: false },
        { ...payload().writes[1], undoable: true },
      ],
    }) })
    await w.get('[data-testid="portal-memory-undo"]').trigger('click')
    await flushPromises()

    expect(axios.post).toHaveBeenCalledWith(`${MEMORY_URL}/writes/w2/undo`, null, expect.anything())
    expect(w.get('[data-testid="portal-agent-memory-notes"]').text()).toBe('Open loop: budget sign-off.')
    expect(w.get('[data-testid="portal-memory-write-scheduled"]').text()).toContain('undone')
    // Undo moved to the now-latest open write.
    expect(w.findAll('[data-testid="portal-memory-undo"]')).toHaveLength(1)
  })

  it('a refused Undo is shown next to the control with the server\'s reason, never swallowed', async () => {
    axios.get.mockResolvedValueOnce({ data: payload() })
    const w = mount(PortalAgentMemory, { props: { agentName: AGENT } })
    await flushPromises()

    axios.post.mockRejectedValueOnce({ response: { status: 409, data: { detail: {
      code: 'not_latest', message: 'A later change exists — undo the most recent one first, so nothing you have not looked at is discarded.',
    } } } })
    await w.get('[data-testid="portal-memory-undo"]').trigger('click')
    await flushPromises()
    expect(w.text()).toContain('undo the most recent one first')
    expect(useClientPortalStore().memoryUndoing).toBeNull()                 // the button is released
  })

  it('an empty memory says what would fill it; a failed load is a failed state, not an empty one', async () => {
    axios.get.mockResolvedValueOnce({ data: payload({ notes: '', writes: [] }) })
    const empty = mount(PortalAgentMemory, { props: { agentName: AGENT } })
    await flushPromises()
    expect(empty.text()).toContain('Nothing yet')
    expect(empty.text()).toContain('scheduled run addressed to you')

    setActivePinia(createPinia())
    axios.get.mockRejectedValueOnce(new Error('network'))
    const failed = mount(PortalAgentMemory, { props: { agentName: AGENT } })
    await flushPromises()
    expect(failed.text()).toContain("Couldn't load memory")
    expect(failed.text()).not.toContain('Nothing yet')
  })
})

describe('clientPortal store — memory slice', () => {
  it('a response that lands after an agent switch is discarded, never shown under the new name', async () => {
    const store = useClientPortalStore()
    let resolveA
    axios.get.mockImplementationOnce(() => new Promise((r) => { resolveA = r }))
    const a = store.loadAgentMemory('agent-a')
    axios.get.mockResolvedValueOnce({ data: payload({ agent_name: 'agent-b', notes: 'B notes', writes: [] }) })
    await store.loadAgentMemory('agent-b')
    resolveA({ data: payload({ agent_name: 'agent-a', notes: 'A notes', writes: [] }) })
    await a
    expect(store.memoryAgent).toBe('agent-b')
    expect(store.memory.notes).toBe('B notes')
  })
})
