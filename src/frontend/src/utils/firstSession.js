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
import { CLAUDE_TAB_COPY } from '../components/onboarding/steps/credentialSteps'

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
 * Agents the person already runs elsewhere (trinity-enterprise#1353). The
 * answer goes to the assistant like the other one; it answers with the steps
 * card (`local-move`, written by utils/localMove.js with this instance's
 * address). The assistant keeps, per person, that the steps were given or
 * that they said no, and the status says so (`local_agents`): the opening
 * then leaves out the answer already given. Steps already given: it asks
 * whether the agents are here instead.
 */
export const PATH_HERE = 'My agents are here'

export function openingGroup(status) {
  const [local, scratch] = OPENING_GROUP.items
  if (status?.local_agents === 'no') return { ...OPENING_GROUP, items: [scratch] }
  if (status?.local_agents === 'offered') {
    return { ...OPENING_GROUP, items: [{ label: PATH_HERE, text: PATH_HERE, path: 'here' }, scratch] }
  }
  return { ...OPENING_GROUP, items: [local, scratch] }
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
 * Connecting Claude at the moment the first agent is about to run. The
 * assistant asks for it with the shared credential field (ent#1358,
 * `ask_credential` for this instance): the person pastes it into the masked
 * field, it is saved on this instance and the assistant hears only
 * "Provided" / "Not provided".
 *
 * The Claude step (trinity-enterprise#1354, ruled 2026-11-24): the field shows
 * three short steps — open a terminal on your own computer, run Claude's own
 * `claude setup-token`, paste the token it prints here — with an Anthropic API
 * key as the alternative in the same field. The instance starts no sign-in and
 * exchanges no code; it tests the token before registering it. The card keeps
 * the name the assistant asked under (the gateway's script asks for
 * ANTHROPIC_API_KEY; a card kept from an older conversation may carry either):
 * the instance takes both names as this one card.
 */
export const CLAUDE_TOKEN_CARD = Object.freeze({ name: 'CLAUDE_SUBSCRIPTION', command: 'claude setup-token' })

/** The instance's own Claude credentials: the API key, and the subscription token card. */
export function isClaudeCredential(card, tokenCard = CLAUDE_TOKEN_CARD) {
  if (!card || card.agent) return false
  return card.name === 'ANTHROPIC_API_KEY' || Boolean(tokenCard && card.name === tokenCard.name)
}

/**
 * What the Claude field says. With the token card: three steps (the second
 * names the command), and the API key as the alternative. Without it: where
 * to create an API key.
 */
export function claudeHelp(tokenCard = CLAUDE_TOKEN_CARD) {
  const apiKey = { label: 'Create one at Anthropic', href: CLAUDE_TAB_COPY.api_key.providerUrl }
  if (!tokenCard) return { lead: 'No key yet?', ...apiKey }
  return {
    steps: [
      { before: 'Open a terminal on your own computer.' },
      { before: 'Run', command: tokenCard.command, after: 'and follow what it says.' },
      { before: 'Paste the token it prints here and press Save.' },
    ],
    alternative: { lead: 'Using an Anthropic API key instead? Paste it here.', ...apiKey },
  }
}

/**
 * The example agents kept off the first screen (checkpoint 4): only where the
 * assistant leads, only until the person has an agent of their own, and never
 * once they asked to see them.
 */
export function examplesToHide(status, seeded = [], ownAgentCount = 0, shownOnRequest = false) {
  if (!status?.leads_setup || shownOnRequest || ownAgentCount > 0) return []
  return Array.isArray(seeded) ? seeded.slice() : []
}
export const SHOW_EXAMPLES_KEY = 'trinity-show-example-agents'
