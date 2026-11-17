// @vitest-environment jsdom
/**
 * trinity-enterprise#1154 — "Person" and "New primary" pick a person.
 *
 * Both fields were a native select over every account on the instance, read
 * from the admin-only `GET /api/users`. They are now BasePeopleInput in
 * must-pick mode over the agent-scoped candidate read
 * (`GET /api/enterprise/assignments/agents/{agent}/candidates?q=&kind=`).
 * Writes stay an admin's (R11, ruled on ent#1154). Pinned, mounted:
 *   - typing asks the scoped read with the field's kind, and the write carries
 *     the picked person's user id;
 *   - nothing is written for text that is nobody: the field says so by name,
 *     and names the invite path when no one matches;
 *   - "New primary" asks for kind=primary;
 *   - the role shows on a row (every reader here is an admin);
 *   - an unavailable read falls back to the select, never a blank field;
 *   - the seat datalist is untouched.
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
import { candidateAnswer, pickPerson, typePerson } from './helpers/pickPerson'

const AGENT = 'scout'
const BASE = `/api/enterprise/assignments/agents/${AGENT}`
const CANDIDATES = `${BASE}/candidates`
const USERS = [
  { id: 1, username: 'ann', name: 'Ann Lee', email: 'ann@example.com', role: 'user' },
  { id: 3, username: 'cat', name: 'Cat Moe', email: 'cat@example.com', role: 'creator' },
]
const ANN = { id: 'a1', user_id: 1, display_name: 'Ann Lee', user_email: 'ann@example.com', role_id: null, kind: 'primary', drift_state: 'no_role' }

let roster
let candidates

function render() {
  const auth = useAuthStore()
  auth.user = { role: 'admin' }
  auth.profileVerified = true
  auth.isAuthenticated = true
  const ent = useEnterpriseStore()
  ent.enterpriseFeatures = ['assignments']
  ent.featureFlagsLoaded = true
  return mount(AgentAssignmentsSection, { props: { agentName: AGENT, people: [] }, attachTo: document.body })
}

const q = (w, id) => w.find(`[data-testid="${id}"]`)
const asked = () => api.get.mock.calls.filter(([u]) => u === CANDIDATES).map(([, cfg]) => cfg.params)

beforeEach(() => {
  Element.prototype.scrollIntoView = vi.fn()
  setActivePinia(createPinia())
  for (const fn of [...Object.values(api), ...Object.values(ax)]) fn.mockReset()
  roster = { agent_name: AGENT, assignments: [ANN], mine: null, primary: 'Ann Lee' }
  candidates = candidateAnswer(USERS, (kind) => roster.assignments.filter((a) => a.kind === kind).map((a) => a.user_id))
  api.get.mockImplementation(async (url, cfg) => {
    if (url === CANDIDATES) return { data: candidates(cfg?.params) }
    if (url === '/api/users') return { data: USERS }
    if (url.endsWith('/canon/roles')) return { data: { roles: [{ id: 'cfo', title: 'CFO' }] } }
    return { data: roster }
  })
  api.post.mockResolvedValue({ data: {} })
  api.delete.mockResolvedValue({ data: {} })
  ax.post.mockResolvedValue({ data: {} })
})

describe('Person', () => {
  it('is a people field, not a select of every account', async () => {
    const w = render()
    await flushPromises()
    expect(q(w, 'add-user').element.tagName).toBe('INPUT')
    expect(q(w, 'add-user').attributes('role')).toBe('combobox')
  })

  it('asks the scoped read with the chosen kind, and writes the picked person', async () => {
    const w = render()
    await flushPromises()
    await q(w, 'add-kind').setValue('viewer')
    await pickPerson(w, 'add-user', 'ca', 'Cat Moe')
    expect(asked()).toEqual([{ q: 'ca', kind: 'viewer' }])
    await q(w, 'assignment-add-form').trigger('submit')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(BASE, { user_id: 3, kind: 'viewer' })
    expect(q(w, 'add-user').element.value).toBe('')            // the person leaves with the write
  })

  it('shows the role on a row', async () => {
    const w = render()
    await flushPromises()
    await typePerson(w, 'add-user', 'ca')
    const row = w.findAll('[role="option"]')[0].text()
    expect(row.indexOf('Cat Moe')).toBeLessThan(row.indexOf('cat@example.com'))
    expect(row).toContain('creator')
  })

  it('writes nothing for text that is nobody, and says so', async () => {
    const w = render()
    await flushPromises()
    await typePerson(w, 'add-user', 'ca')
    expect(q(w, 'add-submit').attributes('disabled')).toBeDefined()
    await q(w, 'add-user').trigger('blur')
    expect(w.find('[role="alert"]').text()).toContain('Choose a person from the list')
    await q(w, 'assignment-add-form').trigger('submit')
    await flushPromises()
    expect(api.post).not.toHaveBeenCalled()
  })

  it('a prefix matching no one says so and names the invite path', async () => {
    const w = render()
    await flushPromises()
    await typePerson(w, 'add-user', 'zed')
    expect(w.text()).toContain('No one on this instance matches “zed”')
    expect(w.text()).toContain('Add person')
  })

  it('leaves the seat datalist alone', async () => {
    const w = render()
    await flushPromises()
    expect(q(w, 'add-role').attributes('list')).toBe(`canon-roles-${AGENT}`)
  })
})

describe('New primary', () => {
  it('asks for kind=primary and never offers the primary it replaces', async () => {
    const w = render()
    await flushPromises()
    await q(w, 'assignment-replace').trigger('click')
    await typePerson(w, 'replace-user', 'an')
    expect(asked()).toEqual([{ q: 'an', kind: 'primary' }])
    expect(w.findAll('[role="option"]')).toHaveLength(0)
    await pickPerson(w, 'replace-user', 'ca', 'Cat Moe')
    await q(w, 'assignment-replace-form').trigger('submit')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(BASE, expect.objectContaining({ user_id: 3, kind: 'primary' }))
  })
})

describe('when the candidate read is unavailable', () => {
  it('falls back to the select, never a blank field', async () => {
    candidates = () => { throw Object.assign(new Error('403'), { response: { status: 403 } }) }
    const w = render()
    await flushPromises()
    await typePerson(w, 'add-user', 'ca')
    await flushPromises()
    const select = q(w, 'add-user-select')
    expect(select.element.tagName).toBe('SELECT')
    expect(select.findAll('option').map((o) => o.text())).toContain('Cat Moe · creator')
    await select.setValue('3')
    await q(w, 'assignment-add-form').trigger('submit')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(BASE, expect.objectContaining({ user_id: 3 }))
  })

  it('New primary falls back to the select too, never a blank field', async () => {
    candidates = () => { throw Object.assign(new Error('403'), { response: { status: 403 } }) }
    const w = render()
    await flushPromises()
    await q(w, 'assignment-replace').trigger('click')
    await typePerson(w, 'replace-user', 'ca')
    await flushPromises()
    const select = q(w, 'replace-user-select')
    expect(select.element.tagName).toBe('SELECT')
    expect(select.findAll('option').map((o) => o.text())).toContain('Cat Moe · creator')
    await select.setValue('3')
    await q(w, 'assignment-replace-form').trigger('submit')
    await flushPromises()
    expect(api.post).toHaveBeenCalledWith(BASE, expect.objectContaining({ user_id: 3, kind: 'primary' }))
  })
})
