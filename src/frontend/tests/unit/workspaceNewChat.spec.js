/**
 * ent#451 — the FRONTEND half of "New chat means a new chat".
 *
 * Review finding on the first cut: thirteen files, seven test files, all Python.
 * The literal reported bug — the watcher branch that read a changed agent as
 * "load that agent's history" — had zero coverage, and the `1497 passed` in the
 * PR body was the pre-existing suite rather than anything new.
 *
 * Two established patterns are used here, because vitest runs
 * `environment: 'node'` with no component-mount harness: a pure function driven
 * directly, and a source assertion in the shape of `portalLeaveSpecificRoute.spec.js`
 * for the rules that live inside an SFC and cannot be imported.
 *
 * The source assertions are deliberately narrow — an ORDERING and a CONJUNCTION,
 * both of which were wrong in a way that type-checks and renders fine.
 */
import { describe, it, expect } from 'vitest'
import fs from 'fs'
import path from 'path'
import { resolveAgentLanding } from '../../src/components/portal/portalUtils.js'

const read = (p) => fs.readFileSync(path.resolve(__dirname, p), 'utf8')
const PORTAL = () => read('../../src/views/Portal.vue')
const CONV = () => read('../../src/components/portal/PortalConversation.vue')
const STORE = () => read('../../src/stores/clientPortal.js')

// Comments name the very symbols these rules forbid, so a bare substring test
// reads the explanation as the code. Same trap as #2415's docstring.
const codeOnly = (src) => src
  .split('\n')
  .filter((l) => !l.trim().startsWith('//') && !l.trim().startsWith('*') && !l.trim().startsWith('/*'))
  .join('\n')

// ---------------------------------------------------------------------------
// The deep link — the blocker found in review
// ---------------------------------------------------------------------------
describe('ent#451 — ?new=1 asks for a fresh thread', () => {
  const agents = [{ name: 'sage' }]
  const threads = [{ id: 'ps_old', agent_name: 'sage' }]

  it('the landing function already honoured it', () => {
    expect(resolveAgentLanding({ agent: 'sage', forceNew: true, agents, threads }))
      .toEqual({ agentName: 'sage', sessionId: null })
    expect(resolveAgentLanding({ agent: 'sage', forceNew: false, agents, threads }).sessionId)
      .toBe('ps_old')
  })

  it('and the view now carries that same answer into the SEND', () => {
    // The bug: `resolveAgentQuery` passed `forceNew` to the landing and set
    // `pendingSession = null`, but never raised `startingNewChat`. So the deep
    // link rendered an empty conversation and the first turn went out with
    // `new_thread: false` — resuming the thread the user asked to leave.
    const src = codeOnly(PORTAL())
    expect(src).toMatch(/const forceNew = !!route\.query\.new/)
    expect(src).toMatch(/startingNewChat\.value = forceNew && !landing\.sessionId/)
  })

  it('reads route.query.new exactly once, so the two consumers cannot drift', () => {
    const hits = codeOnly(PORTAL()).match(/route\.query\.new/g) || []
    expect(hits).toHaveLength(1)
  })
})

