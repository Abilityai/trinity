// @vitest-environment jsdom
/**
 * #3702 — switching exposure ON from the A2A tab must load the template
 * capabilities, or the "Choose…" control that curates the advertised skills
 * (ent#180) stays hidden until a reload.
 *
 * `load()` fetches capabilities only when the agent is already exposed at
 * mount; a panel mounted on an unexposed agent and then toggled on reached
 * `onToggle`, which loaded the card skills but never the capabilities.
 * Mounted over the REAL agents store with axios stubbed.
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
import A2aPanel from '../../src/components/A2aPanel.vue'

const CAPS = ['web-search', 'market-research', 'report-generation']

function config(over = {}) {
  return {
    a2a_exposed: false, inbound_allowlist: [], outbound_endpoints: [],
    curated_skills: null, ...over,
  }
}

function stubApi(initial) {
  axios.get.mockImplementation(async (url) => {
    if (url.endsWith('/config')) return { data: initial }
    if (url.endsWith('/info')) return { data: { capabilities: CAPS } }
    if (url.includes('agent-card')) return { data: { skills: CAPS.map(id => ({ id, name: id })) } }
    return { data: {} }
  })
  axios.put.mockImplementation(async (url, body) => {
    if (url.endsWith('/exposure')) return { data: config({ a2a_exposed: body.enabled }) }
    return { data: {} }
  })
}

const chooseButton = w => w.findAll('button').find(b => b.text().startsWith('Choose'))

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('A2aPanel — Choose… after exposing from the tab (#3702)', () => {
  it('shows Choose… once exposure is switched on, without a reload', async () => {
    stubApi(config({ a2a_exposed: false }))
    const w = mount(A2aPanel, { props: { agentName: 'alpha' } })
    await flushPromises()
    expect(chooseButton(w)).toBeUndefined()

    await w.find('input[type="checkbox"]').setValue(true)
    await flushPromises()

    expect(axios.put).toHaveBeenCalledWith(
      '/api/enterprise/a2a/alpha/exposure', { enabled: true }, expect.anything(),
    )
    expect(chooseButton(w)).toBeDefined()
    expect(w.text()).toContain(`Advertising all ${CAPS.length} capabilities (default).`)
  })

  it('still shows Choose… for an agent exposed at mount', async () => {
    stubApi(config({ a2a_exposed: true }))
    const w = mount(A2aPanel, { props: { agentName: 'alpha' } })
    await flushPromises()
    expect(chooseButton(w)).toBeDefined()
  })
})
