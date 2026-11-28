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

/** Whether the brain is worth offering: it is there, and readable or not, it exists. */
export function offerable(state) {
  return Boolean(state?.agent) && state.state !== 'absent'
}

function notesWords(n) {
  if (typeof n !== 'number') return ''
  if (n === 0) return 'no notes yet'
  return n === 1 ? '1 note' : `${n} notes`
}

/**
 * The card's title: who is already on the person's team, as a list of
 * `{ name, role }`. A second core agent joins the same card with one more
 * entry: "Already on your team: Cornelius, your company brain".
 */
export function teamLabel(members = []) {
  const parts = (members || []).filter((m) => m?.name && m?.role).map((m) => `${m.name}, your ${m.role}`)
  if (!parts.length) return ''
  const list = parts.length === 1 ? parts[0] : `${parts.slice(0, -1).join('; ')}; and ${parts[parts.length - 1]}`
  return `Already on your team: ${list}`
}

/**
 * The card's title: the Chief of Staff (when the instance has one) and the
 * company brain (when it is there), in one card.
 */
export function brainLabel(name, chief = null) {
  return teamLabel([...(chief ? [{ name: chief, role: 'Chief of Staff' }] : []), ...(name ? [{ name, role: 'company brain' }] : [])])
}

/** What the Chief of Staff is for, and how to start, in the words the Workspace shows. */
export function chiefWords(name) {
  return `To start ${name}, your Chief of Staff, open ${name} in your Workspace, tell it your name, and drop a meeting transcript or an email on the conversation (it says "Drop files to send to ${name}"). It reads it and tells you what needs doing.`
}

/** The line under it: what each agent is, and where the brain stands now. */
export function brainNote(name, state, chief = null) {
  const cos = chief ? `${chief} came with this instance too: give it a meeting transcript or an email, tell it your name, and it keeps track of what needs doing.` : ''
  if (!name) return cos
  return [brainState(name, state), cos].filter(Boolean).join(' ')
}

function brainState(name, state) {
  const what = `${name} came with this instance. It keeps what your company knows: give it documents and it writes them up as notes that you and every agent you build can ask about.`
  if (state?.state === 'ready') return `${what} It is ready, with ${notesWords(state.notes) || 'its notes'}.`
  if (state?.state === 'unknown') return `${what} I can't reach it right now, so I can't say whether it is set up.`
  return `${what} Setting it up takes a few minutes, and you don't type anything.`
}

export function brainButton(state) {
  return !offerable(state) || state?.state === 'ready' ? 'Show me how' : 'Set it up'
}

/** Where documents go, in the words the Workspace shows. */
export function dropWords(name) {
  return `To give it a document, open ${name} in your Workspace and drop the file on the conversation (it says "Drop files to send to ${name}"), or attach it with "Attach a file for the agent", and say "take this in". ${name} works out what it is, writes notes from it, and replies with a short summary of what it made.`
}

/** The card's ending once the brain is ready (and, when there is one, how to start the Chief of Staff). */
export function readyWords(name, state, chief = null) {
  const notes = notesWords(state?.notes)
  const brain = name ? `${name} is ready${notes ? `, with ${notes}` : ''}. ${dropWords(name)}` : ''
  return [brain, chief ? chiefWords(chief) : ''].filter(Boolean).join(' ')
}

/** What the assistant is told, in plain words, so it says the same thing. */
export function brainLine(name, state, what, chief = null) {
  const cos = chief ? ` The Chief of Staff ${chief} takes a meeting transcript or an email dropped on its conversation, once the person has told it their name.` : ''
  if (!name) return `${what}${cos}`
  const where = state?.state === 'ready'
    ? `ready${typeof state.notes === 'number' ? ` with ${notesWords(state.notes)}` : ''}`
    : state?.state === 'not_set_up' ? 'not set up yet' : 'not readable right now'
  return `${what} The company brain ${name} is ${where}. Documents reach it in the Workspace: the person drops a file on ${name}'s conversation and says "take this in"; it writes notes and replies with a summary.${cos}`
}

/** The brain's state for the assistant's context: only what the instance said. */
export function brainContext(state) {
  if (!offerable(state)) return null
  return { agent: state.agent, state: state.state, ...(typeof state.notes === 'number' ? { notes: state.notes } : {}) }
}