// ---------------------------------------------------------------------------
// The watcher — the originally reported bug
// ---------------------------------------------------------------------------
describe('ent#451 — a deliberate fresh start survives an agent change', () => {
  it('the newChat branch is tested BEFORE the agent-changed branch', () => {
    // Ordering IS the fix. Both branches are individually correct; with them the
    // other way round a changed agent still wins and calls `loadThread(null)`,
    // which the backend answers with the most-recent thread.
    const src = codeOnly(CONV())
    const fresh = src.indexOf('props.newChat && !sid')
    const changed = src.indexOf("props.agent.name !== oldName || sid")
    expect(fresh).toBeGreaterThan(-1)
    expect(changed).toBeGreaterThan(-1)
    expect(fresh).toBeLessThan(changed)
  })

  it('first paint honours it too — the picker mounts rather than updates', () => {
    // The watcher never runs for a freshly mounted conversation, so the same
    // rule has to hold in `onMounted` or New chat resumes on the first render.
    expect(codeOnly(CONV())).toMatch(/props\.sessionId && !props\.newChat/)
  })

  it('the component declares the prop', () => {
    expect(codeOnly(CONV())).toMatch(/newChat:\s*\{\s*type:\s*Boolean/)
    expect(codeOnly(PORTAL())).toMatch(/:new-chat="startingNewChat"/)
  })
})

// ---------------------------------------------------------------------------
// The send — intent must be spent once a thread exists
// ---------------------------------------------------------------------------
describe('ent#451 — only the first turn of a new chat opens a thread', () => {
  it('both send paths AND on "no session yet"', () => {
    // Without the conjunction the SECOND turn opens a third thread, and every
    // turn after it opens another — the failure mode is unbounded, not a
    // one-off, so this is asserted on both paths rather than sampled.
    const sends = codeOnly(CONV()).match(/newThread: props\.newChat && !currentSessionId\.value/g) || []
    expect(sends).toHaveLength(2)
  })

  it('the streaming path and its synchronous fallback both carry it', () => {
    // The Workspace uses `/chat/stream` and falls back to `/chat`. A flag
    // honoured by only one brings the bug back exactly when streaming fails,
    // which is the least likely moment for anyone to notice.
    const src = codeOnly(STORE())
    const posts = src.match(/new_thread: newThread/g) || []
    expect(posts).toHaveLength(2)
  })

  it('every site that nulls the session also settles the intent', () => {
    // A flag that is only correct because its consumers AND on a second
    // variable is one refactor away from being wrong.
    const src = codeOnly(PORTAL())
    const nulls = (src.match(/pendingSession\.value = null/g) || []).length
    const settles = (src.match(/startingNewChat\.value = (false|forceNew)/g) || []).length
    expect(settles).toBeGreaterThanOrEqual(nulls)
  })
})

// ---------------------------------------------------------------------------
// #2579 — pressing New chat has to CHANGE something, and Main has to be there
// ---------------------------------------------------------------------------
describe('#2579 — the fresh chat is visible and focused', () => {
  it('adoption runs through ONE seam, and only that seam emits', () => {
    // Three sites adopt a session: streaming, the sync /chat fallback, and the
    // voice path's createSession. Raising `bornHere` at some of them drops the
    // provisional tab exactly when streaming is unavailable — the asymmetry
    // ent#451's own spec exists to catch. 4 = the declaration + 3 call sites.
    const src = codeOnly(CONV())
    expect((src.match(/adoptSession\(/g) || [])).toHaveLength(4)
    expect((src.match(/emit\('session-adopted'/g) || [])).toHaveLength(1)
    expect(src).toMatch(/function adoptSession\(id\) \{[\s\S]*?bornHere\.value = true[\s\S]*?emit\('session-adopted', id\)/)
  })

  it('the provisional tab survives the gap between adoption and the refreshed list', () => {
    // The shell clears `startingNewChat` on adoption, BEFORE the new list
    // arrives; `bornHere` is the bridge, and it is spent when the row lands.
    const src = codeOnly(CONV())
    expect(src).toMatch(/const bornHere = ref\(false\)/)
    expect(src).toMatch(/:draft="newChat \|\| bornHere"/)
    expect(src).toMatch(/watch\(\(\) => props\.threads, \(list\) => \{[\s\S]*?bornHere\.value = false/)
  })

  it('the composer is focused in the REMOUNTED instance, not before the press', () => {
    // New chat bumps `convGen`, which remounts the conversation, so focus set
    // before the press is thrown away. It has to happen in `onMounted`.
    const src = codeOnly(CONV())
    expect(src).toMatch(/if \(props\.newChat\) nextTick\(focusComposer\)/)
    expect(src).toMatch(/function focusComposer\(\) \{ textarea\.value\?\.focus\(\) \}/)
    // Membership, not the exact shape: #2559 adds `startVoiceCall` beside it for
    // the Talk door. What this case needs is that the parent can still reach
    // `focusComposer` — pinning the one-token form would fail any sibling PR
    // that legitimately exposes a second thing.
    expect(src).toMatch(/defineExpose\(\{[^}]*\bfocusComposer\b[^}]*\}\)/)
  })

  it('the notice is a SLOT, not a prop plus markup in the conversation', () => {
    // The shell owns every fact it carries, and two sibling PRs restructure
    // this header band next.
    expect(codeOnly(CONV())).toMatch(/<slot name="notice" \/>/)
    expect(codeOnly(PORTAL())).toMatch(/<template #notice>/)
    expect(codeOnly(PORTAL())).toMatch(/data-testid="portal-title-notice"/)
    expect(codeOnly(PORTAL())).toMatch(/role="status"[\s\S]{0,80}aria-live="polite"/)
  })
})

describe('#2579 — Main is listed from the first visit', () => {
  it('landOnAgent no longer destructures an array, and re-checks the route AFTER the ensure', () => {
    // `store.fetchSessions` returns `data.sessions || []` — an ARRAY. The old
    // `const { sessions } = await …` was always undefined, so the repair
    // branch never ran once. The ensure awaits two round trips where the old
    // code awaited one, so the overtake guard has to sit between them.
    const src = codeOnly(PORTAL())
    expect(src).not.toMatch(/const \{ sessions \} = await store\.fetchSessions/)
    expect(src).toMatch(/await ensureMainListed\(name\)[\s\S]{0,400}?if \(activeAgentPageName\.value !== name\) return/)
  })

  it('the ensure is a deduped, capped promise per agent — and both maps die at sign-out', () => {
    // `fetchAllSessions` NEVER rejects, so a resolved entry over a still-missing
    // Main would make the miss permanent for the session. And `onSignOut`
    // resets in place (the OTP form is a branch of this same component), so
    // client B would inherit client A's resolved promises.
    const src = codeOnly(PORTAL())
    expect(src).toMatch(/const mainEnsured = new Map\(\)/)
    expect(src).toMatch(/const mainAttempts = new Map\(\)/)
    expect(src).toMatch(/if \(agentHasMain\(threads\.value, name\)\) return Promise\.resolve\(\)/)
    expect(src).toMatch(/if \(spent >= MAIN_ENSURE_ATTEMPTS\) return Promise\.resolve\(\)/)
    expect(src).toMatch(/\.catch\(\(\) => \{ mainEnsured\.delete\(name\) \}\)/)
    expect(src).toMatch(/watch\(activeAgentName, \(name\) => \{[\s\S]*?ensureMainListed\(name\)/)
    expect(src).toMatch(/mainEnsured\.clear\(\); mainAttempts\.clear\(\)/)
  })
})

describe('#2579 — the title settle cycle', () => {
  const src = () => codeOnly(PORTAL())

  it('bails on a falsy session id and on a failed list read', () => {
    // `sessions-changed` can fire with a null id, and `fetchAllSessions` returns
    // the LAST GOOD list rather than rejecting — without the flag check a flaky
    // network reads as "the title never changed" and reports a working
    // generator broken.
    expect(src()).toMatch(/if \(!sessionId\) return refreshThreads\(\)/)
    expect(src()).toMatch(/if \(list === null \|\| store\.sessionsFailed\) \{ clearTitleSettle\(\); return \}/)
  })

  it('is cleared from three sites, one of them the conversation change', () => {
    // Neither the next turn-done nor onBeforeUnmount fires on a thread switch;
    // without `watch(convKey)` a cycle armed in chat A keeps replacing the list
    // under the user for 16s and can raise a notice above chat B.
    const s = src()
    expect((s.match(/clearTitleSettle\(\)/g) || []).length).toBeGreaterThanOrEqual(4)
    // Arming is idempotent: the handler clears synchronously but arms after an
    // await, so two events close together would leave two cycles running.
    expect(s).toMatch(/function armTitleSettle\(sessionId\) \{\s*clearTitleSettle\(\)/)
  })

  it('only arms for the thread the user is STILL in', () => {
    // The event fires for a thread navigated away from — the main way a reply
    // legitimately arrives unseen. A cycle armed there keeps replacing the list
    // for 16s and can raise a notice above a different conversation.
    // `watch(convKey)` only catches a switch that happens AFTER arming.
    const s = src()
    expect(s).toMatch(/const stillHere = shouldMarkTurnRead\(sessionId, open\)/)
    expect(s).toMatch(/\.then\(\(\) => \{ if \(stillHere\) armTitleSettle\(sessionId\) \}\)/)
    expect(s).toMatch(/watch\(convKey, \(\) => \{ clearTitleSettle\(\) \}\)/)
    expect(s).toMatch(/onBeforeUnmount\(\(\) => \{[\s\S]*?clearTitleSettle\(\)/)
  })

  it('re-asks the same question INSIDE the arm, because the caller decided it two awaits ago', () => {
    // /review. `stillHere` is computed synchronously at the event; the arm runs
    // after `markRead` and `refreshThreads`. A thread switch inside that window
    // passes the caller's check AND fires `watch(convKey)` while nothing is
    // armed yet — so without this second check the cycle is armed on a
    // conversation the person has already left, which is precisely what the
    // caller's own comment claims is prevented.
    const s = src()
    expect(s).toMatch(
      /function armTitleSettle\(sessionId\) \{\s*clearTitleSettle\(\)\s*if \(!shouldMarkTurnRead\(sessionId, activeSessionId\.value \|\| pendingSession\.value\)\) return/
    )
  })

  it('a list-only re-read still owes the ent#491 seeding it replaces', () => {
    // /review. `settleTick` assigns `threads.value` exactly as `refreshThreads`
    // does, so it must seed agent recency the same way or an unranked agent
    // stays unranked for as long as the cycle keeps overwriting the list.
    const s = src()
    expect(s).toMatch(/threads\.value = decorate\(list\)\s*store\.seedAgentRecency\(threads\.value\)/)
  })

  it('a vanished row stops the cycle WITHOUT a verdict', () => {
    // A deleted row (Reset, delete) is not evidence the generator works.
    expect(src()).toMatch(/if \(!row\) \{ clearTitleSettle\(\); return \}/)
  })

  it('the health fetch is gated, never at bootstrap, and never through @/api', () => {
    const s = src()
    expect(s).toMatch(/if \(!shouldFetchTitleHealth\(store\.isPlatformSession, authStore\.role\)\) return/)
    // Only the exhausted cycle asks — one call site, inside settleTick.
    expect((s.match(/refreshTitleHealth\(\)/g) || [])).toHaveLength(2)   // the definition + one caller
    // `@/api` hard-navigates to /login on a 401 under /workspace; a background
    // diagnostic must not bounce an operator out of their conversation.
    const store = codeOnly(STORE())
    expect(store).toMatch(/async fetchTitleGenerationHealth\(\) \{\s*const \{ data \} = await portalHttp\.get/)
    expect(store).toMatch(/'\/api\/settings\/portal-session-policy'/)
    // The same field the settings panel reads — one health path, not two.
    expect(store).toMatch(/return data\?\.title_generation \|\| null/)
  })
})
