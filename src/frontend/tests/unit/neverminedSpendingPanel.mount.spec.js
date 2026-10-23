// @vitest-environment jsdom
/**
 * Agent → Payments → Spending (abilityai/trinity-enterprise#967), mounted.
 *
 * Checkpoint A: the read-only states. The panel is driven against a stubbed
 * client and the assertions read what it renders and what it requested:
 *  - loading is a skeleton, never the empty copy;
 *  - the not-configured state links admins to Settings and never calls the
 *    admin organization route;
 *  - Enroll is offered to the owner of a running agent only;
 *  - funded numbers are Nevermined's, and "unavailable" never invents one;
 *  - a failed first read is LoadFailed; a failed refresh keeps the data;
 *  - KeepAlive re-entry re-reads in the background without a skeleton;
 *  - a response for a superseded agent or an unmounted panel is dropped.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises, RouterLinkStub } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { defineComponent, h, KeepAlive, ref, nextTick } from 'vue'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))

import NeverminedSpendingPanel from '@/components/NeverminedSpendingPanel.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import { useAuthStore } from '@/stores/auth'

const URL_A = '/api/nevermined/agents/a/delegation'

const LIVE = (over = {}) => ({
  source: 'nevermined', status: 'active',
  remaining_cents: 600, spending_limit_cents: 1000, spent_cents: 400,
  transactions_used: 2, max_transactions: 10, currency: 'USD',
  expires_at: '2099-01-01T00:00:00Z', fetched_at: new Date().toISOString(), error: null,
  ...over,
})
const BODY = (over = {}) => ({
  agent_name: 'a', configured: true, funded: false, delegation: null, live: null, history: [], ...over,
})
const FUNDED = (live = LIVE(), over = {}) => BODY({
  funded: true,
  delegation: {
    delegation_id: 'dlg_abcdef0123456789', payment_method_id: 'pm_12345678', provider: 'stripe',
    environment: 'sandbox', created_by: 'owner@example.com', created_at: '2026-10-20T00:00:00Z',
  },
  live,
  history: [{ delegation_id: 'dlg_old0000000001', created_at: '2026-09-01T00:00:00Z', revoked_at: '2026-09-02T00:00:00Z' }],
  ...over,
})

const PROPS = (over = {}) => ({ agentName: 'a', canEdit: true, agentStatus: 'running', ...over })
const GLOBAL = { stubs: { RouterLink: RouterLinkStub } }

let w
async function render(body, props = {}) {
  api.get.mockImplementation(async () => ({ data: body }))
  w = mount(NeverminedSpendingPanel, { props: PROPS(props), attachTo: document.body, global: GLOBAL })
  await flushPromises()
  return w
}
const $ = (id) => w.find(`[data-testid="${id}"]`)

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})
afterEach(() => { w?.unmount(); w = null; document.body.innerHTML = '' })

describe('NeverminedSpendingPanel — read states', () => {
  it('#1 loading is a busy skeleton, never the empty or not-funded copy', async () => {
    api.get.mockImplementation(() => new Promise(() => {}))
    w = mount(NeverminedSpendingPanel, { props: PROPS(), attachTo: document.body, global: GLOBAL })
    await nextTick()

    expect($('nvm-spending-loading').attributes('aria-busy')).toBe('true')
    expect(w.text()).not.toContain('No Nevermined card mandate')
    expect($('nvm-spending-enroll').exists()).toBe(false)
  })

  it('#2 not configured: admins get the Settings link, others plain text; only the delegation route is read', async () => {
    useAuthStore().user = { role: 'admin' }
    await render(BODY({ configured: false }))

    const link = w.findComponent(RouterLinkStub)
    expect(link.exists()).toBe(true)
    expect(link.props('to')).toBe('/settings?tab=integrations')
    expect(link.text()).toContain('Set up the Nevermined organization')
    expect(api.get).toHaveBeenCalledTimes(1)
    expect(api.get).toHaveBeenCalledWith(URL_A)
    expect(api.get.mock.calls.some(([u]) => u.includes('/organization'))).toBe(false)
    w.unmount()

    setActivePinia(createPinia())
    useAuthStore().user = { role: 'creator' }
    await render(BODY({ configured: false }))
    expect(w.findComponent(RouterLinkStub).exists()).toBe(false)
    expect(w.text()).toContain('An admin sets up the Nevermined organization first.')
  })

  it('#3 not funded: owner + running → Enroll enabled', async () => {
    await render(BODY())
    expect(w.text()).toContain('No Nevermined card mandate is bound to this agent.')
    const btn = $('nvm-spending-enroll')
    expect(btn.exists()).toBe(true)
    expect(btn.attributes('disabled')).toBeUndefined()
    expect($('nvm-spending-revoke').exists()).toBe(false)
  })

  it('#3 not funded: owner + stopped → Enroll disabled with the start-the-agent hint', async () => {
    await render(BODY(), { agentStatus: 'stopped' })
    expect($('nvm-spending-enroll').attributes('disabled')).toBeDefined()
    expect($('nvm-spending-not-running').text()).toContain('Start the agent first')
  })

  it('#3 not funded: a shared viewer gets text, no button', async () => {
    await render(BODY(), { canEdit: false })
    expect($('nvm-spending-enroll').exists()).toBe(false)
    expect(w.text()).toContain("The agent's owner can fund it with a Nevermined card mandate.")
  })

  it('#6 funded: the numbers are Nevermined\'s; owner sees Revoke and who bound it', async () => {
    await render(FUNDED())
    const kpis = $('nvm-spending-kpis').text()
    expect(kpis).toContain('$6.00')   // remaining
    expect(kpis).toContain('$10.00')  // limit
    expect(kpis).toContain('$4.00')   // spent
    expect(kpis).toContain('2 of 10') // purchases
    expect($('nvm-spending-expires').attributes('title')).toBeTruthy()
    expect($('nvm-spending-status').text()).toContain('Active')
    expect(w.text()).toContain("Takes effect at the agent's next run")
    expect(w.text()).toContain('Nevermined holds the card')
    expect($('nvm-spending-revoke').exists()).toBe(true)
    expect($('nvm-spending-enroll').exists()).toBe(false)
    expect($('nvm-spending-bound-by').text()).toContain('owner@example.com')
    expect($('nvm-spending-history').text()).toContain('(1)')
  })

  it('#6 funded: a shared viewer sees the numbers but neither Revoke nor the binder\'s email', async () => {
    await render(FUNDED(), { canEdit: false })
    expect($('nvm-spending-kpis').text()).toContain('$6.00')
    expect($('nvm-spending-revoke').exists()).toBe(false)
    expect($('nvm-spending-bound-by').exists()).toBe(false)
    expect(w.text()).not.toContain('owner@example.com')
  })

  it('#7 funded but Nevermined unreachable: every number unknown, nothing derived', async () => {
    await render(FUNDED(LIVE({
      source: 'unavailable', status: 'unknown',
      remaining_cents: null, spending_limit_cents: 1000, spent_cents: 400, error: 'nevermined_unreachable',
    })))
    expect($('nvm-spending-unavailable').text()).toContain('Nevermined unreachable')
    const kpis = $('nvm-spending-kpis').text()
    expect(kpis).toContain('unknown')
    // Neither the reported nor a derived figure (limit − spent = $6.00) is shown.
    expect(kpis).not.toMatch(/\$/)
    expect(w.text()).not.toContain('$6.00')
    expect(w.text()).toContain('nothing is estimated locally')
    expect($('nvm-spending-revoke').exists()).toBe(true)
  })

  it('#15 a failed first read is LoadFailed with Retry', async () => {
    api.get.mockRejectedValueOnce({ response: { status: 500, data: { detail: 'boom' } } })
    w = mount(NeverminedSpendingPanel, { props: PROPS(), attachTo: document.body, global: GLOBAL })
    await flushPromises()

    expect(w.text()).toContain("Couldn't read this agent's Nevermined spending")
    expect($('nvm-spending-kpis').exists()).toBe(false)
    expect(w.text()).not.toContain('No Nevermined card mandate')

    api.get.mockResolvedValueOnce({ data: BODY() })
    const retry = w.findAll('button').find((b) => b.text().includes('Try again'))
    await retry.trigger('click')
    await flushPromises()
    expect(w.text()).toContain('No Nevermined card mandate is bound to this agent.')
  })

  it('#15 a failed refresh keeps the data on screen and raises the stale banner', async () => {
    await render(FUNDED())
    api.get.mockRejectedValueOnce({ response: { status: 503, data: { detail: 'down' } } })
    await $('nvm-spending-refresh').trigger('click')
    await flushPromises()

    expect($('nvm-spending-kpis').text()).toContain('$6.00')
    expect($('nvm-spending-stale').text()).toContain("Couldn't refresh this agent's Nevermined spending")
    expect($('nvm-spending-loading').exists()).toBe(false)
  })

  it('#17 KeepAlive: re-entry re-reads in the background, no skeleton', async () => {
    api.get.mockImplementation(async () => ({ data: FUNDED() }))
    const show = ref(true)
    const Host = defineComponent({
      render: () => h(KeepAlive, null, [show.value
        ? h(NeverminedSpendingPanel, { key: 'p', ...PROPS() })
        : h('div', { key: 'x' }, 'elsewhere')]),
    })
    w = mount(Host, { attachTo: document.body, global: GLOBAL })
    await flushPromises()
    expect(api.get).toHaveBeenCalledTimes(1)

    show.value = false
    await flushPromises()
    let resolve
    api.get.mockImplementation(() => new Promise((r) => { resolve = r }))
    show.value = true
    await flushPromises()

    expect(api.get).toHaveBeenCalledTimes(2)
    expect($('nvm-spending-loading').exists()).toBe(false)
    expect($('nvm-spending-kpis').text()).toContain('$6.00')
    resolve({ data: FUNDED(LIVE({ remaining_cents: 100 })) })
    await flushPromises()
    expect($('nvm-spending-kpis').text()).toContain('$1.00')
  })

  it('a response for an agent the panel has moved off is dropped (generation guard)', async () => {
    let resolveA
    api.get.mockImplementation((url) => (url === URL_A
      ? new Promise((r) => { resolveA = r })
      : Promise.resolve({ data: BODY({ agent_name: 'b' }) })))
    w = mount(NeverminedSpendingPanel, { props: PROPS(), attachTo: document.body, global: GLOBAL })
    await nextTick()
    await w.setProps({ agentName: 'b' })
    await flushPromises()
    expect(api.get).toHaveBeenLastCalledWith('/api/nevermined/agents/b/delegation')
    expect(w.text()).toContain('No Nevermined card mandate is bound to this agent.')

    resolveA({ data: FUNDED() })
    await flushPromises()
    expect($('nvm-spending-kpis').exists()).toBe(false)
  })

  it('#18 every action is a BaseButton; a read landing after unmount writes nothing', async () => {
    await render(FUNDED())
    const ids = w.findAllComponents(BaseButton).map((b) => b.vm.$attrs['data-testid'])
    expect(ids).toEqual(expect.arrayContaining(['nvm-spending-refresh', 'nvm-spending-revoke']))
    w.unmount()

    await render(BODY())
    expect(w.findAllComponents(BaseButton).map((b) => b.vm.$attrs['data-testid'])).toContain('nvm-spending-enroll')

    let resolve
    api.get.mockImplementation(() => new Promise((r) => { resolve = r }))
    await $('nvm-spending-refresh').trigger('click')
    w.unmount()
    w = null
    resolve({ data: FUNDED() })
    await flushPromises() // no throw, no warning about an unmounted instance
  })
})
