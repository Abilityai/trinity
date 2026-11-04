// @vitest-environment jsdom
/**
 * #3655 — with no email provider on this install, the Login page stops saying
 * "We sent a 6-digit code". The code goes to the backend log; the page says
 * so, names the command to read it, points at Admin Login and at Settings →
 * Integrations — and never renders the code itself.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import axios from 'axios'

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ query: {}, path: '/login' }),
}))

import Login from '../../src/views/Login.vue'
import { useAuthStore } from '../../src/stores/auth'

async function requestCode({ delivers }) {
  setActivePinia(createPinia())
  const store = useAuthStore()
  store.$patch({
    modeDetected: true, isLoading: false, emailAuthEnabled: true, emailCodeLoginEnabled: true,
    emailDeliveryAvailable: delivers,
  })
  store.fetchSsoProviders = vi.fn().mockResolvedValue([])
  store.requestEmailCode = vi.fn().mockResolvedValue({ success: true, expiresInSeconds: 600 })
  const w = mount(Login)
  await flushPromises()
  await w.find('#email').setValue('dana@example.com')
  await w.find('form').trigger('submit')
  await flushPromises()
  return w
}

const notice = (w) => w.find('[data-testid="login-no-email-provider"]')

describe('Login — an install with no email provider (#3655)', () => {
  beforeEach(() => { window.location.hash = '' })

  it('says no email was sent, where the code is, and the other ways in', async () => {
    const w = await requestCode({ delivers: false })
    expect(w.text()).not.toContain('We sent a 6-digit code')
    const text = notice(w).text()
    expect(text).toContain('No email provider is configured')
    expect(text).toContain('docker compose logs backend')
    expect(text).toContain('Admin Login')
    expect(text).toContain('Settings → Integrations')
    expect(w.find('#code').exists()).toBe(true)            // the code from the log still works
  })

  it('never renders a code: the page has no six-digit number in it', async () => {
    const w = await requestCode({ delivers: false })
    expect(w.text()).not.toMatch(/\b\d{6}\b/)
  })

  it('offers Admin Login from the notice', async () => {
    const w = await requestCode({ delivers: false })
    await notice(w).find('button').trigger('click')
    expect(w.find('#password').exists()).toBe(true)
  })

  it.each([true, null])('an install that delivers (or an older backend: %s) reads as today', async (delivers) => {
    const w = await requestCode({ delivers })
    expect(w.text()).toContain('We sent a 6-digit code to')
    expect(notice(w).exists()).toBe(false)
  })

  it('reads the flag from the auth-mode response', async () => {
    setActivePinia(createPinia())
    const store = useAuthStore()
    vi.spyOn(axios, 'get').mockResolvedValueOnce({ data: {
      email_auth_enabled: true, email_code_login_enabled: true, email_delivery_available: false } })
    await store.detectAuthMode()
    expect(store.emailDeliveryAvailable).toBe(false)
  })
})
