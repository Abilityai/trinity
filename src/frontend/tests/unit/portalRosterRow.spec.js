// @vitest-environment jsdom
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
import { describe, it, expect, beforeEach, vi } from 'vitest'
import { mount, flushPromises } from '@vue/test-utils'
import { setActivePinia, createPinia } from 'pinia'
import { createRouter, createMemoryHistory } from 'vue-router'
import { visibleAgentRows, AGENT_COLLAPSE_LIMIT } from '../../src/components/portal/portalUtils.js'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { dirname, resolve } from 'node:path'

vi.mock('@/stores/auth', () => ({
  useAuthStore: () => ({ isAuthenticated: true, authHeader: {}, logout: vi.fn() }),
}))
vi.mock('axios', () => {
  const mk = () => ({
    get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  })
  return {
    default: Object.assign(
      { get: vi.fn(), post: vi.fn(), put: vi.fn(), delete: vi.fn(), create: mk },
      { interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
        defaults: { headers: { common: {} } } },
    ),
  }
})

import PortalSidebar from '@/components/portal/PortalSidebar.vue'

let router
beforeEach(async () => {
  setActivePinia(createPinia())
  router = createRouter({
    history: createMemoryHistory(),
    routes: [{ path: '/:p(.*)*', component: { template: '<div />' } }],
  })
  await router.push('/workspace')
  await router.isReady()
})

/** The rendered agent rows, in render order. The row's title is the only thing
 *  unique to it ('… · <key hint> switch agent'); the name is its own line. */
const agentRowNames = (w) => w.findAll('button')
  .filter((b) => (b.attributes('title') || '').includes('switch agent'))
  .map((b) => b.findAll('span').map((s) => s.text()).find((t) => /^a\d+$/.test(t)))

// Resolved off `dirname(import.meta.url)` rather than `new URL(rel, …)`:
// under jsdom the global `URL` is jsdom's own, and `fileURLToPath` rejects it.
const SRC = resolve(dirname(fileURLToPath(import.meta.url)), '../../src/components/portal/PortalSidebar.vue')
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
    // ent#523 added a preview line that takes this slot when there is history,
    // so the subtitle is now the `v-else-if`. The RULE is unchanged and is what
    // is asserted: the condition is `agentLabel`'s own decision, never a second
    // truthiness test that drifts from it on a whitespace-only label.
    expect(row).toMatch(/v-else-if="agentLabel\(a\) !== a\.name"/)
    expect(row).not.toMatch(/v-if="a\.display_label"/)
  })
})

describe('#2159 the roster is bounded and expandable', () => {
  it('shows a fixed number by default rather than the whole fleet', () => {
    // #2424 moved the rule into portalUtils, so this asserts the PROPERTY
    // rather than the old inline slice expression. Same guarantee, and now it
    // fails on a broken bound instead of only on a reworded one.
    const roster = Array.from({ length: 12 }, (_, i) => ({ name: `a${i}` }))
    const collapsed = visibleAgentRows(roster, { expanded: false, askCounts: {} })
    expect(collapsed).toHaveLength(AGENT_COLLAPSE_LIMIT)
    expect(AGENT_COLLAPSE_LIMIT).toBeLessThan(roster.length)
    expect(visibleAgentRows(roster, { expanded: true, askCounts: {} })).toHaveLength(12)
    // (That the COMPONENT still routes its rows through it is asserted below by
    // mounting it — see 'the mounted sidebar renders the bounded list'.)
  })

  it('the mounted sidebar renders the bounded list, in the order it was handed', async () => {
    // ent#621 T6 lifted the SORT into `Portal.vue` — one order, computed once,
    // handed to both sidebar instances so the switch-agent keys can walk
    // exactly the order the eye reads. This used to be two regexes over the
    // sidebar's source (`visibleAgentRows(orderedRoster.value` + the
    // `orderedRoster` computed), which no longer live in this file and, more to
    // the point, proved only that the code had been written. Mounted instead:
    // the bound is a count of RENDERED rows, and "the sidebar does not re-sort"
    // is a deliberately un-sorted roster coming back out in the same order —
    // a re-sort that slipped back in (alphabetical, or by thread recency) fails
    // this even though it would satisfy any spelling of the old regex.
    const roster = Array.from({ length: 12 }, (_, i) => ({ name: `a${(i * 7) % 12}` }))
    const w = mount(PortalSidebar, {
      props: { roster, threads: [], clientEmail: 'me@example.com' },
      global: { plugins: [router] },
    })
    await flushPromises()
    expect(agentRowNames(w)).toEqual(roster.slice(0, AGENT_COLLAPSE_LIMIT).map((a) => a.name))

    // ...and the same list unbounded once expanded, still in the given order.
    await w.findAll('button')
      .find((b) => (b.attributes('aria-expanded') !== undefined))
      .trigger('click')
    await flushPromises()
    expect(agentRowNames(w)).toEqual(roster.map((a) => a.name))
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
