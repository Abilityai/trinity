import { test, expect } from '@playwright/test'

/**
 * trinity-enterprise#950 — the agent header after the ent#887 rewrite.
 *
 * The header shows the bound repository (linked, branch, changes / behind),
 * the tags (two chips and a counted "+N"); the context ring it once had went in ent#955
 * for a running agent. The Talk and Workspace doors are gone: the Workspace is
 * in the nav and the voice call lives in its composer (ent#547).
 *
 * HERMETIC, like the Talk-door spec this replaces: CI's e2e baseline holds zero
 * user agents (the ent#124 seeder sentinel), so the agent exists only in the
 * route stubs below. Every read the header depends on is fulfilled at the
 * network boundary with a WORST-CASE payload — a long repo path, a long branch
 * and ten long tags — because the #2197 class (a rigid row widening the page)
 * only shows when the row is full. The live `body-horizontal-overflow` sweep
 * measures whatever the stack happens to hold, which is usually an agent with
 * no repo and no tags.
 */
const AGENT = 'e2e-agent-header'

const LONG_TAGS = Array.from({ length: 10 }, (_, i) => `a-rather-long-tag-name-${i + 1}`)
const GIT_STATUS = {
  git_enabled: true,
  branch: 'trinity/e2e-agent-header/an-unusually-long-working-branch-name',
  pull_branch: 'main',
  remote_url: 'https://github.com/an-organisation-with-a-long-name/a-repository-with-an-even-longer-name.git',
  last_commit: null,
  changes: [],
  changes_count: 12,
  ahead: 1,
  behind: 7,
}

// Name the offender on failure (the #2197 spec's helper): the widest element
// not inside a clipping ancestor. A bare scrollWidth says only that the page
// is broken — the first two runs of this spec needed a screenshot to say by what.
const BLAME = () => {
  const vw = document.documentElement.clientWidth
  const clipped = (el) => {
    for (let a = el.parentElement; a; a = a.parentElement) {
      if (getComputedStyle(a).overflowX !== 'visible' && a.getBoundingClientRect().right <= vw + 1) return true
    }
    return false
  }
  const hits = []
  for (const el of document.querySelectorAll('body *')) {
    const r = el.getBoundingClientRect()
    if ((r.width === 0 && r.height === 0) || r.right <= vw + 1 || clipped(el)) continue
    hits.push(`${el.tagName}.${String(el.className).split(' ').slice(0, 3).join('.')} +${Math.round(r.right - vw)}px "${(el.textContent || '').trim().slice(0, 24)}"`)
  }
  return hits.slice(0, 3)
}

const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

async function stubAgent(page) {
  await page.route(`**/api/agents/${AGENT}`, (route) => route.fulfill(json({
    name: AGENT,
    status: 'running',
    port: 2299,
    created: new Date().toISOString(),
    resources: { cpu: '1', memory: '1g' },
    container_id: 'e2e-agent-header-container',
    template: 'local:test-echo',
    git_configured: true,
    runtime: 'claude-code',
    ephemeral: false,
    display_label: 'A header with everything in it',
    owner: 'admin',
    is_owner: true,
    is_shared: false,
    is_system: false,
    can_share: true,
    can_delete: true,
    autonomy_enabled: false,
    read_only_enabled: false,
    avatar_url: null,
    shares: [],
    circuit_breaker: null,
  })))
  await page.route(`**/api/agents/${AGENT}/git/status`, (route) => route.fulfill(json(GIT_STATUS)))
  await page.route(`**/api/agents/${AGENT}/tags`, (route) => route.fulfill(json({ agent_name: AGENT, tags: LONG_TAGS })))
  await page.route('**/api/agents/context-stats', (route) => route.fulfill(json({
    agents: [{
      name: AGENT,
      status: 'running',
      activityState: 'idle',
      contextPercent: 72.4,
      contextUsed: 144800,
      contextMax: 200000,
      lastActivityTime: null,
    }],
  })))
}

