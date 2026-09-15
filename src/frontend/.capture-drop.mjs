import { chromium } from '@playwright/test'
const BASE = 'http://localhost:8001'
const PW = process.env.ADMIN_PASSWORD
const OUT = process.env.OUT

const browser = await chromium.launch()
const page = await (await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 })).newPage()
await page.goto(BASE, { waitUntil: 'networkidle' })
const pwd = page.locator('#password')
if (!(await pwd.isVisible({ timeout: 3000 }).catch(() => false))) await page.locator('button:has-text("Admin Login")').click()
await pwd.waitFor({ state: 'visible' }); await pwd.fill(PW)
await page.locator('form button[type="submit"]').click()
await page.waitForFunction(() => localStorage.getItem('token') !== null, { timeout: 15000 })
await page.evaluate(() => localStorage.setItem('trinity_onboarding_dismissed_v1', '1'))
await page.goto(`${BASE}/workspace`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)
const row = page.locator('text=Analyst').first()
if (await row.isVisible({ timeout: 5000 }).catch(() => false)) { await row.click(); await page.waitForTimeout(2500) }

// Dispatch on a DEEP element so the event bubbles up to the conversation root,
// which is where the listeners are.
const fired = await page.evaluate(() => {
  const composer = document.querySelector('textarea') || document.querySelector('main')
  const dt = new DataTransfer()
  dt.items.add(new File(['a,b\n1,2\n'], 'quarterly.csv', { type: 'text/csv' }))
  for (const type of ['dragenter', 'dragover']) {
    composer.dispatchEvent(new DragEvent(type, { bubbles: true, cancelable: true, dataTransfer: dt }))
  }
  return { types: Array.from(dt.types), target: composer.tagName }
})
console.log('dispatched from', fired.target, 'types=', fired.types)
await page.waitForTimeout(800)
const visible = await page.locator('[data-testid="portal-drop-overlay"]').isVisible().catch(() => false)
console.log('drop overlay visible:', visible)
await page.screenshot({ path: `${OUT}/04-drop-affordance.png` })

// And a real drop: does the chip appear?
await page.evaluate(() => {
  const composer = document.querySelector('textarea') || document.querySelector('main')
  const dt = new DataTransfer()
  dt.items.add(new File(['a,b\n1,2\n'], 'quarterly.csv', { type: 'text/csv' }))
  dt.items.add(new File(['x'], 'notes.md', { type: 'text/markdown' }))
  composer.dispatchEvent(new DragEvent('drop', { bubbles: true, cancelable: true, dataTransfer: dt }))
})
await page.waitForTimeout(2500)
const chips = await page.locator('[data-testid="portal-attachment-chip"]').count()
console.log('attachment chips after dropping 2 files:', chips)
await page.screenshot({ path: `${OUT}/05-drop-batch.png` })
await browser.close()
