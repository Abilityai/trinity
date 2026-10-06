// @vitest-environment jsdom
/**
 * ent#784 arm 3, mounted — the two flows `e2e/workspace-drafts.spec.js` lost.
 *
 * The pure arm cases are in `portalDraftLanding.spec.js`. Nothing there proves
 * the SHELL asks the rule the right question: `landOnAgent` has to hand it the
 * drafts map and then actually open the thread it names. That is what was
 * broken — the rule answered "a new chat" for every landing, so the two e2e
 * cases below went red at `c8af3d70` while every unit spec stayed green.
 *
 * Replayed here at the shell's seam (`shallowMount`, the harness of
 * `portalAgentLandingRemount.mount.spec.js`), as the e2e's own two flows:
 *
 *   `:92`  type into A's thread → open B → open A again
 *   `:116` a reload on `/workspace/a/B` with `new:B` holding words
 *
 * `PortalConversation` is a stub, so the assertions stop at the props that
 * decide what the composer binds — the session, the fresh-chat flag, and the
 * `draftKeyFor` those two resolve to. The real textarea, its value and its
 * focus are the e2e's half; what this file owns is that the shell lands on the
 * chat whose key holds the words.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { draftKeyFor, draftStorageKey, persistDraft } from '@/components/portal/portalDrafts'

vi.hoisted(() => {
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }))
})

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, userEmail: 'me@example.com', logout: vi.fn() }),
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
import { usePortalDraftsStore } from '@/stores/portalDrafts'
import Portal from '@/views/Portal.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

const WHO = 'me@example.com'
const Blank = { template: '<div />' }
function makeRouter() {
  return createRouter({
    history: createMemoryHistory(),
    routes: [
      { path: '/workspace', component: Blank },
      { path: '/workspace/inbox', component: Blank },
      { path: '/workspace/c/:sessionId', component: Blank },
      { path: '/workspace/r/:roomId', component: Blank },
      { path: '/workspace/a/:agentName', component: Blank },
      { path: '/login', component: Blank },
    ],
  })
}

// The e2e's own fixture: two agents, two chats with A and one with B, every one
// of them already used (`message_count: 2`), so nothing here is an empty chat
// and the drafts arm is the only thing that can beat "a new chat".
const sess = (id, agent, when) => ({
  id, session_id: id, agent_name: agent, title: `${agent} — ${id}`,
  created_at: when, last_message_at: when, message_count: 2, unread: 0,
})
const THREADS = [
  sess('sess-a', 'drafts-a', '2026-09-03T10:00:00Z'),
  sess('sess-a2', 'drafts-a', '2026-09-02T10:00:00Z'),
  sess('sess-b', 'drafts-b', '2026-09-01T10:00:00Z'),
]

let store
function arm(threads = THREADS) {
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.fetchRoster = vi.fn(async () => {
    store.agents = [{ name: 'drafts-a' }, { name: 'drafts-b' }]
    // The drafts store keys its bucket on the server-reported principal, so
    // without this every draft written below lands in the memory-only map and
    // the reload flow could not be replayed at all.
    store.clientEmail = WHO
    store.error = null
    store.rosterLoaded = true
  })
  store.fetchAllSessions = vi.fn(async () => { store.sessionsFailed = false; return threads })
  store.fetchChatState = vi.fn(async (opts) => (opts ? { state: {}, previews: {} } : {}))
  store.fetchSessions = vi.fn(async () => threads)
  store.fetchAsks = vi.fn(async () => [])
  store.seedAgentRecency = vi.fn()
  store.ensureBriefing = vi.fn()
  store.loadAgentSuggestions = vi.fn()
  store.markChatRead = vi.fn(async () => {})
}

async function boot(path) {
  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  const w = shallowMount(Portal, { global: { plugins: [router] } })
  await flushPromises(); await flushPromises()
  return { w, router }
}

/** Where the shell landed, as the composer will read it. */
function landed(w) {
  const c = w.findComponent({ name: 'PortalConversation' })
  if (!c.exists()) return { exists: false }
  const sessionId = c.props('sessionId')
  const newChat = c.props('newChat')
  const agentName = c.props('agent')?.name
  return {
    exists: true,
    sessionId,
    newChat,
    agentName,
    draftKey: draftKeyFor({ sessionId, agentName, newChat }),
  }
}

/** A sidebar agent-row click: `openAgentPage` pushes this and the watcher lands. */
async function openAgent(router, name) {
  await router.push(`/workspace/a/${name}`)
  await flushPromises(); await flushPromises()
}

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  arm()
})