test.describe('the agent header (ent#950)', () => {
  test.beforeEach(async ({ page }) => { await stubAgent(page) })

  test('@smoke shows repo and tags — and no Talk or Workspace door', async ({ page }) => {
    await page.goto(`/agents/${AGENT}`)
    const repo = page.getByTestId('agent-header-repo')
    await expect(repo).toBeVisible({ timeout: 20000 })
    await expect(repo.locator('a')).toHaveAttribute(
      'href', 'https://github.com/an-organisation-with-a-long-name/a-repository-with-an-even-longer-name',
    )
    await expect(page.getByTestId('agent-header-git-changes')).toContainText('12 changed')
    await expect(page.getByTestId('agent-header-git-ahead')).toContainText('1 ahead')
    await expect(page.getByTestId('agent-header-git-behind')).toContainText('7 behind')

    await expect(page.getByTestId('agent-header-tag')).toHaveCount(2)
    await expect(page.getByTestId('agent-header-tags-more')).toHaveText('+8')


    await expect(page.getByTestId('agent-talk')).toHaveCount(0)
    await expect(page.getByRole('button', { name: 'Workspace', exact: true })).toHaveCount(0)
  })

  test('@smoke the tag editor opens in a dialog and Esc closes it', async ({ page }) => {
    await page.goto(`/agents/${AGENT}`)
    await page.getByTestId('agent-header-tags-edit').click()
    const dialog = page.getByRole('dialog', { name: 'Agent tags' })
    await expect(dialog).toBeVisible()
    await expect(dialog.getByText(`#${LONG_TAGS[9]}`)).toBeVisible()
    await page.keyboard.press('Escape')
    await expect(dialog).toHaveCount(0)
  })

  for (const width of [375, 768, 1440]) {
    test(`@smoke a full header never widens the page at ${width}px (#2197)`, async ({ page }) => {
      await page.setViewportSize({ width, height: 812 })
      await page.goto(`/agents/${AGENT}`)
      await expect(page.getByTestId('agent-header-repo')).toBeVisible({ timeout: 20000 })
      await page.waitForTimeout(400) // tab-strip ResizeObserver settles after paint
      const r = await page.evaluate(() => ({
        scrollWidth: document.documentElement.scrollWidth,
        clientWidth: document.documentElement.clientWidth,
      }))
      const blame = r.scrollWidth > r.clientWidth ? await page.evaluate(BLAME) : []
      expect(r.scrollWidth, `body scrolls horizontally by ${r.scrollWidth - r.clientWidth}px. Widest unclipped: ${blame.join(' | ') || 'none found'}`)
        .toBeLessThanOrEqual(r.clientWidth)
      // Fitting is not enough: the first CI run of this spec fit nothing at
      // 375px by crushing the repo name to zero width while the badges ran off
      // the card. The name must stay legible, and every badge inside the card.
      const fit = await page.evaluate(() => {
        const card = document.querySelector('[data-testid="agent-header-meta"]').getBoundingClientRect()
        const name = document.querySelector('[data-testid="agent-header-repo"] a').getBoundingClientRect()
        const badges = [...document.querySelectorAll('[data-testid^="agent-header-git-"]')]
          .map((b) => b.getBoundingClientRect().right)
        return { nameWidth: name.width, cardRight: card.right, badgeRight: Math.max(...badges) }
      })
      expect(fit.nameWidth, 'repo name crushed').toBeGreaterThanOrEqual(64)
      expect(fit.badgeRight, 'a git badge runs past the header').toBeLessThanOrEqual(fit.cardRight + 1)
    })
  }

  test('@smoke the GitHub line landing moves nothing below the header (#3872)', async ({ page }) => {
    // The live git status is an exec into the running container and lands
    // seconds after first paint. The fixture agent is a LOCAL template bound
    // afterwards (`git_configured`), the shape that used to pop: the row was
    // reserved on the template string alone. Hold the status until the page
    // has painted its reserved row, measure where the tab strip sits, release
    // the status, and measure again — the strip must not have moved, and the
    // row must be the same height before and after (principle 6).
    let release
    const held = new Promise((resolve) => { release = resolve })
    await page.route(`**/api/agents/${AGENT}/git/status`, async (route) => {
      await held
      await route.fulfill(json(GIT_STATUS))
    })

    await page.goto(`/agents/${AGENT}`)
    const row = page.getByTestId('agent-header-git')
    await expect(row.locator('[aria-busy="true"]')).toBeVisible({ timeout: 20000 })
    await page.waitForTimeout(300) // tab-strip ResizeObserver settles after paint
    const before = await page.evaluate(() => ({
      tabs: document.querySelector('[role="tablist"]').getBoundingClientRect().top,
      row: document.querySelector('[data-testid="agent-header-git"]').getBoundingClientRect().height,
    }))

    release()
    await expect(page.getByTestId('agent-header-repo')).toBeVisible()
    await expect(page.getByTestId('agent-header-git-changes')).toContainText('12 changed')
    await page.waitForTimeout(300)
    const after = await page.evaluate(() => ({
      tabs: document.querySelector('[role="tablist"]').getBoundingClientRect().top,
      row: document.querySelector('[data-testid="agent-header-git"]').getBoundingClientRect().height,
    }))

    expect(after.row, 'row 4 changed height when the status landed').toBe(before.row)
    expect(after.tabs, 'the tab strip moved when the status landed').toBe(before.tabs)
  })

  test('@smoke the chat composer has no microphone', async ({ page }) => {
    // Carried over from the retired Talk-door spec: removing the header door
    // must not bring the composer mic back on Agent Detail.
    await page.goto(`/agents/${AGENT}?tab=chat`)
    await expect(page.getByTestId('agent-header-meta')).toBeVisible({ timeout: 20000 })
    await expect(page.locator('button[title="Start voice conversation"]')).toHaveCount(0)
    await expect(page.locator('button[title="Voice session active"]')).toHaveCount(0)
  })
})
