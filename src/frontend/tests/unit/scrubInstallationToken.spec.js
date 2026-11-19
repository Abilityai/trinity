import { describe, it, expect } from 'vitest'
import { scrub } from '../../src/utils/scrub.js'

// trinity-enterprise#1277: installation tokens are `ghs_<appid>_<JWT>`.
describe('scrub: GitHub App installation token', () => {
  it('redacts the whole token, including the id and every JWT segment', () => {
    const token = 'ghs_1234567_abcDEF-123_x.ghiJKL-456_y.mnoPQR-789_z'
    const out = scrub(`fatal: bad credentials ${token} for repo`)
    for (const piece of ['ghs_', '1234567', 'abcDEF', 'ghiJKL', 'mnoPQR']) {
      expect(out).not.toContain(piece)
    }
    expect(out).toContain('for repo')
  })

  it('leaves a word ending in "ghs" before a long file name alone', () => {
    const text = 'see boroughs_population_by_year_2020-2024_final.csv for totals'
    expect(scrub(text)).toBe(text)
  })
})
