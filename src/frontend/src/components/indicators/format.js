/**
 * Words for the behavioral indicators (trinity-enterprise#939). The API sends
 * ids and closed-vocabulary codes only; every sentence a person reads is here.
 */

export const INDICATOR_LABELS = {
  completion: 'Completion',
  checks_passed: 'Checks passed',
  scope_fidelity: 'Scope fidelity',
  escalation_correctness: 'Escalation correctness',
  quality: 'Quality',
  cost_vs_baseline: 'Cost vs baseline',
}

export const INDICATOR_HINTS = {
  checks_passed: 'honesty of claims',
  quality: 'judged runs only',
}

/** The indicators whose runs can be listed (cost has no failing run). */
export const DRILLABLE = ['completion', 'checks_passed', 'scope_fidelity', 'escalation_correctness', 'quality']

export const CHECK_LABELS = {
  execution_exists: 'Run recorded',
  credential_echo: 'No credential in output',
  validate_001: 'Validation',
  deliverable_landed: 'Deliverable landed',
  playbook_outputs: 'Playbook outputs',
  budget_vs_baseline: 'Spend vs baseline',
  scope_fidelity: 'Scope fidelity',
  escalation_correctness: 'Escalation correctness',
}

const REASONS = {
  not_completed: 'The run did not finish',
  claim_without_receipt: 'Said it was done; no record of the result landing',
  background_work_lost: 'Ended while work it started in the background was still running',
  credential_in_output: 'A credential appeared in its output',
  failed_validation: 'Its result failed validation',
  playbook_unresolved: 'The playbook it was asked to run is not installed',
  declared_output_missing: 'An output its playbook declares was not produced',
  outside_baseline: 'Spent far outside its usual range',
  scope_fidelity: 'Did more than it was asked (judge)',
  escalation_correctness: 'Went ahead where it should have asked (judge)',
  low_quality: 'The judge rated the result low',
}

/** One reason code (`check:reason`, `check` or a bare code) as a sentence. */
export function reasonText(code) {
  if (!code) return ''
  const [check, reason] = String(code).split(':')
  if (reason && REASONS[reason]) return REASONS[reason]
  if (REASONS[check]) return REASONS[check]
  const label = CHECK_LABELS[check] || check
  return reason ? `${label} failed (${reason.replace(/_/g, ' ')})` : `${label} failed`
}

export const checkLabel = (id) => CHECK_LABELS[id] || id

/** The headline figure; null when the state is not `ok`. */
export function indicatorValue(ind) {
  if (!ind || ind.state !== 'ok' || typeof ind.value !== 'number') return null
  if (ind.id === 'quality') return `${(ind.value * 5).toFixed(1)} / 5`
  if (ind.id === 'cost_vs_baseline') return `${ind.value.toFixed(1)}×`
  return `${Math.round(ind.value * 100)}%`
}

const usd = (v) => (typeof v === 'number' ? `$${v.toFixed(2)}` : '—')

/** The sample line under the figure — always states what the number rests on. */
export function indicatorSample(ind, days) {
  if (!ind) return ''
  if (ind.state === 'no_runs') return `No graded runs in the last ${days} days`
  switch (ind.id) {
    case 'completion':
      return `${ind.num} of ${ind.n} runs finished the task`
    case 'checks_passed':
      return ind.n ? `${ind.n - ind.num} of ${ind.n} runs failed a check` : 'No runs graded yet'
    case 'scope_fidelity':
      return `${ind.num} of ${ind.n} judged runs did the asked work only`
    case 'escalation_correctness':
      return `${ind.num} of ${ind.n} judged runs that called for it asked or stopped`
    case 'quality':
      return ind.agreement_n
        ? `${ind.n} judged runs · judge agrees with people ${Math.round((ind.agreement || 0) * 100)}% (n=${ind.agreement_n})`
        : `${ind.n} judged runs · no person ratings to calibrate against yet`
    case 'cost_vs_baseline':
      return `${usd(ind.median)} median per run, baseline ${usd(ind.baseline)}`
    default:
      return ''
  }
}

/** Why there is no figure: the sample it has against the sample it needs. */
export function insufficientText(ind, minRuns) {
  if (ind.id === 'cost_vs_baseline') {
    return ind.baseline_n < minRuns
      ? `Not enough history: ${ind.baseline_n} runs in the prior 30 days, needs ${minRuns}`
      : `Not enough runs with a cost: ${ind.n}, needs ${minRuns}`
  }
  const what = ['scope_fidelity', 'escalation_correctness', 'quality'].includes(ind.id) ? 'judged' : 'graded'
  return `Not enough runs: ${ind.n} ${what}, needs ${minRuns}`
}

/** The trend line: points vs the previous window, or the indicator's note. */
export function indicatorFoot(ind, days) {
  if (ind.id === 'cost_vs_baseline') return "baseline = this agent's prior 30 days"
  if (ind.id === 'quality') return ind.agreement_n ? 'calibrated against person ratings' : 'not calibrated yet'
  if (typeof ind.delta !== 'number') return `no prior ${days} days to compare`
  if (ind.delta === 0) return `no change vs prior ${days} days`
  return `${ind.delta > 0 ? '▲' : '▼'} ${Math.abs(ind.delta)} pts vs prior ${days} days`
}

export function footTone(ind) {
  if (typeof ind.delta !== 'number' || ind.delta === 0) return 'text-gray-500 dark:text-gray-400'
  return ind.delta > 0
    ? 'text-status-success-700 dark:text-status-success-400'
    : 'text-status-warning-700 dark:text-status-warning-400'
}

/** A source as a person names it. */
export function sourceText(src) {
  if (!src) return ''
  if (src.kind === 'playbook') return `skill /${src.ref}`
  // No label: the schedule is gone (or not this agent's) — never a raw id.
  if (src.kind === 'schedule') return src.label ? `schedule “${src.label}”` : 'a removed schedule'
  return 'chat turns'
}

/** Whether a source has somewhere to go to fix it. */
export function hasFixPath(src) {
  return src?.kind === 'playbook' || (src?.kind === 'schedule' && !!src.label)
}


export function fmtCost(v) {
  return typeof v === 'number' ? `$${v.toFixed(2)}` : '—'
}

/** Workspace sentences, keyed on indicator + tone (no numbers cross). */
export const PLAIN_WORDS = {
  completion: {
    good: 'Finishes almost everything it is asked to do',
    watch: 'Leaves some tasks unfinished. Worth checking what it was asked.',
    unknown: 'Not enough tasks yet to say how often it finishes',
  },
  checks_passed: {
    good: 'When it says a task is done, the result is there',
    watch: 'Sometimes says a task is done before the result is actually there. Worth checking its work.',
    unknown: 'Not enough tasks yet to say how reliable its "done" is',
  },
  scope_fidelity: {
    good: 'Sticks to what was asked',
    watch: 'Sometimes does more than it was asked. Be specific about what not to touch.',
    unknown: 'Not enough tasks yet to say how well it sticks to what was asked',
  },
  escalation_correctness: {
    good: 'Asks for help when it should',
    watch: 'Sometimes goes ahead where it should have asked. Expect to check its bigger steps.',
    unknown: 'Not enough tasks yet to say how well it asks for help',
  },
}
