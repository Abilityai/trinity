// @vitest-environment jsdom
/**
 * Settings → Outbound A2A endpoints (abilityai/trinity-enterprise#862), mounted.
 *
 * Registering an endpoint decides where a credentialed server-side request may
 * go, so the tests drive the rendered panel against a stubbed client and read
 * what it sends:
 *  - an AAuth registration carries `auth_scheme: 'aauth'` and no credential key;
 *  - switching a stored-credential bearer row to AAuth warns that the secret is
 *    dropped, and sends the scheme;
 *  - remove asks first; a cancel sends nothing; a confirm DELETEs by id;
 *  - the outbound switch writes the kill-switch setting;
 *  - zero endpoints after a SUCCEEDED fetch is the empty copy; a failed fetch is LoadFailed.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))

import A2AEndpointsPanel from '@/components/settings/A2AEndpointsPanel.vue'

const VENDOR = { id: 'ep-1', name: 'vendor', url: 'https://v.example.com/a2a', has_credentials: true, auth_scheme: 'bearer' }

let w
async function render(endpoints = [], enabled = true) {
  api.get.mockImplementation(async () => ({ data: { endpoints, enabled } }))
  api.put.mockImplementation(async () => ({ data: { success: true, enabled } }))
  api.delete.mockImplementation(async () => ({ data: { success: true } }))
  w = mount(A2AEndpointsPanel, { attachTo: document.body })
  await flushPromises()
  return w
}
// The dialog's own buttons — the row carries a 'Remove' of its own.
const button = (label) => [...document.querySelectorAll('[data-testid="confirm-dialog"] button')].find((b) => b.textContent.trim() === label)

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})
afterEach(() => { w?.unmount(); w = null; document.body.innerHTML = '' })

describe('A2AEndpointsPanel', () => {
  it('registers an AAuth endpoint with no credential key', async () => {
    await render()
    await w.find('#a2a-endpoint-name').setValue('cornelius-architect')
    await w.find('#a2a-endpoint-url').setValue('https://a.example.com/a2a/cornelius-architect')
    await w.find('#a2a-endpoint-scheme').setValue('aauth')
    expect(w.find('#a2a-endpoint-credential').exists()).toBe(false)
    await w.find('[data-testid="a2a-endpoint-form"]').trigger('submit')
    await flushPromises()

    expect(api.put).toHaveBeenCalledWith('/api/settings/a2a-endpoints', {
      name: 'cornelius-architect',
      url: 'https://a.example.com/a2a/cornelius-architect',
      auth_scheme: 'aauth',
    })
  })

  it('refuses a non-default port on an AAuth endpoint before sending', async () => {
    await render()
    await w.find('#a2a-endpoint-name').setValue('peer')
    await w.find('#a2a-endpoint-url').setValue('https://a.example.com:8443/a2a/x')
    await w.find('#a2a-endpoint-scheme').setValue('aauth')
    await w.find('[data-testid="a2a-endpoint-form"]').trigger('submit')
    await flushPromises()

    expect(w.text()).toContain('default HTTPS port')
    expect(api.put).not.toHaveBeenCalled()
  })

  it('switching a credentialed bearer row to AAuth warns and sends the scheme', async () => {
    await render([VENDOR])
    await w.find('[data-testid="a2a-endpoint-vendor"]').findAll('button')[0].trigger('click')
    await w.find('#a2a-endpoint-scheme').setValue('aauth')

    expect(w.find('[data-testid="a2a-endpoint-drop-note"]').exists()).toBe(true)
    await w.find('[data-testid="a2a-endpoint-form"]').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/settings/a2a-endpoints', {
      name: 'vendor', url: 'https://v.example.com/a2a', auth_scheme: 'aauth',
    })
  })

  it('a blank credential on a bearer update keeps the stored one (no key sent)', async () => {
    await render([VENDOR])
    await w.find('[data-testid="a2a-endpoint-vendor"]').findAll('button')[0].trigger('click')
    await w.find('#a2a-endpoint-url').setValue('https://v2.example.com/a2a')
    await w.find('[data-testid="a2a-endpoint-form"]').trigger('submit')
    await flushPromises()

    expect(api.put.mock.calls[0][1]).toEqual({ name: 'vendor', url: 'https://v2.example.com/a2a', auth_scheme: 'bearer' })
  })

  it('remove asks first; cancel sends nothing; confirm deletes by id', async () => {
    await render([VENDOR])
    await w.find('[data-testid="a2a-remove-vendor"]').trigger('click')
    await flushPromises()
    expect(api.delete).not.toHaveBeenCalled()
    button('Cancel').click()
    await flushPromises()
    expect(api.delete).not.toHaveBeenCalled()

    await w.find('[data-testid="a2a-remove-vendor"]').trigger('click')
    await flushPromises()
    button('Remove').click()
    await flushPromises()
    expect(api.delete).toHaveBeenCalledWith('/api/settings/a2a-endpoints/ep-1')
  })

  it('the switch writes the outbound kill-switch setting', async () => {
    await render([], false)
    expect(w.find('[data-testid="a2a-outbound-off"]').exists()).toBe(true)
    await w.find('[data-testid="a2a-outbound-toggle"]').trigger('click')
    await flushPromises()

    expect(api.put).toHaveBeenCalledWith('/api/settings/a2a_outbound_enabled', { value: 'true' })
  })

  it('zero endpoints after a succeeded fetch is the empty copy', async () => {
    await render([])
    expect(w.find('[data-testid="a2a-endpoints-empty"]').exists()).toBe(true)
  })

  it('a failed fetch is LoadFailed, never the empty copy', async () => {
    api.get.mockRejectedValue({ response: { data: { detail: 'boom' } } })
    w = mount(A2AEndpointsPanel, { attachTo: document.body })
    await flushPromises()

    expect(w.text()).toContain("Couldn't load the outbound endpoints")
    expect(w.find('[data-testid="a2a-endpoints-empty"]').exists()).toBe(false)
  })
})
