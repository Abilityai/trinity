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
 *
 * Checkpoint B: the verbs. The Nevermined SDK is mocked (`vi.mock`) with a
 * fake instance whose `delegations.setupCard` captures its options, so a test
 * fires `onBooted / onReady / onSuccess({result, handle}) / onError / onClose`
 * exactly as the SDK would and reads what the panel did with them:
 *  - inline vs overlay by the slot's measured width; Esc closes the overlay;
 *  - `pending` is persisted before the bind POST, FUNDED renders from the 201,
 *    `handle.close()` only after it, then the SDK instance is destroyed;
 *  - a refused bind keeps the ids and Retry bind POSTs the same ids without
 *    mounting the form again (incl. 409 agent_key_present, addendum F4);
 *  - an agent change mid-flow never binds to the new agent;
 *  - Revoke goes through ConfirmDialog (Cancel focused), and a revoke that
 *    could not edit the .env shows the backend's env_note verbatim.
 */
import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest'
import { mount, flushPromises, RouterLinkStub } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { defineComponent, h, KeepAlive, ref, nextTick } from 'vue'

const api = vi.hoisted(() => ({ get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() }))
vi.mock('@/api', () => ({ default: api }))

// The Nevermined SDK, faked at the module boundary the panel's dynamic import() hits.
const sdk = vi.hoisted(() => {
  const state = { options: [], nvms: [], valid: [true], initError: null }
  function makeNvm() {
    const handlers = {}
    const hasValidSession = state.valid.length > 1 ? state.valid.shift() : state.valid[0]
    const nvm = {
      hasValidSession,
      on: vi.fn((ev, fn) => { handlers[ev] = fn; return nvm }),
      off: vi.fn(),
      destroy: vi.fn(),
      emit: (ev) => handlers[ev]?.(),
      delegations: { setupCard: vi.fn((opts) => { state.options.push(opts) }) },
    }
    state.nvms.push(nvm)
    return nvm
  }
  const initialize = vi.fn(async () => {
    if (state.initError) throw state.initError
    return makeNvm()
  })
  return { state, initialize }
})
vi.mock('@nevermined-io/ui-widgets', () => ({ NeverminedWidgets: { initialize: sdk.initialize } }))

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
  Object.assign(sdk.state, { options: [], nvms: [], valid: [true], initError: null })
  sessionStorage.clear()
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

// ---------------------------------------------------------------------------
// Checkpoint B — enroll → bind, retry, revoke, teardown
// ---------------------------------------------------------------------------

const WALLET = '0x' + 'b'.repeat(40)
const SESSION = { sessionToken: 'tok', userId: 'u1', userWallet: WALLET, apiKeyHash: 'h', expiresAt: '2099-01-01T00:00:00Z' }
const MINT = { session: SESSION, environment: 'sandbox', org_id: 'org-1' }
const IDS = { delegationId: 'dlg_new0000000001', paymentMethodId: 'pm_new000000001' }
const BOUND = () => ({ ...FUNDED(), effective: 'next_execution', api_key_injected: true })
const REFUSAL = (status, code, message) => ({ response: { status, data: { detail: { code, message } }, headers: {} } })
const BIND_URL = '/api/nevermined/agents/a/delegation'

function deferred() {
  let resolve, reject
  const promise = new Promise((res, rej) => { resolve = res; reject = rej })
  return { promise, resolve, reject }
}

/** Route api.post: the widget-session mint and the bind. */
function routePost({ mint = () => Promise.resolve({ data: MINT }), bind = () => Promise.resolve({ data: BOUND() }) } = {}) {
  api.post.mockImplementation((url, body) => (url.endsWith('/widget-session') ? mint(url) : bind(body, url)))
}
const bindCalls = () => api.post.mock.calls.filter(([u]) => u.endsWith('/delegation'))
const mintCalls = () => api.post.mock.calls.filter(([u]) => u.endsWith('/widget-session'))
const opts = () => sdk.state.options[sdk.state.options.length - 1]
const lastNvm = () => sdk.state.nvms[sdk.state.nvms.length - 1]
const pendingStored = (agent = 'a') => JSON.parse(sessionStorage.getItem(`nvm-pending:${agent}`) || 'null')

