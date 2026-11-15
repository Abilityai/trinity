// @vitest-environment jsdom
/**
 * Settings › Integrations → Nevermined organization (abilityai/trinity-enterprise#967, TD-1), mounted.
 *
 * The organization's two secrets are write-only and switching its environment
 * moves where new mandates are created, so the tests drive the rendered panel
 * against a stubbed client and read what it sent and what it shows (plan §8.3):
 *  - not configured: Save needs both secrets; the PUT carries all four fields;
 *  - configured: secrets blank with "paste to rotate" placeholders; the PUT
 *    omits an empty secret;
 *  - an environment change asks first; a cancel resets the select and sends nothing;
 *  - a 422 shows the backend's message; a 409 delegations_active names the count;
 *  - Remove asks first (danger) and only then DELETEs;
 *  - a saved secret is never rendered back.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))

import NeverminedOrganizationPanel from '@/components/settings/NeverminedOrganizationPanel.vue'
import BaseButton from '@/components/base/BaseButton.vue'

const URL = '/api/nevermined/organization'
const NONE = { configured: false, org_id: null, environment: null, updated_at: null, updated_by: null, active_delegations: 0 }
const SET = (over = {}) => ({
  configured: true, org_id: 'org-123', environment: 'sandbox',
  updated_at: '2026-10-20T00:00:00Z', updated_by: 'admin', active_delegations: 0, ...over,
})
const WIDGET = 'wsec-do-not-render-1234'
const AGENT_KEY = 'sandbox:agentkey-do-not-render'

let w
async function render(state) {
  api.get.mockImplementation(async () => ({ data: state }))
  w = mount(NeverminedOrganizationPanel, { attachTo: document.body })
  await flushPromises()
  return w
}
const $ = (id) => w.find(`[data-testid="${id}"]`)
const dialogBtn = (id) => document.querySelector(`[data-testid="${id}"]`)
const dialogs = () => [...document.querySelectorAll('[data-testid="confirm-dialog"]')]

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})
afterEach(() => { w?.unmount(); w = null; document.body.innerHTML = '' })

describe('NeverminedOrganizationPanel — not configured', () => {
  it('Save stays disabled until both secrets are present; the PUT carries all four fields', async () => {
    await render(NONE)
    expect(api.get).toHaveBeenCalledWith(URL)
    expect($('nvm-org-status').text()).toContain('Nevermined not configured')
    expect($('nvm-org-remove').exists()).toBe(false)
    const save = () => $('nvm-org-save')

    await $('nvm-org-id').setValue('org-123')
    expect(save().attributes('disabled')).toBeDefined()
    expect($('nvm-org-secrets-needed').exists()).toBe(true)
    await $('nvm-org-widget-secret').setValue(WIDGET)
    expect(save().attributes('disabled')).toBeDefined()
    await $('nvm-org-agent-key').setValue(AGENT_KEY)
    expect(save().attributes('disabled')).toBeUndefined()

    // A not-configured organization changes environment without a confirm.
    await $('nvm-org-env').setValue('live')
    expect(dialogs().some((d) => d.textContent.includes('Switch the Nevermined environment'))).toBe(false)

    api.put.mockResolvedValueOnce({ data: SET({ environment: 'live' }) })
    await $('nvm-org-form').trigger('submit')
    await flushPromises()

    expect(api.put).toHaveBeenCalledWith(URL, {
      org_id: 'org-123', environment: 'live', widget_secret: WIDGET, agent_api_key: AGENT_KEY,
    })
    expect($('nvm-org-saved').text()).toContain('Nevermined organization saved.')
    // Write-only: neither secret is rendered or left in a field after the save.
    expect(w.html()).not.toContain(WIDGET)
    expect(w.html()).not.toContain(AGENT_KEY)
    expect($('nvm-org-widget-secret').element.value).toBe('')
    expect($('nvm-org-agent-key').element.value).toBe('')
    expect($('nvm-org-status').text()).toContain('Nevermined configured')
  })

  it('the panel says who holds the card, by Nevermined\'s name', async () => {
    await render(NONE)
    expect(w.text()).toContain('Nevermined holds the card')
    expect(w.text()).not.toMatch(/trinity\s+(charges|holds|stores|keeps|saves)\s+(the|your|a)\s+card|trinity\s+settles/i)
  })

  it('a failed first read is LoadFailed, not an empty form', async () => {
    api.get.mockRejectedValueOnce({ response: { status: 500, data: { detail: 'boom' } } })
    w = mount(NeverminedOrganizationPanel, { attachTo: document.body })
    await flushPromises()
    expect($('nvm-org-form').exists()).toBe(false)
    expect(w.text()).toContain("Couldn't load the Nevermined organization")
  })
})

describe('NeverminedOrganizationPanel — configured', () => {
  it('secrets are blank with "paste to rotate"; a PUT with blank secrets omits them', async () => {
    await render(SET())
    expect($('nvm-org-id').element.value).toBe('org-123')
    for (const id of ['nvm-org-widget-secret', 'nvm-org-agent-key']) {
      expect($(id).element.value).toBe('')
      expect($(id).attributes('type')).toBe('password')
      expect($(id).attributes('placeholder')).toContain('paste to rotate')
    }
    expect($('nvm-org-save').attributes('disabled')).toBeUndefined()

    api.put.mockResolvedValueOnce({ data: SET({ org_id: 'org-456' }) })
    await $('nvm-org-id').setValue('org-456')
    await $('nvm-org-form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith(URL, { org_id: 'org-456', environment: 'sandbox' })
  })

  it('a pasted secret alone is sent (rotation), the other omitted', async () => {
    await render(SET())
    api.put.mockResolvedValueOnce({ data: SET() })
    await $('nvm-org-agent-key').setValue(AGENT_KEY)
    await $('nvm-org-form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith(URL, { org_id: 'org-123', environment: 'sandbox', agent_api_key: AGENT_KEY })
    expect(w.html()).not.toContain(AGENT_KEY)
  })

  it('an environment change asks first; Cancel resets the select and sends nothing', async () => {
    await render(SET())
    await $('nvm-org-env').setValue('live')
    await nextTick()
    expect(dialogBtn('confirm-dialog-cancel')).toBeTruthy()
    expect(document.body.textContent).toContain('Switch the Nevermined environment?')

    dialogBtn('confirm-dialog-cancel').click()
    await flushPromises()
    expect($('nvm-org-env').element.value).toBe('sandbox')

    await $('nvm-org-form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith(URL, { org_id: 'org-123', environment: 'sandbox' })
  })

  it('an environment change confirmed is what the PUT carries', async () => {
    await render(SET())
    await $('nvm-org-env').setValue('live')
    await nextTick()
    dialogBtn('confirm-dialog-confirm').click()
    await flushPromises()
    expect($('nvm-org-env').element.value).toBe('live')

    api.put.mockResolvedValueOnce({ data: SET({ environment: 'live' }) })
    await $('nvm-org-form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith(URL, { org_id: 'org-123', environment: 'live' })
  })

  it('a PUT 422 shows the backend message (a secret with a line break)', async () => {
    await render(SET())
    api.put.mockRejectedValueOnce({
      response: { status: 422, data: { detail: [{ loc: ['body', 'widget_secret'], msg: 'Value error, a Nevermined secret cannot contain a newline', type: 'value_error' }] } },
    })
    await $('nvm-org-widget-secret').setValue('line break')
    await $('nvm-org-form').trigger('submit')
    await flushPromises()
    expect($('nvm-org-save-error').text()).toContain('a Nevermined secret cannot contain a newline')
    expect(w.html()).not.toContain('line break'.replace(' ', ''))
  })

  it('a PUT 422 named refusal shows detail.message', async () => {
    await render(SET())
    api.put.mockRejectedValueOnce({
      response: { status: 422, data: { detail: { code: 'environment_mismatch', message: "The Nevermined agent API key is a 'live' key but the environment is 'sandbox'." } } },
    })
    await $('nvm-org-agent-key').setValue('live:key')
    await $('nvm-org-form').trigger('submit')
    await flushPromises()
    expect($('nvm-org-save-error').text()).toContain("The Nevermined agent API key is a 'live' key")
  })

  it('a 409 delegations_active names the count', async () => {
    await render(SET({ active_delegations: 3 }))
    expect($('nvm-org-active').text()).toBe('3')
    api.put.mockRejectedValueOnce({
      response: {
        status: 409,
        data: { detail: { code: 'delegations_active', message: 'Agents hold an active Nevermined mandate; revoke those mandates first.', active_delegations: 3 } },
      },
    })
    await $('nvm-org-form').trigger('submit')
    await flushPromises()
    const text = $('nvm-org-save-error').text()
    expect(text).toContain('3 agents still hold an active Nevermined mandate')
  })

  it('Remove asks first (danger); Cancel deletes nothing; confirm DELETEs and returns to not configured', async () => {
    await render(SET())
    await $('nvm-org-remove').trigger('click')
    await nextTick()
    expect(document.body.textContent).toContain('Remove the Nevermined organization?')
    dialogBtn('confirm-dialog-cancel').click()
    await flushPromises()
    expect(api.delete).not.toHaveBeenCalled()

    api.delete.mockResolvedValueOnce({ data: NONE })
    await $('nvm-org-remove').trigger('click')
    await nextTick()
    dialogBtn('confirm-dialog-confirm').click()
    await flushPromises()
    expect(api.delete).toHaveBeenCalledWith(URL)
    expect($('nvm-org-status').text()).toContain('Nevermined not configured')
    expect($('nvm-org-remove').exists()).toBe(false)
  })

  it('Remove refused with 409 delegations_active keeps the organization and names the count', async () => {
    await render(SET({ active_delegations: 1 }))
    api.delete.mockRejectedValueOnce({
      response: { status: 409, data: { detail: { code: 'delegations_active', message: 'Revoke those Nevermined mandates first.', active_delegations: 1 } } },
    })
    await $('nvm-org-remove').trigger('click')
    await nextTick()
    dialogBtn('confirm-dialog-confirm').click()
    await flushPromises()
    expect($('nvm-org-remove-error').text()).toContain('1 agent still holds an active Nevermined mandate')
    expect($('nvm-org-status').text()).toContain('Nevermined configured')
  })

  it('every action is a BaseButton', async () => {
    await render(SET())
    const buttons = w.findAllComponents(BaseButton)
    for (const id of ['nvm-org-save', 'nvm-org-remove']) {
      const el = $(id).element
      expect(el.tagName, id).toBe('BUTTON')
      expect(buttons.some((b) => b.element === el || b.element.contains(el)), id).toBe(true)
    }
  })
})
