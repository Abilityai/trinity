/**
 * Decidable rules for the skill-runner admin panel (ent#242).
 *
 * Pure and exported because `vitest` runs `environment: 'node'` with no
 * component-mount harness — a rule that lives inside an SFC is one no test can
 * reach (the ent#392 precedent). The panel is a dispatcher over this file.
 *
 * The shape these rules read is `GET /api/enterprise/skill-runner/status`,
 * whose own docstring says each field is "independently honest" — "enabled but
 * the runner is stopped" is a real, distinguishable state rather than a blanket
 * unavailable. These rules exist to keep it that way on the way to the screen:
 * every arm below is a state the server can actually report, and none of them
 * collapse into a spinner.
 */

/** The runner's own lifecycle, independent of whether the feature is on. */
export const RUNNER_ABSENT = 'absent'
export const RUNNER_STOPPED = 'stopped'
export const RUNNER_RUNNING = 'running'

/**
 * Why the operator cannot act right now, or null when they can.
 *
 * Ordered by what must be true FIRST: a library that was never pulled cannot be
 * synced onto a runner, so reporting "not provisioned" over an unconfigured
 * library would send someone to the wrong screen. The server would refuse
 * either way — this decides which sentence the person reads.
 */
export function blockingReason(status) {
  const lib = (status && status.library) || {}
  if (!status) return 'Status unavailable.'
  if (lib.error) return 'The skills library status could not be read.'
  if (!lib.configured) return 'No skills library is configured yet.'
  if (!lib.synced) return 'The skills library is configured but has never been pulled.'
  return null
}

/** Where a blocked operator should go next — a dead end is a bug (Quality Bar). */
export function blockingAction(status) {
  const lib = (status && status.library) || {}
  if (!status || lib.error) return null
  if (!lib.configured || !lib.synced) {
    return { label: 'Open the Skills library', tab: 'skills' }
  }
  return null
}

export function runnerState(status) {
  if (!status || !status.runner_exists) return RUNNER_ABSENT
  return status.runner_running ? RUNNER_RUNNING : RUNNER_STOPPED
}

/**
 * One line describing the runner, never "unavailable" for three different
 * facts. `absent` and `stopped` are the pair most surfaces collapse.
 */
export function runnerSummary(status) {
  switch (runnerState(status)) {
    case RUNNER_RUNNING:
      return { text: `${status.runner_agent} is running`, variant: 'success' }
    case RUNNER_STOPPED:
      return { text: `${status.runner_agent} is provisioned but not running`, variant: 'warning' }
    default:
      return { text: 'No runner agent yet', variant: 'neutral' }
  }
}

/**
 * Can each action be pressed?
 *
 * `provision` is disabled with a REASON rather than enabled-and-let-it-fail
 * (autoplan taste decision 2): the panel already knows the library is not
 * usable, so spending a round trip and a failed operation to be told so is
 * worse than saying it.
 *
 * `sync` additionally needs a runner — there is nothing to push onto otherwise.
 * Toggling `enabled` is deliberately NOT blocked by library state: turning the
 * feature off must always be possible, and turning it on before the library
 * exists is a legitimate order of operations.
 */
export function actionState(status, { busy = null } = {}) {
  const blocked = blockingReason(status)
  const runner = runnerState(status)
  return {
    toggle: { disabled: !status || busy !== null, busy: busy === 'toggle' },
    provision: {
      disabled: Boolean(blocked) || busy !== null,
      busy: busy === 'provision',
      reason: blocked,
      // Idempotent server-side: a second call only re-syncs.
      label: runner === RUNNER_ABSENT ? 'Provision runner' : 'Re-provision',
    },
    sync: {
      disabled: Boolean(blocked) || runner === RUNNER_ABSENT || busy !== null,
      busy: busy === 'sync',
      reason: blocked || (runner === RUNNER_ABSENT ? 'No runner agent yet.' : null),
    },
  }
}

/**
 * Counts for the header. Shown even when the feature is OFF (autoplan taste
 * decision 3) — they are the honest answer to "what happens if I turn this
 * on", and hiding them makes a dormant install look like an empty one.
 */
export function headerCounts(status) {
  if (!status) return []
  return [
    { label: 'skills exposed', value: Number(status.exposed_skill_count) || 0 },
    { label: 'access grants', value: Number(status.grant_count) || 0 },
    { label: 'in library', value: Number((status.library || {}).skill_count) || 0 },
  ]
}

/**
 * Is this grant already in place?
 *
 * The server is the authority — this only stops the panel from offering a
 * duplicate that would be a no-op or an error. Compared case-sensitively
 * because the backend stores and matches the names verbatim; lower-casing here
 * would claim a duplicate the server does not see.
 */
export function isDuplicateGrant(grants, callerAgent, skillName) {
  if (!callerAgent || !skillName) return false
  return (Array.isArray(grants) ? grants : []).some(
    (g) => g && g.caller_agent === callerAgent && g.skill_name === skillName
  )
}

/**
 * The grant rows out of `GET /api/enterprise/skill-runner/access`.
 *
 * The service answers `{ grants: [...] }` (`service.list_access` wraps
 * `db.list_access`), and the panel first shipped reading the payload AS the
 * array — so `grants` was always `[]`, every grant rendered as "No agent can
 * run any skill yet", and the only human writer for the ACL had no revoke
 * path. A bare list is accepted too, so a server that unwraps later does not
 * silently empty the panel the other way. Anything else — a missing field, a
 * dict-shaped grants value, `null` — is an empty list, never a throw: the
 * status half of the same `load()` must still render.
 */
export function grantsFrom(payload) {
  if (Array.isArray(payload)) return payload
  const rows = payload && typeof payload === 'object' ? payload.grants : null
  return Array.isArray(rows) ? rows : []
}

/** Can the grant form be submitted? Returns a reason when not. */
export function grantFormState(grants, callerAgent, skillName, { busy = false } = {}) {
  if (busy) return { canSubmit: false, reason: null }
  if (!callerAgent || !skillName) return { canSubmit: false, reason: null }
  if (isDuplicateGrant(grants, callerAgent, skillName)) {
    return { canSubmit: false, reason: `${callerAgent} can already run ${skillName}.` }
  }
  return { canSubmit: true, reason: null }
}

/**
 * The confirm sentence for a revoke.
 *
 * Names the CALLER, not just the skill: "Revoke summarise" reads harmlessly,
 * while the decision actually being made is which agent loses the ability to
 * execute it. The autoplan security pass called this out specifically.
 */
export function revokePrompt(grant) {
  if (!grant) return null
  return `Stop ${grant.caller_agent} from running ${grant.skill_name}?`
}

/** Filter rows by caller or skill — a fleet has tens of grants, not thousands. */
export function filterGrants(grants, query) {
  const needle = String(query || '').trim().toLowerCase()
  const rows = Array.isArray(grants) ? grants : []
  if (!needle) return rows
  return rows.filter(
    (g) =>
      String(g?.caller_agent || '').toLowerCase().includes(needle) ||
      String(g?.skill_name || '').toLowerCase().includes(needle)
  )
}
