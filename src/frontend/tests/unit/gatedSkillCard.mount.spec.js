// @vitest-environment jsdom
/**
 * trinity-enterprise#755 — the gated-skill approval card, MOUNTED (#2918).
 *
 * One component (`GatedSkillRequest`) drawn by the Workspace card (`PortalAsks`:
 * the Inbox pane and the chat card) and by Operations (`QueueCard`). It shows
 * which agent would run which skill with what input on whose request, who it is
 * waiting on, and how the request ended. Only a person the approval is
 * addressed to gets the controls, and the note beside a picked Reject is the
 * reason the requester is told.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises, RouterLinkStub } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { nextTick } from 'vue'

// The viewer's platform role, settable per test (#3708). An operator by default,
// so the assertions below describe the operator's card unless a test says otherwise.
const auth = vi.hoisted(() => ({ user: { role: 'operator' } }))
vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn(), user: auth.user }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return { default: Object.assign(mk(), { create: mk }) }
})
vi.mock('@/api', () => ({ default: { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn() } }))

import axios from 'axios'
import GatedSkillRequest from '@/components/operator/GatedSkillRequest.vue'
import QueueCard from '@/components/operator/QueueCard.vue'
import PortalAsks from '@/components/portal/PortalAsks.vue'
import { useOperatorQueueStore } from '@/stores/operatorQueue'
import { useClientPortalStore } from '@/stores/clientPortal'

const gate = (over = {}) => ({
  skills: [{ name: 'pay-invoice', description: 'Pays one supplier invoice.' }],
  requester: { kind: 'agent', label: 'Agent marketing', agent: 'marketing', execution_id: null },
  state: 'pending', execution_id: null, waiting_on: ['Dana Reyes'], can_decide: true, ...over,
})
const row = (over = {}) => ({
  id: 'g1', agent_name: 'finance', request_id: 'gate-abc', type: 'approval', status: 'pending',
  priority: 'high', title: 'Approve the skill pay-invoice on finance', question: 'Agent marketing asked.',
  options: ['Approve', 'Reject'], decided_by_options: true, context: {},
  created_at: '2026-10-12T08:00:00Z', expires_at: '2026-10-13T08:00:00Z',
  proposal: { kind: 'gated_skill', agent: 'finance', skills: ['pay-invoice'], input: '/pay-invoice INV-8 100 EUR',
              requester: 'Agent marketing', fingerprints: { 'pay-invoice': 'abc' } },
  gate: gate(), ...over,
})
const tid = (w, id) => w.find(`[data-testid="${id}"]`)
const stubs = { RouterLink: RouterLinkStub, AgentAvatar: true }

beforeEach(() => {
  auth.user = { role: 'operator' }
  localStorage.clear()
  sessionStorage.clear()
  vi.clearAllMocks()
})

describe('the card itself', () => {
  const card = (over = {}) => mount(GatedSkillRequest, { props: { item: row(over) }, global: { stubs } })

  it('names the agent, the skill with its description, the input, the requester and the deadline', () => {
    const w = card()
    expect(tid(w, 'gate-card-agent').text()).toBe('finance')
    expect(tid(w, 'gate-card-skill').text()).toBe('pay-invoice — Pays one supplier invoice.')
    expect(tid(w, 'gate-card-input').text()).toBe('/pay-invoice INV-8 100 EUR')
    expect(tid(w, 'gate-card-requester').text()).toBe('Agent marketing')
    expect(tid(w, 'gate-card-raised').exists()).toBe(true)
    expect(tid(w, 'gate-card-deadline').text()).not.toBe('')
    expect(tid(w, 'gate-card-input-toggle').exists()).toBe(false)   // a short input is not folded
    expect(tid(w, 'gate-card-outcome').exists()).toBe(false)
    expect(w.text()).not.toContain('fingerprints')
  })

  it('shows the context the agent was also sent, and the one-line rule only to the decider', () => {
    expect(tid(card(), 'gate-card-also-sent').exists()).toBe(false)
    expect(tid(card(), 'gate-card-hint').text()).toContain('Reject runs nothing')
    const w = card({ gate: gate({ also_sent: 'Schedule: /pay-invoice INV-8', can_decide: false }) })
    expect(tid(w, 'gate-card-also-sent').text()).toBe('Schedule: /pay-invoice INV-8')
    expect(tid(w, 'gate-card-hint').exists()).toBe(false)
  })

  it('links the requesting agent run when the platform gives its id', () => {
    const w = card({ gate: gate({ requester: { kind: 'agent', label: 'Agent marketing', agent: 'marketing', execution_id: 'run-1' } }) })
    expect(tid(w, 'gate-card-requester-run').findComponent(RouterLinkStub).props('to'))
      .toBe('/agents/marketing/executions/run-1')
  })

  // trinity#3708: an execution page is an operator route; a Workspace-only member
  // is sent back to the Workspace, so neither run link is drawn for them.
  it.each([
    ['operator', true],
    ['admin', true],
    ['user', false],     // Workspace-only
    [null, false],       // an external client: no platform account
  ])('draws the run links for a %s viewer: %s', (role, shown) => {
    auth.user = role ? { role } : null
    const requested = card({ gate: gate({ requester: { kind: 'agent', label: 'Agent marketing', agent: 'marketing', execution_id: 'run-1' } }) })
    expect(tid(requested, 'gate-card-requester').text()).toContain('Agent marketing')   // the label stays
    expect(tid(requested, 'gate-card-requester-run').exists()).toBe(shown)
    const ended = card({ status: 'responded', gate: gate({ state: 'succeeded', execution_id: 'run-9' }) })
    expect(tid(ended, 'gate-card-outcome').text()).toContain('Ran')                      // the outcome stays
    expect(tid(ended, 'gate-card-run').exists()).toBe(shown)
  })

  it('folds a long input and opens it whole', async () => {
    const long = Array.from({ length: 40 }, (_, i) => `line ${i}`).join('\n')
    const w = card({ proposal: { kind: 'gated_skill', input: long } })
    expect(tid(w, 'gate-card-input').text()).toBe(long)            // never cut, only folded
    expect(tid(w, 'gate-card-input').classes()).toContain('max-h-24')
    await tid(w, 'gate-card-input-toggle').trigger('click')
    expect(tid(w, 'gate-card-input').classes()).not.toContain('max-h-24')
    expect(tid(w, 'gate-card-input-toggle').attributes('aria-expanded')).toBe('true')
  })

  it('renders requester-authored input as text, never as markup', () => {
    const w = card({ proposal: { kind: 'gated_skill', input: '<img src=x onerror="alert(1)">' } })
    expect(tid(w, 'gate-card-input').find('img').exists()).toBe(false)
  })

  it('says who it is waiting on only to someone who cannot decide it', () => {
    expect(tid(card(), 'gate-card-waiting-on').exists()).toBe(false)
    const w = card({ gate: gate({ can_decide: false, waiting_on: ['Dana Reyes', 'sam@example.com'] }) })
    expect(tid(w, 'gate-card-waiting-on').text()).toBe('Dana Reyes, sam@example.com')
  })

  it('with no names to show, says which role it waits on — never a person (trinity-enterprise#848)', () => {
    const w = card({ gate: gate({ can_decide: false, waiting_on: [],
      waiting_role: { value: 'role:finance', label: 'Finance' } }) })
    expect(tid(w, 'gate-card-waiting-on').text()).toBe('Finance')
    const named = card({ gate: gate({ can_decide: false, waiting_on: ['Dana Reyes'],
      waiting_role: { value: 'role:finance', label: 'Finance' } }) })
    expect(tid(named, 'gate-card-waiting-on').text()).toBe('Dana Reyes')
    expect(tid(card({ gate: gate({ waiting_role: { value: 'role:finance', label: 'Finance' } }) }),
      'gate-card-waiting-on').exists()).toBe(false)                // the decider is not told
    expect(tid(card({ gate: gate({ can_decide: false, waiting_on: [],
      waiting_role: { value: 'primary', label: 'primary' } }) }), 'gate-card-waiting-on').text()).toBe('Primary contact')
  })

  it.each([
    ['dispatched', 'Approved', true],
    ['succeeded', 'Ran', true],
    ['failed', 'Ran', true],
    ['denied', 'Rejected', false],
    ['cancelled', 'Cancelled', false],
    ['stale', 'Stale', false],
  ])('an ended request says %s as %s', (state, label, ran) => {
    const w = card({ status: 'responded', gate: gate({ state, execution_id: 'run-9' }) })
    const out = tid(w, 'gate-card-outcome')
    expect(out.attributes('data-state')).toBe(state)
    expect(out.text()).toContain(label)
    expect(tid(w, 'gate-card-run').exists()).toBe(ran)
    if (ran) expect(tid(w, 'gate-card-run').findComponent(RouterLinkStub).props('to')).toBe('/agents/finance/executions/run-9')
    expect(tid(w, 'gate-card-waiting-on').exists()).toBe(false)
  })

  it('an expiry reads as a rejection, also before the record caught up with the ask', () => {
    for (const over of [{ gate: gate({ state: 'expired' }) }, { gate: gate({ state: 'pending' }) }]) {
      const out = tid(card({ status: 'expired', ...over }), 'gate-card-outcome')
      expect(out.attributes('data-state')).toBe('expired')
      expect(out.text()).toContain('counts as a rejection: nothing was run')
    }
  })
})

describe('Operations (QueueCard)', () => {
  async function mountCard(over = {}) {
    const pinia = createPinia()
    setActivePinia(pinia)
    const store = useOperatorQueueStore()
    store.items = [row(over)]
    store.expandedItemId = 'g1'
    const w = mount(QueueCard, { props: { item: store.items[0] }, global: { plugins: [pinia], stubs } })
    await nextTick()
    return w
  }

  it('draws the gate card in place of the raw proposal dump', async () => {
    const w = await mountCard()
    expect(tid(w, 'gate-card').exists()).toBe(true)
    expect(tid(w, 'queue-proposal').exists()).toBe(false)
    expect(tid(w, 'queue-card-question').exists()).toBe(false)   // the card says it once
  })

  it('someone the approval is not addressed to gets no controls, and reads who it waits on', async () => {
    const w = await mountCard({ gate: gate({ can_decide: false }) })
    expect(tid(w, 'approval-send').exists()).toBe(false)
    expect(tid(w, 'approval-note').exists()).toBe(false)
    expect(tid(w, 'gate-card-waiting-on').text()).toBe('Dana Reyes')
  })

  it('the addressee rejects with a reason, sent as the note', async () => {
    axios.post.mockResolvedValue({ data: {} })
    const w = await mountCard()
    expect(tid(w, 'approval-note').attributes('aria-label')).toBe('Note')
    const reject = w.findAll('button').find((b) => b.text() === 'Reject')
    await reject.trigger('click')
    expect(tid(w, 'approval-note').attributes('aria-label')).toBe('Reason')
    await tid(w, 'approval-note').setValue('Wrong supplier')
    await tid(w, 'approval-send').trigger('click')
    await flushPromises()
    expect(axios.post.mock.calls[0][1]).toEqual({ response: 'Reject', response_text: 'Wrong supplier' })
  })

  it('an ordinary approval keeps its proposal block and its controls', async () => {
    const w = await mountCard({ gate: null, proposal: { amount: 5 }, decided_by_options: false })
    expect(tid(w, 'gate-card').exists()).toBe(false)
    expect(tid(w, 'queue-card-question').exists()).toBe(true)
    expect(tid(w, 'queue-proposal').exists()).toBe(true)
    expect(tid(w, 'approval-send').exists()).toBe(true)
  })
})

describe('the Workspace (PortalAsks — the Inbox pane and the chat card)', () => {
  const ask = (over = {}) => {
    const { type, ...rest } = row(over)
    return { ...rest, kind: type, chat_id: null, sync: 'confirmed', aging: false, ended_at: null, ended_by: null }
  }
  function mountAsks(list) {
    setActivePinia(createPinia())
    const store = useClientPortalStore()
    store.asks = list
    store.asksAvailable = true
    return mount(PortalAsks, { props: { agentName: 'finance' }, global: { stubs } })
  }

  it('draws the same card, with Approve and Reject', () => {
    const w = mountAsks([ask()])
    expect(tid(w, 'gate-card').exists()).toBe(true)
    expect(tid(w, 'queue-proposal').exists()).toBe(false)
    expect(tid(w, 'portal-ask-question-g1').exists()).toBe(false)   // the card says it once
    expect(w.findAll('[data-testid="portal-ask-option-g1"]').map((b) => b.text())).toEqual(['Approve', 'Reject'])
    expect(tid(w, 'portal-ask-something-else-g1').exists()).toBe(false)
  })

  it('the note is the reason from the start, and Reject sends it in one click (trinity#3527)', async () => {
    // A Workspace gate card sends on Approve / Reject, so there is no pick to
    // relabel the field after: it reads as the reason before anything is chosen.
    const w = mountAsks([ask()])
    const store = useClientPortalStore()
    const answer = vi.spyOn(store, 'answerAsk').mockResolvedValue({})
    expect(tid(w, 'portal-ask-note-g1').attributes('aria-label')).toBe('Reason')
    expect(tid(w, 'portal-ask-note-g1').attributes('placeholder')).toContain('if you reject')
    await tid(w, 'portal-ask-note-g1').setValue('Wrong supplier')
    await w.findAll('[data-testid="portal-ask-option-g1"]')[1].trigger('click')
    await flushPromises()
    expect(answer).toHaveBeenCalledWith('g1', expect.objectContaining({ response: 'Reject', responseText: 'Wrong supplier' }))
  })

  it('with a Request row shown, the full input is never folded (PR #3648 review)', () => {
    const long = Array.from({ length: 60 }, (_, i) => `line ${i} of the earlier conversation`).join('\n')
    const folded = mountAsks([ask({ proposal: { ...row().proposal, input: long } })])
    expect(tid(folded, 'gate-card-input-toggle').exists()).toBe(true)
    expect(tid(folded, 'gate-card-input').classes()).toContain('max-h-24')
    const shown = mountAsks([ask({ proposal: { ...row().proposal, input: long }, gate: gate({ request: '/pay-invoice 1 EUR' }) })])
    expect(tid(shown, 'gate-card-input-toggle').exists()).toBe(false)
    expect(tid(shown, 'gate-card-input').classes()).not.toContain('max-h-24')
  })

  it('a history-carrying request leads with the typed message above the full input (trinity#3527)', () => {
    const w = mountAsks([ask({ gate: gate({ request: '/pay-invoice 100 EUR' }) })])
    expect(tid(w, 'gate-card-request').text()).toBe('/pay-invoice 100 EUR')
    expect(tid(w, 'gate-card-history-note').exists()).toBe(true)
    const plain = mountAsks([ask()])
    expect(tid(plain, 'gate-card-request').exists()).toBe(false)
  })

  it('a Workspace-only member sees who asked, with no run link (trinity#3708)', () => {
    auth.user = { role: 'user' }
    const w = mountAsks([ask({ gate: gate({ requester: { kind: 'agent', label: 'Agent marketing', agent: 'marketing', execution_id: 'run-1' } }) })])
    expect(tid(w, 'gate-card-requester').text()).toContain('Agent marketing')
    expect(tid(w, 'gate-card-requester-run').exists()).toBe(false)
  })

  it('an approved request keeps its card and says it ran', () => {
    const w = mountAsks([ask({ status: 'answered', ended_by: 'you', gate: gate({ state: 'succeeded', execution_id: 'run-9' }) })])
    expect(tid(w, 'gate-card-outcome').text()).toContain('The run finished successfully.')
    expect(tid(w, 'gate-card-run').exists()).toBe(true)
    expect(tid(w, 'portal-ask-send-g1').exists()).toBe(false)
  })
})
