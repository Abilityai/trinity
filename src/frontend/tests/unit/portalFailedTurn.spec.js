/**
 * The Workspace says WHY a turn failed, and offers Retry only where re-sending
 * can work (#2320).
 *
 * The client's give-up path was built entirely on absence — no reply, no
 * in-flight marker — so a turn the backend had diagnosed precisely ("your
 * subscription is out of credit") was reported to the user as "we've lost track
 * of this turn", with no Retry. #2320 gives the server a way to say what
 * happened and gives the client two decisions to make with it:
 *
 *   1. **Is this verdict mine?** A thread outlives its turns, and the record
 *      lives for 15 minutes. Believing a verdict without checking the execution
 *      id would report turn N's failure as turn N+1's — a new way to lie about
 *      the same thing.
 *
 *   2. **May the user re-send?** `retryable` is the server's answer where it has
 *      one, and the pre-existing `!lost` rule everywhere else. Getting this
 *      backwards costs money in one direction (#2120 double-billing) and a
 *      dead-ended user in the other (#2150).
 *
 * WHAT THIS FILE CAN AND CANNOT DO. There is no component-mount harness in this
 * project — `@vue/test-utils` is not a dependency and vitest runs
 * `environment: 'node'` with no DOM — so `PortalConversation.vue` cannot be
 * mounted, and adding a dependency to test one change is not the trade. The two
 * decisions above are single expressions inside that component, so rather than
 * re-implementing them here (a copy that agrees with itself and proves nothing)
 * these tests EXTRACT THE REAL EXPRESSION FROM THE SOURCE and evaluate it. What
 * runs is the shipped code; only the inputs are ours. Everything that is genuine
 * exported code — the store's `fetchHistory` mapping — is tested directly, and
 * the wiring around the two expressions is pinned with the source-structure
 * guards this repo already uses for exactly this gap (see
 * `portalComposerWiring.spec.js`).
 */
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { setActivePinia, createPinia } from 'pinia'

// Harness requirement, not style: the store reads localStorage at state
// construction and installs an axios interceptor at import, and vitest runs this
// in `environment: 'node'`. Same shape as clientPortalBatchSessions.spec.js.
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

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: false, authHeader: {} }),
}))

