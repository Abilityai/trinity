import { test, expect } from '@playwright/test'

/**
 * The Skills lists scroll inside the window (trinity#3448, design-system p28).
 *
 * jsdom has no layout, and `skillsBoundedChain.mount.spec.js` can only pin the
 * flex chain's classes. Two defects passed that chain and every mounted spec,
 * and only a real engine showed them (found at the eyeball of trinity-enterprise#660):
 *
 *   1. The Library stayed 9,976 to 26,207 px tall. Each card's AssignedAgents
 *      renders a `sr-only` label, which is `position: absolute`, and BoundedRegion
 *      was not positioned, so the labels escaped the scroll box and Chrome counted
 *      them in the page's scrollable area. The cards scrolled in the box and the
 *      page below it was empty. Firefox does not show this, so this file has to
 *      run in Chromium.
 *   2. A 4 px strip of cards showed under the docked Shared heading. A sticky
 *      `bottom-0` stops at the scroller's padding edge, so padding on the scroller
 *      is a gap under the dock. It belongs on an inner element.
 *
 * WHAT IS PROVED, AND HOW (the shape of `operations-bounded-lists.spec.js`).
 *   - The page may exceed the window only when the region sits at its computed
 *     min-height: that is the shortfall. The floor is read from computed style,
 *     never restated here.
 *   - "The page never follows the item count": one window rendered with many
 *     skills and with a few gives the same page.
 *   - The gap is measured in ONE evaluate, and `elementFromPoint` on the box's last
 *     pixel row says what a reader actually sees there.
 *
 * CI scope: `@smoke`, hermetic. Every Skills read and the agent are synthetic
 * (`page.route`), so the zero-agent CI stack runs it.
 */

const AGENT = 'e2e-3448'
const isPath = (pathname) => (url) => new URL(url).pathname === pathname
const under = (prefix) => (url) => new URL(url).pathname.startsWith(prefix)

// Tall windows leave the region above its floor; 375 × 667 puts it on it.
const WINDOWS = [[1440, 900], [905, 1150], [768, 1024], [375, 667]]
// The dock is on from 640 px up.
const DOCKED_WINDOWS = WINDOWS.filter(([width]) => width >= 640)

const CATEGORIES = ['workspace', 'project-management', 'research-and-analysis']

function librarySkill(i) {
  return {
    name: `e2e-skill-${String(i).padStart(3, '0')}`,
    description: `Fixture skill ${i}, with a description long enough to wrap onto a second line of its card.`,
    path: `skills/e2e-skill-${i}/SKILL.md`,
    source_id: i % 2 ? 'src_aaaaaaaa' : 'src_bbbbbbbb',
    source_name: i % 2 ? 'Team skills' : 'Vendor skills',
    category: CATEGORIES[i % CATEGORIES.length],
    user_invocable: true,
    requires: { packages: [], binaries: [], env: [] },
    shadowed_by: [],
  }
}

function ownSkill(i) {
  const name = `e2e-own-${String(i).padStart(2, '0')}`
  return {
    name, dir: name, source: 'agent', description: `Own fixture ${i}`, path: `.claude/skills/${name}/SKILL.md`,
    user_invocable: true, automation: null, argument_hint: null, approval: null,
  }
}

const range = (n) => Array.from({ length: n }, (_, i) => i + 1)

/** The reads both surfaces share. `state` holds what they answer, so a test can reload with fewer. */
async function stubSkills(page, state) {
  await page.route(isPath('/api/skills/library/status'), (route) => route.fulfill({ json: {
    configured: true, cloned: true, skill_count: state.skills, commit_sha: 'abcdef1234', branch: 'main',
  } }))
  await page.route(isPath('/api/skills/library'), (route) => route.fulfill({
    json: range(state.skills).map(librarySkill),
  }))
  await page.route(isPath('/api/skills/library/sets'), (route) => route.fulfill({ json: [] }))
  // `assignable_agents` is what renders the card's assign control, and with it
  // the sr-only label of defect 1. Without an agent to offer, the bug cannot show.
  await page.route(isPath('/api/skills/assignments'), (route) => route.fulfill({ json: {
    assignments: {}, scope: 'all', assignable_agents: [{ name: 'e2e-target', display_label: null }],
  } }))
}

/** A running agent with `state.own` own skills and five shared ones. */
async function stubAgent(page, state) {
  const shared = [1, 2, 3, 4, 5].map(librarySkill)
  await page.route(under(`/api/agents/${AGENT}`), (route) => route.fulfill({ json: {} }))
  await page.route(isPath(`/api/agents/${AGENT}`), (route) => route.fulfill({ json: {
    name: AGENT, status: 'running', type: 'agent', can_share: true, can_delete: true, owner: 'admin', tags: [],
  } }))
  await page.route(isPath(`/api/agents/${AGENT}/playbooks`), (route) => route.fulfill({ json: {
    skills: range(state.own).map(ownSkill), count: state.own, skill_paths: ['.claude/skills'],
  } }))
  await page.route(isPath(`/api/agents/${AGENT}/skills`), (route) => route.fulfill({
    json: shared.map((s) => ({ skill_name: s.name, individual: true, via_sets: [], delivery_status: null })),
  }))
  await page.route(isPath(`/api/agents/${AGENT}/skill-sets`), (route) => route.fulfill({ json: [] }))
  await page.route(isPath(`/api/agents/${AGENT}/skill-gates`), (route) => route.fulfill({ json: {
    agent_name: AGENT, gates: [], cleared_defaults: [], approver_kinds: [], approvers: [], hook: null,
  } }))
}

