// Regression test for #677 / #859 — MCP Keys Copy buttons must reach
// the clipboard. #677: the buttons called `navigator.clipboard.writeText`
// with no fallback and swallowed every rejection silently. #859: after
// PR #700 moved the component views/ApiKeys.vue →
// components/settings/McpKeysTab.vue, the `copyToClipboard` import was
// dropped, so both buttons threw `ReferenceError` and failed silently.
//
// This test creates a fresh API key, clicks both copy actions in the
// "Your MCP API Key is Ready!" modal, and reads the clipboard back to
// verify the content actually landed. Tagged @smoke so CI runs it on
// the canonical route (#859 acceptance criterion).

import { test, expect } from '@playwright/test'

// Canonical MCP Keys route since #302/#700 (the legacy /api-keys path
// 301-redirects here). Hitting it directly keeps the smoke gate from
// depending on the redirect.
const MCP_KEYS_ROUTE = '/settings?tab=mcp-keys'

test.describe('@smoke api-keys copy buttons (#677, #859)', () => {
  test.beforeEach(async ({ context, page }) => {
    // Headless browsers gate clipboard read by default — opt in for the test.
    await context.grantPermissions(['clipboard-read', 'clipboard-write'])

    // #1134: McpKeysTab's onMounted fires POST /api/mcp/keys/ensure-default;
    // when no user-scoped key exists it auto-creates "Default MCP Key" and
    // opens the "Your MCP API Key is Ready!" modal at an arbitrary point
    // mid-test — both modals are `fixed z-10 inset-0`, so the auto-modal
    // lands on top and intercepts the Create click. Neutralize the race:
    // call ensure-default ourselves, then reload — the remounted page's own
    // ensure-default is a guaranteed no-op, so the auto-modal can never
    // appear during the test body.
    await page.goto(MCP_KEYS_ROUTE)
    const token = await page.evaluate(() => localStorage.getItem('token'))
    if (token) {
      await page.request
        .post('/api/mcp/keys/ensure-default', {
          headers: { Authorization: `Bearer ${token}` },
        })
        .catch(() => {})
    }
    await page.reload()
  })

  test('Copy Config button writes MCP JSON to clipboard', async ({ page }) => {
    await page.goto(MCP_KEYS_ROUTE)

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
    await page.goto(MCP_KEYS_ROUTE)

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
  // Cleanup via the backend API rather than clicking through the UI. The
  // test under test is clipboard behavior; cleanup is housekeeping. The
  // old UI-walk (revoke modal → confirm → delete modal → confirm) ran
  // ~6 sequential clicks and routinely blew the per-test 30s budget on
  // slow CI runners, leaving the test red even though the assertions
  // had already passed.
  //
  // DELETE /api/mcp/keys/{id} hard-deletes the row regardless of
  // active/revoked state (no need for the revoke → delete sequence the
  // UI enforces). JWT lives in localStorage['token'] (`stores/auth.js`).
  const token = await page.evaluate(() => localStorage.getItem('token'))
  if (!token) return
  const headers = { Authorization: `Bearer ${token}` }
  const list = await page.request
    .get('/api/mcp/keys', { headers })
    .then((r) => (r.ok() ? r.json() : []))
    .catch(() => [])
  const match = Array.isArray(list) ? list.find((k) => k.name === keyName) : null
  if (match) {
    await page.request
      .delete(`/api/mcp/keys/${match.id}`, { headers })
      .catch(() => {})
  }
}
