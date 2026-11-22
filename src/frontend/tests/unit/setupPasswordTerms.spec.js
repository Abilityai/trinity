// @vitest-environment jsdom
/**
 * trinity-enterprise#1329 — registration terms on the /setup page, link mode.
 *
 * Opened from an emailed setup link, the page asks `GET
 * /api/setup/registration-terms` (no parameters: never the code, never the
 * email). 404 → the page is as before. 200 → a terms block above the submit
 * button, a required tick, and `registration_terms_version` in the post.
 * 503 → a "could not be loaded" line with Retry, submit disabled. The submit
 * refusals 409 / 400 / 503 are each handled on the page.
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
import { useAuthStore } from '../../src/stores/auth'

const PW = 'Sup3rSecret!!'
const CODE = 'b'.repeat(64)
const EMAIL = 'owner@example.com'
const TERMS_URL = '/api/setup/registration-terms'
const TERMS = {
  version: 'v3',
  title: 'Service terms',
  text: '## Use\nBe **kind**.<img src=x onerror="alert(1)"><script>window.__pwned=1</script>',
  url: 'https://example.com/terms',
}

const httpError = (status, data = {}) => Object.assign(new Error(`HTTP ${status}`), { response: { status, data } })

function termsAnswer(...answers) {
  // Each call to the terms route takes the next answer; other GETs are unused.
  const queue = [...answers]
  get.mockImplementation(async (url) => {
    if (url !== TERMS_URL) throw new Error(`unexpected GET ${url}`)
    const a = queue.length > 1 ? queue.shift() : queue[0]
    if (a instanceof Error) throw a
    return { data: a }
  })
}

async function mountLinkPage() {
  nav.hash = `#claim=${CODE}&email=${encodeURIComponent(EMAIL)}`
  const w = mount(SetupPassword, { attachTo: document.body })
  useAuthStore().loginWithCredentials = loginWithCredentials
  await flushPromises()
  await w.find('#password').setValue(PW)
  await w.find('#confirmPassword').setValue(PW)
  return w
}

const submitDisabled = (w) => w.find('button.cta').attributes('disabled') !== undefined
const block = (w) => w.find('[data-testid="registration-terms"]')
const tick = (w) => w.find('#acceptRegistrationTerms')

beforeEach(() => {
  setActivePinia(createPinia())
  for (const f of [post, get, push, replace, loginWithCredentials]) f.mockReset()
  sessionStorage.clear()
  document.body.innerHTML = ''
  nav.query = {}
  nav.hash = ''
  claimRequired = 'instance-id'
})

describe('registration terms on a setup link', () => {
  it('asks for the terms with no parameters at all — never the code or the email', async () => {
    termsAnswer(httpError(404))
    await mountLinkPage()
    expect(get).toHaveBeenCalledTimes(1)
    expect(get.mock.calls[0]).toEqual([TERMS_URL])
    expect(JSON.stringify(get.mock.calls)).not.toContain(CODE)
    expect(JSON.stringify(get.mock.calls)).not.toContain('owner')
  })

  it('404: no terms block, submit enabled, no version posted', async () => {
    termsAnswer(httpError(404))
    post.mockResolvedValue({ data: { email_registered: true } })
    loginWithCredentials.mockResolvedValue(true)
    const w = await mountLinkPage()
    expect(block(w).exists()).toBe(false)
    expect(w.find('[data-testid="registration-terms-unavailable"]').exists()).toBe(false)
    expect(submitDisabled(w)).toBe(false)
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(post.mock.calls[0][1]).not.toHaveProperty('registration_terms_version')
  })

  it('200: shows title, sanitized text, link and a labelled tick above the submit button', async () => {
    termsAnswer(TERMS)
    const w = await mountLinkPage()
    const b = block(w)
    expect(b.exists()).toBe(true)
    expect(b.text()).toContain('Service terms')
    const body = b.find('[data-testid="registration-terms-text"]')
    expect(body.find('strong').text()).toBe('kind')
    expect(body.html()).not.toContain('onerror')
    expect(body.find('script').exists()).toBe(false)
    expect(window.__pwned).toBeUndefined()
    const a = b.find('a[href="https://example.com/terms"]')
    expect(a.exists()).toBe(true)
    expect(a.attributes('rel')).toContain('noopener')
    // The tick is a real checkbox with a label naming the terms.
    expect(tick(w).attributes('type')).toBe('checkbox')
    expect(w.find('label[for="acceptRegistrationTerms"]').text()).toMatch(/I have read and accept .*Service terms/)
    // Above the submit button.
    const cta = w.find('button.cta').element
    expect(b.element.compareDocumentPosition(cta) & Node.DOCUMENT_POSITION_FOLLOWING).toBeTruthy()
  })

  it('200 with only a url: no text box, the link is shown', async () => {
    termsAnswer({ ...TERMS, text: '' })
    const w = await mountLinkPage()
    expect(block(w).find('[data-testid="registration-terms-text"]').exists()).toBe(false)
    expect(block(w).find('a[href="https://example.com/terms"]').exists()).toBe(true)
  })

  it('never renders a non-http link', async () => {
    termsAnswer({ ...TERMS, url: 'javascript:alert(1)' })
    const w = await mountLinkPage()
    expect(block(w).html()).not.toContain('javascript:')
  })

  it('200: submit disabled until ticked, then posts registration_terms_version', async () => {
    termsAnswer(TERMS)
    post.mockResolvedValue({ data: { email_registered: true } })
    loginWithCredentials.mockResolvedValue(true)
    const w = await mountLinkPage()
    expect(submitDisabled(w)).toBe(true)
    await tick(w).setValue(true)
    expect(submitDisabled(w)).toBe(false)
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(post.mock.calls[0][1].registration_terms_version).toBe('v3')
    expect(push).toHaveBeenCalledWith('/')
  })

  it('503: a "could not be loaded" line with Retry, submit disabled; Retry loads them', async () => {
    termsAnswer(httpError(503, { detail: { code: 'registration_terms_unavailable', retryable: true } }), TERMS)
    const w = await mountLinkPage()
    const line = w.find('[data-testid="registration-terms-unavailable"]')
    expect(line.exists()).toBe(true)
    expect(line.text()).toMatch(/could not be loaded/i)
    expect(submitDisabled(w)).toBe(true)
    await line.find('button').trigger('click')
    await flushPromises()
    expect(get).toHaveBeenCalledTimes(2)
    expect(block(w).exists()).toBe(true)
    expect(w.find('[data-testid="registration-terms-unavailable"]').exists()).toBe(false)
  })

  it('submit 409 registration_terms_changed: reloads the terms, unticks, says they changed', async () => {
    termsAnswer(TERMS, { ...TERMS, version: 'v4', title: 'Service terms (new)' })
    post.mockRejectedValue(httpError(409, { detail: { code: 'registration_terms_changed', version: 'v4' } }))
    const w = await mountLinkPage()
    await tick(w).setValue(true)
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(get).toHaveBeenCalledTimes(2)
    expect(tick(w).element.checked).toBe(false)
    expect(submitDisabled(w)).toBe(true)
    expect(block(w).text()).toContain('Service terms (new)')
    expect(w.text()).toMatch(/terms have changed/i)
    expect(push).not.toHaveBeenCalled()
    // The next submit carries the new version.
    post.mockResolvedValue({ data: { email_registered: true } })
    await tick(w).setValue(true)
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(post.mock.calls[1][1].registration_terms_version).toBe('v4')
  })

  it('submit 400 registration_terms_required: unticked, submit disabled, asks to accept', async () => {
    termsAnswer(TERMS)
    post.mockRejectedValue(httpError(400, { detail: { code: 'registration_terms_required' } }))
    const w = await mountLinkPage()
    await tick(w).setValue(true)
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(tick(w).element.checked).toBe(false)
    expect(submitDisabled(w)).toBe(true)
    expect(w.text()).toMatch(/accept the terms/i)
    expect(w.text()).not.toContain('[object Object]')
  })

  it('submit 503 registration_terms_unavailable: shows the retry line', async () => {
    termsAnswer(TERMS)
    post.mockRejectedValue(httpError(503, { detail: { code: 'registration_terms_unavailable', retryable: true } }))
    const w = await mountLinkPage()
    await tick(w).setValue(true)
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(w.find('[data-testid="registration-terms-unavailable"]').exists()).toBe(true)
    expect(submitDisabled(w)).toBe(true)
    expect(w.text()).not.toContain('[object Object]')
  })
})

describe('outside link mode', () => {
  it('never asks for registration terms', async () => {
    claimRequired = null
    termsAnswer(TERMS)
    mount(SetupPassword)
    await flushPromises()
    expect(get).not.toHaveBeenCalled()
  })
})
