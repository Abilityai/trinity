// @vitest-environment jsdom
/**
 * trinity-enterprise#638 — the seat decision record, mounted.
 *
 * The panel owns four verbs (record, correct, reconfirm/reverse, close) and
 * the honest rendering of the record: expired says expired, routed says
 * routed to canon, history is collapsed, another seat is labelled, and a
 * refusal (the grammar receipt) lands on the field it names. Mounted against
 * the real store with HTTP mocked (#2918 — a regex over the SFC proves none
 * of that).
 */
import { beforeEach, describe, expect, it, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'

vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
})
vi.mock('@/stores/auth', async () => {
  const { ref } = await import('vue')
  const authed = ref(true)
  return { useAuthStore: () => ({ get isAuthenticated() { return authed.value }, get authHeader() { return { Authorization: 'Bearer jwt' } } }) }
})
vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})
vi.mock('@/api', () => ({ default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }))

import axios from 'axios'
import { useClientPortalStore } from '@/stores/clientPortal'
import PortalAgentDecisions from '@/components/portal/PortalAgentDecisions.vue'

const AGENT = 'sales-companion'
const URL = `/api/enterprise/client-portal/agents/${AGENT}/decisions`
const ME = 'gary@example.com'
const ago = (h) => new Date(Date.now() - h * 3600_000).toISOString()

function decision(over = {}) {
  return {
    id: 'd1', seat: ME, outcome: 'approved', decided: 'Renew the Acme contract',
    alternatives: ['let it lapse', 'renegotiate first'], criterion: 'renewal cost below the switching cost',
    reversal: 'Acme raises the price above the switching cost',
    decided_by: { role: 'sales-lead', person: ME }, decided_at: ago(2), review_by: '2027-01-31',
    notes: null, ask_class: 'vendor-renewal', scope: 'seat', status: 'active', supersedes_id: null, cites: [],
    request_id: null, close_reason: null, closed_at: null, closed_by: null, reconfirmed_at: null, writable: true,
    ...over,
  }
}

function page(over = {}) {
  return {
    agent_name: AGENT, my_seat: ME, seats: [ME],
    decisions: [decision()],
    stats: { recorded: 1, reused: 0, reuse_rate: 0, reversed: 0,
             ask_classes: [{ ask_class: 'vendor-renewal', count: 1, criteria: ['renewal cost below the switching cost'], reversals: 0, expired: 0, stable: false }] },
    can_record: true,
    ...over,
  }
}

async function mountWith(payload) {
  axios.get.mockResolvedValueOnce({ data: payload })
  const w = mount(PortalAgentDecisions, { props: { agentName: AGENT }, attachTo: document.body })
  await flushPromises()
  return w
}

beforeEach(() => { setActivePinia(createPinia()); vi.clearAllMocks(); document.body.innerHTML = '' })

