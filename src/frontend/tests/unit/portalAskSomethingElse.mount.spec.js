// @vitest-environment jsdom
/**
 * #3242 — the Workspace asks panel's "Something else": typing with no pick arms
 * it, Send carries the reserved literal with the instruction, Enter never sends
 * an auto-armed state, and an approval the projection marks
 * `decided_by_options` (a gate) offers no chip (T8 ruling).
 *
 * The store's `answerAsk` is spied: it maps `{response, responseText}` onto the
 * POST body one-to-one (stores/clientPortal.js), so the call IS the body.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
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

const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'approval', priority: 'medium',
  title: `Ask ${id}`, question: `Ask ${id}`, options: ['Yes', 'No'],
  created_at: '2026-10-05T10:00:00Z', expires_at: null, status: 'pending',
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
afterEach(() => { wrapper?.unmount(); wrapper = null })

function mountAsks(asks) {
  store.asks = asks
  store.asksAvailable = true
  wrapper = mount(PortalAsks, { props: { agentName: 'scout' }, attachTo: document.body })
  return wrapper
}

describe('PortalAsks — Something else (#3242)', () => {
  it('typing arms it and Send carries the literal with the instruction', async () => {
    const answer = vi.spyOn(store, 'answerAsk').mockResolvedValue({})
    const w = mountAsks([ask('a1')])
    const send = () => tid(w, 'portal-ask-send-a1')
    expect(send().attributes('disabled')).toBeDefined()
    await tid(w, 'portal-ask-note-a1').setValue('Ship to staging first')
    expect(tid(w, 'portal-ask-something-else-a1').attributes('aria-pressed')).toBe('true')
    expect(send().attributes('aria-label')).toBe('Send instruction')
    expect(send().attributes('disabled')).toBeUndefined()
    await send().element.form.dispatchEvent(new Event('submit'))
    await flushPromises()
    expect(answer).toHaveBeenCalledWith('a1', expect.objectContaining({
      response: '(something else)', responseText: 'Ship to staging first',
    }))
  })

  it('the chip alone does not send — the instruction is required', async () => {
    const answer = vi.spyOn(store, 'answerAsk').mockResolvedValue({})
    const w = mountAsks([ask('a2')])
    await tid(w, 'portal-ask-something-else-a2').trigger('click')
    expect(tid(w, 'portal-ask-send-a2').attributes('disabled')).toBeDefined()
    await tid(w, 'portal-ask-send-a2').element.form.dispatchEvent(new Event('submit'))
    await flushPromises()
    expect(answer).not.toHaveBeenCalled()
  })

  it('Enter in the note is blocked while the state is only auto-armed', async () => {
    const w = mountAsks([ask('a3')])
    await tid(w, 'portal-ask-note-a3').setValue('Ship to staging first')
    const ev = new KeyboardEvent('keydown', { key: 'Enter', cancelable: true, bubbles: true })
    tid(w, 'portal-ask-note-a3').element.dispatchEvent(ev)
    expect(ev.defaultPrevented).toBe(true)
    // After an explicit pick Enter is the send path again (B2).
    await tid(w, 'portal-ask-option-a3').trigger('click')
    const ev2 = new KeyboardEvent('keydown', { key: 'Enter', cancelable: true, bubbles: true })
    tid(w, 'portal-ask-note-a3').element.dispatchEvent(ev2)
    expect(ev2.defaultPrevented).toBe(false)
  })

  it('a picked option keeps the note a note', async () => {
    const answer = vi.spyOn(store, 'answerAsk').mockResolvedValue({})
    const w = mountAsks([ask('a4')])
    await tid(w, 'portal-ask-option-a4').trigger('click')
    await tid(w, 'portal-ask-note-a4').setValue('ok')
    await tid(w, 'portal-ask-send-a4').element.form.dispatchEvent(new Event('submit'))
    await flushPromises()
    expect(answer).toHaveBeenCalledWith('a4', expect.objectContaining({ response: 'Yes', responseText: 'ok' }))
  })

  it('an approval decided by its options has no chip, and typing never arms Send', async () => {
    const w = mountAsks([ask('a5', { decided_by_options: true })])
    expect(tid(w, 'portal-ask-something-else-a5').exists()).toBe(false)
    await tid(w, 'portal-ask-note-a5').setValue('anything')
    expect(tid(w, 'portal-ask-send-a5').attributes('disabled')).toBeDefined()
  })
})