async function enroll() {
  await $('nvm-spending-enroll').trigger('click')
  await flushPromises()
}
function beforeUnloadPrevented() {
  const e = new Event('beforeunload', { cancelable: true })
  window.dispatchEvent(e)
  return e.defaultPrevented
}

describe('NeverminedSpendingPanel — enroll and bind', () => {
  it('#4 inline: mint → initialize verbatim → setupCard in the slot → pending persisted → bind → FUNDED from the 201 → close → destroy', async () => {
    const notify = vi.fn()
    const bindReply = deferred()
    routePost({ bind: () => bindReply.promise })
    await render(BODY(), { measureWidth: () => 600, notify })
    await enroll()

    expect(mintCalls()).toEqual([['/api/nevermined/agents/a/widget-session']])
    expect(sdk.initialize).toHaveBeenCalledWith({ session: SESSION, environment: 'sandbox' })
    const o = opts()
    expect(o.container).toBeInstanceOf(HTMLElement)
    expect(document.body.contains(o.container)).toBe(true)
    expect(o.container.getAttribute('data-testid')).toBe('nvm-spending-widget-slot')
    expect(o.width).toBe(600)
    expect(o.height).toBe(720)
    expect(o.provider).toBe('stripe')
    expect($('nvm-spending-opening').exists()).toBe(true)

    o.onBooted()
    o.onReady()
    await nextTick()
    expect($('nvm-spending-opening').exists()).toBe(false)
    expect(beforeUnloadPrevented()).toBe(true)

    const handle = { close: vi.fn(() => o.onClose()) }
    o.onSuccess({ result: IDS, handle })
    await nextTick()
    // Persisted BEFORE the bind answers; the form is not closed yet.
    expect(pendingStored()).toMatchObject({ agent: 'a', ...IDS, provider: 'stripe' })
    expect(bindCalls()).toEqual([[BIND_URL, { ...IDS, provider: 'stripe' }]])
    expect(handle.close).not.toHaveBeenCalled()
    expect($('nvm-spending-binding').exists()).toBe(true)
    expect(beforeUnloadPrevented()).toBe(true)

    bindReply.resolve({ data: BOUND() })
    await flushPromises()

    expect($('nvm-spending-kpis').text()).toContain('$6.00')
    expect(api.get).toHaveBeenCalledTimes(1) // FUNDED came from the 201 — no re-read
    expect(handle.close).toHaveBeenCalledTimes(1)
    expect(lastNvm().destroy).toHaveBeenCalled()
    expect(pendingStored()).toBeNull()
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('Funded through Nevermined'), 'success')
    expect(beforeUnloadPrevented()).toBe(false)
  })

  it('#5 narrow slot: the SDK overlay (no container); Esc closes it back to not funded', async () => {
    routePost()
    await render(BODY(), { measureWidth: () => 360 })
    await enroll()

    const o = opts()
    expect(o.container).toBeUndefined()
    expect(o.width).toBeUndefined()
    expect($('nvm-spending-overlay-note').text()).toContain('Press Esc')
    expect($('nvm-spending-widget-slot').element.style.display).toBe('none')

    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await nextTick()
    expect(lastNvm().destroy).toHaveBeenCalled()
    expect($('nvm-spending-enroll').exists()).toBe(true)
    expect($('nvm-spending-overlay-note').exists()).toBe(false)
    expect(bindCalls()).toHaveLength(0)
  })

  it('#5 Esc does nothing for the inline form', async () => {
    routePost()
    await render(BODY(), { measureWidth: () => 600 })
    await enroll()
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    await nextTick()
    expect(lastNvm().destroy).not.toHaveBeenCalled()
    expect($('nvm-spending-enroll').exists()).toBe(false)
  })

  it('the user closing the form before success returns to not funded, no error', async () => {
    routePost()
    await render(BODY(), { measureWidth: () => 600 })
    await enroll()
    opts().onClose()
    await nextTick()
    expect($('nvm-spending-enroll').exists()).toBe(true)
    expect($('nvm-spending-enroll-error').exists()).toBe(false)
    expect(lastNvm().destroy).toHaveBeenCalled()
  })

  it('an onError renders userMessage, never the raw message', async () => {
    routePost()
    await render(BODY(), { measureWidth: () => 600 })
    await enroll()
    opts().onError({ code: 'NETWORK', message: 'ECONNRESET upstream-host', userMessage: 'Nevermined could not be reached.' })
    await nextTick()
    expect($('nvm-spending-enroll-error').text()).toContain('Nevermined could not be reached.')
    expect(w.text()).not.toContain('ECONNRESET')
  })

  it('ids Nevermined returned off-pattern are never POSTed', async () => {
    routePost()
    await render(BODY(), { measureWidth: () => 600 })
    await enroll()
    opts().onSuccess({ result: { delegationId: 'bad id' }, handle: { close: vi.fn() } })
    await flushPromises()
    expect(bindCalls()).toHaveLength(0)
    expect($('nvm-spending-enroll-error').text()).toContain('Nevermined returned ids Trinity could not use')
  })

  it('a widget-session refusal shows the backend message; a failed SDK load says so', async () => {
    api.post.mockRejectedValueOnce(REFUSAL(503, 'nevermined_unreachable', 'Nevermined is unreachable right now.'))
    await render(BODY(), { measureWidth: () => 600 })
    await enroll()
    expect($('nvm-spending-enroll-error').text()).toContain('Nevermined is unreachable right now.')
    expect(sdk.initialize).not.toHaveBeenCalled()
    w.unmount()

    routePost()
    await render(BODY(), { measureWidth: () => 600, loadSdk: () => Promise.reject(new Error('chunk')) })
    await enroll()
    expect($('nvm-spending-enroll-error').text()).toContain('Could not load the Nevermined widget')
  })

  it('an unusable session body or an INVALID_SESSION init is a named error', async () => {
    routePost({ mint: () => Promise.resolve({ data: { session: { nope: 1 }, environment: 'sandbox' } }) })
    await render(BODY(), { measureWidth: () => 600 })
    await enroll()
    expect(sdk.initialize).not.toHaveBeenCalled()
    expect($('nvm-spending-enroll-error').text()).toContain("Nevermined returned a session Trinity's widget could not use.")
    w.unmount()

    sdk.state.initError = Object.assign(new Error('bad'), { name: 'WidgetInitError', code: 'INVALID_SESSION' })
    routePost()
    await render(BODY(), { measureWidth: () => 600 })
    await enroll()
    expect($('nvm-spending-enroll-error').text()).toContain('INVALID_SESSION')
  })

  it('#12 bind 409 agent_not_running: backend message + Retry bind POSTs the same ids, form not mounted again; onClose mid-POST keeps pending', async () => {
    const first = deferred()
    let n = 0
    routePost({ bind: () => (++n === 1 ? first.promise : Promise.resolve({ data: BOUND() })) })
    await render(BODY(), { measureWidth: () => 600 })
    await enroll()
    const o = opts()
    o.onSuccess({ result: IDS, handle: { close: vi.fn() } })
    await nextTick()
    o.onClose() // the user dismisses during the POST
    await nextTick()
    expect(pendingStored()).toMatchObject(IDS)

    first.reject(REFUSAL(409, 'agent_not_running', 'Start the agent, then bind the Nevermined mandate again.'))
    await flushPromises()
    const err = $('nvm-spending-enroll-error')
    expect(err.text()).toContain('Start the agent, then bind the Nevermined mandate again.')
    expect(err.text()).toContain('The mandate already exists at Nevermined')
    expect(pendingStored()).toMatchObject(IDS)

    const retry = err.findAll('button').find((b) => b.text().includes('Retry bind'))
    await retry.trigger('click')
    await flushPromises()
    expect(bindCalls()).toHaveLength(2)
    expect(bindCalls()[1]).toEqual(bindCalls()[0])
    expect(lastNvm().delegations.setupCard).toHaveBeenCalledTimes(1)
    expect(sdk.state.options).toHaveLength(1)
    expect($('nvm-spending-kpis').text()).toContain('$6.00')
    expect(pendingStored()).toBeNull()
  })

  it('#12 bind 409 agent_key_present (addendum F4): the backend message verbatim, Retry bind → 201 → FUNDED', async () => {
    const MSG = "This agent's credentials already hold an NVM_API_KEY that is not the organization's Nevermined agent key. Remove it from the agent's credentials and bind again; the mandate still exists at Nevermined."
    let n = 0
    routePost({ bind: () => (++n === 1 ? Promise.reject(REFUSAL(409, 'agent_key_present', MSG)) : Promise.resolve({ data: BOUND() })) })
    await render(BODY(), { measureWidth: () => 600 })
    await enroll()
    opts().onSuccess({ result: IDS, handle: { close: vi.fn() } })
    await flushPromises()

    const err = $('nvm-spending-enroll-error')
    expect(err.text()).toContain(MSG)
    expect(pendingStored()).toMatchObject(IDS)
    expect($('nvm-spending-kpis').exists()).toBe(false)

    await err.findAll('button').find((b) => b.text().includes('Retry bind')).trigger('click')
    await flushPromises()
    expect(bindCalls().map(([, b]) => b)).toEqual([{ ...IDS, provider: 'stripe' }, { ...IDS, provider: 'stripe' }])
    expect(sdk.state.options).toHaveLength(1)
    expect($('nvm-spending-kpis').text()).toContain('$6.00')
  })

  it('bind 422 delegation_inactive clears pending and offers a new mandate', async () => {
    routePost({ bind: () => Promise.reject(REFUSAL(422, 'delegation_inactive', 'Nevermined reports this mandate is not active.')) })
    await render(BODY(), { measureWidth: () => 600 })
    await enroll()
    opts().onSuccess({ result: IDS, handle: { close: vi.fn() } })
    await flushPromises()
    expect($('nvm-spending-enroll-error').text()).toContain('Create a new Nevermined mandate')
    expect(pendingStored()).toBeNull()
  })

  it('bind 409 delegation_active re-reads and lands on FUNDED', async () => {
    const notify = vi.fn()
    routePost({ bind: () => Promise.reject(REFUSAL(409, 'delegation_active', 'Already funded through Nevermined.')) })
    await render(BODY(), { measureWidth: () => 600, notify })
    api.get.mockImplementation(async () => ({ data: FUNDED() }))
    await enroll()
    opts().onSuccess({ result: IDS, handle: { close: vi.fn() } })
    await flushPromises()
    expect(api.get).toHaveBeenCalledTimes(2)
    expect($('nvm-spending-kpis').text()).toContain('$6.00')
    expect(notify).toHaveBeenCalledWith(expect.stringContaining('already funded'), 'info')
  })

  it('#13 an agent change between setupCard and onSuccess: no POST, destroyed, pending kept for the original agent', async () => {
    routePost()
    await render(BODY(), { measureWidth: () => 600 })
    await enroll()
    const o = opts()
    const nvm = lastNvm()
    api.get.mockImplementation(async () => ({ data: BODY({ agent_name: 'b' }) }))
    await w.setProps({ agentName: 'b' })
    await flushPromises()
    expect(nvm.destroy).toHaveBeenCalled()

    o.onSuccess({ result: IDS, handle: { close: vi.fn() } })
    await flushPromises()
    expect(bindCalls()).toHaveLength(0)
    expect(pendingStored('a')).toMatchObject({ agent: 'a', ...IDS })
    expect(pendingStored('b')).toBeNull()
    expect($('nvm-spending-pending').exists()).toBe(false)
  })

  it('#14 a persisted pending on a not-funded agent: banner; Retry bind POSTs it; Discard clears it', async () => {
    sessionStorage.setItem('nvm-pending:a', JSON.stringify({ agent: 'a', ...IDS, provider: 'stripe', at: 1 }))
    routePost()
    await render(BODY())
    expect($('nvm-spending-pending').text()).toContain('A Nevermined mandate was created but not bound')
    expect($('nvm-spending-enroll').exists()).toBe(false)

    await $('nvm-spending-pending-retry').trigger('click')
    await flushPromises()
    expect(bindCalls()).toEqual([[BIND_URL, { ...IDS, provider: 'stripe' }]])
    expect(sdk.initialize).not.toHaveBeenCalled()
    expect($('nvm-spending-kpis').text()).toContain('$6.00')
    w.unmount()

    sessionStorage.setItem('nvm-pending:a', JSON.stringify({ agent: 'a', ...IDS, provider: 'stripe', at: 1 }))
    await render(BODY())
    await $('nvm-spending-pending-discard').trigger('click')
    await nextTick()
    expect(pendingStored()).toBeNull()
    expect($('nvm-spending-pending').exists()).toBe(false)
    expect($('nvm-spending-enroll').exists()).toBe(true)
  })

  it('#16 an already-expired session re-mints once; twice is "start again"; session-expired with a mounted form changes nothing', async () => {
    sdk.state.valid = [false, true]
    routePost()
    await render(BODY(), { measureWidth: () => 600 })
    await enroll()
    expect(mintCalls()).toHaveLength(2)
    expect(sdk.state.options).toHaveLength(1)
    expect(sdk.state.nvms[0].destroy).toHaveBeenCalled()

    lastNvm().emit('session-expired')
    await nextTick()
    expect(lastNvm().destroy).not.toHaveBeenCalled()
    expect($('nvm-spending-enroll-error').exists()).toBe(false)
    w.unmount()

    sdk.state.valid = [false, false]
    vi.clearAllMocks()
    routePost()
    await render(BODY(), { measureWidth: () => 600 })
    await enroll()
    expect(mintCalls()).toHaveLength(2)
    expect($('nvm-spending-enroll-error').text()).toContain('The Nevermined session expired — start again.')
  })

  it('#17 KeepAlive: leaving the tab during the form destroys it and disarms beforeunload', async () => {
    routePost()
    api.get.mockImplementation(async () => ({ data: BODY() }))
    const show = ref(true)
    const Host = defineComponent({
      render: () => h(KeepAlive, null, [show.value
        ? h(NeverminedSpendingPanel, { key: 'p', ...PROPS(), measureWidth: () => 600 })
        : h('div', { key: 'x' }, 'elsewhere')]),
    })
    w = mount(Host, { attachTo: document.body, global: GLOBAL })
    await flushPromises()
    await enroll()
    opts().onReady()
    await nextTick()
    expect(beforeUnloadPrevented()).toBe(true)

    show.value = false
    await flushPromises()
    expect(lastNvm().destroy).toHaveBeenCalled()
    expect(beforeUnloadPrevented()).toBe(false)
  })

  it('#18 unmount during the form destroys the SDK instance and drops the Esc listener', async () => {
    routePost()
    await render(BODY(), { measureWidth: () => 360 })
    await enroll()
    const nvm = lastNvm()
    w.unmount()
    w = null
    expect(nvm.destroy).toHaveBeenCalledTimes(1)
    window.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape' }))
    expect(nvm.destroy).toHaveBeenCalledTimes(1)
  })
})

