import { chromium } from '@playwright/test'
import fs from 'node:fs'

const BASE = process.env.BASE || 'http://localhost:8001'
const PW = process.env.ADMIN_PASSWORD
const OUT = process.env.OUT
const shots = []

async function shot(page, name, note) {
  await page.screenshot({ path: `${OUT}/${name}.png`, fullPage: false })
  shots.push({ name, note })
  console.log(`  captured ${name} — ${note}`)
}

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 })
const page = await ctx.newPage()
page.on('console', m => { if (m.type() === 'error') console.log('  [console.error]', m.text().slice(0, 200)) })

await page.goto(BASE, { waitUntil: 'networkidle' })
const pwd = page.locator('#password')
if (!(await pwd.isVisible({ timeout: 3000 }).catch(() => false))) {
  await page.locator('button:has-text("Admin Login")').click()
}
await pwd.waitFor({ state: 'visible', timeout: 10000 })
await pwd.fill(PW)
await page.locator('form button[type="submit"]').click()
await page.waitForFunction(() => localStorage.getItem('token') !== null, { timeout: 15000 })
await page.evaluate(() => localStorage.setItem('trinity_onboarding_dismissed_v1', '1'))
console.log('logged in')

// 01 — click the agent: lands in a conversation, band + Main tab
await page.goto(`${BASE}/workspace`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)
const row = page.locator('text=Analyst').first()
if (await row.isVisible({ timeout: 5000 }).catch(() => false)) { await row.click(); await page.waitForTimeout(2500) }
await shot(page, '01-lands-in-conversation', 'Clicking the agent opens the chat — band with stats + chart, Main pinned first, Reset in the header')

// 02 — Agent details in the rail's place
const details = page.locator('[data-testid="portal-open-agent-details"]').first()
if (await details.isVisible({ timeout: 4000 }).catch(() => false)) {
  await details.click(); await page.waitForTimeout(1800)
  await shot(page, '02-agent-details', 'Agent details opens INTO THE RAIL\'S PLACE — chats, what it can do, reports')
  const close = page.locator('[aria-label="Close agent details"]').first()
  if (await close.isVisible({ timeout: 2000 }).catch(() => false)) { await close.click(); await page.waitForTimeout(1200) }
} else { console.log('  !! Agent details button not found') }

// 03 — the rail is back on the tab it was showing
await shot(page, '03-rail-returns', 'Closing details returns the rail, untouched')

// 04 — the drop affordance
await page.evaluate(() => {
  const main = document.querySelector('main') || document.body
  const dt = new DataTransfer()
  const file = new File(['x'], 'quarterly.csv', { type: 'text/csv' })
  dt.items.add(file)
  main.dispatchEvent(new DragEvent('dragenter', { bubbles: true, dataTransfer: dt }))
  main.dispatchEvent(new DragEvent('dragover', { bubbles: true, dataTransfer: dt }))
})
await page.waitForTimeout(900)
await shot(page, '04-drop-affordance', 'ent#524: dragging files over the conversation names what will happen')

fs.writeFileSync(`${OUT}/manifest.json`, JSON.stringify(shots, null, 2))
await browser.close()
console.log(`\ndone — ${shots.length} screenshots`)
