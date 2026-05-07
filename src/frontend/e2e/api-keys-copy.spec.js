// Regression test for #677 — API Keys page Copy buttons must reach the
// clipboard. Prior to the fix, the buttons called `navigator.clipboard.writeText`
// with no fallback and swallowed every rejection silently.
//
// This test creates a fresh API key, clicks both copy actions in the
// "Your MCP API Key is Ready!" modal, and reads the clipboard back to
// verify the content actually landed.

import { test, expect } from '@playwright/test'

test.describe('@interactive api-keys copy buttons (#677)', () => {
  test.beforeEach(async ({ context }) => {
    // Headless browsers gate clipboard read by default — opt in for the test.
    await context.grantPermissions(['clipboard-read', 'clipboard-write'])
  })

  test('Copy Config button writes MCP JSON to clipboard', async ({ page }) => {
    await page.goto('/api-keys')

    await page.getByRole('button', { name: /create api key/i }).first().click()

    const keyName = `e2e-copy-${Date.now()}`
    await page.getByPlaceholder('My Claude Code Key').fill(keyName)
    await page.getByRole('button', { name: 'Create', exact: true }).click()

    // The "Your MCP API Key is Ready!" modal renders the Copy Config button.
    const copyConfigBtn = page.getByRole('button', { name: /copy config/i })
    await expect(copyConfigBtn).toBeVisible({ timeout: 10000 })

    await copyConfigBtn.click()

    // Visual confirmation flips to "Copied!" only on success.
    await expect(page.getByRole('button', { name: /copied!/i })).toBeVisible({
      timeout: 3000,
    })

    const clipboardText = await page.evaluate(() => navigator.clipboard.readText())
    expect(clipboardText).toContain('"mcpServers"')
    expect(clipboardText).toContain('"trinity"')
    expect(clipboardText).toContain('Bearer trinity_mcp_')

    // Cleanup — close modal, then revoke + delete the test key.
    await page.getByRole('button', { name: /i've copied the configuration/i }).click()
    await cleanupKey(page, keyName)
  })

  test('Copy key icon button writes raw key to clipboard', async ({ page }) => {
    await page.goto('/api-keys')

    await page.getByRole('button', { name: /create api key/i }).first().click()

    const keyName = `e2e-copy-key-${Date.now()}`
    await page.getByPlaceholder('My Claude Code Key').fill(keyName)
    await page.getByRole('button', { name: 'Create', exact: true }).click()

    const copyKeyBtn = page.getByTitle('Copy key')
    await expect(copyKeyBtn).toBeVisible({ timeout: 10000 })

    await copyKeyBtn.click()

    const clipboardText = await page.evaluate(() => navigator.clipboard.readText())
    expect(clipboardText).toMatch(/^trinity_mcp_/)
    // Sanity: API keys are 44 chars (trinity_mcp_ + 32-char random).
    expect(clipboardText.length).toBeGreaterThan(20)

    await page.getByRole('button', { name: /i've copied the configuration/i }).click()
    await cleanupKey(page, keyName)
  })
})

async function cleanupKey(page, keyName) {
  // The list refreshes after the modal closes — find the row by name and delete.
  const row = page.locator('li', { has: page.getByText(keyName, { exact: true }) }).first()
  if (await row.count() === 0) return

  // Revoke first if still active, then delete. Both buttons trigger a confirm dialog.
  const revokeBtn = row.getByRole('button', { name: /revoke/i })
  if (await revokeBtn.isVisible().catch(() => false)) {
    await revokeBtn.click()
    await page.getByRole('button', { name: /^confirm$/i }).click().catch(() => {})
    await page.waitForTimeout(200)
  }
  await row.getByRole('button', { name: /delete/i }).click()
  await page.getByRole('button', { name: /^confirm$/i }).click().catch(() => {})
}
