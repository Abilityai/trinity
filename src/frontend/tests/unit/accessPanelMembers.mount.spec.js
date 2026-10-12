// @vitest-environment jsdom
/**
 * trinity-enterprise#837 PR 2 (AC5) — Agent Detail › Access: the platform role
 * on each person's row, and "Give a platform account" for a Pending one.
 *
 * Mounted: every control here gates a grant (a role applies on every agent; an
 * account is a platform login), so the tests drive the rendered panel against
 * stubbed transports and read the requests it sends. The serves section is
 * stubbed — its own spec covers it — and only the props it receives are read.
 *
 * What is pinned:
 *  - the controls appear only for an entitled, SERVER-verified admin, never on
 *    their own row (the route refuses a self-change); everyone else keeps the
 *    read-only chip and PR 1's copy;
 *  - every role change asks first (the approval gate, 2026-10-12), and a cancel
 *    or a refusal puts the select back on the stored role — a native select
 *    keeps a picked value the model never took;
 *  - "Give a platform account" asks first, says what a platform account sees,
 *    calls the store, and reloads the list.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), patch: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))
const ax = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('axios', () => ({ default: ax }))

import AccessPanel from '@/components/AccessPanel.vue'
import { useAuthStore } from '@/stores/auth'
import { useEnterpriseStore } from '@/stores/enterprise'

const AGENT = 'scout'
const ACCESS_URL = `/api/agents/${AGENT}/access`
const MEMBERS_URL = '/api/enterprise/user-management/members'

let rows
const SAM = () => ({ email: 'sam@example.com', username: 'sam', role: 'operator', status: 'active', last_active: '2026-10-11T09:00:00Z', allow_proactive: false })
const MAYA = () => ({ email: 'maya@example.com', username: 'maya@example.com', role: 'user', status: 'active', last_active: null, allow_proactive: false })
const ME = () => ({ email: 'admin@example.com', username: 'admin', role: 'admin', status: 'active', last_active: '2026-10-12T08:00:00Z', allow_proactive: false })
const LEE = () => ({ email: 'lee@example.com', username: null, role: null, status: 'pending', last_active: null, allow_proactive: false })

function jwtFor(sub) {
  const b64 = (o) => btoa(JSON.stringify(o)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_')
  return `${b64({ alg: 'HS256' })}.${b64({ sub })}.sig`
}

function render({ admin = true, verified = true, features = ['user_management', 'assignments'] } = {}) {
  const auth = useAuthStore()
  auth.user = { role: admin ? 'admin' : 'operator' }
  auth.token = jwtFor(admin ? 'admin' : 'olga')
  auth.profileVerified = verified
  auth.isAuthenticated = true
  const ent = useEnterpriseStore()
  ent.enterpriseFeatures = features
  ent.featureFlagsLoaded = true
  return mount(AccessPanel, {
    props: { agentName: AGENT, agentOwner: 'olga' },
    attachTo: document.body,
    global: { stubs: { AgentAssignmentsSection: { name: 'AgentAssignmentsSection', props: ['agentName', 'agentOwner', 'people', 'reloadPeople'], template: '<div data-testid="serves-stub" />' } } },
  })
}

const rowOf = (w, email) => w.findAll('li').find((li) => li.text().includes(email))
const dialog = () => document.body.querySelector('[data-testid="confirm-dialog"]')
const dialogText = () => dialog()?.textContent || ''
const clickDialog = async (which) => {
  document.body.querySelector(`[data-testid="confirm-dialog-${which}"]`).click()
  await flushPromises()
}
const refused = (status, detail) => Object.assign(new Error(String(status)), { response: { status, data: { detail } } })
const accessReads = () => ax.get.mock.calls.filter(([u]) => u === ACCESS_URL).length

beforeEach(() => {
  setActivePinia(createPinia())
  document.body.innerHTML = ''
  for (const fn of [...Object.values(api), ...Object.values(ax)]) fn.mockReset()
  rows = [SAM(), MAYA(), ME(), LEE()]
  ax.get.mockImplementation(async (url) => (url === ACCESS_URL ? { data: rows.map((r) => ({ ...r })) } : { data: [] }))
  api.get.mockResolvedValue({ data: [] })
  api.put.mockImplementation(async (url, body) => ({ data: { username: url.split('/')[3], role: body.role } }))
  api.post.mockResolvedValue({ data: { id: 9, username: 'lee@example.com', email: 'lee@example.com', role: 'user', whitelist: 'added', email_sent: true } })
})

describe('who gets the controls', () => {
  it('an entitled, server-verified admin gets a role select on every other active row', async () => {
    const w = render()
    await flushPromises()
    const selects = w.findAll('[data-testid="access-role"]')
    expect(selects).toHaveLength(2)                                    // sam, maya — never your own row
    expect(rowOf(w, 'sam@example.com').find('select').element.value).toBe('operator')
    expect(rowOf(w, 'maya@example.com').find('select').element.value).toBe('user')
    expect(rowOf(w, 'maya@example.com').find('select').text()).toContain('user — Workspace only')
    // "every agent" is in the accessible name and the header, not a caption under every select.
    expect(rowOf(w, 'sam@example.com').find('select').attributes('aria-label')).toBe('Platform role for sam (every agent)')
    expect(w.text()).not.toContain('Platform role · every agent')
    const mine = rowOf(w, 'admin@example.com')
    expect(mine.find('select').exists()).toBe(false)
    expect(mine.text()).toContain('admin (you)')
    // One fixed-width slot on EVERY row — empty on your own — so Proactive and Remove line up.
    for (const li of w.findAll('li')) expect(li.findAll('[data-testid="access-member-slot"]')).toHaveLength(1)
    expect(mine.find('[data-testid="access-member-slot"]').text()).toBe('')
    expect(rowOf(w, 'lee@example.com').find('[data-testid="access-give-account"]').exists()).toBe(true)
    expect(rowOf(w, 'sam@example.com').find('[data-testid="access-give-account"]').exists()).toBe(false)
  })

  it.each([
    ['an install without the module', { features: ['assignments'] }],
    ['a non-admin owner', { admin: false }],
    ['a profile the server has not verified yet', { verified: false }],
  ])('%s keeps the read-only chip and the pointer to Settings', async (_label, opts) => {
    const w = render(opts)
    await flushPromises()
    expect(w.find('[data-testid="access-role"]').exists()).toBe(false)
    expect(w.find('[data-testid="access-give-account"]').exists()).toBe(false)
    expect(w.find('[data-testid="access-member-slot"]').exists()).toBe(false)
    expect(rowOf(w, 'sam@example.com').text()).toContain('operator')
    expect(w.text()).toContain('an admin sets in Settings → Access')
  })

  it('an admin is told the role applies to every agent', async () => {
    const w = render()
    await flushPromises()
    expect(w.text()).toContain('A platform role applies to every agent')
    expect(w.text()).not.toContain('an admin sets in Settings → Access')
  })

  it('an active account that never signed in says so', async () => {
    const w = render()
    await flushPromises()
    expect(rowOf(w, 'maya@example.com').text()).toContain('never signed in')
    expect(rowOf(w, 'sam@example.com').text()).not.toContain('never signed in')
  })
})

describe('changing a role', () => {
  it('asks before raising, and only then writes', async () => {
    const w = render()
    await flushPromises()
    await rowOf(w, 'sam@example.com').find('select').setValue('creator')
    expect(dialogText()).toContain('Raise sam to creator?')
    expect(api.put).not.toHaveBeenCalled()
    const before = accessReads()
    await clickDialog('confirm')
    expect(api.put).toHaveBeenCalledWith('/api/users/sam/role', { role: 'creator' })
    expect(accessReads()).toBe(before + 1)                             // the list re-reads the server
    expect(w.text()).toContain('sam is now creator on every agent.')
  })

  it('asks before lowering to user, with what it costs', async () => {
    const w = render()
    await flushPromises()
    await rowOf(w, 'sam@example.com').find('select').setValue('user')
    expect(dialogText()).toContain('Lower sam to user — Workspace only?')
    expect(dialogText()).toContain('They lose the operator UI on every agent.')
    await clickDialog('confirm')
    expect(api.put).toHaveBeenCalledWith('/api/users/sam/role', { role: 'user' })
  })

  it('says what an admin can do before making one', async () => {
    const w = render()
    await flushPromises()
    await rowOf(w, 'maya@example.com').find('select').setValue('admin')
    expect(dialogText()).toContain('Raise maya@example.com to admin?')
    expect(dialogText()).toContain('including other people')
  })

  it('a cancel writes nothing and puts the select back', async () => {
    const w = render()
    await flushPromises()
    const select = rowOf(w, 'sam@example.com').find('select')
    await select.setValue('admin')
    await clickDialog('cancel')
    expect(api.put).not.toHaveBeenCalled()
    expect(rowOf(w, 'sam@example.com').find('select').element.value).toBe('operator')
  })

  it('Esc is a cancel', async () => {
    const w = render()
    await flushPromises()
    await rowOf(w, 'sam@example.com').find('select').setValue('user')
    document.body.querySelector('[role="dialog"]').dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }))
    await flushPromises()
    expect(dialog()).toBe(null)
    expect(api.put).not.toHaveBeenCalled()
    expect(rowOf(w, 'sam@example.com').find('select').element.value).toBe('operator')
  })

  it('one role save at a time: every other select waits for it', async () => {
    let release
    api.put.mockImplementationOnce(() => new Promise((ok) => { release = () => ok({ data: {} }) }))
    const w = render()
    await flushPromises()
    await rowOf(w, 'sam@example.com').find('select').setValue('creator')
    await clickDialog('confirm')
    expect(rowOf(w, 'maya@example.com').find('select').attributes('disabled')).toBeDefined()
    release()
    await flushPromises()
    expect(rowOf(w, 'maya@example.com').find('select').attributes('disabled')).toBeUndefined()
  })

  it('a refusal shows on the row in the server\'s words and puts the select back', async () => {
    api.put.mockRejectedValueOnce(refused(400, 'Cannot change your own role'))
    const w = render()
    await flushPromises()
    await rowOf(w, 'sam@example.com').find('select').setValue('creator')
    await clickDialog('confirm')
    const row = rowOf(w, 'sam@example.com')
    expect(row.text()).toContain('Cannot change your own role')
    expect(row.find('select').element.value).toBe('operator')
  })
})

describe('giving a platform account', () => {
  it('asks first, says what the account sees, then creates it at user and reloads', async () => {
    const w = render()
    await flushPromises()
    await rowOf(w, 'lee@example.com').find('[data-testid="access-give-account"]').trigger('click')
    expect(dialogText()).toContain('Give lee@example.com a platform account?')
    expect(dialogText()).toContain('colleagues, not to outside clients')
    expect(api.post).not.toHaveBeenCalled()
    const before = accessReads()
    rows[3] = { ...LEE(), username: 'lee@example.com', role: 'user', status: 'active' }
    await clickDialog('confirm')
    expect(api.post).toHaveBeenCalledWith(MEMBERS_URL, { email: 'lee@example.com', role: 'user' })
    expect(accessReads()).toBe(before + 1)
    expect(w.text()).toContain('lee@example.com now has a platform account (user — Workspace only)')
    expect(rowOf(w, 'lee@example.com').find('select').element.value).toBe('user')
  })

  it('a cancel creates nothing', async () => {
    const w = render()
    await flushPromises()
    await rowOf(w, 'lee@example.com').find('[data-testid="access-give-account"]').trigger('click')
    await clickDialog('cancel')
    expect(api.post).not.toHaveBeenCalled()
  })

  it('names a role an earlier allow-list entry gave, and an unsent email', async () => {
    api.post.mockResolvedValueOnce({ data: { id: 9, username: 'lee@example.com', email: 'lee@example.com', role: 'operator', whitelist: 'kept', email_sent: false } })
    const w = render()
    await flushPromises()
    await rowOf(w, 'lee@example.com').find('[data-testid="access-give-account"]').trigger('click')
    await clickDialog('confirm')
    expect(w.text()).toContain('(operator)')
    expect(w.text()).toContain('earlier allow-list entry')
    expect(w.text()).toContain("invite email couldn't be sent")
  })

  it('a refusal shows on the row, and the list is read again', async () => {
    api.post.mockRejectedValueOnce(refused(409, 'lee@example.com already has a platform account (role: user)'))
    const w = render()
    await flushPromises()
    const before = accessReads()
    await rowOf(w, 'lee@example.com').find('[data-testid="access-give-account"]').trigger('click')
    await clickDialog('confirm')
    expect(rowOf(w, 'lee@example.com').text()).toContain('already has a platform account')
    expect(accessReads()).toBe(before + 1)
  })
})

describe('what the serves section is given', () => {
  it('the people list only once it has loaded, and a reload it can await', async () => {
    let release
    ax.get.mockImplementationOnce(() => new Promise((ok) => { release = () => ok({ data: rows.map((r) => ({ ...r })) }) }))
    const w = render()
    await flushPromises()
    const stub = w.findComponent({ name: 'AgentAssignmentsSection' })
    expect(w.find('[aria-busy="true"]').exists()).toBe(true)           // first load: the skeleton
    expect(stub.props('people')).toBe(null)
    expect(stub.props('agentOwner')).toBe('olga')
    release()
    await flushPromises()
    expect(stub.props('people').map((p) => p.email)).toEqual(rows.map((r) => r.email))
    const before = accessReads()
    await stub.props('reloadPeople')()
    expect(accessReads()).toBe(before + 1)
    expect(w.find('[aria-busy="true"]').exists()).toBe(false)          // a reload never re-shows the skeleton
  })

  it('another agent starts from no list: never the previous agent\'s people', async () => {
    const w = render()
    await flushPromises()
    await rowOf(w, 'sam@example.com').find('select').setValue('creator')   // a confirm left open
    let release
    ax.get.mockImplementationOnce(() => new Promise((ok) => { release = () => ok({ data: [MAYA()] }) }))
    await w.setProps({ agentName: 'other' })
    await flushPromises()
    expect(w.find('[aria-busy="true"]').exists()).toBe(true)
    expect(w.text()).not.toContain('sam@example.com')
    expect(dialog()).toBe(null)
    expect(w.findComponent({ name: 'AgentAssignmentsSection' }).props('people')).toBe(null)
    release()
    await flushPromises()
    expect(w.findAll('li').map((li) => li.text().includes('maya@example.com'))).toEqual([true])
  })

  it('a failed first load gives the section nothing to warn from', async () => {
    ax.get.mockRejectedValueOnce(refused(500, 'boom'))
    const w = render()
    await flushPromises()
    expect(w.findComponent({ name: 'AgentAssignmentsSection' }).props('people')).toBe(null)
    expect(w.text()).toContain("Couldn't load access list.")
  })
})
