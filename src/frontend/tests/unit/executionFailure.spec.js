import { describe, it, expect } from 'vitest'
import {
  failureCodeFromSummary,
  failuresTileState,
  stripFailureCode,
  triggerLabel,
} from '@/utils/executionFailure'

/**
 * ent#100 — the "Recent failures" tile's whole decision surface.
 *
 * The tile itself is a thin renderer over these functions because
 * `vitest.config.js` pins `environment: 'node'` (pure modules, no mounting), so
 * anything left inside the SFC is untestable here. What is asserted below is
 * therefore what the tile actually does.
 */

describe('failureCodeFromSummary — it READS a code, it does not GUESS one', () => {
  it('reads the marker the platform actually emits', () => {
    expect(failureCodeFromSummary('[auth] token rejected')).toBe('auth')
    expect(failureCodeFromSummary('[lease_expired] no callback')).toBe('lease_expired')
  })

  it('returns null for prose — a guessed label is worse than no label', () => {
    // The whole reason this is not a classifier: re-deriving a code from a
    // 200-char truncation of a message whose code the backend knew and threw
    // away would be a third, unenforced copy of failure_classifier.py.
    expect(failureCodeFromSummary('Timed out after 3600s')).toBeNull()
    expect(failureCodeFromSummary('Authentication failed: invalid token')).toBeNull()
    expect(failureCodeFromSummary('')).toBeNull()
    expect(failureCodeFromSummary(null)).toBeNull()
    expect(failureCodeFromSummary(undefined)).toBeNull()
    expect(failureCodeFromSummary(42)).toBeNull()
  })

  it('is ANCHORED — a mid-string bracket cannot mint its own chrome', () => {
    // `error_summary` is agent/LLM-authored, prompt-injectable content.
    expect(failureCodeFromSummary('build failed [critical] see log')).toBeNull()
    expect(failureCodeFromSummary(' [auth] leading space')).toBeNull()
  })

  it('matches LOWER-CASE only, which is the shape the writer emits', () => {
    // `pull_coordination_service.py` formats the plain `str` field, so the
    // marker reads `[auth]`. Had it formatted the `str, Enum` MEMBER the text
    // would be `[TaskExecutionErrorCode.AUTH]` — which must NOT match, rather
    // than rendering a Python repr as a badge on an operator's dashboard.
    expect(failureCodeFromSummary('[AUTH] token rejected')).toBeNull()
    expect(failureCodeFromSummary('[TaskExecutionErrorCode.AUTH] x')).toBeNull()
  })
})

describe('stripFailureCode', () => {
  it('removes the marker so the message line does not repeat the chip', () => {
    expect(stripFailureCode('[auth] token rejected')).toBe('token rejected')
  })

  it('leaves ordinary messages alone', () => {
    expect(stripFailureCode('Timed out after 3600s')).toBe('Timed out after 3600s')
    expect(stripFailureCode(null)).toBe('')
  })
})

describe('triggerLabel — explicit fallback, never a silent blank', () => {
  it('passes the platform vocabulary through unchanged', () => {
    // The Operations → Executions panel renders `row.triggered_by` raw; a tile
    // inventing its own words for the same values would show one execution
    // under two names.
    expect(triggerLabel('schedule')).toBe('schedule')
    expect(triggerLabel('webhook')).toBe('webhook')
    expect(triggerLabel('fan_out')).toBe('fan_out')
  })

  it("falls back to an explicit 'other', not to blank", () => {
    // The 2026-07-30 learning: the constant that degrades silently lies.
    expect(triggerLabel(null)).toBe('other')
    expect(triggerLabel(undefined)).toBe('other')
    expect(triggerLabel('')).toBe('other')
    expect(triggerLabel('   ')).toBe('other')
  })

  it('bounds an unexpected value so it cannot blow the column open', () => {
    expect(triggerLabel('a'.repeat(80))).toBe('other')
    expect(triggerLabel('Weird Trigger!')).toBe('other')
    // A plausible-looking future trigger still renders verbatim rather than
    // being flattened — it is the platform's own vocabulary.
    expect(triggerLabel('room')).toBe('room')
    expect(triggerLabel('future_thing')).toBe('future_thing')
  })
})

