// @vitest-environment jsdom
/**
 * trinity-enterprise#837 PR 2 (AC7) — "People this agent serves" and access.
 *
 * An assignment is a relationship, not an access grant (ent#500): the Inbox
 * shows an agent's asks only to people the agent is shared with (or who own
 * it), so a person assigned but not shared waits on asks nobody can see.
 * Decision D4 (2026-10-12): assigning someone also shares the agent with them,
 * and the screen says so. The approval gate added the order — the assignment
 * first, then the share — so a failed assignment grants nothing and a failed
 * share lands in the row's "Not shared" warning.
 *
 * Mounted against a stubbed `@/api` (the assignment endpoints) and `axios`
 * (the agents store's share call); the people list arrives as a prop, the way
 * AccessPanel passes it.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))
const ax = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('axios', () => ({ default: ax }))

import AgentAssignmentsSection from '@/components/AgentAssignmentsSection.vue'
import { useAuthStore } from '@/stores/auth'
import { useEnterpriseStore } from '@/stores/enterprise'

const AGENT = 'scout'
const BASE = `/api/enterprise/assignments/agents/${AGENT}`
const SHARE = (agent = AGENT) => `/api/agents/${agent}/share`

const USERS = [
  { id: 1, username: 'ann@example.com', name: 'Ann Lee', email: 'Ann@Example.com', role: 'user' },
  { id: 2, username: 'bob', name: 'Bob Ray', email: 'bob@example.com', role: 'operator' },
  { id: 3, username: 'cat@example.com', name: 'Cat Moe', email: 'cat@example.com', role: 'user' },
  { id: 4, username: 'olga', name: 'Olga Owner', email: 'olga@example.com', role: 'operator' },
  { id: 5, username: 'admin', name: 'The Admin', email: 'admin@example.com', role: 'admin' },
  { id: 6, username: 'nomail', name: 'No Mail', email: null, role: 'user' },
]
const ANN = { id: 'a1', user_id: 1, display_name: 'Ann Lee', user_email: 'ann@example.com', role_id: null, kind: 'primary', drift_state: 'no_role' }
const BOB = { id: 'a2', user_id: 2, display_name: 'Bob Ray', user_email: 'bob@example.com', role_id: null, kind: 'approver', drift_state: 'no_role' }
const PEOPLE = () => [
  { email: 'ann@example.com', username: 'ann@example.com', role: 'user', status: 'active' },
  { email: 'lee@example.com', username: null, role: null, status: 'pending' },
]

let roster
function jwtFor(sub) {
  const b64 = (o) => btoa(JSON.stringify(o)).replace(/=+$/, '').replace(/\+/g, '-').replace(/\//g, '_')
  return `${b64({ alg: 'HS256' })}.${b64({ sub })}.sig`
}

function render({ admin = true, people = PEOPLE(), reloadPeople = vi.fn(async () => {}), agentOwner = 'olga', agentName = AGENT } = {}) {
  const auth = useAuthStore()
  auth.user = { role: admin ? 'admin' : 'operator' }
  auth.token = jwtFor(admin ? 'admin' : 'olga')
  auth.profileVerified = true
  auth.isAuthenticated = true
  const ent = useEnterpriseStore()
  ent.enterpriseFeatures = ['assignments', 'user_management']
  ent.featureFlagsLoaded = true
  const w = mount(AgentAssignmentsSection, {
    props: { agentName, people, reloadPeople, agentOwner },
    attachTo: document.body,
  })
  return { w, reloadPeople }
}

const q = (w, id) => w.find(`[data-testid="${id}"]`)
const rowFor = (w, name) => w.findAll('li').find((li) => li.text().includes(name))
const refused = (status, detail) => Object.assign(new Error(String(status)), { response: { status, data: { detail } } })
const shares = () => ax.post.mock.calls.filter(([u]) => u.endsWith('/share'))

async function add(w, userId, kind = 'viewer') {
  await q(w, 'add-user').setValue(String(userId))
  await q(w, 'add-kind').setValue(kind)
  await q(w, 'assignment-add-form').trigger('submit')
  await flushPromises()
}

beforeEach(() => {
  setActivePinia(createPinia())
  for (const fn of [...Object.values(api), ...Object.values(ax)]) fn.mockReset()
  roster = { agent_name: AGENT, assignments: [ANN, BOB], mine: null, primary: 'Ann Lee' }
  api.get.mockImplementation(async (url) => {
    if (url === '/api/users') return { data: USERS }
    if (url.endsWith('/canon/roles')) return { data: { roles: [], reason: 'no_canon', message: 'none' } }
    return { data: roster }
  })
  api.post.mockImplementation(async (url, body) => {
    roster = { ...roster, assignments: [...roster.assignments, { id: `n${body.user_id}`, user_id: body.user_id, display_name: USERS.find((u) => u.id === body.user_id).name, user_email: (USERS.find((u) => u.id === body.user_id).email || '').toLowerCase() || null, kind: body.kind, drift_state: 'no_role' }] }
    return { data: {} }
  })
  api.patch.mockResolvedValue({ data: {} })
  api.delete.mockResolvedValue({ data: {} })
  ax.post.mockResolvedValue({ data: {} })
})

describe('each person\'s platform role is shown', () => {
  it('on the rows', async () => {
    const { w } = render()
    await flushPromises()
    expect(rowFor(w, 'Ann Lee').find('[data-testid="assignment-platform-role"]').text()).toBe('user — Workspace only')
    expect(rowFor(w, 'Bob Ray').find('[data-testid="assignment-platform-role"]').text()).toBe('operator')
  })

  it('in the pickers', async () => {
    const { w } = render()
    await flushPromises()
    const options = q(w, 'add-user').findAll('option').map((o) => o.text())
    expect(options).toContain('Cat Moe · Workspace only')
    expect(options).toContain('Bob Ray · operator')
  })

  it('from the people list when the viewer cannot read the users list', async () => {
    const { w } = render({ admin: false })
    await flushPromises()
    expect(api.get).not.toHaveBeenCalledWith('/api/users')
    expect(rowFor(w, 'Ann Lee').find('[data-testid="assignment-platform-role"]').text()).toBe('user — Workspace only')
    expect(rowFor(w, 'Bob Ray').find('[data-testid="assignment-platform-role"]').exists()).toBe(false)
  })

  it('names who is not offered because they have no account yet', async () => {
    const { w } = render()
    await flushPromises()
    expect(q(w, 'assignments-not-offered').text()).toBe('Not offered: lee@example.com (no platform account yet).')
  })
})

describe('assigning someone shares the agent with them', () => {
  it('writes the assignment first, then the share, and says so', async () => {
    const { w, reloadPeople } = render()
    await flushPromises()
    await add(w, 3)
    expect(api.post).toHaveBeenCalledWith(BASE, { user_id: 3, kind: 'viewer' })
    expect(ax.post).toHaveBeenCalledWith(SHARE(), { email: 'cat@example.com' }, expect.anything())
    expect(api.post.mock.invocationCallOrder[0]).toBeLessThan(ax.post.mock.invocationCallOrder[0])
    expect(q(w, 'assignments-share-note').text()).toBe('Shared with Cat Moe so they can use it in the Workspace.')
    expect(reloadPeople).toHaveBeenCalled()
  })

  it('tells the admin before they add anyone', async () => {
    const { w } = render()
    await flushPromises()
    expect(q(w, 'assignments-share-hint').text()).toContain("also shares it with them")
  })

  it.each([
    ['the owner', 4],
    ['yourself', 5],
    ['an account with no email', 6],
  ])('never shares with %s', async (_label, userId) => {
    const { w } = render()
    await flushPromises()
    await add(w, userId)
    expect(api.post).toHaveBeenCalled()
    expect(shares()).toEqual([])
  })

  it('an already-shared person is not news', async () => {
    ax.post.mockRejectedValueOnce(refused(409, 'Agent is already shared with cat@example.com'))
    const { w, reloadPeople } = render()
    await flushPromises()
    await add(w, 3)
    expect(shares()).toHaveLength(1)
    expect(q(w, 'assignments-share-note').exists()).toBe(false)
    expect(w.text()).not.toContain('already shared')
    expect(reloadPeople).not.toHaveBeenCalled()
  })

  it('a failed assignment shares nothing', async () => {
    api.post.mockRejectedValueOnce(refused(409, 'That assignment already exists.'))
    const { w } = render()
    await flushPromises()
    await add(w, 3)
    expect(w.text()).toContain('That assignment already exists.')
    expect(shares()).toEqual([])
  })

  it('a failed share keeps the assignment and shows on the row why it is not shared', async () => {
    ax.post.mockRejectedValueOnce(refused(404, 'Agent not found'))
    const { w } = render()
    await flushPromises()
    await add(w, 3)
    const row = rowFor(w, 'Cat Moe')
    expect(row.find('[data-testid="assignment-not-shared"]').text()).toContain("Not shared with Cat Moe: they can't see this agent or its asks in the Workspace.")
    expect(row.find('[data-testid="assignment-not-shared"]').text()).toContain('Agent not found')
    expect(q(w, 'assignments-share-note').exists()).toBe(false)
  })

  it('a failed share is said even when the people list is not on screen', async () => {
    ax.post.mockRejectedValueOnce(refused(404, 'Agent not found'))
    const { w } = render({ people: null })
    await flushPromises()
    await add(w, 3)
    expect(w.text()).toContain('Added, but not shared with them')
    expect(w.text()).toContain('Agent not found')
  })

  it('shares the agent it assigned on, even if the page moved on meanwhile', async () => {
    let release
    api.post.mockImplementationOnce(() => new Promise((ok) => { release = () => ok({ data: {} }) }))
    const { w } = render()
    await flushPromises()
    await q(w, 'add-user').setValue('3')
    await q(w, 'assignment-add-form').trigger('submit')
    await w.setProps({ agentName: 'other' })
    release()
    await flushPromises()
    expect(ax.post).toHaveBeenCalledWith(SHARE(AGENT), { email: 'cat@example.com' }, expect.anything())
    expect(ax.post).not.toHaveBeenCalledWith(SHARE('other'), expect.anything(), expect.anything())
  })

  it('replacing the primary shares the agent with the new one', async () => {
    const { w } = render()
    await flushPromises()
    await q(w, 'assignment-replace').trigger('click')
    await q(w, 'replace-user').setValue('3')
    await q(w, 'assignment-replace-form').trigger('submit')
    await flushPromises()
    expect(ax.post).toHaveBeenCalledWith(SHARE(), { email: 'cat@example.com' }, expect.anything())
  })
})

describe('an assignment the agent is not shared for', () => {
  it('warns on the row and offers the share', async () => {
    const { w, reloadPeople } = render()
    await flushPromises()
    const bob = rowFor(w, 'Bob Ray')
    expect(bob.find('[data-testid="assignment-not-shared"]').exists()).toBe(true)
    expect(rowFor(w, 'Ann Lee').find('[data-testid="assignment-not-shared"]').exists()).toBe(false)
    await bob.find('[data-testid="assignment-share"]').trigger('click')
    await flushPromises()
    expect(ax.post).toHaveBeenCalledWith(SHARE(), { email: 'bob@example.com' }, expect.anything())
    expect(reloadPeople).toHaveBeenCalled()
  })

  it('compares addresses without case', async () => {
    roster.assignments = [{ ...ANN, user_email: 'ANN@example.com' }]
    const { w } = render()
    await flushPromises()
    expect(rowFor(w, 'Ann Lee').find('[data-testid="assignment-not-shared"]').exists()).toBe(false)
  })

  it('says nothing until the people list has loaded', async () => {
    const { w } = render({ people: null })
    await flushPromises()
    expect(w.find('[data-testid="assignment-not-shared"]').exists()).toBe(false)
  })

  it('never warns about the owner or yourself', async () => {
    roster.assignments = [
      { ...ANN },
      { id: 'a4', user_id: 4, display_name: 'Olga Owner', user_email: 'olga@example.com', kind: 'viewer', drift_state: 'no_role' },
      { id: 'a5', user_id: 5, display_name: 'The Admin', user_email: 'admin@example.com', kind: 'approver', drift_state: 'no_role' },
    ]
    const { w } = render()
    await flushPromises()
    expect(w.find('[data-testid="assignment-not-shared"]').exists()).toBe(false)
  })

  it('says nothing about a row until the users list says who the owner is', async () => {
    roster.assignments = [{ id: 'a4', user_id: 4, display_name: 'Olga Owner', user_email: 'olga@example.com', kind: 'viewer', drift_state: 'no_role' }]
    api.get.mockImplementation(async (url) => {
      if (url === '/api/users') throw refused(500, 'boom')
      if (url.endsWith('/canon/roles')) return { data: { roles: [], reason: 'no_canon', message: 'none' } }
      return { data: roster }
    })
    const { w } = render()
    await flushPromises()
    expect(w.find('[data-testid="assignment-not-shared"]').exists()).toBe(false)
  })

  it('is an admin hint: a non-admin sees no warning', async () => {
    const { w } = render({ admin: false })
    await flushPromises()
    expect(w.find('[data-testid="assignment-not-shared"]').exists()).toBe(false)
  })
})
