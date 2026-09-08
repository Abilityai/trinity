/**
 * #2200 — document/window listeners must be armed BEFORE any await in the
 * mount hook that owns them.
 *
 * `Dashboard.vue` registered the ent#261 `/` type-to-filter hotkey at the END of
 * an `async onMounted`, after `await Promise.allSettled([...5 fetches...])`.
 * The fleet paints as soon as `fetchAgents()` ALONE resolves (the template gate
 * `agents.length > 0`), but the listener waited on the SLOWEST of the five — so
 * there was a window in which the dashboard rendered as interactive and every
 * `/` was silently dropped. Measured at ~50ms on an idle local instance, but the
 * quantity is `max(five) - fetchAgents()` and is therefore UNBOUNDED: a large
 * fleet, a cold DB, or one slow endpoint widens it without limit.
 *
 * The position was inherited, not chosen — the hotkey was appended beside a
 * pre-existing `handleClickOutside` registration whose slot had silently BECOME
 * post-await when PERF-269 introduced the parallel fetch. That is the mechanism
 * this guard exists to catch: nobody edits the listener, and it breaks anyway.
 *
 * SCOPE — deliberately repo-wide, not one file. #2130 was the same class (sync
 * work with no data dependency stranded after `await Promise.allSettled` in
 * `onMounted`) and shipped a guard that hardcodes a single path
 * (`agentDetailDeepLink.spec.js`), so it structurally could not see
 * `Dashboard.vue` and the class recurred three days later.
 *
 * HONEST LIMITATION — this guard keys on `addEventListener`, so it catches the
 * LISTENER variant only. It would NOT have caught #2130, which was a routing
 * write. The general rule ("no sync work with no data dependency behind a mount
 * await") needs real AST traversal; this repo has no ESLint toolchain at all
 * (no dep, no script, no workflow), so that is filed as a follow-up rather than
 * bolted onto a P2 bug fix.
 *
 * Known parser blind spots (regex scanner, no string tokenizer — verified by
 * probe at review, #2200): an UNBALANCED `}` inside a string literal in a hook
 * body silently truncates the scanned body (false PASS past it), and a `//`
 * inside a string mangles the rest of that line (false PASS for a listener
 * after it on the same line). Both are silent-direction misses; neither shape
 * exists in the tree today, and the Dashboard-specific test below is the
 * belt for the file this guard was minted for. The word `addEventListener` or
 * `await` inside a string false-FAILS instead — loud and fixable, accepted.
 *
 * There is no component-mount harness in this project (no @vue/test-utils), and
 * the defect is one of lifecycle ORDERING rather than of any pure function, so
 * this is a source-structure guard — the same shape as `agentDetailDeepLink.spec.js`
 * and the Python AST guards (`test_ent109_git_env_seam.py`).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync, readdirSync } from 'fs'
import { join, relative, sep } from 'path'
import { fileURLToPath } from 'url'

const SRC_ROOT = fileURLToPath(new URL('../../src', import.meta.url))

const HOOKS = ['onMounted', 'onActivated']
const LISTENER = /\b(?:document|window)\.addEventListener\s*\(/

/**
 * Files permitted to register a listener after a mount await.
 *
 * An allowlist that grows silently is a disabled guard, so: every entry carries
 * its reason inline, `MAX_ALLOWLIST` pins the DIRECTION (may shrink, never grow
 * without a deliberate reviewed edit), and a test below fails if an entry stops
 * being needed — so a fixed file cannot leave a stale exemption behind.
 */
const ALLOWLIST = [
  // ent#438 emptied this list: its only entry was `views/AgentWorkspace.vue`,
  // whose late `resize` listener was exempted, and that page is retired (the
  // canvas is a durable surface now, and voice conversation moved into the
  // Workspace in ent#440). The guard's own stale-exemption test is what caught
  // it — the entry is dropped rather than left behind, and MAX_ALLOWLIST
  // ratchets DOWN to 0 accordingly: the direction it pins is "may shrink".
]
const MAX_ALLOWLIST = 0 // may shrink, never grow without a deliberate reviewed edit

