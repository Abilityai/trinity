/**
 * #2196 — the roster availability chip.
 *
 * The Workspace lists agents whose container no longer exists. The fix is NOT to
 * hide them (see the backend tests for why that would empty a customer's roster
 * on any Docker fault) — it is to label them. So the rule this file guards is:
 * a chip for the two non-ready states, nothing for the other two, and no control
 * ever disabled.
 *
 * The rule lives in `portalUtils.js` rather than as a `v-if` in four templates
 * for a mechanical reason: this project has no component-mount harness
 * (`package.json` carries no @vue/test-utils, jsdom or happy-dom), so a rule
 * expressed in a template can only be guarded by regexing source — which catches
 * deletion but never a WRONG rule, and breaks on any reformat. A pure function
 * is genuinely executed. The source-regex assertions at the bottom are therefore
 * scoped to what only source can answer: that the surfaces consume the shared
 * helper, and that nothing became disabled.
 */
import { describe, it, expect } from 'vitest'
import { agentRowTitle } from '../../src/components/portal/portalUtils.js'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import { availabilityChip } from '../../src/components/portal/portalUtils'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')
const SIDEBAR = read('../../src/components/portal/PortalSidebar.vue')
const AGENT_PAGE = read('../../src/components/portal/PortalAgentPage.vue')

describe('#2196 which states get a chip', () => {
  it('labels an agent whose container is gone', () => {
    const chip = availabilityChip({ availability: 'unavailable', owner: 'alice' })
    expect(chip).toBeTruthy()
    expect(chip.label).toBe('Unavailable')
  })

  it('labels a stopped agent too', () => {
    // Not a half-truth: a chip that appeared ONLY for containerless agents would
    // imply stopped agents are fine to chat with. They are not — they get a 502.
    expect(availabilityChip({ availability: 'stopped' })).toBeTruthy()
  })

  it('renders nothing for a healthy agent', () => {
    expect(availabilityChip({ availability: 'ready' })).toBeNull()
  })

  it('renders nothing for `unknown` — the fail-open direction', () => {
    // `unknown` means Trinity could not read container state at all, and one
    // unreadable socket marks EVERY card at once. Labelling it would put a
    // warning across the whole roster over an infrastructure fault the viewer
    // can neither see nor act on.
    expect(availabilityChip({ availability: 'unknown' })).toBeNull()
  })

  it('renders nothing for an absent or unrecognised value', () => {
    // An older payload predates the field entirely; anything unexpected must
    // degrade to today's appearance rather than to a scary chip.
    expect(availabilityChip({})).toBeNull()
    expect(availabilityChip(null)).toBeNull()
    expect(availabilityChip({ availability: 'running' })).toBeNull()   // Docker's word, not the card's
  })
})

describe('#2196 what the chip says', () => {
  it('names the owner, because the card already carries them', () => {
    const chip = availabilityChip({ availability: 'unavailable', owner: 'alice' })
    expect(chip.title).toContain('alice')
    expect(chip.title).toContain('start it')
  })

  it('falls back to "its owner" when the card has none', () => {
    const chip = availabilityChip({ availability: 'unavailable' })
    expect(chip.title).toContain('its owner')
  })

  it('never tells the viewer to try again', () => {
    // For both of these states, retrying cannot work — that is the misleading
    // half of the copy this issue is fixing.
    for (const availability of ['stopped', 'unavailable']) {
      for (const detailed of [false, true]) {
        const chip = availabilityChip({ availability }, { detailed })
        expect(chip.title.toLowerCase()).not.toContain('try again')
      }
    }
  })

  it('uses semantic variants only — never a raw palette colour', () => {
    for (const availability of ['stopped', 'unavailable']) {
      const chip = availabilityChip({ availability }, { detailed: true })
      expect(chip.variant).toMatch(/^(warning|danger)$/)
    }
  })
})

