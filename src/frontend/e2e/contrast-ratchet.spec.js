import { test, expect } from '@playwright/test'
import { readFileSync, writeFileSync } from 'node:fs'
import { join } from 'node:path'

/**
 * Text-contrast ratchet (#2201) — the mechanical half the design system was
 * missing, in the same shape as the raw-colour ratchet (#2605).
 *
 * #2201 fixed what could be fixed at the TOKEN level: the inverted tertiary ink
 * pair, the warm families' light text tier, the Operations badge. What remains
 * is a long tail of `text-gray-400` written with no dark sibling — and fixing
 * those needs a semantic ink token, because adding the missing `dark:` half to
 * every one of ~1,500 sites would grow the raw-colour ratchet by ~1,500 raw
 * classes. Two guards pulling against each other is itself the finding, and it
 * is written up in the contract; the migration is tracked separately.
 *
 * So this freezes the tail instead of pretending it is gone. Per page and theme
 * it counts DISTINCT failing text treatments (one per colour-pair + class
 * string, not per node, so a longer list of rows is not a regression) and fails
 * when a count grows. The number may only shrink — and a file with no baseline
 * entry is held to ZERO, so a new page cannot arrive pre-broken.
 *
 * Regenerate deliberately, never incidentally:
 *   CONTRAST_BASELINE_UPDATE=1 npx playwright test e2e/contrast-ratchet.spec.js
 */
// `process.cwd()` rather than `import.meta.url`: these specs are transpiled to
// CJS (the package declares no `"type": "module"`), where `import.meta` is not
// available, and Playwright runs from the config's directory.
const BASELINE = join(process.cwd(), 'e2e', 'contrast-baseline.json')
const UPDATE = process.env.CONTRAST_BASELINE_UPDATE === '1'

const PAGES = [['/', 'dashboard'], ['/operations', 'operations'], ['/settings', 'settings'],
               ['/library', 'library'], ['/workspace', 'workspace']]
const THEMES = ['light', 'dark']

// Runs in the page. Deliberately conservative about what counts as a failure:
// anything it cannot measure honestly is SKIPPED rather than guessed, because a
// scanner that invents findings gets muted, and one that invents passes is worse.
const SCAN = () => {
  const lum = (rgb) => {
    const [r, g, b] = rgb.map((v) => { const s = v / 255; return s <= 0.04045 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4 })
    return 0.2126 * r + 0.7152 * g + 0.0722 * b
  }
  const ratio = (a, b) => { const la = lum(a), lb = lum(b); const [hi, lo] = la >= lb ? [la, lb] : [lb, la]; return (hi + 0.05) / (lo + 0.05) }
  const parse = (s) => {
    const m = String(s || '').match(/rgba?\(([^)]+)\)/)
    if (!m) return null
    const p = m[1].split(/[ ,/]+/).filter(Boolean).map(Number)
    return { rgb: p.slice(0, 3), a: p.length > 3 ? p[3] : 1 }
  }
  // null when the ground cannot be computed — a gradient or image behind the
  // text. Reporting those as white-on-white is a scanner bug, not a finding.
  const groundOf = (el) => {
    for (let e = el; e; e = e.parentElement) {
      const cs = getComputedStyle(e)
      if (cs.backgroundImage && cs.backgroundImage !== 'none') return null
      const c = parse(cs.backgroundColor)
      if (c && c.a > 0.5) return c.rgb
    }
    return parse(getComputedStyle(document.body).backgroundColor)?.rgb || null
  }

  const found = new Map()
  for (const el of document.querySelectorAll('body *')) {
    if (el.children.length) continue
    const text = (el.textContent || '').trim()
    if (!text) continue
    const r = el.getBoundingClientRect()
    if (r.width < 2 || r.height < 2) continue
    const cs = getComputedStyle(el)
    if (cs.visibility === 'hidden' || cs.display === 'none') continue
    // WCAG exempts disabled controls and purely decorative text.
    if (Number(cs.opacity) < 0.95) continue
    if (el.closest('[disabled], [aria-disabled="true"], [aria-hidden="true"]')) continue
    const fg = parse(cs.color)
    const bg = groundOf(el)
    if (!fg || !bg || fg.a < 0.95) continue
    const px = parseFloat(cs.fontSize) || 14
    const weight = Number(cs.fontWeight) || 400
    const large = px >= 24 || (px >= 18.66 && weight >= 700)
    const rr = ratio(fg.rgb, bg)
    if (rr >= (large ? 3 : 4.5)) continue
    // One entry per TREATMENT, not per node: a page listing 40 rows in the same
    // muted style is one defect, and counting nodes would make the ratchet move
    // with how much data the instance happens to hold.
    const key = `${String(el.className)}|${cs.color}|${bg.join(',')}`
    if (!found.has(key)) {
      found.set(key, { ratio: Math.round(rr * 100) / 100, need: large ? 3 : 4.5, sample: text.slice(0, 30), cls: String(el.className).slice(0, 60) })
    }
  }
  return [...found.values()].sort((a, b) => a.ratio - b.ratio)
}

test.describe.configure({ mode: 'serial' })

test('@interactive text contrast does not regress on any page, in either theme', async ({ page }) => {
  const baseline = UPDATE ? {} : JSON.parse(readFileSync(BASELINE, 'utf8'))
  const measured = {}
  const grown = []

  await page.setViewportSize({ width: 1440, height: 1000 })
  // The session comes from the `setup` project's storageState; the theme is a
  // localStorage key the store reads at boot, so it is set once per pass and the
  // next navigation picks it up.
  await page.goto('/')
  for (const theme of THEMES) {
    await page.evaluate((t) => localStorage.setItem('trinity-theme', t), theme)
    for (const [path, name] of PAGES) {
      const key = `${theme}:${name}`
      await page.goto(path)
      await page.waitForLoadState('networkidle')
      await page.waitForTimeout(1500)
      const hits = await page.evaluate(SCAN)
      measured[key] = hits.length
      if (UPDATE) continue
      // A page with no entry is held to zero — a new surface cannot arrive
      // pre-broken and quietly inherit an allowance.
      const allowed = baseline[key] ?? 0
      if (hits.length > allowed) {
        grown.push(`${key}: ${hits.length} failing treatments (baseline ${allowed})\n` +
          hits.slice(0, 5).map((h) => `      ${h.ratio}:1 (needs ${h.need}) "${h.sample}" .${h.cls}`).join('\n'))
      }
    }
  }

  if (UPDATE) {
    writeFileSync(BASELINE, JSON.stringify(measured, null, 2) + '\n')
    test.info().annotations.push({ type: 'baseline', description: JSON.stringify(measured) })
    return
  }

  expect(grown.join('\n'), 'text contrast regressed').toEqual('')

  // The other half of a ratchet: a stale ceiling is a ratchet that stopped
  // biting. Same rule as the raw-colour baseline (#2605).
  const stale = Object.entries(baseline)
    .filter(([k, v]) => measured[k] !== undefined && measured[k] < v)
    .map(([k, v]) => `${k}: baseline ${v}, now ${measured[k]} — lower it`)
  expect(stale, 'contrast improved; lower the baseline so it keeps biting').toEqual([])
})
