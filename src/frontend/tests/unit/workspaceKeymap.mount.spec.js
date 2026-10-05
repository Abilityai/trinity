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
    global: {
      plugins: [router],
      // `PortalKeyList` and `BaseModal` under it are NOT stubbed: what the
      // ⌘/ cases prove is that a REAL `aria-modal` dialog appears, carrying
      // the `data-ws-key-list` marker on the same element — which is what lets
      // ⌘/ close its own list while every other key is suppressed under it. A
      // stub renders an empty tag and passes all of that with the dialog
      // unplugged and the marker on the wrong element.
      // `teleport: false` as well: `shallowMount` stubs `<Teleport>`, and
      // `BaseModal` teleports its overlay to `<body>` — with the stub in place
      // the dialog never reaches the document, which is the only place the
      // dispatcher's `[aria-modal]` probe can see it.
      stubs: {
        PortalConversation: ConversationStub,
        PortalKeyList: false,
        BaseModal: false,
        teleport: false,
      },
    },
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

// The rail takes the COLUMN form at and above `sm` and the bottom-sheet form
// below it, and `isWideViewport` asks `matchMedia`. jsdom answers every query
// with `matches: false`, i.e. a phone — so a test about the column has to say
// so, and a test about the sheet has to say that.
function viewport(wide) {
  window.matchMedia = (q) => ({
    matches: wide && q.includes('min-width'),
    media: q,
    addEventListener() {}, removeEventListener() {}, addListener() {}, removeListener() {},
  })
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

  it('a new chat started on the agent already on stage is what ⌥↑ comes back to', async () => {
    // Review H1. ⌘J on the agent you are already in nulls `pendingSession`
    // under an UNCHANGED agent — the same shape as a load-instruction clear —
    // so the writer needs `startingNewChat` to tell the two apart. Without it
    // the memory still names the chat ⌘J just left, and ⌥↑ reopens it.
    const { w } = await boot('/workspace/c/s-old')
    expect(showing(w).session).toBe('s-old')

    await press(w, { key: 'j', meta: true })                 // ⌘J — a fresh chat with scout
    expect(showing(w).agent).toBe('scout')
    expect(showing(w).newChat).toBe(true)
    expect(showing(w).session).toBe(null)

    await press(w, { key: 'ArrowDown', alt: true })           // → sage
    expect(showing(w).agent).toBe('sage')
    await press(w, { key: 'ArrowUp', alt: true })             // ← scout
    expect(showing(w).agent).toBe('scout')
    expect(showing(w).newChat).toBe(true)                     // not 's-old' again
    expect(showing(w).session).toBe(null)
  })

  it('a load-instruction clear under an unchanged agent is still ignored', async () => {
    // The other half of H1: `startingNewChat` false means the null is an
    // instruction, not an intent (`openAgentPage`, `openRoom` and the
    // unreachable arm all clear it), and the chat the person was reading a
    // moment ago must survive it. Written as the clear itself because every
    // live door either leaves the stage or pushes a route that then lands a
    // new chat — what is pinned here is the writer's rule.
    const { w } = await boot('/workspace/c/s-old')
    expect(showing(w).session).toBe('s-old')

    w.vm.pendingSession = null                                // agent unchanged, no new-chat intent
    await w.vm.$nextTick()
    expect(w.vm.startingNewChat).toBe(false)

    await press(w, { key: 'ArrowDown', alt: true })           // → sage
    expect(showing(w).agent).toBe('sage')
    await press(w, { key: 'ArrowUp', alt: true })             // ← scout
    expect(showing(w).agent).toBe('scout')
    expect(showing(w).session).toBe('s-old')
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

describe('ent#621 [C] — ⌘. shows and hides the rail', () => {
  it('toggles the rail column, remembers the tab, and persists', async () => {
    viewport(true)
    const { w } = await boot('/workspace/c/s-main')
    // The rail starts collapsed; `⌘.` is the expand button's chord.
    expect(w.vm.railState.open).toBe(false)
    await press(w, { key: '.', meta: true })
    expect(w.vm.railState.open).toBe(true)
    // Persisted under the rail's own key — the key writes nothing new (T5).
    expect(localStorage.getItem('trinity-workspace-rail')).toContain('"open":true')

    // Moving to another tab and closing, then reopening: the rail comes back
    // where it was, because the key reuses `railState.tab` rather than
    // keeping a second memory that could disagree with the button's.
    w.vm.setRailTab(w.vm.railTabs[1].id)
    await press(w, { key: '.', meta: true })
    expect(w.vm.railState.open).toBe(false)
    await press(w, { key: '.', meta: true })
    expect(w.vm.railState.open).toBe(true)
    expect(w.vm.railState.tab).toBe(w.vm.railTabs[1].id)
  })

  it('says which tab came back, for anyone who cannot see it', async () => {
    viewport(true)
    const { w } = await boot('/workspace/c/s-main')
    await press(w, { key: '.', meta: true })
    expect(announced(w)).toContain('rail open')
    await press(w, { key: '.', meta: true })
    expect(announced(w)).toBe('Rail closed')
  })

  it('below `sm` it opens and closes the bottom sheet instead, persisting nothing', async () => {
    // The sheet is the phone's rail. `open` must NOT be written: a tap on a
    // phone that reloads into a pushed-open column is the ent#474 rule.
    viewport(false)
    const { w } = await boot('/workspace/c/s-main')
    await press(w, { key: '.', meta: true })
    expect(w.vm.railSheetOpen).toBe(true)
    expect(w.vm.railState.open).toBe(false)
    await press(w, { key: '.', meta: true })
    expect(w.vm.railSheetOpen).toBe(false)
  })

  it('is a silent no-op on a page with no rail', async () => {
    viewport(true)
    // The agent page has no rail column at all. A key that toggled hidden
    // state there would be a key that does nothing visible — twice.
    const { w } = await boot('/workspace/a/nova')
    expect(w.vm.railHasColumn).toBe(false)
    const before = w.vm.railState.open
    const e = await press(w, { key: '.', meta: true })
    expect(w.vm.railState.open).toBe(before)
    // Not claimed either: nothing here owns the chord.
    expect(e.defaultPrevented).toBe(false)
  })

  it('stays out of the way during a voice call', async () => {
    viewport(true)
    const { w } = await boot('/workspace/c/s-main')
    w.vm.voiceCall = { active: true, agentName: 'scout', voiceSessionId: 'v1' }
    await w.vm.$nextTick()
    const before = w.vm.railState.open
    await press(w, { key: '.', meta: true })
    expect(w.vm.railState.open).toBe(before)
  })
})

describe('ent#621 [C] — ⌥. walks the rail tabs', () => {
  it('opens a closed rail on its remembered tab first, then moves on', async () => {
    viewport(true)
    const { w } = await boot('/workspace/c/s-main')
    expect(w.vm.railState.open).toBe(false)
    const first = w.vm.railTabs[0].id

    await press(w, { key: '.', alt: true })
    expect(w.vm.railState.open).toBe(true)
    expect(w.vm.railState.tab).toBe(first)

    await press(w, { key: '.', alt: true })
    expect(w.vm.railState.tab).toBe(w.vm.railTabs[1].id)
    expect(announced(w)).toBe(w.vm.railTabs[1].label)
  })

  it('cycles only the tabs this session may SEE, and wraps', async () => {
    viewport(true)
    const { w } = await boot('/workspace/c/s-main')
    const visible = w.vm.railTabs.map((t) => t.id)
    expect(visible.length).toBeGreaterThan(1)
    await press(w, { key: '.', alt: true })          // opens on the first
    for (let i = 1; i < visible.length; i += 1) {
      await press(w, { key: '.', alt: true })
      expect(w.vm.railState.tab).toBe(visible[i])
    }
    // Past the last visible tab is the first visible one — never a tab the
    // door closed, which `RAIL_TAB_ORDER` cycling would have reached.
    await press(w, { key: '.', alt: true })
    expect(w.vm.railState.tab).toBe(visible[0])
  })

  it('does not move the agent behind it', async () => {
    viewport(true)
    const { w } = await boot('/workspace/c/s-main')
    await press(w, { key: '.', alt: true })
    expect(showing(w).agent).toBe('scout')
  })
})

describe('ent#621 [C] — ⌘/ opens the key list', () => {
  it('opens, lists every key the map declares but the reserved one, and toggles shut', async () => {
    const { w } = await boot('/workspace/c/s-main')
    expect(document.querySelector('[data-testid="ws-key-list"]')).toBe(null)

    await press(w, { key: '/', meta: true })
    const dialog = document.querySelector('[data-ws-key-list]')
    expect(dialog).not.toBe(null)
    expect(dialog.getAttribute('role')).toBe('dialog')
    expect(dialog.getAttribute('aria-modal')).toBe('true')

    // Rendered FROM the map: every non-reserved entry has a row, and ⌘K — the
    // chord ent#577 owns and nothing binds — has none.
    const rows = Array.from(document.querySelectorAll('[data-ws-key-row]'))
      .map((el) => el.getAttribute('data-ws-key-row'))
    // Same SET as the map's rows (the dialog groups them — what moves you,
    // what you do here, what works anywhere — so the order differs on purpose).
    expect([...rows].sort()).toEqual(w.vm.keyListRowsNow.map((r) => r.action).sort())
    expect(rows).not.toContain('find-anything')
    expect(rows).toContain('agent-next')
    expect(rows).toContain('rail-tab-next')
    expect(rows).toContain('close-top')

    // The one action allowed to see through its own dialog: the chord that
    // opened the list closes it, rather than being swallowed by the modal
    // suppression the list itself triggers.
    await press(w, { key: '/', meta: true })
    expect(document.querySelector('[data-ws-key-list]')).toBe(null)
  })

  it('suppresses every other key while it is open', async () => {
    const { w } = await boot('/workspace/c/s-main')
    await press(w, { key: '/', meta: true })
    await press(w, { key: 'ArrowDown', alt: true })
    expect(showing(w).agent).toBe('scout')
  })

  it('is allowed during a voice call — opening a dialog leaves nothing', async () => {
    const { w } = await boot('/workspace/c/s-main')
    w.vm.voiceCall = { active: true, agentName: 'scout', voiceSessionId: 'v1' }
    await w.vm.$nextTick()
    await press(w, { key: '/', meta: true })
    expect(document.querySelector('[data-ws-key-list]')).not.toBe(null)
  })
})
