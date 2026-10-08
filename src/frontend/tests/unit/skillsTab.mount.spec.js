// @vitest-environment jsdom
/**
 * trinity-enterprise#754 — the merged Skills tab, MOUNTED (#2918).
 *
 * The rules are executed in skillCards.spec.js; this proves the wiring a rule
 * test cannot see: what each role is offered, what Run sends and what it does
 * with each answer, what the approval controls write, and what a stopped
 * agent shows. Only `@/api` is mocked — the stores and the component are real.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { nextTick } from 'vue'

const { api } = vi.hoisted(() => ({
  api: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() },
}))
vi.mock('@/api', () => ({ default: api }))
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ role: 'user', isAuthenticated: true }),
}))

import SkillsTab from '../../src/components/skills/SkillsTab.vue'

const A = 'fin'
const NOTICE = 'Not run: the skill pay-invoice on fin needs approval before it can run.'
const REFUSED = 'The skill pay-invoice is not installed on fin.'

const live = (name, over = {}) => ({
  name, description: `${name} does things`, path: `.claude/skills/${name}/SKILL.md`,
  user_invocable: true, automation: null, argument_hint: null, source: 'agent', dir: name, approval: null, ...over,
})
const LIVE = { skills: [live('daily-report', { automation: 'autonomous' }), live('pay-invoice'),
  live('shared-one', { source: 'platform' })], count: 3, skill_paths: ['.claude/skills'] }
const MAP = (over = {}) => ({
  agent_name: A, gates: [{ skill_name: 'pay-invoice', approver: 'primary', approver_reachable: true, origin: 'set' }],
  cleared_defaults: [], approver_kinds: ['primary', 'approver'],
  approvers: [{ kind: 'primary', reachable: true, viewer_fills: false }, { kind: 'approver', reachable: false, viewer_fills: false }],
  default_deadline_hours: 24, hook: null, ...over,
})

let state
function respond(over = {}) {
  state = { playbooks: { data: LIVE }, map: MAP(), probe: MAP({ hook: 'ok' }), sets: { data: [] }, ...over }
  api.get.mockImplementation((url, cfg) => {
    if (url === '/api/skills/library/status') return Promise.resolve({ data: { configured: true, skill_count: 1 } })
    if (url === '/api/skills/library') return Promise.resolve({ data: [{ name: 'shared-one', description: 'from the library' }] })
    if (url === `/api/agents/${A}/skills`) return Promise.resolve({ data: [{ skill_name: 'shared-one', individual: true, via_sets: [] }] })
    if (url === `/api/agents/${A}/playbooks`) {
      const p = state.playbooks
      return p instanceof Error || p?.response ? Promise.reject(p) : Promise.resolve(p)
    }
    if (url === `/api/agents/${A}/skill-gates`) {
      return Promise.resolve({ data: cfg?.params?.probe ? state.probe : state.map })
    }
    if (url === `/api/agents/${A}/skill-sets`) {
      return state.sets?.response ? Promise.reject(state.sets) : Promise.resolve(state.sets)
    }
    return Promise.resolve({ data: [] })
  })
}

async function flush() {
  for (let i = 0; i < 4; i++) { await nextTick(); await flushPromises() }
}

async function mountTab(props = {}) {
  const notify = vi.fn()
  const w = mount(SkillsTab, {
    props: { agentName: A, agentStatus: 'running', canManage: true, notify, ...props },
    attachTo: document.body,
    global: { stubs: { teleport: true, 'router-link': true } },
  })
  await flush()
  return { w, notify }
}

const card = (w, section, name) => w.find(`[data-testid="skill-card-${section}-${name}"]`)
const tid = (w, id) => w.find(`[data-testid="${id}"]`)

beforeEach(() => {
  setActivePinia(createPinia())
  for (const f of Object.values(api)) f.mockReset()
  respond()
})

describe('sections and roles', () => {
  it('lists own skills and shared skills, each once', async () => {
    const { w } = await mountTab()
    expect(card(w, 'own', 'daily-report').exists()).toBe(true)
    expect(card(w, 'own', 'pay-invoice').exists()).toBe(true)
    expect(card(w, 'own', 'shared-one').exists()).toBe(false)      // delivered by the library
    expect(card(w, 'shared', 'shared-one').exists()).toBe(true)
    expect(tid(w, 'skill-mode-daily-report').text()).toContain('runs unattended')
  })

  it('the owner gets the approval row and the assignment controls', async () => {
    const { w } = await mountTab()
    expect(tid(w, 'skill-approval-daily-report').exists()).toBe(true)
    expect(tid(w, 'skills-assign-open').exists()).toBe(true)
    expect(tid(w, 'skill-unassign-shared-one').exists()).toBe(true)
  })

  it('a viewer sees every gate and can Run, but no approval or assignment control', async () => {
    const { w } = await mountTab({ canManage: false })
    expect(tid(w, 'skill-gate-pay-invoice').text()).toBe('Needs approval from the primary contact')
    expect(tid(w, 'skill-run-daily-report').element.disabled).toBe(false)
    expect(tid(w, 'skill-approval-daily-report').exists()).toBe(false)
    expect(tid(w, 'skills-assign-open').exists()).toBe(false)
    expect(tid(w, 'skill-unassign-shared-one').exists()).toBe(false)
  })

  it('no approval controls on an ephemeral agent or the system agent', async () => {
    const ghost = (await mountTab({ isEphemeral: true })).w
    expect(tid(ghost, 'skill-approval-daily-report').exists()).toBe(false)
    const system = (await mountTab({ isSystem: true })).w
    expect(tid(system, 'skill-approval-daily-report').exists()).toBe(false)
    expect(tid(system, 'skills-assign-open').exists()).toBe(false)
  })
})

describe('a stopped agent', () => {
  it('shows its last-known list with Run disabled and says so', async () => {
    respond({ playbooks: { data: { ...LIVE, last_known: { captured_at: '2026-10-08T10:00:00Z', reason: 'stopped' } } } })
    const { w } = await mountTab({ agentStatus: 'exited' })
    expect(api.get).toHaveBeenCalledWith(`/api/agents/${A}/playbooks`, { params: { last_known: true } })
    expect(tid(w, 'skills-own-meta').text()).toMatch(/^The agent is stopped\. Start the agent to run\./)
    expect(card(w, 'own', 'daily-report').exists()).toBe(true)
    expect(tid(w, 'skill-run-daily-report').element.disabled).toBe(true)
  })

  it('with no list kept, says to start it — never an empty grid', async () => {
    respond({ playbooks: { response: { status: 503, data: { detail: 'Agent is not running.' } } } })
    const { w } = await mountTab({ agentStatus: 'exited' })
    expect(tid(w, 'skills-own-empty').text()).toBe('Start the agent to see its own skills.')
  })
})

describe('Run', () => {
  it('sends the skill in async mode and opens the run on Tasks', async () => {
    api.post.mockResolvedValue({ status: 200, data: { status: 'accepted', execution_id: 'exec-1' } })
    const { w } = await mountTab()
    await tid(w, 'skill-run-daily-report').trigger('click')
    await flush()
    expect(api.post).toHaveBeenCalledWith(`/api/agents/${A}/task`, { message: '/daily-report', async_mode: true })
    expect(w.emitted('run-with-instructions')).toEqual([['__NAVIGATE_TASKS__:exec-1']])
  })

  it('a gated skill raises the approval: the notice, and no run to open', async () => {
    api.post.mockResolvedValue({ status: 202, data: { status: 'pending_approval', message: NOTICE } })
    const { w, notify } = await mountTab()
    await tid(w, 'skill-run-pay-invoice').trigger('click')
    await flush()
    expect(notify).toHaveBeenCalledWith(NOTICE, 'info', { timeout: 8000 })
    expect(w.emitted('run-with-instructions')).toBeUndefined()
  })

  it('a gate refusal goes to the persistent error toast', async () => {
    api.post.mockRejectedValue({ response: { status: 409, headers: { 'x-trinity-error-code': 'gated_skill_not_installed' },
      data: { detail: { status: 'refused', code: 'gated_skill_not_installed', message: REFUSED } } } })
    const { w, notify } = await mountTab()
    await tid(w, 'skill-run-pay-invoice').trigger('click')
    await flush()
    expect(notify).toHaveBeenCalledWith(REFUSED, 'error')
  })

  it('any other failure stays on the card, next to the button', async () => {
    api.post.mockRejectedValue({ response: { status: 500, data: { detail: 'Agent exploded' } } })
    const { w } = await mountTab()
    await tid(w, 'skill-run-daily-report').trigger('click')
    await flush()
    expect(card(w, 'own', 'daily-report').find('[data-testid="inline-error"]').text()).toContain('Agent exploded')
  })

  it('Edit & Run prefills the Tasks tab', async () => {
    const { w } = await mountTab()
    await tid(w, 'skill-edit-run-daily-report').trigger('click')
    expect(w.emitted('run-with-instructions')).toEqual([['/daily-report ']])
  })
})

describe('Requires approval', () => {
  it('turning it on sends the first kind someone fills', async () => {
    api.put.mockResolvedValue({ data: { gate: {}, changed: true, warnings: [] } })
    const { w } = await mountTab()
    await tid(w, 'skill-approval-daily-report').trigger('click')
    await flush()
    expect(api.put).toHaveBeenCalledWith(`/api/agents/${A}/skill-gates/daily-report`, { approver: 'primary' })
  })

  it('turning it off clears the gate', async () => {
    api.delete.mockResolvedValue({ data: { changed: true, cleared: 'deleted' } })
    const { w } = await mountTab()
    await tid(w, 'skill-approval-pay-invoice').trigger('click')
    await flush()
    expect(api.delete).toHaveBeenCalledWith(`/api/agents/${A}/skill-gates/pay-invoice`)
  })

  it('a kind nobody fills is listed but cannot be selected', async () => {
    const { w } = await mountTab()
    const opts = tid(w, 'skill-approver-pay-invoice').findAll('option')
    const approver = opts.find((o) => o.element.value === 'approver')
    expect(approver.element.disabled).toBe(true)
    expect(approver.text()).toContain('nobody yet')
  })

  it('changing the approver of a gated skill writes the new kind', async () => {
    respond({ map: MAP({ approvers: [{ kind: 'primary', reachable: true, viewer_fills: false },
      { kind: 'approver', reachable: true, viewer_fills: false }] }) })
    api.put.mockResolvedValue({ data: { warnings: [] } })
    const { w } = await mountTab()
    await tid(w, 'skill-approver-pay-invoice').setValue('approver')
    await flush()
    expect(api.put).toHaveBeenCalledWith(`/api/agents/${A}/skill-gates/pay-invoice`, { approver: 'approver' })
  })

  it('a refused write is named on that card', async () => {
    api.put.mockRejectedValue({ response: { status: 422, data: { detail: { message: 'Gates are not set on ephemeral agents.' } } } })
    const { w } = await mountTab()
    await tid(w, 'skill-approval-daily-report').trigger('click')
    await flush()
    expect(card(w, 'own', 'daily-report').find('[data-testid="inline-error"]').text())
      .toContain('Gates are not set on ephemeral agents.')
  })

  it('"you approve this" for the person who fills the kind', async () => {
    respond({ map: MAP({ approvers: [{ kind: 'primary', reachable: true, viewer_fills: true }] }) })
    const { w } = await mountTab({ canManage: false })
    expect(tid(w, 'skill-gate-pay-invoice').text()).toBe('Needs approval: you approve this')
  })

  it('a gate whose skill is gone is kept on its own card, with Clear for the owner', async () => {
    respond({ map: MAP({ gates: [{ skill_name: 'old-report', approver: 'primary', approver_reachable: true, origin: 'set' }] }) })
    api.delete.mockResolvedValue({ data: { changed: true } })
    const { w } = await mountTab()
    expect(tid(w, 'skill-gate-old-report').text()).toBe("Not in this agent's skills list: gate kept")
    await tid(w, 'skill-clear-gate-old-report').trigger('click')
    await flush()
    expect(api.delete).toHaveBeenCalledWith(`/api/agents/${A}/skill-gates/old-report`)
  })
})

describe('in-agent enforcement warning (owner, gates exist)', () => {
  it('probes after the map loads, and warns on an image that predates the hook', async () => {
    respond({ probe: MAP({ hook: 'predates' }) })
    const { w } = await mountTab()
    expect(api.get).toHaveBeenCalledWith(`/api/agents/${A}/skill-gates`, { params: { probe: true } })
    expect(tid(w, 'skills-hook-warning').text()).toMatch(/predates the in-agent gate check/)
  })

  it('says nothing when the hook is ok or nobody answered', async () => {
    for (const hook of ['ok', 'unknown']) {
      respond({ probe: MAP({ hook }) })
      const { w } = await mountTab()
      expect(tid(w, 'skills-hook-warning').text()).toBe('')
    }
  })

  it('never probes for a viewer', async () => {
    await mountTab({ canManage: false })
    expect(api.get).not.toHaveBeenCalledWith(`/api/agents/${A}/skill-gates`, { params: { probe: true } })
  })
})

describe('filter', () => {
  it('narrows both sections by name or description', async () => {
    const { w } = await mountTab()
    await tid(w, 'skills-filter').setValue('daily')        // BaseInput puts attrs on the <input>
    expect(card(w, 'own', 'daily-report').exists()).toBe(true)
    expect(card(w, 'own', 'pay-invoice').exists()).toBe(false)
    expect(card(w, 'shared', 'shared-one').exists()).toBe(false)
  })
})

describe('sets on the Shared head', () => {
  it('a failed sets read is named on the head with a retry — never a silently missing line', async () => {
    respond({ sets: { response: { status: 500, data: { detail: 'boom' } } } })
    const { w } = await mountTab({ canManage: false })
    expect(tid(w, 'skills-sets-error').text()).toContain("Couldn't read this agent's sets")
    const before = api.get.mock.calls.filter(([u]) => u === `/api/agents/${A}/skill-sets`).length
    await tid(w, 'skills-sets-retry').trigger('click')
    await flush()
    expect(api.get.mock.calls.filter(([u]) => u === `/api/agents/${A}/skill-sets`).length).toBe(before + 1)
  })

  it('a set that needs credentials says so on the head', async () => {
    respond({ sets: { data: [{ name: 'dev-kit', status: 'ok', members: [], prerequisites: { state: 'missing', missing_env: ['GH_TOKEN'] } }] } })
    const { w } = await mountTab()
    expect(tid(w, 'skills-sets-credentials').text()).toBe('Some sets need credentials')
  })
})
