/**
 * ent#242 — the skill-runner admin panel's decidable rules.
 *
 * The panel exists because the operator half of the runner was API-only: the
 * six admin endpoints had no writer outside curl, so the ACL table could not be
 * populated by a human at all. These tests pin the rules that decide what the
 * operator is told — in particular that the eight states the server can report
 * stay eight states, rather than collapsing into "unavailable".
 */
import { describe, it, expect } from 'vitest'
import {
  blockingReason, blockingAction, runnerState, runnerSummary, actionState,
  headerCounts, isDuplicateGrant, grantFormState, revokePrompt, filterGrants, grantsFrom,
  RUNNER_ABSENT, RUNNER_STOPPED, RUNNER_RUNNING,
} from '../../src/components/settings/skillRunnerPanel.js'

const lib = (over = {}) => ({ configured: true, synced: true, skill_count: 41, ...over })
const status = (over = {}) => ({
  enabled: true, library: lib(), runner_agent: 'skill-runner',
  runner_exists: true, runner_running: true,
  exposed_skill_count: 12, grant_count: 3, ...over,
})

describe('blocking state', () => {
  it('is null when the library is usable', () => {
    expect(blockingReason(status())).toBeNull()
  })

  it('reports an unconfigured library before a missing runner', () => {
    // Ordering matters: a library that was never pulled cannot be synced onto a
    // runner, so "not provisioned" would send the operator to the wrong screen.
    const s = status({ library: lib({ configured: false }), runner_exists: false })
    expect(blockingReason(s)).toMatch(/no skills library is configured/i)
  })

  it('distinguishes configured-but-never-pulled from unconfigured', () => {
    expect(blockingReason(status({ library: lib({ synced: false }) })))
      .toMatch(/never been pulled/i)
  })

  it('surfaces a library read failure rather than claiming it is unconfigured', () => {
    expect(blockingReason(status({ library: lib({ error: 'boom' }) })))
      .toMatch(/could not be read/i)
  })

  it('offers somewhere to go when blocked — a dead end is the bug', () => {
    expect(blockingAction(status({ library: lib({ configured: false }) })))
      .toEqual({ label: 'Open the Skills library', tab: 'skills' })
  })

  it('offers no action for a read failure, which no navigation fixes', () => {
    expect(blockingAction(status({ library: lib({ error: 'boom' }) }))).toBeNull()
  })
})

describe('runner lifecycle — absent and stopped are NOT the same', () => {
  it('absent', () => {
    expect(runnerState(status({ runner_exists: false }))).toBe(RUNNER_ABSENT)
  })
  it('stopped', () => {
    expect(runnerState(status({ runner_running: false }))).toBe(RUNNER_STOPPED)
  })
  it('running', () => {
    expect(runnerState(status())).toBe(RUNNER_RUNNING)
  })

  it('says which, in words, for each', () => {
    expect(runnerSummary(status()).text).toMatch(/is running/)
    expect(runnerSummary(status({ runner_running: false })).text).toMatch(/not running/)
    expect(runnerSummary(status({ runner_exists: false })).text).toMatch(/no runner agent/i)
  })

  it('gives each state a different badge tone', () => {
    const tones = [status(), status({ runner_running: false }), status({ runner_exists: false })]
      .map((s) => runnerSummary(s).variant)
    expect(new Set(tones).size).toBe(3)
  })
})

describe('action enablement', () => {
  it('enables provision and sync on a healthy install', () => {
    const a = actionState(status())
    expect(a.provision.disabled).toBe(false)
    expect(a.sync.disabled).toBe(false)
  })

  it('disables provision WITH the reason instead of letting it fail', () => {
    const a = actionState(status({ library: lib({ configured: false }) }))
    expect(a.provision.disabled).toBe(true)
    expect(a.provision.reason).toMatch(/no skills library/i)
  })

  it('cannot sync onto a runner that does not exist, and says so', () => {
    const a = actionState(status({ runner_exists: false }))
    expect(a.sync.disabled).toBe(true)
    expect(a.sync.reason).toMatch(/no runner agent/i)
    // ...but provisioning one is exactly what you should do next.
    expect(a.provision.disabled).toBe(false)
    expect(a.provision.label).toMatch(/provision runner/i)
  })

  it('calls a second provision a re-provision — it is idempotent, not a mistake', () => {
    expect(actionState(status()).provision.label).toMatch(/re-provision/i)
  })

  it('never blocks the toggle on library state — turning it OFF must always work', () => {
    const a = actionState(status({ enabled: true, library: lib({ configured: false }) }))
    expect(a.toggle.disabled).toBe(false)
  })

  it('disables everything while one action is in flight, and marks which', () => {
    const a = actionState(status(), { busy: 'provision' })
    expect(a.provision.busy).toBe(true)
    expect(a.sync.disabled).toBe(true)
    expect(a.toggle.disabled).toBe(true)
    expect(a.sync.busy).toBe(false)
  })
})

describe('header counts', () => {
  it('shows counts even when the feature is off — dormant is not empty', () => {
    const counts = headerCounts(status({ enabled: false }))
    expect(counts.map((c) => c.value)).toEqual([12, 3, 41])
  })

  it('coerces missing numbers to 0 rather than rendering NaN', () => {
    expect(headerCounts({ library: {} }).map((c) => c.value)).toEqual([0, 0, 0])
  })

  it('renders nothing without a status', () => {
    expect(headerCounts(null)).toEqual([])
  })
})

