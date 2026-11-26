// @vitest-environment jsdom
/**
 * #3655 — with no email provider on this install, the Login page stops saying
 * "We sent a 6-digit code". It says no email was sent, points at Admin Login and
 * at Settings → Integrations, and never renders the code itself.
 *
 * Where the code is depends on the install. The backend logs a mail's body only
 * on a development machine that asked for it (`email_code_in_log`), so the page
 * names the log and the command only then. Anywhere else it says the code is
 * nowhere to be read: sending someone to a log that does not hold it is a dead end.
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

async function requestCode({ delivers, codeInLog = false }) {
  setActivePinia(createPinia())
  const store = useAuthStore()
  store.$patch({
    modeDetected: true, isLoading: false, emailAuthEnabled: true, emailCodeLoginEnabled: true,
    emailDeliveryAvailable: delivers, emailCodeInLog: codeInLog,
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
  // #4603: each test asks for a code for the same address; start every one
  // with an empty request clock.
  beforeEach(() => { window.location.hash = ''; window.sessionStorage.clear() })

  it('says no email was sent, that the code is nowhere to read, and the other ways in', async () => {
    const w = await requestCode({ delivers: false })
    expect(w.text()).not.toContain('a 6-digit code is on its way')
    const text = notice(w).text()
    expect(text).toContain('No email provider is configured')
    expect(text).toContain('was not delivered and is not stored anywhere')
    expect(text).toContain('Admin Login')
    expect(text).toContain('Settings → Integrations')
  })

  it('does not send anyone to the backend log for a code that is not in it', async () => {
    const w = await requestCode({ delivers: false })
    expect(notice(w).text()).not.toContain('docker compose logs')
    expect(notice(w).text()).not.toContain('backend log')
    expect(w.find('[data-testid="login-code-in-log"]').exists()).toBe(false)
  })

  it('names the log and the command on a development install that prints the code', async () => {
    const w = await requestCode({ delivers: false, codeInLog: true })
    const text = notice(w).text()
    expect(text).toContain('its code was written to the backend log')
    expect(text).toContain('docker compose logs backend')
    expect(w.find('[data-testid="login-code-nowhere"]').exists()).toBe(false)
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

  it.each([true, null])('an install that delivers (or an older backend: %s) reads as an honest maybe (#4693)', async (delivers) => {
    const w = await requestCode({ delivers })
    expect(w.text()).toContain('is registered, a 6-digit code is on its way')
    expect(notice(w).exists()).toBe(false)
  })

  it('reads the flag from the auth-mode response', async () => {
    setActivePinia(createPinia())
    const store = useAuthStore()
    vi.spyOn(axios, 'get').mockResolvedValueOnce({ data: {
      email_auth_enabled: true, email_code_login_enabled: true, email_delivery_available: false } })
    await store.detectAuthMode()
    expect(store.emailDeliveryAvailable).toBe(false)
    expect(store.emailCodeInLog).toBe(false)               // an older backend has no field
  })

  it('reads whether the code is in the log, and only an explicit true counts', async () => {
    setActivePinia(createPinia())
    const store = useAuthStore()
    vi.spyOn(axios, 'get').mockResolvedValueOnce({ data: {
      email_auth_enabled: true, email_delivery_available: false, email_code_in_log: true } })
    await store.detectAuthMode()
    expect(store.emailCodeInLog).toBe(true)
    vi.spyOn(axios, 'get').mockResolvedValueOnce({ data: {
      email_auth_enabled: true, email_delivery_available: false, email_code_in_log: 'yes' } })
    await store.detectAuthMode()
    expect(store.emailCodeInLog).toBe(false)
  })
})
