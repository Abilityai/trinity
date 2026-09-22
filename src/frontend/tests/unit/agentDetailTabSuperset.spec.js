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
 * ent#479 moved `buildTabs` into `utils/agentTabs.js` and this spec IMPORTS it
 * rather than brace-matching it out of the SFC and `new Function`-ing the text.
 * Same executed behaviour, one less parser. The gate's own cases live in
 * `agentTabs.spec.js`; what stays here is the deep-link WIRING, which is a
 * property of `AgentDetail.vue` and still read from its source.
 *
 * @source-text-pin: the remaining reads assert how AgentDetail.vue WIRES the
 * resolver (that `ALL_TAB_IDS` is derived from the builder rather than
 * restated, that the late reconcile guards itself, that it runs in both
 * lifecycle hooks). Those are call-site shape, not rendered behaviour: a mount
 * that lands on the right tab passes identically whether the superset came
 * from the builder or from a freshly re-typed literal, which is the #2153
 * regression itself.
 */
import { describe, it, expect } from 'vitest'
import { buildTabs } from '../../src/utils/agentTabs'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

const SRC = fileURLToPath(new URL('../../src/views/AgentDetail.vue', import.meta.url))
const source = readFileSync(SRC, 'utf8')

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