vi.mock('axios', () => {
  const inst = {
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import axios from 'axios'
import { stripComments } from './helpers/stripComments'
import { useClientPortalStore } from '@/stores/clientPortal'

const CONVERSATION = fileURLToPath(
  new URL('../../src/components/portal/PortalConversation.vue', import.meta.url))
// Comments necessarily quote the very patterns these guards scan for, so they
// are stripped before any structural assertion (helpers/stripComments).
const code = () => stripComments(readFileSync(CONVERSATION, 'utf8'))

beforeEach(() => {
  setActivePinia(createPinia())
  vi.clearAllMocks()
})

function signedInStore() {
  const store = useClientPortalStore()
  store.portalToken = 'portal-token'
  return store
}

const OUTCOME = {
  execution_id: 'exec-abc',
  category: 'auth',
  message: "The agent has reached its usage limit and can't respond right now.",
  retryable: false,
}

// ---------------------------------------------------------------------------
// The verdict crosses the store boundary
// ---------------------------------------------------------------------------

describe('#2320 — fetchHistory carries the verdict to the component', () => {
  it('maps last_turn_outcome onto lastTurnOutcome, verbatim', async () => {
    const store = signedInStore()
    axios.get.mockResolvedValue({
      data: { session_id: 's1', messages: [], last_turn_outcome: OUTCOME },
    })

    const out = await store.fetchHistory('scribe', 's1')

    // Verbatim: the component reads `.execution_id`, `.message` and
    // `.retryable` off this object, so a renamed or reshaped key here silently
    // disables the whole feature rather than failing anywhere visible.
    expect(out.lastTurnOutcome).toEqual(OUTCOME)
  })

  it('reports no verdict as null, whether the server sends null or nothing', async () => {
    const store = signedInStore()

    axios.get.mockResolvedValue({ data: { session_id: 's1', messages: [], last_turn_outcome: null } })
    expect((await store.fetchHistory('scribe', 's1')).lastTurnOutcome).toBeNull()

    // The absent case is the OLDER BACKEND, which must degrade to the pre-#2320
    // lost/idle handling rather than to `undefined` — the component's
    // `if (outcome)` would cope either way, but `data.lastTurnOutcome` is also
    // read directly in `awaitPersistedReply`, where a normalized null is what
    // keeps the two call sites agreeing.
    axios.get.mockResolvedValue({ data: { session_id: 's1', messages: [] } })
    const out = await store.fetchHistory('scribe', 's1')
    expect(out.lastTurnOutcome).toBeNull()
    expect('lastTurnOutcome' in out).toBe(true)
  })

  it('does not disturb the fields the reply poll already reads', async () => {
    // `awaitPersistedReply` reads messages, inFlightExecutionId and the budget
    // from this same response — the verdict rides that poll, it does not
    // replace it.
    const store = signedInStore()
    axios.get.mockResolvedValue({
      data: {
        session_id: 's1',
        messages: [{ role: 'assistant', content: 'hi' }],
        in_flight_execution_id: 'exec-abc',
        in_flight_wait_budget_seconds: 555,
        last_turn_outcome: OUTCOME,
      },
    })

    const out = await store.fetchHistory('scribe', 's1')

    expect(out.sessionId).toBe('s1')
    expect(out.messages).toHaveLength(1)
    expect(out.inFlightExecutionId).toBe('exec-abc')
    expect(out.inFlightWaitBudgetSeconds).toBe(555)
    expect(out.lastTurnOutcome).toEqual(OUTCOME)
  })
})

// ---------------------------------------------------------------------------
// "Is this verdict mine?" — the real predicate, evaluated
// ---------------------------------------------------------------------------

/** Lift `awaitPersistedReply`'s outcome guard out of the source and compile it. */
function outcomeGuard() {
  const m = code().match(
    /if \((outcome[^)]*)\)\s*\{\s*\n\s*return \{ failed: true, outcome \}/)
  expect(m, 'awaitPersistedReply no longer guards the outcome before believing it')
    .not.toBeNull()
  // eslint-disable-next-line no-new-func
  return new Function('outcome', 'executionId', `return !!(${m[1]})`)
}

describe('#2320 — a verdict is only believed when it names THIS turn', () => {
  it('accepts the verdict for the execution being waited on', () => {
    expect(outcomeGuard()(OUTCOME, 'exec-abc')).toBe(true)
  })

  it('ignores a verdict left by an earlier turn on the same thread', () => {
    // The record lives 15 minutes and the thread outlives every turn on it. The
    // backend clears it at dispatch, but that clear is best-effort (Redis down
    // ⇒ no-op), so this check is the half that cannot fail open.
    expect(outcomeGuard()(OUTCOME, 'exec-NEXT')).toBe(false)
  })

  it('ignores a verdict when there is nothing to match it against', () => {
    // `executionId` defaults to null — the synchronous-send fallback path calls
    // `awaitPersistedReply` without one. Believing an unmatchable verdict there
    // would attribute a stale failure to a turn that may be running fine.
    const guard = outcomeGuard()
    expect(guard(OUTCOME, null)).toBe(false)
    expect(guard(OUTCOME, undefined)).toBe(false)
  })

  it('is inert when the server offered no verdict', () => {
    const guard = outcomeGuard()
    expect(guard(null, 'exec-abc')).toBe(false)
    expect(guard(undefined, 'exec-abc')).toBe(false)
    // An outcome with no id can never match — it must not be believed by
    // accident when `executionId` is also missing.
    expect(guard({ message: 'x' }, undefined)).toBe(false)
  })
})

describe('#2320 — where the verdict is read in the poll loop', () => {
  it('lets a delivered reply win over a verdict', () => {
    // A turn that ANSWERED is not a failure, whatever a stale record says. The
    // reply check must come first, or a late-arriving reply is discarded in
    // favour of an older verdict.
    const body = code().split('async function awaitPersistedReply')[1]
    const replyAt = body.indexOf('return { response: last.content')
    const outcomeAt = body.indexOf('return { failed: true, outcome }')
    expect(replyAt).toBeGreaterThan(-1)
    expect(outcomeAt).toBeGreaterThan(replyAt)
  })

  it('reads the verdict before the idle timer, not after it', () => {
    // A diagnosed failure is authoritative regardless of the marker, so it is
    // reported at once. Behind the idle branch it would be delayed by a whole
    // give-up window while the client pretends not to know.
    const body = code().split('async function awaitPersistedReply')[1]
    const outcomeAt = body.indexOf('return { failed: true, outcome }')
    const idleAt = body.indexOf('if (data.inFlightExecutionId)')
    expect(idleAt).toBeGreaterThan(-1)
    expect(outcomeAt).toBeLessThan(idleAt)
  })

  it('is given the execution id at every call site that has one', () => {
    // The guard above is only reachable when the caller passes an id. Both
    // waiting surfaces — the live send and the reattach after a reload — must.
    // `await awaitPersistedReply(` matches the CALLS and not the `async
    // function` definition — a filter that also swept up the definition would
    // pass on its own parameter list and prove nothing.
    const calls = [...code().matchAll(/await awaitPersistedReply\(([\s\S]*?)\)\n/g)]
      .map((m) => m[1])
    expect(calls.length, 'expected the send and the reattach to be the two waiting surfaces')
      .toBe(2)
    calls.forEach((args) => {
      expect(args, `a caller waits without naming its turn: ${args}`)
        .toMatch(/execution_?[iI]d/)
    })
  })
})

// ---------------------------------------------------------------------------
// "May the user re-send?" — the real expression, evaluated
// ---------------------------------------------------------------------------

/** Lift the `retryable:` resolution out of the settle path and compile it.
 *
 * THE RULE is that `send()` and `retry()` cannot disagree about retryability —
 * a Retry offered on the first send and withheld on the retry (or the reverse)
 * is worse than either rule applied consistently.
 *
 * This guard used to encode that rule as "there are at least two `retryable:`
 * expressions and they are textually identical", which is one SHAPE that
 * satisfies it, not the rule itself. ent#155 made both callers settle through a
 * single `settleDelivery`, so there is now exactly one expression — the rule
 * holds *by construction*, and the old assertion failed on the change that made
 * it unbreakable. A guard pinned to one literal source shape rejects the next
 * legitimate edit, and the tempting fix is to delete the rule; so the rule is
 * kept and the shape widened.
 *
 * Both shapes are admissible, and each carries its own proof obligation:
 *
 *   - **shared** (today): one expression, and every `deliver()` caller reaches
 *     it through the same settle function — checked here, because with one
 *     expression the textual-agreement check is vacuous and something has to
 *     take over its job.
 *   - **duplicated** (pre-ent#155): two or more expressions, all identical.
 */
function retryRules() {
  const src = code()
  const exprs = src
    .split('\n')
    .filter((l) => l.includes('markFailed(') && l.includes('retryable:') && l.includes('res?'))
    .map((l) => l.match(/retryable:\s*(.+?)\s*\}\)/)[1])
  expect(exprs.length, 'the retryability resolution must exist in source')
    .toBeGreaterThanOrEqual(1)
  // Whatever the shape, no two resolutions may differ.
  expect(new Set(exprs).size, `retryability resolutions disagree: ${[...new Set(exprs)].join(' | ')}`)
    .toBe(1)

  if (exprs.length === 1) {
    // Shared shape: agreement is structural, so prove the structure. Both
    // `deliver()` callers must hand off to the one settle function — if a third
    // caller appeared, or `retry()` went back to marking failure itself, the
    // single expression would silently stop covering it.
    const settles = src.match(/const res = await deliver\([^)]*\)\n\s*settleDelivery\(/g) || []
    const delivers = src.match(/await deliver\(/g) || []
    expect(settles.length, 'every deliver() caller must settle through settleDelivery')
      .toBe(delivers.length)
    expect(delivers.length, 'send() and retry() are the two deliver() callers')
      .toBeGreaterThanOrEqual(2)
  }

  // eslint-disable-next-line no-new-func
  return new Function('res', `return (${exprs[0]})`)
}

describe('#2320 — retryability resolution', () => {
  // The three shapes `deliver()` can return, pinned to the source below by
  // `deliver() enumerates every give-up it returns`.
  const SERVER_VERDICT_RETRYABLE = { failed: true, error: 'busy', retryable: true }
  const SERVER_VERDICT_FINAL = { failed: true, error: 'usage limit', retryable: false }
  const LOST = { lost: true, retryable: false, error: "we've lost track of this turn" }
  const DISPATCH_ERROR = { error: 'Network Error' }

  it('lets a server verdict decide, in both directions', () => {
    const resolve = retryRules()
    // The load-bearing half: `busy` and `capacity` are refused BEFORE anything
    // reaches the agent, so re-sending is exactly the right action — and the
    // pre-#2320 rule would have withheld it.
    expect(resolve(SERVER_VERDICT_RETRYABLE)).toBe(true)
    expect(resolve(SERVER_VERDICT_FINAL)).toBe(false)
  })

  it('keeps Retry for a dispatch that never reached the server', () => {
    // No `retryable`, no `lost` — `deliver`'s catch. Nothing was created, so
    // re-sending is safe, and this is the ONE path #2320 deliberately leaves on
    // the old rule.
    expect(retryRules()(DISPATCH_ERROR)).toBe(true)
  })

  it('still withholds Retry from a turn we merely lost sight of', () => {
    // #2133/#2120, intact: a lost turn probably RAN and was billed. Note this
    // holds twice over — `retryable: false` is explicit AND `!res.lost` would
    // also say false. The explicit field is what makes the rule survive a
    // future give-up that is not spelled `lost`.
    expect(retryRules()(LOST)).toBe(false)
    expect(retryRules()({ lost: true })).toBe(false)
  })

  it('uses ?? and not ||, so an explicit false is honoured', () => {
    // The bug this shape prevents: `res.retryable || !res.lost` would read a
    // server verdict of `false` as "unset" and fall through to `!lost`, which
    // for a `failed` result is TRUE — handing a Retry to the exact turn the
    // server just said must not be re-sent.
    const resolve = retryRules()
    expect(resolve({ failed: true, retryable: false })).toBe(false)
    expect(code()).toMatch(/retryable:\s*res\?\.retryable \?\? !res\?\.lost/)
  })

  it('survives a null/undefined result without throwing', () => {
    // `deliver()` returns `true` on success and the call sites guard on
    // `res !== true`, but the expression uses optional chaining precisely so a
    // future return of undefined degrades to "retryable" rather than crashing
    // the send handler mid-way and leaving the row unmarked.
    const resolve = retryRules()
    expect(resolve(undefined)).toBe(true)
    expect(resolve(null)).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Wiring the two expressions cannot reach
// ---------------------------------------------------------------------------

describe('#2320 — deliver() enumerates every give-up it returns', () => {
  const deliverBody = () => code().split('async function deliver(')[1].split('\nasync function ')[0]

  it('returns the server verdict with its own copy and its own retryability', () => {
    expect(deliverBody()).toMatch(
      /return \{ failed: true, error: data\.outcome\.message,\s*retryable: data\.outcome\.retryable === true \}/)
  })

  it('marks every lost branch non-retryable explicitly', () => {
    // Explicitly, not by omission: `!res?.lost` already yields false for these,
    // but stating it keeps the two rules from silently diverging and makes each
    // give-up self-describing at the point it is decided.
    const lostReturns = deliverBody().split('return {').filter((s) => s.startsWith(' lost: true'))
    expect(lostReturns.length).toBeGreaterThanOrEqual(3)
    lostReturns.forEach((r) => expect(r.slice(0, 200)).toMatch(/retryable: false/))
  })

  it('no longer throws for the idle case, which is how it lost its message', () => {
    // #2150 believed the `!data` branch preserved a Retry for a genuine
    // no-answer, but `awaitPersistedReply` never returned null — `idle` fell
    // into the lost-track copy instead. Both now return, distinctly.
    const body = deliverBody()
    expect(body).toMatch(/if \(data\?\.lost && data\.idle\)/)
    expect(body).not.toMatch(/throw new Error\('The agent did not reply/)
  })
})

describe('#2320 — the verdict outlives the tab that sent the turn', () => {
  it('applies a verdict found on load, when nothing is running', () => {
    // Reopening the thread previously showed the user's own question with no
    // reply and no explanation — a question the agent appeared to have ignored.
    const body = code().split('async function loadThread(')[1].split('\nfunction ')[0]
    expect(body).toMatch(/lastTurnOutcome/)
    expect(body).toMatch(/if \(outcome\) markLastUserTurnFailed\(outcome\)/)
  })

  it('reattaches instead when a turn IS running, and does not also mark it failed', () => {
    // Order matters: the in-flight branch must return, or a live turn gets the
    // previous turn's verdict stamped on its message the moment the thread opens.
    const body = code().split('async function loadThread(')[1].split('\nfunction ')[0]
    const reattachAt = body.indexOf('if (inFlight)')
    const outcomeAt = body.indexOf('if (outcome)')
    expect(reattachAt).toBeGreaterThan(-1)
    expect(reattachAt).toBeLessThan(outcomeAt)
    expect(body.slice(reattachAt, outcomeAt)).toMatch(/await reattach\([^)]*\); return \}/)
  })

  it('renders a failure on the reattach path, which used to render nothing', () => {
    // This surface checked ONLY for a reply, so a failed or lost turn ended with
    // the spinner simply stopping: no message, no Retry. A client that refreshed
    // mid-turn learned LESS than one that stayed, which is backwards.
    const body = code().split('async function reattach(')[1].split('\nfunction ')[0]
    expect(body).toMatch(/if \(data\?\.failed\)/)
    expect(body).toMatch(/markLastUserTurnFailed\(data\.outcome\)/)
    expect(body).toMatch(/else if \(data\?\.lost\)/)
  })

  // The real function, lifted out of the SFC and run against injected fakes —
  // the shipped code executes, only the inputs are ours. A regex over the body
  // would pin an implementation shape; this pins the behaviour that matters.
  function loadMarkLastUserTurnFailed() {
    const src = code()
    const start = src.indexOf('function markLastUserTurnFailed(')
    expect(start).toBeGreaterThan(-1)
    const body = src.slice(start, src.indexOf('\n}', start) + 2)
    const calls = []
    const messages = { value: [] }
    const markFailed = (i, content, error, opts) => calls.push({ i, content, error, opts })
    // eslint-disable-next-line no-new-func
    const fn = new Function('messages', 'markFailed',
                            `${body}; return markLastUserTurnFailed`)(messages, markFailed)
    return { fn, calls, messages }
  }

  it('marks the unanswered tail, carrying the verdict retryable bit', () => {
    const { fn, calls, messages } = loadMarkLastUserTurnFailed()
    messages.value = [
      { role: 'user', content: 'first' },
      { role: 'assistant', content: 'answered' },
      { role: 'user', content: 'the one that failed' },
    ]
    fn({ message: 'The agent is busy.', retryable: true })
    expect(calls).toHaveLength(1)
    expect(calls[0].i).toBe(2)
    expect(calls[0].content).toBe('the one that failed')
    expect(calls[0].error).toBe('The agent is busy.')
    expect(calls[0].opts).toEqual({ retryable: true })
  })

  it('marks NOTHING when the last turn was already answered', () => {
    // The regression this guards: two raise sites (`portal_chat`'s roster 404
    // and its availability refusal) fire BEFORE `_persist_user_turn`, recording
    // a verdict while leaving no user row of their own. Walking back to "the
    // last user message" would pin that verdict onto an EARLIER, answered turn
    // — reporting a successful exchange as failed and offering a Retry that
    // re-sends it.
    const { fn, calls, messages } = loadMarkLastUserTurnFailed()
    messages.value = [
      { role: 'user', content: 'asked' },
      { role: 'assistant', content: 'and answered' },
    ]
    fn({ message: 'Something went wrong.', retryable: false })
    expect(calls).toEqual([])
  })

  it('defaults retryable to false for any non-true verdict', () => {
    const { fn, calls, messages } = loadMarkLastUserTurnFailed()
    messages.value = [{ role: 'user', content: 'q' }]
    for (const outcome of [{ message: 'x' }, { message: 'x', retryable: 'yes' },
                           { message: 'x', retryable: 1 }, { message: 'x', retryable: null }]) {
      calls.length = 0
      fn(outcome)
      expect(calls[0].opts).toEqual({ retryable: false })
    }
  })

  it('is a no-op without a message, and on an empty thread', () => {
    const { fn, calls, messages } = loadMarkLastUserTurnFailed()
    messages.value = [{ role: 'user', content: 'q' }]
    fn(null); fn({}); fn({ retryable: true })
    expect(calls).toEqual([])
    messages.value = []
    fn({ message: 'x' })
    expect(calls).toEqual([])
  })
})
