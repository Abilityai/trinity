import { chromium } from '@playwright/test'
const PW = process.env.ADMIN_PASSWORD
const browser = await chromium.launch()
const page = await (await browser.newContext({ viewport: { width: 1600, height: 1000 } })).newPage()
await page.goto('http://localhost:8002', { waitUntil: 'networkidle' })
const pwd = page.locator('#password')
if (!(await pwd.isVisible({ timeout: 4000 }).catch(() => false))) await page.locator('button:has-text("Admin Login")').click()
await pwd.waitFor({ state: 'visible' }); await pwd.fill(PW)
await page.locator('form button[type="submit"]').click()
await page.waitForFunction(() => localStorage.getItem('token') !== null, { timeout: 15000 })
await page.evaluate(() => localStorage.setItem('trinity_onboarding_dismissed_v1', '1'))
await page.goto('http://localhost:8002/workspace', { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)
await page.locator('text=Analyst').first().click()
await page.waitForTimeout(2500)
const varOf = (n) => page.evaluate((name) => {
  const el = document.querySelector('[style*="--ws-sidebar"]')
  return el ? getComputedStyle(el).getPropertyValue(name).trim() : '(none)'
}, n)
const msgW = () => page.evaluate(() => {
  const el = document.querySelector('[class*="--ws-message-max"]')
  return el ? Math.round(el.getBoundingClientRect().width) : -1
})
// Two elements carry this label: the desktop rail's expand button and the
// mobile strip (sm:hidden). Scope to the desktop rail.
const btn = page.locator('[aria-label="Conversation rail"] [aria-label="Open the conversation rail"]').first()
console.log('btn count:', await btn.count(), 'visible:', await btn.isVisible().catch(() => 'err'))
console.log('bbox:', JSON.stringify(await btn.boundingBox().catch(() => null)))
console.log('railVisible? rail aside:', await page.locator('[aria-label="Conversation rail"]').count(),
            'bbox:', JSON.stringify(await page.locator('[aria-label="Conversation rail"]').first().boundingBox().catch(() => null)))
await btn.click({ force: true })
await page.waitForTimeout(1500)
console.log('rail var after open:', await varOf('--ws-rail'), '| handle:', await page.locator('[data-testid="ws-handle-rail"]').count())
const before = await msgW()
const rh = page.locator('[data-testid="ws-handle-rail"]').first()
if (await rh.count()) {
  const b = await rh.boundingBox()
  await page.mouse.move(b.x + b.width/2, b.y + b.height/2)
  await page.mouse.down()
  await page.mouse.move(b.x + b.width/2 + 90, b.y + b.height/2, { steps: 10 })
  await page.mouse.up()
  await page.waitForTimeout(500)
  console.log('drag RIGHT -> rail var:', await varOf('--ws-rail'), '(narrower?)')
  console.log('messages', before, '->', await msgW())
  await rh.dblclick(); await page.waitForTimeout(400)
  console.log('dblclick reset -> rail var:', await varOf('--ws-rail'))
}
await page.screenshot({ path: process.env.OUT + '/02-rail-resized.png' })
await browser.close()
