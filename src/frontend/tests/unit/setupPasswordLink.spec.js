// @vitest-environment jsdom
/**
 * trinity-enterprise#1312 — the /setup page opened from an emailed setup link.
 *
 * The link carries the setup code and the admin email, in the fragment
 * (`/setup#claim=…&email=…`, the form that is sent) or the query (accepted).
 * On an `instance-id` install the page reads both, clears them from the address
 * bar at once, hides the instance-ID field, shows the email read-only and posts
 * the code as `claim_code`. A refused link names the next step and never says
 * "instance ID". A reload after the address bar was cleared asks for the link
 * again; the code itself is never stored. On any other install the values are
 * ignored, but still cleared from the address bar.
 *
 * The Login page's one-line "already set up" notice is mounted here too.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const nav = vi.hoisted(() => ({ query: {}, hash: '' }))
const post = vi.fn()
const get = vi.fn()
const push = vi.fn()
const replace = vi.fn()
const loginWithCredentials = vi.fn()
let claimRequired = 'instance-id'

vi.mock('axios', () => {
  const instance = {
    get: (...a) => get(...a),
    post: (...a) => post(...a),
    defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: nav.query, hash: nav.hash, params: {} }),
  useRouter: () => ({ push, replace }),
}))
vi.mock('../../src/router', () => ({
  clearSetupCache: vi.fn(),
  getSetupClaimRequired: () => claimRequired,
}))
vi.mock('../../src/components/TrinityMark.vue', () => ({ default: { template: '<span />' } }))

// eslint-disable-next-line import/first
import SetupPassword from '../../src/views/SetupPassword.vue'
// eslint-disable-next-line import/first
import Login from '../../src/views/Login.vue'
// eslint-disable-next-line import/first
import { useAuthStore } from '../../src/stores/auth'

const PW = 'Sup3rSecret!!'
const CODE = 'a'.repeat(64)
const EMAIL = 'owner@example.com'
const MISMATCH = 'That instance ID does not match this server.'

function mountPage() {
  const w = mount(SetupPassword)
  useAuthStore().loginWithCredentials = loginWithCredentials
  return w
}

async function fillPasswords(w) {
  await w.find('#password').setValue(PW)
  await w.find('#confirmPassword').setValue(PW)
}

const submitDisabled = (w) => w.find('button.cta').attributes('disabled') !== undefined

beforeEach(() => {
  setActivePinia(createPinia())
  post.mockReset()
  get.mockReset()
  push.mockReset()
  replace.mockReset()
  loginWithCredentials.mockReset()
  sessionStorage.clear()
  nav.query = {}
  nav.hash = ''
  claimRequired = 'instance-id'
})

describe('a setup link on an instance-id install', () => {
  it.each([
    ['the fragment', () => { nav.hash = `#claim=${CODE}&email=${encodeURIComponent(EMAIL)}` }],
    ['the query', () => { nav.query = { claim: CODE, email: EMAIL } }],
  ])('reads the code and email from %s and clears the address bar', async (_l, arrange) => {
    arrange()
    const w = mountPage()
    expect(replace).toHaveBeenCalledWith({ path: '/setup' })
    expect(w.find('#instanceId').exists()).toBe(false)
    const emailInput = w.find('#adminEmail')
    expect(emailInput.element.value).toBe(EMAIL)
    expect(emailInput.attributes('readonly')).toBeDefined()
    expect(emailInput.attributes('aria-readonly')).toBe('true')
    expect(w.find('label[for="adminEmail"]').text()).not.toContain('(optional)')
    // The code is never rendered.
    expect(w.html()).not.toContain(CODE)
  })

  it('posts the code as claim_code with the link email, then signs in', async () => {
    nav.hash = `#claim=${CODE}&email=${encodeURIComponent(EMAIL)}`
    post.mockResolvedValue({ data: { email_registered: true } })
    loginWithCredentials.mockResolvedValue(true)
    const w = mountPage()
    expect(submitDisabled(w)).toBe(true)
    await fillPasswords(w)
    expect(submitDisabled(w)).toBe(false)
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(post).toHaveBeenCalledTimes(1)
    const [url, body] = post.mock.calls[0]
    expect(url).toBe('/api/setup/admin-password')
    expect(body.claim_code).toBe(CODE)
    expect(body.email).toBe(EMAIL)
    expect(loginWithCredentials).toHaveBeenCalledWith(EMAIL, PW)
    expect(push).toHaveBeenCalledWith('/')
    expect(sessionStorage.getItem('setup-link-opened')).toBeNull()
  })

  it('keeps the email editable and required when the link carried none', async () => {
    nav.hash = `#claim=${CODE}`
    const w = mountPage()
    const emailInput = w.find('#adminEmail')
    expect(emailInput.attributes('readonly')).toBeUndefined()
    await fillPasswords(w)
    expect(submitDisabled(w)).toBe(true)
    await emailInput.setValue(EMAIL)
    expect(submitDisabled(w)).toBe(false)
  })

  it('turns a refused link into the "ask for a new link" card, never "instance ID"', async () => {
    nav.hash = `#claim=${CODE}&email=${encodeURIComponent(EMAIL)}`
    post.mockRejectedValue({ response: { status: 403, data: { detail: MISMATCH } } })
    const w = mountPage()
    await fillPasswords(w)
    await w.find('form').trigger('submit')
    await flushPromises()
    const card = w.find('[data-testid="setup-link-problem"]')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('ask for a new one')
    expect(w.text()).not.toMatch(/instance id/i)
    expect(w.find('form').exists()).toBe(false)
    expect(push).not.toHaveBeenCalled()
  })

  it('sends an already-set-up refusal to login with the notice, at once', async () => {
    nav.hash = `#claim=${CODE}&email=${encodeURIComponent(EMAIL)}`
    post.mockRejectedValue({ response: { status: 403, data: { detail: 'Setup already completed.' } } })
    const w = mountPage()
    await fillPasswords(w)
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(push).toHaveBeenCalledWith({ path: '/login', query: { notice: 'setup-complete' } })
    expect(w.text()).not.toMatch(/instance id/i)
  })

  it.each([
    ['an email but no code', { email: EMAIL }],
    ['a code of the wrong shape', { claim: 'short', email: EMAIL }],
    ['a repeated code (an array)', { claim: [CODE, CODE], email: EMAIL }],
  ])('shows the link card for %s, without the form', (_l, query) => {
    nav.query = query
    const w = mountPage()
    expect(replace).toHaveBeenCalledWith({ path: '/setup' })
    expect(w.find('[data-testid="setup-link-problem"]').exists()).toBe(true)
    expect(w.find('form').exists()).toBe(false)
    expect(w.text()).not.toMatch(/instance id/i)
  })

  it('after a reload asks for the link again, and stored only a flag', () => {
    nav.hash = `#claim=${CODE}&email=${encodeURIComponent(EMAIL)}`
    mountPage().unmount()
    for (let i = 0; i < sessionStorage.length; i++) {
      expect(sessionStorage.getItem(sessionStorage.key(i))).not.toContain(CODE)
    }
    // The reload: same tab, address bar already cleared.
    nav.hash = ''
    replace.mockReset()
    const w = mountPage()
    expect(replace).not.toHaveBeenCalled()
    const card = w.find('[data-testid="setup-link-problem"]')
    expect(card.exists()).toBe(true)
    expect(card.text()).toContain('open the link from your email again')
    expect(w.find('#instanceId').exists()).toBe(false)
  })

  it('reads the link once the status fetch that failed in the guard succeeds', async () => {
    claimRequired = undefined
    get.mockResolvedValue({ data: { claim_required: 'instance-id' } })
    nav.hash = `#claim=${CODE}&email=${encodeURIComponent(EMAIL)}`
    const w = mountPage()
    await flushPromises()
    expect(w.find('#instanceId').exists()).toBe(false)
    expect(w.find('#adminEmail').element.value).toBe(EMAIL)
  })
})

describe('a setup link on any other install', () => {
  it('is ignored, but still cleared from the address bar', () => {
    claimRequired = null
    nav.hash = `#claim=${CODE}&email=${encodeURIComponent(EMAIL)}`
    const w = mountPage()
    expect(replace).toHaveBeenCalledWith({ path: '/setup' })
    expect(w.find('#adminEmail').element.value).toBe('')
    expect(w.find('#adminEmail').attributes('readonly')).toBeUndefined()
    expect(w.find('[data-testid="setup-link-problem"]').exists()).toBe(false)
  })
})

describe('the Login notice after a used link', () => {
  it('says the instance is already set up when the guard sent the notice', () => {
    nav.query = { notice: 'setup-complete' }
    const w = mount(Login)
    const notice = w.find('[data-testid="login-setup-complete"]')
    expect(notice.exists()).toBe(true)
    expect(notice.text()).toContain('already set up')
  })

  it('is absent otherwise', () => {
    const w = mount(Login)
    expect(w.find('[data-testid="login-setup-complete"]').exists()).toBe(false)
  })
})
