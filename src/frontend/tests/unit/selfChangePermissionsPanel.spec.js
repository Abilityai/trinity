// @vitest-environment jsdom
/**
 * trinity-enterprise#756 — an agent's self-change permissions as
 * admin-only toggles in its Settings.
 *
 * Mounted (#2918): each toggle gates a grant write, so the proof is a click
 * that reaches the API with the right capability and value, and the honest
 * states — skeleton before the first load, LoadFailed on a failed one, nothing
 * at all for a non-owner (the read 404s), a refusal next to its own toggle.
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { flushPromises, mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

const get = vi.fn()
const put = vi.fn()
vi.mock('../../src/api', () => ({ default: { get: (...a) => get(...a), put: (...a) => put(...a) } }))

// eslint-disable-next-line import/first
import SelfChangePermissionsPanel from '../../src/components/settings/SelfChangePermissionsPanel.vue'
// eslint-disable-next-line import/first
import { useAuthStore } from '../../src/stores/auth'
// eslint-disable-next-line import/first
import { useCapabilityGrantsStore } from '../../src/stores/capabilityGrants'
// eslint-disable-next-line import/first
import { permissionRequests, SELF_CHANGE_CAPABILITIES } from '../../src/utils/capabilityGrants'

const AGENT = 'orchestrator'
const CAPS = SELF_CHANGE_CAPABILITIES.map((c) => c.id)
const httpError = (status, detail) => Object.assign(new Error('x'), { response: { status, data: { detail } } })

function grantsBody (held = {}) {
  return { agent_name: AGENT, grants: CAPS.map((c) => ({
    capability: c, granted: Boolean(held[c]),
    granted_by: held[c] ? 'root' : null, granted_at: held[c] ? '2026-10-01T09:00:00Z' : null,
  })) }
}

function route ({ grants = grantsBody(), autonomy = { autonomy_enabled: true }, queue = { items: [] } } = {}) {
  get.mockImplementation(async (url) => {
    if (url.endsWith('/capability-grants')) {
      if (grants instanceof Error) throw grants
      return { data: grants }
    }
    if (url.endsWith('/autonomy')) return { data: autonomy }
    if (url === '/api/operator-queue') {
      if (queue instanceof Error) throw queue
      return { data: queue }
    }
    throw new Error(`unexpected GET ${url}`)
  })
}

async function mountAs (role) {
  const auth = useAuthStore()
  auth.user = { username: role === 'admin' ? 'root' : 'owner', role }
  const w = mount(SelfChangePermissionsPanel, {
    props: { agentName: AGENT },
    global: { stubs: { RouterLink: { template: '<a><slot /></a>' } } },
  })
  await flushPromises()
  return w
}

const byId = (w, id) => w.find(`[data-testid="${id}"]`)

beforeEach(() => {
  setActivePinia(createPinia())
  get.mockReset()
  put.mockReset()
})

describe('SelfChangePermissionsPanel (ent#756)', () => {
  it('a fresh agent shows every permission off, with a line saying so — never a blank card', async () => {
    route()
    const w = await mountAs('admin')
    for (const c of CAPS) {
      expect(byId(w, `self-change-toggle-${c}`).attributes('aria-checked')).toBe('false')
      expect(byId(w, `self-change-granted-${c}`).text()).toBe('Off')
    }
    expect(byId(w, 'self-change-all-off').exists()).toBe(true)
  })

  it('an admin turns one on: the PUT names the capability, and the row shows who and when', async () => {
    route()
    const w = await mountAs('admin')
    put.mockResolvedValue({ data: { changed: true } })
    route({ grants: grantsBody({ 'schedules.manage': true }) })
    await byId(w, 'self-change-toggle-schedules.manage').trigger('click')
    await flushPromises()
    expect(put).toHaveBeenCalledWith(
      `/api/agents/${AGENT}/capability-grants/schedules.manage`, { granted: true })
    expect(byId(w, 'self-change-toggle-schedules.manage').attributes('aria-checked')).toBe('true')
    expect(byId(w, 'self-change-granted-schedules.manage').text()).toContain('Granted by root')
    expect(byId(w, 'self-change-all-off').exists()).toBe(false)
  })

  it('the owner sees the state but cannot flip it, and is told who can', async () => {
    route({ grants: grantsBody({ 'agents.manage': true }) })
    const w = await mountAs('user')
    expect(byId(w, 'self-change-admin-only').exists()).toBe(true)
    const t = byId(w, 'self-change-toggle-agents.manage')
    expect(t.attributes('aria-checked')).toBe('true')
    expect(t.attributes('disabled')).toBeDefined()
    await t.trigger('click')
    expect(put).not.toHaveBeenCalled()
  })

  it('a refusal lands next to the toggle it was for, in the server\'s words', async () => {
    route()
    const w = await mountAs('admin')
    put.mockRejectedValue(httpError(422, { code: 'calibrating_agent', message: 'This companion is still calibrating.' }))
    await byId(w, 'self-change-toggle-instructions.manage').trigger('click')
    await flushPromises()
    expect(byId(w, 'self-change-instructions.manage').text()).toContain('still calibrating')
    expect(byId(w, 'self-change-skills.manage').text()).not.toContain('still calibrating')
    expect(byId(w, 'self-change-toggle-instructions.manage').attributes('aria-checked')).toBe('false')
  })

  it('a failed load is LoadFailed, never the all-off copy', async () => {
    route({ grants: httpError(500, 'boom') })
    const w = await mountAs('admin')
    expect(byId(w, 'self-change-ready').exists()).toBe(false)
    expect(byId(w, 'self-change-all-off').exists()).toBe(false)
    expect(w.text()).toContain("Couldn't load this agent's permissions")
  })

  it('renders nothing for someone who is not the owner (the read 404s)', async () => {
    route({ grants: httpError(404, 'Agent not found') })
    const w = await mountAs('user')
    expect(byId(w, 'self-change-permissions').exists()).toBe(false)
  })

  it('shows autonomy beside the permissions, as a person\'s switch and not a grant', async () => {
    route({ autonomy: { autonomy_enabled: false } })
    const w = await mountAs('admin')
    expect(byId(w, 'self-change-autonomy').text()).toMatch(/Autonomy\s*·\s*off/)
    expect(byId(w, 'self-change-toggle-autonomy').exists()).toBe(false)
  })

  it("links this agent's open permission requests", async () => {
    route({ queue: { items: [
      { id: 'q1', status: 'pending', title: 'Need schedules.manage to pause the sales agent' },
      { id: 'q2', status: 'pending', title: 'Weekly report ready' },
    ] } })
    const w = await mountAs('admin')
    expect(get).toHaveBeenCalledWith('/api/operator-queue',
      { params: { agent_name: AGENT, status: 'pending', limit: 200 } })
    expect(byId(w, 'self-change-request-q1').text()).toContain('Open in Operations')
    expect(byId(w, 'self-change-request-q2').exists()).toBe(false)
  })
})

describe('SelfChangePermissionsPanel — races and refreshes (#3330 review)', () => {
  it('a PUT that lands after the panel switched agents never retargets the panel', async () => {
    // C1: the store reloaded the OLD agent after the await, so the panel on
    // screen showed A's grants and its next toggle wrote to A.
    route()
    let release
    put.mockImplementationOnce(() => new Promise((r) => { release = r })).mockResolvedValue({ data: {} })
    const w = await mountAs('admin')
    await byId(w, 'self-change-toggle-agents.manage').trigger('click')
    await w.setProps({ agentName: 'sales-companion' })
    await flushPromises()
    release({ data: {} })
    await flushPromises()
    expect(useCapabilityGrantsStore().agentName).toBe('sales-companion')
    await byId(w, 'self-change-toggle-skills.manage').trigger('click')
    await flushPromises()
    expect(put.mock.calls.map((c) => c[0])).toEqual([
      `/api/agents/${AGENT}/capability-grants/agents.manage`,
      '/api/agents/sales-companion/capability-grants/skills.manage',
    ])
  })

  it('one in-flight toggle does not re-enable another that is still in flight', async () => {
    route()
    const releases = []
    put.mockImplementation(() => new Promise((r) => releases.push(r)))
    const w = await mountAs('admin')
    await byId(w, 'self-change-toggle-skills.manage').trigger('click')
    await byId(w, 'self-change-toggle-agents.manage').trigger('click')
    releases[0]({ data: {} })
    await flushPromises()
    expect(byId(w, 'self-change-toggle-agents.manage').attributes('disabled')).toBeDefined()
    releases[1]({ data: {} })
    await flushPromises()
  })

  it('a failed refresh keeps the toggles on screen and says so', async () => {
    route()
    const w = await mountAs('admin')
    put.mockResolvedValue({ data: {} })
    route({ grants: httpError(500, 'boom') })
    await byId(w, 'self-change-toggle-schedules.manage').trigger('click')
    await flushPromises()
    expect(byId(w, 'self-change-ready').exists()).toBe(true)
    expect(byId(w, 'self-change-stale').text()).toContain("Couldn't refresh the permissions")
  })
})

describe('permissionRequests', () => {
  it('matches the permission id in the title, case-insensitively, and nothing looser', () => {
    const got = permissionRequests([
      { id: 'a', status: 'pending', title: 'Grant AGENTS.MANAGE please' },
      { id: 'b', status: 'pending', title: 'Can I change my schedules?' },
      { id: 'c', status: 'responded', title: 'skills.manage' },
      { id: 'd', title: 'instructions.manage for CLAUDE.md' },
    ])
    expect(got.map((r) => [r.id, r.capability])).toEqual([
      ['a', 'agents.manage'], ['d', 'instructions.manage'],
    ])
  })
})
