/**
 * Pure rules behind the Workspace loop panel (ent#458).
 *
 * Everything decidable lives here rather than in the SFC: `vitest.config.js`
 * runs `environment: 'node'` with no component-mount harness, so a rule written
 * inside a component is a rule no test can reach. The components below are
 * dispatchers over these functions.
 */

// Statuses that mean "this loop is still going". Mirrors the backend's own
// ACTIVE_STATUSES (`stores/loops.js`, `db/loops.py`); a status the frontend has
// never heard of is treated as NOT active, so an unknown value can never leave
// the panel claiming work is in flight forever.
export const ACTIVE_STATUSES = ['queued', 'running']

/**
 * Guardrail defaults shown before Start (AC #1).
 *
 * These mirror the OPTIONAL fields' defaults in `models.StartLoopRequest` —
 * the server is the authority and
 * re-validates everything; showing them here is what makes the bounds visible
 * BEFORE the user commits, which is the actual acceptance criterion. Cross-
 * language, so they cannot be imported: `portalLoops.spec.js` pins each value
 * against the backend model's declared default so the two fail loudly instead
 * of drifting quietly.
 */
export const GUARDRAIL_DEFAULTS = Object.freeze({
  no_progress_threshold: 3,      // stop after K identical replies (#1157)
  max_consecutive_failures: 3,   // continue-mode cutoff (#1167)
  on_failure: 'abort',           // fail fast (#1167)
  delay_seconds: 0,
  max_cost_usd: null,            // null = no budget (#1155)
  max_duration_seconds: null,    // null = no deadline (#1156)
})

/**
 * What the form starts with. NOT the same thing as the block above, and kept
 * apart deliberately: `max_runs` is REQUIRED by the server (`Field(...)`, no
 * default), so 10 is this UI's suggestion and nothing else. Folding it into
 * GUARDRAIL_DEFAULTS would make the parity test assert a server default that
 * does not exist — a pin holding a fiction in place.
 */
export const FORM_INITIAL = Object.freeze({
  ...GUARDRAIL_DEFAULTS,
  message: '',
  max_runs: 10,
  stop_signal: '',
  timeout_per_run: null,
})

export function isActive(loop) {
  return ACTIVE_STATUSES.includes(loop?.status)
}

export function activeLoops(loops) {
  return (loops || []).filter(isActive)
}

/**
 * The word a person reads for a finished loop — honest about WHY it ended.
 *
 * "Stopped" alone is the answer that wastes someone's afternoon: a loop that
 * hit its cost budget, one that ran out of wall clock, one that was repeating
 * itself, and one a human stopped are four different situations with four
 * different next actions. The runtime already distinguishes them in
 * `stop_reason`; this is only the place that refuses to flatten them.
 */
export function loopStatusLabel(loop) {
  const status = loop?.status
  const reason = loop?.stop_reason
  if (status === 'queued') return 'Queued'
  if (status === 'running') return 'Running'
  if (status === 'completed') return 'Done'
  if (status === 'completed_with_errors') return 'Done, with errors'
  if (status === 'failed') {
    return reason === 'max_consecutive_failures' ? 'Failed — too many errors in a row' : 'Failed'
  }
  if (status === 'interrupted') return 'Interrupted by a restart'
  if (status === 'stopped') {
    switch (reason) {
      case 'budget_exhausted': return 'Stopped — cost budget reached'
      case 'deadline_exceeded': return 'Stopped — time limit reached'
      case 'no_progress': return 'Stopped — it stopped making progress'
      case 'stop_signal_matched': return 'Stopped — it reported it was done'
      case 'max_runs_reached': return 'Done'
      case 'user_stopped': return 'Stopped by you'
      default: return 'Stopped'
    }
  }
  return status ? String(status) : 'Unknown'
}

/** Severity for the status chip. Never a colour name — the SFC owns tokens. */
export function loopStatusTone(loop) {
  const status = loop?.status
  if (isActive(loop)) return 'active'
  if (status === 'completed') return 'ok'
  if (status === 'failed') return 'danger'
  if (status === 'completed_with_errors' || status === 'interrupted') return 'warn'
  if (status === 'stopped') {
    return loop?.stop_reason === 'max_runs_reached' ? 'ok' : 'warn'
  }
  return 'neutral'
}

function ratio(used, limit) {
  if (typeof limit !== 'number' || !(limit > 0)) return null
  if (typeof used !== 'number' || !Number.isFinite(used) || used < 0) return null
  return Math.max(0, Math.min(1, used / limit))
}

/**
 * How much of each guardrail is left (AC #1: "guardrail headroom").
 *
 * A guardrail that was never set reports `null` rather than 0 or 100%: "no
 * budget" and "budget untouched" are different facts, and a bar rendered at
 * either extreme asserts the wrong one. Runs are always bounded (`max_runs` is
 * required), so that entry is always present.
 */
export function loopHeadroom(loop) {
  const runs = ratio(loop?.runs_completed, loop?.max_runs)
  const cost = ratio(loop?.total_cost, loop?.max_cost_usd)
  const time = ratio(loop?.elapsed_seconds, loop?.max_duration_seconds)
  return {
    runs: runs === null ? null : { used: loop.runs_completed, limit: loop.max_runs, fraction: runs },
    cost: cost === null ? null : { used: loop.total_cost, limit: loop.max_cost_usd, fraction: cost },
    time: time === null ? null : { used: loop.elapsed_seconds, limit: loop.max_duration_seconds, fraction: time },
  }
}

