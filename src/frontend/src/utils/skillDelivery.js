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
      const names = failed.length ? ` (${failed.join(', ')})` : ''
      return {
        tone: DELIVERY_TONE.bad,
        text: `${lead}; some skills did not install${names}. Sync now to retry.`,
        needsSync: true,
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
