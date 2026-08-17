// #925 — the client cron validator must agree with the backend grammar row-for-row.
//
// Contract: tests/fixtures/cron-grammar-cases.json (repo root) — verdicts PROBED
// against the live `services/schedule_validation.py::validate_cron_expression`
// at generation time. The backend twin `tests/unit/test_925_cron_grammar_fixture.py`
// re-proves every row against the real validator in the backend CI env (the drift
// alarm for APScheduler upgrades); THIS spec proves the mirror agrees. Rows marked
// `divergence: "client-stricter"` are server-valid inputs the client deliberately
// rejects (Python \d/int() accept Unicode digits etc.; the client is ASCII-only).
import { describe, it, expect, vi, afterEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import {
  validateCronExpression,
  shouldShowCronError,
  computeCronValidityMap,
  CRON_PRESETS,
} from '@/utils/cronValidation'

const FIXTURE_URL = new URL('../../../../tests/fixtures/cron-grammar-cases.json', import.meta.url)
const rows = JSON.parse(readFileSync(fileURLToPath(FIXTURE_URL), 'utf-8'))

describe('cron grammar fixture parity (client mirror)', () => {
  it('loads a non-trivial fixture', () => {
    expect(Array.isArray(rows)).toBe(true)
    expect(rows.length).toBeGreaterThanOrEqual(100)
    expect(rows.filter((r) => r.valid).length).toBeGreaterThanOrEqual(40)
    expect(rows.filter((r) => !r.valid).length).toBeGreaterThanOrEqual(40)
  })

  for (const row of rows) {
    const label = row.expr === null ? '<null>' : JSON.stringify(row.expr)
    if (row.divergence === 'client-stricter') {
      it(`${label} — documented divergence: server accepts, client rejects (${row.note})`, () => {
        // The fixture records the SERVER verdict (true); the client is
        // deliberately stricter on this absurd-input class.
        expect(row.valid).toBe(true)
        expect(validateCronExpression(row.expr).valid).toBe(false)
      })
    } else {
      it(`${label} — ${row.valid ? 'ACCEPT' : 'REJECT'} (${row.note})`, () => {
        const verdict = validateCronExpression(row.expr)
        expect(verdict.valid).toBe(row.valid)
        if (row.valid) {
          expect(verdict.error).toBeNull()
        } else {
          expect(typeof verdict.error).toBe('string')
          expect(verdict.error.length).toBeGreaterThan(0)
        }
      })
    }
  }

  it('quirk rows are present and pinned (guard against fixture pruning)', () => {
    const byExpr = Object.fromEntries(rows.map((r) => [r.expr, r.valid]))
    expect(byExpr['0-0/2 * * * *']).toBe(true) // falsy-zero last → span = max
    expect(byExpr['5-5/1 * * * *']).toBe(false) // truthy last → span 0
    expect(byExpr['0 9 * * MON/999']).toBe(true) // name prefix drops the step
    expect(byExpr['0 9 * * 0-6,1']).toBe(true) // comma branch keeps ranges raw…
    expect(byExpr['0 9 * * 0-6']).toBe(false) // …bare range → sun-sat inverted
    expect(byExpr['0 9 lastx * *']).toBe(true) // 'last' is a prefix match
  })
})

describe('CRON_PRESETS', () => {
  it('ships exactly the four historical presets, byte-identical', () => {
    expect(CRON_PRESETS).toEqual([
      { label: 'Daily 9 AM', expression: '0 9 * * *' },
      { label: 'Weekly Mon', expression: '0 9 * * 1' },
      { label: 'Every 6h', expression: '0 */6 * * *' },
      { label: 'Every 30m', expression: '*/30 * * * *' },
    ])
  })

  it('every preset is valid (presets never warn)', () => {
    for (const p of CRON_PRESETS) {
      expect(validateCronExpression(p.expression).valid).toBe(true)
    }
  })

  it('every preset is fixture-proven, not just mirror-proven', () => {
    const byExpr = Object.fromEntries(rows.map((r) => [r.expr, r.valid]))
    for (const p of CRON_PRESETS) {
      expect(byExpr[p.expression]).toBe(true)
    }
  })
})

describe('error message shape (pinned loosely — substrings, not bytes)', () => {
  it('field-count error names "5 fields" and shows an example', () => {
    const { error } = validateCronExpression('@daily')
    expect(error).toContain('5 fields')
    expect(error).toContain('0 9 * * *')
    expect(error).toContain('got 1')
  })

  it('empty input reports 0 fields', () => {
    expect(validateCronExpression('').error).toContain('got 0')
    expect(validateCronExpression('   ').error).toContain('got 0')
  })

  it('item errors name the failing field', () => {
    expect(validateCronExpression('99 0 * * *').error).toContain('minute')
    expect(validateCronExpression('0 24 * * *').error).toContain('hour')
    expect(validateCronExpression('0 9 0 * *').error).toContain('day')
    expect(validateCronExpression('0 9 * 13 *').error).toContain('month')
    expect(validateCronExpression('0 9 * * 8').error).toContain('day_of_week')
  })

  it('echoed user tokens are truncated (no ballooning error slot)', () => {
    const blob = 'x'.repeat(300)
    const { valid, error } = validateCronExpression(`${blob} * * * *`)
    expect(valid).toBe(false)
    expect(error.length).toBeLessThan(160)
  })
})

describe('shouldShowCronError (display gate truth table)', () => {
  const cases = [
    // [valid, expr, touched, editing] -> expected
    [{ valid: true, expr: '0 9 * * *', touched: true, editing: false }, false, 'valid never shows'],
    [{ valid: true, expr: '', touched: true, editing: true }, false, 'valid never shows even editing'],
    // Create flow: no flash before first blur…
    [{ valid: false, expr: 'garbage', touched: false, editing: false }, false, 'creating, untouched → no flash'],
    // …live after first blur, non-empty only.
    [{ valid: false, expr: 'garbage', touched: true, editing: false }, true, 'creating, touched, non-empty invalid → show'],
    [{ valid: false, expr: '', touched: true, editing: false }, false, 'creating, empty → native required owns it'],
    [{ valid: false, expr: '   ', touched: true, editing: false }, false, 'creating, whitespace-only → treated as empty'],
    // Edit flow: unconditional when invalid (E1 — a dead Update button must be explained).
    [{ valid: false, expr: 'garbage', touched: false, editing: true }, true, 'editing, invalid, untouched → show'],
    [{ valid: false, expr: '', touched: false, editing: true }, true, 'editing, EMPTY stored cron, untouched → show'],
    [{ valid: false, expr: null, touched: false, editing: true }, true, 'editing, null stored cron → show'],
    [{ valid: false, expr: null, touched: false, editing: false }, false, 'creating, null expr, untouched → no flash'],
  ]
  for (const [input, expected, name] of cases) {
    it(name, () => {
      expect(shouldShowCronError(input)).toBe(expected)
    })
  }
})

describe('computeCronValidityMap', () => {
  it('returns a plain object keyed by id (template bracket access)', () => {
    const map = computeCronValidityMap([
      { id: 'a', cron_expression: '0 9 * * *' },
      { id: 'b', cron_expression: 'not a cron' },
    ])
    expect(map.constructor).toBe(Object)
    expect(map['a']).toBe(true)
    expect(map['b']).toBe(false)
  })

  it('a null cron on a row is invalid, never a throw', () => {
    const map = computeCronValidityMap([{ id: 'x', cron_expression: null }])
    expect(map['x']).toBe(false)
  })

  it('tolerates empty/absent lists and rows without ids', () => {
    expect(computeCronValidityMap([])).toEqual({})
    expect(computeCronValidityMap(null)).toEqual({})
    expect(computeCronValidityMap(undefined)).toEqual({})
    expect(computeCronValidityMap([null, { cron_expression: '* * * * *' }])).toEqual({})
  })
})

describe('total / fail-open contract', () => {
  afterEach(() => {
    vi.restoreAllMocks()
  })

  it('null and undefined are REJECTED (0 fields), not a crash', () => {
    expect(validateCronExpression(null).valid).toBe(false)
    expect(validateCronExpression(undefined).valid).toBe(false)
  })

  it('non-string input is coerced, not thrown on', () => {
    expect(validateCronExpression(5).valid).toBe(false) // "5" → 1 field
    expect(validateCronExpression(['0 9 * * *']).valid).toBe(true) // String() → "0 9 * * *"
  })

  it('an internal crash fails OPEN {valid:true} with a console.error (server 400 is the backstop)', () => {
    const spy = vi.spyOn(console, 'error').mockImplementation(() => {})
    const evil = {
      toString() {
        throw new Error('boom — simulated port bug')
      },
    }
    const verdict = validateCronExpression(evil)
    expect(verdict).toEqual({ valid: true, error: null })
    expect(spy).toHaveBeenCalled()
  })
})
