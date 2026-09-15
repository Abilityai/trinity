import { chromium } from '@playwright/test'
import fs from 'node:fs'

const BASE = process.env.BASE || 'http://localhost:8001'
const PW = process.env.ADMIN_PASSWORD
const OUT = process.env.OUT
const AGENT = process.env.AGENT || 'analyst-demo'
const shots = []

async function shot(page, name, note) {
  const file = `${OUT}/${name}.png`
  await page.screenshot({ path: file, fullPage: false })
  shots.push({ name, note, file })
  console.log(`  captured ${name} — ${note}`)
}

const browser = await chromium.launch()
const ctx = await browser.newContext({ viewport: { width: 1600, height: 1000 }, deviceScaleFactor: 2 })
const page = await ctx.newPage()
page.on('console', m => { if (m.type() === 'error') console.log('  [console.error]', m.text().slice(0, 160)) })

// --- login -----------------------------------------------------------------
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

// --- 01 Workspace landing / sidebar ---------------------------------------
await page.goto(`${BASE}/workspace`, { waitUntil: 'networkidle' })
await page.waitForTimeout(2500)
await shot(page, '01-workspace-landing-sidebar', 'Workspace landing + sidebar (agents, rooms, recent chats)')

// --- 02 Agent page (Start a chat button, stats, what it can do, reports) ---
const agentRow = page.locator(`text=Analyst`).first()
if (await agentRow.isVisible({ timeout: 5000 }).catch(() => false)) {
  await agentRow.click()
  await page.waitForTimeout(2500)
  await shot(page, '02-agent-page', 'Agent page as it is today — the Start a chat button, stats, what it can do, reports')
  // scrolled view of the rest of the page
  await page.mouse.wheel(0, 700)
  await page.waitForTimeout(1200)
  await shot(page, '03-agent-page-scrolled', 'Agent page below the fold — the context that must move to Agent details')
  await page.mouse.wheel(0, -1400)
  await page.waitForTimeout(600)
} else {
  console.log('  !! agent row not found on the sidebar')
}

// --- 04 Conversation + chat tab strip -------------------------------------
const startChat = page.locator('button:has-text("Start a chat")').first()
if (await startChat.isVisible({ timeout: 4000 }).catch(() => false)) {
  await startChat.click()
  await page.waitForTimeout(2500)
  await shot(page, '04-conversation-chat-tabs', 'Conversation: the ent#451 chat tab strip (no pinned Main slot yet) + composer')
} else {
  console.log('  !! Start a chat button not found — capturing whatever is on screen')
  await shot(page, '04-conversation-chat-tabs', 'Conversation (Start a chat button not found)')
}

// --- 05 Rail open ----------------------------------------------------------
const railToggle = page.locator('[aria-label*="rail" i], button[title*="rail" i]').first()
if (await railToggle.isVisible({ timeout: 3000 }).catch(() => false)) {
  await railToggle.click()
} else {
  // fall back to the collapsed strip's first icon button
  const strip = page.locator('button[title*="Work"]').first()
  if (await strip.isVisible({ timeout: 3000 }).catch(() => false)) await strip.click()
}
await page.waitForTimeout(2000)
await shot(page, '05-rail-open', 'The ent#474 rail open on Work — the frame #523 docks the page into')

fs.writeFileSync(`${OUT}/manifest.json`, JSON.stringify(shots, null, 2))
await browser.close()
console.log(`\ndone — ${shots.length} screenshots in ${OUT}`)
