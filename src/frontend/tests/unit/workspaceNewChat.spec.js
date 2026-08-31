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
