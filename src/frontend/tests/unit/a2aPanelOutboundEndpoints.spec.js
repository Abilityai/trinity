// @vitest-environment jsdom
/**
 * trinity-enterprise#898 — the agent's A2A tab no longer registers outbound
 * endpoints.
 *
 * Its form wrote the per-agent enterprise table, which `call_a2a_agent` never
 * reads: the call path resolves only the platform-wide registry behind
 * `/api/settings/a2a-endpoints` (#736). So an endpoint registered from the tab
 * could never be called. The tab now points at Settings › Integrations, where
 * `A2AEndpointsPanel` writes the registry the call path reads
 * (`a2aEndpointsPanel.mount.spec.js` pins that wire; the backend half — that
 * the Settings PUT is resolvable — is
 * `test_ent761_outbound_control_oss.py::test_register_list_resolve_remove_with_no_provider_registered`).
 * Rows already in the per-agent table are listed as not callable, with a
 * Remove so an operator can clear them.
 *
 * Mounted over the REAL agents store with axios stubbed, so the wire calls are
 * the store's, not a mock's.
 */
import { readFileSync, readdirSync, statSync } from 'node:fs'
import { join, relative } from 'node:path'
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount, RouterLinkStub } from '@vue/test-utils'
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

function config(over = {}) {
  return {
    a2a_exposed: true, inbound_allowlist: [], outbound_endpoints: [],
    curated_skills: null, ...over,
  }
}

const LEGACY = { id: 'ep-1', name: 'acme', url: 'https://acme.example/a2a', has_credentials: true }

async function mountWith(cfg) {
  axios.get.mockImplementation(async (url) => {
    if (url.endsWith('/config')) return { data: cfg }
    return { data: { skills: [], capabilities: [] } }
  })
  const w = mount(A2aPanel, {
    props: { agentName: 'beta' },
    global: { stubs: { RouterLink: RouterLinkStub } },
  })
  await flushPromises()
  return w
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

describe('A2aPanel — outbound endpoints live in Settings (ent#898)', () => {
  it('offers no form that registers an endpoint from the agent tab', async () => {
    const w = await mountWith(config())
    expect(w.find('[data-testid="a2a-endpoint-form"]').exists()).toBe(false)
    expect(w.findAll('button').map(b => b.text())).not.toContain('Register endpoint')
    expect(axios.post).not.toHaveBeenCalledWith(
      expect.stringMatching(/\/api\/enterprise\/a2a\/[^/]+\/endpoints$/),
      expect.anything(), expect.anything(),
    )
  })

  it('points at Settings › Integrations, where call_a2a_agent reads from', async () => {
    const w = await mountWith(config())
    const link = w.findComponent('[data-testid="a2a-endpoints-settings-link"]')
    expect(link.exists()).toBe(true)
    expect(link.props('to')).toBe('/settings?tab=integrations')
  })

  it('lists a per-agent row as not callable and lets the operator remove it', async () => {
    axios.delete.mockResolvedValue({ data: { status: 'removed', endpoint_id: 'ep-1' } })
    const w = await mountWith(config({ outbound_endpoints: [LEGACY] }))

    const legacy = w.find('[data-testid="a2a-legacy-endpoints"]')
    expect(legacy.exists()).toBe(true)
    expect(legacy.text()).toContain('acme')
    expect(legacy.text()).toMatch(/not callable/i)

    await legacy.find('[data-testid="a2a-legacy-endpoint-remove"]').trigger('click')
    await flushPromises()
    expect(axios.delete).toHaveBeenCalledWith(
      '/api/enterprise/a2a/beta/endpoints/ep-1', expect.anything(),
    )
  })

  it('shows no legacy block when the per-agent table is empty', async () => {
    const w = await mountWith(config())
    expect(w.find('[data-testid="a2a-legacy-endpoints"]').exists()).toBe(false)
  })

  it('the agents store has no way left to write the per-agent table', () => {
    expect(useAgentsStore().registerA2aEndpoint).toBeUndefined()
  })

  it('no UI source POSTs to the per-agent endpoints route', () => {
    const src = join(__dirname, '..', '..', 'src')
    const offenders = []
    const walk = (dir) => {
      for (const name of readdirSync(dir)) {
        const p = join(dir, name)
        if (statSync(p).isDirectory()) { walk(p); continue }
        if (!/\.(vue|js|ts)$/.test(name)) continue
        const text = readFileSync(p, 'utf8')
        if (/\.post\(\s*`\/api\/enterprise\/a2a\/\$\{[^}]+\}\/endpoints`/.test(text)) {
          offenders.push(relative(src, p))
        }
      }
    }
    walk(src)
    expect(offenders).toEqual([])
  })
})
