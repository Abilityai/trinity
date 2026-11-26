import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve } from 'node:path'
import { unitsAgo } from '../../src/utils/timestamps'

/**
 * #4140: two date formatters built `${n} weeks ago` by hand, so 7-13 days read
 * "1 weeks ago". Both now go through the shared helper.
 *
 * @source-text-pin: a call-site guard; both formatters are private to their SFCs (one inside the whole Settings view), so a mount would prove one rendering, not that neither file hand-builds the plural again
 */
const src = (p) => readFileSync(resolve(__dirname, '../../src', p), 'utf-8')

describe('"weeks ago" uses the shared plural helper (#4140)', () => {
  it('reads "1 week ago" for one week and "2 weeks ago" for two', () => {
    expect(unitsAgo(1, 'week')).toBe('1 week ago')
    expect(unitsAgo(2, 'week')).toBe('2 weeks ago')
  })

  for (const file of ['views/Settings.vue', 'components/settings/SubscriptionsPanel.vue']) {
    it(`${file} has no hand-built "weeks ago"`, () => {
      const s = src(file)
      expect(s).not.toMatch(/weeks ago`/)
      expect(s).toMatch(/unitsAgo\(Math\.floor\(diffInDays \/ 7\), 'week'\)/)
    })
  }
})
