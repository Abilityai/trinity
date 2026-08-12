/**
 * #2128 — the Workspace multi-agent picker is gated on a rooms capability the
 * portal principal can actually read.
 *
 * A chat with two or more agents is a room, and `/api/rooms` is served only by a
 * module a community build does not have. The picker offered multi-select
 * anyway, so selecting two agents dead-ended in a generic "Could not start that
 * chat." — an affordance that is always offered and can never work.
 *
 * The signal cannot come from the frontend entitlement store: it reads
 * `/api/settings/feature-flags`, which is platform-JWT gated, and returns [] for
 * any caller without one — i.e. for EVERY external client, including on an
 * entitled instance. So the capability rides on the roster payload, and this
 * file pins the three things that carry the fix:
 *
 *   1. the pure selection decisions (single vs multi), because there is no
 *      component-mount harness in this project (no @vue/test-utils) and
 *      anything that must be tested has to be a pure function;
 *   2. the store contract — the flag is raised only by a successful roster,
 *      lowered only by a definitive refusal, and every room call is gated;
 *   3. source-structure guards for the parts (a prop default, a template branch
 *      ORDER, a v-if term) that no unit test can reach — the same shape as
 *      `agentDetailDeepLink.spec.js` and the Python AST guards.
 *
 * Every guard in (3) strips comments before scanning: a comment explaining what
 * not to write necessarily contains the offending string, and a text scan will
 * flag its own documentation.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

// The store reads localStorage at state construction and installs an axios
// interceptor at import, and vitest runs these in `environment: 'node'` — so
// these shims are a HARNESS REQUIREMENT, not a stylistic choice. Same shape as
// workspaceSessionRenewal.spec.js.
vi.hoisted(() => {
  const store = new Map()
  globalThis.localStorage = {
    getItem: (k) => (store.has(k) ? store.get(k) : null),
    setItem: (k, v) => store.set(k, String(v)),
    removeItem: (k) => store.delete(k),
    clear: () => store.clear(),
  }
  globalThis.window = globalThis.window || { location: { pathname: '/workspace' } }
})

vi.mock('@/stores/auth', async () => {
  const { ref } = await import('vue')
  const authed = ref(false)
  return {
    useAuthStore: () => ({
      get isAuthenticated() { return authed.value },
      get authHeader() { return authed.value ? { Authorization: 'Bearer platform-jwt' } : {} },
    }),
    __setAuthed: (v) => { authed.value = v },
  }
})

vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: {
      request: { use: vi.fn() },
      response: { use: vi.fn() },
    },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import axios from 'axios'
import { applyAgentSelection, collapseSelection } from '@/components/portal/portalUtils'
import { useClientPortalStore, MULTI_AGENT_UNAVAILABLE } from '@/stores/clientPortal'

const PORTAL = fileURLToPath(new URL('../../src/views/Portal.vue', import.meta.url))
const PICKER = fileURLToPath(new URL('../../src/components/portal/PortalAgentPicker.vue', import.meta.url))

/** Strip HTML/JS comments so prose about the rule isn't scanned as code. */
function stripComments(code) {
  return code
    .replace(/<!--[\s\S]*?-->/g, '')      // Vue template comments
    .replace(/\/\*[\s\S]*?\*\//g, '')     // JS block comments
    .replace(/^[ \t]*\/\/.*$/gm, '')      // whole-line // comments
    .replace(/([^:])\/\/.*$/gm, '$1')     // trailing // comments (keep URLs)
}

const portalSource = () => stripComments(readFileSync(PORTAL, 'utf8'))
const pickerSource = () => stripComments(readFileSync(PICKER, 'utf8'))

const rosterResponse = (extra = {}) => ({
  data: { client_email: 'client@example.com', agents: [{ name: 'scout' }], ...extra },
})

function signedInStore() {
  const store = useClientPortalStore()
  store.portalToken = 'portal-token'
  return store
}

// ---------------------------------------------------------------------------
// F1-F9 — the pure selection decisions
// ---------------------------------------------------------------------------

describe('#2128 selection semantics (AC 1, AC 3)', () => {
  it('F1 single-select: picking from nothing selects that one', () => {
    expect(applyAgentSelection([], 'a', { multi: false })).toEqual(['a'])
  })

  it('F2 single-select: picking another REPLACES rather than adding', () => {
    expect(applyAgentSelection(['a'], 'b', { multi: false })).toEqual(['b'])
  })

  it('F3 single-select: picking the selected one clears it', () => {
    // Click-to-clear is why this is `aria-pressed` on a button and not a radio:
    // a radio cannot be un-checked by activating it.
    expect(applyAgentSelection(['a'], 'a', { multi: false })).toEqual([])
  })

  it('F4 multi-select: picking another ADDS (unchanged shipped behaviour)', () => {
    expect(applyAgentSelection(['a'], 'b', { multi: true })).toEqual(['a', 'b'])
  })

  it('F5 multi-select: picking a selected one removes just that one', () => {
    expect(applyAgentSelection(['a', 'b'], 'a', { multi: true })).toEqual(['b'])
  })

  it('F6 never mutates the input array', () => {
    const input = ['a']
    Object.freeze(input)
    expect(() => applyAgentSelection(input, 'b', { multi: true })).not.toThrow()
    expect(() => applyAgentSelection(input, 'a', { multi: true })).not.toThrow()
    expect(input).toEqual(['a'])
  })

  it('F7 defaults to single-select when options are omitted', () => {
    // The fail-safe direction: a caller that forgets gets the surface that
    // works on every edition.
    expect(applyAgentSelection(['a'], 'b')).toEqual(['b'])
  })

  it('F8 collapseSelection keeps the most recent pick', () => {
    expect(collapseSelection(['a', 'b'], { multi: false })).toEqual(['b'])
    expect(collapseSelection(['a', 'b', 'c'], { multi: false })).toEqual(['c'])
  })

  it('F9 collapseSelection is identity when multi, or with nothing to collapse', () => {
    expect(collapseSelection(['a', 'b'], { multi: true })).toEqual(['a', 'b'])
    expect(collapseSelection(['a'], { multi: false })).toEqual(['a'])
    expect(collapseSelection([], { multi: false })).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// F10-F13, F18, F23-F24 — source-structure guards
// ---------------------------------------------------------------------------

describe('#2128 structure guards', () => {
  it('F10 the picker declares `multi` with a fail-safe default of false', () => {
    // Anchored on the `multi` KEY, not on a bare /default:\s*false/ — the file
    // already carries `busy: { type: Boolean, default: false }`, so a loose
    // pattern would pass with the prop deleted entirely.
    const src = pickerSource()
    expect(
      /multi\s*:\s*\{[^}]*default\s*:\s*false[^}]*\}/.test(src),
      'PortalAgentPicker must declare a `multi` prop defaulting to false — a ' +
      'caller that forgets to bind it must get single-select, not this bug again'
    ).toBe(true)
    expect(/multi\s*:\s*\{[^}]*default\s*:\s*true/.test(src)).toBe(false)
  })

  it('F11 Portal.vue binds the capability into the picker', () => {
    expect(portalSource()).toContain(':multi="store.multiAgentChatAvailable"')
  })

  it('F12 the room-refusal branch sits BEFORE <PortalConversation', () => {
    const src = portalSource()
    // The EXACT v-else-if string: indexOf('activeRoomIdFromRoute') alone hits
    // the PortalRoom v-if first and measures nothing.
    const branchAt = src.indexOf('v-else-if="activeRoomIdFromRoute"')
    const convAt = src.indexOf('<PortalConversation')

    // Both asserted present BEFORE comparing: indexOf returns -1 on absence,
    // and expect(-1).toBeLessThan(x) passes — so a deleted branch would go green.
    expect(branchAt, 'the room-URL refusal branch is gone').toBeGreaterThan(-1)
    expect(convAt, '<PortalConversation is gone').toBeGreaterThan(-1)
    expect(
      branchAt,
      'the room-URL refusal branch must precede <PortalConversation. After it, a ' +
      'room URL falls through and opens a DIFFERENT agent\'s conversation ' +
      '(activeAgent defaults to the first roster entry), or renders a blank <main>.'
    ).toBeLessThan(convAt)
  })

  it('F18 the capability is a term in <PortalRoom>\'s own v-if', () => {
    // Nothing else implements AC #4. Someone simplifying the chain could drop
    // this with every other test still green.
    const src = portalSource()
    const roomAt = src.indexOf('<PortalRoom')
    expect(roomAt, '<PortalRoom is gone').toBeGreaterThan(-1)
    const vIfAt = src.indexOf('v-if=', roomAt)
    expect(vIfAt, '<PortalRoom has no v-if').toBeGreaterThan(-1)
    const vIf = src.slice(vIfAt, src.indexOf('\n', vIfAt))
    expect(
      vIf,
      'PortalRoom must not mount when rooms are unavailable — its onMounted ' +
      'issues GET /api/rooms/:id, which is the 404 this issue is about'
    ).toContain('store.multiAgentChatAvailable')
  })

  it('F23 only a cleanly-loaded roster may claim the capability is absent', () => {
    const src = portalSource()
    const branchAt = src.indexOf('v-else-if="activeRoomIdFromRoute"')
    expect(branchAt).toBeGreaterThan(-1)
    const branch = src.slice(branchAt, src.indexOf('<PortalConversation'))

    // The honest-copy split: a transient 5xx on an ENTITLED instance must not
    // render "aren't enabled here", which is a false statement about the
    // operator's build.
    for (const term of ['store.rosterLoaded', 'store.unavailable', 'store.error']) {
      expect(branch, `the room branch must distinguish ${term}`).toContain(term)
    }
    const claimAt = branch.indexOf("isn't available on this instance")
    expect(claimAt, 'the room branch never states the capability is absent').toBeGreaterThan(-1)
    // …and that claim must come last, after all three qualifying arms.
    for (const term of ['store.rosterLoaded', 'store.unavailable', 'store.error']) {
      expect(branch.indexOf(term), `${term} must be tested before the claim`).toBeLessThan(claimAt)
    }
  })

  it('F24 every exit from the stage tests roomId, not sessionId alone', () => {
    // The refusal must not be a room the user cannot leave. Each of these
    // navigated only on `sessionId`, so from /workspace/r/:id nothing moved.
    const src = portalSource()
    for (const fn of ['startBlankChat', 'newChatWithAgent', 'onSignOut']) {
      const at = src.indexOf(`function ${fn}(`)
      expect(at, `${fn} is gone`).toBeGreaterThan(-1)
      const body = src.slice(at, src.indexOf('\n}', at))
      expect(
        body,
        `${fn} navigates only on sessionId — from a room URL it changes nothing, ` +
        `leaving the user parked on the refusal with no way out`
      ).toContain('route.params.roomId')
    }
  })
})

// ---------------------------------------------------------------------------
// F14-F17, F19-F22 — the store contract
// ---------------------------------------------------------------------------

describe('#2128 store gate', () => {
  beforeEach(() => {
    localStorage.clear()
    setActivePinia(createPinia())
    vi.clearAllMocks()
  })

  it('F14 a roster reporting the capability raises the flag', async () => {
    const store = signedInStore()
    axios.get.mockResolvedValueOnce(rosterResponse({ multi_agent_chat_available: true }))
    await store.fetchRoster()
    expect(store.multiAgentChatAvailable).toBe(true)
    expect(store.rosterLoaded).toBe(true)
  })

  it('F15 a payload that omits the field reads as ABSENT', async () => {
    // Strict `=== true`: an older backend, a proxied HTML body, or a string
    // "false" must all fail closed.
    const store = signedInStore()
    for (const payload of [{}, { multi_agent_chat_available: 'false' }, { multi_agent_chat_available: 1 }]) {
      axios.get.mockResolvedValueOnce(rosterResponse(payload))
      await store.fetchRoster()
      expect(store.multiAgentChatAvailable).toBe(false)
    }
  })

  it('F16 createRoom refuses without issuing the request', async () => {
    const store = signedInStore()
    store.multiAgentChatAvailable = false
    await expect(store.createRoom(['a', 'b'], 'x')).rejects.toMatchObject({
      code: 'rooms_unavailable',
      message: MULTI_AGENT_UNAVAILABLE,
    })
    expect(axios.post).not.toHaveBeenCalled()
  })

  it('F17 fetchRooms resolves empty without issuing the request', async () => {
    const store = signedInStore()
    store.multiAgentChatAvailable = false
    await expect(store.fetchRooms()).resolves.toEqual([])
    expect(axios.get).not.toHaveBeenCalled()
  })

  it('F19 a transport failure does NOT lower the flag', async () => {
    // A failed request is not evidence the capability went away. Lowering here
    // would unmount a live room and flash a refusal it then takes back.
    const store = signedInStore()
    axios.get.mockResolvedValueOnce(rosterResponse({ multi_agent_chat_available: true }))
    await store.fetchRoster()
    expect(store.multiAgentChatAvailable).toBe(true)

    axios.get.mockRejectedValueOnce({ message: 'Network Error' })
    await store.fetchRoster()
    expect(store.multiAgentChatAvailable).toBe(true)
    expect(store.rosterLoaded).toBe(true)
  })

  it.each([404, 403])('F20 a definitive %s from the rooms endpoint self-heals the flag', async (status) => {
    // The entitlement can vanish BETWEEN the roster load and the confirm. Without
    // this the picker still 404s mid-session and shows the generic dead-end copy.
    const store = signedInStore()
    store.multiAgentChatAvailable = true
    axios.post.mockRejectedValueOnce({ response: { status } })

    await expect(store.createRoom(['a', 'b'], 'x')).rejects.toMatchObject({
      code: 'rooms_unavailable',
    })
    expect(store.multiAgentChatAvailable).toBe(false)
  })

  it.each([
    [403, 'agent_not_accessible', "You do not have access to agent 'scout'"],
    [404, 'room_not_found', 'Room not found'],
    [410, 'room_closed', 'This room is closed'],
  ])('F20c a coded %i refusal is a DENIAL, not an absent engine', async (status, code, message) => {
    // The status alone is not the signal. On a fully ENTITLED instance the
    // rooms module answers "you can't reach that agent" with a 403 and "you're
    // not in that room" with a uniform 404 — both with its own structured
    // detail. Lowering the capability on those turns one denied request into a
    // session-long false claim about the operator's build, and replaces the
    // only message that tells the user what to do.
    const store = signedInStore()
    store.multiAgentChatAvailable = true
    axios.post.mockRejectedValueOnce({ response: { status, data: { detail: { code, message } } } })

    await expect(store.createRoom(['a', 'b'], 'x')).rejects.toMatchObject({
      response: { data: { detail: { code } } },
    })
    expect(
      store.multiAgentChatAvailable,
      'a refusal the serving module authored proves the module is SERVING'
    ).toBe(true)
  })

  it('F20d a coded refusal keeps the server\'s own words', async () => {
    // The generic path in onPickerConfirm reads `detail.message`; clobbering
    // `err.code`/`err.message` would route it into the rooms-unavailable arm
    // and the real reason would never reach the user.
    const store = signedInStore()
    store.multiAgentChatAvailable = true
    axios.post.mockRejectedValueOnce({
      response: { status: 403, data: { detail: { code: 'agent_not_accessible', message: 'nope' } } },
    })
    const err = await store.createRoom(['a', 'b'], 'x').catch((e) => e)
    expect(err.code).toBeUndefined()
    expect(err.message).not.toBe(MULTI_AGENT_UNAVAILABLE)
  })

  it.each([
    [404, 'Not Found'],                                                   // route never mounted
    [403, "Enterprise feature 'x' is not licensed for this instance."],   // mounted, unlicensed
  ])('F20e a STRING-detail %i is absence and still self-heals', async (status, detail) => {
    const store = signedInStore()
    store.multiAgentChatAvailable = true
    axios.post.mockRejectedValueOnce({ response: { status, data: { detail } } })
    await expect(store.createRoom(['a', 'b'], 'x')).rejects.toMatchObject({
      code: 'rooms_unavailable',
    })
    expect(store.multiAgentChatAvailable).toBe(false)
  })

  it('F20b a 500 from the rooms endpoint does NOT lower the flag', async () => {
    const store = signedInStore()
    store.multiAgentChatAvailable = true
    axios.post.mockRejectedValueOnce({ response: { status: 500 } })
    await expect(store.createRoom(['a', 'b'], 'x')).rejects.toBeTruthy()
    expect(store.multiAgentChatAvailable).toBe(true)
  })

  it('F21 signOut clears both per-session flags', async () => {
    const store = signedInStore()
    axios.get.mockResolvedValueOnce(rosterResponse({ multi_agent_chat_available: true }))
    await store.fetchRoster()

    store.signOut()
    expect(store.multiAgentChatAvailable).toBe(false)
    expect(store.rosterLoaded).toBe(false)
  })

  it('F21b a 401 during roster load leaves the flags cleared', async () => {
    // `endSession()` runs inside the catch, so anything assigned after it —
    // in the catch OR in `finally` — would re-set what the sign-out just cleared.
    const store = signedInStore()
    axios.get.mockResolvedValueOnce(rosterResponse({ multi_agent_chat_available: true }))
    await store.fetchRoster()

    axios.get.mockRejectedValueOnce({ response: { status: 401 } })
    await store.fetchRoster()
    expect(store.multiAgentChatAvailable).toBe(false)
    expect(store.rosterLoaded).toBe(false)
  })

  it('F22 all FIVE room actions are gated, and none issues a request', async () => {
    // Three of these have exactly one caller each, inside a component the render
    // gate stops mounting — "unreachable by construction" is a claim about
    // today's call graph, in a file a sibling issue proposes to rewrite.
    const store = signedInStore()
    store.multiAgentChatAvailable = false

    await expect(store.createRoom(['a', 'b'], 'x')).rejects.toMatchObject({ code: 'rooms_unavailable' })
    await expect(store.fetchRoom('r1')).rejects.toMatchObject({ code: 'rooms_unavailable' })
    await expect(store.postRoomMessage('r1', 'hi')).rejects.toMatchObject({ code: 'rooms_unavailable' })
    await expect(store.addRoomParticipant('r1', 'scout')).rejects.toMatchObject({ code: 'rooms_unavailable' })
    await expect(store.fetchRooms()).resolves.toEqual([])

    expect(axios.get).not.toHaveBeenCalled()
    expect(axios.post).not.toHaveBeenCalled()
  })

  it('F22b with the flag raised, the room actions issue their requests as before', async () => {
    // The gate must not be a one-way ratchet — an entitled instance is unchanged.
    const store = signedInStore()
    store.multiAgentChatAvailable = true
    axios.post.mockResolvedValueOnce({ data: { id: 'r1' } })
    axios.get.mockResolvedValueOnce({ data: { rooms: [{ id: 'r1' }] } })

    await expect(store.createRoom(['a', 'b'], 'x')).resolves.toEqual({ id: 'r1' })
    await expect(store.fetchRooms()).resolves.toEqual([{ id: 'r1', is_room: true }])
    expect(axios.post).toHaveBeenCalledWith(
      '/api/rooms', { name: 'x', agents: ['a', 'b'] }, expect.anything()
    )
  })

  it('F17b fetchAllSessions still degrades to threads-only with no rooms', async () => {
    const store = signedInStore()
    store.multiAgentChatAvailable = false
    store.agents = []
    await expect(store.fetchAllSessions()).resolves.toEqual([])
    expect(axios.get).not.toHaveBeenCalled()
  })
})
