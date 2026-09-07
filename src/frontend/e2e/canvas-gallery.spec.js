import { test, expect } from '@playwright/test'
import {
  GALLERY,
  GALLERY_AGENT,
  adminToken,
  seedGallery,
  clearGallery,
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

// In-page measurement. Runs after every diagram and image has settled.
function measureKit() {
  const kit = document.querySelector('[data-testid="canvas-panel"] .canvas-kit')
  if (!kit) return { blocks: 0, issues: [{ msg: 'no canvas kit on the page' }] }
  const kr = kit.getBoundingClientRect()
  const doc = document.documentElement
  const issues = []
  const px = (n) => Math.round(n)
  if (doc.scrollWidth > doc.clientWidth + 1) {
    issues.push({ msg: `page scrolls horizontally: ${doc.scrollWidth} > ${doc.clientWidth}` })
  }
  if (kit.scrollWidth > kit.clientWidth + 1) {
    issues.push({ msg: `kit scrolls horizontally: ${kit.scrollWidth} > ${kit.clientWidth}` })
  }
  const scrolls = (el) => {
    const cs = getComputedStyle(el)
    return /(auto|scroll|hidden|clip)/.test(cs.overflowX) || /(auto|scroll|hidden|clip)/.test(cs.overflowY)
  }
  const blocks = [...kit.querySelectorAll('[data-canvas-block]')]
  for (const b of blocks) {
    const r = b.getBoundingClientRect()
    const id = b.dataset.canvasBlock
    const kind = b.dataset.canvasKind
    if (r.height < 4) issues.push({ id, kind, msg: 'block has no height' })
    // A kind that draws must have drawn SOMETHING — its figure, or its named
    // fallback. A titled block with nothing under the title has height and
    // passes every geometric check while being the worst outcome.
    const drew = {
      diagram: 'svg, pre',
      image: 'img, p',
      chart: 'canvas, svg, .flex.items-end, p, pre',
    }[kind]
    if (drew && !b.querySelector(drew)) issues.push({ id, kind, msg: `${kind} block rendered nothing` })
    if (r.right > kr.right + 1 || r.left < kr.left - 1) {
      issues.push({ id, kind, msg: `block box outside the kit: ${px(r.left)}..${px(r.right)} vs ${px(kr.left)}..${px(kr.right)}` })
    }
    for (const el of b.querySelectorAll('*')) {
      const er = el.getBoundingClientRect()
      if (!er.width || !er.height) continue
      const boxSpill = Math.max(er.right - r.right, r.left - er.left)
      // Text that runs past its own box has no rect of its own; scrollWidth
      // sees it. Only for boxes that do not scroll themselves.
      const textSpill = !scrolls(el) && el.clientWidth ? el.scrollWidth - el.clientWidth : 0
      if (boxSpill <= 1 && textSpill <= 1) continue
      let a = el.parentElement
      let contained = false
      while (a && a !== b) {
        if (scrolls(a)) { contained = true; break }
        a = a.parentElement
      }
      if (contained) continue
      const cls = typeof el.className === 'string' ? el.className.split(/\s+/).slice(0, 3).join(' ') : ''
      const what = boxSpill > 1 ? `spills ${px(boxSpill)}px past the block` : `has ${px(textSpill)}px of text past its box`
      issues.push({ id, kind, msg: `<${el.tagName.toLowerCase()} class="${cls}"> ${what}` })
      break
    }
  }
  // Stacked siblings must not overlap (grid regions lay blocks side by side).
  const byParent = new Map()
  for (const b of blocks) {
    const p = b.parentElement
    if (p.classList.contains('ck-slot-grid')) continue
    if (!byParent.has(p)) byParent.set(p, [])
    byParent.get(p).push(b)
  }
  for (const list of byParent.values()) {
    for (let i = 1; i < list.length; i++) {
      const a = list[i - 1].getBoundingClientRect()
      const c = list[i].getBoundingClientRect()
      if (c.top < a.bottom - 1) {
        issues.push({ id: list[i].dataset.canvasBlock, kind: list[i].dataset.canvasKind, msg: `overlaps the block above by ${px(a.bottom - c.top)}px` })
      }
    }
  }
  return { blocks: blocks.length, issues }
}

// Everything the panel shows asynchronously has arrived: no diagram skeleton,
// no workspace-image skeleton, every <img> decoded (or failed).
async function settled(page) {
  await page.waitForFunction(() => {
    const kit = document.querySelector('[data-testid="canvas-panel"] .canvas-kit')
    if (!kit) return false
    if (kit.querySelector('[aria-busy="true"]')) return false
    // A lazy image far down a scroll container never loads on its own, so
    // the load-or-fail outcome could not be measured; ask for it eagerly.
    for (const i of kit.querySelectorAll('img[loading="lazy"]')) if (!i.complete) i.loading = 'eager'
    return [...kit.querySelectorAll('img')].every((i) => i.complete)
  }, null, { timeout: 45000 })
}

// For the screenshot only: let the panel's scroll ancestors grow so a full
// page capture shows the whole canvas rather than one viewport of it.
async function unclip(page) {
  await page.evaluate(() => {
    let el = document.querySelector('[data-testid="canvas-panel"]')
    while (el && el !== document.body) {
      const cs = getComputedStyle(el)
      if (/(auto|scroll|hidden)/.test(cs.overflowY) || cs.height.endsWith('px') && el.scrollHeight > el.clientHeight + 1) {
        el.style.overflow = 'visible'
        el.style.height = 'auto'
        el.style.maxHeight = 'none'
        el.style.minHeight = '0'
      }
      el = el.parentElement
    }
    document.documentElement.style.height = 'auto'
    document.body.style.height = 'auto'
  })
}

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
