import { chromium } from '@playwright/test'
const BASE = 'http://localhost:8002'
const PW = process.env.ADMIN_PASSWORD
const OUT = process.env.OUT

const browser = await chromium.launch()
const page = await (await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 })).newPage()
const errs = []
page.on('console', m => { if (m.type() === 'error') errs.push(m.text().slice(0, 160)) })
await page.goto(BASE, { waitUntil: 'networkidle' })
const pwd = page.locator('#password')
if (!(await pwd.isVisible({ timeout: 4000 }).catch(() => false))) await page.locator('button:has-text("Admin Login")').click()
await pwd.waitFor({ state: 'visible' }); await pwd.fill(PW)
await page.locator('form button[type="submit"]').click()
await page.waitForFunction(() => localStorage.getItem('token') !== null, { timeout: 15000 })
await page.evaluate(() => localStorage.setItem('trinity_onboarding_dismissed_v1', '1'))
await page.goto(`${BASE}/workspace`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)
const row = page.locator('text=Analyst').first()
if (await row.isVisible({ timeout: 5000 }).catch(() => false)) { await row.click(); await page.waitForTimeout(2500) }

const varOf = (n) => page.evaluate((name) => {
  const el = document.querySelector('[style*="--ws-sidebar"]')
  return el ? getComputedStyle(el).getPropertyValue(name).trim() : '(no grid element)'
}, n)

console.log('sidebar var:', await varOf('--ws-sidebar'), '| rail var:', await varOf('--ws-rail'), '| msg cap:', await varOf('--ws-message-max'))

// --- drag the sidebar handle right by 80px --------------------------------
const h = page.locator('[data-testid="ws-handle-sidebar"]')
console.log('sidebar handle present:', await h.count())
if (await h.count()) {
  const box = await h.boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.down()
  await page.mouse.move(box.x + box.width / 2 + 80, box.y + box.height / 2, { steps: 10 })
  await page.mouse.up()
  await page.waitForTimeout(400)
  console.log('after +80 drag  sidebar var:', await varOf('--ws-sidebar'))
}

// --- clamp: drag far past the maximum -------------------------------------
if (await h.count()) {
  const box = await h.boundingBox()
  await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2)
  await page.mouse.down()
  await page.mouse.move(box.x + 900, box.y + box.height / 2, { steps: 12 })
  await page.mouse.up()
  await page.waitForTimeout(400)
  console.log('after huge drag sidebar var (clamped to 480px?):', await varOf('--ws-sidebar'))
}

// --- keyboard ---------------------------------------------------------------
if (await h.count()) {
  await h.focus()
  await page.keyboard.press('Home')
  await page.waitForTimeout(300)
  console.log('after Home     sidebar var (min 200px?):', await varOf('--ws-sidebar'))
  const aria = await h.evaluate((el) => ({
    role: el.getAttribute('role'), now: el.getAttribute('aria-valuenow'),
    min: el.getAttribute('aria-valuemin'), max: el.getAttribute('aria-valuemax'),
    label: el.getAttribute('aria-label'),
  }))
  console.log('aria:', JSON.stringify(aria))
}

// --- persistence across reload ---------------------------------------------
await page.reload({ waitUntil: 'networkidle' })
await page.waitForTimeout(2500)
console.log('after reload   sidebar var (persisted?):', await varOf('--ws-sidebar'))
console.log('stored:', await page.evaluate(() => {
  const k = Object.keys(localStorage).find((x) => x.startsWith('trinity-workspace-columns'))
  return k ? `${k} = ${localStorage.getItem(k)}` : '(nothing stored)'
}))

// --- the rail handle appears only with the rail open ------------------------
console.log('rail handle while collapsed:', await page.locator('[data-testid="ws-handle-rail"]').count())
await page.screenshot({ path: `${OUT}/01-resized.png` })
console.log('console errors:', errs.length ? errs.slice(0, 3) : 'none')
await browser.close()
