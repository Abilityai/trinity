// @vitest-environment jsdom
/**
 * trinity-enterprise#610 PR A2, §3g L6 B2 / B3 / B5 / after-Send — how an ask
 * card behaves while a person answers it.
 *
 *   B2  Picking an approval option moved nothing: focus stayed on the chip (a
 *       `type=button`), so the Enter a person pressed next re-clicked it and
 *       UNSELECTED the option. A pick now moves focus to the note field (not on
 *       a coarse pointer, where it would pop a keyboard), and Enter there sends.
 *       Still two steps (#2375): the pick only arms Send.
 *   B3  A question the agent offered options for showed a bare text box. The
 *       options now render as quick picks that FILL the answer and never send.
 *   B5  "Unconfirmed" is the platform's own bookkeeping (no confirming poll has
 *       read the row back yet); to a Workspace reader it says nothing they can
 *       act on. Hidden here; every other state still shows.
 *   Send  The controls unmount when the ask ends, and focus fell to <body>. It
 *       now lands on the answered card.
 *
 * Fixtures pin NON-ambient values: `sync: 'unconfirmed'` (the default fixture is
 * `confirmed`, which shows no badge either way) and a question WITH options.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { nextTick } from 'vue'
import { setActivePinia, createPinia } from 'pinia'

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
import { workspaceAskBadge, questionQuickPicks, QUICK_PICK_MAX } from '@/utils/operatorQueue'

const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', priority: 'medium',
  title: `Ask ${id}`, question: `Ask ${id}`, options: ['Yes', 'No'],
  created_at: '2026-09-20T10:00:00Z', expires_at: null, status: 'pending',
  chat_id: null, sync: 'confirmed', aging: false, ended_at: null, ended_by: null, ...over,
})

const tid = (w, id) => w.find(`[data-testid="${id}"]`)
let store
let wrapper

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  store = useClientPortalStore()
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  delete window.matchMedia
})

function mountAsks(asks, props = {}) {
  store.asks = asks
  store.asksAvailable = true
  wrapper = mount(PortalAsks, { props: { agentName: 'scout', ...props }, attachTo: document.body })
  return wrapper
}

describe('B5 — workspaceAskBadge', () => {
  it('hides "Unconfirmed" and keeps every state a reader can act on', () => {
    expect(workspaceAskBadge({ sync: 'unconfirmed' })).toBeNull()
    expect(workspaceAskBadge({ sync: 'changed' })?.label).toBe('Changed by the agent')
    expect(workspaceAskBadge({ sync: 'closed' })).not.toBeNull()
    expect(workspaceAskBadge({ sync: 'confirmed', aging: true })?.label).toBe('Waiting')
    // An unconfirmed row that is also aging still says it is waiting.
    expect(workspaceAskBadge({ sync: 'unconfirmed', aging: true })?.label).toBe('Waiting')
  })

  it('a pending unconfirmed ask shows no badge; a changed one does', () => {
    const w = mountAsks([ask('u1', { sync: 'unconfirmed' }), ask('c1', { sync: 'changed' })])
    expect(w.find('[data-testid="portal-ask-u1"] [data-testid="queue-sync-badge"]').exists()).toBe(false)
    expect(w.find('[data-testid="portal-ask-c1"] [data-testid="queue-sync-badge"]').text()).toContain('Changed')
  })
})

describe('B3 — a question keeps the options the agent offered', () => {
  it('questionQuickPicks: a question\'s options; nothing for approvals or alerts; over-long ones dropped', () => {
    const long = 'x'.repeat(QUICK_PICK_MAX + 1)
    expect(questionQuickPicks({ type: 'question', options: ['A', 'B', long] })).toEqual(['A', 'B'])
    expect(questionQuickPicks({ type: 'approval', options: ['A'] })).toEqual([])
    expect(questionQuickPicks({ type: 'alert', options: ['A'] })).toEqual([])
    expect(QUICK_PICK_MAX).toBe(500)
  })

  it('a pick fills the answer and focuses it — it never sends', async () => {
    const answer = vi.spyOn(store, 'answerAsk').mockResolvedValue({})
    const w = mountAsks([ask('q1', { kind: 'question', options: ['Monday', 'Friday'] })])
    const picks = w.findAll('[data-testid="portal-ask-pick-q1"]')
    expect(picks.map((b) => b.text())).toEqual(['Monday', 'Friday'])
    await picks[1].trigger('click')
    await nextTick()
    const input = tid(w, 'portal-ask-input-q1')
    expect(input.element.value).toBe('Friday')
    expect(document.activeElement).toBe(input.element)
    expect(answer).not.toHaveBeenCalled()
  })
})

describe('B2 — a pick moves to the note; Enter sends', () => {
  it('picking an option focuses the note field', async () => {
    const w = mountAsks([ask('ap1')])
    await tid(w, 'portal-ask-option-ap1').trigger('click')
    await nextTick()
    expect(document.activeElement).toBe(tid(w, 'portal-ask-note-ap1').element)
  })

  it('an un-pick leaves focus where it is', async () => {
    const w = mountAsks([ask('ap1')])
    const chip = tid(w, 'portal-ask-option-ap1')
    await chip.trigger('click')
    await nextTick()
    chip.element.focus()
    await chip.trigger('click')
    await nextTick()
    expect(document.activeElement).toBe(chip.element)
  })

  it('on a coarse pointer the keyboard is not popped', async () => {
    window.matchMedia = (q) => ({ matches: q.includes('coarse'), media: q, addEventListener() {}, removeEventListener() {} })
    const w = mountAsks([ask('ap1')])
    const chip = tid(w, 'portal-ask-option-ap1')
    chip.element.focus()
    await chip.trigger('click')
    await nextTick()
    expect(document.activeElement).not.toBe(tid(w, 'portal-ask-note-ap1').element)
  })

  // jsdom has no implicit submission (Enter in a field → the form's submit), so
  // this drives the form's submit — what that Enter fires in a browser. The
  // key itself is proven in the live walk (A2 round 1, Codex C6).
  it("the note's form submit (what Enter fires there) sends the picked option", async () => {
    const answer = vi.spyOn(store, 'answerAsk').mockResolvedValue({})
    const w = mountAsks([ask('ap1')])
    await tid(w, 'portal-ask-option-ap1').trigger('click')
    await tid(w, 'portal-ask-note-ap1').setValue('ok')
    await tid(w, 'portal-ask-note-ap1').element.form.dispatchEvent(new Event('submit'))
    await flushPromises()
    expect(answer).toHaveBeenCalledWith('ap1', expect.objectContaining({ response: 'Yes', responseText: 'ok' }))
  })
})

describe('after Send — focus lands on the answered card', () => {
  it('the card that was answered takes focus once its controls are gone', async () => {
    vi.spyOn(store, 'answerAsk').mockImplementation(async (id) => {
      store.asks = store.asks.map((a) => (a.id === id ? { ...a, status: 'answered', ended_at: '2026-09-29T10:00:00Z', ended_by: 'you' } : a))
      return { status: 'answered' }
    })
    const w = mountAsks([ask('ap1')], { agentName: null, askIds: ['ap1'] })
    await tid(w, 'portal-ask-option-ap1').trigger('click')
    await tid(w, 'portal-ask-send-ap1').element.form.dispatchEvent(new Event('submit'))
    await flushPromises()
    await nextTick()
    expect(tid(w, 'portal-ask-send-ap1').exists()).toBe(false)
    expect(document.activeElement).toBe(tid(w, 'portal-ask-ap1').element)
  })
})

describe('A2 round 1 — the card while it is answered', () => {
  it('a quick pick does not pop the keyboard on a coarse pointer (like an approval pick)', async () => {
    window.matchMedia = (q) => ({ matches: q.includes('coarse'), media: q, addEventListener() {}, removeEventListener() {} })
    const w = mountAsks([ask('q1', { kind: 'question', options: ['Monday', 'Friday'] })])
    const chip = w.findAll('[data-testid="portal-ask-pick-q1"]')[0]
    chip.element.focus()
    await chip.trigger('click')
    await nextTick()
    expect(tid(w, 'portal-ask-input-q1').element.value).toBe('Monday')
    expect(document.activeElement).not.toBe(tid(w, 'portal-ask-input-q1').element)
  })

  it('the quick pick that filled the answer shows as chosen, and stops when the answer is edited', async () => {
    const w = mountAsks([ask('q1', { kind: 'question', options: ['Monday', 'Friday'] })])
    const picks = () => w.findAll('[data-testid="portal-ask-pick-q1"]')
    expect(picks().map((b) => b.attributes('aria-pressed'))).toEqual(['false', 'false'])
    await picks()[1].trigger('click')
    expect(picks().map((b) => b.attributes('aria-pressed'))).toEqual(['false', 'true'])
    expect(picks()[1].attributes('class')).not.toEqual(picks()[0].attributes('class'))
    await tid(w, 'portal-ask-input-q1').setValue('Friday, after 3pm')
    expect(picks().map((b) => b.attributes('aria-pressed'))).toEqual(['false', 'false'])
  })

  it('chips, fields, Send and the answered card carry the design-system focus ring, not the browser outline', () => {
    const w = mountAsks([ask('ap1'), ask('q1', { kind: 'question', options: ['Monday'] })])
    const ring = 'focus-visible:ring-2'
    for (const id of ['portal-ask-option-ap1', 'portal-ask-send-ap1', 'portal-ask-pick-q1', 'portal-ask-ap1']) {
      expect(tid(w, id).classes(), id).toEqual(expect.arrayContaining(['focus:outline-none', ring]))
    }
    for (const id of ['portal-ask-note-ap1', 'portal-ask-input-q1']) {
      expect(tid(w, id).classes(), id).toEqual(expect.arrayContaining(['focus:outline-none', 'focus:border-action-primary-500']))
    }
  })

  it('an idle chip and a field keep a 3:1 outline on the dark amber card (gray-500, never gray-600)', () => {
    const w = mountAsks([ask('ap1'), ask('q1', { kind: 'question', options: ['Monday'] })])
    for (const id of ['portal-ask-option-ap1', 'portal-ask-pick-q1', 'portal-ask-note-ap1', 'portal-ask-input-q1']) {
      const cls = tid(w, id).classes()
      expect(cls, id).toContain('dark:border-gray-500')
      expect(cls, id).not.toContain('dark:border-gray-600')
    }
  })

  it('a second submit while the first is in flight sends nothing (Enter is now the main path)', async () => {
    let release
    const answer = vi.spyOn(store, 'answerAsk').mockImplementation(() => new Promise((r) => { release = r }))
    const w = mountAsks([ask('ap1')])
    await tid(w, 'portal-ask-option-ap1').trigger('click')
    const form = tid(w, 'portal-ask-note-ap1').element.form
    form.dispatchEvent(new Event('submit'))
    form.dispatchEvent(new Event('submit'))
    await flushPromises()
    expect(answer).toHaveBeenCalledTimes(1)
    release({})
    await flushPromises()
  })

  it('with showUrgency the card header carries the priority and the expiry (the Inbox pane)', () => {
    const soon = new Date(Date.now() + 2 * 60 * 60 * 1000 + 60_000).toISOString()
    const a = ask('ap1', { priority: 'critical', expires_at: soon })
    const w = mountAsks([a], { agentName: null, askIds: ['ap1'], showUrgency: true })
    expect(tid(w, 'portal-ask-priority-ap1').text()).toBe('Critical')
    expect(tid(w, 'portal-ask-expiry-ap1').text()).toBe('Expires in 2h')
    w.unmount()
    const plain = mountAsks([a], { agentName: null, askIds: ['ap1'] })
    expect(tid(plain, 'portal-ask-priority-ap1').exists()).toBe(false)
    expect(tid(plain, 'portal-ask-expiry-ap1').exists()).toBe(false)
  })

  it("a stacked option reads label first: the label at the 14px label size, the consequence in secondary ink", () => {
    const w = mountAsks([ask('ap1', { brief: { impact: { Yes: 'Pays $4,120 today' } } })])
    const opt = tid(w, 'portal-ask-option-ap1')
    const label = opt.find('span.block')                  // the label (its text rendered by AskMarkdown)
    const hint = opt.find('[id^="portal-ask-impact-"]')   // the consequence
    expect(label.classes()).toEqual(expect.arrayContaining(['text-sm']))
    expect(hint.classes()).toEqual(expect.arrayContaining(['text-[12.5px]', 'text-gray-600', 'dark:text-gray-300']))
  })
})
