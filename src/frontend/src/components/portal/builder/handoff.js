/**
 * A job one of a person's agents did because another of their agents asked: the first
 * time the page sees one, it says so and offers a walk to where the job is listed
 * (`PortalBuildHandoff.vue`). Read from the jobs themselves (`levelSignals.js`), never
 * from what the guide says.
 */
import { walkLink } from '@/components/onboarding/showMeWalk'

const list = (value) => (Array.isArray(value) ? value : [])

/** The newest finished job an own agent did for another own agent: `{ from, to, at }`, or null. */
export function latestHandoff(signals) {
  const own = new Set(list(signals?.own))
  let found = null
  for (const to of own) {
    for (const j of list(signals?.jobs?.[to])) {
      const from = j?.source_agent_name
      if (j?.triggered_by !== 'agent' || j?.status !== 'success') continue
      if (!from || from === to || !own.has(from)) continue
      const at = j.started_at || ''
      if (!found || String(at).localeCompare(String(found.at)) > 0) found = { from, to, at }
    }
  }
  return found
}

/** Offered once: until they opened the walk or put the note away. */
export function handoffDue({ handoff, intake }) {
  return Boolean(handoff) && !intake?.handoffSeen
}

export function markHandoffSeen(intake) {
  return { ...intake, handoffSeen: true }
}

/** The walk to the job: the Dashboard, the agent that did it, its Tasks. */
export function handoffLink(handoff) {
  return handoff ? walkLink('tasks', handoff.to, { from: handoff.from }) : null
}
