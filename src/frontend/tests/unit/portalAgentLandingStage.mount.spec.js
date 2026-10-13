// @vitest-environment jsdom
/**
 * ent#784 (review) — what the stage is once an agent has LANDED.
 *
 * ent#784 made `/workspace/a/:name` the URL a landed new chat RESTS on, where
 * every earlier version left it within a tick. Three things had only ever been
 * true because nobody stayed there, and each was reproduced in a browser:
 *
 *   1. Clicking the row of the agent you are already on cleared the fresh-chat
 *      intent (`openAgentPage`) and then pushed the URL it was already on — a
 *      no-op, so the landing never re-ran. The composer still read "New chat",
 *      and its first send went out without `new_thread`, which the server
 *      resolves to the agent's MAIN chat.
 *   2. The rail rules still read the agent URL as "a page, not a conversation",
 *      so a landed new chat had no rail — and the first send, which moves the
 *      URL to `/workspace/c/:id`, slid one in beside a reply mid-stream.
 *   3. A landing that REUSES a chat (the empty one, or a remembered one) goes
 *      through `openThread`, and a thread mount focuses nothing — so "the cursor
 *      is in the message field" held for a new chat and not for the agent's
 *      empty one.
 *
 * `Portal.vue` is mounted for real (shallow — the harness of
 * `portalAgentLandingRemount.mount.spec.js`), so the click door, the rail's
 * conditions and the focus hand-off are the shell's own.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { defineComponent, h } from 'vue'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'

// The landing's focus rule asks `(pointer: fine)`; jsdom answers every query
// `false`, i.e. a phone. Each case says which device it is on.
let finePointer = false
vi.hoisted(() => {
  window.matchMedia = window.matchMedia || ((q) => ({
    matches: false, media: q, addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  }))
})
function installMatchMedia() {
  window.matchMedia = (q) => ({
    matches: q.includes('pointer: fine') ? finePointer : false,
    media: q,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  })
}

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
import Portal from '@/views/Portal.vue'

globalThis.ResizeObserver = globalThis.ResizeObserver || class { observe() {} unobserve() {} disconnect() {} }

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

// scout's Main is USED, so opening scout is a new chat (arm 5). sage's Main is
// EMPTY, so opening sage reuses it (arm 4) — the landing that names a thread.
const SCOUT_MAIN = { id: 't1', session_id: 't1', agent_name: 'scout', is_main: true, last_message_at: '2026-09-27T10:00:00Z', unread: 0 }
const SAGE_EMPTY = { id: 'g1', session_id: 'g1', agent_name: 'sage', is_main: true, created_at: '2026-09-20T10:00:00Z', unread: 0 }
const THREADS = [SCOUT_MAIN, SAGE_EMPTY]

let store
let focusComposer

// Stubbed, but honest about the one thing the shell reaches through
// `conversationRef` here: the auto-stub exposes no `focusComposer`.
const ConversationStub = defineComponent({
  name: 'PortalConversation',
  props: ['agent', 'sessionId', 'newChat', 'threads', 'focusOnMount', 'prefill'],
  setup(_, { expose }) {
    expose({ focusComposer, startVoiceCall: vi.fn(), endVoiceCall: vi.fn() })
    return () => h('div')
  },
})

function arm() {
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.fetchRoster = vi.fn(async () => {
    store.agents = [{ name: 'scout' }, { name: 'sage' }]
    store.error = null
    store.rosterLoaded = true
  })
  store.fetchAllSessions = vi.fn(async () => { store.sessionsFailed = false; return THREADS })
  store.fetchChatState = vi.fn(async (opts) => (opts ? { state: {}, previews: {} } : {}))
  store.fetchSessions = vi.fn(async () => THREADS)
  store.fetchAsks = vi.fn(async () => [])
  store.seedAgentRecency = vi.fn()
  store.ensureBriefing = vi.fn()
  store.loadAgentSuggestions = vi.fn()
  store.markChatRead = vi.fn(async () => {})
}

let mounted = []
async function boot(path) {
  arm()
  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  const w = shallowMount(Portal, {
    global: { plugins: [router], stubs: { PortalConversation: ConversationStub } },
  })
  mounted.push(w)
  await flushPromises(); await flushPromises()
  return { w, router }
}

const conversation = (w) => w.findComponent({ name: 'PortalConversation' })
// `uid` is the instance identity: a `convGen` bump changes `:key`, so a remount
// is a new component instance with a new uid. (Not `c.vm` — with a hand-written
// stub the wrapper hands back a fresh proxy per read, which never compares equal.)
const showing = (w) => {
  const c = conversation(w)
  return { session: c.props('sessionId'), newChat: c.props('newChat'), agent: c.props('agent')?.name, uid: c.vm.$.uid }
}
// The desktop sidebar's agent row: the click door, exactly as the template binds it.
async function clickAgentRow(w, name) {
  w.findAllComponents({ name: 'PortalSidebar' })[0].vm.$emit('open-agent', name)
  await flushPromises()
  await w.vm.$nextTick()
}
const rail = (w) => w.findComponent({ name: 'PortalRail' })

beforeEach(() => {
  localStorage.clear()
  setActivePinia(createPinia())
  finePointer = false
  installMatchMedia()
  focusComposer = vi.fn()
})
afterEach(() => {
  for (const w of mounted) w.unmount()
  mounted = []
})

describe('ent#784 — clicking the agent you are already on keeps the new chat', () => {
  it('a second click on the same row does not spend the fresh-chat intent', async () => {
    const { w, router } = await boot('/workspace/a/scout')
    const landed = showing(w)
    expect(landed.newChat).toBe(true)
    expect(landed.session).toBe(null)

    await clickAgentRow(w, 'scout')

    // Still the SAME unsent chat, and still one that will ask for a new thread:
    // `newChat` is what the composer turns into `new_thread` on its first send,
    // and without it the server files the message in the agent's Main.
    const after = showing(w)
    expect(after.newChat).toBe(true)
    expect(after.session).toBe(null)
    expect(after.uid).toBe(landed.uid)   // no remount
    expect(router.currentRoute.value.path).toBe('/workspace/a/scout')
  })

  it('a click on a DIFFERENT agent still lands on that agent', async () => {
    // The door must stay a door: the early return is for the row you are on.
    const { w } = await boot('/workspace/a/scout')
    await clickAgentRow(w, 'sage')
    expect(showing(w).agent).toBe('sage')
  })

  it('the same click from a THREAD of that agent still lands fresh', async () => {
    // Being "on scout" is not being on scout's NEW chat: from one of its
    // threads the row means what it always meant.
    const { w, router } = await boot('/workspace/c/t1')
    expect(showing(w).session).toBe('t1')
    await clickAgentRow(w, 'scout')
    expect(router.currentRoute.value.path).toBe('/workspace/a/scout')
    expect(showing(w).newChat).toBe(true)
    expect(showing(w).session).toBe(null)
  })
})

describe('ent#784 — a landed new chat has the rail every other chat has', () => {
  it('the rail is beside a thread (control)', async () => {
    const { w } = await boot('/workspace/c/t1')
    expect(rail(w).exists()).toBe(true)
  })

  it('and beside the new chat an agent click lands on', async () => {
    // Same agent, same conversation component, one URL apart. Without the rail
    // here the first send — which moves the URL to `/workspace/c/:id` — brings
    // the column in beside a reply that is already streaming.
    const { w, router } = await boot('/workspace/a/scout')
    expect(router.currentRoute.value.path).toBe('/workspace/a/scout')
    expect(showing(w).newChat).toBe(true)
    expect(rail(w).exists()).toBe(true)
  })

  it('but not beside the refusal for an agent the link cannot reach', async () => {
    const { w } = await boot('/workspace/a/ghost')
    expect(rail(w).exists()).toBe(false)
  })

  it('and its column is held while an agent link is still loading (#2711)', async () => {
    // The agent URL is a 1:1 conversation route now, so it reserves the rail's
    // column mid-load like `/workspace/c/:id` does — or the conversation paints
    // full width and gives the width back the moment the roster lands.
    arm()
    store.fetchRoster = vi.fn(() => new Promise(() => {}))   // the roster never answers
    const router = makeRouter()
    await router.push('/workspace/a/scout')
    await router.isReady()
    const w = shallowMount(Portal, {
      global: { plugins: [router], stubs: { PortalConversation: ConversationStub } },
    })
    mounted.push(w)
    await flushPromises()
    expect(w.find('[data-reserved="true"]').exists()).toBe(true)
  })
})

describe('ent#784 — a landing that reuses a chat puts the caret in the field too', () => {
  it('opening an agent whose empty chat is reused focuses the composer (fine pointer)', async () => {
    finePointer = true
    const { w, router } = await boot('/workspace/c/t1')
    focusComposer.mockClear()

    await clickAgentRow(w, 'sage')

    expect(router.currentRoute.value.path).toBe('/workspace/c/g1')   // arm 4: the empty chat
    expect(showing(w).session).toBe('g1')
    expect(focusComposer).toHaveBeenCalled()
  })

  it('and does not on a touch device, where focus summons the keyboard', async () => {
    finePointer = false
    const { w, router } = await boot('/workspace/c/t1')
    focusComposer.mockClear()

    await clickAgentRow(w, 'sage')

    expect(router.currentRoute.value.path).toBe('/workspace/c/g1')
    expect(focusComposer).not.toHaveBeenCalled()
  })

  it('a second click on the row you are on hands the caret back', async () => {
    // The click itself moved focus to the sidebar button; nothing remounts, so
    // nothing else would return it.
    finePointer = true
    const { w } = await boot('/workspace/a/scout')
    focusComposer.mockClear()
    await clickAgentRow(w, 'scout')
    expect(focusComposer).toHaveBeenCalled()
  })
})
