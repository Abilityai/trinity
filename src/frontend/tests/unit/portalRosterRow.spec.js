/**
 * #2159 — the sidebar agent roster: loading state, top-5, label-not-slug.
 *
 * Three defects that only compound on a real fleet: an empty block during a slow
 * load (reads as hung, not loading), the whole roster rendered at once (chats
 * pushed below the fold), and the slug as the row title with the description as
 * subtitle (the description is not identity — two agents can share one, and it
 * pushed the only unique handle off the row).
 *
 * The label fallback is the part worth executing rather than eyeballing: NULL
 * `display_label` means "render the slug" (ent#181), and the backend
 * deliberately does NOT coalesce it, so if this end forgot to, every unlabelled
 * agent would render as blank.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

const SRC = fileURLToPath(new URL('../../src/components/portal/PortalSidebar.vue', import.meta.url))
const source = readFileSync(SRC, 'utf8')

/** Extract `const agentLabel = (a) => ...` and run it. */
const agentLabel = (() => {
  const m = source.match(/const agentLabel = \(a\) => [^\n]+/)
  expect(m, 'agentLabel is gone').toBeTruthy()
  // eslint-disable-next-line no-new-func
  return new Function(`${m[0]}; return agentLabel`)()
})()

describe('#2159 row title is the human-facing name', () => {
  it('uses display_label when set', () => {
    expect(agentLabel({ name: 'evt-boss', display_label: 'Due Diligence' })).toBe('Due Diligence')
  })

  it('falls back to the slug when unset — NULL means "render the slug"', () => {
    // The backend passes NULL through on purpose. If this end also declined to
    // resolve it, every unlabelled agent would render blank.
    expect(agentLabel({ name: 'evt-boss', display_label: null })).toBe('evt-boss')
    expect(agentLabel({ name: 'evt-boss' })).toBe('evt-boss')
  })

  it('treats a whitespace-only label as unset', () => {
    expect(agentLabel({ name: 'evt-boss', display_label: '   ' })).toBe('evt-boss')
  })
})

describe('#2159 the row no longer shows the description', () => {
  it('renders the slug as the subtitle, not the description', () => {
    // The description is not identity: two agents can share one, and rendering
    // it as the subtitle pushed the only unique handle off the row entirely.
    const block = source.slice(source.indexOf('v-for="a in shownAgents"'))
    const row = block.slice(0, block.indexOf('</button>'))
    expect(row).toMatch(/\{\{ a\.name \}\}/)
    expect(row).not.toMatch(/a\.description/)
  })

  it('shows the subtitle only when the title is NOT already the slug', () => {
    // A raw `v-if="a.display_label"` disagrees with agentLabel on a
    // whitespace-only label: agentLabel calls it unset and falls back to the
    // slug, while raw truthiness still renders the subtitle — so the row
    // prints the slug as both title and subtitle. The condition has to be the
    // same decision agentLabel makes, not a second one that can drift from it.
    const block = source.slice(source.indexOf('v-for="a in shownAgents"'))
    const row = block.slice(0, block.indexOf('</button>'))
    expect(row).toMatch(/v-if="agentLabel\(a\) !== a\.name"/)
    expect(row).not.toMatch(/v-if="a\.display_label"/)
  })
})

describe('#2159 the roster is bounded and expandable', () => {
  it('shows a fixed number by default rather than the whole fleet', () => {
    expect(source).toMatch(/const AGENT_COLLAPSE_LIMIT = \d+/)
    expect(source).toMatch(/agentsExpanded\.value \? props\.roster : props\.roster\.slice\(0, AGENT_COLLAPSE_LIMIT\)/)
  })

  it('uses ONE persistent toggle, not two v-if-alternated buttons', () => {
    // #2101's lesson: alternating two elements drops keyboard focus on collapse.
    const toggles = source.match(/@click="agentsExpanded = !agentsExpanded"/g) || []
    expect(toggles.length).toBe(1)
    expect(source).toMatch(/:aria-expanded="agentsExpanded"/)
  })

  it('only offers the toggle when there is something to expand', () => {
    expect(source).toMatch(/v-if="roster\.length > AGENT_COLLAPSE_LIMIT"/)
  })
})

describe('#2159 loading is distinguishable from empty', () => {
  it('renders a skeleton while loading with nothing yet', () => {
    expect(source).toMatch(/v-if="loadingRoster && !roster\.length"/)
    expect(source).toMatch(/aria-busy="true"/)
    expect(source).toMatch(/Loading your agents/)
  })

  it('suppresses the empty state while loading', () => {
    // Otherwise a slow fleet shows "No agents shared with you yet" and then
    // takes it back — worse than the blank it replaced.
    expect(source).toMatch(/v-if="!roster\.length && !loadingRoster"/)
  })
})