describe('#2196 the two states are collapsed for an external client', () => {
  it('shows one label for both to a client', () => {
    // The states differ only in whether the operator deleted or lost the agent.
    // Neither is actionable for an external client — the Workspace has no start
    // control, by design — so distinguishing them discloses operator-internal
    // state for no benefit.
    const stopped = availabilityChip({ availability: 'stopped' })
    const gone = availabilityChip({ availability: 'unavailable' })
    expect(stopped.label).toBe(gone.label)
    expect(stopped.title).toBe(gone.title)
  })

  it('distinguishes them for a platform session', () => {
    // An operator looking at their own fleet: for them it IS the difference
    // between "start it" and "find out what happened to it".
    const stopped = availabilityChip({ availability: 'stopped' }, { detailed: true })
    const gone = availabilityChip({ availability: 'unavailable' }, { detailed: true })
    expect(stopped.label).not.toBe(gone.label)
    expect(stopped.variant).not.toBe(gone.variant)
  })

  it('carries the raw state through so a caller can still branch on it', () => {
    expect(availabilityChip({ availability: 'stopped' }).state).toBe('stopped')
  })
})

describe('#2196 the surfaces consume the shared rule', () => {
  it('the sidebar row renders the chip from portalUtils', () => {
    expect(SIDEBAR).toMatch(/availabilityChip/)
    expect(SIDEBAR).toMatch(/<BaseBadge[^>]*chipFor\(a\)\.variant/)
  })

  it('the agent page uses the SAME helper, not a second rule', () => {
    // Four inline conditions across four components is how four surfaces end up
    // disagreeing about the same agent.
    expect(AGENT_PAGE).toMatch(/availabilityChip/)
    expect(AGENT_PAGE).not.toMatch(/availability === 'unavailable'/)
  })

  it('the agent page keeps availability BESIDE health, not inside it', () => {
    // Health is the last persisted agent_health_checks row — stale by design —
    // while availability is read at request time. One dot carrying both
    // freshness semantics tells the viewer neither.
    expect(AGENT_PAGE).toMatch(/healthDot/)
    expect(AGENT_PAGE).toMatch(/availabilityDot/)
    expect(AGENT_PAGE).not.toMatch(/healthDot.*availability|availability.*: healthDot/)
  })

  it('nothing is disabled by availability', () => {
    // The reversal that survived review: disabling would relocate the dead state
    // rather than remove it — a client whose agents are all stopped (a routine
    // resource-saving posture) would get an entirely inert Workspace.
    for (const src of [SIDEBAR, AGENT_PAGE]) {
      expect(src).not.toMatch(/:disabled="[^"]*availability/)
      expect(src).not.toMatch(/:disabled="[^"]*chipFor/)
    }
  })

  it('the roster is not re-sorted by availability', () => {
    // Rows would jump as agents start and stop — the design system's
    // layout-stability rule forbids that, and the top-5 fold is #2159's design.
    const shown = SIDEBAR.match(/const shownAgents = computed\([\s\S]*?\)\)/)
    expect(shown).toBeTruthy()
    expect(shown[0]).not.toMatch(/sort|availability/)
  })

  it('the chip slot reserves its footprint so the row does not reflow', () => {
    const block = SIDEBAR.slice(SIDEBAR.indexOf('v-for="a in shownAgents"'))
    const row = block.slice(0, block.indexOf('</button>'))
    expect(row).toMatch(/min-w-\[[\d.]+rem\][^"]*flex justify-end/)
  })

  it('the row title carries the state, so it is reachable without colour', () => {
    // #2424 moved the composition into portalUtils::agentRowTitle, so this
    // asserts the property instead of the old inline ternary. Stronger: it now
    // catches a chip title that is dropped as well as one that is reworded.
    const withChip = agentRowTitle({
      label: 'a', name: 'a', chipTitle: 'This agent is stopped — ask admin to start it.',
    })
    expect(withChip).toContain('This agent is stopped')
    expect(agentRowTitle({ label: 'a', name: 'a' })).not.toMatch(/—/)
    // #2424 additionally requires a pending ask to be reachable the same way.
    expect(agentRowTitle({ label: 'a', name: 'a', askCount: 2 })).toMatch(/2 asks/i)
  })
})
