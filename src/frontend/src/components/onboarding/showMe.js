// Show me: the real page where a setting lives, with that one control highlighted and a
// short note. Kept apart from the components so a test can read it without a browser.
// Each place is marked on its page with `data-tour="showme-<key>"`. An agent's keys, its
// people, who it may call and its schedules are walks from the Dashboard instead
// (`showMeWalk.js`); their targets here still serve links opened before that.

import { SHOW_ME_WALKS, walkLink } from './showMeWalk'

export const SHOW_ME_TARGETS = Object.freeze({
  permissions: Object.freeze({ tab: 'permissions', title: 'Who this agent can call', body: 'Tick the agents it may call, then save the permissions.' }),
  people: Object.freeze({ tab: 'settings', section: 'people', title: 'Who can use this agent', body: "Add a person's email here to share the agent with them." }),
  schedules: Object.freeze({ tab: 'schedules', title: "This agent's schedules", body: 'Each schedule has its own switch to turn it on or off.' }),
  'public-links': Object.freeze({ tab: 'sharing', title: 'Public chat links', body: 'Open Public links, then create a link that lets anyone chat with this agent.' }),
  skills: Object.freeze({ tab: 'skills', title: "This agent's skills", body: 'Assign skills adds one from your skills library to this agent.' }),
  channels: Object.freeze({ tab: 'sharing', title: 'Slack, Telegram and WhatsApp', body: 'Pick a channel and follow its steps. Anything secret goes in here, never in a chat.' }),
  keys: Object.freeze({ path: '/settings', tab: 'integrations', title: 'API keys', body: 'Keys for the whole instance. Paste them here, never in a chat.' }),
})

/**
 * The page for a Show me, or null for a place it doesn't know. A walk gets what it lights
 * up in `extra`: the key names for credentials, or `{ to }` / `{ schedule }`.
 */
export function showMeLink(key, agent = '', extra = []) {
  if (SHOW_ME_WALKS.includes(key)) return walkLink(key, agent, extra)
  return showMeSpot(key, agent)
}

/** The page itself, with the control lit up: no walk, for any place that has a target. */
export function showMeSpot(key, agent = '') {
  const target = Object.hasOwn(SHOW_ME_TARGETS, key) ? SHOW_ME_TARGETS[key] : null
  if (!target) return null
  const query = new URLSearchParams({ tab: target.tab })
  if (target.section) query.set('section', target.section)
  query.set('showme', key)
  if (target.path) return `${target.path}?${query}`
  if (!agent) return null
  return `/agents/${encodeURIComponent(agent)}?${query}`
}
