/**
 * cronValidation.js — client-side mirror of the backend's cron acceptance grammar (#925).
 *
 * SOURCE OF TRUTH: `src/backend/services/schedule_validation.py::validate_cron_expression`
 * (strict 5-field split + `_dow_to_apscheduler` translation) + APScheduler 3.11's
 * CronTrigger field expressions (`apscheduler/triggers/cron/expressions.py`).
 * This file is a hand-rolled, zero-dependency port of that EXACT grammar — not of
 * "standard cron". Cron libs (cron-parser, cronstrue) implement a foreign grammar
 * (@daily, 6-field, quartz L/#, dow 0-6 wrap …) with proven verdict disagreements.
 *
 * CONTRACT: `tests/fixtures/cron-grammar-cases.json` — probe-generated against the live
 * backend validator, asserted row-for-row by BOTH `tests/unit/test_925_cron_grammar_fixture.py`
 * (backend CI env — fires on an APScheduler grammar change) and
 * `src/frontend/tests/unit/cronValidation.spec.js` (this mirror). Grammar drift fails CI loudly.
 *
 * PARITY OUTRANKS TIDINESS. Several upstream behaviours look like bugs and must NOT be
 * "fixed" here — each is probe-verified server-ACCEPT and pinned by a fixture row:
 *   - Name expressions are START-ANCHORED PREFIX matches with no step group
 *     (APScheduler's Month/Weekday/LastDay regexes carry no `$` anchor), so
 *     `MON/999`, `jan/0`, `jan-feb/5`, `mon-`, `lastx`, `last/0` all ACCEPT
 *     (trailing garbage / steps silently dropped).
 *   - `_dow_to_apscheduler` comma-branch asymmetry: a range INSIDE a comma list stays raw
 *     numeric (`0-6,1` → `0-6,mon` ACCEPT) while a bare range maps both endpoints
 *     (`0-6` → `sun-sat` — inverted in APScheduler's mon=0..sun=6 order — REJECT).
 *   - Falsy-zero range end: APScheduler computes the step span as `(self.last or MAX)`
 *     (Python truthiness), so an explicit last of 0 falls back to the field max —
 *     `0-0/2` (minute) ACCEPTs while `5-5/1` REJECTs. Mirrored with JS `||`, NOT `??`,
 *     intentionally-because-upstream-buggy: `??` would reject every `N-0/step` row on a
 *     min-0 field that the server accepts (a false warning — the damaging direction).
 *
 * DOCUMENTED DIVERGENCE (client-stricter, absurd input only): digits are ASCII `[0-9]`.
 * Python's `\d` / `int()` also accept Unicode digits (`٥`), `+` signs and underscores;
 * the server therefore accepts e.g. `٥ * * * *` that this mirror rejects. Marked
 * `divergence: "client-stricter"` in the fixture; never mirrored on purpose.
 *
 * FAIL-OPEN: `validateCronExpression` is total and never throws. An internal error
 * (port bug) returns `{valid: true, error: null}` + console.error — client validation
 * is advisory UX; the backend 400 remains the enforcement authority.
 */

// Unix cron day-of-week (0/7=Sun) → APScheduler named days.
// Mirrors schedule_validation._DOW_NAMES verbatim.
const DOW_NAMES = { 0: 'sun', 1: 'mon', 2: 'tue', 3: 'wed', 4: 'thu', 5: 'fri', 6: 'sat', 7: 'sun' }

const MONTHS = ['jan', 'feb', 'mar', 'apr', 'may', 'jun', 'jul', 'aug', 'sep', 'oct', 'nov', 'dec']
// APScheduler order: mon=0 .. sun=6 (NOT unix 0=Sun).
const WEEKDAYS = ['mon', 'tue', 'wed', 'thu', 'fri', 'sat', 'sun']

// Field order + bounds mirror APScheduler's MIN_VALUES/MAX_VALUES for the five
// fields the backend passes to CronTrigger.
const FIELDS = [
  { name: 'minute', min: 0, max: 59 },
  { name: 'hour', min: 0, max: 23 },
  { name: 'day', min: 1, max: 31, extra: 'day' },
  { name: 'month', min: 1, max: 12, extra: 'month' },
  { name: 'day_of_week', min: 0, max: 6, extra: 'dow' },
]

/** The schedule-form quick presets. Exported so "presets never warn" is tested
 *  against the SHIPPED list, not a mirrored literal (labels/expressions are
 *  byte-identical to the former hardcoded buttons). */
