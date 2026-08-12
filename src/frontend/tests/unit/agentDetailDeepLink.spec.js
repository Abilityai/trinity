/**
 * #2130 — the ?tab= deep-link landing must be applied BEFORE any await in the
 * lifecycle hooks that own it.
 *
 * `applyDeepLinkRouting()` reads only `route.query` and writes local refs, so it
 * has no dependency on loaded agent data. It used to be called at the END of
 * `onMounted` / `onActivated`, after `await Promise.allSettled([...])`. On a
 * RUNNING agent that batch also holds `checkDashboardExists()` and
 * `checkBrainOrbCapability()` — container round-trips — so the deep-linked tab
 * was applied ~10s late. For that whole window the page showed Overview, and
 * then it STOLE a tab the user had clicked in the meantime
 * (measured: open `?tab=git`, click Reports at 2s, page jumps to Git at ~10s).
 *
 * There is no component-mount harness in this project (no @vue/test-utils), and
 * the defect is one of lifecycle ORDERING rather than of any pure function, so
 * this is a source-structure guard — the same shape as the Python AST guards
 * (`test_ent109_git_env_seam.py`, `test_1891_python_version_parity.py`).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

const SRC = fileURLToPath(new URL('../../src/views/AgentDetail.vue', import.meta.url))
const source = readFileSync(SRC, 'utf8')

/** Strip line + block comments so prose like "before any await" isn't scanned. */
function stripComments(code) {
  return code
    .replace(/\/\*[\s\S]*?\*\//g, '')   // block comments
    .replace(/^[ \t]*\/\/.*$/gm, '')      // whole-line // comments
    .replace(/([^:])\/\/.*$/gm, '$1')      // trailing // comments (keep URLs)
}

/** Return the body of `hook(async () => { ... })` via brace matching. */
function hookBody(hookName) {
  const start = source.indexOf(`${hookName}(async () => {`)
  expect(start, `${hookName}(async () => { … }) not found`).toBeGreaterThan(-1)
  const open = source.indexOf('{', start)
  let depth = 0
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth++
    else if (source[i] === '}') {
      depth--
      if (depth === 0) return source.slice(open + 1, i)
    }
  }
  throw new Error(`unbalanced braces in ${hookName}`)
}

describe('#2130 AgentDetail ?tab= deep-link ordering', () => {
  for (const hook of ['onMounted', 'onActivated']) {
    it(`${hook} applies the deep-link before its first await`, () => {
      const body = stripComments(hookBody(hook))

      const applyAt = body.indexOf('applyDeepLinkRouting()')
      expect(applyAt, `${hook} must call applyDeepLinkRouting()`).toBeGreaterThan(-1)

      const awaitAt = body.search(/\bawait\b/)
      if (awaitAt === -1) return // no awaits at all — trivially fine

      expect(
        applyAt,
        `${hook} calls applyDeepLinkRouting() AFTER an await. The ?tab= landing ` +
        `then waits on unrelated network work (on a running agent that includes ` +
        `container round-trips, ~10s), showing Overview meanwhile and overriding ` +
        `a tab the user clicked in that window. Apply it before the awaits.`
      ).toBeLessThan(awaitAt)
    })

    it(`${hook} still applies it exactly once`, () => {
      const body = stripComments(hookBody(hook))
      const calls = body.match(/applyDeepLinkRouting\(\)/g) || []
      expect(calls.length, `${hook} should call applyDeepLinkRouting() exactly once`).toBe(1)
    })
  }

  it('the retired-session redirect still guards ahead of the deep-link (ent#358)', () => {
    // The redirect navigates away and early-returns; applying a tab first would
    // be wasted work on an unmounting view.
    for (const hook of ['onMounted', 'onActivated']) {
      const body = stripComments(hookBody(hook))
      const redirectAt = body.indexOf('redirectRetiredSessionLink()')
      const applyAt = body.indexOf('applyDeepLinkRouting()')
      expect(redirectAt, `${hook} must keep the ent#358 guard`).toBeGreaterThan(-1)
      expect(redirectAt, `${hook}: ent#358 guard must precede the deep-link`).toBeLessThan(applyAt)
    }
  })

  it('applyDeepLinkRouting reads only route state (no loaded-agent dependency)', () => {
    // This is what makes the early call correct — if it ever starts reading
    // `agent.value`, the ordering above has to be reconsidered.
    const start = source.indexOf('function applyDeepLinkRouting() {')
    expect(start).toBeGreaterThan(-1)
    const open = source.indexOf('{', start)
    let depth = 0, end = -1
    for (let i = open; i < source.length; i++) {
      if (source[i] === '{') depth++
      else if (source[i] === '}') { depth--; if (depth === 0) { end = i; break } }
    }
    const body = source.slice(open + 1, end)
    expect(body).not.toMatch(/\bagent\.value\b/)
  })
})
