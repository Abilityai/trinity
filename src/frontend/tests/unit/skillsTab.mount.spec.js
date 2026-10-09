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
import { useSkillsStore } from '../../src/stores/skills'

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
// An answer: a response, a rejection (`{response}` / Error), or a promise the
// test settles by hand.
const answer = (v) => (v instanceof Promise ? v
  : v instanceof Error || v?.response ? Promise.reject(v) : Promise.resolve(v))
function respond(over = {}) {
  state = {
    playbooks: { data: LIVE }, map: MAP(), probe: MAP({ hook: 'ok' }), sets: { data: [] },
    rows: { data: [{ skill_name: 'shared-one', individual: true, via_sets: [] }] },
    library: { data: [{ name: 'shared-one', description: 'from the library' }] },
    mapAnswer: null,
    ...over,
  }
  api.get.mockImplementation((url, cfg) => {
    if (url === '/api/skills/library/status') return Promise.resolve({ data: { configured: true, skill_count: 1 } })
    if (url === '/api/skills/library') return answer(state.library)
    if (url === `/api/agents/${A}/skills`) return answer(state.rows)
    if (url === `/api/agents/${A}/playbooks`) return answer(state.playbooks)
    if (url === `/api/agents/${A}/skill-gates`) {
      if (!cfg?.params?.probe && state.mapAnswer) return answer(state.mapAnswer)
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

describe('an agent switch (AgentDetail is KeepAlive\'d: the tab stays open)', () => {
  const B = 'ops'
  function serveTwo() {
    api.get.mockImplementation((url, cfg) => {
      if (url === '/api/skills/library/status') return Promise.resolve({ data: { configured: true, skill_count: 1 } })
      if (url === '/api/skills/library') return Promise.resolve({ data: [{ name: 'shared-one', description: 'from the library' }] })
      const m = url.match(/^\/api\/agents\/([^/]+)\/(.+)$/)
      if (!m) return Promise.resolve({ data: [] })
      const [, who, what] = m
      if (what === 'skills') {
        return Promise.resolve({ data: who === A ? [{ skill_name: 'shared-one', individual: true, via_sets: [] }] : [] })
      }
      if (what === 'playbooks') return Promise.resolve({ data: LIVE })
      if (what === 'skill-gates') return Promise.resolve({ data: MAP({ agent_name: who, hook: cfg?.params?.probe ? 'ok' : null }) })
      return Promise.resolve({ data: [] })
    })
  }
  function deferred() {
    let resolve
    const promise = new Promise((r) => { resolve = r })
    return { promise, resolve }
  }

  it("the previous agent's outcome line and card errors do not follow", async () => {
    serveTwo()
    api.put.mockResolvedValue({ data: { delivery: null } })
    api.post.mockRejectedValue({ response: { status: 500, data: { detail: 'Agent exploded' } } })
    const { w } = await mountTab()
    await tid(w, 'skill-unassign-shared-one').trigger('click')
    await flush()
    await tid(w, 'confirm-dialog-confirm').trigger('click')
    await flush()
    await tid(w, 'skill-run-daily-report').trigger('click')
    await flush()
    expect(tid(w, 'skills-saved-note').text()).toBe('Unassigned shared-one.')
    expect(card(w, 'own', 'daily-report').find('[data-testid="inline-error"]').exists()).toBe(true)

    await w.setProps({ agentName: B })
    await flush()

    expect(tid(w, 'skills-saved-note').exists()).toBe(false)
    expect(card(w, 'own', 'daily-report').find('[data-testid="inline-error"]').exists()).toBe(false)
  })

  it("the next agent's Shared section waits for its own assignments", async () => {
    serveTwo()
    const opsRows = deferred()
    const inner = api.get.getMockImplementation()
    api.get.mockImplementation((url, cfg) => (url === `/api/agents/${B}/skills` ? opsRows.promise : inner(url, cfg)))
    const { w } = await mountTab()
    expect(card(w, 'shared', 'shared-one').exists()).toBe(true)

    await w.setProps({ agentName: B })
    await flush()
    expect(tid(w, 'skills-shared-empty').exists()).toBe(false)    // not "No shared skills yet" before B answers
    expect(tid(w, 'skills-assign-open').exists()).toBe(false)

    opsRows.resolve({ data: [] })
    await flush()
    expect(tid(w, 'skills-shared-empty').exists()).toBe(true)
  })

  it('an unsaved tick on the previous agent is neither offered nor saved on the next (PR review)', async () => {
    // Both agents hold the same individual set (none), so the switch changes
    // nothing the draft watches: only opening the dialog may reset it.
    api.get.mockImplementation((url, cfg) => {
      if (url === '/api/skills/library/status') return Promise.resolve({ data: { configured: true, skill_count: 2 } })
      if (url === '/api/skills/library') return Promise.resolve({ data: [{ name: 'shared-one' }, { name: 'lib-a' }] })
      const m = url.match(/^\/api\/agents\/([^/]+)\/(.+)$/)
      if (!m) return Promise.resolve({ data: [] })
      const [, who, what] = m
      if (what === 'skills') return Promise.resolve({ data: [] })
      if (what === 'playbooks') return Promise.resolve({ data: LIVE })
      if (what === 'skill-gates') return Promise.resolve({ data: MAP({ agent_name: who, hook: cfg?.params?.probe ? 'ok' : null }) })
      return Promise.resolve({ data: [] })
    })
    api.put.mockResolvedValue({ data: { delivery: null } })
    const { w } = await mountTab()
    const tick = () => w.find('[data-testid="skill-assign-list"] input[value="lib-a"]')
    const button = (label) => w.findAll('button').find((b) => b.text().includes(label))

    await tid(w, 'skills-assign-open').trigger('click')
    await flush()
    await tick().setValue(true)
    await flush()
    await button('Close').trigger('click')                  // closed without saving
    await flush()

    await w.setProps({ agentName: B })
    await flush()
    await tid(w, 'skills-assign-open').trigger('click')
    await flush()
    expect(tick().element.checked).toBe(false)
    await button('Save assignments').trigger('click')
    await flush()
    expect(api.put).not.toHaveBeenCalled()
  })

  it('a switch reads the next agent once: one list read, one probe (PR review)', async () => {
    serveTwo()
    const { w } = await mountTab()
    // The next agent differs in state and has a skills-changed tick of its own:
    // three watchers see the switch, and the switch is one load.
    useSkillsStore().changedAt = { [B]: 1 }
    api.get.mockClear()

    await w.setProps({ agentName: B, agentStatus: 'stopped' })
    await flush()

    // Every agent's reads, not only B's: a watcher that fires before the store
    // has switched reads the agent being left.
    const reads = (path, probe) => api.get.mock.calls
      .filter(([u, cfg]) => u.endsWith(`/${path}`) && !!cfg?.params?.probe === probe).map(([u]) => u)
    expect(reads('playbooks', false)).toEqual([`/api/agents/${B}/playbooks`])
    expect(reads('skill-gates', true)).toEqual([`/api/agents/${B}/skill-gates`])
  })

  it('a start or a stop on the same agent re-reads its list and probes again', async () => {
    serveTwo()
    const { w } = await mountTab()
    api.get.mockClear()

    await w.setProps({ agentStatus: 'stopped' })
    await flush()

    const reads = (path, probe) => api.get.mock.calls
      .filter(([u, cfg]) => u === `/api/agents/${A}/${path}` && !!cfg?.params?.probe === probe).length
    expect(reads('playbooks', false)).toBe(1)
    expect(reads('skill-gates', true)).toBe(1)
  })

  it('a run that answers after the switch neither opens Tasks nor toasts, and frees the button', async () => {
    serveTwo()
    const run = deferred()
    api.post.mockImplementation(() => run.promise)
    const { w, notify } = await mountTab()
    await tid(w, 'skill-run-daily-report').trigger('click')
    await nextTick()

    await w.setProps({ agentName: B })
    await flush()
    expect(tid(w, 'skill-run-daily-report').element.disabled).toBe(false)   // B's card is not "Starting…"

    run.resolve({ status: 202, data: { status: 'pending_approval', message: NOTICE } })
    await flush()
    expect(notify).not.toHaveBeenCalled()
    expect(w.emitted('run-with-instructions')).toBeUndefined()
  })
})


describe('honest loading: nothing is drawn from a read that has not answered', () => {
  function deferred() {
    let resolve
    let reject
    const promise = new Promise((res, rej) => { resolve = res; reject = rej })
    return { promise, resolve, reject }
  }
  const skeleton = (w) => w.find('[role="status"][aria-busy="true"]')

  it("until this agent's assignments answer, no skill is placed (and none is called left over)", async () => {
    const rows = deferred()
    respond({ rows: rows.promise })
    const { w } = await mountTab()
    expect(card(w, 'own', 'shared-one').exists()).toBe(false)     // not "from library: the next sync removes it"
    expect(tid(w, 'skills-shared-empty').exists()).toBe(false)
    expect(tid(w, 'skills-assign-open').exists()).toBe(false)     // no draft built from unknown assignments

    rows.resolve({ data: [{ skill_name: 'shared-one', individual: true, via_sets: [] }] })
    await flush()
    expect(card(w, 'shared', 'shared-one').exists()).toBe(true)
    expect(card(w, 'own', 'shared-one').exists()).toBe(false)
  })

  it('a failed assignments read is named with a retry, never "No shared skills yet"', async () => {
    respond({ rows: { response: { status: 500, data: { detail: 'boom' } } } })
    const { w } = await mountTab()
    expect(tid(w, 'skills-shared-empty').exists()).toBe(false)
    expect(w.find('[data-testid="skills-shared"] [data-testid="load-failed"]').exists()).toBe(true)
    expect(card(w, 'own', 'daily-report').exists()).toBe(true)          // own skills still show
    expect(card(w, 'own', 'shared-one').text()).not.toContain('from library')   // not called left over
  })

  it('a failed library read is named, never "the library has no skills yet"', async () => {
    respond({ library: { response: { status: 500, data: { detail: 'git clone failed' } } } })
    const { w } = await mountTab()
    expect(w.text()).not.toContain('The library is configured but has no skills yet')
    expect(w.find('[data-testid="skills-shared"] [data-testid="load-failed"]').text()).toContain('git clone failed')
  })

  it('cards wait for the approval map: no toggle shows "off" before the gates are known', async () => {
    const map = deferred()
    respond({ mapAnswer: map.promise })
    const { w } = await mountTab()
    expect(tid(w, 'skill-approval-pay-invoice').exists()).toBe(false)
    expect(skeleton(w).exists()).toBe(true)

    map.resolve({ data: MAP() })
    await flush()
    expect(tid(w, 'skill-gate-pay-invoice').text()).toBe('Needs approval from the primary contact')
  })

  it('a failed approval-map read is named for everyone, with a retry, and the toggles are held', async () => {
    respond({ mapAnswer: { response: { status: 500, data: { detail: 'db down' } } } })
    const { w } = await mountTab()
    expect(tid(w, 'skills-gates-error').text()).toContain("Couldn't read which skills need approval")
    expect(tid(w, 'skill-approval-daily-report').element.disabled).toBe(true)   // the switch itself

    state.mapAnswer = null
    await tid(w, 'skills-gates-retry').trigger('click')
    await flush()
    expect(tid(w, 'skills-gates-error').exists()).toBe(false)
    expect(tid(w, 'skill-gate-pay-invoice').text()).toBe('Needs approval from the primary contact')
  })

  it('a running agent that is not answering yet offers to check again', async () => {
    respond({ playbooks: { response: { status: 503, data: { detail: 'Could not connect to agent' } } } })
    const { w } = await mountTab()
    expect(tid(w, 'skills-own-empty').text()).toContain("isn't answering right now")
    state.playbooks = { data: LIVE }
    await tid(w, 'skills-own-retry').trigger('click')
    await flush()
    expect(card(w, 'own', 'daily-report').exists()).toBe(true)
  })

  it('a failed refresh keeps the list and says it could not refresh', async () => {
    const { w } = await mountTab()
    state.playbooks = { response: { status: 500, data: { detail: 'boom' } } }
    await w.setProps({ agentStatus: 'exited' })          // any refresh trigger
    await flush()
    expect(card(w, 'own', 'daily-report').exists()).toBe(true)
    expect(tid(w, 'skills-own-meta').text()).toContain("Couldn't refresh this agent's skills")
  })

  it('a 200 that is not a skills listing is a failure, never an empty list', async () => {
    respond({ playbooks: { data: { detail: 'not a listing' } } })
    const { w } = await mountTab()
    expect(tid(w, 'skills-own-empty').exists()).toBe(false)
    expect(w.find('[data-testid="skills-own"] [data-testid="load-failed"]').exists()).toBe(true)
  })
})

describe('approval writes go to the gate the card shows', () => {
  // A skill whose frontmatter name differs from its directory: the gate is
  // kept under the directory, and the card found it there.
  const REPORT = { ...LIVE, skills: [...LIVE.skills, live('report', { dir: 'weekly-report', path: '.claude/skills/weekly-report/SKILL.md' })] }
  const DIR_GATE = MAP({
    gates: [{ skill_name: 'weekly-report', approver: 'primary', approver_reachable: true, origin: 'set' }],
    approvers: [{ kind: 'primary', reachable: true, viewer_fills: false }, { kind: 'approver', reachable: true, viewer_fills: false }],
  })

  it('turning it off clears the gate under the key it is stored on', async () => {
    respond({ playbooks: { data: REPORT }, map: DIR_GATE })
    api.delete.mockResolvedValue({ data: { changed: true } })
    const { w } = await mountTab()
    expect(tid(w, 'skill-gate-report').text()).toBe('Needs approval from the primary contact')
    await tid(w, 'skill-approval-report').trigger('click')
    await flush()
    expect(api.delete).toHaveBeenCalledWith(`/api/agents/${A}/skill-gates/weekly-report`)
  })

  it('changing its approver rewrites that gate, never a second one under the name', async () => {
    respond({ playbooks: { data: REPORT }, map: DIR_GATE })
    api.put.mockResolvedValue({ data: { warnings: [] } })
    const { w } = await mountTab()
    await tid(w, 'skill-approver-report').setValue('approver')
    await flush()
    expect(api.put).toHaveBeenCalledWith(`/api/agents/${A}/skill-gates/weekly-report`, { approver: 'approver' })
  })

  it('turning it on sends the first kind someone fills — not a constant', async () => {
    respond({ map: MAP({ gates: [], approvers: [{ kind: 'primary', reachable: false, viewer_fills: false },
      { kind: 'approver', reachable: true, viewer_fills: false }] }) })
    api.put.mockResolvedValue({ data: { warnings: [] } })
    const { w } = await mountTab()
    await tid(w, 'skill-approval-daily-report').trigger('click')
    await flush()
    expect(api.put).toHaveBeenCalledWith(`/api/agents/${A}/skill-gates/daily-report`, { approver: 'approver' })
  })

  it('the default follows the map when it is re-read after the card was drawn', async () => {
    respond()                                             // primary reaches someone
    api.delete.mockResolvedValue({ data: { changed: true } })
    api.put.mockResolvedValue({ data: { warnings: [] } })
    const { w } = await mountTab()
    // A write elsewhere re-reads the map; by now nobody fills primary.
    state.map = MAP({ gates: [], approvers: [{ kind: 'primary', reachable: false, viewer_fills: false },
      { kind: 'approver', reachable: true, viewer_fills: false }] })
    await tid(w, 'skill-approval-pay-invoice').trigger('click')
    await flush()
    await tid(w, 'skill-approval-daily-report').trigger('click')
    await flush()
    expect(api.put).toHaveBeenCalledWith(`/api/agents/${A}/skill-gates/daily-report`, { approver: 'approver' })
  })

  it("each switch is named for its skill (one per card)", async () => {
    const { w } = await mountTab()
    expect(tid(w, 'skill-approval-daily-report').attributes('aria-label')).toBe('Requires approval for /daily-report')
  })
})

describe('section heads', () => {
  it('the Own count counts every card the section lists, a kept gate included', async () => {
    respond({ map: MAP({ gates: [
      { skill_name: 'pay-invoice', approver: 'primary', approver_reachable: true, origin: 'set' },
      { skill_name: 'gone', approver: 'primary', approver_reachable: true, origin: 'set' },
    ] }) })
    const { w } = await mountTab()
    expect(card(w, 'own', 'gone').exists()).toBe(true)          // "gate kept"
    expect(tid(w, 'skills-own-count').text()).toBe('3')         // daily-report, pay-invoice, gone
  })

  it('before a sync, the Shared line says statuses appear after one', async () => {
    const { w } = await mountTab()
    expect(tid(w, 'skills-sync-meta').text()).toBe('Not synced from this screen yet: statuses appear after a sync. '
      + 'Skills are also copied in when the agent starts.')
  })
})

describe('the approval row', () => {
  it('the approver picker is the small field select, so it fits the 40px row', async () => {
    const { w } = await mountTab()
    const picker = tid(w, 'skill-approver-pay-invoice')
    expect(picker.classes()).toEqual(expect.arrayContaining(['py-1', 'text-[12.5px]', 'border']))
    expect(picker.classes()).not.toContain('py-2')
    // Approval off: disabled, still a bordered box (the recipe dims it to .45).
    expect(tid(w, 'skill-approver-daily-report').element.disabled).toBe(true)
    expect(tid(w, 'skill-approver-daily-report').classes()).toContain('disabled:opacity-45')
  })
})

describe('the Own skills meta line', () => {
  it('names each folder the agent scanned once', async () => {
    // An image whose server runs as `developer` lists one folder under two names.
    respond({ playbooks: { data: { ...LIVE, skill_paths: ['.claude/skills', '.claude/skills'] } } })
    const { w } = await mountTab()
    expect(tid(w, 'skills-own-meta').text()).toBe('From .claude/skills')
  })
})

describe('in-agent enforcement warning wording', () => {
  it('names every hook state in words, never its code', async () => {
    // (`predates` is an English word its own sentence uses; pinned above.)
    for (const hook of ['missing', 'not_root_owned', 'writable', 'unsupported_runtime', 'some_new_state']) {
      respond({ probe: MAP({ hook }) })
      const { w } = await mountTab()
      const text = tid(w, 'skills-hook-warning').text()
      expect(text.length, hook).toBeGreaterThan(20)
      expect(text, hook).not.toContain(hook)
    }
  })
})


describe('destructive verbs restate the consequence and focus the safe action (principle 19)', () => {
  it('Unassign asks first, and says the approval requirement will go with it', async () => {
    respond({ map: MAP({ gates: [{ skill_name: 'shared-one', approver: 'primary', approver_reachable: true, origin: 'set' }] }) })
    api.put.mockResolvedValue({ data: { delivery: null } })
    const { w } = await mountTab()

    await tid(w, 'skill-unassign-shared-one').trigger('click')
    await flush()
    expect(api.put).not.toHaveBeenCalled()
    const dialog = tid(w, 'confirm-dialog')
    expect(dialog.text()).toContain('Unassign /shared-one?')
    expect(tid(w, 'confirm-dialog-message').text()).toBe('The library skill will be removed from this agent, '
      + 'along with its approval requirement. You can assign it again from Assign skills.')
    expect(document.activeElement?.dataset?.testid).toBe('confirm-dialog-cancel')

    await tid(w, 'confirm-dialog-cancel').trigger('click')
    await flush()
    expect(api.put).not.toHaveBeenCalled()

    await tid(w, 'skill-unassign-shared-one').trigger('click')
    await flush()
    await tid(w, 'confirm-dialog-confirm').trigger('click')
    await flush()
    expect(api.put).toHaveBeenCalledTimes(1)
    expect(api.put.mock.calls[0][1]).toEqual({ skills: [] })
  })

  it('an ungated skill: the dialog says only what will be removed', async () => {
    const { w } = await mountTab()
    await tid(w, 'skill-unassign-shared-one').trigger('click')
    await flush()
    expect(tid(w, 'confirm-dialog-message').text())
      .toBe('The library skill will be removed from this agent. You can assign it again from Assign skills.')
  })

  it("the details dialog opens on Close, not on 'Unassign library skill'", async () => {
    respond({ rows: { data: [{ skill_name: 'shared-one', individual: true, via_sets: [], delivery_status: 'conflict' }] } })
    const { w } = await mountTab()
    await tid(w, 'skill-conflict-note').trigger('click')
    await flush()
    expect(tid(w, 'skill-conflict-unassign').exists()).toBe(true)
    expect(document.activeElement?.dataset?.testid).not.toBe('skill-conflict-unassign')
  })

  it("the sets dialog opens on a safe control, not on 'Unassign set'", async () => {
    respond({ sets: { data: [{ name: 'dev-kit', status: 'ok', members: [], prerequisites: null }] } })
    const { w } = await mountTab()
    await tid(w, 'skills-sets-open').trigger('click')
    await flush()
    expect(tid(w, 'set-unassign-dev-kit').exists()).toBe(true)
    expect(document.activeElement?.dataset?.testid).not.toBe('set-unassign-dev-kit')
  })
})
