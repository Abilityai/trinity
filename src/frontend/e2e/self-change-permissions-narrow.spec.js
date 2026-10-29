import { test, expect } from '@playwright/test'

/**
 * Settings › Guardrails › "Permissions to change itself" at phone width
 * (#3542, and the PR #3570 review).
 *
 * At 390px each permission kept the desktop row — toggle and label, then the
 * "Granted by <who> · <time>" status beside them — so the label column was a
 * few words wide, the status wrapped a word or two per line, no two rows put
 * the status at the same x, and a granter that is an email (one unbreakable
 * word) ran past the card. Below `sm` a row now stacks: toggle and label on
 * one line, the status on its own line, the description under both. From
 * `sm` up the desktop row is unchanged.
 *
 * HERMETIC, like `agent-header.spec.js`: CI's e2e baseline holds no user
 * agents, so the agent exists only in the route stubs below, and the grants
 * are a WORST-CASE payload — an email granter too long for one line even at
 * full width, a multi-word name, a short one, and rows that are off — because
 * the defect only shows when the status line is long.
 *
 * jsdom does no layout, so this is the only place the claim can be measured:
 * boxes from one render, judged against the row's own edges (never an
 * absolute pixel floor, which would pin the host's scrollbar).
 */
const AGENT = 'e2e-self-change'
// No hyphen: a hyphen is a break opportunity, and dots and `@` are not, so
// this is one unbreakable run — the case the #3542 wrap could not shrink.
const LONG_EMAIL = 'averylongreviewer.name.for.release.testing@examplecompany.com'
const CAPS = ['agents.manage', 'instructions.manage', 'projects.manage', 'schedules.manage', 'skills.manage']
const GRANTERS = {
  'skills.manage': LONG_EMAIL,
  'instructions.manage': 'admin',
  'agents.manage': 'A Reviewer With A Long Display Name',
}

const json = (body) => ({ status: 200, contentType: 'application/json', body: JSON.stringify(body) })

