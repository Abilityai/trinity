// @vitest-environment jsdom
/**
 * trinity-enterprise#885 — the A2A tab has no "Who can reach it" controls.
 *
 * The keyless trusted-network door and the internal-only scope are gone, so
 * the panel offers no scope select, no keyless toggle and no internal URL —
 * even when a database row from rc1–rc3 still makes the config carry
 * `a2a_scope: 'internal'` and an `internal_endpoint_url`. The card URL is
 * always the public origin's, and the exposure toggle PUTs exactly
 * `{ enabled }`. Mounted over the REAL agents store with axios stubbed, so
 * the wire body is the store's, not a mock's.
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
import { useAgentsStore } from '../../src/stores/agents'
import A2aPanel from '../../src/components/A2aPanel.vue'

// What an rc1–rc3 row still yields: exposed, internal scope, keyless on.
function staleConfig(over = {}) {
  return {
    a2a_exposed: true, a2a_scope: 'internal', a2a_keyless_internal: true,
    internal_endpoint_url: 'http://inst.example.net/a2a/beta',
    inbound_allowlist: [], outbound_endpoints: [], curated_skills: null, ...over,
  }
}

async function mountWith(config) {
  axios.get.mockImplementation(async (url) => {
    if (url.endsWith('/config')) return { data: config }
    return { data: { skills: [], capabilities: [] } }
  })
  const w = mount(A2aPanel, { props: { agentName: 'beta' } })
  await flushPromises()
  return w
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('A2aPanel — no scope controls (ent#885)', () => {
  it('offers no scope select, keyless toggle or internal URL on an exposed agent', async () => {
    const w = await mountWith(staleConfig())
    expect(w.find('[data-testid="a2a-scope"]').exists()).toBe(false)
    expect(w.find('[data-testid="a2a-scope-select"]').exists()).toBe(false)
    expect(w.find('[data-testid="a2a-keyless"]').exists()).toBe(false)
    expect(w.find('[data-testid="a2a-internal-url"]').exists()).toBe(false)
    expect(w.text()).not.toContain('Who can reach it')
  })

  it('shows the public-origin card URL even for a stale internal row', async () => {
    const w = await mountWith(staleConfig())
    const expected = `${window.location.origin}/a2a/beta/.well-known/agent-card.json`
    expect(w.text()).toContain(expected)
    expect(w.text()).not.toContain('inst.example.net')
  })

  it('toggling exposure PUTs exactly { enabled }', async () => {
    const w = await mountWith(staleConfig({ a2a_exposed: false }))
    axios.put.mockResolvedValue({ data: staleConfig() })
    await w.get('input[type="checkbox"]').setValue(true)
    await flushPromises()
    expect(axios.put).toHaveBeenCalledTimes(1)
    const [url, body] = axios.put.mock.calls[0]
    expect(url).toBe('/api/enterprise/a2a/beta/exposure')
    expect(body).toEqual({ enabled: true })
  })

  it('the store sends no scope or keyless field whatever it is handed', async () => {
    axios.put.mockResolvedValue({ data: {} })
    await useAgentsStore().setA2aExposure('beta', false, { scope: 'internal', keyless: true })
    expect(axios.put.mock.calls[0][1]).toEqual({ enabled: false })
  })
})