describe('grants payload shape', () => {
  // `service.list_access` wraps the rows: `{ grants: [...] }`. The panel first
  // shipped reading the payload AS the array, so every grant rendered as the
  // empty state and nothing could be revoked from the UI.
  const row = { id: 1, caller_agent: 'recon', skill_name: 'summarize', granted_by: 'admin', created_at: 'x' }

  it('unwraps the { grants } envelope the service actually sends', () => {
    expect(grantsFrom({ grants: [row] })).toEqual([row])
  })

  it('still accepts a bare list', () => {
    expect(grantsFrom([row])).toEqual([row])
  })

  it('is an empty list on any other shape, never a throw', () => {
    for (const bad of [null, undefined, {}, { grants: null }, { grants: {} }, 'nope', 42]) {
      expect(grantsFrom(bad)).toEqual([])
    }
  })

  it('the panel reads the envelope through grantsFrom, not Array.isArray(a.data)', () => {
    const panelSrc = readFileSync(
      fileURLToPath(new URL('../../src/components/settings/SkillRunnerPanel.vue', import.meta.url)), 'utf8'
    )
    expect(panelSrc).toMatch(/grants\.value = grantsFrom\(a\.data\)/)
    expect(panelSrc).not.toMatch(/Array\.isArray\(a\.data\) \? a\.data/)
  })
})

describe('grants', () => {
  const grants = [
    { caller_agent: 'atlas', skill_name: 'summarise' },
    { caller_agent: 'scribe', skill_name: 'translate' },
  ]

  it('spots a duplicate', () => {
    expect(isDuplicateGrant(grants, 'atlas', 'summarise')).toBe(true)
    expect(isDuplicateGrant(grants, 'atlas', 'translate')).toBe(false)
  })

  it('is case-SENSITIVE, because the backend matches verbatim', () => {
    // Lower-casing here would claim a duplicate the server does not see, and
    // the operator would be blocked from making a grant that is legal.
    expect(isDuplicateGrant(grants, 'Atlas', 'summarise')).toBe(false)
  })

  it('blocks a duplicate submission and says why', () => {
    const s = grantFormState(grants, 'atlas', 'summarise')
    expect(s.canSubmit).toBe(false)
    expect(s.reason).toMatch(/can already run/i)
  })

  it('gives no reason for a merely-incomplete form — that is not an error', () => {
    expect(grantFormState(grants, 'atlas', '')).toEqual({ canSubmit: false, reason: null })
  })

  it('allows a new pair', () => {
    expect(grantFormState(grants, 'atlas', 'translate').canSubmit).toBe(true)
  })

  it('names the CALLER in the revoke confirm, not just the skill', () => {
    // "Revoke summarise" reads harmlessly; the decision is which agent loses
    // the ability to execute it.
    const p = revokePrompt(grants[0])
    expect(p).toContain('atlas')
    expect(p).toContain('summarise')
  })

  it('filters by caller or skill', () => {
    expect(filterGrants(grants, 'atlas')).toHaveLength(1)
    expect(filterGrants(grants, 'TRANS')).toHaveLength(1)
    expect(filterGrants(grants, '')).toHaveLength(2)
    expect(filterGrants(null, 'x')).toEqual([])
  })
})

// --- the tab registration (ent#242) -----------------------------------------
//
// Source guards, because the entitled path cannot be rendered in a node
// environment and the local instance registers no enterprise modules
// (`enterprise_features: []`). These pin the two properties that decide who
// ever sees the panel.
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

const settingsSrc = readFileSync(
  fileURLToPath(new URL('../../src/views/Settings.vue', import.meta.url)), 'utf8'
)

describe('Settings tab registration', () => {
  it('is gated on the skill_runner entitlement, declaratively', () => {
    // The same `requires:` seam sso and credential-vault use — an unentitled
    // install does not render the tab at all, rather than showing a panel whose
    // every control 404s.
    expect(settingsSrc).toMatch(
      /\{\s*id:\s*'skill-runner'.*requires:\s*'skill_runner'/s
    )
  })

  it('is admin-only, matching require_human_admin on every endpoint', () => {
    const line = settingsSrc.split('\n').find((l) => l.includes("id: 'skill-runner'"))
    expect(line).toMatch(/adminOnly:\s*true/)
  })

  it('renders the panel only for its own tab', () => {
    expect(settingsSrc).toMatch(
      /<SkillRunnerPanel v-if="activeTab === 'skill-runner'"/
    )
  })
})

describe('loading treatment', () => {
  const panelSrc = readFileSync(
    fileURLToPath(new URL('../../src/components/settings/SkillRunnerPanel.vue', import.meta.url)),
    'utf8'
  )

  it('uses a skeleton, not the scanline beam (#2540)', () => {
    // The beam is CHART loading only; panels load with a skeleton keyed on
    // "no data yet". Caught by portalLoadingTreatment.spec.js's importer
    // allowlist when this panel first reached for ScanlineReveal.
    expect(panelSrc).toContain('SkeletonLoader')
    expect(panelSrc).not.toContain('ScanlineReveal')
  })

  it('gates on a view verdict, never a bare loading flag (#1927)', () => {
    expect(panelSrc).toMatch(/v-if="view\.state === 'loading'"/)
    expect(panelSrc).not.toMatch(/v-if="loading"/)
  })
})