describe('PortalAgentDecisions (mounted)', () => {
  it('renders the record criterion-first with the evidence line, and the empty state teaches the next action', async () => {
    const w = await mountWith(page())
    expect(axios.get).toHaveBeenCalledWith(URL, expect.anything())
    const card = w.get('[data-testid="portal-decision-active"]')
    expect(card.text()).toContain('Renew the Acme contract')
    expect(card.text()).toContain('Because renewal cost below the switching cost')
    expect(card.text()).toContain('Instead of let it lapse / renegotiate first')
    expect(card.text()).toContain('sales-lead')
    expect(w.get('[data-testid="portal-decisions-stats"]').text()).toContain('1 recorded · 0 reused')

    setActivePinia(createPinia()); document.body.innerHTML = ''
    const empty = await mountWith(page({ decisions: [], stats: { recorded: 0, reused: 0, reuse_rate: 0, reversed: 0, ask_classes: [] } }))
    expect(empty.get('[data-testid="portal-decisions-empty"]').text()).toContain('record why')
  })

  it('expired says expired, routed says routed, history is collapsed, another seat is labelled', async () => {
    const w = await mountWith(page({
      seats: [ME, 'ops@example.com'],
      decisions: [
        decision({ id: 'x', status: 'expired', review_by: '2020-01-01' }),
        decision({ id: 'r', status: 'routed', scope: 'direction', decided: 'Raise prices 10%' }),
        decision({ id: 's', status: 'superseded', decided: 'old wording' }),
        decision({ id: 'v', status: 'reversed', close_reason: 'Acme raised the price', decided: 'Renew Acme' }),
        decision({ id: 'o', seat: 'ops@example.com', writable: false, decided: 'Ops thing' }),
      ],
    }))
    expect(w.get('[data-testid="portal-decision-expired"]').text()).toContain('expired')
    expect(w.get('[data-testid="portal-decision-routed"]').text()).toContain('routed to canon')
    expect(w.find('[data-testid="portal-decision-routed"] [data-testid^="portal-decision-close-"]').exists()).toBe(false)
    expect(w.get('[data-testid="portal-decisions-history"]').text()).toContain('History · 2')
    expect(w.get('[data-testid="portal-decision-reversed"]').text()).toContain('Acme raised the price')
    const other = w.findAll('[data-testid="portal-decision-active"]').find((c) => c.text().includes('Ops thing'))
    expect(other.text()).toContain('seat · ops@example.com')
    expect(other.find('[data-testid^="portal-decision-close-"]').exists()).toBe(false)   // read-only
  })

  it('records a decision: alternatives are chips, the body is posted, the list re-reads', async () => {
    const w = await mountWith(page({ decisions: [], stats: { recorded: 0, reused: 0, reuse_rate: 0, reversed: 0, ask_classes: [] } }))
    await w.get('[data-testid="portal-decision-record-open"]').trigger('click')
    const form = w.get('[data-testid="portal-decision-form"]')
    const inputs = form.findAll('input')
    const byLabel = (label) => {
      const lab = form.findAll('label').find((l) => l.text().startsWith(label))
      return form.find(`#${lab.attributes('for')}`)
    }
    await byLabel('What was decided').setValue('Renew the Acme contract')
    await byLabel('The criterion').setValue('renewal cost below the switching cost')
    const alt = byLabel('Alternatives')
    await alt.setValue('let it lapse'); await alt.trigger('keydown', { key: 'Enter' })
    await alt.setValue('renegotiate first'); await alt.trigger('keydown', { key: 'Enter' })
    expect(w.findAll('[data-testid="portal-decision-alternative"]')).toHaveLength(2)
    await byLabel('What would reverse').setValue('Acme raises the price')
    await byLabel('Review by').setValue('2027-01-31')
    expect(inputs.length).toBeGreaterThan(3)

    axios.post.mockResolvedValueOnce({ data: { decision: decision(), hint: null } })
    axios.get.mockResolvedValueOnce({ data: page() })
    await form.trigger('submit')
    await flushPromises()
    expect(axios.post).toHaveBeenCalledWith(URL, expect.objectContaining({
      outcome: 'approved', decided: 'Renew the Acme contract', alternatives: ['let it lapse', 'renegotiate first'],
      criterion: 'renewal cost below the switching cost', reversal: 'Acme raises the price', review_by: '2027-01-31', scope: 'seat',
    }), expect.anything())
    expect(w.find('[data-testid="portal-decision-form"]').exists()).toBe(false)
    expect(w.get('[data-testid="portal-decision-active"]').text()).toContain('Renew the Acme contract')
  })

  it('a refused record lands on the field the receipt names and the form stays open', async () => {
    const w = await mountWith(page())
    await w.get('[data-testid="portal-decision-record-open"]').trigger('click')
    axios.post.mockRejectedValueOnce({ response: { status: 422, data: { detail: {
      code: 'decision_prose_only', message: 'The record is prose where a field belongs.',
      receipt: { fields: { criterion: 'criterion must be one line (a paragraph is prose — put it in notes)' } },
    } } } })
    await w.get('[data-testid="portal-decision-form"]').trigger('submit')
    await flushPromises()
    expect(w.get('[data-testid="portal-decision-refusal"]').text()).toContain('prose where a field belongs')
    expect(w.get('[data-testid="portal-decision-form"]').text()).toContain('criterion must be one line')
    expect(useClientPortalStore().decisionBusy).toBeNull()
  })

  it('a direction decision shows the canon hint the server returned', async () => {
    const w = await mountWith(page())
    await w.get('[data-testid="portal-decision-record-open"]').trigger('click')
    axios.post.mockResolvedValueOnce({ data: { decision: decision({ id: 'r', status: 'routed', scope: 'direction' }), hint: 'take it to direction canon as a proposal' } })
    axios.get.mockResolvedValueOnce({ data: page({ decisions: [decision({ id: 'r', status: 'routed', scope: 'direction' })] }) })
    await w.get('[data-testid="portal-decision-form"]').trigger('submit')
    await flushPromises()
    expect(w.get('[data-testid="portal-decision-hint"]').text()).toContain('direction canon')
  })

  it('close goes through a confirm and posts the action; reverse asks for the reason inline', async () => {
    const w = await mountWith(page())
    await w.get('[data-testid="portal-decision-close-d1"]').trigger('click')
    await flushPromises()
    expect(document.body.textContent).toContain('Close this decision?')
    expect(axios.post).not.toHaveBeenCalled()
    axios.post.mockResolvedValueOnce({ data: { decision: decision({ status: 'closed' }), hint: null } })
    axios.get.mockResolvedValueOnce({ data: page({ decisions: [decision({ status: 'closed' })] }) })
    const confirmBtn = [...document.querySelectorAll('button')].find((b) => b.textContent.trim() === 'Close' && b.closest('[role="dialog"], .fixed'))
    confirmBtn.click()
    await flushPromises()
    expect(axios.post).toHaveBeenCalledWith(`${URL}/d1/actions`, { action: 'close' }, expect.anything())
    expect(w.get('[data-testid="portal-decisions-history"]').text()).toContain('History · 1')

    setActivePinia(createPinia()); document.body.innerHTML = ''; vi.clearAllMocks()
    const w2 = await mountWith(page())
    await w2.get('[data-testid="portal-decision-reverse-d1"]').trigger('click')
    await w2.get('[data-testid="portal-decision-reason"]').setValue('Acme raised the price')
    axios.post.mockResolvedValueOnce({ data: { decision: decision({ status: 'reversed' }), hint: null } })
    axios.get.mockResolvedValueOnce({ data: page({ decisions: [decision({ status: 'reversed', close_reason: 'Acme raised the price' })] }) })
    await w2.get('[data-testid="portal-decision-inline-reverse"]').trigger('submit')
    await flushPromises()
    expect(axios.post).toHaveBeenCalledWith(`${URL}/d1/actions`, { action: 'reverse', reason: 'Acme raised the price' }, expect.anything())
  })

  it('correct opens the form pre-filled and posts a supersede', async () => {
    const w = await mountWith(page())
    await w.get('[data-testid="portal-decision-correct-d1"]').trigger('click')
    const form = w.get('[data-testid="portal-decision-form"]')
    expect(form.findAll('[data-testid="portal-decision-alternative"]')).toHaveLength(2)
    axios.post.mockResolvedValueOnce({ data: { decision: decision({ id: 'd2', supersedes_id: 'd1' }), hint: null } })
    axios.get.mockResolvedValueOnce({ data: page({ decisions: [decision({ id: 'd2', supersedes_id: 'd1' }), decision({ status: 'superseded' })] }) })
    await form.trigger('submit')
    await flushPromises()
    expect(axios.post).toHaveBeenCalledWith(`${URL}/d1/actions`, expect.objectContaining({ action: 'supersede', fields: expect.objectContaining({ decided: 'Renew the Acme contract' }) }), expect.anything())
  })
})

describe('clientPortal store — decisions slice', () => {
  it('a response that lands after an agent switch is discarded', async () => {
    const store = useClientPortalStore()
    let resolveA
    axios.get.mockImplementationOnce(() => new Promise((r) => { resolveA = r }))
    const a = store.loadAgentDecisions('agent-a')
    axios.get.mockResolvedValueOnce({ data: page({ agent_name: 'agent-b' }) })
    await store.loadAgentDecisions('agent-b')
    resolveA({ data: page({ agent_name: 'agent-a' }) })
    await a
    expect(store.decisionsAgent).toBe('agent-b')
    expect(store.decisions.agent_name).toBe('agent-b')
  })
})