describe('ent#784 — the shell lands on the chat holding the draft', () => {
  it('A → B → A comes back to the words typed in A (e2e :92)', async () => {
    const { w, router } = await boot('/workspace/c/sess-a')
    expect(landed(w).sessionId).toBe('sess-a')

    // Typing into A's thread. The store write is what the composer's
    // write-through binding does on every keystroke.
    const drafts = usePortalDraftsStore()
    drafts.set('thread:sess-a', 'half a thought for A')
    await flushPromises()

    // B: a different agent, nothing typed with it — a fresh chat.
    await openAgent(router, 'drafts-b')
    expect(landed(w)).toMatchObject({ agentName: 'drafts-b', sessionId: null, newChat: true })

    // Back to A. This is the assertion that was red: the landing resolved to a
    // new chat (`sessionId: null`, key `new:drafts-a`) and the composer came up
    // empty, while A's row still carried the draft mark.
    await openAgent(router, 'drafts-a')
    expect(landed(w)).toMatchObject({
      agentName: 'drafts-a', sessionId: 'sess-a', newChat: false, draftKey: 'thread:sess-a',
    })
    expect(drafts.get(landed(w).draftKey)).toBe('half a thought for A')
  })

  it('a reload on an agent page restores its unsaved chat, then A still has its own (e2e :116)', async () => {
    // What a reload sees: the bucket on disk, written by the session before it.
    persistDraft(localStorage, WHO, 'thread:sess-a', { text: 'for A', updatedAt: 100 })
    persistDraft(localStorage, WHO, 'new:drafts-b', { text: 'for B', updatedAt: 200 })
    expect(localStorage.getItem(draftStorageKey(WHO))).toContain('for B')

    const { w, router } = await boot('/workspace/a/drafts-b')
    // B's draft lives on the unsaved chat, so the landing IS a new chat — and
    // its key is the one holding the words, which is what makes the composer
    // come up with them in it.
    expect(landed(w)).toMatchObject({
      agentName: 'drafts-b', sessionId: null, newChat: true, draftKey: 'new:drafts-b',
    })
    const drafts = usePortalDraftsStore()
    expect(drafts.get('new:drafts-b')).toBe('for B')

    // And clicking A goes to A's thread, not to a new chat (the red one).
    await openAgent(router, 'drafts-a')
    expect(landed(w)).toMatchObject({
      agentName: 'drafts-a', sessionId: 'sess-a', newChat: false, draftKey: 'thread:sess-a',
    })
    expect(drafts.get('thread:sess-a')).toBe('for A')
  })

  it('an agent with an empty chat reuses it, and the GET that INSERTS is not called', async () => {
    // F2 / arm 4, both halves. `ensureMainListed` is a GET that INSERTS
    // (`list_sessions` → `ensure_main_session`), so for an agent that already
    // has an empty chat but no Main it would add a SECOND empty row — which
    // the 2026-10-05 ruling forbids ("at most one empty chat per agent at any
    // time"). The frontend guard is the only place this can be held; the read
    // must simply not happen.
    setActivePinia(createPinia())
    arm([
      { id: 'spare', session_id: 'spare', agent_name: 'drafts-a', created_at: '2026-09-05T10:00:00Z' },
      sess('sess-b', 'drafts-b', '2026-09-01T10:00:00Z'),
    ])
    const { w } = await boot('/workspace/a/drafts-a')
    expect(landed(w)).toMatchObject({ agentName: 'drafts-a', sessionId: 'spare', newChat: false })
    expect(store.fetchSessions).not.toHaveBeenCalled()
  })

  it('an agent whose chats are all used still gets its Main on this visit (#2579)', async () => {
    // The guard above must not swallow the case #2579 was about: with nothing
    // empty to reuse, the pinned Main is still ensured on the first visit.
    setActivePinia(createPinia())
    arm([sess('sess-a', 'drafts-a', '2026-09-03T10:00:00Z')])
    await boot('/workspace/a/drafts-a')
    expect(store.fetchSessions).toHaveBeenCalledWith('drafts-a')
  })

  it('the landing is not a gesture: the composer may focus only on a fine pointer', async () => {
    // T3(b), on the drafted-thread path. `landOnAgent` sets this before it
    // branches, so a landing cannot inherit a previous gesture's `always` and
    // pop the soft keyboard on a phone.
    const drafts = usePortalDraftsStore()
    const { w, router } = await boot('/workspace/c/sess-a')
    drafts.set('thread:sess-a', 'words')
    await flushPromises()
    await openAgent(router, 'drafts-a')
    expect(w.findComponent({ name: 'PortalConversation' }).props('focusOnMount')).toBe('fine-pointer')
  })
})
