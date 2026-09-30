/**
 * Workspace Projects — the decidable rules (trinity-enterprise#661).
 *
 * Pure functions over plain data so they are unit-tested without a mount; the
 * components are dispatchers over them. The server is the authority for every
 * permission — these only decide what to OFFER, never what is allowed.
 */

export const STATUS_BADGE = Object.freeze({ active: 'success', paused: 'warning', done: 'neutral' })
const STATUS_LABEL = { active: 'Active', paused: 'Paused', done: 'Done' }
const VISIBILITY_LABEL = { members: 'Members only', company: 'Company' }
const AGENT_STATE_LABEL = { active: 'Can work on it', pending: 'Waiting for owner', declined: 'Owner declined' }

export const NAME_MAX = 120
export const GOAL_MAX = 1000

export function statusLabel(status) {
  return STATUS_LABEL[status] || 'Unknown'
}

export function visibilityLabel(visibility) {
  return VISIBILITY_LABEL[visibility] || 'Members only'
}

export function agentStateLabel(state) {
  return AGENT_STATE_LABEL[state] || 'Unknown'
}

/**
 * What "Work on it" offers, one row per agent on the project (declined ones
 * are not offered). `kind`:
 *   reopen      — my newest linked chat with this agent
 *   new         — start a chat with this agent and link it
 *   pending     — the agent's owner has not approved it yet
 *   unavailable — the agent is not on my own roster
 *   archived    — the project is archived: nothing new can be linked
 */
export function workOnItOptions(project, rosterNames) {
  const roster = new Set(rosterNames || [])
  const archived = Boolean(project?.archived_at)
  const chats = [...(project?.my_chats || [])].sort(
    (a, b) => String(b.last_message_at || '').localeCompare(String(a.last_message_at || '')),
  )
  return (project?.agents || [])
    .filter((a) => a.state !== 'declined')
    .map((a) => {
      const agent = a.agent_name
      const chat = chats.find((c) => c.agent_name === agent) || null
      let kind
      if (a.state === 'pending') kind = 'pending'
      else if (!roster.has(agent)) kind = 'unavailable'
      else if (chat) kind = 'reopen'
      else kind = archived ? 'archived' : 'new'
      return { agent, kind, chat }
    })
}

/** Whether a chat header offers project controls, and which. */
export function chatProjectActions({ projectsAvailable, isPlatform, isMain, sessionId, project }) {
  const show = projectsAvailable === true && isPlatform === true && !isMain && Boolean(sessionId)
  return { show, linked: show && Boolean(project) }
}

const CODE_MESSAGE = {
  owner_unreachable: "This agent's owner can't be asked here. Ask them to add it to the project themselves.",
  already_linked: 'This chat is already in a project. Detach it there first.',
  agent_not_on_project: "Add this chat's agent to the project first, then try again.",
  main_chat: "The Main chat can't belong to a project. Start a new chat for it.",
  archived: 'This project is archived. Restore it to add to it.',
  not_permitted: 'Only the person who created this project can change that.',
  unknown_person: 'Only people at your company can be added. Check the email, e.g. name@your-company.com.',
  agent_not_found: "That agent isn't on your list, so you can't add it.",
  too_many_members: 'This project has reached its member limit.',
  too_many_links: 'This project has reached its link limit.',
  creator_is_permanent: "The project's creator can't be removed.",
  project_not_found: "This project isn't available to you.",
}

/** What happened and what to do, in the user's words (principle 25). */
export function projectErrorMessage(err) {
  const res = err && err.response
  if (!res) return 'Could not reach the server. Check your connection and try again.'
  const detail = res.data && res.data.detail
  const code = detail && typeof detail === 'object' ? detail.code : null
  if (code && CODE_MESSAGE[code]) return CODE_MESSAGE[code]
  if (detail && typeof detail === 'object' && typeof detail.message === 'string') return detail.message
  return 'Something went wrong on the server. Try again in a moment.'
}

/** The list tabs: all I can see / I'm a member / company, plus a name search. */
export function filterProjects(list, filter, query) {
  const q = String(query || '').trim().toLowerCase()
  return (list || []).filter((p) => {
    if (filter === 'member' && !p.my_role) return false
    if (filter === 'company' && p.visibility !== 'company') return false
    return !q || String(p.name || '').toLowerCase().includes(q)
  })
}

/** Field errors that name the problem and give an example (principle 17). */
export function validateProjectForm(form) {
  const errors = {}
  const name = String(form?.name || '').trim()
  const goal = String(form?.goal || '').trim()
  if (!name) errors.name = 'Give the project a name, e.g. "Q4 launch".'
  else if (name.length > NAME_MAX) errors.name = `Keep the name to ${NAME_MAX} characters or fewer.`
  if (!goal) errors.goal = 'Say what the project is for, e.g. "Ship the self-serve plan to 50 customers."'
  else if (goal.length > GOAL_MAX) errors.goal = `Keep the goal to ${GOAL_MAX} characters or fewer.`
  const url = String(form?.tracker_url || '').trim()
  if (url && !/^https?:\/\/\S+$/i.test(url)) {
    errors.tracker_url = 'Use a full web link, e.g. https://github.com/your-org/repo/issues/12.'
  }
  return errors
}
