// @vitest-environment jsdom
/**
 * trinity-enterprise#709 — the stepped Change password dialog, mounted.
 *
 * The API client is mocked at the module boundary; everything else — the
 * dialog, its store, BaseModal/BaseInput/BaseButton — is the real code.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

vi.mock('../../src/api', () => ({
  default: { post: vi.fn(), put: vi.fn(), get: vi.fn() },
}))

import api from '../../src/api'
import ChangePasswordDialog from '../../src/components/settings/ChangePasswordDialog.vue'
import { passwordRequirements, meetsPasswordRequirements } from '../../src/utils/passwordRules'

const OLD = 'Old-Passw0rd!-709'
const NEW = 'New-Passw0rd!-709'

const q = (id) => document.body.querySelector(`[data-testid="${id}"]`)
const input = (id) => q(id)?.querySelector('input') || q(id)

async function type(id, value) {
  const el = input(id)
  el.value = value
  el.dispatchEvent(new Event('input'))
  await nextTick()
}

async function click(id) {
  q(id).click()
  await flushPromises()
}

function refusal(code, message = code, status = 400) {
  return Object.assign(new Error(message), { response: { status, data: { detail: { code, message } } } })
}

let wrapper
async function open() {
  wrapper = mount(ChangePasswordDialog, { props: { modelValue: true }, attachTo: document.body })
  await flushPromises()
}

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  wrapper?.unmount()
  document.body.innerHTML = ''
})

describe('passwordRules', () => {
  it('mirrors the first-run rules', () => {
    expect(meetsPasswordRequirements(NEW)).toBe(true)
    expect(passwordRequirements('short').filter((r) => !r.met).map((r) => r.id))
      .toEqual(['length', 'upper', 'digit', 'symbol'])
  })
})

describe('ChangePasswordDialog', () => {
  it('goes straight from the current password to the new one when no 2FA is required', async () => {
    api.post.mockResolvedValue({ data: { verified: true, second_factor_required: false } })
    await open()
    expect(q('step-counter').textContent).toContain('Step 1 of 2')
    await type('current-password', OLD)
    await click('primary-button')

    expect(api.post).toHaveBeenCalledWith('/api/users/me/password/verify', { current_password: OLD })
    expect(q('second-factor-code')).toBeNull()
    expect(q('new-password')).not.toBeNull()
    expect(q('step-counter').textContent).toContain('Step 2 of 2')
  })

  it('shows the second-factor step when the gate requires it', async () => {
    api.post.mockResolvedValue({ data: { verified: true, second_factor_required: true } })
    await open()
    await type('current-password', OLD)
    await click('primary-button')

    expect(q('second-factor-code')).not.toBeNull()
    expect(q('new-password')).toBeNull()
    expect(q('step-counter').textContent).toContain('Step 2 of 3')
    // Continue is blocked until a code is entered.
    expect(q('primary-button').disabled).toBe(true)
    await type('second-factor-code', '123456')
    await click('primary-button')
    expect(q('new-password')).not.toBeNull()
  })

  it('a wrong current password stays on step 1 with the named error', async () => {
    api.post.mockRejectedValue(refusal('current_password_incorrect', 'Your current password is incorrect.'))
    await open()
    await type('current-password', 'nope')
    await click('primary-button')

    expect(q('current-password')).not.toBeNull()
    expect(document.body.textContent).toContain('Your current password is incorrect.')
  })

  it('shows a mismatch before submit and blocks the change', async () => {
    api.post.mockResolvedValue({ data: { verified: true, second_factor_required: false } })
    await open()
    await type('current-password', OLD)
    await click('primary-button')
    await type('new-password', NEW)
    await type('confirm-password', NEW + 'x')

    expect(document.body.textContent).toContain("Passwords don't match")
    expect(q('primary-button').disabled).toBe(true)
    q('primary-button').closest('form').dispatchEvent(new Event('submit'))
    await flushPromises()
    expect(api.put).not.toHaveBeenCalled()
  })

  it('a weak new password cannot be submitted and its unmet rules are marked', async () => {
    api.post.mockResolvedValue({ data: { verified: true, second_factor_required: false } })
    await open()
    await type('current-password', OLD)
    await click('primary-button')
    await type('new-password', 'weak')
    await type('confirm-password', 'weak')
    expect(q('primary-button').disabled).toBe(true)
    expect(q('requirements').querySelectorAll('[data-met="false"]').length).toBeGreaterThan(0)
  })

  it('refuses the current password as the new one before submit', async () => {
    api.post.mockResolvedValue({ data: { verified: true, second_factor_required: false } })
    await open()
    await type('current-password', OLD)
    await click('primary-button')
    await type('new-password', OLD)
    await type('confirm-password', OLD)
    expect(document.body.textContent).toContain('must be different')
    expect(q('primary-button').disabled).toBe(true)
  })

  it('submits once everything is valid and shows the success state', async () => {
    api.post.mockResolvedValue({ data: { verified: true, second_factor_required: false } })
    api.put.mockResolvedValue({ data: { success: true, access_token: 'tok', other_sessions_signed_out: true } })
    await open()
    await type('current-password', OLD)
    await click('primary-button')
    await type('new-password', NEW)
    await type('confirm-password', NEW)
    expect(q('primary-button').disabled).toBe(false)
    await click('primary-button')

    expect(api.put).toHaveBeenCalledWith('/api/users/me/password', {
      current_password: OLD, new_password: NEW, confirm_password: NEW,
    })
    expect(q('change-password-done').textContent).toContain('Password updated')
    expect(q('change-password-done').textContent).toContain('other sessions were signed out')
  })

  it('a wrong second-factor code on submit returns to the code step', async () => {
    api.post.mockResolvedValue({ data: { verified: true, second_factor_required: true } })
    api.put.mockRejectedValue(refusal('second_factor_invalid', 'That code is wrong or has expired.'))
    await open()
    await type('current-password', OLD)
    await click('primary-button')
    await type('second-factor-code', '000000')
    await click('primary-button')
    await type('new-password', NEW)
    await type('confirm-password', NEW)
    await click('primary-button')

    expect(api.put.mock.calls[0][1].second_factor_code).toBe('000000')
    expect(q('second-factor-code')).not.toBeNull()
    expect(document.body.textContent).toContain('That code is wrong or has expired.')
  })
})