describe('NeverminedSpendingPanel — revoke', () => {
  const ENV_NOTE = "agent_not_running — the Nevermined mandate is revoked and the delegation id is inert; the organization's agent key stays in the agent's .env until the next bind rewrites it or you remove it"
  const dialog = () => document.querySelector('[data-testid="confirm-dialog"]')
  const dialogBtn = (id) => document.querySelector(`[data-testid="${id}"]`)

  it('#6 Revoke asks first (Cancel focused, Nevermined holds the card); Cancel deletes nothing', async () => {
    await render(FUNDED())
    await $('nvm-spending-revoke').trigger('click')
    await flushPromises()

    expect(dialog()).not.toBeNull()
    expect(dialogBtn('confirm-dialog-title').textContent).toContain('Revoke this mandate at Nevermined?')
    const msg = dialogBtn('confirm-dialog-message').textContent
    expect(msg).toContain('Nevermined holds the card')
    expect(msg).toContain('$6.00')
    expect(dialogBtn('confirm-dialog-confirm').textContent).toContain('Revoke mandate')
    expect(document.activeElement?.getAttribute('data-testid')).toBe('confirm-dialog-cancel')

    dialogBtn('confirm-dialog-cancel').click()
    await flushPromises()
    expect(api.delete).not.toHaveBeenCalled()
    expect($('nvm-spending-kpis').exists()).toBe(true)
  })

  it('#6 confirm → DELETE → toast + env_note verbatim → re-read → not funded', async () => {
    const notify = vi.fn()
    await render(FUNDED(), { notify })
    api.delete.mockResolvedValueOnce({ data: { revoked: true, delegation_id: 'dlg_abcdef0123456789', env_removed: false, env_note: ENV_NOTE } })
    api.get.mockImplementation(async () => ({ data: BODY() }))
    await $('nvm-spending-revoke').trigger('click')
    await flushPromises()
    dialogBtn('confirm-dialog-confirm').click()
    await flushPromises()

    expect(api.delete).toHaveBeenCalledWith(BIND_URL)
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(w.text()).toContain('No Nevermined card mandate is bound to this agent.')
    const note = $('nvm-spending-env-note')
    expect(note.text()).toContain(ENV_NOTE)
    expect(note.attributes('data-tone')).toBe('stopped')
    expect(w.text()).not.toMatch(/stored key is inert/)
    expect(notify).toHaveBeenCalledWith('Mandate revoked at Nevermined.', 'success')
  })

  it('#6 env_removed:true shows no note; agent_unreachable picks the warning tone', async () => {
    await render(FUNDED())
    api.delete.mockResolvedValueOnce({ data: { revoked: true, delegation_id: 'x', env_removed: true, env_note: null } })
    api.get.mockImplementation(async () => ({ data: BODY() }))
    await $('nvm-spending-revoke').trigger('click')
    await flushPromises()
    dialogBtn('confirm-dialog-confirm').click()
    await flushPromises()
    expect($('nvm-spending-env-note').exists()).toBe(false)
    w.unmount()

    const UNREACH = ENV_NOTE.replace('agent_not_running', 'agent_unreachable')
    await render(FUNDED())
    api.delete.mockResolvedValueOnce({ data: { revoked: true, delegation_id: 'x', env_removed: false, env_note: UNREACH } })
    api.get.mockImplementation(async () => ({ data: BODY() }))
    await $('nvm-spending-revoke').trigger('click')
    await flushPromises()
    dialogBtn('confirm-dialog-confirm').click()
    await flushPromises()
    expect($('nvm-spending-env-note').attributes('data-tone')).toBe('unreachable')
    expect($('nvm-spending-env-note').text()).toContain(UNREACH)
  })

  it('#6 revoke 404 no_active_delegation re-reads; 503 keeps FUNDED with the backend message', async () => {
    await render(FUNDED())
    api.delete.mockRejectedValueOnce(REFUSAL(404, 'no_active_delegation', 'No active Nevermined mandate.'))
    api.get.mockImplementation(async () => ({ data: BODY() }))
    await $('nvm-spending-revoke').trigger('click')
    await flushPromises()
    dialogBtn('confirm-dialog-confirm').click()
    await flushPromises()
    expect(api.get).toHaveBeenCalledTimes(2)
    expect(w.text()).toContain('No Nevermined card mandate is bound to this agent.')
    w.unmount()

    await render(FUNDED())
    api.delete.mockRejectedValueOnce(REFUSAL(503, 'nevermined_unreachable', 'Nevermined is unreachable; the mandate is still active.'))
    await $('nvm-spending-revoke').trigger('click')
    await flushPromises()
    dialogBtn('confirm-dialog-confirm').click()
    await flushPromises()
    expect($('nvm-spending-revoke-error').text()).toContain('Nevermined is unreachable; the mandate is still active.')
    expect($('nvm-spending-kpis').text()).toContain('$6.00')
  })
})
