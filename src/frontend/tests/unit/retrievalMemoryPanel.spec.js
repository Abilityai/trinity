// @vitest-environment jsdom
/**
 * trinity-enterprise#1022 — retrieval memory's two switches, MOUNTED.
 *
 * The platform card (Settings → General) writes the plain system setting
 * `trinity_memory_enabled` through the settings store and shows an honest
 * status line; the per-agent row PUTs `{ opt_out }` and says "Platform switch
 * is off" rather than pretending a saved choice is live. Both are store /
 * wire writes behind a toggle, so a regex over the SFC proves nothing (#2829,
 * #2918): the real components are driven with @vue/test-utils.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const inst = { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } } }
  return { default: { ...inst, create: () => inst } }
})
vi.mock('../../src/api', () => ({
  default: { get: vi.fn(), put: vi.fn(), post: vi.fn(), delete: vi.fn() },
}))

import axios from 'axios'
import api from '../../src/api'
import RetrievalMemoryPanel from '../../src/components/settings/RetrievalMemoryPanel.vue'
import RetrievalMemoryToggle from '../../src/components/agent-settings/RetrievalMemoryToggle.vue'

function platformStatus(over = {}) {
  return {
    enabled: false, available: false, reason: null, embedding_model: 'm', backend: 'sqlite',
    fact_count: 0, last_retained_at: null, daily_retain_limit: 200,
    extractor: 'auto', platform_key: true, ...over,
  }
}

describe('RetrievalMemoryPanel (Settings → General)', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('is off by default and says nothing is captured', async () => {
    api.get.mockResolvedValue({ data: platformStatus() })
    const w = mount(RetrievalMemoryPanel)
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/settings/memory')
    expect(w.get('[data-testid="retrieval-memory-status"]').text()).toMatch(/Currently off/)
    expect(w.get('[data-testid="retrieval-memory-toggle"]').attributes('aria-checked')).toBe('false')
  })

  it('turning it on writes the plain setting through the store and reloads the status', async () => {
    api.get.mockResolvedValueOnce({ data: platformStatus() })
    axios.put.mockResolvedValue({ data: { key: 'trinity_memory_enabled', value: 'true' } })
    api.get.mockResolvedValueOnce({ data: platformStatus({ enabled: true, available: true, fact_count: 3 }) })
    const w = mount(RetrievalMemoryPanel)
    await flushPromises()
    await w.get('[data-testid="retrieval-memory-toggle"]').trigger('click')
    await flushPromises()
    expect(axios.put).toHaveBeenCalledWith('/api/settings/trinity_memory_enabled', { value: 'true' })
    expect(w.get('[data-testid="retrieval-memory-status"]').text()).toMatch(/3 facts stored/)
    expect(w.get('[data-testid="retrieval-memory-toggle"]').attributes('aria-checked')).toBe('true')
  })

  // trinity-enterprise#1027 — where extraction runs.
  it('shows the current extractor mode in the select', async () => {
    api.get.mockResolvedValue({ data: platformStatus({ extractor: 'agent' }) })
    const w = mount(RetrievalMemoryPanel)
    await flushPromises()
    expect(w.get('[data-testid="retrieval-memory-extractor"]').element.value).toBe('agent')
  })

  it('changing the extractor writes trinity_memory_extractor through the store and reloads', async () => {
    api.get.mockResolvedValueOnce({ data: platformStatus({ extractor: 'auto' }) })
    axios.put.mockResolvedValue({ data: { key: 'trinity_memory_extractor', value: 'platform' } })
    api.get.mockResolvedValueOnce({ data: platformStatus({ extractor: 'platform' }) })
    const w = mount(RetrievalMemoryPanel)
    await flushPromises()
    await w.get('[data-testid="retrieval-memory-extractor"]').setValue('platform')
    await flushPromises()
    expect(axios.put).toHaveBeenCalledWith('/api/settings/trinity_memory_extractor', { value: 'platform' })
    expect(w.get('[data-testid="retrieval-memory-extractor"]').element.value).toBe('platform')
  })

  it('platform-only without a platform key says nothing is captured', async () => {
    api.get.mockResolvedValue({ data: platformStatus({ enabled: true, available: true, extractor: 'platform', platform_key: false }) })
    const w = mount(RetrievalMemoryPanel)
    await flushPromises()
    expect(w.get('[data-testid="retrieval-memory-extractor-status"]').text()).toMatch(/No platform key — nothing is captured/)
  })

  it('agent-only says only running agents capture', async () => {
    api.get.mockResolvedValue({ data: platformStatus({ enabled: true, available: true, extractor: 'agent' }) })
    const w = mount(RetrievalMemoryPanel)
    await flushPromises()
    expect(w.get('[data-testid="retrieval-memory-extractor-status"]').text()).toMatch(/Only running agents capture/)
  })

  it('a failed extractor change shows an inline error and reloads the saved value', async () => {
    api.get.mockResolvedValue({ data: platformStatus({ extractor: 'auto' }) })
    axios.put.mockRejectedValue(new Error('boom'))
    const w = mount(RetrievalMemoryPanel)
    await flushPromises()
    await w.get('[data-testid="retrieval-memory-extractor"]').setValue('agent')
    await flushPromises()
    expect(w.find('[data-testid="inline-error"]').exists()).toBe(true)
    expect(w.get('[data-testid="retrieval-memory-extractor"]').element.value).toBe('auto')
  })

  it('an "on but unavailable" platform names the reason instead of reporting success', async () => {
    api.get.mockResolvedValue({ data: platformStatus({ enabled: true, available: false, reason: 'embedding model could not load' }) })
    const w = mount(RetrievalMemoryPanel)
    await flushPromises()
    expect(w.get('[data-testid="retrieval-memory-status"]').text()).toMatch(/unavailable — embedding model could not load/)
  })
})

describe('RetrievalMemoryToggle (agent Guardrails pane)', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('says the platform switch is off rather than pretending the choice is live', async () => {
    api.get.mockResolvedValue({ data: { enabled_platform: false, opt_out: false, available: false, reason: null, fact_count: 0 } })
    const w = mount(RetrievalMemoryToggle, { props: { agentName: 'beta' } })
    await flushPromises()
    expect(api.get).toHaveBeenCalledWith('/api/agents/beta/retrieval-memory')
    expect(w.get('[data-testid="retrieval-memory-agent-status"]').text()).toMatch(/Platform switch is off/)
  })

  it('switching the agent off PUTs opt_out: true and reflects the answer', async () => {
    api.get.mockResolvedValue({ data: { enabled_platform: true, opt_out: false, available: true, reason: null, fact_count: 2 } })
    api.put.mockResolvedValue({ data: { enabled_platform: true, opt_out: true, available: true, reason: null, fact_count: 2 } })
    const notify = vi.fn()
    const w = mount(RetrievalMemoryToggle, { props: { agentName: 'beta', notify } })
    await flushPromises()
    expect(w.get('[data-testid="retrieval-memory-agent-toggle"]').attributes('aria-checked')).toBe('true')
    await w.get('[data-testid="retrieval-memory-agent-toggle"]').trigger('click')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/agents/beta/retrieval-memory', { opt_out: true })
    expect(w.get('[data-testid="retrieval-memory-agent-status"]').text()).toMatch(/Kept out/)
    expect(notify).toHaveBeenCalled()
  })
})
