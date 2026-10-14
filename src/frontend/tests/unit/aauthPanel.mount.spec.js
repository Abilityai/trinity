// @vitest-environment jsdom
/**
 * Settings → AAuth panel (abilityai/trinity-enterprise#862), mounted.
 *
 * Every control here is a store write that changes this instance's identity
 * posture, so the tests drive the rendered panel against a stubbed client and
 * read the requests it sends:
 *  - the switch sends ONLY `enabled` (never the env-sourced issuer it did not touch);
 *  - "on but no valid issuer" is shown as inactive, never as a green switch;
 *  - an invalid issuer is named before saving and cannot be saved;
 *  - changing a LIVE issuer asks first, and a cancel sends nothing;
 *  - a failed first load is LoadFailed, not an empty panel.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))

import AAuthPanel from '@/components/settings/AAuthPanel.vue'

const STATE = (over = {}) => ({
  enabled: false, enabled_source: 'default',
  issuer: 'https://a.example.com', issuer_source: 'env', issuer_valid: true,
  live: false, identity_example: 'aauth:<agent-name>@a.example.com',
  prototype_enabled: false,
  readiness: { issuer_valid: true, outbound_enabled: false, endpoints: 0, aauth_endpoints: 0 },
  ...over,
})

let w
async function render(state) {
  api.get.mockImplementation(async () => ({ data: state }))
  api.put.mockImplementation(async (_url, body) => ({ data: { ...state, ...body } }))
  w = mount(AAuthPanel, { attachTo: document.body })
  await flushPromises()
  return w
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})
afterEach(() => { w?.unmount(); w = null; document.body.innerHTML = '' })

describe('AAuthPanel', () => {
  it('the switch sends only `enabled`, never the env-sourced issuer', async () => {
    await render(STATE())
    await w.find('[data-testid="aauth-toggle"] button, [data-testid="aauth-toggle"]').trigger('click')
    await flushPromises()

    expect(api.put).toHaveBeenCalledTimes(1)
    expect(api.put).toHaveBeenCalledWith('/api/settings/aauth', { enabled: true })
  })

  it('shows "on but no valid issuer" as inactive', async () => {
    await render(STATE({ enabled: true, issuer: '', issuer_valid: false, live: false }))

    expect(w.find('[data-testid="aauth-inert"]').exists()).toBe(true)
    expect(w.find('[data-testid="aauth-status"]').text()).toContain('On, inactive')
  })

  it('names a bad issuer and will not save it', async () => {
    await render(STATE({ issuer: '', issuer_source: 'default', issuer_valid: false }))
    await w.find('#aauth-issuer').setValue('https://trinity.example.com:8443')

    expect(w.text()).toContain('No port')
    expect(w.find('[data-testid="aauth-issuer-save"]').attributes('disabled')).toBeDefined()
    await w.find('#aauth-issuer').trigger('keyup.enter')
    await flushPromises()
    expect(api.put).not.toHaveBeenCalled()
  })

  it('saves a new issuer on an instance that is not live without asking', async () => {
    await render(STATE({ issuer: '', issuer_source: 'default', issuer_valid: false }))
    await w.find('#aauth-issuer').setValue('https://b.example.com')
    await w.find('[data-testid="aauth-issuer-save"]').trigger('click')
    await flushPromises()

    expect(api.put).toHaveBeenCalledWith('/api/settings/aauth', { issuer: 'https://b.example.com' })
  })

  it('asks before changing a live issuer, and a cancel sends nothing', async () => {
    await render(STATE({ enabled: true, live: true, issuer_source: 'settings' }))
    await w.find('#aauth-issuer').setValue('https://b.example.com')
    await w.find('[data-testid="aauth-issuer-save"]').trigger('click')
    await flushPromises()

    expect(api.put).not.toHaveBeenCalled()
    expect(document.body.textContent).toContain('Peers that listed the old identities')
    const cancel = [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Cancel')
    cancel.click()
    await flushPromises()
    expect(api.put).not.toHaveBeenCalled()

    await w.find('[data-testid="aauth-issuer-save"]').trigger('click')
    await flushPromises()
    const confirm = [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Change issuer')
    confirm.click()
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith('/api/settings/aauth', { issuer: 'https://b.example.com' })
  })

  it('a failed first load renders LoadFailed, not the settings', async () => {
    api.get.mockRejectedValue({ response: { data: { detail: 'boom' } } })
    w = mount(AAuthPanel, { attachTo: document.body })
    await flushPromises()

    expect(w.text()).toContain("Couldn't load the AAuth settings")
    expect(w.find('[data-testid="aauth-toggle"]').exists()).toBe(false)
  })

  it('the readiness checklist names the next step for what is missing', async () => {
    await render(STATE())
    const items = w.findAll('[data-testid="aauth-readiness"] li')

    expect(items).toHaveLength(4)
    expect(items[2].attributes('data-ok')).toBe('false')
    expect(items[2].text()).toContain('Turn on outbound calls')
  })
})
