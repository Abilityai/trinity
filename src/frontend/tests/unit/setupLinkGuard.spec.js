// @vitest-environment jsdom
/**
 * trinity-enterprise#1312 — a used setup link lands on the sign-in page with a
 * notice, and a setup code never survives into a bug report.
 *
 * Driven through `router.push` on the app's own router, so the guard in
 * `router/index.js` is what runs. Once setup is completed, a /setup visit that
 * carried link values (fragment or query) goes to /login?notice=setup-complete
 * with nothing of the link forwarded; every other /setup visit still goes to a
 * bare /login, exactly as before.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'

vi.mock('axios', () => {
  const instance = {
    get: vi.fn(async () => ({ data: {} })),
    post: vi.fn(async () => ({ data: {} })),
    defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})
vi.mock('../../src/views/Login.vue', () => ({ default: { render: () => null } }))
vi.mock('../../src/views/SetupPassword.vue', () => ({ default: { render: () => null } }))
vi.mock('../../src/views/Dashboard.vue', () => ({ default: { render: () => null } }))

import router, { clearSetupCache } from '../../src/router'
import { useAuthStore } from '../../src/stores/auth'
import { scrub } from '../../src/utils/scrub.js'

const CODE = 'b'.repeat(64)
const here = () => router.currentRoute.value.fullPath

describe('the router guard on a completed install', () => {
  beforeEach(async () => {
    setActivePinia(createPinia())
    useAuthStore().isLoading = false
    clearSetupCache()
    globalThis.fetch = vi.fn(async () => ({ json: async () => ({ setup_completed: true }) }))
    await router.push('/chat')
  })

  it.each([
    ['a fragment link', `/setup#claim=${CODE}&email=owner%40example.com`],
    ['a query link', `/setup?claim=${CODE}&email=owner%40example.com`],
  ])('sends %s to login with the notice and nothing of the link', async (_l, url) => {
    await router.push(url)
    expect(here()).toBe('/login?notice=setup-complete')
    expect(here()).not.toContain(CODE)
    expect(here()).not.toContain('owner')
  })

  it('sends a plain /setup visit to a bare /login, as before', async () => {
    await router.push('/setup')
    expect(here()).toBe('/login')
  })
})

describe('scrub: a setup code in a URL', () => {
  it.each([
    [`https://host.example/setup#claim=${CODE}&email=owner%40example.com`],
    [`https://host.example/setup?claim=${CODE}`],
    [`/setup?x=1&claim=${CODE}`],
  ])('redacts the code in %s', (url) => {
    const out = scrub(url)
    expect(out).not.toContain(CODE)
    expect(out).toContain('claim=[REDACTED]')
  })
})
