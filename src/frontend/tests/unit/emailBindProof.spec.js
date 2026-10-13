// @vitest-environment jsdom
/**
 * trinity-enterprise#720 — binding a sign-in email needs proof of the mailbox.
 *
 * The first-run email step (and Settings, through the same store action) tries
 * the bind once; when the server answers `code_required` the store has the code
 * sent and the step asks for it, then binds WITH the code. On an install that
 * cannot deliver mail an admin's first try simply succeeds (the audited #82
 * transition), so no code field ever appears. MOUNTED: the step is a template
 * branch over what the store returns, and only a render proves both halves meet.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const api = { put: vi.fn(), post: vi.fn(), get: vi.fn() }
  return { default: api, ...api }
})

import axios from 'axios'
import StepEmail from '@/components/onboarding/steps/StepEmail.vue'
import { useAuthStore } from '@/stores/auth'
import { apiErrorMessage } from '@/utils/apiError'

const refusal = (status, code, message) =>
  Object.assign(new Error(message), { response: { status, data: { detail: { code, message } } } })

function mountStep() {
  const pinia = createPinia()
  setActivePinia(pinia)
  const auth = useAuthStore()
  auth.fetchUserProfile = vi.fn().mockResolvedValue()
  const w = mount(StepEmail, { props: { ctx: { hasEmail: false } }, global: { plugins: [pinia] } })
  return { w, auth }
}

describe('first-run email step — mailbox proof (ent#720)', () => {
  beforeEach(() => { vi.clearAllMocks() })

  it('asks for the code the server sent, then binds with it', async () => {
    axios.put
      .mockRejectedValueOnce(refusal(400, 'code_required', 'Enter the code we sent to that address.'))
      .mockResolvedValueOnce({ data: { success: true, verified: true } })
    axios.post.mockResolvedValueOnce({ data: { sent: true } })
    const { w, auth } = mountStep()

    await w.find('[data-testid="first-run-email-input"]').setValue('me@example.com')
    await w.find('form').trigger('submit')
    await flushPromises()

    expect(axios.post).toHaveBeenCalledWith('/api/users/me/email/code', { email: 'me@example.com' }, expect.anything())
    const code = w.find('[data-testid="first-run-email-code"]')
    expect(code.exists()).toBe(true)
    expect(w.emitted('complete')).toBeUndefined()

    await code.setValue('123456')
    await w.find('form').trigger('submit')
    await flushPromises()

    expect(axios.put).toHaveBeenLastCalledWith('/api/users/me/email',
      { email: 'me@example.com', code: '123456' }, expect.anything())
    expect(auth.fetchUserProfile).toHaveBeenCalled()
    expect(w.emitted('complete')).toHaveLength(1)
  })

  it('an admin on an install that cannot deliver mail binds on the first try — no code field', async () => {
    axios.put.mockResolvedValueOnce({ data: { success: true, verified: false } })
    const { w } = mountStep()
    await w.find('[data-testid="first-run-email-input"]').setValue('admin@example.com')
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(axios.post).not.toHaveBeenCalled()
    expect(w.find('[data-testid="first-run-email-code"]').exists()).toBe(false)
    expect(w.emitted('complete')).toHaveLength(1)
  })

  it('a wrong code keeps the step open and shows the server\'s own sentence', async () => {
    axios.put
      .mockRejectedValueOnce(refusal(400, 'code_required', 'Enter the code'))
      .mockRejectedValueOnce(refusal(400, 'invalid_code', 'That code is wrong or has expired. Request a new one.'))
    axios.post.mockResolvedValueOnce({ data: { sent: true } })
    const { w } = mountStep()
    await w.find('[data-testid="first-run-email-input"]').setValue('me@example.com')
    await w.find('form').trigger('submit')
    await flushPromises()
    await w.find('[data-testid="first-run-email-code"]').setValue('000000')
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(w.text()).toContain('That code is wrong or has expired')
    expect(w.emitted('complete')).toBeUndefined()
    expect(w.find('[data-testid="first-run-email-code"]').exists()).toBe(true)
  })

  it('"Use a different email" starts over', async () => {
    axios.put.mockRejectedValueOnce(refusal(400, 'code_required', 'Enter the code'))
    axios.post.mockResolvedValueOnce({ data: { sent: true } })
    const { w } = mountStep()
    await w.find('[data-testid="first-run-email-input"]').setValue('me@example.com')
    await w.find('form').trigger('submit')
    await flushPromises()
    await w.find('[data-testid="first-run-email-change"]').trigger('click')
    expect(w.find('[data-testid="first-run-email-code"]').exists()).toBe(false)
  })
})

describe('apiErrorMessage reads a named refusal', () => {
  it('returns detail.message for {code, message}', () => {
    expect(apiErrorMessage(refusal(409, 'email_in_use', 'That email is taken'))).toBe('That email is taken')
  })
})