describe('failuresTileState — the green ✓ needs POSITIVE evidence', () => {
  const HEALTHY = {
    listLoaded: true,
    listError: false,
    statsLoaded: true,
    statsError: false,
    itemCount: 0,
    failed24h: 0,
    rosterSize: 3,
  }

  it('renders the confirmed all-clear only when everything is green', () => {
    const s = failuresTileState(HEALTHY)
    expect(s.state).toBe('empty')
    expect(s.emptyTitle).toBe('No failures in 24h ✓')
  })

  it('is loading before the first list GET lands', () => {
    expect(failuresTileState({ ...HEALTHY, listLoaded: false }).state).toBe('loading')
  })

  it('is an error — never the empty copy — when the first load FAILS (#1926)', () => {
    const s = failuresTileState({ ...HEALTHY, listLoaded: false, listError: true })
    expect(s.state).toBe('error')
    expect(s.emptyTitle).toBe('')
  })

  // --- the three independent routes to a manufactured all-clear -------------

  it('ROUTE 1: a failed list GET cannot produce the green ✓', () => {
    const s = failuresTileState({ ...HEALTHY, listError: true })
    expect(s.state).toBe('empty')
    expect(s.emptyTitle).not.toContain('✓')
    expect(s.emptyTitle).toBe('No failures listed')
  })

  it('ROUTE 2: a failed /stats GET cannot produce the green ✓', () => {
    // The 24h total is a second request. If it fails, the count is UNKNOWN,
    // which is not zero — and it is never inferred from items.length, because
    // a bounded page cannot produce a 24h total.
    const s = failuresTileState({
      ...HEALTHY,
      statsError: true,
      failed24h: null,
    })
    expect(s.state).toBe('empty')
    expect(s.emptyTitle).not.toContain('✓')
    expect(s.emptyHint).toContain('not a confirmed all-clear')
  })

  it('ROUTE 2b: stats that never loaded at all cannot produce the green ✓', () => {
    const s = failuresTileState({ ...HEALTHY, statsLoaded: false, failed24h: null })
    expect(s.emptyTitle).not.toContain('✓')
  })

  it('ROUTE 3: an unenumerable fleet cannot produce the green ✓', () => {
    // `accessible_agent_names` → `docker_service.list_all_agents_fast()`, which
    // returns [] when the Docker client is None AND on ANY exception. For a
    // non-admin every fleet accessor then early-returns zeros at HTTP 200:
    // the fetch "succeeds", `loaded` is honestly true, and the tile would
    // assert an all-clear manufactured by an infrastructure fault — on the
    // fleet-monitoring surface. The same fault empties the Grid's roster, so an
    // empty roster is the positive-enumerability signal available client-side.
    const s = failuresTileState({ ...HEALTHY, rosterSize: 0 })
    expect(s.state).toBe('empty')
    expect(s.emptyTitle).not.toContain('✓')
    expect(s.emptyTitle).toBe('Fleet list unavailable')
    expect(s.emptyHint).toContain('all-clear cannot be confirmed')
  })

  it('no fault combination reaches the ✓ except the fully-healthy one', () => {
    // Exhaustive over the fault dimensions rather than one case each: a future
    // reordering of the branches cannot re-open a route by accident.
    for (const listError of [true, false]) {
      for (const statsLoaded of [true, false]) {
        for (const statsError of [true, false]) {
          for (const rosterSize of [0, 3]) {
            for (const failed24h of [0, null]) {
              const s = failuresTileState({
                listLoaded: true,
                listError,
                statsLoaded,
                statsError,
                itemCount: 0,
                failed24h,
                rosterSize,
              })
              const healthy =
                !listError && statsLoaded && !statsError && rosterSize > 0 && failed24h === 0
              expect(
                s.emptyTitle.includes('✓'),
                `listError=${listError} statsLoaded=${statsLoaded} statsError=${statsError} `
                  + `rosterSize=${rosterSize} failed24h=${failed24h}`,
              ).toBe(healthy)
            }
          }
        }
      }
    }
  })

  // --- the two states that are not empty ------------------------------------

  it('renders rows when there are rows', () => {
    const s = failuresTileState({ ...HEALTHY, itemCount: 2, failed24h: 2 })
    expect(s.state).toBe('ready')
    expect(s.note).toBeNull()
  })

  it('explains "counted but not listed" instead of contradicting itself', () => {
    // /stats counts status IN ('failed','error'); the list endpoint filters ONE
    // status. A fleet whose only recent failures are legacy 'error' rows would
    // otherwise render "3 in 24h" beside "No failures in 24h ✓".
    const s = failuresTileState({ ...HEALTHY, itemCount: 0, failed24h: 3 })
    expect(s.state).toBe('ready')
    expect(s.note).toContain('3 failed')
    expect(s.emptyTitle).toBe('')
  })

  it('a refresh failure over loaded rows stays ready (stale-while-revalidate)', () => {
    const s = failuresTileState({ ...HEALTHY, itemCount: 4, listError: true, statsError: true })
    expect(s.state).toBe('ready')
  })

  it('defaults to loading when called with nothing', () => {
    expect(failuresTileState().state).toBe('loading')
  })
})
