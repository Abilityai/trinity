// @vitest-environment jsdom
/**
 * trinity-enterprise#1101 — the External MCP access switch in Settings → MCP
 * Keys, mounted.
 *
 * What only a mounted tab proves:
 *  - the switch is offered to an admin only; anyone else reads the state;
 *  - the switch writes `PUT /api/settings/mcp-external-access {enabled}` and
 *    Clear sends the DELETE, shown only while a Settings value exists;
 *  - the source line says which tier decided (Settings vs environment);
 *  - when off, the connection snippet is replaced by a callout naming the
 *    refusal, never a URL an outside client cannot reach;
 *  - the URL is the server's `advertised_url` — there is no client-side
 *    `:8080` guess left to disagree with it.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const apiMock = vi.hoisted(() => ({
  get: vi.fn(),
  put: vi.fn(),
  delete: vi.fn(),
}))
vi.mock('@/api', () => ({ default: apiMock }))
vi.mock('axios', () => ({ default: { get: vi.fn(async () => ({ data: { url: null, default_url: '' } })) } }))

import McpKeysTab from '@/components/settings/McpKeysTab.vue'
import { useAuthStore } from '@/stores/auth'

const ADVERTISED = 'https://trinity.example.com/mcp'

function state(overrides = {}) {
  return {
    enabled: true,
    source: 'settings',
    env_value: null,
    advertised_url: ADVERTISED,
    raw_port_bind_address: null,
    ...overrides,
  }
}

let w

async function render(role, initial) {
  setActivePinia(createPinia())
  const auth = useAuthStore()
  auth.user = { username: 'admin', role }
  auth.token = 'jwt-1101'
  apiMock.get.mockImplementation(async (url) => {
    if (url === '/api/settings/mcp-external-access') return { data: initial }
    throw new Error(`unexpected GET ${url}`)
  })
  // One key already exists, so ensure-default does not open the created-key modal.
  globalThis.fetch = vi.fn(async () => ({
    ok: true,
    json: async () => [{ id: 'k1', name: 'default', key_prefix: 'trinity_mcp_x', scope: 'user', is_active: true,
      created_at: '2026-11-01T09:00:00Z', last_used_at: null, usage_count: 0 }],
  }))
  w = mount(McpKeysTab, { attachTo: document.body })
  await flushPromises()
  return w
}

const findSwitch = () => w.find('button[role="switch"][aria-label*="External MCP access"]')
const findButton = (text) => w.findAll('button').find((b) => b.text() === text)
const snippet = () => w.find('[data-testid="mcp-connection-snippet"]')
const offCallout = () => w.find('[data-testid="mcp-external-access-off"]')

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { w?.unmount(); w = null; document.body.innerHTML = '' })

describe('ent#1101 External MCP access in the MCP Keys tab', () => {
  it('offers the switch to an admin, reflecting the server state', async () => {
    await render('admin', state())
    const sw = findSwitch()
    expect(sw.exists()).toBe(true)
    expect(sw.attributes('aria-checked')).toBe('true')
  })

  it('gives a non-admin a read-only status line, no switch', async () => {
    await render('creator', state({ enabled: false, source: 'default' }))
    expect(findSwitch().exists()).toBe(false)
    expect(findButton('Clear')).toBeUndefined()
    expect(w.text()).toContain('External MCP access')
    expect(w.text()).toContain('Off — default for a fresh install; agents are unaffected')
  })

  it('writes {enabled} through the typed route when the switch is flipped', async () => {
    await render('admin', state({ enabled: false, source: 'default' }))
    apiMock.put.mockResolvedValueOnce({ data: state({ enabled: true, source: 'settings' }) })
    await findSwitch().trigger('click')
    await flushPromises()
    expect(apiMock.put).toHaveBeenCalledWith('/api/settings/mcp-external-access', { enabled: true })
    expect(findSwitch().attributes('aria-checked')).toBe('true')
    expect(w.text()).toContain('On — set here')
  })

  it('shows Clear only while a Settings value exists, and Clear sends the DELETE', async () => {
    await render('admin', state({ enabled: true, source: 'environment', env_value: 'true' }))
    expect(findButton('Clear')).toBeUndefined()
    w.unmount()

    await render('admin', state({ enabled: true, source: 'settings' }))
    const clear = findButton('Clear')
    expect(clear).toBeDefined()
    expect(w.text()).toContain('the environment or the default then decides')
    apiMock.delete.mockResolvedValueOnce({ data: state({ enabled: false, source: 'default' }) })
    await clear.trigger('click')
    await flushPromises()
    expect(apiMock.delete).toHaveBeenCalledWith('/api/settings/mcp-external-access')
    expect(findButton('Clear')).toBeUndefined()
    expect(w.text()).toContain('Off — default for a fresh install; agents are unaffected')
  })

  it('names the environment as the deciding tier when it is', async () => {
    await render('admin', state({ enabled: true, source: 'environment', env_value: 'true' }))
    expect(w.text()).toContain('MCP_EXTERNAL_ACCESS=true')
    expect(w.text()).toContain('a value saved here overrides it')
    expect(w.text()).not.toContain('On — set here')
  })

  it('replaces the connection snippet with the refusal callout when off', async () => {
    await render('admin', state({ enabled: false, source: 'default' }))
    expect(snippet().exists()).toBe(false)
    expect(offCallout().exists()).toBe(true)
    expect(offCallout().text()).toContain('external_mcp_access_off')
    expect(offCallout().text()).toContain('Agents keep connecting')
    expect(w.text()).not.toContain(ADVERTISED)
  })

  it('renders the server advertised_url in the snippet, with no client :8080 guess', async () => {
    await render('admin', state())
    expect(offCallout().exists()).toBe(false)
    expect(snippet().text()).toContain(`"url": "${ADVERTISED}"`)
    expect(w.html()).not.toContain(':8080')
  })
})