export const CRON_PRESETS = [
  { label: 'Daily 9 AM', expression: '0 9 * * *' },
  { label: 'Weekly Mon', expression: '0 9 * * 1' },
  { label: 'Every 6h', expression: '0 */6 * * *' },
  { label: 'Every 30m', expression: '*/30 * * * *' },
]

// Verbatim port of schedule_validation._dow_to_apscheduler — BRANCH ORDER MATTERS
// (`/` passthrough before `,` before `-` before single). Do not reorder.
function dowToApscheduler(dow) {
  const token = (t) => {
    // Python `_DOW_NAMES[int(t)]` with ValueError/KeyError → token unchanged.
    // ASCII-digits-only is the documented client-stricter divergence (int() also
    // takes unicode digits / '+' / underscores — absurd inputs, not mirrored).
    if (!/^[0-9]+$/.test(t)) return t
    const name = DOW_NAMES[Number(t)]
    return name !== undefined ? name : t
  }
  if (dow === '*' || dow.includes('/')) return dow
  if (dow.includes(',')) return dow.split(',').map(token).join(',')
  if (dow.includes('-')) {
    // Python split('-', 1): split at the FIRST '-' only.
    const i = dow.indexOf('-')
    return `${token(dow.slice(0, i))}-${token(dow.slice(i + 1))}`
  }
  return token(dow)
}

// Echoed user tokens are bounded so a pasted blob can't balloon the error slot.
function echo(item) {
  return item.length > 24 ? `${item.slice(0, 24)}…` : item
}

function itemError(rawItem, field, reason) {
  return `Invalid cron expression: "${echo(rawItem)}" is not valid for ${field.name}${reason ? ` — ${reason}` : ''}`
}

/**
 * Validate one comma-separated item of one field. Mirrors APScheduler's
 * compile_expression: compilers tried in order, first REGEX match wins (a match
 * whose constructor/validate_range fails is an error, never a fall-through).
 * `item` is the post-translation token (dow), `rawItem` the user's original.
 * Returns an error string or null.
 */
function checkItem(item, rawItem, field) {
  if (item === '') {
    return `Invalid cron expression: ${field.name} has an empty list item`
  }

  // 1. AllExpression: r'\*(?:/(?P<step>\d+))?$'
  let m = /^\*(?:\/([0-9]+))?$/.exec(item)
  if (m) {
    if (m[1] !== undefined) {
      const step = Number(m[1])
      if (step === 0) return itemError(rawItem, field, 'step must be at least 1')
      if (step > field.max - field.min) {
        return itemError(rawItem, field, `step ${step} is larger than the ${field.min}–${field.max} range`)
      }
    }
    return null
  }

  // 2. RangeExpression: r'(?P<first>\d+)(?:-(?P<last>\d+))?(?:/(?P<step>\d+))?$'
  m = /^([0-9]+)(?:-([0-9]+))?(?:\/([0-9]+))?$/.exec(item)
  if (m) {
    const first = Number(m[1])
    let last = m[2] !== undefined ? Number(m[2]) : undefined
    const step = m[3] !== undefined ? Number(m[3]) : undefined
    // AllExpression.__init__: zero step raises before anything else.
    if (step === 0) return itemError(rawItem, field, 'step must be at least 1')
    // RangeExpression.__init__: bare value ⇒ last = first (so `8` in dow hits the ≤max check).
    if (last === undefined && step === undefined) last = first
    if (last !== undefined && first > last) {
      return itemError(rawItem, field, 'range first value is greater than last')
    }
    // validate_range, in APScheduler's order: super() full-span step check first…
    if (step !== undefined && step > field.max - field.min) {
      return itemError(rawItem, field, `step ${step} is larger than the ${field.min}–${field.max} range`)
    }
    if (first < field.min) {
      return itemError(rawItem, field, `values must be ${field.min}–${field.max}`)
    }
    if (last !== undefined && last > field.max) {
      return itemError(rawItem, field, `values must be ${field.min}–${field.max}`)
    }
    // …then the actual-span step check. `||` (not `??`) is INTENTIONAL: mirrors
    // APScheduler's Python-truthiness `(self.last or MAX) - self.first`, where an
    // explicit last of 0 falls back to the field max (probed: `0-0/2` ACCEPT,
    // `5-5/1` REJECT). `??` would reject server-valid crons — see header comment.
    const span = (last || field.max) - first
    if (step !== undefined && step > span) {
      return itemError(rawItem, field, `step ${step} is larger than the range of "${echo(item)}"`)
    }
    return null
  }

  // 3. Field-specific compilers (all PREFIX matches — no `$` anchor upstream).
  if (field.extra === 'day') {
    // LastDayOfMonthExpression: re.compile(r'last', re.IGNORECASE), re.match ⇒
    // start-anchored prefix. `lastx`/`last-`/`last/0` ACCEPT; `xlast` falls through.
    // (WeekdayPositionExpression needs an embedded space — unreachable after the
    // whitespace field split, so deliberately not ported.)
    if (/^last/i.test(item)) return null
  } else if (field.extra === 'month' || field.extra === 'dow') {
    // Month/WeekdayRangeExpression: r'(?P<first>[a-z]+)(?:-(?P<last>[a-z]+))?'
    // (IGNORECASE, no end anchor) — trailing garbage/steps silently dropped.
    const names = field.extra === 'month' ? MONTHS : WEEKDAYS
    const nm = /^([a-zA-Z]+)(?:-([a-zA-Z]+))?/.exec(item)
    if (nm) {
      // Regex matched ⇒ this compiler OWNS the item; a bad name is an error,
      // never a fall-through (mirrors the propagating ValueError).
      const firstIdx = names.indexOf(nm[1].toLowerCase())
      if (firstIdx === -1) {
        return itemError(rawItem, field, `"${echo(nm[1])}" is not a recognized name (use ${names[0]}..${names[names.length - 1]})`)
      }
      if (nm[2] !== undefined) {
        const lastIdx = names.indexOf(nm[2].toLowerCase())
        if (lastIdx === -1) {
          return itemError(rawItem, field, `"${echo(nm[2])}" is not a recognized name (use ${names[0]}..${names[names.length - 1]})`)
        }
        if (firstIdx > lastIdx) {
          return itemError(rawItem, field, 'range first value is greater than last')
        }
      }
      return null
    }
  }

  return itemError(rawItem, field, null)
}

