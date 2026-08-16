/**
 * #2153 — every tab the viewer can see is deep-linkable, and no tab they cannot
 * see is.
 *
 * `?tab=` used to resolve against a hand-maintained `DEEP_LINK_TABS` list that
 * omitted `a2a`, `loops`, `playbooks`, `access` and `nevermined` — real,
 * rendered tabs whose links died on Overview with no error. Two definitions of
 * "which tabs exist", only one of them maintained when a tab was added.
 *
 * The fix deletes the list: `buildTabs()` is pure and takes the gating flags as
 * arguments, so the deep-link resolver asks it for the superset.
 *
 * That purity is what lets this be a REAL test rather than another
 * source-structure guard (this project has no @vue/test-utils, so the sibling
 * #2130 spec has to scan text). The function is extracted from the SFC and
 * executed, so what is asserted here is the behaviour that actually ships — add
 * a tab, and these tests see it.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

const SRC = fileURLToPath(new URL('../../src/views/AgentDetail.vue', import.meta.url))
const source = readFileSync(SRC, 'utf8')

/** Pull `function buildTabs(...) { ... }` out of the SFC by brace matching. */
function extractBuildTabs() {
  const start = source.indexOf('function buildTabs(')
  expect(start, 'buildTabs() not found — did it get inlined back into the computed?')
    .toBeGreaterThan(-1)
  const open = source.indexOf('{', source.indexOf(')', start))
  let depth = 0
  for (let i = open; i < source.length; i++) {
    if (source[i] === '{') depth++
    else if (source[i] === '}') {
      depth--
      if (depth === 0) {
        // eslint-disable-next-line no-new-func
        return new Function(`${source.slice(start, i + 1)}; return buildTabs`)()
      }
    }
  }
  throw new Error('unterminated buildTabs()')
}

const buildTabs = extractBuildTabs()
const idsFor = (flags) => buildTabs(flags).map((t) => t.id)

const OWNER = {
  isSystem: false, hasDashboardFlag: true, brainOrbVisible: true,
  canShare: true, a2aVisible: true, gitSync: true,
}
const NON_OWNER = { ...OWNER, canShare: false }

// The five the old list forgot. Named individually so a regression says which.
const PREVIOUSLY_UNREACHABLE = ['a2a', 'loops', 'playbooks', 'access', 'nevermined']

describe('#2153 the deep-link superset', () => {
  for (const id of PREVIOUSLY_UNREACHABLE) {
    it(`includes '${id}', which the old DEEP_LINK_TABS omitted`, () => {
      expect(idsFor(OWNER)).toContain(id)
    })
  }

  it('resolves against the derived superset, not a literal list', () => {
    // The actual fix. Restoring a hand-written array here is how the two
    // definitions drift apart again, and every test above would still pass.
    expect(source).toMatch(/ALL_TAB_IDS\.includes\(resolved\)/)
    expect(source).not.toMatch(/const DEEP_LINK_TABS\s*=/)
  })

  it('derives the superset from the builder rather than restating it', () => {
    expect(source).toMatch(/const ALL_TAB_IDS = buildTabs\(/)
  })
})

describe('#2153 a non-owner still cannot reach owner-only tabs', () => {
  // The question #2130 deferred as "needs a non-owner blank-panel decision".
  // Resolving against what the viewer can see answers it without one: the
  // fallback for a non-owner is unchanged, and no blank panel is ever rendered.
  const ownerOnly = ['access', 'sharing', 'permissions', 'a2a', 'folders', 'skills', 'settings']

  for (const id of ownerOnly) {
    it(`omits '${id}' when can_share is false`, () => {
      expect(idsFor(NON_OWNER)).not.toContain(id)
    })
  }

  it('still offers the shared tabs to a non-owner', () => {
    const ids = idsFor(NON_OWNER)
    for (const id of ['overview', 'tasks', 'chat', 'reports', 'schedules', 'loops', 'info']) {
      expect(ids).toContain(id)
    }
  })

  it('omits the sharing tabs for a system agent even when can_share is true', () => {
    const ids = idsFor({ ...OWNER, isSystem: true })
    expect(ids).not.toContain('sharing')
    expect(ids).not.toContain('permissions')
  })
})

describe('#2153 the late reconcile cannot re-create #2130', () => {
  // #2130 was a late write to `activeTab` yanking users off a tab they had
  // clicked. The reconcile added here is another late write, so the guard that
  // it only acts while `activeTab` is untouched is load-bearing.
  const body = (() => {
    const start = source.indexOf('function reconcileDeepLinkVisibility(')
    expect(start, 'reconcileDeepLinkVisibility() not found').toBeGreaterThan(-1)
    return source.slice(start, source.indexOf('\n}', start))
  })()

  it('only overrides the tab it set itself', () => {
    expect(body).toMatch(/activeTab\.value === requested/)
  })

  it('clears its intent so it can act at most once', () => {
    expect(body).toMatch(/deepLinkedTab = null/)
  })

  it('runs in BOTH lifecycle hooks — one is the #1672 bug class', () => {
    const calls = source.match(/^\s*reconcileDeepLinkVisibility\(\)/gm) || []
    expect(calls.length).toBe(2)
  })
})
