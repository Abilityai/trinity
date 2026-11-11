/**
 * A welcome per account type. Trinity has four: `user` (Workspace only), `operator` (the
 * Dashboard too, to run agents shared with them), `creator` and `admin` (who build, and
 * have Get started). Someone who cannot build is told so at the start, in plain words,
 * and shown only what their account has: no guide, no levels, no walk to a setting that
 * belongs to an agent's owner. Pure, so every rule is tested in node.
 */
import { walkDoorSteps } from './portalBuildTour'

/**
 * Which welcome: only for someone signed in to Trinity whose role the server reported.
 * Someone an agent was shared with by email has no account here, and no admin to ask.
 */
export function accountKind({ isPlatformSession = false, role = '' } = {}) {
  if (!isPlatformSession) return null
  return role === 'user' || role === 'operator' ? role : null
}

/** Once per kind of account (kept on the saved answers), so a raised role is welcomed again. */
export function welcomeDue({ kind = null, profileVerified = false, intake = null } = {}) {
  return Boolean(kind && profileVerified && intake?.accountWelcome !== kind)
}

const NOTHING_SHARED = 'No agents have been shared with you yet. Ask your admin or a colleague to share one.'
const ASK = 'Need more? Ask your admin.'

const WORDS = Object.freeze({
  user: {
    account: 'Workspace account',
    lead: 'You have a Workspace account. You can work with the agents your team shared with you.',
    can: [
      'Chat with the agents shared with you',
      'Answer what they ask you, in your Inbox',
      'Pick up any chat where you left it',
    ],
    cannot: [
      'Building agents',
      'The Dashboard, where agents are set up and run',
    ],
  },
  operator: {
    account: 'Operator account',
    lead: 'You have an operator account. You can run the agents shared with you and watch what they do on the Dashboard.',
    can: [
      'Chat with the agents shared with you, and answer what they ask in your Inbox',
      'Open the Dashboard to see each agent shared with you and what it is doing',
      'Start an agent and give it a task',
    ],
    cannot: [
      'Building agents',
      "Changing an agent's settings: its schedules, its keys and who it is shared with belong to its owner or an admin",
    ],
  },
})

/** What the welcome says for this kind of account, or null for one that builds. */
export function accountWelcome(kind, { agents = [] } = {}) {
  const words = WORDS[kind]
  if (!words) return null
  return { ...words, empty: Array.isArray(agents) && agents.length ? '' : NOTHING_SHARED, closing: ASK }
}

/** The short walk round the Workspace: what they have, and for an operator the way to the Dashboard. */
export function welcomeTourSteps(kind) {
  const steps = [
    {
      target: 'agents',
      title: 'These are your agents',
      body: 'The agents shared with you. Click one to start a chat with it, and say what you need in your own words.',
    },
    {
      target: 'inbox',
      title: 'This is your Inbox',
      body: 'When an agent needs a decision or your approval, it asks you here, and waits for your answer.',
    },
  ]
  if (kind !== 'operator') return steps
  return [...steps, {
    ...walkDoorSteps()[0],
    body: 'The Dashboard shows each agent shared with you and what it is doing. This link is always here, in the sidebar. It opens in a new tab, and there each click is lit up for you.',
  }]
}

/** For someone who builds but did not set Trinity up: said once, in Get started. */
export const CREATOR_NOTE = 'You have a creator account: you build agents and manage your own. Setting up Trinity itself (connecting Claude, adding people) belongs to your admin.'
