// @vitest-environment jsdom
/**
 * #3004 — the /setup page on an `ADMIN_PASSWORD_SOURCE=instance-id` install.
 *
 * The first admin must give this server's EC2 instance ID; email becomes
 * optional (AWS review: no PII required) and the updates opt-in, which sends
 * the email, is off while there is none. A wrong ID stays on the page with a
 * named error: every other 403 still means "already set up" and goes to /login.
 * Every other install keeps the page as it was: email required, no ID field.
 *
 * Mounted rather than grepped (design-system contract): these are all
 * properties of what renders and what is posted.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'

const post = vi.fn()
const get = vi.fn()
const push = vi.fn()
const loginWithCredentials = vi.fn()
// null = the server said no claim; undefined = the guard's status fetch failed.
let claimRequired = null

vi.mock('axios', () => ({ default: { post: (...a) => post(...a), get: (...a) => get(...a) } }))
vi.mock('vue-router', () => ({ useRouter: () => ({ push }) }))
vi.mock('../../src/router', () => ({
  clearSetupCache: vi.fn(),
  getSetupClaimRequired: () => claimRequired,
}))
vi.mock('../../src/stores/auth', () => ({
  useAuthStore: () => ({ loginWithCredentials }),
}))
vi.mock('../../src/components/TrinityMark.vue', () => ({ default: { template: '<span />' } }))

// eslint-disable-next-line import/first
import SetupPassword from '../../src/views/SetupPassword.vue'

const PW = 'Sup3rSecret!!'
const IID = 'i-0abc1234def567890'
const MISMATCH = 'That instance ID does not match this server.'

async function fill(w, { id, email = '', pw = PW } = {}) {
  if (id !== undefined) await w.find('#instanceId').setValue(id)
  await w.find('#adminEmail').setValue(email)
  await w.find('#password').setValue(pw)
  await w.find('#confirmPassword').setValue(pw)
}

const submitDisabled = (w) => w.find('button.cta').attributes('disabled') !== undefined

beforeEach(() => {
  post.mockReset()
  get.mockReset()
  push.mockReset()
  loginWithCredentials.mockReset()
  claimRequired = null
})

describe('instance-id claim mode', () => {
  beforeEach(() => { claimRequired = 'instance-id' })

  it('asks for the instance ID first, with the console path and a link', () => {
    const w = mount(SetupPassword)
    const fields = w.findAll('.field')
    expect(fields[0].find('#instanceId').exists()).toBe(true)
    expect(fields[0].text()).toContain('EC2 instance ID')
    expect(fields[0].text()).toContain('EC2 console → Instances')
    const link = fields[0].find('a')
    expect(link.attributes('href')).toContain('console.aws.amazon.com/ec2')
    expect(link.attributes('rel')).toContain('noopener')
  })

  it('marks email optional and submits without one once the ID has the right shape', async () => {
    const w = mount(SetupPassword)
    expect(w.find('label[for="adminEmail"]').text()).toContain('(optional)')

    await fill(w, { id: 'not-an-id' })
    expect(submitDisabled(w)).toBe(true)
    expect(w.text()).toContain('Format: i-0abc123')

    await fill(w, { id: IID })
    expect(submitDisabled(w)).toBe(false)
  })

  it('still rejects a malformed email when one is given', async () => {
    const w = mount(SetupPassword)
    await fill(w, { id: IID, email: 'nope' })
    expect(submitDisabled(w)).toBe(true)
  })

  it('disables the updates opt-in while email is blank', async () => {
    const w = mount(SetupPassword)
    const consent = () => w.find('#consentUpdates')
    expect(consent().attributes('disabled')).toBeDefined()
    await w.find('#adminEmail').setValue('me@acme.com')
    expect(consent().attributes('disabled')).toBeUndefined()
  })

  it('posts the trimmed ID and a blank email, then signs in by the returned username', async () => {
    post.mockResolvedValue({ data: { success: true, email_registered: false, username: 'admin' } })
    loginWithCredentials.mockResolvedValue(true)
    const w = mount(SetupPassword)
    await fill(w, { id: `  ${IID} ` })
    await w.find('form').trigger('submit')
    await flushPromises()

    expect(post).toHaveBeenCalledWith('/api/setup/admin-password', expect.objectContaining({
      claim_code: IID, email: '', consent_updates: false,
    }))
    expect(loginWithCredentials).toHaveBeenCalledWith('admin', PW)
    expect(push).toHaveBeenCalledWith('/')
  })

  it('shows a wrong ID inline and stays on the page', async () => {
    post.mockRejectedValue({ response: { status: 403, data: { detail: MISMATCH } } })
    const w = mount(SetupPassword)
    await fill(w, { id: IID })
    await w.find('form').trigger('submit')
    await flushPromises()

    expect(w.find('.errbox').text()).toContain(MISMATCH)
    expect(w.text()).not.toContain('Setup has already been completed.')
    await new Promise((r) => setTimeout(r, 0))
    expect(push).not.toHaveBeenCalled()
  })
})

describe('every other install', () => {
  it('has no instance-ID field and still requires an email', async () => {
    const w = mount(SetupPassword)
    expect(w.find('#instanceId').exists()).toBe(false)
    expect(w.find('label[for="adminEmail"]').text()).not.toContain('(optional)')
    await fill(w, {})
    expect(submitDisabled(w)).toBe(true)
    await w.find('#adminEmail').setValue('me@acme.com')
    expect(submitDisabled(w)).toBe(false)
  })

  it('keeps the updates opt-in enabled and never asks the server again', () => {
    const w = mount(SetupPassword)
    expect(w.find('#consentUpdates').attributes('disabled')).toBeUndefined()
    expect(get).not.toHaveBeenCalled()
  })

  it('does not send a claim code', async () => {
    post.mockResolvedValue({ data: { success: true, email_registered: true, username: 'admin' } })
    loginWithCredentials.mockResolvedValue(true)
    const w = mount(SetupPassword)
    await fill(w, { email: 'me@acme.com' })
    await w.find('form').trigger('submit')
    await flushPromises()
    expect(post.mock.calls[0][1]).not.toHaveProperty('claim_code')
    expect(loginWithCredentials).toHaveBeenCalledWith('me@acme.com', PW)
  })
})

describe('claim mode unknown (the guard could not read /api/setup/status)', () => {
  beforeEach(() => { claimRequired = undefined })

  it('asks the server on mount and shows the ID field when it is required', async () => {
    get.mockResolvedValue({ data: { setup_completed: false, claim_required: 'instance-id' } })
    const w = mount(SetupPassword)
    expect(submitDisabled(w)).toBe(true)
    await flushPromises()
    expect(get).toHaveBeenCalledWith('/api/setup/status')
    expect(w.find('#instanceId').exists()).toBe(true)
  })

  it('shows the plain form when the server says no claim is needed', async () => {
    get.mockResolvedValue({ data: { setup_completed: false, claim_required: null } })
    const w = mount(SetupPassword)
    await flushPromises()
    expect(w.find('#instanceId').exists()).toBe(false)
    await fill(w, { email: 'me@acme.com' })
    expect(submitDisabled(w)).toBe(false)
  })

  it('says it could not load, and keeps submit off, when the retry fails too', async () => {
    get.mockRejectedValue(new Error('network'))
    const w = mount(SetupPassword)
    await flushPromises()
    expect(w.find('.errbox').text()).toContain('Could not reach this server')
    await fill(w, { email: 'me@acme.com' })
    expect(submitDisabled(w)).toBe(true)
  })
})

describe('layout stays put while typing (design-system: reserve space)', () => {
  // Every status line (strength, checklist, match, ID format) must already
  // have its box before the first keystroke, or the centred card shifts.
  const blocks = (w) => w.findAll('form *')
    .filter((n) => !n.element.closest('svg') && (!n.element.closest('.labrow') || n.classes('labrow')))
    .map((n) => n.element.tagName)

  it('renders the strength meter and checklist before any password is typed', () => {
    claimRequired = 'instance-id'
    const w = mount(SetupPassword)
    expect(w.find('.meter').exists()).toBe(true)
    expect(w.findAll('.reqs > div')).toHaveLength(5)
  })

  it('adds no block when the ID is malformed and the passwords differ', async () => {
    claimRequired = 'instance-id'
    const w = mount(SetupPassword)
    const before = blocks(w)
    await w.find('#instanceId').setValue('not-an-id')
    await w.find('#password').setValue(PW)
    await w.find('#confirmPassword').setValue(PW + 'x')
    expect(w.text()).toContain('Passwords do not match')
    expect(w.text()).toContain('Format: i-0abc123')
    expect(blocks(w)).toEqual(before)
  })
})
