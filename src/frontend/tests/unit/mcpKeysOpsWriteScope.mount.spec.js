// @vitest-environment jsdom
/**
 * #3180 — the `ops_write` key scope in Settings → MCP Keys, mounted.
 *
 * Three things only a mounted tab can prove:
 *  - an admin is offered the write tier as its own radio, beside (not instead
 *    of) "Ops (read-only)", and choosing it sends `scope: "ops_write"` — the
 *    exact string the backend mints, so a typo here is a 400, not a silent
 *    Standard key;
 *  - a non-admin is not offered it (the backend refuses them anyway);
 *  - a minted ops_write key is BADGED in the list. Unbadged it would read as a
 *    Standard key — the defect the `ops` badge was added for (#2323).
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

vi.mock('axios', () => ({ default: { get: vi.fn(async () => ({ data: { url: null, default_url: '' } })) } }))

import McpKeysTab from '@/components/settings/McpKeysTab.vue'
import { useAuthStore } from '@/stores/auth'

const KEYS = [
  { id: 'k-ow', name: 'ops automation', key_prefix: 'trinity_mcp_ow3180', scope: 'ops_write', is_active: true,
    agent_name: null, created_at: '2026-10-01T09:00:00Z', last_used_at: null, usage_count: 0, username: 'admin' },
  { id: 'k-ops', name: 'dashboard', key_prefix: 'trinity_mcp_op3180', scope: 'ops', is_active: true,
    agent_name: null, created_at: '2026-10-01T09:00:00Z', last_used_at: null, usage_count: 0, username: 'admin' },
]

let w
let posted

async function render(role) {
  setActivePinia(createPinia())
  const auth = useAuthStore()
  auth.user = { username: 'admin', role }
  auth.token = 'jwt-3180'
  posted = []
  globalThis.fetch = vi.fn(async (url, opts = {}) => {
    if (opts.method === 'POST' && url === '/api/mcp/keys') {
      posted.push(JSON.parse(opts.body))
      return { ok: true, json: async () => ({ ...KEYS[0], api_key: 'trinity_mcp_secret3180' }) }
    }
    return { ok: true, json: async () => KEYS }
  })
  w = mount(McpKeysTab, { attachTo: document.body })
  await flushPromises()
  return w
}

beforeEach(() => { vi.clearAllMocks() })
afterEach(() => { w?.unmount(); w = null; document.body.innerHTML = '' })

describe('#3180 ops_write in the MCP Keys tab', () => {
  it('badges a minted ops_write key, distinct from the read-only ops badge', async () => {
    await render('admin')
    const badge = w.find('span[title^="Operational-write machine credential"]')
    expect(badge.exists()).toBe(true)
    expect(badge.text()).toBe('Ops (writes)')
    // The two bounded tiers never share a label.
    expect(w.text()).toContain('Ops (read-only)')
    expect(badge.classes().some((c) => c.startsWith('bg-status-'))).toBe(true)
  })

  it('offers an admin the write tier and mints it with the exact backend scope', async () => {
    await render('admin')
    await w.find('button').trigger('click') // "Create API Key"
    const radio = w.find('input[type="radio"][value="ops_write"]')
    expect(radio.exists()).toBe(true)
    // Beside the read-only tier, not replacing it.
    expect(w.find('input[type="radio"][value="ops"]').exists()).toBe(true)
    const label = radio.element.closest('label').textContent
    expect(label).toContain('Ops (operational writes)')
    expect(label).toContain('circuit breaker')
    expect(label).toContain('system agent')

    await radio.setValue(true)
    await w.find('input[placeholder="My Claude Code Key"]').setValue('ops automation')
    const create = w.findAll('button').find((b) => b.text() === 'Create')
    await create.trigger('click')
    await flushPromises()
    expect(posted).toEqual([{ name: 'ops automation', description: null, scope: 'ops_write' }])
  })

  it('does not offer a non-admin any bounded scope', async () => {
    await render('creator')
    await w.find('button').trigger('click')
    expect(w.find('input[type="radio"][value="ops_write"]').exists()).toBe(false)
    expect(w.find('input[type="radio"][value="ops"]').exists()).toBe(false)
  })
})
