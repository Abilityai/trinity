/**
 * "Start a chat" must leave whatever specific Workspace route you are on.
 *
 * `newChatWithAgent` sets conversation state and then navigates back to the
 * bare `/workspace`. Without that navigation the state change is invisible: the
 * stage renders by route, so `/workspace/a/:agentName` keeps showing the agent
 * page (its `v-if` is the FIRST branch of the chain) and the chat the user
 * asked for never appears. The button looks dead.
 *
 * The condition was an ENUMERATION of route params, and it has been wrong twice
 * for the same reason:
 *
 *   #2128  — added `roomId` after picking an agent from a room URL left the
 *            room (and its refusal panel) on screen.
 *   ent#360 — added `/workspace/a/:agentName` and did not extend the list, so
 *            "Start a chat" on the agent page did nothing visible.
 *
 * So this guards the SHAPE of the answer, not one more param name: the check
 * must be about being on the bare route, so a fourth Workspace route cannot
 * reintroduce it.
 *
 * #2161 kept that rule and moved it behind `escapeStage()` → the pure
 * `shouldEscapeStage(path, query)` in portalUtils, for two reasons the inline
 * form could not cover: it becomes testable by CALLING it (this project has no
 * component-mount harness, so an inline closure over `route` can only ever be
 * checked by scanning for a spelling), and it extends to the QUERY —
 * `/workspace?agent=X` passes the path check while still carrying X into the
 * next session, and `?agent=` is re-read by `bootstrap()` after every sign-in.
 *
 * These assertions therefore moved one level down rather than being relaxed:
 * the exits must delegate, and the rule itself is asserted where it now lives.
 * `startBlankChat` left the list because it has had no callers since ent#361
 * (8e5157f1) handed `@new-chat` to the picker — it is deleted, not converted.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

const source = readFileSync(
  fileURLToPath(new URL('../../src/views/Portal.vue', import.meta.url)), 'utf8',
)

/** Body of `function <name>(...) { ... }` by brace matching. */
function fnBody(name) {
  const start = source.indexOf(`function ${name}(`)
  expect(start, `${name}() not found`).toBeGreaterThan(-1)
  const open = source.indexOf('{', source.indexOf(')', start))
  let depth = 0
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth++
    else if (source[i] === '}' && --depth === 0) return source.slice(start, i + 1)
  }
  throw new Error(`unterminated ${name}()`)
}

describe('leaving a specific Workspace route when starting a chat', () => {
  // Both LIVE stage exits, not just the reported one — they carried the same
  // enumeration, so ent#360 broke them together; fixing only the button in the
  // bug report leaves sign-out carrying the agent id into the next session's URL.
  const EXITS = ['newChatWithAgent', 'onSignOut']

  it.each(EXITS)('%s delegates to the shared stage escape', (fn) => {
    // Delegation IS the property now: the rule (path + query) lives in one
    // place, so a future stage route or query key is fixed everywhere at once.
    expect(fnBody(fn)).toMatch(/escapeStage\(\)/)
  })

  it.each(EXITS)('%s does NOT enumerate route params — that is what broke twice', (fn) => {
    // `sessionId || roomId` was correct until a third route existed. Naming
    // params here means the next route silently re-breaks the control. Kept
    // alongside the delegation check: an enumeration could creep back BESIDE
    // the call, which a delegation-only assertion would not notice.
    expect(fnBody(fn)).not.toMatch(/route\.params\.(sessionId|roomId|agentName)/)
  })

  it('the escape still asks about route shape and still pushes the bare route', () => {
    // The two assertions that used to be made against each exit, now made once
    // against the single place that answers for all of them.
    const body = fnBody('escapeStage')
    expect(body).toMatch(/shouldEscapeStage\(route\.path,\s*route\.query\)/)
    expect(body).toMatch(/router\.push\('\/workspace'\)/)
  })

  it('startBlankChat is gone rather than fixed', () => {
    // ent#361 (8e5157f1) renamed it out of the `@new-chat` binding and gave that
    // event to the picker, so it has had zero callers since. A dead function
    // carrying a "fixed" comment reads as live to the next person.
    expect(source).not.toMatch(/function startBlankChat\(/)
  })
})

describe('every specific Workspace route is under /workspace/', () => {
  // The premise the fix rests on: if a route were ever added at a different
  // path, `route.path !== '/workspace'` would still be true there, so the check
  // stays correct. This pins that the bare route is exactly '/workspace'.
  const router = readFileSync(
    fileURLToPath(new URL('../../src/router/index.js', import.meta.url)), 'utf8',
  )

  it('has a bare /workspace route the fix can compare against', () => {
    expect(router).toMatch(/path:\s*'\/workspace'/)
  })

  it('keeps the specific ones nested beneath it', () => {
    for (const p of ['/workspace/c/:sessionId', '/workspace/r/:roomId', '/workspace/a/:agentName']) {
      expect(router).toContain(p)
    }
  })
})
