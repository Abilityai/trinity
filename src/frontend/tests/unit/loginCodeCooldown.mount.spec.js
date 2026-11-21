// @vitest-environment jsdom
/**
 * #4603 — the login page holds its own request clock for sign-in codes.
 *
 * Trinity sends at most 3 codes per 10 minutes to one address (a budget shared
 * with Telegram, Slack, WhatsApp and MCP sign-in). Before this, a person who
 * did not get a code went back and asked again, and each request over the
 * budget was answered "a code is on its way". Now the page refuses to send
 * inside its cooldown and names the rule; the code step has its own resend
 * control; and when the server says the address is over budget (429) the page
 * shows that sentence instead of promising a code.
 */
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import axios from 'axios'

vi.mock('vue-router', () => ({
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
  useRoute: () => ({ query: {}, path: '/login' }),
}))

import Login from '../../src/views/Login.vue'
import { useAuthStore } from '../../src/stores/auth'
import { recordCodeRequest, codeRequestWaitMs, LOCK_MS, WINDOW_MS } from '../../src/utils/loginCodeCooldown'

const OVER = 'Too many sign-in code requests. Try again in 10 minutes.'
const RULE = 'at most 3 codes per 10 minutes'

let post
async function mountLogin() {
  setActivePinia(createPinia())
  const store = useAuthStore()
  store.$patch({
    modeDetected: true, isLoading: false, emailAuthEnabled: true, emailCodeLoginEnabled: true,
    emailDeliveryAvailable: true,
  })
  store.fetchSsoProviders = vi.fn().mockResolvedValue([])
  const w = mount(Login)
  await flushPromises()
  return w
}

async function ask(w, email = 'dana@example.com') {
  await w.find('#email').setValue(email)
  await w.find('form').trigger('submit')
  await flushPromises()
}

const backToEmail = async (w) => {
  const back = w.findAll('button').find((b) => b.text().includes('Back to email'))
  await back.trigger('click')
  await flushPromises()
}
const sendButton = (w) => w.find('form button[type="submit"]')
const resend = (w) => w.find('[data-testid="login-resend-code"]')

beforeEach(() => {
  vi.useFakeTimers({ toFake: ['Date', 'setInterval', 'clearInterval'] })
  vi.setSystemTime(new Date('2026-11-21T10:00:00Z'))
  window.sessionStorage.clear()
  window.location.hash = ''
  post = vi.spyOn(axios, 'post').mockResolvedValue({ data: { success: true, message: 'ok' } })
})
afterEach(() => {
  vi.useRealTimers()
  vi.restoreAllMocks()
})

describe('Login — sign-in code cooldown (#4603)', () => {
  it('does not send a second request inside the cooldown, and says why', async () => {
    const w = await mountLogin()
    await ask(w)
    await backToEmail(w)
    expect(sendButton(w).attributes('disabled')).toBeDefined()
    expect(w.find('[data-testid="login-code-cooldown"]').text()).toContain(RULE)
    expect(w.find('[data-testid="login-code-cooldown"]').text()).toMatch(/\d:\d\d/)
    await w.find('form').trigger('submit')               // Enter must not bypass it
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(1)
  })

  it('lets the next request through once the lock has passed', async () => {
    const w = await mountLogin()
    await ask(w)
    await backToEmail(w)
    vi.advanceTimersByTime(LOCK_MS + 1000)
    await flushPromises()
    expect(sendButton(w).attributes('disabled')).toBeUndefined()
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(2)
  })

  it('does not hold a different address', async () => {
    const w = await mountLogin()
    await ask(w, 'dana@example.com')
    await backToEmail(w)
    await ask(w, 'other@example.com')
    expect(post).toHaveBeenCalledTimes(2)
  })

  it('offers a resend control on the code step, held by the same cooldown', async () => {
    const w = await mountLogin()
    await ask(w)
    expect(resend(w).exists()).toBe(true)
    expect(resend(w).attributes('disabled')).toBeDefined()
    expect(w.find('[data-testid="login-code-cooldown"]').text()).toContain(RULE)
    vi.advanceTimersByTime(LOCK_MS + 1000)
    await flushPromises()
    expect(resend(w).attributes('disabled')).toBeUndefined()
    await resend(w).trigger('click')
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(2)
    expect(w.find('#code').exists()).toBe(true)            // still on the code step
  })

  it('shows the over-budget sentence instead of "a code is on its way"', async () => {
    const w = await mountLogin()
    await ask(w)
    vi.advanceTimersByTime(LOCK_MS + 1000)
    await flushPromises()
    post.mockRejectedValueOnce({ response: { status: 429, data: { detail: OVER } } })
    await resend(w).trigger('click')
    await flushPromises()
    expect(w.find('[data-testid="inline-error"]').text()).toContain(OVER)
    expect(w.text()).not.toContain('a 6-digit code is on its way')
    expect(w.text()).not.toContain('Access Denied')
  })

  it('over budget from the email step stays on the email step', async () => {
    post.mockRejectedValueOnce({ response: { status: 429, data: { detail: OVER } } })
    const w = await mountLogin()
    await ask(w)
    expect(w.find('#code').exists()).toBe(false)
    expect(w.find('[data-testid="inline-error"]').text()).toContain(OVER)
    expect(w.text()).not.toContain('Access Denied')
    expect(sendButton(w).attributes('disabled')).toBeDefined()
  })
})

describe('loginCodeCooldown — the client request clock', () => {
  const T = Date.parse('2026-11-21T10:00:00Z')

  it('is free for an address never asked for', () => {
    expect(codeRequestWaitMs('a@x.com', T)).toBe(0)
  })

  it('holds one address for the lock after each request, case-insensitively', () => {
    recordCodeRequest('A@x.com ', T)
    expect(codeRequestWaitMs('a@x.com', T + 1000)).toBe(LOCK_MS - 1000)
    expect(codeRequestWaitMs('b@x.com', T + 1000)).toBe(0)
  })

  it('after three requests holds until the oldest leaves the window', () => {
    recordCodeRequest('a@x.com', T)
    recordCodeRequest('a@x.com', T + 2 * LOCK_MS)
    recordCodeRequest('a@x.com', T + 4 * LOCK_MS)
    const at = T + 6 * LOCK_MS
    expect(codeRequestWaitMs('a@x.com', at)).toBe(T + WINDOW_MS - at)
    expect(codeRequestWaitMs('a@x.com', T + WINDOW_MS)).toBe(0)
  })

  it('survives storage that throws', () => {
    const spy = vi.spyOn(Storage.prototype, 'getItem').mockImplementation(() => { throw new Error('blocked') })
    vi.spyOn(Storage.prototype, 'setItem').mockImplementation(() => { throw new Error('blocked') })
    expect(() => recordCodeRequest('a@x.com', T)).not.toThrow()
    expect(codeRequestWaitMs('a@x.com', T + 1000)).toBe(LOCK_MS - 1000)
    spy.mockRestore()
  })
})