/**
 * Validate a cron expression exactly as the backend will.
 * Total: coerces null/undefined/non-strings; never throws (fail-open on internal error).
 * @returns {{valid: boolean, error: string|null}}
 */
export function validateCronExpression(expr) {
  try {
    const text = String(expr ?? '').trim()
    // ''.split(/\s+/) is [''] — an empty expression is 0 fields, like Python's ''.split().
    const fields = text === '' ? [] : text.split(/\s+/)
    if (fields.length !== 5) {
      return {
        valid: false,
        error:
          `Invalid cron expression: expected 5 fields (minute hour day month day_of_week), ` +
          `got ${fields.length} — e.g. "0 9 * * *"`,
      }
    }

    for (let f = 0; f < FIELDS.length; f++) {
      const field = FIELDS[f]
      const raw = fields[f]
      // The backend translates the WHOLE dow field before CronTrigger sees it.
      const translated = field.extra === 'dow' ? dowToApscheduler(raw) : raw
      // The translation never adds/removes commas, so raw/translated items align
      // positionally — errors echo what the user actually typed.
      const items = translated.split(',')
      const rawItems = raw.split(',')
      for (let i = 0; i < items.length; i++) {
        const err = checkItem(items[i], rawItems[i] !== undefined ? rawItems[i] : items[i], field)
        if (err) return { valid: false, error: err }
      }
    }
    return { valid: true, error: null }
  } catch (e) {
    // Advisory layer: a port bug must not brick the panel or block saves.
    // The server 400 remains the authority.
    console.error('cronValidation: internal error (failing open)', e)
    return { valid: true, error: null }
  }
}

/**
 * Pure display gate for the form's inline error (node-testable, not buried in a computed).
 * - valid ⇒ never show.
 * - editing ⇒ show whenever invalid (incl. empty/whitespace stored cron — a disabled
 *   Update button must never be unexplained).
 * - creating ⇒ show only after first blur (touched) AND for non-empty input — empty
 *   stays with the native `required` bubble; no mid-typing flash before first blur.
 */
export function shouldShowCronError({ valid, expr, touched, editing }) {
  if (valid) return false
  if (editing) return true
  return Boolean(touched) && String(expr ?? '').trim() !== ''
}

/**
 * id → validity for the schedules list (row warning icons).
 * Plain object (NOT a Map) — template bracket access. A null/absent cron on a row
 * coerces to invalid without throwing.
 */
export function computeCronValidityMap(schedules) {
  const map = {}
  for (const s of schedules || []) {
    if (!s || s.id === undefined || s.id === null) continue
    map[s.id] = validateCronExpression(s.cron_expression).valid
  }
  return map
}
