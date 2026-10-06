// @vitest-environment jsdom
/**
 * trinity-enterprise#810 — Agent Detail › Access: "People this agent serves".
 *
 * The admin UI over the ent#500 assignment endpoints. Mounted (#2918): every
 * write is a predicate on what the admin sees and clicks, so the tests drive
 * the rendered section against a stubbed API and read the requests it sends.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), patch: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))

import AgentAssignmentsSection from '@/components/AgentAssignmentsSection.vue'
import { useAuthStore } from '@/stores/auth'
import { useEnterpriseStore } from '@/stores/enterprise'
import { replacePrimarySteps, seatLine, driftNote, writeError } from '@/utils/assignments'

const AGENT = 'scout'
const ANN = { id: 'a1', user_id: 1, display_name: 'Ann Lee', role_id: 'cfo', kind: 'primary', drift_state: 'unknown', proactive_consent: true, proactive_consent_state: 'consented' }
const BOB = { id: 'a2', user_id: 2, display_name: 'Bob Ray', role_id: 'cfo', kind: 'approver', drift_state: 'changed', proactive_consent: false, proactive_consent_state: 'not_asked' }
const USERS = [
  { id: 1, username: 'ann', name: 'Ann Lee' },
  { id: 2, username: 'bob', name: 'Bob Ray' },
  { id: 3, username: 'cat', name: 'Cat Moe' },
  { id: 4, username: 'dan', name: 'Dan Off', suspended_at: '2026-01-01T00:00:00Z' },
]

let roster
let canonRoles
function serve() {
  api.get.mockImplementation(async (url) => {
    if (url === '/api/users') return { data: USERS }
    if (url === `/api/agents/${AGENT}/canon/roles`) return { data: canonRoles }
    return { data: roster }
  })
}

function render({ admin = true, entitled = true } = {}) {
  const auth = useAuthStore()
  auth.user = { role: admin ? 'admin' : 'user' }
  auth.profileVerified = true
  auth.isAuthenticated = true
  const ent = useEnterpriseStore()
  ent.enterpriseFeatures = entitled ? ['assignments'] : []
  ent.featureFlagsLoaded = true
  return mount(AgentAssignmentsSection, { props: { agentName: AGENT }, attachTo: document.body })
}

const conflict = (detail) => Object.assign(new Error('409'), { response: { status: 409, data: { detail } } })
const q = (w, id) => w.find(`[data-testid="${id}"]`)

beforeEach(() => {
  setActivePinia(createPinia())
  for (const fn of Object.values(api)) fn.mockReset()
  roster = { agent_name: AGENT, assignments: [ANN, BOB], mine: null, primary: 'Ann Lee' }
  canonRoles = { agent_name: AGENT, roles: [], unavailable: null, reason: 'no_canon', message: 'this agent declares no canon' }
  serve()
  api.post.mockResolvedValue({ data: {} })
  api.patch.mockResolvedValue({ data: {} })
  api.delete.mockResolvedValue({ data: {} })
})

describe('gating', () => {
  it('is absent, not broken, without the assignments module', async () => {
    const w = render({ entitled: false })
    await flushPromises()
    expect(q(w, 'agent-assignments').exists()).toBe(false)
    expect(api.get).not.toHaveBeenCalled()
  })
})

describe('what an admin sees', () => {
  it('shows the primary apart from the stakeholders, with the seat it serves', async () => {
    const w = render()
    await flushPromises()
    expect(q(w, 'assignments-seat').text()).toBe('Serves the seat of its primary: cfo (Ann Lee)')
    expect(q(w, 'assignment-primary').text()).toContain('Ann Lee')
    expect(q(w, 'assignment-primary').text()).toContain('Primary')
    const rows = w.findAll('[data-testid="assignment-row"]')
    expect(rows).toHaveLength(1)
    expect(rows[0].text()).toContain('Bob Ray')
  })

  it('shows consent and a changed role file as a warning on its row', async () => {
    const w = render()
    await flushPromises()
    expect(q(w, 'assignment-primary').find('[data-testid="assignment-consent"]').text()).toBe('Proactive briefs: consented')
    const bob = w.find('[data-testid="assignment-row"]')
    expect(bob.find('[data-testid="assignment-consent"]').text()).toBe('Proactive briefs: not asked')
    expect(bob.find('[data-testid="assignment-drift"]').text()).toContain('role file changed')
  })

  it('warns when there is no primary, and names the next action when nobody is assigned', async () => {
    roster = { agent_name: AGENT, assignments: [], mine: null, primary: null }
    const w = render()
    await flushPromises()
    expect(q(w, 'assignments-no-primary').text()).toContain('nowhere to go')
    expect(q(w, 'assignments-empty').text()).toContain('Add the person this agent coaches first.')
    expect(q(w, 'assignments-seat').text()).toBe('No seat yet')
  })
})

describe('writes', () => {
  it('adds a person picked by name, with the seat filled in, and reloads', async () => {
    const w = render()
    await flushPromises()
    const options = q(w, 'add-user').findAll('option').map((o) => o.text())
    expect(options).toContain('Cat Moe')
    expect(options).not.toContain('Dan Off')                      // suspended
    expect(q(w, 'add-role').element.value).toBe('cfo')            // the agent's seat
    await q(w, 'add-user').setValue('3')
    await q(w, 'add-kind').setValue('viewer')
    await q(w, 'assignment-add-form').trigger('submit')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(`/api/enterprise/assignments/agents/${AGENT}`, { user_id: 3, role_id: 'cfo', kind: 'viewer' })
    expect(api.get.mock.calls.filter(([u]) => u.startsWith('/api/enterprise/assignments')).length).toBe(2)
  })

  it('surfaces the backend error by name when a write is refused', async () => {
    api.post.mockRejectedValueOnce(conflict('That assignment already exists, or this agent already has a primary human.'))
    const w = render()
    await flushPromises()
    await q(w, 'add-user').setValue('2')
    await q(w, 'assignment-add-form').trigger('submit')
    await flushPromises()
    expect(w.text()).toContain('That assignment already exists, or this agent already has a primary human.')
  })

  it('changes a kind with one action on the row', async () => {
    const w = render()
    await flushPromises()
    await w.find('[data-testid="assignment-row"] [data-testid="assignment-kind"]').setValue('viewer')
    await flushPromises()
    expect(api.patch).toHaveBeenCalledWith(`/api/enterprise/assignments/agents/${AGENT}/a2`, { kind: 'viewer' })
  })

  it('removes a stakeholder', async () => {
    const w = render()
    await flushPromises()
    await w.find('[data-testid="assignment-row"] [data-testid="assignment-remove"]').trigger('click')
    await flushPromises()
    expect(api.delete).toHaveBeenCalledWith(`/api/enterprise/assignments/agents/${AGENT}/a2`)
  })
})

describe('replacing the primary', () => {
  async function replaceWith(w, userId, oldBecomes) {
    await q(w, 'assignment-replace').trigger('click')
    await q(w, 'replace-user').setValue(String(userId))
    await q(w, 'replace-old-becomes').setValue(oldBecomes)
    await q(w, 'assignment-replace-form').trigger('submit')
    await flushPromises()
  }

  it('demotes the old primary before writing the new one', async () => {
    const order = []
    api.patch.mockImplementation(async (url, body) => { order.push(['patch', url, body]); return { data: {} } })
    api.post.mockImplementation(async (url, body) => { order.push(['post', url, body]); return { data: {} } })
    const w = render()
    await flushPromises()
    await replaceWith(w, 3, 'viewer')
    expect(order).toEqual([
      ['patch', `/api/enterprise/assignments/agents/${AGENT}/a1`, { kind: 'viewer' }],
      ['post', `/api/enterprise/assignments/agents/${AGENT}`, { user_id: 3, role_id: 'cfo', kind: 'primary' }],
    ])
    expect(q(w, 'assignment-replace-form').exists()).toBe(false)
  })

  it('puts the old primary back when the new one cannot be written', async () => {
    api.post.mockRejectedValueOnce(conflict('User not found'))
    const w = render()
    await flushPromises()
    await replaceWith(w, 3, 'viewer')
    expect(api.patch.mock.calls).toEqual([
      [`/api/enterprise/assignments/agents/${AGENT}/a1`, { kind: 'viewer' }],
      [`/api/enterprise/assignments/agents/${AGENT}/a1`, { kind: 'primary' }],
    ])
    expect(q(w, 'assignment-replace-form').text()).toContain('User not found')
  })
})

describe('who cannot change it', () => {
  it('shows a non-admin the same section read-only, controls hidden', async () => {
    const w = render({ admin: false })
    await flushPromises()
    expect(q(w, 'assignment-primary').text()).toContain('Ann Lee')
    expect(q(w, 'assignment-add-form').exists()).toBe(false)
    expect(q(w, 'assignment-kind').exists()).toBe(false)
    expect(q(w, 'assignment-remove').exists()).toBe(false)
    expect(q(w, 'assignment-replace').exists()).toBe(false)
    expect(q(w, 'assignments-readonly-note').text()).toBe('Only an instance admin can change who this agent serves.')
    expect(api.get).not.toHaveBeenCalledWith('/api/users')
  })

  it('shows a shared user only what the backend gives them', async () => {
    roster = { agent_name: AGENT, assignments: null, mine: { ...BOB }, primary: 'Ann Lee' }
    const w = render({ admin: false })
    await flushPromises()
    expect(q(w, 'assignments-limited').text()).toContain('Primary: Ann Lee')
    expect(q(w, 'assignments-limited').text()).toContain('You are an approver on this agent.')
  })

  it('names a failed first load and retries', async () => {
    api.get.mockRejectedValueOnce(conflict('Agent not found'))
    const w = render()
    await flushPromises()
    expect(w.text()).toContain('Agent not found')
  })
})

describe('helpers', () => {
  const rows = [ANN, BOB]

  it('replaces with no current primary by creating one', () => {
    expect(replacePrimarySteps([], { newUserId: 3, oldBecomes: 'viewer', roleId: 'cfo' })).toEqual({
      steps: [{ op: 'create', body: { user_id: 3, role_id: 'cfo', kind: 'primary' } }], rollback: [],
    })
  })

  it('deletes the old primary row when the old person already holds that kind', () => {
    const both = [ANN, { ...BOB, id: 'a3', user_id: 1, kind: 'approver' }]
    const { steps, rollback } = replacePrimarySteps(both, { newUserId: 3, oldBecomes: 'approver', roleId: 'cfo' })
    expect(steps[0]).toEqual({ op: 'delete', id: 'a1' })
    expect(rollback).toEqual([{ op: 'create', body: { user_id: 1, role_id: 'cfo', kind: 'primary' } }])
  })

  it('deletes and can restore when the old primary is removed', () => {
    const { steps } = replacePrimarySteps(rows, { newUserId: 3, oldBecomes: 'remove', roleId: 'cfo' })
    expect(steps.map((s) => s.op)).toEqual(['delete', 'create'])
  })

  it('does nothing when the new primary is the current one', () => {
    expect(replacePrimarySteps(rows, { newUserId: 1, oldBecomes: 'viewer', roleId: 'cfo' }).steps).toEqual([])
  })

  it('states an unchecked role file rather than hiding it', () => {
    expect(driftNote({ drift_state: 'unknown' })).toEqual({ tone: 'muted', text: 'Role file not checked yet' })
    expect(driftNote({ drift_state: 'current' }).tone).toBe('ok')
  })

  it('reads the seat from the primary row only', () => {
    expect(seatLine({ assignments: [BOB] })).toEqual({ form: 'none' })
    expect(writeError({ response: { data: { detail: [{ msg: 'field required' }] } } }, 'x')).toBe('field required')
  })
})

describe('seats (trinity-enterprise#811)', () => {
  const SEAT = `/api/enterprise/assignments/agents/${AGENT}/seat`

  it('names the seat the agent holds itself, and holds wins over serves', async () => {
    roster = { ...roster, held_seat: 'brain' }
    const w = render()
    await flushPromises()
    expect(q(w, 'assignments-seat').text()).toBe('Holds the seat: brain')
  })

  it('records the agent as the holder of a seat', async () => {
    api.put = vi.fn(async () => ({ data: {} }))
    const w = render()
    await flushPromises()
    await q(w, 'holder-edit').trigger('click')
    await q(w, 'holder-role').setValue('orchestrator')
    await q(w, 'holder-form').trigger('submit')
    await flushPromises()
    expect(api.put).toHaveBeenCalledWith(SEAT, { role_id: 'orchestrator' })
  })

  it('names the agent that already holds the seat when refused', async () => {
    api.put = vi.fn(async () => { throw conflict('The seat orchestrator is already held by the agent brain-bot.') })
    const w = render()
    await flushPromises()
    await q(w, 'holder-edit').trigger('click')
    await q(w, 'holder-role').setValue('orchestrator')
    await q(w, 'holder-form').trigger('submit')
    await flushPromises()
    expect(q(w, 'holder-error').text()).toContain('already held by the agent brain-bot')
  })

  it('clears the seat the agent holds', async () => {
    roster = { ...roster, held_seat: 'brain' }
    const w = render()
    await flushPromises()
    await q(w, 'holder-clear').trigger('click')
    await flushPromises()
    expect(api.delete).toHaveBeenCalledWith(SEAT)
  })

  it('adds a person with kind only — the seat is optional', async () => {
    roster = { agent_name: AGENT, assignments: [], mine: null, primary: null }
    const w = render()
    await flushPromises()
    await q(w, 'add-user').setValue('3')
    expect(q(w, 'add-submit').attributes('disabled')).toBeUndefined()
    await q(w, 'assignment-add-form').trigger('submit')
    await flushPromises()
    const [, body] = api.post.mock.calls[0]
    expect(body).toEqual({ user_id: 3, kind: 'primary' })
  })

  it("sets and clears a row's seat (an explicit null clears)", async () => {
    const w = render()
    await flushPromises()
    const bob = () => w.find('[data-testid="assignment-row"]')
    await bob().find('[data-testid="assignment-edit-role"]').trigger('click')
    await bob().find('[data-testid="assignment-role-input"]').setValue('head-of-sales')
    await bob().find('[data-testid="assignment-role-form"]').trigger('submit')
    await flushPromises()
    expect(api.patch).toHaveBeenLastCalledWith(`/api/enterprise/assignments/agents/${AGENT}/a2`, { role_id: 'head-of-sales' })

    await bob().find('[data-testid="assignment-edit-role"]').trigger('click')
    await bob().find('[data-testid="assignment-role-clear"]').trigger('click')
    await flushPromises()
    expect(api.patch).toHaveBeenLastCalledWith(`/api/enterprise/assignments/agents/${AGENT}/a2`, { role_id: null })
  })

  it('hides every seat control from a non-admin', async () => {
    roster = { ...roster, held_seat: 'brain' }
    const w = render({ admin: false })
    await flushPromises()
    expect(q(w, 'assignments-seat').text()).toBe('Holds the seat: brain')
    expect(q(w, 'assignments-holder').exists()).toBe(false)
    expect(q(w, 'assignment-edit-role').exists()).toBe(false)
  })

  it('replaces the primary without naming a seat when none is given', () => {
    const { steps } = replacePrimarySteps([], { newUserId: 3, oldBecomes: 'viewer', roleId: '  ' })
    expect(steps).toEqual([{ op: 'create', body: { user_id: 3, kind: 'primary' } }])
  })
})


describe('canon seats, drift and consent (trinity-enterprise#817)', () => {
  const CANON = {
    agent_name: AGENT, canon_root: 'canon', unavailable: null, reason: null, message: null,
    roles: [
      { id: 'cfo', title: 'Chief Financial Officer', updated: '2026-09-30', path: 'canon/roles/cfo.yaml', error: null },
      { id: 'head-of-sales', title: 'Head of Sales', updated: null, path: 'canon/roles/head-of-sales.yaml', error: null },
    ],
  }

  it("names the seat by its canon title, with the file's updated stamp", async () => {
    canonRoles = CANON
    const w = render()
    await flushPromises()
    expect(q(w, 'assignments-seat').text().replace(/\s+/g, ' '))
      .toBe('Serves the seat of its primary: Chief Financial Officer (cfo) (Ann Lee) · updated 2026-09-30')
    expect(q(w, 'assignment-primary').find('[data-testid="assignment-seat"]').text()).toBe('Chief Financial Officer (cfo)')
  })

  it('offers the canon seats on every seat field, and a typed id still works', async () => {
    canonRoles = CANON
    const w = render()
    await flushPromises()
    const list = w.find(`datalist#canon-roles-${AGENT}`)
    expect(list.findAll('option').map((o) => o.attributes('value'))).toEqual(['cfo', 'head-of-sales'])
    expect(q(w, 'add-role').attributes('list')).toBe(`canon-roles-${AGENT}`)
    await q(w, 'add-user').setValue('3')
    await q(w, 'add-role').setValue('not-in-canon')
    await q(w, 'assignment-add-form').trigger('submit')
    await flushPromises()
    expect(api.post.mock.calls[0][1].role_id).toBe('not-in-canon')
  })

  it('tells an admin why there are no seats to suggest', async () => {
    const w = render()
    await flushPromises()
    expect(q(w, 'assignments-canon-note').text()).toContain('this agent declares no canon')
  })

  it('reads consent in three states', async () => {
    roster = { ...roster, assignments: [
      ANN, { ...BOB, proactive_consent_state: 'declined' },
    ] }
    const w = render()
    await flushPromises()
    expect(w.find('[data-testid="assignment-row"] [data-testid="assignment-consent"]').text()).toBe('Proactive briefs: declined')
  })

  it('warns on a missing role file, names why drift is unknown, and says nothing without a seat', async () => {
    roster = { ...roster, assignments: [
      { ...ANN, drift_state: 'missing', drift_reason: 'role_file_not_found' },
      { ...BOB, drift_state: 'unknown', drift_reason: 'not_recorded' },
      { ...BOB, id: 'a3', user_id: 3, display_name: 'Cat Moe', role_id: null, kind: 'viewer', drift_state: 'no_role' },
    ] }
    const w = render()
    await flushPromises()
    expect(q(w, 'assignment-primary').find('[data-testid="assignment-drift"]').text()).toBe('No role file for this seat in the canon')
    const [bob, cat] = w.findAll('[data-testid="assignment-row"]')
    expect(bob.find('[data-testid="assignment-drift"]').text()).toContain('set the seat again to start tracking')
    expect(cat.find('[data-testid="assignment-drift"]').exists()).toBe(false)
  })
})