// One evaluate, so every number describes the same frame.
function pageGeometry(page, regionTestId) {
  return page.evaluate((id) => {
    const region = document.querySelector(`[data-testid="${id}"]`)
    return {
      window: document.documentElement.clientHeight,
      page: document.documentElement.scrollHeight,
      regionHeight: Math.round(region.getBoundingClientRect().height),
      regionFloor: parseFloat(getComputedStyle(region).minHeight),
    }
  }, regionTestId)
}

function expectPageIsTheWindow(g) {
  if (g.regionHeight > g.regionFloor) {
    expect(g.page, `page ${g.page}px in a ${g.window}px window`).toBe(g.window)
  }
}

function dockGeometry(page) {
  return page.evaluate(() => {
    const region = document.querySelector('[data-testid="skills-region"]')
    const head = document.querySelector('[data-testid="skills-shared-head"]')
    const box = region.getBoundingClientRect()
    const dock = head.getBoundingClientRect()
    const x = box.left + box.width / 2
    const lastRow = document.elementFromPoint(x, box.bottom - 1)
    return {
      gapBelow: dock.bottom >= box.bottom ? 0 : box.bottom - dock.bottom,
      overshoot: dock.bottom - box.bottom,
      gapAbove: dock.top - box.top,
      lastRowIsHeading: Boolean(lastRow && head.contains(lastRow)),
    }
  })
}

const scrollBox = (page, to) => page.getByTestId('skills-region').evaluate((el, where) => {
  el.scrollTop = where === 'end' ? el.scrollHeight : Math.floor((el.scrollHeight - el.clientHeight) / 2)
}, to)

test.describe('Library → Skills never grows the page (trinity#3448)', () => {
  for (const [width, height] of WINDOWS) {
    test(`@smoke the page follows the window, not the library, at ${width}x${height}`, async ({ page }) => {
      await page.setViewportSize({ width, height })
      const state = { skills: 112 }
      await stubSkills(page, state)

      await page.goto('/library?tab=skills')
      await expect(page.getByTestId('library-skill-e2e-skill-112')).toBeAttached()
      await expect(page.getByTestId('library-skills-total')).toHaveText('112 skills')
      const full = await pageGeometry(page, 'library-skills-region')
      expectPageIsTheWindow(full)

      // The same window with a few skills: the same page, whatever the count.
      state.skills = 3
      await page.reload()
      await expect(page.getByTestId('library-skills-total')).toHaveText('3 skills')
      const few = await pageGeometry(page, 'library-skills-region')
      expect(full.page, `${full.page}px with 112 skills, ${few.page}px with 3`).toBe(few.page)
    })
  }
})

test.describe('the agent Skills tab (trinity#3448, trinity-enterprise#660)', () => {
  async function openTab(page, width, height, state = { skills: 30, own: 24 }) {
    await page.setViewportSize({ width, height })
    await stubSkills(page, state)
    await stubAgent(page, state)
    await page.goto(`/agents/${AGENT}?tab=skills`)
    await expect(page.getByTestId('skills-tab-total')).toHaveText(`${state.own} own · 5 shared`)
    await expect(page.getByTestId('skill-card-shared-e2e-skill-005')).toBeAttached()
  }

  for (const [width, height] of WINDOWS) {
    test(`@smoke the page follows the window, not the list, at ${width}x${height}`, async ({ page }) => {
      const state = { skills: 30, own: 24 }
      await openTab(page, width, height, state)
      const full = await pageGeometry(page, 'skills-region')
      expectPageIsTheWindow(full)

      state.own = 1
      await page.reload()
      await expect(page.getByTestId('skills-tab-total')).toHaveText('1 own · 5 shared')
      const few = await pageGeometry(page, 'skills-region')
      expect(full.page, `${full.page}px with 24 own skills, ${few.page}px with 1`).toBe(few.page)
    })
  }

  for (const [width, height] of DOCKED_WINDOWS) {
    test(`@smoke Shared's heading docks flush to the box at ${width}x${height}`, async ({ page }) => {
      await openTab(page, width, height)

      // On arrival, and with cards scrolling under it: 0 px of cards below it.
      for (const step of ['arrival', 'mid']) {
        if (step === 'mid') await scrollBox(page, 'mid')
        const dock = await dockGeometry(page)
        expect(Math.abs(dock.overshoot), `the dock's bottom against the box's (${step})`).toBeLessThan(0.5)
        expect(dock.gapBelow, `gap under the dock (${step})`).toBeLessThan(0.5)
        expect(dock.lastRowIsHeading, `the box's last pixel row is the heading (${step})`).toBe(true)
      }

      // Reached: it sits at the top edge, flush.
      await scrollBox(page, 'end')
      const dock = await dockGeometry(page)
      expect(Math.abs(dock.gapAbove)).toBeLessThan(0.5)
    })
  }

  test('@smoke below 640 px the heading does not dock', async ({ page }) => {
    await openTab(page, 375, 667)
    const dock = await dockGeometry(page)
    expect(dock.gapAbove, 'the heading waits below the fold').toBeGreaterThanOrEqual(0)
    expect(dock.overshoot, 'and is not pinned to the box').toBeGreaterThan(0)
    expect(dock.lastRowIsHeading).toBe(false)

    await scrollBox(page, 'end')
    expect(Math.abs((await dockGeometry(page)).gapAbove), 'it still sticks at the top').toBeLessThan(0.5)
  })
})
