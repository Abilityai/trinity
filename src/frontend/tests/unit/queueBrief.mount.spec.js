// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A2, §3g L6 — the E2 render seam: an ask's
 * agent-authored brief (`ask.brief = {why, recommendation, if_no_answer,
 * impact}`), rendered by `components/operator/QueueBrief.vue` (the QueueProposal
 * pattern). L8 supplies the data and mounts it on the operator's cards; here it
 * is proven on FIXTURE asks inside the Workspace card.
 *
 * Order (reconcile row 15): body → proposal → Why now → "{agent} recommends" →
 * options → "If you don't answer by {time}" beside the answer row. With an
 * `impact` for an offered option, the options become full-width stacked buttons
 * (label, then a 12.5px hint joined by aria-describedby). Every section renders
 * only when present, and only through PortalMarkdown (DOMPurify) — the brief is
 * agent text shown to a client.
 *
 * @source-text-pin: the last block pins an ABSENCE — no v-html in QueueBrief —
 * which no render can show for a payload a test did not think of.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { readFileSync, existsSync } from 'node:fs'
import { resolve } from 'node:path'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import { useClientPortalStore } from '@/stores/clientPortal'
import PortalAsks from '@/components/portal/PortalAsks.vue'
import { briefOf, briefImpactFor } from '@/utils/operatorQueue'

const BRIEF = {
  why: 'The **invoice** is due Friday.',
  recommendation: 'Approve — it matches the PO.',
  if_no_answer: 'I will hold the payment.',
  impact: { approve: 'Pays $4,120 today', reject: 'Vendor is told no', bogus: 'not an option' },
}
const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', priority: 'high',
  title: 'Pay the vendor?', question: 'Release the September payment?', options: ['approve', 'reject'],
  proposal: { action: 'pay', amount: '4120' },
  created_at: '2026-09-28T10:00:00Z', expires_at: '2026-10-01T15:00:00Z', status: 'pending',
  chat_id: null, sync: 'confirmed', aging: false, ended_at: null, ended_by: null, brief: BRIEF, ...over,
})

let store, wrapper
beforeEach(() => {
  setActivePinia(createPinia())
  store = useClientPortalStore()
})
afterEach(() => { wrapper?.unmount(); wrapper = null; delete window.__briefXss })

function mountAsks(asks) {
  store.asks = asks
  store.asksAvailable = true
  wrapper = mount(PortalAsks, { props: { agentName: 'scout' }, attachTo: document.body })
  return wrapper
}
const el = (w, id) => w.find(`[data-testid="${id}"]`)
const before = (a, b) => !!(a.compareDocumentPosition(b) & Node.DOCUMENT_POSITION_FOLLOWING)

describe('briefOf / briefImpactFor — what of a brief is shown', () => {
  it('keeps the non-empty strings; nothing at all is null', () => {
    expect(briefOf({ brief: { why: '  ', recommendation: 3, if_no_answer: 'x' } })).toEqual({ if_no_answer: 'x' })
    expect(briefOf({ brief: { why: '' } })).toBeNull()
    expect(briefOf({})).toBeNull()
    expect(briefOf({ brief: 'not an object' })).toBeNull()
  })
  it('impact only for an OFFERED option of an approval, strings only', () => {
    expect(briefImpactFor(ask('a'))).toEqual({ approve: 'Pays $4,120 today', reject: 'Vendor is told no' })
    expect(briefImpactFor(ask('q', { kind: 'question' }))).toEqual({})
    expect(briefImpactFor(ask('b', { brief: { impact: { approve: { nested: 1 } } } }))).toEqual({})
  })
})

describe('QueueBrief inside the ask card (mounted)', () => {
  it('renders in the decided order around the proposal and the options', () => {
    const w = mountAsks([ask('a1')])
    const q = el(w, 'portal-ask-question-a1').element
    const proposal = el(w, 'queue-proposal').element
    const why = el(w, 'queue-brief-why').element
    const rec = el(w, 'queue-brief-recommendation').element
    const opt = w.findAll('[data-testid="portal-ask-option-a1"]')[0].element
    const fallback = el(w, 'queue-brief-if-no-answer').element
    expect(before(q, proposal) && before(proposal, why) && before(why, rec) && before(rec, opt) && before(opt, fallback)).toBe(true)
    expect(el(w, 'queue-brief-why').text()).toContain('Why now')
    expect(el(w, 'queue-brief-recommendation').text()).toContain('scout recommends')
    expect(el(w, 'queue-brief-if-no-answer').text()).toMatch(/^If you don't answer by /)
    // Markdown, rendered — not its syntax.
    expect(w.find('[data-testid="queue-brief-why"] strong').text()).toBe('invoice')
  })

  it('with no expiry it says plainly "If you don\'t answer"', () => {
    const w = mountAsks([ask('a1', { expires_at: null })])
    expect(el(w, 'queue-brief-if-no-answer').text()).toMatch(/^If you don't answer\s*I will hold/)
  })

  it('an option with an impact is a stacked button whose hint describes it', () => {
    const w = mountAsks([ask('a1')])
    const [approve] = w.findAll('[data-testid="portal-ask-option-a1"]')
    const hintId = approve.attributes('aria-describedby')
    expect(hintId).toBeTruthy()
    expect(document.getElementById(hintId).textContent).toBe('Pays $4,120 today')
    expect(approve.classes()).toContain('w-full')
    expect(w.text()).not.toContain('not an option')
  })

  it('no impact → the options stay chips; no brief → no brief block at all', () => {
    const w = mountAsks([ask('a1', { brief: null })])
    const [approve] = w.findAll('[data-testid="portal-ask-option-a1"]')
    expect(approve.attributes('aria-describedby')).toBeUndefined()
    expect(approve.classes()).not.toContain('w-full')
    expect(w.find('[data-testid^="queue-brief"]').exists()).toBe(false)
  })

  it('each section only when present; an ended ask keeps why/recommendation, drops the fallback', () => {
    const w = mountAsks([ask('a1', { brief: { recommendation: 'Approve' } }),
      ask('e1', { status: 'answered', ended_by: 'you', ended_at: '2026-09-28T11:00:00Z' })])
    expect(w.find('[data-testid="portal-ask-a1"] [data-testid="queue-brief-why"]').exists()).toBe(false)
    expect(w.find('[data-testid="portal-ask-a1"] [data-testid="queue-brief-recommendation"]').exists()).toBe(true)
    expect(w.find('[data-testid="portal-ask-e1"] [data-testid="queue-brief-why"]').exists()).toBe(true)
    expect(w.find('[data-testid="portal-ask-e1"] [data-testid="queue-brief-if-no-answer"]').exists()).toBe(false)
  })

  it('agent text cannot run script: <img onerror> is sanitised away', () => {
    const w = mountAsks([ask('a1', { brief: { why: '<img src=x onerror="window.__briefXss=1">hi' } })])
    const img = w.find('[data-testid="queue-brief-why"] img')
    if (img.exists()) expect(img.attributes('onerror')).toBeUndefined()
    expect(window.__briefXss).toBeUndefined()
  })
})

describe('QueueBrief is not a second markdown renderer (source guard)', () => {
  const file = resolve(process.cwd(), 'src/components/operator/QueueBrief.vue')
  it('renders agent text only through PortalMarkdown', () => {
    expect(existsSync(file)).toBe(true)
    const sfc = readFileSync(file, 'utf8')
    expect(sfc).not.toMatch(/v-html/)
    expect(sfc).toMatch(/<PortalMarkdown/)
  })
})
