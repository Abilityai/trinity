// @vitest-environment jsdom
/**
 * #3455 / #3456 — the Settings email whitelist: say what is wrong before
 * asking the server, and address a row by its exact stored value.
 *
 * #3455: the field is `type="email"` but sits outside a `<form>`, so the
 * browser's own check never ran and `a@`, `@b.com` and a 10 000-character
 * string all went to the server — which stored them. The rule now lives in
 * `utils/emailWhitelist.js` (mirroring `db_models.EmailWhitelistAdd`, which is
 * authoritative) and the settings store refuses to POST a value that fails it.
 *
 * #3456: a row whose text contains `/` could not be removed. The server half is
 * the route's `:path` converter; the client half is that the value is sent as
 * ONE encoded segment, which this pins.
 *
 * Three layers, each proving what only it can: the rule (pure), the store
 * actions against a mocked HTTP client (an invalid entry never reaches the
 * API; a row is addressed as one encoded segment), and `views/Settings.vue`
 * itself, shallow-mounted under jsdom — typing into the real field and
 * clicking the real buttons — so the wiring is proven by a click, not by a
 * regex over the SFC (#2918).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { createPinia, setActivePinia } from 'pinia'
import { flushPromises, shallowMount } from '@vue/test-utils'

// The full axios surface, because mounting Settings.vue pulls in `api.js`
// (`axios.create` + interceptors) through its stores.
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  const inst = mk()
  return { default: Object.assign(inst, { create: () => mk() }) }
})
vi.mock('vue-router', () => ({
  useRoute: () => ({ query: { tab: 'access' } }),
  useRouter: () => ({ push: vi.fn(), replace: vi.fn() }),
}))

import axios from 'axios'
import { useSettingsStore } from '../../src/stores/settings'
import {
  WHITELIST_EMAIL_MAX_LENGTH,
  validateWhitelistEmail,
  whitelistEntryUrl,
} from '../../src/utils/emailWhitelist'
import { apiErrorMessage } from '../../src/utils/apiError'
import { useAuthStore } from '../../src/stores/auth'
import Settings from '../../src/views/Settings.vue'

const LONG = 'a'.repeat(10_000)

beforeEach(() => {
  setActivePinia(createPinia())
  vi.restoreAllMocks()
  vi.clearAllMocks()
  axios.post.mockResolvedValue({ data: { success: true } })
  axios.delete.mockResolvedValue({ data: { success: true } })
})

describe('validateWhitelistEmail — the rule (#3455)', () => {
  it.each([
    ['a@', /not an email address/],
    ['@b.com', /not an email address/],
    ['not-an-email', /not an email address/],
    ['a@b@example.com', /not an email address/],
    ['', /Enter an email address/],
    ['   ', /Enter an email address/],
    ['user name@example.com', /cannot contain spaces/],
    ['user@exam\tple.com', /cannot contain spaces/],
    ['user@example.com\u0000', /cannot contain spaces/],
  ])('refuses %j with a named reason', (value, reason) => {
    const { email, error } = validateWhitelistEmail(value)
    expect(error).toMatch(reason)
    expect(email).toBe('')
  })

  // Labelled, so a 10 000-character value does not become the test's title.
  it.each([
    ['a 10 000-character string', LONG],
    ['a 262-character address', 'a'.repeat(250) + '@example.com'],
  ])('refuses %s as too long', (_label, value) => {
    const reason = /too long/
    const { email, error } = validateWhitelistEmail(value)
    expect(error).toMatch(reason)
    expect(email).toBe('')
  })

  it('names the fix with an example, every time (design-system principle 17)', () => {
    for (const bad of ['a@', '', 'user name@example.com']) {
      expect(validateWhitelistEmail(bad).error).toContain('user@example.com')
    }
  })

  it('states both lengths for an oversized entry, and never echoes it', () => {
    const { error } = validateWhitelistEmail(LONG)
    expect(error).toContain('10000')
    expect(error).toContain(String(WHITELIST_EMAIL_MAX_LENGTH))
    expect(error.length).toBeLessThan(200)
  })

  it.each([
    ['user@example.com', 'user@example.com'],
    ['  User@Example.COM \n', 'user@example.com'],
    ['first.last+tag@sub.example.com', 'first.last+tag@sub.example.com'],
    ['team/ops@example.com', 'team/ops@example.com'],
    ['user@localhost', 'user@localhost'],
  ])('accepts %j, trimmed and lower-cased', (value, normalized) => {
    expect(validateWhitelistEmail(value)).toEqual({ email: normalized, error: '' })
  })

  it('accepts an address of exactly the maximum length', () => {
    const edge = 'a'.repeat(WHITELIST_EMAIL_MAX_LENGTH - '@example.com'.length) + '@example.com'
    expect(edge.length).toBe(WHITELIST_EMAIL_MAX_LENGTH)
    expect(validateWhitelistEmail(edge)).toEqual({ email: edge, error: '' })
  })

  it('answers a non-string without throwing', () => {
    for (const v of [null, undefined, 7, {}]) {
      expect(validateWhitelistEmail(v).error).toMatch(/Enter an email address/)
    }
  })
})

describe('settings store — an invalid entry never reaches the API (#3455)', () => {
  it.each([
    ['"a@"', 'a@'],
    ['"@b.com"', '@b.com'],
    ['a 10 000-character string', LONG],
    ['only spaces', '   '],
  ])('does not POST %s, and rejects with the reason', async (_label, value) => {
    const store = useSettingsStore()

    const err = await store.addWhitelistEmail(value).catch((e) => e)

    expect(err).toBeInstanceOf(Error)
    expect(err.message).toBe(validateWhitelistEmail(value).error)
    expect(axios.post).not.toHaveBeenCalled()
    // What Settings.vue renders is `apiErrorMessage(err, …)`: the reason must
    // survive that, not collapse into the generic fallback.
    expect(apiErrorMessage(err, 'fallback')).toBe(err.message)
  })

  it('POSTs a valid entry trimmed and lower-cased, and returns what was sent', async () => {
    const store = useSettingsStore()

    const email = await store.addWhitelistEmail('  User@Example.COM ')

    expect(email).toBe('user@example.com')
    expect(axios.post).toHaveBeenCalledTimes(1)
    expect(axios.post).toHaveBeenCalledWith('/api/settings/email-whitelist', {
      email: 'user@example.com',
      source: 'manual',
      default_role: 'user',
    })
  })

  it("lets the server's own reason through when it still refuses", async () => {
    const refusal = { response: { status: 422, data: { detail: 'email is too long (300 characters; the limit is 254)' } } }
    axios.post.mockRejectedValue(refusal)
    const store = useSettingsStore()

    const err = await store.addWhitelistEmail('user@example.com').catch((e) => e)

    expect(err).toBe(refusal)
    expect(apiErrorMessage(err, 'fallback')).toBe('email is too long (300 characters; the limit is 254)')
  })
})

describe('removing a row addresses its exact stored value (#3456)', () => {
  it.each([
    ['team/ops@example.com', '/api/settings/email-whitelist/team%2Fops%40example.com'],
    ['not/an/email', '/api/settings/email-whitelist/not%2Fan%2Femail'],
    ['a//b', '/api/settings/email-whitelist/a%2F%2Fb'],
    ['what?x=1#frag', '/api/settings/email-whitelist/what%3Fx%3D1%23frag'],
    ['100% not an email', '/api/settings/email-whitelist/100%25%20not%20an%20email'],
    ['user@example.com', '/api/settings/email-whitelist/user%40example.com'],
  ])('DELETEs %j as one encoded segment', async (stored, url) => {
    const store = useSettingsStore()

    await store.removeWhitelistEmail(stored)

    expect(axios.delete).toHaveBeenCalledTimes(1)
    expect(axios.delete).toHaveBeenCalledWith(url)
    // …and it decodes back to exactly what the list showed.
    const sent = axios.delete.mock.calls[0][0]
    expect(decodeURIComponent(sent.slice('/api/settings/email-whitelist/'.length))).toBe(stored)
    expect(sent.slice('/api/settings/email-whitelist/'.length)).not.toContain('/')
  })

  it('does NOT validate on remove — a bad row that predates validation must go', async () => {
    const store = useSettingsStore()

    await store.removeWhitelistEmail(LONG)

    expect(axios.delete).toHaveBeenCalledWith(whitelistEntryUrl(LONG))
  })
})

describe('Settings.vue — the whitelist section, mounted', () => {
  const ROWS = [
    { id: 1, email: 'user@example.com', source: 'manual', added_at: null },
    { id: 2, email: 'team/ops@example.com', source: 'manual', added_at: null },
  ]
  const WHITELIST = '/api/settings/email-whitelist'
  const whitelistPosts = () => axios.post.mock.calls.filter(([url]) => url === WHITELIST)
  const whitelistGets = () => axios.get.mock.calls.filter(([url]) => url === WHITELIST)

  async function mountSettings() {
    const pinia = createPinia()
    setActivePinia(pinia)
    useAuthStore().$patch({ user: { role: 'admin' } })
    // Every other fetch Settings.vue makes at mount gets an empty answer; the
    // children are stubbed (shallow), so only the view's own loads run.
    axios.get.mockImplementation((url) => {
      if (url === WHITELIST) return Promise.resolve({ data: { whitelist: ROWS } })
      if (url === '/api/settings') return Promise.resolve({ data: [] })
      return Promise.resolve({ data: {} })
    })
    const wrapper = shallowMount(Settings, {
      global: { plugins: [pinia], stubs: { InlineError: false } },
    })
    await flushPromises()
    return wrapper
  }

  const field = (w) => w.find('input[type="email"]')
  const addButton = (w) => w.findAll('button').find((b) => b.text().includes('Add Email'))
  const reason = (w) => w.find('[data-testid="whitelist-add-error"]')

  beforeEach(() => {
    vi.spyOn(console, 'error').mockImplementation(() => {})
  })

  it('shows the whitelist on the Access tab (the harness reaches the section)', async () => {
    const w = await mountSettings()
    expect(field(w).exists()).toBe(true)
    expect(w.text()).toContain('team/ops@example.com')
    expect(reason(w).exists()).toBe(false)
  })

  it.each([
    ['"a@"', 'a@'],
    ['"@b.com"', '@b.com'],
    ['a 10 000-character string', LONG],
  ])('an invalid entry (%s) shows the reason beside the field and sends nothing', async (_label, value) => {
    const w = await mountSettings()

    await field(w).setValue(value)
    await addButton(w).trigger('click')
    await flushPromises()

    expect(reason(w).exists()).toBe(true)
    expect(reason(w).attributes('role')).toBe('alert')
    expect(reason(w).text()).toContain(validateWhitelistEmail(value).error)
    expect(whitelistPosts()).toEqual([])
    // The entry stays in the field to be corrected, and the control is live again.
    expect(field(w).element.value).toBe(value)
    expect(addButton(w).attributes('disabled')).toBeUndefined()
  })

  it('refuses on Enter exactly as it does on the button', async () => {
    const w = await mountSettings()

    await field(w).setValue('a@')
    await field(w).trigger('keyup.enter')
    await flushPromises()

    expect(reason(w).text()).toContain('not an email address')
    expect(whitelistPosts()).toEqual([])
  })

  it('a valid entry is sent trimmed and lower-cased, the field clears and the list reloads', async () => {
    const w = await mountSettings()
    const loadsBefore = whitelistGets().length

    await field(w).setValue('  New.User@Example.com ')
    await addButton(w).trigger('click')
    await flushPromises()

    expect(whitelistPosts()).toEqual([[WHITELIST, { email: 'new.user@example.com', source: 'manual', default_role: 'user' }]])
    expect(field(w).element.value).toBe('')
    expect(reason(w).exists()).toBe(false)
    expect(whitelistGets().length).toBe(loadsBefore + 1)
  })

  it("shows the server's own reason when it still refuses, and a corrected retry clears it", async () => {
    const w = await mountSettings()
    axios.post.mockRejectedValueOnce({
      response: { status: 422, data: { detail: 'email must not contain whitespace or control characters' } },
    })

    await field(w).setValue('user@example.com')
    await addButton(w).trigger('click')
    await flushPromises()

    expect(reason(w).text()).toContain('email must not contain whitespace or control characters')

    await addButton(w).trigger('click')
    await flushPromises()

    expect(reason(w).exists()).toBe(false)
  })

  it('the reason persists until dismissed', async () => {
    const w = await mountSettings()

    await field(w).setValue('a@')
    await addButton(w).trigger('click')
    await flushPromises()
    await reason(w).find('button[aria-label^="Dismiss"]').trigger('click')

    expect(reason(w).exists()).toBe(false)
  })

  it('Remove on a row containing a slash DELETEs its encoded value (#3456)', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(true)
    const w = await mountSettings()
    const row = w.findAll('tbody tr').find((tr) => tr.text().includes('team/ops@example.com'))

    await row.findAll('button').find((b) => b.text().includes('Remove')).trigger('click')
    await flushPromises()

    expect(axios.delete).toHaveBeenCalledTimes(1)
    expect(axios.delete.mock.calls[0][0]).toBe('/api/settings/email-whitelist/team%2Fops%40example.com')
  })

  it('a declined confirmation removes nothing', async () => {
    vi.spyOn(window, 'confirm').mockReturnValue(false)
    const w = await mountSettings()
    const row = w.findAll('tbody tr').find((tr) => tr.text().includes('team/ops@example.com'))

    await row.findAll('button').find((b) => b.text().includes('Remove')).trigger('click')
    await flushPromises()

    expect(axios.delete).not.toHaveBeenCalled()
  })
})
