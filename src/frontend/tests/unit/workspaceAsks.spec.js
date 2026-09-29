/**
 * The Workspace asks store contract (ent#364).
 *
 * The feature's central claim is "one row, three renderings, cleared everywhere".
 * In the client that reduces to: all three surfaces read ONE list, and answering
 * removes the row from it. So these tests are about the list — the three components
 * are template work over what is pinned here.
 *
 * Also pinned: a backend that does not serve `/asks` answers 404/403 on every poll,
 * and that has to be silent. Asks are OSS core since ent#428, so this is now the
 * OLDER-backend case rather than the unentitled one — but the guard still earns its
 * place, and without it such an install would log a warning every 20 seconds.
 *
 * ent#429 adds the expiry wording, which lives in `portalUtils` for the same reason
 * everything else decidable does: a sentence composed inside a component is a
 * sentence no test can reach.
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { setActivePinia, createPinia } from 'pinia'

import { expiredLabel, askThreadLink } from '@/components/portal/portalUtils'
import { buildQueueResponse } from '@/utils/operatorQueue'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'

vi.hoisted(() => {
  const mem = new Map()
  globalThis.localStorage = {
    getItem: (k) => (mem.has(k) ? mem.get(k) : null),
    setItem: (k, v) => mem.set(k, String(v)),
    removeItem: (k) => mem.delete(k),
    clear: () => mem.clear(),
  }
  const session = new Map()
  globalThis.sessionStorage = {
    getItem: (k) => (session.has(k) ? session.get(k) : null),
    setItem: (k, v) => session.set(k, String(v)),
    removeItem: (k) => session.delete(k),
    clear: () => session.clear(),
  }
  globalThis.window = globalThis.window || { location: { pathname: '/workspace' } }
})

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: false, authHeader: {}, logout: vi.fn() }),
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

// #2261: workspace calls run on `portalHttp` now, so the stubs go there —
// stubbing the global `axios.get` would silently no longer be the code path.
import { useClientPortalStore, portalHttp } from '@/stores/clientPortal'

const ask = (id, over = {}) => ({
  id, agent_name: 'scout', kind: 'question', priority: 'medium',
  title: 'Ship it?', question: 'Ship it?', options: ['yes', 'no'],
  created_at: '2026-08-19T10:00:00Z', expires_at: null, status: 'pending',
  chat_id: null, ...over,
})

let store

beforeEach(() => {
  localStorage.clear()
  sessionStorage.clear()
  setActivePinia(createPinia())
  vi.clearAllMocks()
  localStorage.setItem('trinity.portalToken', 'portal-token')   // a signed-in client
  store = useClientPortalStore()
})

describe('one list, three renderings', () => {
  it('the count, the agent filter and the full list all come from one fetch', async () => {
    portalHttp.get.mockResolvedValueOnce({
      data: [ask('a1'), ask('a2', { agent_name: 'sage' })],
    })

    await store.fetchAsks()

    expect(portalHttp.get).toHaveBeenCalledTimes(1)
    expect(store.askCount).toBe(2)                       // sidebar
    expect(store.asksForAgent('scout').map((a) => a.id)).toEqual(['a1'])  // agent page
    expect(store.asks).toHaveLength(2)                   // inline / global
  })

  it('answering clears the badge everywhere with no second call; the row stays, now answered', async () => {
    portalHttp.get.mockResolvedValueOnce({ data: [ask('a1'), ask('a2')] })
    await store.fetchAsks()

    portalHttp.post.mockResolvedValueOnce({ data: { ...ask('a1'), status: 'answered', ended_by: 'you' } })
    await store.answerAsk('a1', { response: 'yes' })

    expect(store.askCount).toBe(1)
    // trinity-enterprise#611: an ended ask stays listed (7 days) so the person
    // sees how it ended — replaced by the server's projection, never patched.
    expect(store.asksForAgent('scout').map((a) => [a.id, a.status])).toEqual([['a1', 'answered'], ['a2', 'pending']])
    expect(portalHttp.get).toHaveBeenCalledTimes(1)      // no refetch needed
  })

  it('an expired ask still renders but is not counted', async () => {
    // #1142 deletes terminal rows, so expiry must be VISIBLE while it exists — but
    // it is not something to nag about: the person cannot answer it any more.
    portalHttp.get.mockResolvedValueOnce({
      data: [ask('a1'), ask('a2', { status: 'expired' })],
    })

    await store.fetchAsks()

    expect(store.asks).toHaveLength(2)
    expect(store.askCount).toBe(1)
    expect(store.asksForAgent('scout')).toHaveLength(2)
  })

  it('narrows server-side when an agent is named', async () => {
    portalHttp.get.mockResolvedValueOnce({ data: [ask('a1')] })
    await store.fetchAsks('scout')

    const [, config] = portalHttp.get.mock.calls[0]
    expect(config.params).toEqual({ agent_name: 'scout', include_ended: true })   // trinity-enterprise#611
  })
})

describe('an unentitled build says nothing', () => {
  it('404 leaves the feature unavailable and logs nothing', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    portalHttp.get.mockRejectedValueOnce({ response: { status: 404 } })

    await store.fetchAsks()

    expect(store.asksAvailable).toBe(false)
    expect(store.asks).toEqual([])
    expect(warn).not.toHaveBeenCalled()
    warn.mockRestore()
  })

  it('403 is equally silent — the module is present but not entitled', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    portalHttp.get.mockRejectedValueOnce({ response: { status: 403 } })

    await store.fetchAsks()

    expect(store.asksAvailable).toBe(false)
    expect(warn).not.toHaveBeenCalled()
    warn.mockRestore()
  })

  // trinity-enterprise#610 (PR A0) REVERSED this case. It used to pin "a real
  // failure clears the list rather than showing stale asks" — and with
  // `asksAvailable=false` that blanked every PortalAsks surface and dropped the
  // sidebar badge to zero on a 5xx: the Workspace claiming "nothing needs you"
  // during an outage (#2915). The design-system contract forbids exactly that
  // ("never overwrite the data with a synthetic empty payload in a catch",
  // ent#253). A failure now keeps the last good list and SAYS it failed.
  it('a real failure is reported once, keeps the last good list and says it failed', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    portalHttp.get.mockResolvedValueOnce({ data: [ask('a1')] })
    await store.fetchAsks()

    portalHttp.get.mockRejectedValueOnce({ response: { status: 500 } })
    await store.fetchAsks()

    expect(warn).toHaveBeenCalledTimes(1)
    expect(store.asks.map((a) => a.id)).toEqual(['a1'])
    expect(store.askCount).toBe(1)
    expect(store.asksFailed).toBe(true)
    expect(store.asksLoaded).toBe(true)
    // Left as it was: flipping it would blank PortalAsks (its render gate).
    expect(store.asksAvailable).toBe(true)
    warn.mockRestore()
  })

  it('a 503 asks_unavailable or a network error before any success is a failure, not absence', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    portalHttp.get.mockRejectedValueOnce({
      response: { status: 503, data: { detail: { code: 'asks_unavailable' } } },
    })
    await store.fetchAsks()

    expect(store.asksFailed).toBe(true)
    expect(store.asksLoaded).toBe(false)
    expect(store.asks).toEqual([])

    portalHttp.get.mockRejectedValueOnce(new Error('Network Error'))
    await store.fetchAsks()
    expect(store.asksFailed).toBe(true)
    expect(store.asksLoaded).toBe(false)
    warn.mockRestore()
  })

  it('a success after a failure clears the failure and stamps the load time', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    portalHttp.get.mockRejectedValueOnce({ response: { status: 500 } })
    await store.fetchAsks()
    expect(store.asksFailed).toBe(true)
    expect(store.asksLoadedAt).toBe(null)

    portalHttp.get.mockResolvedValueOnce({ data: [ask('a2')] })
    await store.fetchAsks()

    expect(store.asksFailed).toBe(false)
    expect(store.asksLoaded).toBe(true)
    expect(store.asksAvailable).toBe(true)
    expect(typeof store.asksLoadedAt).toBe('number')
    expect(store.asks.map((a) => a.id)).toEqual(['a2'])
    warn.mockRestore()
  })

  it('a 404 after a success is absence again, not a failure', async () => {
    portalHttp.get.mockResolvedValueOnce({ data: [ask('a1')] })
    await store.fetchAsks()

    portalHttp.get.mockRejectedValueOnce({ response: { status: 404 } })
    await store.fetchAsks()

    expect(store.asksAvailable).toBe(false)
    expect(store.asks).toEqual([])
    expect(store.asksFailed).toBe(false)
    // A surface that no longer exists has no "loaded" verdict either.
    expect(store.asksLoaded).toBe(false)
  })

  // Keeping the last good list on a failure makes the list session-scoped
  // state: it must not outlive a sign-out, or the next client on the same
  // browser sees the previous client's asks on their first failed read.
  it('a sign-out drops the kept SESSION list too (round-3 /cso #2)', () => {
    // fetchAllSessions returns `lastSessions` on a failed refresh — the same
    // last-good-list shape as the asks, with the same next-client leak.
    store.lastSessions = [{ id: 's1', agent_name: 'scout' }]
    store.sessionsFailed = true
    store.signOut()
    expect(store.lastSessions).toEqual([])
    expect(store.sessionsFailed).toBe(false)
  })

  it('a sign-out drops the kept list, so the next client never inherits it', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    portalHttp.get.mockResolvedValueOnce({ data: [ask('a1')] })
    await store.fetchAsks()
    expect(store.asks.map((a) => a.id)).toEqual(['a1'])

    store.signOut()
    expect(store.asks).toEqual([])
    expect(store.asksAvailable).toBe(false)
    expect(store.asksLoaded).toBe(false)
    expect(store.asksFailed).toBe(false)
    expect(store.asksLoadedAt).toBe(null)

    store.portalToken = 'another-client'
    portalHttp.get.mockRejectedValueOnce({ response: { status: 503 } })
    await store.fetchAsks()

    expect(store.asks).toEqual([])
    expect(store.askCount).toBe(0)
    expect(store.asksFailed).toBe(true)
    warn.mockRestore()
  })

  // A 401 is not an outage, it is the end of the session. The 20s poll runs
  // `fetchAsks`, never the roster, and `portalHttp`'s 401 interceptor acts only
  // for a platform session, so for a portal-token client this branch is the
  // ONLY thing that ends it. Counted as `asksFailed`, the expired client's list
  // stayed on screen indefinitely.
  it('a 401 on a portal-token session ends it, as the roster does, and drops the list', async () => {
    const warn = vi.spyOn(console, 'warn').mockImplementation(() => {})
    portalHttp.get.mockResolvedValueOnce({ data: [ask('a1')] })
    await store.fetchAsks()
    expect(store.asks.map((a) => a.id)).toEqual(['a1'])

    portalHttp.get.mockRejectedValueOnce({ response: { status: 401 } })
    const out = await store.fetchAsks()

    expect(out).toEqual([])
    expect(store.asks).toEqual([])
    expect(store.askCount).toBe(0)
    expect(store.asksFailed).toBe(false)
    expect(store.portalToken).toBe(null)
    expect(store.isClientSignedIn).toBe(false)
    expect(store.sessionExpired).toBe(true)
    expect(store.resumePath).toBe(window.location.pathname)
    expect(localStorage.getItem('trinity.portalToken')).toBe(null)
    // Not an outage: nothing to warn about.
    expect(warn).not.toHaveBeenCalled()
    warn.mockRestore()
  })

  it('a read that resolves after sign-out does not write the old list back', async () => {
    let resolve
    portalHttp.get.mockReturnValueOnce(new Promise((r) => { resolve = r }))
    const inFlight = store.fetchAsks()

    store.signOut()
    resolve({ data: [ask('a1')] })
    await inFlight

    expect(store.asks).toEqual([])
    expect(store.asksLoaded).toBe(false)
    expect(store.asksAvailable).toBe(false)
  })

  it('a read that resolves after ANOTHER client signed in is dropped too', async () => {
    let resolve
    portalHttp.get.mockReturnValueOnce(new Promise((r) => { resolve = r }))
    const inFlight = store.fetchAsks()

    store.signOut()
    store.portalToken = 'another-client'
    resolve({ data: [ask('a1')] })
    await inFlight

    expect(store.asks).toEqual([])
    expect(store.asksLoaded).toBe(false)
  })

  it('does not poll at all when nobody is signed in', async () => {
    localStorage.clear()
    setActivePinia(createPinia())
    const anon = useClientPortalStore()

    await anon.fetchAsks()

    expect(portalHttp.get).not.toHaveBeenCalled()
  })
})

describe('answering surfaces the backend refusal', () => {
  it('propagates the error and keeps the ask, so nothing looks resolved that is not', async () => {
    portalHttp.get.mockResolvedValueOnce({ data: [ask('a1')] })
    await store.fetchAsks()

    portalHttp.post.mockRejectedValueOnce({
      response: { status: 409, data: { detail: { code: 'expired', message: 'This ask expired before it was answered.' } } },
    })

    await expect(store.answerAsk('a1', { response: 'yes' })).rejects.toBeTruthy()
    expect(store.askCount).toBe(1)
  })
})

// --- expiry wording (ent#429) -------------------------------------------------
//
// AC #3 asks for an expired ask to be visibly expired "with the time it lapsed".
// The window matters: the #1142 sweep DELETES terminal rows, so between lapsing
// and being swept this row is the only evidence the question was ever asked, and
// "this expired" without a WHEN cannot tell an hour ago from last March.
describe('expiredLabel', () => {
  const T = Date.parse('2026-08-21T12:00:00Z')

  it('says how long ago, in the units a reader thinks in', () => {
    expect(expiredLabel('2026-08-21T11:59:40Z', T)).toContain('moments ago')
    expect(expiredLabel('2026-08-21T11:30:00Z', T)).toContain('30m ago')
    expect(expiredLabel('2026-08-21T09:00:00Z', T)).toContain('3h ago')
    expect(expiredLabel('2026-08-18T12:00:00Z', T)).toContain('3d ago')
  })

  it('switches to a date once "Nd ago" stops meaning anything', () => {
    const label = expiredLabel('2026-06-01T12:00:00Z', T)
    expect(label).not.toContain('d ago')
    expect(label).toContain('expired on')
  })

  it('always still says it expired before it was answered', () => {
    for (const iso of ['2026-08-21T11:30:00Z', '2026-06-01T12:00:00Z', null, 'nonsense']) {
      expect(expiredLabel(iso, T)).toContain('before it was answered')
    }
  })

  it('degrades to the bare sentence rather than inventing a time', () => {
    // A wrong time is worse than no time: the reader would act on it.
    expect(expiredLabel(null, T)).toBe('This expired before it was answered.')
    expect(expiredLabel('nonsense', T)).toBe('This expired before it was answered.')
    expect(expiredLabel(undefined, T)).toBe('This expired before it was answered.')
  })

  it('says nothing about timing when the row claims to expire in the future', () => {
    // Not actually expired, so something upstream disagrees with us — say less.
    expect(expiredLabel('2026-08-22T12:00:00Z', T)).toBe('This expired before it was answered.')
  })
})

// --- the thread link, and the surface that never rendered (ent#429) -----------
describe('askThreadLink', () => {
  it('links to the thread an ask was raised against', () => {
    expect(askThreadLink({ chat_id: 'sess-1' }, null)).toBe('sess-1')
    expect(askThreadLink({ chat_id: 'sess-1' }, 'sess-2')).toBe('sess-1')
  })

  it('offers nothing when the reader is already there', () => {
    expect(askThreadLink({ chat_id: 'sess-1' }, 'sess-1')).toBeNull()
  })

  it('offers nothing for a homeless ask', () => {
    // Pre-ent#429 rows, and any ask whose attachment could not be resolved.
    expect(askThreadLink({ chat_id: null }, 'sess-1')).toBeNull()
    expect(askThreadLink({}, null)).toBeNull()
    expect(askThreadLink(undefined, null)).toBeNull()
  })
})

describe('the inline-in-chat surface filters by agent NAME', () => {
  // ent#429. `PortalConversation` passes `props.agent`, which is the agent
  // OBJECT `{name, owner, ...}`, and `asksForAgent` compares it against the
  // string `a.agent_name` — so it matched nothing and the third of ent#364's
  // three surfaces had never rendered. It failed silently: an empty ask list is
  // a legitimate state, and the wrapper is `v-if="agentAsks.length"`.
  //
  // Asserted against the SOURCE because this repo's vitest runs in `node` with
  // no component-mount harness, so a prop passed wrongly is otherwise unreachable
  // by any test. Crude, but it is the difference between pinning this and not.
  it('the store getter matches on the name, and only the name', () => {
    setActivePinia(createPinia())
    const store = useClientPortalStore()
    store.asks = [{ id: 'a1', agent_name: 'scout', status: 'pending' }]

    expect(store.asksForAgent('scout')).toHaveLength(1)
    expect(store.asksForAgent({ name: 'scout' })).toHaveLength(0)
  })

  it('the conversation passes a name, not the agent object', async () => {
    const fs = await import('node:fs')
    const src = fs.readFileSync(
      new URL('../../src/components/portal/PortalConversation.vue', import.meta.url),
      'utf8',
    )
    expect(src).toContain('store.asksForAgent(props.agent.name)')
    expect(src).not.toContain('store.asksForAgent(props.agent)')
    expect(src).toMatch(/:agent-name="agent\.name"/)
  })
})


describe('#2375 — the wire shape of an answer', () => {
  // The component cannot be mounted (vitest is environment:'node' with no
  // harness), so the rule splits: the PAYLOAD is pinned through the shared
  // builder the component calls, and the WIRING is source-asserted below.
  it('a typed answer to a question travels as the DECISION, never as a note', () => {
    expect(buildQueueResponse({ kind: 'question', answer: '  deploy tuesday  ' }))
      .toEqual({ response: 'deploy tuesday', response_text: null })
  })

  it('an approval sends the picked option verbatim, with the note riding beside it', () => {
    expect(buildQueueResponse({ kind: 'approval', option: 'Approve', note: ' after 6pm ' }))
      .toEqual({ response: 'Approve', response_text: 'after 6pm' })
  })

  it('an approval with nothing picked sends NOTHING — Send stays disarmed', () => {
    expect(buildQueueResponse({ kind: 'approval', option: null, note: 'just a note' }))
      .toBeNull()
  })

  it('an alert acknowledges', () => {
    expect(buildQueueResponse({ kind: 'acknowledge' }))
      .toEqual({ response: 'acknowledged', response_text: null })
  })

  it('the store forwards exactly what the builder produced', async () => {
    portalHttp.get.mockResolvedValueOnce({ data: [ask('a1')] })
    await store.fetchAsks()
    portalHttp.post.mockResolvedValueOnce({ data: { ...ask('a1'), status: 'responded' } })

    const body = buildQueueResponse({ kind: 'question', answer: 'deploy tuesday' })
    await store.answerAsk('a1', { response: body.response, responseText: body.response_text })

    const [, posted] = portalHttp.post.mock.calls[0]
    expect(posted).toEqual({ response: 'deploy tuesday', response_text: null })
  })

  it('#2915: an acknowledged resend carries the override; nothing else changes shape', async () => {
    portalHttp.get.mockResolvedValueOnce({ data: [ask('a1')] })
    await store.fetchAsks()
    portalHttp.post.mockResolvedValueOnce({ data: { ...ask('a1'), status: 'responded' } })
    await store.answerAsk('a1', { response: 'deploy tuesday', responseText: null, acknowledgeDivergence: true })
    const [, posted] = portalHttp.post.mock.calls[0]
    expect(posted).toEqual({ response: 'deploy tuesday', response_text: null, acknowledge_divergence: true })
  })
})

describe('#2375 — the panel goes through the shared module (source-asserted)', () => {
  const sfc = readFileSync(
    fileURLToPath(new URL('../../src/components/portal/PortalAsks.vue', import.meta.url)),
    'utf8',
  )

  it('imports the one home of the payload, the controls rule and the labels', () => {
    expect(sfc).toMatch(
      /import \{ optionsOf, queueResponseKind, buildQueueResponse, queueTypeLabel \} from '@\/utils\/operatorQueue'/,
    )
  })

  it('never one-taps an option straight into an answer', () => {
    // The exact regression: option buttons used to call answer() directly, so a
    // tap on an irreversible decision had no note and no explicit submit.
    expect(sfc).not.toMatch(/@click="answer\(/)
    // A tap only arms Send: the option click goes through `pick`, which writes
    // the pick and moves focus to the note (ent#610 §3g B2) — and never sends.
    expect(sfc).toMatch(/@click="pick\(ask, opt\)"/)
    const pickBody = sfc.match(/function pick\(ask, opt\) \{([\s\S]*?)\n\}/)?.[1] || ''
    expect(pickBody).toMatch(/picks\[ask\.id\] = /)
    expect(pickBody).not.toMatch(/submit|answerAsk/)
    expect(sfc).toMatch(/:disabled="busyId === ask\.id \|\| !picks\[ask\.id\]"/)
  })

  it('builds every submission through buildQueueResponse', () => {
    expect(sfc).toMatch(/const body = buildQueueResponse\(/)
    expect(sfc).not.toMatch(/responseText: text \|\| null/)
  })

  it('renders the shared type labels, not a third bespoke set', () => {
    expect(sfc).toMatch(/queueTypeLabel\(kind\)/)
    expect(sfc).not.toMatch(/Approval needed/)
  })

  it('an approval carries an optional note field', () => {
    // trinity-enterprise#610: ids are `${tid.prefix}-…` now (default prefix
    // 'portal-ask', so the rendered id is unchanged — mounted in
    // portalAsksTestidPrefix.mount.spec.js).
    expect(sfc).toMatch(/\$\{tid\.prefix\}-note-\$\{ask\.id\}/)
  })
})
