/**
 * #2964 — the chat card's title before the feed has read the turn.
 *
 * The chat's live card starts from a synthetic item. If its title is the raw
 * message and the feed's row carries `clean_title(message)`, a long message
 * re-wraps (2 lines → 1) when the row lands ~2 s in: the card jumps.
 * `previewTitle` mirrors `client_portal/work/service.py::clean_title`, and
 * the contract is the shared fixture `tests/fixtures/portal-work-titles.json`
 * (repo root): titles PROBED from the real `clean_title`, never hand-typed.
 * Its backend twin `tests/unit/test_2964_portal_work_title_fixture.py`
 * re-proves every row against the server; THIS spec proves the mirror agrees.
 * Rows marked `masked` hold a secret only the server masks, so the mirror is
 * asserted to keep the raw text there (the documented, accepted gap).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { TITLE_MAX, previewTitle } from '@/components/portal/portalWork'

const FIXTURE_URL = new URL('../../../../tests/fixtures/portal-work-titles.json', import.meta.url)
const rows = JSON.parse(readFileSync(fileURLToPath(FIXTURE_URL), 'utf-8'))

describe('#2964 — previewTitle agrees with the server row-for-row', () => {
  it('the fixture is non-empty and TITLE_MAX matches it', () => {
    expect(rows.length).toBeGreaterThan(10)
    expect(TITLE_MAX).toBe(120)
  })

  for (const row of rows.filter((r) => !r.masked)) {
    it(`${row.note}`, () => {
      expect(previewTitle(row.message)).toBe(row.title)
    })
  }

  it('a masked row differs only by the server-side masking', () => {
    const masked = rows.filter((r) => r.masked)
    expect(masked.length).toBeGreaterThan(0)
    for (const row of masked) expect(previewTitle(row.message)).not.toBe(row.title)
  })
})

describe('#2964 — previewTitle edges the fixture does not name', () => {
  it('null and undefined read as no message', () => {
    expect(previewTitle(null)).toBe('(no message)')
    expect(previewTitle(undefined)).toBe('(no message)')
  })

  it('never exceeds TITLE_MAX code points', () => {
    for (const n of [119, 120, 121, 500]) {
      expect(Array.from(previewTitle('ab '.repeat(n))).length).toBeLessThanOrEqual(TITLE_MAX)
    }
  })
})
