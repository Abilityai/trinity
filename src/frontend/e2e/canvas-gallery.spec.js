import { test, expect } from '@playwright/test'
import {
  GALLERY,
  GALLERY_AGENT,
  adminToken,
  seedGallery,
  clearGallery,
  measureKit,
  settled,
  unclip,
} from './helpers/canvas-gallery.js'

// The canvas gallery (#2583): every block layout an agent can produce,
// rendered on both canvas surfaces — Agent Detail's Canvas tab (full width)
// and the Workspace rail (a 24rem column) — in both themes, and MEASURED
// rather than eyeballed:
//
//   - the page never scrolls horizontally, and neither does the kit itself;
//   - every block's box sits inside the kit's box and has height;
//   - a kind that draws (diagram, image, chart) drew its figure or its named
//     fallback — a titled block with nothing under the title is the worst
//     outcome and passes every geometric check;
//   - nothing inside a block spills past the block, as a box OR as text,
//     unless a scroll container between them owns the overflow (Principle 7:
//     wide content scrolls in its own container);
//   - stacked siblings do not overlap.
//
// The gallery is seeded through the real PUT route (helpers/canvas-gallery.js),
// so what is asserted is what an agent can actually write. Screenshots land in
// e2e/canvas-gallery-shots/ (gitignored) as the visual record for the PR.
//
// Not in the @smoke tier: it needs an agent to write to (`CANVAS_AGENT`,
// default `test-harness-agent`) and takes a couple of minutes.

const WIDTHS = [1280, 1920]
const THEMES = ['light', 'dark']
// Outside `outputDir`, which Playwright empties at the start of EVERY run —
// the screenshots are the visual record and must survive a second run.
const SHOTS = 'e2e/canvas-gallery-shots'
const KEEP = !!process.env.KEEP_GALLERY

// Serial: the write route is rate-limited per agent (60/min), and two workers
// seeding seventeen canvases each trip it.
test.describe.configure({ mode: 'serial' })

test.beforeAll(async ({ request, baseURL }) => {
  const token = await adminToken(request, baseURL)
  const failures = await seedGallery(request, baseURL, token)
  expect(failures, 'every gallery canvas is accepted by the write route').toEqual([])
})

test.afterAll(async ({ request, baseURL }) => {
  if (KEEP) return
  const token = await adminToken(request, baseURL)
  await clearGallery(request, baseURL, token)
})

async function openDetail(page) {
  await page.goto(`/agents/${GALLERY_AGENT}?tab=canvas`)
  await page.getByTestId('canvas-select').waitFor({ timeout: 20000 })
}

async function openRail(page) {
  // Start from a collapsed rail every time (the open state and tab are
  // remembered per browser), so the strip's icon button is the one opener.
  await page.addInitScript(() => localStorage.removeItem('trinity-workspace-rail'))
  await page.goto(`/workspace?agent=${GALLERY_AGENT}`)
  const strip = page.getByTestId('portal-rail-tab-canvas')
  await strip.waitFor({ timeout: 20000 })
  await strip.click({ timeout: 10000 })
  await page.getByTestId('portal-rail-canvas').waitFor({ timeout: 20000 })
}

async function selectCanvas(page, canvasId) {
  await page.locator(`[data-testid="canvas-select"] [data-canvas-id="${canvasId}"]`).click({ timeout: 10000 })
  await expect(page.locator(`[data-testid="canvas-panel"][data-canvas-id="${canvasId}"]`)).toBeVisible()
}

// The rail is a fixed 24rem column whatever the viewport, so one width covers
// it; Agent Detail's panel is fluid and is measured at two. One navigation
// per test, then the selector — a reader's own path, and the portal detail
// route is rate-limited (60 reads a minute), which reloading per canvas trips.
const SURFACES = {
  detail: { open: openDetail, widths: WIDTHS },
  rail: { open: openRail, widths: [1280] },
}

for (const [surface, { open, widths }] of Object.entries(SURFACES)) {
  for (const theme of THEMES) {
    for (const width of widths) {
      test(`${surface} · ${theme} · ${width}px — every gallery canvas renders bounded`, async ({ page }) => {
        test.setTimeout(10 * 60 * 1000)
        await page.setViewportSize({ width, height: 900 })
        await page.addInitScript((t) => localStorage.setItem('trinity-theme', t), theme)
        await open(page)

        const report = []
        for (const canvas of GALLERY) {
          await selectCanvas(page, canvas.canvas_id)
          // The selector switches synchronously; the blocks arrive with the
          // detail fetch. Wait for this canvas's own block count, and surface
          // the panel's own error line if the fetch failed.
          await expect
            .poll(
              () => page.evaluate(() => {
                const err = document.querySelector('[data-testid="canvas-detail-error"]')
                if (err) return `ERROR: ${err.textContent.trim()}`
                return [...document.querySelectorAll('[data-testid="canvas-panel"] [data-canvas-block]')].map((b) => b.dataset.canvasBlock)
              }),
              { timeout: 30000, message: `${canvas.canvas_id} renders every block it holds` },
            )
            .toHaveLength(canvas.blocks.length)
          await settled(page)
          const result = await page.evaluate(measureKit)
          report.push({ canvas: canvas.canvas_id, ...result })
          await unclip(page)
          await page.screenshot({
            path: `${SHOTS}/${surface}-${theme}-${width}-${canvas.canvas_id}.png`,
            fullPage: true,
          })
        }

        const problems = report.filter((r) => r.issues.length)
        const summary = problems
          .map((r) => `${r.canvas}:\n` + r.issues.map((i) => `    ${i.id ? `[${i.kind}#${i.id}] ` : ''}${i.msg}`).join('\n'))
          .join('\n')
        expect(problems, `layout problems on ${surface}/${theme}/${width}:\n${summary}`).toEqual([])
      })
    }
  }
}
