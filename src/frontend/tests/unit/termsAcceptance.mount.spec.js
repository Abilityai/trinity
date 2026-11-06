// @vitest-environment jsdom
/**
 * trinity-enterprise#1054 — the terms screen a person accepts once.
 *
 * Mounted against a stubbed `api` and a memory-history router, so what is read
 * is the rendered page, the requests it sends and where it navigates:
 *  - the text and its version are shown; the one accept action stays disabled
 *    until the box is ticked, and sends the version that was shown;
 *  - a successful accept leaves for the Workspace (or the page asked for);
 *  - a stale version reloads the text, says so, and asks for a fresh tick;
 *  - an invitation that is no longer live offers no accept action, only a way out;
 *  - declining signs out — the person is never left with no way off the page;
 *  - the page never navigates by itself while nothing was accepted (a refusal
 *    that keeps arriving cannot become a redirect loop).
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))
const ax = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), defaults: { headers: { common: {} } } }))
vi.mock('axios', () => ({ default: ax }))

import TermsAcceptance from '@/views/TermsAcceptance.vue'
import { useAuthStore } from '@/stores/auth'

// trinity-enterprise#1107: the routes are OSS — every Trinity serves its terms.
const CURRENT_URL = '/api/terms/current'
const ACCEPT_URL = '/api/terms/accept'

const Blank = { template: '<div />' }

function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/terms', component: TermsAcceptance },
      { path: '/workspace', component: Blank },
      { path: '/login', component: Blank },
      { path: '/', component: Blank },
    ],
  })
}

function current(over = {}) {
  return { data: { required: true, version: '2026-11', title: 'House rules', text: '## Terms\n\nBe kind.', accepted: false, is_default: false, ...over } }
}

function refusal(status, detail) {
  const e = new Error(`HTTP ${status}`)
  e.response = { status, data: { detail } }
  return e
}

async function mountAt(path = '/terms') {
  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  const wrapper = mount(TermsAcceptance, { global: { plugins: [router] } })
  await flushPromises()
  return { wrapper, router }
}

const box = (w) => w.find('[data-testid="terms-agree"]')
const acceptBtn = (w) => w.find('[data-testid="terms-accept"]')

beforeEach(() => {
  setActivePinia(createPinia())
  for (const f of [api.get, api.post, ax.post]) f.mockReset()
  const auth = useAuthStore()
  auth.user = { username: 'maya@example.com', email: 'maya@example.com', role: 'user' }
  auth.isAuthenticated = true
  auth.token = 'tok'
  api.get.mockImplementation((url) => (url === CURRENT_URL ? Promise.resolve(current()) : Promise.reject(new Error(url))))
})

describe('TermsAcceptance — reading and accepting', () => {
  it('shows the version and the rendered text', async () => {
    const { wrapper } = await mountAt()
    expect(api.get).toHaveBeenCalledWith(CURRENT_URL)
    expect(wrapper.find('[data-testid="terms-version"]').text()).toContain('2026-11')
    expect(wrapper.find('[data-testid="terms-text"]').html()).toContain('Be kind.')
  })

  it('keeps the one accept action disabled until the box is ticked', async () => {
    const { wrapper } = await mountAt()
    expect(acceptBtn(wrapper).attributes('disabled')).toBeDefined()
    await box(wrapper).setValue(true)
    expect(acceptBtn(wrapper).attributes('disabled')).toBeUndefined()
    expect(wrapper.findAll('[data-testid="terms-accept"]')).toHaveLength(1)
  })

  it('accepts the version it showed and leaves for the Workspace', async () => {
    api.post.mockResolvedValue({ data: { accepted: true, created: true, version: '2026-11', accepted_at: 'x' } })
    const { wrapper, router } = await mountAt()
    await box(wrapper).setValue(true)
    await acceptBtn(wrapper).trigger('click')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(ACCEPT_URL, { version: '2026-11' })
    expect(router.currentRoute.value.path).toBe('/workspace')
  })

  it('returns to the Workspace page it was sent from', async () => {
    api.post.mockResolvedValue({ data: { accepted: true, created: true, version: '2026-11', accepted_at: 'x' } })
    const { wrapper, router } = await mountAt('/terms?redirect=%2Fworkspace%3Fagent%3Dscout')
    await box(wrapper).setValue(true)
    await acceptBtn(wrapper).trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.fullPath).toBe('/workspace?agent=scout')
  })

  it('a stale version reloads the text, says so, and asks for a fresh tick', async () => {
    api.post.mockRejectedValue(refusal(409, { code: 'version_stale', message: 'changed', version: '2026-12' }))
    const { wrapper, router } = await mountAt()
    api.get.mockResolvedValue(current({ version: '2026-12', text: 'New text.' }))
    await box(wrapper).setValue(true)
    await acceptBtn(wrapper).trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/terms')
    expect(wrapper.find('[data-testid="terms-version"]').text()).toContain('2026-12')
    expect(wrapper.find('[data-testid="terms-text"]').html()).toContain('New text.')
    expect(wrapper.find('[data-testid="terms-stale"]').exists()).toBe(true)
    expect(box(wrapper).element.checked).toBe(false)
    expect(acceptBtn(wrapper).attributes('disabled')).toBeDefined()
  })

  it('a failed accept is shown beside the action and the page stays', async () => {
    api.post.mockRejectedValue(refusal(500, 'boom'))
    const { wrapper, router } = await mountAt()
    await box(wrapper).setValue(true)
    await acceptBtn(wrapper).trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="inline-error"]').exists()).toBe(true)
    expect(router.currentRoute.value.path).toBe('/terms')
  })
})

describe('TermsAcceptance — ways out', () => {
  it('declining signs out and goes to the sign-in page', async () => {
    const { wrapper, router } = await mountAt()
    const auth = useAuthStore()
    const logout = vi.spyOn(auth, 'logout').mockResolvedValue()
    await wrapper.find('[data-testid="terms-decline"]').trigger('click')
    await flushPromises()
    expect(logout).toHaveBeenCalledTimes(1)
    expect(router.currentRoute.value.path).toBe('/login')
    expect(api.post).not.toHaveBeenCalled()
  })

  it('an invitation that is no longer live offers no accept, only sign-out', async () => {
    const { wrapper } = await mountAt('/terms?reason=invite_revoked')
    expect(wrapper.find('[data-testid="terms-closed"]').exists()).toBe(true)
    expect(acceptBtn(wrapper).exists()).toBe(false)
    expect(wrapper.find('[data-testid="terms-decline"]').exists()).toBe(true)
  })

  it('a refusal on accept that says the invite expired closes the page the same way', async () => {
    api.post.mockRejectedValue(refusal(403, { code: 'invite_expired', message: 'gone', version: null }))
    const { wrapper } = await mountAt()
    await box(wrapper).setValue(true)
    await acceptBtn(wrapper).trigger('click')
    await flushPromises()
    expect(wrapper.find('[data-testid="terms-closed"]').exists()).toBe(true)
    expect(acceptBtn(wrapper).exists()).toBe(false)
  })

  it('a failed load offers retry and sign-out, never an accept', async () => {
    api.get.mockRejectedValue(refusal(500, 'down'))
    const { wrapper } = await mountAt()
    expect(wrapper.find('[data-testid="load-failed"]').exists()).toBe(true)
    expect(acceptBtn(wrapper).exists()).toBe(false)
    expect(wrapper.find('[data-testid="terms-decline"]').exists()).toBe(true)
  })

  it('already accepted: a Continue the person clicks — the page never moves by itself', async () => {
    api.get.mockResolvedValue(current({ accepted: true }))
    const { wrapper, router } = await mountAt()
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/terms')
    expect(acceptBtn(wrapper).exists()).toBe(false)
    await wrapper.find('[data-testid="terms-continue"]').trigger('click')
    await flushPromises()
    expect(router.currentRoute.value.path).toBe('/workspace')
  })

  it('nothing published: says so and offers Continue, no accept', async () => {
    api.get.mockResolvedValue(current({ required: false, version: '', text: '' }))
    const { wrapper, router } = await mountAt()
    expect(router.currentRoute.value.path).toBe('/terms')
    expect(acceptBtn(wrapper).exists()).toBe(false)
    expect(wrapper.find('[data-testid="terms-continue"]').exists()).toBe(true)
  })

  it('shows a skeleton, not an accept, before the text arrives', async () => {
    let resolve
    api.get.mockReturnValue(new Promise((r) => { resolve = r }))
    const router = makeRouter()
    await router.push('/terms')
    await router.isReady()
    const wrapper = mount(TermsAcceptance, { global: { plugins: [router] } })
    await flushPromises()
    expect(wrapper.find('[aria-busy="true"]').exists()).toBe(true)
    expect(acceptBtn(wrapper).exists()).toBe(false)
    resolve(current())
    await flushPromises()
    expect(acceptBtn(wrapper).exists()).toBe(true)
  })
})
