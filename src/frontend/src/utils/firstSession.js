/**
 * The first session where the assistant leads setup (trinity-enterprise#1355).
 *
 * The instance says so on the assistant's status (`leads_setup`). There the
 * setup dialog does not open by itself and the assistant is the only wizard:
 * one opening question with two answers, then a path that ends with the
 * person's own agent having run once.
 *
 * Everything the page writes for that session lives here, and so do the
 * names of its steps, so journey events (trinity-enterprise#1352) have one
 * place to take them from. Nothing here is sent anywhere by itself.
 */

/** The steps of the first session, in order. Names only: nothing emits them yet. */
export const FIRST_SESSION_STEPS = Object.freeze([
  'opened',            // the assistant opened by itself, with the one question
  'path_chosen',       // "already run agents" or "start from scratch" (or a typed answer)
  'site_given',        // the website link the proposals are made from
  'agents_proposed',   // three agents that fit the business
  'build_clicked',     // the person picked one and pressed Build
  'claude_connected',  // connected at the moment the first agent is about to run
  'first_run',         // the agent ran once, right away
  'result_opened',     // its result is in front of the person
  'schedule_offered',  // a schedule, offered after the result
  'channel_offered',   // one channel: a chat app or email
  'trial_offered',     // the trial and the subscribe offer
  'tour',              // the short look around the places that now matter
])

export const PATH_LOCAL = 'I already run agents in Claude Code'
export const PATH_SCRATCH = 'Start from scratch'

export const OPENING =
  "Hi, I'm Trinity's assistant 👋\n\nLet's get **an agent of your own** working in a few minutes. Where are you starting from?"

/** The one question, as the panel's starter group: two buttons and nothing else. */
export const OPENING_GROUP = Object.freeze({
  heading: 'Where are you starting from?',
  items: [
    { label: PATH_LOCAL, text: PATH_LOCAL, path: 'local' },
    { label: PATH_SCRATCH, text: PATH_SCRATCH, path: 'scratch' },
  ],
})

/**
 * The hand-off for agents the person already runs elsewhere. Moving them in is
 * its own piece of work (trinity-enterprise#1353); until it lands this is the
 * one place the button leads, and it says so plainly, with the other path open.
 */
export const LOCAL_HANDOFF =
  "Bringing in agents you already run is coming soon. Meanwhile I can build you a new one from scratch in a few minutes: press **Start from scratch** below, or tell me about your business."

/** The seam for that hand-off: one function to replace when it exists. */
export function localHandOff() {
  return { reply: LOCAL_HANDOFF, offer: [OPENING_GROUP.items[1]] }
}

/**
 * Whether this is the assistant-led first session: the instance says the
 * assistant leads, and the person has no agent of their own yet (the ones the
 * install came with do not count).
 */
export function isFirstSession(status, fleet = [], examples = []) {
  if (!status?.leads_setup) return false
  return !(fleet || []).some((a) => a?.name && !examples.includes(a.name))
}

/**
 * The first result: where the assistant leads, the first run that ends well
 * opens in front of the person, once. Kept in this browser until journey
 * events (trinity-enterprise#1352) can say it from the instance.
 */
export const FIRST_RESULT_KEY = 'trinity-assistant-first-result'
export function firstResultSeen() {
  try {
    return Boolean(localStorage.getItem(FIRST_RESULT_KEY))
  } catch {
    return true      // storage refused: never jump the page about
  }
}
export function markFirstResultSeen() {
  try {
    localStorage.setItem(FIRST_RESULT_KEY, String(Date.now()))
  } catch { /* it may open once more; harmless */ }
}

/** Where a run's result is read in the app. */
export function resultPath(agent, runId) {
  return `/agents/${encodeURIComponent(agent)}/executions/${encodeURIComponent(runId)}`
}

/**
 * The session keeps its script until the first result has been seen, even
 * once the person has an agent of their own (it was just built).
 */
export function inFirstSession(status, fleet = [], examples = []) {
  if (!status?.leads_setup) return false
  return isFirstSession(status, fleet, examples) || !firstResultSeen()
}

/**
 * Connecting Claude at the moment the first agent is about to run. Today the
 * panel shows a masked field and saves the key on this instance (never sent to
 * the assistant). The one-button sign-in is its own work
 * (trinity-enterprise#1354): when it exists, it is set here, as
 * `{ label, start() }`, and the card shows that button instead of the field.
 */
export const CLAUDE_SIGN_IN = null