async function openPanel(page, { width, theme }) {
  const grantedAt = new Date(Date.now() - 3 * 3600 * 1000).toISOString()
  await page.addInitScript((t) => localStorage.setItem('trinity-theme', t), theme)
  await page.setViewportSize({ width, height: 900 })
  await page.route(`**/api/agents/${AGENT}`, (route) => route.fulfill(json({
    name: AGENT,
    status: 'running',
    port: 2299,
    created: new Date().toISOString(),
    resources: { cpu: '1', memory: '1g' },
    container_id: `${AGENT}-container`,
    template: 'local:test-echo',
    runtime: 'claude-code',
    ephemeral: false,
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
  await page.route(`**/api/agents/${AGENT}/capability-grants`, (route) => route.fulfill(json({
    agent_name: AGENT,
    grants: CAPS.map((c) => ({
      capability: c,
      granted: c in GRANTERS,
      granted_by: GRANTERS[c] || null,
      granted_at: c in GRANTERS ? grantedAt : null,
    })),
  })))
  await page.route(`**/api/agents/${AGENT}/autonomy`, (route) => route.fulfill(json({ autonomy_enabled: false })))
  await page.route(/\/api\/operator-queue(\?|$)/, (route) => route.fulfill(json({ items: [] })))

  await page.goto(`/agents/${AGENT}?tab=settings&section=guardrails`)
  await expect(page.getByTestId('self-change-ready')).toBeVisible({ timeout: 20000 })
  await expect(page.getByTestId('self-change-granted-skills.manage')).toContainText(LONG_EMAIL)
}

/** Every row's boxes, from one frame. */
function measure(page) {
  return page.evaluate((caps) => {
    const box = (el) => {
      const r = el.getBoundingClientRect()
      return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height }
    }
    // The painted extent of an element's text — it can pass its own box, and
    // padding moves it without moving the box.
    const ink = (el) => {
      const range = document.createRange()
      range.selectNodeContents(el)
      const rects = [...range.getClientRects()].filter((r) => r.width > 0)
      return { left: Math.min(...rects.map((r) => r.left)), right: Math.max(...rects.map((r) => r.right)) }
    }
    // Line boxes the text occupies: one client rect per line fragment.
    const lines = (el) => {
      const range = document.createRange()
      range.selectNodeContents(el)
      return new Set([...range.getClientRects()].filter((r) => r.width > 0).map((r) => Math.round(r.top))).size
    }
    // Where each line of the text starts. A <button> centres its text, so a
    // label that wraps inside BaseToggle started each line at a different x.
    const lineLefts = (el) => {
      const range = document.createRange()
      range.selectNodeContents(el)
      const byLine = new Map()
      for (const r of [...range.getClientRects()].filter((x) => x.width > 0)) {
        const top = Math.round(r.top)
        byLine.set(top, Math.min(byLine.get(top) ?? Infinity, r.left))
      }
      return [...byLine.values()]
    }
    const rows = caps.map((id) => {
      const row = document.querySelector(`[data-testid="self-change-${id}"]`)
      const toggle = row.querySelector(`[data-testid="self-change-toggle-${id}"]`)
      const track = toggle.querySelector(':scope > span:first-child')
      const label = toggle.querySelector(':scope > span:last-child')
      const status = row.querySelector(`[data-testid="self-change-granted-${id}"]`)
      const desc = row.querySelector(':scope > p')
      return {
        id,
        text: status.textContent.trim(),
        row: box(row),
        track: box(track),
        toggle: box(toggle),
        label: { ...box(label), lines: lines(label), lineLefts: lineLefts(label) },
        status: { ...box(status), lines: lines(status), ink: ink(status), overflowX: status.scrollWidth - status.clientWidth },
        desc: { ...box(desc), ink: ink(desc) },
      }
    })
    return {
      pageOverflow: document.documentElement.scrollWidth - document.documentElement.clientWidth,
      rows,
    }
  }, CAPS)
}

const PX = 0.5

// Per-row checks are soft, so one run names every row and every way it fails.
// 320px is where the longest label ("Change other agents' schedules") wraps.
for (const theme of ['light', 'dark']) {
  for (const width of [320, 390]) {
    test(`@smoke at ${width}px each permission stacks and nothing passes the card — ${theme}`, async ({ page }) => {
      await openPanel(page, { width, theme })
      const m = await measure(page)

      expect.soft(m.pageOverflow, 'the page scrolls sideways').toBeLessThanOrEqual(0)
      for (const r of m.rows) {
        // Every line of the label starts at the label's left edge (#4093: a
        // wrapped label sat centred beside its switch).
        for (const x of r.label.lineLefts) expect.soft(x - r.label.left, `${r.id}: a label line starts ${Math.round(x - r.label.left)}px in from the label's left edge`).toBeLessThanOrEqual(PX)
        // Nothing past the row's right edge — the email granter included.
        expect.soft(r.status.ink.right, `${r.id}: "${r.text}" runs ${Math.round(r.status.ink.right - r.row.right)}px past the row`).toBeLessThanOrEqual(r.row.right + PX)
        expect.soft(r.status.overflowX, `${r.id}: the status overflows its own box`).toBeLessThanOrEqual(0)
        // The label starts on the toggle's line and stays inside the row.
        expect.soft(r.label.top < r.track.bottom && r.label.bottom > r.track.top, `${r.id}: the label is off the toggle's line`).toBe(true)
        expect.soft(r.label.right, `${r.id}: the label runs past the row`).toBeLessThanOrEqual(r.row.right + PX)
        // The status has its own line under the whole toggle — label included —
        // so nothing squeezes the label into a column, and the description runs under both.
        expect.soft(r.status.top, `${r.id}: the status sits beside the toggle, not under it`).toBeGreaterThanOrEqual(r.toggle.bottom - PX)
        expect.soft(r.desc.top, `${r.id}: the description starts above the status's end`).toBeGreaterThanOrEqual(r.status.bottom - PX)
      }
      // One left edge for the status and the description text, on every row.
      const lefts = m.rows.flatMap((r) => [r.status.ink.left, r.desc.ink.left])
      expect.soft(Math.max(...lefts) - Math.min(...lefts), `status/description left edges differ: ${lefts.map(Math.round)}`).toBeLessThanOrEqual(PX)
      // "Off" is one word on one line (a min-w-0 attempt once split it "O / ff").
      for (const r of m.rows.filter((x) => x.text === 'Off')) expect.soft(r.status.lines, `${r.id}: "Off" wraps`).toBe(1)
      // The 320px case exists to put a wrapped label under the check above.
      if (width === 320) expect(m.rows.some((r) => r.label.lines > 1), 'no label wraps at 320px, so the alignment check measured nothing').toBe(true)
    })
  }

  test(`@smoke at 1280px the desktop row is kept, the status on the toggle's line — ${theme}`, async ({ page }) => {
    await openPanel(page, { width: 1280, theme })
    const m = await measure(page)

    expect.soft(m.pageOverflow, 'the page scrolls sideways').toBeLessThanOrEqual(0)
    for (const r of m.rows) {
      expect.soft(r.status.ink.right, `${r.id}: "${r.text}" runs past the row`).toBeLessThanOrEqual(r.row.right + PX)
      expect.soft(r.status.top, `${r.id}: the status dropped below the toggle`).toBeLessThan(r.track.bottom)
      expect.soft(Math.abs(r.status.ink.right - r.row.right), `${r.id}: the status is not at the row's right edge`).toBeLessThanOrEqual(PX)
      expect.soft(r.label.lines, `${r.id}: the label wraps`).toBe(1)
    }
    for (const r of m.rows.filter((x) => x.text === 'Off')) expect.soft(r.status.lines, `${r.id}: "Off" wraps`).toBe(1)
  })
}
