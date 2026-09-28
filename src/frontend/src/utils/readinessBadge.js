/**
 * The owner's readiness stamp as a badge on the agents list and the fleet grid
 * (trinity-enterprise#527 rider, operator ruling 2026-09-24).
 *
 * One predicate for both surfaces, like `pressureBadge`: a second copy is how a
 * list and a grid end up disagreeing about the same agent.
 *
 * `readiness` is what `GET /api/agents` attaches: `{status, changed_at, source}`
 * for a stamped companion, `null` otherwise. No stamp → no badge — whether an
 * unstamped agent is a companion at all is in its template.yaml, which a list
 * never reads, so the badge is never a guessed "calibrating". The words and the
 * variants match the role card (`PortalAgentRole.vue`): ready = success,
 * calibrating = warning, both with a dot.
 *
 * @param {{status?: string, changed_at?: string|null, source?: string}|null|undefined} readiness
 * @returns {{label: string, variant: 'success'|'warning', title: string}|null}
 */
export function readinessBadge(readiness) {
  const status = readiness && readiness.status
  if (status !== 'ready' && status !== 'calibrating') return null
  const day = typeof readiness.changed_at === 'string' && /^\d{4}-\d{2}-\d{2}/.test(readiness.changed_at)
    ? readiness.changed_at.slice(0, 10)
    : null
  const since = day ? ` since ${day} (UTC)` : ''
  let title
  if (status === 'ready') {
    title = readiness.source === 'rollout'
      ? `Ready — carried over when the readiness gate shipped${since}`
      : `Ready — marked ready by its owner${since}`
  } else {
    title = `Calibrating${since} — its scheduled brief is paused until its owner marks it ready`
  }
  return { label: status, variant: status === 'ready' ? 'success' : 'warning', title }
}
