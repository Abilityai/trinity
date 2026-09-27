import { test, expect } from '@playwright/test'
import { adminToken, GALLERY_AGENT } from './helpers/canvas-gallery.js'

/**
 * Inline markdown in table cells (#2771).
 *
 * The parse and the value/escaping rules are unit-tested (`inlineMarkdown.spec.js`,
 * node env). The half that CANNOT be reached there is the sanitizer: vitest runs
 * `environment: 'node'` and DOMPurify's DOM-less stub has no `addHook`, so the
 * one thing a policy object cannot prove — that a script payload in a cell is
 * actually stripped from the rendered DOM — is proven here, in a browser, on
 * content written through the real canvas write route.
 *
 * Seeded through `PUT /api/agents/{name}/canvas/{id}` exactly as an agent would,
 * so what is asserted is what an agent can really produce.
 */
const CANVAS_ID = 'g-2771-cell-markdown'
const AGENT = GALLERY_AGENT

const XSS = '<script>window.__2771 = "popped"</script><img src=x onerror="window.__2771=\'popped\'">'

const CANVAS = {
  title: 'Cell markdown',
  blocks: [
    {
      id: 'cells',
      kind: 'table',
      title: 'Inline markdown in cells',
      payload: {
        columns: ['**Item**', 'Status'],
        rows: [
          ['**Deploy**', '`done` — see [runbook](https://example.com)'],
          ['*soon*', '~~dropped~~'],
          ['payload', XSS],
          ['number', 7],
          ['object', { nested: true }],
          ['empty', null],
          ['block', '# Heading\n- one\n- two'],
        ],
      },
    },
    {
      // AC: a ```table fence inside a `markdown` block takes the same path
      // (`CanvasMarkdown` delegates the segment to the same renderer), so the
      // two ways an agent can write a table must not behave differently —
      // which is the inconsistency this issue is actually about.
      id: 'fence',
      kind: 'markdown',
      title: 'Fenced table',
      payload: {
        markdown: 'Before.\n\n```table\n{"columns": ["Item"], "rows": [["**Fenced**"]]}\n```\n\nAfter.',
      },
    },
  ],
}

test.describe.configure({ mode: 'serial' })

test.beforeAll(async ({ request, baseURL }) => {
  const token = await adminToken(request, baseURL)
  const res = await request.put(
    `${baseURL}/api/agents/${encodeURIComponent(AGENT)}/canvas/${CANVAS_ID}`,
    { headers: { Authorization: `Bearer ${token}` }, data: { ...CANVAS, audience: 'roster' } }
  )
  expect(res.ok(), `seed failed: ${res.status()} ${(await res.text()).slice(0, 300)}`).toBe(true)
})

test.afterAll(async ({ request, baseURL }) => {
  const token = await adminToken(request, baseURL)
  await request.delete(`${baseURL}/api/agents/${encodeURIComponent(AGENT)}/canvas/${CANVAS_ID}`, {
    headers: { Authorization: `Bearer ${token}` },
  })
})

async function openCanvas(page) {
  await page.goto(`/agents/${AGENT}?tab=canvas`)
  await page.getByTestId('canvas-select').waitFor({ timeout: 20000 })
  await page.locator(`[data-testid="canvas-select"] [data-canvas-id="${CANVAS_ID}"]`).click({ timeout: 10000 })
  await expect(page.locator(`[data-testid="canvas-panel"][data-canvas-id="${CANVAS_ID}"]`)).toBeVisible()
}

test('@interactive table cells render inline markdown and strip script payloads', async ({ page }) => {
  const popped = []
  page.on('dialog', (d) => { popped.push('dialog'); d.dismiss() })
  await openCanvas(page)

  const table = page.locator(`[data-canvas-id="${CANVAS_ID}"] table`).first()
  await expect(table).toBeVisible()

  // 1. The three things agents actually write.
  await expect(table.locator('td strong', { hasText: 'Deploy' })).toBeVisible()
  await expect(table.locator('td code', { hasText: 'done' })).toBeVisible()
  const link = table.locator('td a', { hasText: 'runbook' })
  await expect(link).toBeVisible()
  await expect(link).toHaveAttribute('href', 'https://example.com')
  // Same hardening as every other link surface.
  await expect(link).toHaveAttribute('target', '_blank')
  await expect(link).toHaveAttribute('rel', /noopener/)

  await expect(table.locator('td em', { hasText: 'soon' })).toBeVisible()
  await expect(table.locator('td del', { hasText: 'dropped' })).toBeVisible()

  // 2. Headers too — consistency with cells is the AC.
  await expect(table.locator('th strong', { hasText: 'Item' })).toBeVisible()

  // 3. No raw markdown characters survive anywhere in the table.
  const text = await table.innerText()
  expect(text, 'literal markdown leaked into a cell').not.toMatch(/\*\*Deploy\*\*|~~dropped~~|\[runbook\]/)

  // 4. The sanitizer — the thing a unit test cannot reach.
  expect(await page.evaluate(() => window.__2771), 'a cell executed script').toBeUndefined()
  expect(popped, 'a cell opened a dialog').toEqual([])
  await expect(table.locator('script')).toHaveCount(0)
  await expect(table.locator('img')).toHaveCount(0)

  // 4b. The fenced path renders through the same component.
  const fenced = page.locator(`[data-canvas-id="${CANVAS_ID}"] table`).nth(1)
  await expect(fenced.locator('td strong', { hasText: 'Fenced' })).toBeVisible()

  // 5. Non-strings keep their pre-#2771 rendering; a block element degrades to
  //    text rather than breaking the row.
  expect(text).toContain('7')
  expect(text).toContain('{"nested":true}')
  await expect(table.locator('td h1, td ul, td li, td pre, td table')).toHaveCount(0)
  expect(text, 'a heading in a cell should degrade to its own text').toContain('Heading')
})

test('@interactive a cell never widens the table past its scroll container', async ({ page }) => {
  // Principle 7, and the #2583 layout bar: a rendered link or code span must
  // wrap inside the bounded viewport, not push the column.
  await openCanvas(page)
  const overflow = await page.evaluate(() => {
    const de = document.documentElement
    return de.scrollWidth - de.clientWidth
  })
  expect(overflow, 'the canvas page scrolls horizontally').toBeLessThanOrEqual(0)
})
