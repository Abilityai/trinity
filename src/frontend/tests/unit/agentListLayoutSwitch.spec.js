// @vitest-environment jsdom
/**
 * #3465 — which of the Dashboard list's three row layouts renders is decided
 * by the LIST's width, not only the window's.
 *
 * The defect: the tablet layout switched on a viewport `md:` alone. At a 768
 * window with the systems rail open the list is about 496px wide, and the
 * tablet row's second line needs about 610px (three reserved toggles, the
 * success bar, the meter, the task counts). The `flex-1 min-w-0` success cell
 * collapsed to 0 with its text painting over the meter, and the no-wrap task
 * counts ran 50-67px past the row's right edge.
 *
 * jsdom has no layout engine and evaluates no media or container query, so a
 * mount alone cannot see which layout is displayed. This spec therefore does
 * the two halves it CAN do for real, and nothing by regex:
 *
 *   1. mounts `AgentListPanel` and takes the class lists off the three
 *      rendered layout blocks of a row, and
 *   2. compiles exactly those classes through the real `tailwind.config.js`
 *      and resolves the `display` cascade for a simulated (window, list)
 *      width pair — source order, enclosing `@media` / `@container` and all.
 *
 * That catches what a class-string read cannot: a variant that is misspelled
 * or unregistered (it emits nothing), and two rules whose winner depends on
 * the order Tailwind emits them in (design-system-contract.md, #2662).
 *
 * What it cannot prove is that the tablet row FITS at the threshold — that is
 * a browser measurement (e2e/dashboard-list-view.spec.js, #3465 case).
 */
import { describe, it, expect, vi, beforeAll, beforeEach } from 'vitest'
import { mount } from '@vue/test-utils'
import { createPinia, setActivePinia } from 'pinia'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import postcss from 'postcss'
import tailwindcss from 'tailwindcss'
import loadConfig from 'tailwindcss/loadConfig.js'

vi.mock('vue-router', () => ({ useRouter: () => ({ push: vi.fn() }) }))
vi.mock('axios', () => {
  const inst = {
    get: vi.fn(() => Promise.resolve({ data: {} })), post: vi.fn(), put: vi.fn(), delete: vi.fn(),
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
    defaults: { headers: { common: {} } },
  }
  return { default: Object.assign(inst, { create: () => inst }) }
})

import AgentListPanel from '../../src/components/AgentListPanel.vue'

const HERE = dirname(fileURLToPath(import.meta.url))
const REM = 16

const STUBS = {
  AgentAvatar: true, RuntimeBadge: true, RunningStateToggle: true, AutonomyToggle: true,
  ReadOnlyToggle: true, CapacityMeter: true, RouterLink: true,
}

/** The three layout blocks of the first rendered row, as class lists. */
function mountedLayouts() {
  const wrapper = mount(AgentListPanel, {
    props: {
      agents: [{ name: 'alpha', status: 'running', runtime: 'claude-code', tags: [], is_owner: true }],
    },
    global: { stubs: STUBS },
  })
  const row = wrapper.find('[data-agent="alpha"]').element
  const lg = wrapper.find('[data-testid="row-secondary-lg"]').element.parentElement
  const md = wrapper.find('[data-testid="row-secondary-md"]').element.parentElement
  // The avatar and the lg / md blocks precede it; the base block is last.
  const base = row.lastElementChild
  expect(new Set([lg, md, base]).size, 'three distinct layout blocks').toBe(3)
  for (const el of [lg, md, base]) expect(el.parentElement).toBe(row)
  return {
    html: wrapper.html(),
    lg: [...lg.classList],
    md: [...md.classList],
    base: [...base.classList],
  }
}

async function compile(html) {
  const config = loadConfig(resolve(HERE, '../../tailwind.config.js'))
  const result = await postcss([
    tailwindcss({ ...config, content: [{ raw: html }], corePlugins: { preflight: false } }),
  ]).process('@tailwind utilities;', { from: undefined })
  return result.root
}

/** Does this at-rule hold for the simulated widths? Unknown shapes throw. */
function atRuleHolds(node, { viewport, list }) {
  if (node.name === 'media') {
    const m = node.params.match(/^\(min-width:\s*([\d.]+)px\)$/)
    if (!m) throw new Error(`unmodelled @media ${node.params}`)
    return viewport >= Number(m[1])
  }
  if (node.name === 'container') {
    const m = node.params.match(/^agent-list\s+\(min-width:\s*([\d.]+)rem\)$/)
    if (!m) throw new Error(`unmodelled @container ${node.params}`)
    return list >= Number(m[1]) * REM
  }
  throw new Error(`unmodelled at-rule @${node.name}`)
}

/**
 * The `display` an element with these classes computes to. Every rule here is
 * a single class selector (equal specificity), so the last one in source order
 * whose enclosing conditions hold is the winner — the same answer the browser
 * gives.
 */
function displayOf(root, classes, widths) {
  let winner = 'block' // a <div> with no display utility
  root.walkRules((rule) => {
    const cls = rule.selector.replace(/^\./, '').replace(/\\/g, '')
    if (!classes.includes(cls)) return
    let display = null
    rule.walkDecls('display', (d) => { display = d.value })
    if (display === null) return
    for (let p = rule.parent; p && p.type === 'atrule'; p = p.parent) {
      if (!atRuleHolds(p, widths)) return
    }
    winner = display
  })
  return winner
}

describe('the list row layout follows the list width (#3465)', () => {
  let layouts
  let css

  beforeEach(() => setActivePinia(createPinia()))
  beforeAll(async () => {
    setActivePinia(createPinia())
    layouts = mountedLayouts()
    css = await compile(layouts.html)
  })

  const shown = (widths) =>
    ['lg', 'md', 'base'].filter((k) => displayOf(css, layouts[k], widths) !== 'none')

  it('the reported case: a 768 window with the rail open gets the compact row', () => {
    // 768 - 224 (rail) - 48 (panel padding) = 496. The tablet row's fixed
    // parts alone are wider than that.
    expect(shown({ viewport: 768, list: 496 })).toEqual(['base'])
  })

  it('a 768 window with the rail collapsed keeps the tablet row', () => {
    expect(shown({ viewport: 768, list: 664 })).toEqual(['md'])
  })

  it('renders exactly one layout at every width, switching at 40rem and 68rem', () => {
    const cases = [
      // [window, list, layout]
      [390, 342, 'base'],
      // Below `md` the window still decides: unchanged by #3465.
      [700, 652, 'base'],
      [768, 639, 'base'],
      [768, 640, 'md'],
      [1024, 752, 'md'],
      [1280, 992, 'md'],
      [1440, 1087, 'md'],
      [1440, 1088, 'lg'],
      [1440, 1152, 'lg'],
      [1920, 1632, 'lg'],
    ]
    for (const [viewport, list, layout] of cases) {
      expect(shown({ viewport, list }), `window ${viewport}px, list ${list}px`).toEqual([layout])
    }
  })

  it('the desktop row is a set of grid items, never a box', () => {
    expect(displayOf(css, layouts.lg, { viewport: 1440, list: 1152 })).toBe('contents')
  })
})
