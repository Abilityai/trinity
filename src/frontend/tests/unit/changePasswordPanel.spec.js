// @vitest-environment jsdom
/**
 * #3825 — the Settings password panel, mounted.
 *
 * Proves the store write (the fresh token replaces ours), the submit gate, and
 * that each named server refusal lands next to the field it is about.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { RouterLinkStub, flushPromises, mount as rawMount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import axios from 'axios'
import ChangePasswordPanel from '../../src/components/settings/ChangePasswordPanel.vue'
import { useAuthStore } from '../../src/stores/auth'

const GOOD = 'New-Passw0rd-xyz!'

const mount = (component) => rawMount(component, { global: { stubs: { RouterLink: RouterLinkStub } } })

function fill(wrapper, current, next, confirm = next) {
  return Promise.all([
    wrapper.get('[data-testid="current-password"]').setValue(current),
    wrapper.get('[data-testid="new-password"]').setValue(next),
    wrapper.get('[data-testid="confirm-password"]').setValue(confirm),
  ])
}

function refusal(status, detail) {
  return Object.assign(new Error('refused'), { response: { status, data: { detail } } })
}

describe('ChangePasswordPanel', () => {
  beforeEach(() => {
    setActivePinia(createPinia())
    localStorage.clear()
    vi.restoreAllMocks()
  })

  it('submits, swaps in the fresh token and clears the form', async () => {
    const put = vi.spyOn(axios, 'put').mockResolvedValue({
      data: { access_token: 'fresh-token', token_type: 'bearer', other_sessions_signed_out: true, admin_env_stale: true },
    })
    const auth = useAuthStore()
    auth.token = 'old-token'
    const wrapper = mount(ChangePasswordPanel)
    await fill(wrapper, 'Old-Passw0rd-xyz!', GOOD)
    await wrapper.get('form').trigger('submit')
    await flushPromises()

    expect(put).toHaveBeenCalledWith('/api/users/me/password', {
      current_password: 'Old-Passw0rd-xyz!', new_password: GOOD, revoke_api_keys: false,
    })
    expect(auth.token).toBe('fresh-token')
    expect(localStorage.getItem('token')).toBe('fresh-token')
    expect(wrapper.find('[data-testid="change-password-done"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="env-stale"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="sessions-not-signed-out"]').exists()).toBe(false)
    expect(wrapper.get('[data-testid="new-password"]').element.value).toBe('')
  })

  it('says so when the other sessions could not be signed out', async () => {
    vi.spyOn(axios, 'put').mockResolvedValue({
      data: { access_token: 't', other_sessions_signed_out: false, admin_env_stale: false },
    })
    const wrapper = mount(ChangePasswordPanel)
    await fill(wrapper, 'Old-Passw0rd-xyz!', GOOD)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.find('[data-testid="sessions-not-signed-out"]').exists()).toBe(true)
    expect(wrapper.find('[data-testid="env-stale"]').exists()).toBe(false)
  })

  it('will not submit a short or mismatched new password', async () => {
    const put = vi.spyOn(axios, 'put')
    const wrapper = mount(ChangePasswordPanel)
    const button = () => wrapper.get('[data-testid="change-password-submit"]').element

    await fill(wrapper, 'Old-Passw0rd-xyz!', 'short')
    expect(button().disabled).toBe(true)
    expect(wrapper.text()).toContain('Use at least 12 characters')

    await fill(wrapper, 'Old-Passw0rd-xyz!', GOOD, `${GOOD}x`)
    expect(button().disabled).toBe(true)
    expect(wrapper.text()).toContain("Doesn't match the new password.")

    await wrapper.get('form').trigger('submit')
    expect(put).not.toHaveBeenCalled()
  })

  it('puts a wrong current password on the current-password field and keeps the old token', async () => {
    vi.spyOn(axios, 'put').mockRejectedValue(refusal(400, { code: 'invalid_current_password', message: 'x' }))
    const auth = useAuthStore()
    auth.token = 'old-token'
    const wrapper = mount(ChangePasswordPanel)
    await fill(wrapper, 'Wrong-Passw0rd-1!', GOOD)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('The current password is wrong.')
    expect(auth.token).toBe('old-token')
    expect(wrapper.find('[data-testid="change-password-done"]').exists()).toBe(false)
  })

  it('lists the server\'s complexity reasons under the new password', async () => {
    vi.spyOn(axios, 'put').mockRejectedValue(refusal(422, {
      code: 'weak_password', message: 'x', errors: ['Must contain at least one digit (0-9)'],
    }))
    const wrapper = mount(ChangePasswordPanel)
    await fill(wrapper, 'Old-Passw0rd-xyz!', 'NoDigitsHere-abc!')
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.text()).toContain('Must contain at least one digit (0-9)')
  })

  it('shows a rate limit as a form error', async () => {
    vi.spyOn(axios, 'put').mockRejectedValue(refusal(429, { code: 'too_many_attempts', message: 'x' }))
    const wrapper = mount(ChangePasswordPanel)
    await fill(wrapper, 'Old-Passw0rd-xyz!', GOOD)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(wrapper.get('[data-testid="inline-error"]').text()).toContain('Wait 15 minutes')
  })

  it('sends the opt-in and reports the keys it revoked', async () => {
    const put = vi.spyOn(axios, 'put').mockResolvedValue({
      data: { access_token: 't', other_sessions_signed_out: true, admin_env_stale: false,
              api_keys_revoked: 2, api_keys_active: 0 },
    })
    const wrapper = mount(ChangePasswordPanel)
    await fill(wrapper, 'Old-Passw0rd-xyz!', GOOD)
    await wrapper.get('[data-testid="revoke-api-keys"]').setValue(true)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    expect(put.mock.calls[0][1].revoke_api_keys).toBe(true)
    expect(wrapper.get('[data-testid="api-keys-revoked"]').text()).toContain('Revoked 2 API keys')
    expect(wrapper.find('[data-testid="api-keys-active"]').exists()).toBe(false)
  })

  it('says how many keys still work when the opt-in was left off', async () => {
    vi.spyOn(axios, 'put').mockResolvedValue({
      data: { access_token: 't', other_sessions_signed_out: true, admin_env_stale: false,
              api_keys_revoked: 0, api_keys_active: 1 },
    })
    const wrapper = mount(ChangePasswordPanel)
    await fill(wrapper, 'Old-Passw0rd-xyz!', GOOD)
    await wrapper.get('form').trigger('submit')
    await flushPromises()
    const note = wrapper.get('[data-testid="api-keys-active"]')
    expect(note.text()).toContain('1 API key you created still works')
    expect(note.findComponent(RouterLinkStub).props('to')).toBe('/settings?tab=mcp-keys')
  })
})