/**
 * One line for the collapsed strip.
 *
 * Deliberately says nothing when nothing is running: the strip's whole value is
 * that it is quiet until it isn't, and a permanent "0 loops" chip is noise that
 * trains people to stop reading it.
 */
export function stripSummary(loops) {
  const active = activeLoops(loops)
  if (!active.length) return null
  const agents = new Set(active.map((l) => l.agent_name).filter(Boolean))
  const loopWord = active.length === 1 ? 'loop' : 'loops'
  if (agents.size <= 1) return `${active.length} ${loopWord} running`
  return `${active.length} ${loopWord} running on ${agents.size} agents`
}

/** Group loops by agent, active first, so a room shows who is busy. */
export function byAgent(loops, participants) {
  const out = new Map()
  for (const name of participants || []) out.set(name, [])
  for (const loop of loops || []) {
    const key = loop?.agent_name
    if (!key) continue
    if (!out.has(key)) out.set(key, [])
    out.get(key).push(loop)
  }
  for (const [, list] of out) {
    list.sort((a, b) => (isActive(b) ? 1 : 0) - (isActive(a) ? 1 : 0))
  }
  return out
}

/**
 * Client-side pre-flight for the start form — UX, never authority.
 *
 * The server re-validates all of this (`models.StartLoopRequest` plus the
 * router's #1156 deadline check and ent#338's agent-cap refusal). The point of
 * repeating it is that a person editing a form learns the bound while they are
 * typing instead of after a round trip; anything this misses is still refused
 * with a named reason by the backend.
 */
export function validateStartForm(form, { agentTimeoutCap } = {}) {
  const errors = {}
  const message = (form?.message || '').trim()
  if (!message) errors.message = 'Say what the loop should do each run.'

  const runs = Number(form?.max_runs)
  if (!Number.isInteger(runs) || runs < 1 || runs > 100) {
    errors.max_runs = 'Between 1 and 100 runs.'
  }

  // `1` is rejected, not clamped: "stop after 1 identical reply" cannot mean
  // anything — repetition needs at least two (#1157).
  const k = form?.no_progress_threshold
  if (k !== null && k !== undefined && k !== '' && Number(k) === 1) {
    errors.no_progress_threshold = 'Use 0 to switch this off, or 2 or more.'
  }

  const perRun = numberOrNull(form?.timeout_per_run)
  if (perRun !== null && typeof agentTimeoutCap === 'number' && perRun > agentTimeoutCap) {
    // ent#338: the agent's own ceiling. The server refuses this too — surfacing
    // it here is what stops the user discovering their ceiling by rejection.
    errors.timeout_per_run = `This agent's limit is ${agentTimeoutCap}s.`
  }

  const deadline = numberOrNull(form?.max_duration_seconds)
  const effectivePerRun = perRun ?? (typeof agentTimeoutCap === 'number' ? agentTimeoutCap : null)
  if (deadline !== null && effectivePerRun !== null && deadline < effectivePerRun) {
    errors.max_duration_seconds = `Must be at least one run (${effectivePerRun}s).`
  }

  const budget = numberOrNull(form?.max_cost_usd)
  if (budget !== null && !(budget > 0)) {
    errors.max_cost_usd = 'Leave empty for no budget, or enter more than 0.'
  }

  return { valid: Object.keys(errors).length === 0, errors }
}

function numberOrNull(v) {
  if (v === null || v === undefined || v === '') return null
  const n = Number(v)
  return Number.isFinite(n) ? n : null
}

/** Build the POST body, omitting empty optionals so server defaults apply. */
export function startPayload(form) {
  const body = {
    message: (form?.message || '').trim(),
    max_runs: Number(form?.max_runs),
  }
  const optional = {
    max_cost_usd: numberOrNull(form?.max_cost_usd),
    max_duration_seconds: numberOrNull(form?.max_duration_seconds),
    timeout_per_run: numberOrNull(form?.timeout_per_run),
    delay_seconds: numberOrNull(form?.delay_seconds),
    no_progress_threshold: numberOrNull(form?.no_progress_threshold),
  }
  for (const [k, v] of Object.entries(optional)) {
    if (v !== null) body[k] = v
  }
  const signal = (form?.stop_signal || '').trim()
  if (signal) body.stop_signal = signal
  return body
}

/**
 * The named reason a start was refused, for the states the backend answers with
 * a structured `detail` dict rather than a string (ent#338, #1156, capacity).
 */
export function startErrorMessage(err) {
  const detail = err?.response?.data?.detail
  if (detail && typeof detail === 'object') {
    if (detail.error === 'loop_timeout_exceeds_agent_cap') {
      return `Per-run timeout is above this agent's limit of ${detail.agent_cap_seconds}s.`
    }
    if (detail.message) return detail.message
  }
  if (typeof detail === 'string') return detail
  if (err?.response?.status === 403) return 'You do not have access to run loops on this agent.'
  return 'Could not start the loop.'
}
