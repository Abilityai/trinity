// #2703 — what a Save / assign says about DELIVERY, in one place.
//
// Before this every assign surface said "Saved. Sync now, or the agent picks
// them up on next start." — true, and the reason a Library assign silently
// did nothing until someone pressed Sync. The backend now delivers on assign
// and reports honestly; this maps its report onto the three things a user
// needs to know (it worked / it will on start / it did not, and why). Pure,
// so the wording rule is testable without mounting a panel (vitest runs
// `environment: 'node'`).

export const DELIVERY_TONE = Object.freeze({ ok: 'ok', pending: 'pending', bad: 'bad', none: 'none' })

const REASON_TEXT = Object.freeze({
  injection_in_progress: 'the agent is mid-sync',
  agent_not_ready: 'the agent is still starting',
  docker_unavailable: 'the container state could not be read',
  injection_error: 'the install failed',
})

/** #2914 — one sentence for a name conflict, shared by the partial and conflict arms. */
function conflictClause(names) {
  const list = names.length ? ` (${names.join(', ')})` : ''
  const noun = names.length === 1 ? 'a skill' : 'skills'
  return `the agent already has its own ${noun} with that name${list} — its copy is kept and runs. Unassign the library skill, or rename the agent's.`
}

/**
 * @param {{status?: string, reason?: string, skills?: Record<string,{status:string,error?:string}>}|null|undefined} report
 * @param {{ saved?: boolean }} [opts]  `saved` prefixes "Saved" (the Skills tab); the Library control omits it.
 * @returns {{ tone: string, text: string, needsSync: boolean }}
 */
export function deliveryText(report, { saved = true } = {}) {
  const lead = saved ? 'Saved' : 'Assigned'
  if (!report || !report.status) {
    // Nothing was added (a PUT that only dropped names, or an older backend).
    return { tone: DELIVERY_TONE.none, text: saved ? 'Saved.' : 'Assigned.', needsSync: false }
  }
  switch (report.status) {
    case 'injected':
      return { tone: DELIVERY_TONE.ok, text: `${lead} and delivered — available now.`, needsSync: false }
    case 'partial': {
      const failed = Object.entries(report.skills || {})
        .filter(([, v]) => v && v.status === 'failed')
        .map(([k]) => k)
      const conflicts = Object.entries(report.skills || {})
        .filter(([, v]) => v && v.status === 'conflict')
        .map(([k]) => k)
      // #2914: a conflict is not a failed install — a retry via Sync would
      // refuse again by design. Name it apart, and only ask for a Sync when
      // something actually failed.
      if (!failed.length && conflicts.length) {
        return {
          tone: DELIVERY_TONE.bad,
          text: `${lead}; ${conflictClause(conflicts)}`,
          needsSync: false,
        }
      }
      const names = failed.length ? ` (${failed.join(', ')})` : ''
      const tail = conflicts.length ? ` ${conflictClause(conflicts)}` : ''
      return {
        tone: DELIVERY_TONE.bad,
        text: `${lead}; some skills did not install${names}. Sync now to retry.${tail}`,
        needsSync: true,
      }
    }
    case 'conflict': {
      // #2914: every requested name collides with a skill the agent wrote
      // itself. Nothing was written; the agent's own copy is what runs.
      const conflicts = Object.entries(report.skills || {})
        .filter(([, v]) => v && v.status === 'conflict')
        .map(([k]) => k)
      return {
        tone: DELIVERY_TONE.bad,
        text: `${lead} but not delivered: ${conflictClause(conflicts)}`,
        needsSync: false,
      }
    }
    case 'pending_start':
      return {
        tone: DELIVERY_TONE.pending,
        text: `${lead} — the agent is stopped, so it applies on next start.`,
        needsSync: false,
      }
    case 'in_progress':
      return {
        tone: DELIVERY_TONE.pending,
        text: `${lead} — still installing; the lists update when it lands.`,
        needsSync: false,
      }
    case 'not_delivered': {
      const why = REASON_TEXT[report.reason] || 'it could not be delivered'
      return {
        tone: DELIVERY_TONE.bad,
        text: `${lead} but not delivered: ${why}. Sync now, or the agent picks it up on next start.`,
        needsSync: true,
      }
    }
    default:
      return { tone: DELIVERY_TONE.pending, text: `${lead}.`, needsSync: false }
  }
}