/** Strip line + block comments so prose like "before any await" isn't scanned. */
function stripComments(code) {
  return code
    .replace(/\/\*[\s\S]*?\*\//g, '') // block comments
    .replace(/^[ \t]*\/\/.*$/gm, '') // whole-line // comments
    .replace(/([^:])\/\/.*$/gm, '$1') // trailing // comments (keep URLs)
}

/** Every .vue/.js file under src/, as paths relative to src/ with POSIX separators. */
function sourceFiles(dir = SRC_ROOT, out = []) {
  for (const entry of readdirSync(dir, { withFileTypes: true })) {
    const full = join(dir, entry.name)
    if (entry.isDirectory()) {
      if (entry.name === 'node_modules') continue
      sourceFiles(full, out)
    } else if (/\.(vue|js)$/.test(entry.name)) {
      out.push(relative(SRC_ROOT, full).split(sep).join('/'))
    }
  }
  return out
}

/** Bodies of every `hook(async ...)` in `code`, via brace matching. */
function asyncHookBodies(code, hook) {
  const bodies = []
  const marker = `${hook}(async`
  let from = 0
  for (;;) {
    const start = code.indexOf(marker, from)
    if (start === -1) return bodies
    const open = code.indexOf('{', start)
    if (open === -1) return bodies
    let depth = 0
    let closed = false
    for (let i = open; i < code.length; i++) {
      if (code[i] === '{') depth++
      else if (code[i] === '}') {
        depth--
        if (depth === 0) {
          bodies.push(code.slice(open + 1, i))
          from = i
          closed = true
          break
        }
      }
    }
    // Unbalanced braces would silently truncate the scan — fail loudly instead.
    if (!closed) throw new Error(`unbalanced braces after ${marker}`)
  }
}

/**
 * Offending hook bodies in one file: those registering a listener after an await.
 *
 * `await` is matched at ANY depth, not just top level. That is conservative on
 * purpose — a guard that false-PASSES is useless, while a false-FAIL is loud and
 * fixable — and it costs nothing today: no hook body in the tree has a nested
 * await ahead of its listener.
 */
function violations(relPath) {
  const source = stripComments(readFileSync(join(SRC_ROOT, relPath), 'utf8'))
  if (!LISTENER.test(source)) return []

  const found = []
  for (const hook of HOOKS) {
    for (const body of asyncHookBodies(source, hook)) {
      const awaitAt = body.search(/\bawait\b/)
      if (awaitAt === -1) continue // no awaits — trivially fine
      const after = body.slice(awaitAt)
      const listenerAt = after.search(LISTENER)
      if (listenerAt !== -1) found.push(hook)
    }
  }
  return found
}

const FILES = sourceFiles()
const ALLOWED = new Set(ALLOWLIST.map((e) => e.file))

describe('#2200 mount-hook listener ordering', () => {
  it('finds source files to scan (the guard is not vacuously green)', () => {
    expect(FILES.length).toBeGreaterThan(100)
    expect(FILES).toContain('views/Dashboard.vue')
  })

  it('no document/window listener is registered after an await in a mount hook', () => {
    const offenders = []
    for (const file of FILES) {
      if (ALLOWED.has(file)) continue
      for (const hook of violations(file)) offenders.push(`${file} (${hook})`)
    }

    expect(
      offenders,
      'These files register a document/window listener AFTER an await in a mount ' +
        'hook. The surface paints — and reads as interactive — as soon as its own ' +
        'data resolves, but the listener waits on every awaited call in the hook, ' +
        'so user input in that window is silently dropped (#2200; ~50ms measured, ' +
        'unbounded in principle). Register listeners at the TOP of the hook, above ' +
        'any await:\n  ' + offenders.join('\n  ')
    ).toEqual([])
  })

  it('Dashboard arms BOTH listeners before its first await (#2200 regression)', () => {
    const source = stripComments(readFileSync(join(SRC_ROOT, 'views/Dashboard.vue'), 'utf8'))
    const bodies = asyncHookBodies(source, 'onMounted')
    expect(bodies.length, 'Dashboard.vue must have an async onMounted').toBe(1)
    const body = bodies[0]

    const awaitAt = body.search(/\bawait\b/)
    expect(awaitAt, 'Dashboard onMounted should still await its parallel fetches').toBeGreaterThan(-1)

    for (const handler of ['handleClickOutside', 'handleDashboardKeydown']) {
      const at = body.indexOf(`addEventListener('${handler === 'handleClickOutside' ? 'click' : 'keydown'}', ${handler})`)
      expect(at, `onMounted must register ${handler}`).toBeGreaterThan(-1)
      expect(
        at,
        `Dashboard registers ${handler} AFTER the awaited fetches. The fleet paints ` +
          'as soon as fetchAgents() alone resolves, so this leaves a window in which ' +
          'the dashboard looks interactive and `/` is silently dropped (#2200).'
      ).toBeLessThan(awaitAt)
    }
  })

  it('teardown still pairs with the (now earlier) registration', () => {
    // Hoisting must not silently drop a removeEventListener — that would trade a
    // dead-window bug for a leak.
    const source = stripComments(readFileSync(join(SRC_ROOT, 'views/Dashboard.vue'), 'utf8'))
    for (const [event, handler] of [
      ['click', 'handleClickOutside'],
      ['keydown', 'handleDashboardKeydown'],
    ]) {
      expect(source).toContain(`document.addEventListener('${event}', ${handler})`)
      expect(source).toContain(`document.removeEventListener('${event}', ${handler})`)
    }
  })

  it('the allowlist has not grown', () => {
    expect(
      ALLOWLIST.length,
      'Adding an allowlist entry disables this guard for a file. Fix the ordering ' +
        'instead, or raise MAX_ALLOWLIST deliberately with the reason recorded inline.'
    ).toBeLessThanOrEqual(MAX_ALLOWLIST)
    for (const entry of ALLOWLIST) {
      expect(entry.reason, `allowlist entry ${entry.file} must carry a reason`).toBeTruthy()
    }
  })

  it('every allowlist entry is still needed (no stale exemptions)', () => {
    for (const entry of ALLOWLIST) {
      expect(FILES, `allowlisted file ${entry.file} no longer exists — drop the entry`).toContain(entry.file)
      expect(
        violations(entry.file).length,
        `${entry.file} no longer registers a listener after an await — remove its ` +
          'allowlist entry so the guard covers it again.'
      ).toBeGreaterThan(0)
    }
  })
})
