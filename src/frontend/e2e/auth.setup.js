import { test as setup, expect } from '@playwright/test'

const ADMIN_PASSWORD = process.env.ADMIN_PASSWORD
if (!ADMIN_PASSWORD) {
  throw new Error('ADMIN_PASSWORD env var must be set for e2e tests')
}

setup('authenticate as admin', async ({ page }) => {
  await page.goto('/')

  // Default landing form is email-auth (when EMAIL_AUTH_ENABLED is true). The
  // admin form is reached via a toggle button labelled "🔐 Admin Login" — the
  // emoji prefix breaks Playwright's role-name normalization, so match by text.
  // When EMAIL_AUTH_ENABLED is false the admin form shows immediately and the
  // toggle isn't rendered.
  const passwordInput = page.locator('#password')
  if (!(await passwordInput.isVisible({ timeout: 3000 }).catch(() => false))) {
    await page.locator('button:has-text("Admin Login")').click()
  }

  // Wait for the password input to be ready, then fill + submit. Using the
  // form's submit button (rather than a name regex) keeps this resilient to
  // copy changes.
  await passwordInput.waitFor({ state: 'visible', timeout: 10000 })
  await passwordInput.fill(ADMIN_PASSWORD)
  await page.locator('form button[type="submit"]').click()

  // Wait for the JWT to land in localStorage. Trinity's auth store persists
  // the token here after a successful login (see stores/auth.js). Saving
  // storageState before this completes produces an empty state and every
  // downstream test lands on /login.
  await expect
    .poll(() => page.evaluate(() => localStorage.getItem('token')), { timeout: 10000 })
    .not.toBeNull()
  await expect(passwordInput).toBeHidden({ timeout: 5000 })

  // Pre-close the first-run overlay (ent#581, which replaced the ent#52
  // wizard). On a fresh CI stack — no Claude credential, no agent of the
  // admin's own — it opens on the Dashboard, and its `fixed inset-0` backdrop
  // intercepts pointer events: any spec that navigates to `/` and clicks
  // (e.g. dashboard-grid-view) would race it and flake. Seeding the same key
  // "Finish later" writes keeps every spec's inherited storageState free of it
  // (specs that WANT it navigate with `?onboarding=1`, which bypasses this
  // key). Same origin as `token`, so it is captured by the storageState below.
  await page.evaluate(() =>
    localStorage.setItem('trinity_first_run_closed', '1')
  )

  await page.context().storageState({ path: 'e2e/.auth/admin.json' })
})
