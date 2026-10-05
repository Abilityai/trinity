// @vitest-environment jsdom
/**
 * ent#621 [B] — the Workspace keys, dispatched by the shell.
 *
 * The rules are pure and proven in `workspaceKeymap.spec.js`. What this file
 * proves is the WIRING, which a pure test cannot reach and a regex over
 * `Portal.vue` would pass with an inverted condition: that a real `keydown` on
 * a real `window` reaches the listener, resolves to an action, walks the
 * suppression ladder, and moves the stage the way a click would.
 *
 * `Portal.vue` is mounted for real (shallow — the harness of
 * `portalAgentLandingRemount.mount.spec.js`), with ONE custom stub: the
 * conversation, because the auto-stub exposes nothing and
 * `conversationRef.value?.cycleChat?.()` would then hide deleted wiring behind
 * its own optional chaining — green with the chat keys unplugged.
 */
import { describe, it, expect, beforeEach, afterEach, vi } from 'vitest'
import { shallowMount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { defineComponent, h } from 'vue'

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

// scout: Main + one older thread. sage: Main + two. nova: nothing at all — the
// agent a key lands on for the first time.
const ROSTER = [{ name: 'scout' }, { name: 'sage' }, { name: 'nova' }]
const THREADS = [
  { id: 's-main', session_id: 's-main', agent_name: 'scout', is_main: true, title: 'Main', last_message_at: '2026-09-27T12:00:00Z', unread: 0 },
  { id: 's-old', session_id: 's-old', agent_name: 'scout', title: 'Pricing page', last_message_at: '2026-09-26T12:00:00Z', unread: 0 },
  { id: 'g-main', session_id: 'g-main', agent_name: 'sage', is_main: true, title: 'Main', last_message_at: '2026-09-25T12:00:00Z', unread: 0 },
  { id: 'g-old', session_id: 'g-old', agent_name: 'sage', title: 'Q3 numbers', last_message_at: '2026-09-24T12:00:00Z', unread: 0 },
]

let store
let cycleChat
let focusComposer

// The conversation, stubbed but HONEST about its exposed surface: the shell
// reaches `cycleChat` and `focusComposer` through `conversationRef`, and the
// auto-stub exposes neither.
const ConversationStub = defineComponent({
  name: 'PortalConversation',
  props: ['agent', 'sessionId', 'newChat', 'threads', 'focusOnMount', 'prefill', 'draftKey'],
  setup(_, { expose }) {
    expose({ cycleChat, focusComposer, startVoiceCall: vi.fn(), endVoiceCall: vi.fn() })
    return () => h('div')
  },
})

function arm(threads = THREADS, roster = ROSTER) {
  store = useClientPortalStore()
  store.portalToken = 'tok'
  store.fetchRoster = vi.fn(async () => {
    store.agents = roster.slice()
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

// Every mount arms a `window` listener, so a wrapper left standing would answer
// the NEXT test's key press FIRST and `preventDefault` it — after which this
// shell correctly yields on `defaultPrevented` and the case reads as broken
// wiring. Unmounted in `afterEach`, which is also what proves the teardown
// removes the listener.
let mounted = []
async function boot(path, { threads = THREADS, roster = ROSTER } = {}) {
  arm(threads, roster)
  const router = makeRouter()
  await router.push(path)
  await router.isReady()
  const w = shallowMount(Portal, {
    attachTo: document.body,
    global: { plugins: [router], stubs: { PortalConversation: ConversationStub } },
  })
  mounted.push(w)
  await flushPromises(); await flushPromises()
  return { w, router }
}

// A real event on the real window — the listener is armed there, so anything
// less would test a function call rather than a key press.
async function press(w, { key, alt = false, shift = false, meta = false, target = null, prevented = false } = {}) {
  const e = new KeyboardEvent('keydown', {
    key, altKey: alt, shiftKey: shift, metaKey: meta, bubbles: true, cancelable: true,
  })
  if (prevented) e.preventDefault()
  ;(target || window).dispatchEvent(e)
  await flushPromises()
  await w.vm.$nextTick()
  return e
}

const conversation = (w) => w.findComponent({ name: 'PortalConversation' })
const showing = (w) => {
  const c = conversation(w)
  if (!c.exists()) return { agent: null, session: null, newChat: null }
  return { agent: c.props('agent')?.name, session: c.props('sessionId'), newChat: c.props('newChat'), focus: c.props('focusOnMount') }
}
const announced = (w) => w.find('[data-testid="ws-key-announce"]').text()

beforeEach(() => {
  localStorage.clear()
  document.body.innerHTML = ''
  setActivePinia(createPinia())
  cycleChat = vi.fn()
  focusComposer = vi.fn()
})
afterEach(() => {
  for (const w of mounted) w.unmount()
  mounted = []
})

describe('ent#621 — ⌥↑ / ⌥↓ switch agent', () => {
  it('walks the roster order the sidebar shows, and wraps at both ends', async () => {
    const { w } = await boot('/workspace/c/s-main')
    expect(showing(w).agent).toBe('scout')
    // The order is `orderRosterAgents` over the shell's own list: scout (most
    // recent), sage, nova.
    expect(w.vm.orderedRoster.map((a) => a.name)).toEqual(['scout', 'sage', 'nova'])

    await press(w, { key: 'ArrowDown', alt: true })
    expect(showing(w).agent).toBe('sage')

    await press(w, { key: 'ArrowDown', alt: true })
    expect(showing(w).agent).toBe('nova')

    // Past the last agent is the first one, not a dead end.
    await press(w, { key: 'ArrowDown', alt: true })
    expect(showing(w).agent).toBe('scout')

    // And backwards wraps the other way.
    await press(w, { key: 'ArrowUp', alt: true })
    expect(showing(w).agent).toBe('nova')
  })

  it('a key press IS a gesture, so the caret goes back to the message field', async () => {
    // A thread mount focuses nothing on its own (only a new chat or a restored
    // draft does), so without the dispatcher's own call the AC's "the cursor
    // stays in the message field" is false for every move onto a thread.
    const { w } = await boot('/workspace/c/s-main')
    focusComposer.mockClear()
    await press(w, { key: 'ArrowDown', alt: true })
    expect(focusComposer).toHaveBeenCalled()
    expect(showing(w).focus).toBe('always')
  })

  it('a roster of one is a silent no-op', async () => {
    const { w } = await boot('/workspace/c/s-main', { roster: [{ name: 'scout' }] })
    const before = showing(w)
    await press(w, { key: 'ArrowDown', alt: true })
    // Nothing moved, and nothing was typed into the field either — the key is
    // ours, it simply has nowhere to go.
    expect(showing(w)).toEqual(before)
  })

  it('lands on a never-opened agent exactly as a click would, and names the URL', async () => {
    const { w, router } = await boot('/workspace/c/s-main')
    await press(w, { key: 'ArrowUp', alt: true })      // scout → nova (wrap)
    const s = showing(w)
    expect(s.agent).toBe('nova')
    expect(s.newChat).toBe(true)
    expect(s.session).toBe(null)
    // The fresh arm does not navigate on its own, and without this push the URL
    // still names the chat we left — which the `/c/:id` watcher snaps back to
    // on the next thread-list change.
    expect(router.currentRoute.value.path).toBe('/workspace/a/nova')
    // ...and the push happens AFTER the state write, so the route watcher's
    // re-fire is absorbed by `landOnAgent`'s hoisted guard. Pushed FIRST, the
    // landing would arrive through that watcher instead — with the route
    // default's `'fine-pointer'` focus, which is the wrong answer for a key
    // press, and without the last-open id the key asked for.
    expect(s.focus).toBe('always')
  })

  it('says out loud what it landed on', async () => {
    const { w } = await boot('/workspace/c/s-main')
    await press(w, { key: 'ArrowDown', alt: true })
    expect(announced(w)).toContain('sage')
    await press(w, { key: 'ArrowDown', alt: true })
    expect(announced(w)).toBe('nova — New chat')
  })
})

describe('ent#621 — the last chat open with each agent', () => {
  it('⌥↑ back to an agent returns to the chat that was open there', async () => {
    const { w } = await boot('/workspace/c/s-old')
    expect(showing(w).session).toBe('s-old')

    await press(w, { key: 'ArrowDown', alt: true })          // → sage, never opened
    expect(showing(w).agent).toBe('sage')

    await press(w, { key: 'ArrowUp', alt: true })            // ← scout
    expect(showing(w).agent).toBe('scout')
    expect(showing(w).session).toBe('s-old')                 // not Main, and not a fresh chat
  })

  it('an agent left on an unsent new chat comes back through the ordinary arms', async () => {
    // The memory records `null` for an unsent chat rather than a sentinel, so
    // `agentLanding` answers — one landing rule, not two.
    const { w } = await boot('/workspace/c/s-main')
    await press(w, { key: 'ArrowUp', alt: true })            // → nova, fresh
    expect(showing(w).newChat).toBe(true)
    await press(w, { key: 'ArrowDown', alt: true })          // → scout
    expect(showing(w).agent).toBe('scout')
    await press(w, { key: 'ArrowUp', alt: true })            // ← nova again
    expect(showing(w).agent).toBe('nova')
    expect(showing(w).newChat).toBe(true)
    expect(showing(w).session).toBe(null)
  })

  it('a stale id is not honoured — the landing validates it', async () => {
    // The memory is in-session, so the only way to hold an id the list no longer
    // has is for the list to change under it. The rule lives in `agentLanding`
    // (ent#784); what this proves is that the shell routes through it.
    const { w } = await boot('/workspace/c/g-old')
    expect(showing(w).session).toBe('g-old')
    await press(w, { key: 'ArrowDown', alt: true })          // → nova
    store.agents = ROSTER.slice()
    // sage's second chat is gone; its Main remains.
    w.vm.threads = THREADS.filter((t) => t.id !== 'g-old')
    await w.vm.$nextTick()
    await press(w, { key: 'ArrowUp', alt: true })            // ← sage
    expect(showing(w).agent).toBe('sage')
    expect(showing(w).session).not.toBe('g-old')
  })
})

describe('ent#621 — ⌥⇧↑ / ⌥⇧↓ switch chat', () => {
  it('asks the conversation to walk its own tab strip', async () => {
    const { w } = await boot('/workspace/c/s-main')
    await press(w, { key: 'ArrowDown', alt: true, shift: true })
    expect(cycleChat).toHaveBeenCalledWith(1)
    await press(w, { key: 'ArrowUp', alt: true, shift: true })
    expect(cycleChat).toHaveBeenCalledWith(-1)
    // Shift is not a modifier the agent keys accept, so the stage did not move.
    expect(showing(w).agent).toBe('scout')
  })

  it('the chat keys do not move the agent, and the agent keys do not touch the strip', async () => {
    const { w } = await boot('/workspace/c/s-main')
    await press(w, { key: 'ArrowDown', alt: true })
    expect(cycleChat).not.toHaveBeenCalled()
  })
})

describe('ent#621 — the suppression ladder', () => {
  it('anything modal stops the keys, and does not claim the event either', async () => {
    const { w } = await boot('/workspace/c/s-main')
    const modal = document.createElement('div')
    modal.setAttribute('aria-modal', 'true')
    document.body.appendChild(modal)

    const e = await press(w, { key: 'ArrowDown', alt: true })
    expect(showing(w).agent).toBe('scout')
    // Not claimed: the overlay's own handlers must still see the key.
    expect(e.defaultPrevented).toBe(false)

    modal.remove()
    await press(w, { key: 'ArrowDown', alt: true })
    expect(showing(w).agent).toBe('sage')
  })

  it('the rail sheet is not "something modal over the rail"', async () => {
    // It carries `aria-modal` (it is a dialog), so without the marker it would
    // suppress every key including the rail's own.
    const { w } = await boot('/workspace/c/s-main')
    const sheet = document.createElement('aside')
    sheet.setAttribute('aria-modal', 'true')
    sheet.setAttribute('data-ws-rail-sheet', 'true')
    document.body.appendChild(sheet)
    // The agent keys still stop at it — the exemption is PER ACTION, and the
    // stage behind an open sheet must not move.
    await press(w, { key: 'ArrowDown', alt: true })
    expect(showing(w).agent).toBe('scout')
  })

  it('the mobile drawer stops the keys', async () => {
    const { w } = await boot('/workspace/c/s-main')
    w.vm.mobileNav = true
    await w.vm.$nextTick()
    await press(w, { key: 'ArrowDown', alt: true })
    expect(showing(w).agent).toBe('scout')
  })

  it('a voice call makes the moving keys silent no-ops', async () => {
    const { w } = await boot('/workspace/c/s-main')
    w.vm.voiceCall = { active: true, agentName: 'scout', voiceSessionId: 'v1' }
    await w.vm.$nextTick()
    await press(w, { key: 'ArrowDown', alt: true })
    expect(showing(w).agent).toBe('scout')
    await press(w, { key: 'ArrowDown', alt: true, shift: true })
    expect(cycleChat).not.toHaveBeenCalled()
  })

  it('yields to a nearer owner that already claimed the event', async () => {
    // The protocol is `defaultPrevented`, not a registry: the typeahead's bare
    // arrows and the Esc ladder are handled closer to the target, and the shell
    // must not act on an event they consumed.
    const { w } = await boot('/workspace/c/s-main')
    await press(w, { key: 'ArrowDown', alt: true, prevented: true })
    expect(showing(w).agent).toBe('scout')
  })

  it('a focused <select> keeps its native Alt+↓', async () => {
    const { w } = await boot('/workspace/c/s-main')
    const sel = document.createElement('select')
    document.body.appendChild(sel)
    const e = await press(w, { key: 'ArrowDown', alt: true, target: sel })
    expect(showing(w).agent).toBe('scout')
    expect(e.defaultPrevented).toBe(false)
    sel.remove()
  })

  it('a chord nobody owns is left to the browser', async () => {
    // ⌘K is reserved for ent#577: in the map, no handler, and crucially no
    // `preventDefault` — the address-bar search keeps working until it ships.
    const { w } = await boot('/workspace/c/s-main')
    const e = await press(w, { key: 'k', meta: true })
    expect(e.defaultPrevented).toBe(false)
    expect(showing(w).agent).toBe('scout')
  })

  it('a held key moves one agent, not one per repeat', async () => {
    const { w } = await boot('/workspace/c/s-main')
    const e = new KeyboardEvent('keydown', { key: 'ArrowDown', altKey: true, repeat: true, bubbles: true, cancelable: true })
    window.dispatchEvent(e)
    await flushPromises()
    expect(showing(w).agent).toBe('scout')
    expect(e.defaultPrevented).toBe(false)
  })
})
