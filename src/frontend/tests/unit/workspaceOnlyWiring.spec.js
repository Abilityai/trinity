/**
 * trinity-enterprise#837 review — the three page hand-offs the review round added.
 *
 * @source-text-pin: each pinned line hands an executed, unit-tested helper to a
 * page whose mount would pull in its whole store graph (Portal, Settings) or a
 * live fetch (SharedCanvas). The decisions are executed in canvasShare.spec.js
 * (`shareFetchClient`) and workspaceOnly.spec.js (`canvasReturnPath`,
 * `existingAccountNotice`); these pins make deleting a hand-off fail the suite.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

const read = (rel) => readFileSync(fileURLToPath(new URL(rel, import.meta.url)), 'utf8')

describe('workspace-only hand-offs (review round 1)', () => {
  it('a shared canvas is fetched with the Workspace session when it is the only one', () => {
    const page = read('../../src/views/SharedCanvas.vue')
    expect(page).toContain('const client = shareFetchClient({')
    expect(page).toContain("}) === 'workspace' ? portalHttp : api")
    expect(page).toContain("query: { redirect: $route.fullPath } }")
  })

  it('the Workspace code sign-in returns to the shared canvas that sent the visitor', () => {
    expect(read('../../src/views/Portal.vue')).toContain(
      'const resumeTo = store.resumePath || canvasReturnPath(route.query.redirect)')
  })

  it('the whitelist form names an existing account\'s role', () => {
    expect(read('../../src/views/Settings.vue')).toContain(
      'whitelistNotice.value = existingAccountNotice(data?.existing_account_role)')
  })
})
