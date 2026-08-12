/**
 * Pure helpers for the Grid's "Recent failures" info tile (ent#100).
 *
 * Everything the tile decides lives here rather than in the SFC, because
 * `vitest.config.js` pins `environment: 'node'` — a pure module is the only
 * part of a tile that can be unit-tested at all. The tile is then a thin
 * renderer over these three functions.
 *
 * ## This module is NOT a failure classifier, and that is the point
 *
 * ent#100's acceptance criteria ask for error-code tags from the platform's
 * taxonomy (AUTH, TIMEOUT, AGENT_ERROR, …). `TaskExecutionErrorCode` is an
 * IN-MEMORY enum on `TerminalEnvelope`: `schedule_executions` has no
 * `error_code` column, so the code is discarded at the terminal write and
 * `FleetExecutionSummary.error_summary` is just `SUBSTR(error, 1, 200)`.
 *
 * Re-deriving the code here — from a 200-character truncation of a message the
 * backend knew the code for and threw away — would be a third, unenforced copy
 * of a classifier that already exists as a byte-identity-mirrored PAIR
 * (`src/backend/services/failure_classifier.py` ↔
 * `src/scheduler/failure_classifier.py`, pinned by
 * `tests/unit/test_904_sigkill_no_false_auth.py::TestBackendSchedulerParity`).
 * A guessed label on an operator's failure dashboard is worse than no label.
 *
 * So `failureCodeFromSummary` READS a marker the platform actually emitted and
 * returns `null` otherwise. Today the only writer of that marker is
 * `services/pull_coordination_service.py`, which architecture.md documents as
 * dark until a pull pilot — so on every current install the chip is absent and
 * the row spends its width on the real `error_summary` instead. When the
 * `error_code` column lands (follow-up, listed in the PR body), the chip starts
 * appearing with zero UI churn.
 */

/**
 * The `[code]` marker the platform writes ahead of the message, or `null`.
 *
 * ANCHORED and charset-bounded on purpose. `error_summary` is agent/LLM-authored
 * text — prompt-injectable content — so a loose match would let an agent print
 * `[critical]` mid-sentence and mint its own chrome on a fleet dashboard.
 *
 * The charset is deliberately lower-case-only: `pull_coordination_service`
 * formats the plain `str` field, so the marker reads `[auth]`. Had it formatted
 * the `str, Enum` member the text would be `[TaskExecutionErrorCode.AUTH]` —
 * which this regex would (correctly) stop matching, rather than rendering a
 * Python repr as a badge.
 *
 * @param {string|null|undefined} errorSummary
 * @returns {string|null} the raw lower-case code, or null when absent
 */
export function failureCodeFromSummary(errorSummary) {
  if (typeof errorSummary !== 'string') return null
  const m = /^\[([a-z][a-z_]{0,31})\]\s*/.exec(errorSummary)
  return m ? m[1] : null
}

/**
 * `error_summary` with any leading `[code]` marker removed, so the message line
 * does not repeat what the chip already says.
 *
 * @param {string|null|undefined} errorSummary
 * @returns {string} '' when there is nothing to show
 */
export function stripFailureCode(errorSummary) {
  if (typeof errorSummary !== 'string') return ''
  return errorSummary.replace(/^\[[a-z][a-z_]{0,31}\]\s*/, '').trim()
}

/**
 * Known `triggered_by` values, kept in step with the backend filter allow-list
 * (`routers/executions.py::_VALID_TRIGGERS`) plus the trigger values that reach
 * `schedule_executions` without being filterable there.
 *
 * This is a DISPLAY passthrough list, never a second taxonomy: the Operations →
 * Executions panel renders `row.triggered_by` raw, and a tile that invented its
 * own words for the same values would show one execution under two names.
 */
const KNOWN_TRIGGERS = new Set([
  'schedule', 'manual', 'agent', 'mcp', 'chat', 'session', 'public', 'webhook',
  'fan_out', 'loop', 'reminder', 'room', 'a2a', 'event', 'voip', 'voice',
  'self_task', 'self_chat', 'telegram', 'slack', 'whatsapp', 'paid', 'user',
])

/**
 * A bounded, display-safe label for a row's `triggered_by`.
 *
 * Falls back to an EXPLICIT `'other'`, never to blank and never to "no value" —
 * the 2026-07-30 learning is that a constant which degrades silently lies. An
 * unrecognised-but-plausible value still renders verbatim (it is the platform's
 * own vocabulary and the Executions panel shows it raw); only a missing or
 * malformed one becomes `other`, which also keeps the column from being blown
 * open by an unexpectedly long string.
 *
 * @param {string|null|undefined} triggeredBy
 * @returns {string}
 */
