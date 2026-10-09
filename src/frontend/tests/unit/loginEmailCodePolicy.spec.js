// @vitest-environment jsdom
/**
 * trinity-enterprise#849 — the login page honours the SSO "keep email-code
 * sign-in available" policy.
 *
 * With the policy off and an SSO provider enabled, the page shows the SSO
 * buttons and Admin Login and no email-code form. SSO buttons and Admin Login
 * no longer sit inside the email block, so they survive `email_auth_enabled`
 * being off too. Mounted, because the gate is a template predicate.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import axios from 'axios'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))

import Login from '../../src/views/Login.vue'
import { useAuthStore } from '../../src/stores/auth'

async function render({ emailAuth = true, emailCode = true, providers = [] } = {}) {
  setActivePinia(createPinia())
  const store = useAuthStore()
  store.$patch({
    modeDetected: true,
    isLoading: false,
    emailAuthEnabled: emailAuth,
    emailCodeLoginEnabled: emailCode,
  })
  store.fetchSsoProviders = vi.fn().mockResolvedValue(providers)
  const w = mount(Login)
  await flushPromises()
  return { w, store }
}

const OKTA = [{ id: 'p1', name: 'Okta' }]
const has = (w, sel) => w.find(sel).exists()
const adminButton = (w) => w.findAll('button').find((b) => b.text().includes('Admin Login'))

describe('Login — email-code policy (ent#849)', () => {
  beforeEach(() => {
    window.location.hash = ''
  })

  it('policy off + provider enabled: SSO + Admin Login, no email form', async () => {
    const { w } = await render({ emailCode: false, providers: OKTA })
    expect(has(w, '#email')).toBe(false)
    expect(has(w, 'a[href="/api/enterprise/sso/login/p1"]')).toBe(true)
    expect(adminButton(w)).toBeTruthy()
    expect(has(w, '#password')).toBe(false)
  })

  it('Admin Login from SSO-only offers a way back to the SSO buttons', async () => {
    const { w } = await render({ emailCode: false, providers: OKTA })
    await adminButton(w).trigger('click')
    expect(has(w, '#password')).toBe(true)
    const back = w.findAll('button').find((b) => b.text().includes('Back to sign-in options'))
    expect(back).toBeTruthy()
    await back.trigger('click')
    expect(has(w, 'a[href="/api/enterprise/sso/login/p1"]')).toBe(true)
  })

  it('policy on: email form, SSO buttons and Admin Login together (unchanged)', async () => {
    const { w } = await render({ emailCode: true, providers: OKTA })
    expect(has(w, '#email')).toBe(true)
    expect(has(w, 'a[href="/api/enterprise/sso/login/p1"]')).toBe(true)
    expect(adminButton(w)).toBeTruthy()
  })

  it('no provider and email on: email form + Admin Login (OSS default)', async () => {
    const { w } = await render({ emailCode: true, providers: [] })
    expect(has(w, '#email')).toBe(true)
    expect(adminButton(w)).toBeTruthy()
  })

  it('email auth off and no provider: admin form directly', async () => {
    const { w } = await render({ emailAuth: false, emailCode: false, providers: [] })
    expect(has(w, '#password')).toBe(true)
    expect(has(w, '#email')).toBe(false)
  })

  it('email auth off but a provider enabled: SSO stays reachable', async () => {
    const { w } = await render({ emailAuth: false, emailCode: false, providers: OKTA })
    expect(has(w, 'a[href="/api/enterprise/sso/login/p1"]')).toBe(true)
    expect(has(w, '#email')).toBe(false)
  })

  it('holds the loading state until the SSO providers are known', async () => {
    setActivePinia(createPinia())
    const store = useAuthStore()
    store.$patch({ modeDetected: true, isLoading: false, emailAuthEnabled: true, emailCodeLoginEnabled: false })
    let resolve
    store.fetchSsoProviders = vi.fn(() => new Promise((r) => { resolve = r }))
    const w = mount(Login)
    await flushPromises()
    expect(has(w, '[aria-busy="true"]')).toBe(true)
    expect(has(w, '#password')).toBe(false)
    resolve(OKTA)
    await flushPromises()
    expect(has(w, '[aria-busy="true"]')).toBe(false)
    expect(has(w, 'a[href="/api/enterprise/sso/login/p1"]')).toBe(true)
  })
})

describe('auth store — email-code calls follow the policy flag', () => {
  it('requestEmailCode refuses locally when email-code sign-in is off', async () => {
    setActivePinia(createPinia())
    const store = useAuthStore()
    store.$patch({ emailAuthEnabled: true, emailCodeLoginEnabled: false })
    const res = await store.requestEmailCode('a@example.com')
    expect(res.success).toBe(false)
  })

  it('a policy switched off after page load names SSO and re-reads the mode', async () => {
    setActivePinia(createPinia())
    const store = useAuthStore()
    store.$patch({ emailAuthEnabled: true, emailCodeLoginEnabled: true })
    const post = vi.spyOn(axios, 'post').mockRejectedValue(
      { response: { status: 403, data: { detail: 'email_code_disabled' } } })
    const get = vi.spyOn(axios, 'get').mockResolvedValue(
      { data: { email_auth_enabled: true, email_code_login_enabled: false } })
    const res = await store.requestEmailCode('a@example.com')
    expect(res.error).toBe('Email-code sign-in is turned off. Sign in with SSO.')
    expect(store.emailCodeLoginEnabled).toBe(false)
    post.mockRestore()
    get.mockRestore()
  })
})
