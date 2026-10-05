// @vitest-environment jsdom
/**
 * trinity-enterprise#747 (Discuss) and #748 (Dismiss) — the ask card's two new
 * verbs, the store actions behind them, and where a discussed ask is drawn.
 *
 *   Dismiss  one click, no confirmation. The ask reads as dismissed on every
 *            surface at once (one list), the card offers Undo, and the server is
 *            told only when the Undo window lapses — so an undone dismissal never
 *            reaches the agent. A refused dismissal puts the ask back, with why.
 *   Discuss  opens (or continues) the ask's own chat and navigates there through
 *            the existing open-thread route; it is hidden inside that chat, and
 *            that chat draws the ask as its tile.
 *
 * Every predicate gating a store write is proven by mounting the component.
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

import { useClientPortalStore, portalHttp, ASK_DISMISS_UNDO_MS } from '@/stores/clientPortal'
import PortalAsks from '@/components/portal/PortalAsks.vue'
import { chatTurnAsks, placeAsksInThread, isDiscussedIn } from '@/components/portal/portalChatAsks'
import { queueEnding, queueEndingText } from '@/utils/operatorQueue'

const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'question', priority: 'medium',
  title: `Ask ${id}`, question: `Ask ${id}`, options: null,
  created_at: '2026-09-20T10:00:00Z', expires_at: null, status: 'pending',
  chat_id: null, raised_in_turn: false, discussion_chat_id: null,
  sync: 'confirmed', aging: false, ended_at: null, ended_by: null, ...over,
})

const tid = (w, id) => w.find(`[data-testid="${id}"]`)
let store
let wrapper

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
  vi.useFakeTimers()
  store = useClientPortalStore()
  store.portalToken = 'tok'
  portalHttp.post = vi.fn()
})
afterEach(() => {
  wrapper?.unmount()
  wrapper = null
  vi.useRealTimers()
})

function mountAsks(asks, props = {}) {
  store.asks = asks
  store.asksAvailable = true
  wrapper = mount(PortalAsks, { props: { agentName: 'scout', ...props }, attachTo: document.body })
  return wrapper
}

describe('Dismiss (ent#748)', () => {
  it('one click shows the ask as dismissed everywhere, with Undo, and sends nothing yet', async () => {
    const w = mountAsks([ask('a1')])
    expect(store.askCount).toBe(1)

    await tid(w, 'portal-ask-dismiss-a1').trigger('click')

    expect(store.asks[0].status).toBe('dismissed')
    expect(store.askCount).toBe(0)
    expect(tid(w, 'portal-ask-undo-a1').exists()).toBe(true)
    expect(tid(w, 'portal-ask-dismiss-a1').exists()).toBe(false)
    expect(portalHttp.post).not.toHaveBeenCalled()
  })

  it('Undo inside the window restores the ask and never reaches the server', async () => {
    const w = mountAsks([ask('a1')])
    await tid(w, 'portal-ask-dismiss-a1').trigger('click')

    await tid(w, 'portal-ask-undo-a1').trigger('click')
    vi.advanceTimersByTime(ASK_DISMISS_UNDO_MS * 2)
    await flushPromises()

    expect(store.asks[0].status).toBe('pending')
    expect(store.askCount).toBe(1)
    expect(portalHttp.post).not.toHaveBeenCalled()
    expect(tid(w, 'portal-ask-dismiss-a1').exists()).toBe(true)
  })

  it('when the window lapses the dismissal is sent once and the server projection lands', async () => {
    portalHttp.post.mockResolvedValue({ data: ask('a1', { status: 'dismissed', ended_by: 'you', ended_at: '2026-10-02T10:00:00Z' }) })
    const w = mountAsks([ask('a1')])
    await tid(w, 'portal-ask-dismiss-a1').trigger('click')

    vi.advanceTimersByTime(ASK_DISMISS_UNDO_MS)
    await flushPromises()

    expect(portalHttp.post).toHaveBeenCalledTimes(1)
    expect(portalHttp.post.mock.calls[0][0]).toBe('/api/enterprise/client-portal/asks/a1/dismiss')
    expect(store.asks[0].ended_at).toBe('2026-10-02T10:00:00Z')
    expect(store.askDismissals).toEqual({})
    expect(tid(w, 'portal-ask-undo-a1').exists()).toBe(false)
  })

  it('a refused dismissal puts the ask back and says why beside it', async () => {
    portalHttp.post.mockRejectedValue(Object.assign(new Error('x'), {
      response: { status: 503, data: { detail: { message: 'Try again in a moment.' } } },
    }))
    const w = mountAsks([ask('a1')])
    await tid(w, 'portal-ask-dismiss-a1').trigger('click')

    vi.advanceTimersByTime(ASK_DISMISS_UNDO_MS)
    await flushPromises()

    expect(store.asks[0].status).toBe('pending')
    expect(w.text()).toContain('Try again in a moment.')
  })

  it('a poll inside the window does not resurrect the ask as pending', async () => {
    mountAsks([ask('a1')])
    store.dismissAsk('a1')
    portalHttp.get = vi.fn().mockResolvedValue({ data: [ask('a1')] })

    await store.fetchAsks()

    expect(store.asks[0].status).toBe('dismissed')
  })

  it('Undo is withdrawn the moment the dismissal goes on the wire', async () => {
    let resolvePost
    portalHttp.post.mockImplementation(() => new Promise((r) => { resolvePost = r }))
    const w = mountAsks([ask('a1')])
    await tid(w, 'portal-ask-dismiss-a1').trigger('click')

    vi.advanceTimersByTime(ASK_DISMISS_UNDO_MS)
    await nextTick()

    expect(tid(w, 'portal-ask-undo-a1').exists()).toBe(false)
    expect(store.undoDismissAsk('a1')).toBe(false)
    expect(store.asks[0].status).toBe('dismissed')
    resolvePost({ data: ask('a1', { status: 'dismissed', ended_by: 'you' }) })
    await flushPromises()
    expect(store.askDismissals).toEqual({})
  })

  it('a poll that finds the ask already ended closes the Undo window', async () => {
    const w = mountAsks([ask('a1')])
    await tid(w, 'portal-ask-dismiss-a1').trigger('click')
    portalHttp.get = vi.fn().mockResolvedValue({ data: [ask('a1', { status: 'answered', ended_by: 'operator' })] })

    await store.fetchAsks()
    vi.advanceTimersByTime(ASK_DISMISS_UNDO_MS * 2)
    await flushPromises()

    expect(store.asks[0].status).toBe('answered')
    expect(store.undoDismissAsk('a1')).toBe(false)
    expect(portalHttp.post).not.toHaveBeenCalled()
  })

  it('signing out drops a dismissal still in its window instead of sending it', async () => {
    mountAsks([ask('a1')])
    store.dismissAsk('a1')
    store.signOut()

    vi.advanceTimersByTime(ASK_DISMISS_UNDO_MS * 2)
    await flushPromises()

    expect(portalHttp.post).not.toHaveBeenCalled()
  })

  it('a waiting-only surface keeps the card through its Undo window', async () => {
    const w = mountAsks([ask('a1')], { agentName: null, askIds: null, pendingOnly: true })
    await tid(w, 'portal-ask-dismiss-a1').trigger('click')
    expect(tid(w, 'portal-ask-undo-a1').exists()).toBe(true)
  })

  it('an alert offers no Dismiss and no Discuss — "Got it" already ends it', () => {
    const w = mountAsks([ask('a1', { kind: 'alert' })])
    expect(tid(w, 'portal-ask-dismiss-a1').exists()).toBe(false)
    expect(tid(w, 'portal-ask-discuss-a1').exists()).toBe(false)
  })

  it('reads as "Dismissed by you" — its own ending, not a cancel', () => {
    const ending = queueEnding({ status: 'dismissed', ended_by: 'you', ended_at: '2026-10-02T10:00:00Z' })
    expect(queueEndingText(ending)).toBe('Dismissed by you')
    // The Operating Room's raw row: status cancelled, the ledger says dismissed.
    expect(queueEnding({ status: 'cancelled', disposition: 'dismissed', disposed_by_email: 'c@x.io' }).kind).toBe('dismissed')
  })
})

describe('Discuss (ent#747)', () => {
  it('opens the discussion and navigates to it through open-thread', async () => {
    const discussed = ask('a1', { discussion_chat_id: 'chat-9' })
    portalHttp.post.mockResolvedValue({ data: { chat_id: 'chat-9', agent_name: 'scout', created: true, title: 'Ask a1', ask: discussed } })
    const w = mountAsks([ask('a1')])

    await tid(w, 'portal-ask-discuss-a1').trigger('click')
    await flushPromises()

    expect(portalHttp.post.mock.calls[0][0]).toBe('/api/enterprise/client-portal/asks/a1/discuss')
    expect(w.emitted('open-thread')).toEqual([[{ id: 'chat-9', agent_name: 'scout' }]])
    // The ask row learned its chat, so the next click says "Continue".
    expect(store.asks[0].discussion_chat_id).toBe('chat-9')
    expect(tid(w, 'portal-ask-discuss-a1').text()).toBe('Continue discussion')
    // The ask itself is untouched: still waiting, still answerable.
    expect(store.asks[0].status).toBe('pending')
  })

  it('is hidden inside the chat it would open', () => {
    const w = mountAsks([ask('a1', { discussion_chat_id: 'chat-9' })], { currentSessionId: 'chat-9' })
    expect(tid(w, 'portal-ask-discuss-a1').exists()).toBe(false)
    expect(tid(w, 'portal-ask-dismiss-a1').exists()).toBe(true)
  })

  it('a refused Discuss stays on the card with the server’s reason', async () => {
    portalHttp.post.mockRejectedValue(Object.assign(new Error('x'), {
      response: { status: 409, data: { detail: { message: 'This ask is already answered.' } } },
    }))
    const w = mountAsks([ask('a1')])

    await tid(w, 'portal-ask-discuss-a1').trigger('click')
    await flushPromises()

    expect(w.emitted('open-thread')).toBeUndefined()
    expect(w.text()).toContain('This ask is already answered.')
  })
})

describe('a discussed ask is drawn in its discussion chat', () => {
  it('chatTurnAsks includes the ask a chat discusses, wherever it was raised', () => {
    const background = ask('bg', { chat_id: 'main', raised_in_turn: false, discussion_chat_id: 'chat-9' })
    const elsewhere = ask('other', { chat_id: 'main', raised_in_turn: false })
    expect(chatTurnAsks([background, elsewhere], 'chat-9').map((a) => a.id)).toEqual(['bg'])
    expect(chatTurnAsks([background], 'main')).toEqual([])
    expect(isDiscussedIn(background, 'chat-9')).toBe(true)
  })

  it('heads the thread and survives truncated history', () => {
    const discussed = ask('a1', { created_at: '2026-01-01T00:00:00Z', discussion_chat_id: 'chat-9' })
    const rows = [
      { kind: 'message', message: { at: '2026-10-01T10:00:00Z' } },
      { kind: 'message', message: { at: '2026-10-01T10:01:00Z' } },
    ]
    const placed = placeAsksInThread(rows, [discussed], { truncated: true, sessionId: 'chat-9' })
    expect(placed[0]).toEqual({ kind: 'ask', ask: discussed })
    expect(placed).toHaveLength(3)
    // Without the session it is an ordinary old ask, and truncation drops it.
    expect(placeAsksInThread(rows, [discussed], { truncated: true })).toHaveLength(2)
  })
})

describe('store.answerAsk — where the woken agent\'s result will land (ent#747)', () => {
  // #3181 review F1: only a discussed ask's run reports into a chat.
  it('watches the discussion chat only, and only when work was resumed', async () => {
    store.asks = [ask('a1')]
    portalHttp.post.mockResolvedValueOnce({ data: ask('a1', { status: 'answered', resume_requested: true, chat_id: 'main', discussion_chat_id: 'disc' }) })
    await store.answerAsk('a1', { response: 'EU' })
    expect(store.askResultWatch).toMatchObject({ askId: 'a1', agentName: 'scout', chatId: 'disc' })

    store.askResultWatch = null
    store.asks = [ask('a2')]
    portalHttp.post.mockResolvedValueOnce({ data: ask('a2', { status: 'answered', resume_requested: true, chat_id: 'main' }) })
    await store.answerAsk('a2', { response: 'EU' })
    expect(store.askResultWatch).toBeNull()

    store.askResultWatch = null
    store.asks = [ask('a3')]
    portalHttp.post.mockResolvedValueOnce({ data: ask('a3', { status: 'answered', resume_requested: false, chat_id: 'main' }) })
    await store.answerAsk('a3', { response: 'EU' })
    expect(store.askResultWatch).toBeNull()
  })
})

describe('store.discussAsk', () => {
  it('rethrows so the control can say why', async () => {
    portalHttp.post.mockRejectedValue(new Error('down'))
    store.asks = [ask('a1')]
    await expect(store.discussAsk('a1')).rejects.toThrow('down')
    await nextTick()
    expect(store.asks[0].discussion_chat_id).toBeNull()
  })
})
