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
  const body = fnBody('newChatWithAgent')

  it('navigates away from any non-bare route', () => {
    expect(body).toMatch(/route\.path\s*!==\s*'\/workspace'/)
  })

  it('does NOT enumerate route params — that is what broke twice', () => {
    // `sessionId || roomId` was correct until a third route existed. Naming
    // params here means the next route silently re-breaks the button.
    expect(body).not.toMatch(/route\.params\.sessionId\s*\|\|/)
    expect(body).not.toMatch(/route\.params\.agentName/)
  })

  it('still pushes to the bare workspace route', () => {
    expect(body).toMatch(/router\.push\('\/workspace'\)/)
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
