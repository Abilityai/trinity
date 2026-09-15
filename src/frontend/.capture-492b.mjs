import { chromium } from '@playwright/test'
const BASE = 'http://localhost:8002'
const PW = process.env.ADMIN_PASSWORD
const OUT = process.env.OUT
const browser = await chromium.launch()
const page = await (await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 })).newPage()
await page.goto(BASE, { waitUntil: 'networkidle' })
const pwd = page.locator('#password')
if (!(await pwd.isVisible({ timeout: 4000 }).catch(() => false))) await page.locator('button:has-text("Admin Login")').click()
await pwd.waitFor({ state: 'visible' }); await pwd.fill(PW)
await page.locator('form button[type="submit"]').click()
await page.waitForFunction(() => localStorage.getItem('token') !== null, { timeout: 15000 })
await page.evaluate(() => {
  localStorage.setItem('trinity_onboarding_dismissed_v1', '1')
  // start from the default layout
  Object.keys(localStorage).filter(k => k.startsWith('trinity-workspace-columns')).forEach(k => localStorage.removeItem(k))
})
await page.goto(`${BASE}/workspace`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)
const row = page.locator('text=Analyst').first()
if (await row.isVisible({ timeout: 5000 }).catch(() => false)) { await row.click(); await page.waitForTimeout(2500) }

const varOf = (n) => page.evaluate((name) => {
  const el = document.querySelector('[style*="--ws-sidebar"]')
  return el ? getComputedStyle(el).getPropertyValue(name).trim() : '(none)'
}, n)
const msgWidth = () => page.evaluate(() => {
  const el = document.querySelector('[class*="--ws-message-max"]')
  return el ? Math.round(el.getBoundingClientRect().width) : -1
})

// open the rail (its collapsed strip's first icon button)
const expand = page.getByLabel("Open the conversation rail").first()
if (await expand.isVisible({ timeout: 3000 }).catch(() => false)) { await expand.click(); await page.waitForTimeout(1200) }
console.log('rail open? rail var:', await varOf('--ws-rail'))
console.log('rail handle now present:', await page.locator('[data-testid="ws-handle-rail"]').count())

const before = await msgWidth()
const rh = page.locator('[data-testid="ws-handle-rail"]')
if (await rh.count()) {
  const b = await rh.boundingBox()
  // drag RIGHT — the rail is to its right, so this must NARROW the rail
  await page.mouse.move(b.x + b.width / 2, b.y + b.height / 2)
  await page.mouse.down()
  await page.mouse.move(b.x + b.width / 2 + 90, b.y + b.height / 2, { steps: 10 })
  await page.mouse.up()
  await page.waitForTimeout(500)
  console.log('after dragging the rail handle RIGHT, rail var:', await varOf('--ws-rail'), '(should have shrunk)')
  const after = await msgWidth()
  console.log(`message column: ${before}px -> ${after}px (AC2: narrowing the rail widens the messages)`)
}
await page.screenshot({ path: `${OUT}/02-rail-resized.png` })

// double-click resets
if (await rh.count()) {
  await rh.dblclick()
  await page.waitForTimeout(400)
  console.log('after dblclick, rail var (default 384px?):', await varOf('--ws-rail'))
}
await browser.close()
