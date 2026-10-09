/**
 * #3453 / #3454 — Settings.vue is the live consumer of the two units whose
 * behaviour is proven elsewhere (`queryTabRestore.spec.js` drives the
 * composable, `backupStatusPanel.spec.js` mounts the panel).
 *
 * @source-text-pin: Settings.vue is one ~4,000-line view that fires dozens of fetches at mount, so no spec mounts it; a unit nobody calls proves nothing, and these pin the call sites only — the behaviour is asserted by the two specs above.
 */
import { describe, expect, it } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'

const settings = stripComments(
  readFileSync(fileURLToPath(new URL('../../src/views/Settings.vue', import.meta.url)), 'utf8')
)

describe('Settings.vue wiring', () => {
  it('takes its active tab from useQueryTab and has no second writer (#3453)', () => {
    expect(settings).toMatch(/const\s*\{\s*activeTab\s*,\s*selectTab\s*\}\s*=\s*useQueryTab\(/)
    // A direct assignment would be a writer the late-write guard cannot see.
    expect(settings).not.toMatch(/activeTab\.value\s*=[^=]/)
    expect(settings).not.toMatch(/v-model="activeTab"/)
  })

  it('hands the retention response\'s backup block to the panel on the Retention tab (#3454)', () => {
    const tag = settings.match(/<BackupStatusPanel[^>]*\/>/)
    expect(tag).not.toBeNull()
    expect(tag[0]).toContain(':backup="retention?.backup"')
    // "Loaded" is "a response has arrived", not "no fetch in flight".
    expect(tag[0]).toContain(':has-loaded="retention !== null"')
    expect(tag[0]).not.toContain('retentionLoading')
  })
})
