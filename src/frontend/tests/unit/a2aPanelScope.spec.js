// @vitest-environment jsdom
/**
 * trinity-enterprise#838 — the A2A tab's "Who can reach it" controls.
 *
 * Mounted over a stubbed agents store. Pins: the scope select only on an
 * exposed agent; internal scope shows the keyless toggle and, when the server
 * gives one, the URL to register; changing either sends ONLY that field with
 * the current exposure, so the other keeps its value; public hides both.
 */
import { describe, expect, it, vi, beforeEach } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'

const store = {
  getA2aConfig: vi.fn(),
  setA2aExposure: vi.fn(),
  getA2aCard: vi.fn(async () => ({ skills: [] })),
  getAgentInfo: vi.fn(async () => ({ capabilities: [] })),
}
vi.mock('@/stores/agents', () => ({ useAgentsStore: () => store }))

import A2aPanel from '@/components/A2aPanel.vue'

function cfg(over = {}) {
  return {
    a2a_exposed: true, a2a_scope: 'public', a2a_keyless_internal: true,
    internal_endpoint_url: null, inbound_allowlist: [], outbound_endpoints: [],
    curated_skills: null, ...over,
  }
}

async function mountWith(config) {
  store.getA2aConfig.mockResolvedValue(config)
  const w = mount(A2aPanel, { props: { agentName: 'beta' } })
  await flushPromises()
  return w
}

beforeEach(() => { Object.values(store).forEach((f) => f.mockClear?.()) })

describe('A2aPanel — who can reach it (ent#838)', () => {
  it('is not offered while the agent is not exposed', async () => {
    const w = await mountWith(cfg({ a2a_exposed: false }))
    expect(w.find('[data-testid="a2a-scope"]').exists()).toBe(false)
  })

  it('public scope: the select, no keyless toggle, no URL', async () => {
    const w = await mountWith(cfg())
    expect(w.get('[data-testid="a2a-scope-select"]').element.value).toBe('public')
    expect(w.find('[data-testid="a2a-keyless"]').exists()).toBe(false)
    expect(w.find('[data-testid="a2a-internal-url"]').exists()).toBe(false)
  })

  it('internal scope: the keyless toggle and the URL to register', async () => {
    const w = await mountWith(cfg({ a2a_scope: 'internal', internal_endpoint_url: 'http://inst.example.net/a2a/beta' }))
    expect(w.get('[data-testid="a2a-keyless"]').attributes('aria-checked')).toBe('true')
    expect(w.get('[data-testid="a2a-internal-url"]').element.value).toBe('http://inst.example.net/a2a/beta')
  })

  it('internal with no internal address yet says who sets it', async () => {
    const w = await mountWith(cfg({ a2a_scope: 'internal' }))
    expect(w.find('[data-testid="a2a-internal-url"]').exists()).toBe(false)
    expect(w.text()).toContain('An admin sets this instance')
  })

  it('choosing a scope sends only the scope, with the current exposure', async () => {
    const w = await mountWith(cfg())
    store.setA2aExposure.mockResolvedValue(cfg({ a2a_scope: 'internal' }))
    await w.get('[data-testid="a2a-scope-select"]').setValue('internal')
    await flushPromises()
    expect(store.setA2aExposure).toHaveBeenCalledWith('beta', true, { scope: 'internal' })
    expect(w.find('[data-testid="a2a-keyless"]').exists()).toBe(true)
  })

  it('the keyless toggle sends only keyless', async () => {
    const w = await mountWith(cfg({ a2a_scope: 'internal' }))
    store.setA2aExposure.mockResolvedValue(cfg({ a2a_scope: 'internal', a2a_keyless_internal: false }))
    await w.get('[data-testid="a2a-keyless"]').trigger('click')
    await flushPromises()
    expect(store.setA2aExposure).toHaveBeenCalledWith('beta', true, { keyless: false })
    expect(w.get('[data-testid="a2a-keyless"]').attributes('aria-checked')).toBe('false')
  })
})
