/**
 * The company brain (trinity-enterprise#1409, epic #1407).
 *
 * Cornelius comes with every instance and is part of the product, not an
 * example. After the person's first result the assistant offers it in one
 * card; accepting sets it up (its own `/first-run`) with no command typed, and
 * the card then says it is ready and how to give it documents. The instance
 * says where it stands (`GET /api/onboarding/brain`): absent, not set up,
 * ready with how many notes, or unknown. Everything said here is written from
 * that answer, never guessed.
 */

/** "Offered and answered" is kept in this browser: a "Not now" never comes back. */
export const BRAIN_OFFER_KEY = 'trinity-assistant-brain-offer'

/** What `/first-run` is told: unattended, so the brain starts empty (the template's default). */
export const BRAIN_SETUP_MESSAGE = '/first-run --empty'

export function brainOfferAnswered() {
  try {
    return Boolean(localStorage.getItem(BRAIN_OFFER_KEY))
  } catch {
    return true      // storage refused: never offer again and again
  }
}
export function markBrainOffer(answer) {
  try {
    localStorage.setItem(BRAIN_OFFER_KEY, answer)
  } catch { /* it may be offered once more; harmless */ }
}

/** The seeded agents the screens call examples: every one but the brain. */
export function examplesOf(seeded = [], brain = null) {
  return Array.isArray(seeded) ? seeded.filter((n) => n !== brain) : []
}

/** Whether the brain is worth offering: it is there, and readable or not, it exists. */
export function offerable(state) {
  return Boolean(state?.agent) && state.state !== 'absent'
}

function notesWords(n) {
  if (typeof n !== 'number') return ''
  if (n === 0) return 'no notes yet'
  return n === 1 ? '1 note' : `${n} notes`
}

/** The card's title. */
export function brainLabel(name) {
  return `${name}, your company brain`
}

/** The line under it: what the brain is, and where it stands now. */
export function brainNote(name, state) {
  const what = `${name} came with this instance. It keeps what your company knows: give it documents and it writes them up as notes that you and every agent you build can ask about.`
  if (state?.state === 'ready') return `${what} It is ready, with ${notesWords(state.notes) || 'its notes'}.`
  if (state?.state === 'unknown') return `${what} I can't reach it right now, so I can't say whether it is set up.`
  return `${what} Setting it up takes a few minutes, and you don't type anything.`
}

export function brainButton(state) {
  return state?.state === 'ready' ? 'Show me how' : 'Set it up'
}

/** Where documents go, in the words the Workspace shows. */
export function dropWords(name) {
  return `To give it a document, open ${name} in your Workspace and drop the file on the conversation (it says "Drop files to send to ${name}"), or attach it with "Attach a file for the agent", and say "take this in". ${name} works out what it is, writes notes from it, and replies with a short summary of what it made.`
}

/** The card's ending once the brain is ready. */
export function readyWords(name, state) {
  const notes = notesWords(state?.notes)
  return `${name} is ready${notes ? `, with ${notes}` : ''}. ${dropWords(name)}`
}

/** What the assistant is told, in plain words, so it says the same thing. */
export function brainLine(name, state, what) {
  const where = state?.state === 'ready'
    ? `ready${typeof state.notes === 'number' ? ` with ${notesWords(state.notes)}` : ''}`
    : state?.state === 'not_set_up' ? 'not set up yet' : 'not readable right now'
  return `${what} The company brain ${name} is ${where}. Documents reach it in the Workspace: the person drops a file on ${name}'s conversation and says "take this in"; it writes notes and replies with a summary.`
}

/** The brain's state for the assistant's context: only what the instance said. */
export function brainContext(state) {
  if (!offerable(state)) return null
  return { agent: state.agent, state: state.state, ...(typeof state.notes === 'number' ? { notes: state.notes } : {}) }
}