export function triggerLabel(triggeredBy) {
  if (typeof triggeredBy !== 'string') return 'other'
  const t = triggeredBy.trim()
  if (!t) return 'other'
  if (KNOWN_TRIGGERS.has(t)) return t
  return /^[a-z][a-z0-9_]{0,15}$/.test(t) ? t : 'other'
}

/**
 * The tile's whole state machine, as one pure function (ent#100).
 *
 * ## Why the green state needs a POSITIVE signal, not an absence of rows
 *
 * "No failures in 24h ✓" is a positive claim about the fleet on the fleet's own
 * monitoring surface. There are three independent routes to asserting it from a
 * FAULT, and all three are closed here:
 *
 *  1. **A failed list GET.** Design-contract principle 15 / #1926: an empty
 *     state requires a fetch that succeeded and returned zero, never
 *     `list.length === 0`.
 *  2. **A failed `/stats` GET.** The 24h total is a second request; if it fails
 *     while the list succeeds, the count is UNKNOWN — which is not zero. The
 *     count is never inferred from `items.length`, because a bounded page
 *     cannot produce a 24h total.
 *  3. **An unenumerable fleet.** `accessible_agent_names` resolves through
 *     `docker_service.list_all_agents_fast()`, which returns `[]` when the
 *     Docker client is None AND on ANY exception (denied socket, daemon
 *     restart, timeout — the warning is even throttled). For a non-admin that
 *     makes every fleet accessor early-return zeros at **HTTP 200**: the fetch
 *     "succeeds", `loaded` is honestly true, and a green all-clear is
 *     manufactured by an infrastructure fault. The same fault empties the
 *     Grid's own roster, so a non-empty roster is the positive enumerability
 *     signal available here with no backend change. (The durable fix is the
 *     ent#384 treatment — resolve the accessible set from
 *     `db.get_all_agent_metadata()` — which is a backend change and therefore
 *     out of ent#100's scope.)
 *
 * Note the asymmetry that makes this safe rather than merely cautious: a
 * refresh failure with rows already loaded stays `ready` (stale-while-revalidate,
 * principle 13) — it is only the CONFIRMATION that requires everything to be
 * green on this cycle.
 *
 * @param {object} o
 * @param {boolean} o.listLoaded   a list GET has succeeded at least once
 * @param {boolean} o.listError    the most recent list GET failed
 * @param {boolean} o.statsLoaded  a /stats GET has succeeded at least once
 * @param {boolean} o.statsError   the most recent /stats GET failed
 * @param {number}  o.itemCount    rows currently held
 * @param {number|null} o.failed24h  24h failure total; null = unknown
 * @param {number}  o.rosterSize   agents the Grid can currently enumerate
 * @returns {{state: string, emptyTitle: string, emptyHint: string, note: string|null}}
 */
export function failuresTileState({
  listLoaded = false,
  listError = false,
  statsLoaded = false,
  statsError = false,
  itemCount = 0,
  failed24h = null,
  rosterSize = 0,
} = {}) {
  const blank = { emptyTitle: '', emptyHint: '', note: null }

  // First load has not landed: an outright failure is an error, otherwise the
  // chassis skeleton. Never the empty copy (#1926).
  if (!listLoaded) {
    return { ...blank, state: listError ? 'error' : 'loading' }
  }

  if (itemCount > 0) return { ...blank, state: 'ready' }

  // Counted but not listed. `/stats` counts `status IN ('failed','error')`
  // while the list endpoint filters ONE status, so a fleet whose only recent
  // failures are legacy `'error'` rows lands here. Rendering the green ✓ beside
  // "3 in 24h" would be a self-contradicting tile, so the tile stays `ready`
  // and the body renders this note instead of the row list.
  if (typeof failed24h === 'number' && failed24h > 0) {
    return {
      ...blank,
      state: 'ready',
      note:
        `${failed24h} failed in the last 24h · none in the latest page ` +
        '(older, or a legacy status). Open executions for the full list.',
    }
  }

  if (rosterSize <= 0) {
    return {
      state: 'empty',
      emptyTitle: "Fleet list unavailable",
      emptyHint:
        'No agents could be enumerated, so an all-clear cannot be confirmed. '
        + 'Check the fleet loads, then refresh.',
      note: null,
    }
  }

  const confirmed =
    !listError && statsLoaded && !statsError && failed24h === 0
  if (confirmed) {
    return {
      state: 'empty',
      emptyTitle: 'No failures in 24h ✓',
      emptyHint: 'Nothing needs attention on this window.',
      note: null,
    }
  }

  return {
    state: 'empty',
    emptyTitle: 'No failures listed',
    emptyHint:
      "The 24h total couldn't be read, so this is not a confirmed all-clear.",
    note: null,
  }
}
