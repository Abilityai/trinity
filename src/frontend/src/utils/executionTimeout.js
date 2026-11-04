/**
 * The per-agent execution timeout as Settings › General edits it
 * (trinity-enterprise#353, inside #887).
 *
 * The backend stores seconds and accepts 60–7200 (TIMEOUT-001 / #665); people
 * think in minutes, so the control edits minutes and this module owns the one
 * conversion and the two error texts.
 */
export const TIMEOUT_MIN_MINUTES = 1
export const TIMEOUT_MAX_MINUTES = 120
export const TIMEOUT_DEFAULT_MINUTES = 60

/** A named, actionable reason the value cannot be saved, or '' when it can. */
export function validateTimeoutMinutes(value) {
  const n = typeof value === 'string' ? Number(value.trim()) : value
  if (value === '' || value === null || value === undefined || Number.isNaN(n)) {
    return `Enter a number of minutes between ${TIMEOUT_MIN_MINUTES} and ${TIMEOUT_MAX_MINUTES}, for example ${TIMEOUT_DEFAULT_MINUTES}.`
  }
  if (!Number.isInteger(n)) {
    return `Use whole minutes, for example ${Math.round(n) || TIMEOUT_MIN_MINUTES}.`
  }
  if (n < TIMEOUT_MIN_MINUTES || n > TIMEOUT_MAX_MINUTES) {
    return `The timeout must be between ${TIMEOUT_MIN_MINUTES} and ${TIMEOUT_MAX_MINUTES} minutes (2 hours); ${n} is outside that.`
  }
  return ''
}

export const minutesToSeconds = (minutes) => Number(minutes) * 60
export const secondsToMinutes = (seconds) => Math.round(Number(seconds) / 60)

/**
 * What a failed save means, in the operator's words.
 *
 * The one refusal that needs its own sentence is #929's: the agent cap is the
 * ceiling for every schedule, so lowering it under an active schedule's own
 * timeout is refused with `agent_timeout_below_active_schedules`. The text
 * names the conflict and where to fix it (the Schedules tab), and
 * `schedulesConflict` lets the row offer that tab as a button.
 */
export function describeTimeoutError(err) {
  const detail = err?.response?.data?.detail
  if (detail && typeof detail === 'object' && detail.error === 'agent_timeout_below_active_schedules') {
    const count = Array.isArray(detail.blocking_schedules) ? detail.blocking_schedules.length : 0
    const maxMinutes = secondsToMinutes(detail.max_schedule_timeout_seconds || 0)
    const which = count === 1 ? 'An active schedule runs' : `${count || 'Some'} active schedules run`
    return {
      message: `${which} for up to ${maxMinutes} min, longer than the new cap. Shorten ${count === 1 ? 'that schedule' : 'those schedules'} on the Schedules tab first, or keep the cap at ${maxMinutes} min or more.`,
      schedulesConflict: true,
    }
  }
  if (typeof detail === 'string' && detail) return { message: detail, schedulesConflict: false }
  if (detail && typeof detail === 'object' && detail.message) return { message: detail.message, schedulesConflict: false }
  return { message: 'The timeout was not saved. Try again.', schedulesConflict: false }
}
