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

/**
 * Whether a chat header offers project controls, and which. An invited
 * outside client (v2.4) sees a linked chat's badge and Detach only — no
 * "Project" button (guests don't create or file into projects) and no Wrap up
 * (their chats carry no project context).
 */
export function chatProjectActions({ projectsAvailable, isPlatform, isMain, sessionId, project }) {
  const base = projectsAvailable === true && !isMain && Boolean(sessionId)
  const platform = isPlatform === true
  const linked = base && Boolean(project)
  const show = base && (platform || linked)
  return { show, linked, canAdd: show && platform, canWrapUp: linked && platform }
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
  if (form?.steward === 'person' && !/^[^@\s]+@[^@\s]+$/.test(String(form?.steward_email || '').trim())) {
    errors.steward = "Enter the steward's work email, e.g. name@your-company.com."
  }
  const url = String(form?.tracker_url || '').trim()
  if (url && !/^https?:\/\/\S+$/i.test(url)) {
    errors.tracker_url = 'Use a full web link, e.g. https://github.com/your-org/repo/issues/12.'
  }
  return errors
}

/**
 * The form's steward fields for a stored steward. `me` covers "not set" too:
 * the server defaults a new project's steward to its creator.
 */
export function stewardFormValue(steward, myEmail) {
  if (steward && steward.kind === 'agent') return { steward: `agent:${steward.ref}`, steward_email: '' }
  if (steward && steward.kind === 'person' && steward.ref && steward.ref !== myEmail) {
    return { steward: 'person', steward_email: steward.ref }
  }
  return { steward: 'me', steward_email: '' }
}

/** The API's steward for the form's fields; null leaves the server default. */
export function stewardPayload(form, myEmail) {
  if (String(form.steward || '').startsWith('agent:')) return { kind: 'agent', ref: form.steward.slice(6) }
  if (form.steward === 'person') return { kind: 'person', ref: String(form.steward_email || '').trim().toLowerCase() }
  return myEmail ? { kind: 'person', ref: myEmail } : null
}

// --- v2: tasks and the log ------------------------------------------------------

export const TASK_STATUSES = ['active', 'blocked', 'needs-decision', 'paused', 'pending-verification', 'done']
const TASK_STATUS_LABEL = {
  active: 'Active', blocked: 'Blocked', 'needs-decision': 'Needs a decision', paused: 'Paused',
  'pending-verification': 'Awaiting verification', done: 'Done',
}
export const TASK_STATUS_BADGE = Object.freeze({
  active: 'info', blocked: 'danger', 'needs-decision': 'warning', paused: 'neutral',
  'pending-verification': 'autonomous', done: 'success',
})

export function taskStatusLabel(status) {
  return TASK_STATUS_LABEL[status] || 'Unknown'
}

/** Open = anything but awaiting verification or done (the PM standard's lattice). */
export function isOpenTask(task) {
  return !['pending-verification', 'done'].includes(task?.status)
}

/** What the status control offers: a done task can only be reopened. */
export function taskStatusOptions(task) {
  return task?.status === 'done' ? ['done', 'active'] : [...TASK_STATUSES]
}

export function groupTasks(tasks) {
  const byId = (a, b) => String(a.id).localeCompare(String(b.id), undefined, { numeric: true })
  const list = [...(tasks || [])].sort(byId)
  return {
    open: list.filter(isOpenTask),
    verifying: list.filter((t) => t.status === 'pending-verification'),
    done: list.filter((t) => t.status === 'done'),
  }
}

export const LOG_KINDS = ['decision', 'deliverable', 'task', 'blocker', 'handoff', 'note']
// `status` is written by a health update (v3.3), never offered in the add form.
const LOG_KIND_LABEL = {
  decision: 'Decision', deliverable: 'Deliverable', task: 'Task', blocker: 'Blocker', handoff: 'Hand-off', note: 'Note',
  status: 'Status',
}
export const LOG_KIND_BADGE = Object.freeze({
  decision: 'purple', deliverable: 'success', task: 'info', blocker: 'danger', handoff: 'warning', note: 'neutral',
  status: 'info',
})

export function logKindLabel(kind) {
  return LOG_KIND_LABEL[kind] || 'Note'
}

/** "You", "scout (agent)", "Imported from corbin", or the email. */
export function authorLabel(author, myEmail) {
  const a = String(author || '')
  if (a.startsWith('agent:')) return `${a.slice(6)} (agent)`
  if (a.startsWith('import:')) return `Imported from ${a.slice(7)}`
  if (myEmail && a === myEmail) return 'You'
  return a || 'Unknown'
}

/** The fixed instruction Wrap up sends into a project chat (design §v2.6). */
export function wrapUpPrompt(projectName) {
  return `Please wrap up this conversation for the project "${projectName}". ` +
    'Record what came out of it in the project log with add_project_log_entry — one entry per ' +
    'meaningful outcome: each decision made, deliverable finished, blocker hit or hand-off. ' +
    'Update the project tasks to match (create, move to pending-verification with your evidence, ' +
    'or add notes), and skip anything that was only discussion. Then tell me briefly what you recorded.'
}

const TASK_LOG_KIND_LABEL = {
  created: 'Created', status: 'Status', reopened: 'Reopened', done_claim: 'Done claim',
  note: 'Note', imported: 'Imported',
}

/** A task-log entry's kind in words ("Done claim", "Reopened"). */
export function taskLogKindLabel(kind) {
  return TASK_LOG_KIND_LABEL[kind] || 'Note'
}

// --- v3: the hub (health, roll-up, metrics, steward digest) ----------------------

export const HEALTH_STATES = ['on-track', 'at-risk', 'off-track']
const HEALTH_LABEL = { 'on-track': 'On track', 'at-risk': 'At risk', 'off-track': 'Off track' }
export const HEALTH_BADGE = Object.freeze({ 'on-track': 'success', 'at-risk': 'warning', 'off-track': 'danger' })

export function healthLabel(state) {
  return HEALTH_LABEL[state] || 'No health yet'
}

const plural = (n, one, many) => `${n} ${n === 1 ? one : many}`

/** "5 open · 1 blocked · 2 done" — statuses in the lattice's order, zero counts left out. */
export function rollupLine(byStatus = {}) {
  const parts = TASK_STATUSES.filter((st) => byStatus[st]).map((st) => `${byStatus[st]} ${taskStatusLabel(st).toLowerCase()}`)
  return parts.join(' · ') || 'No tasks yet'
}

/**
 * What needs the steward on one project, as short lines in the order a steward
 * acts on them. Shared by the steward view and the project's roll-up card.
 */
export function attentionLines(item) {
  const lines = []
  const n = (list) => (Array.isArray(list) ? list.length : 0)
  if (n(item.awaiting_verification)) lines.push({ key: 'verify', text: `${plural(n(item.awaiting_verification), 'task', 'tasks')} to verify` })
  if (n(item.stuck)) lines.push({ key: 'stuck', text: `${plural(n(item.stuck), 'task', 'tasks')} blocked or waiting on a decision` })
  if (n(item.stale)) lines.push({ key: 'stale', text: `${plural(n(item.stale), 'task', 'tasks')} untouched for a week` })
  if (item.open_asks) lines.push({ key: 'asks', text: `${plural(item.open_asks, 'open ask', 'open asks')}` })
  if (item.health_due) lines.push({ key: 'health', text: item.health ? 'Health update due' : 'No health update yet' })
  if (item.quiet) lines.push({ key: 'quiet', text: 'Quiet for two weeks' })
  return lines
}

/** SVG polyline points for a small trend, or '' when there's nothing to draw. */
export function trendPoints(values, width = 96, height = 24, pad = 2) {
  const v = (values || []).filter((x) => typeof x === 'number' && Number.isFinite(x))
  if (v.length < 2) return ''
  const min = Math.min(...v)
  const max = Math.max(...v)
  const span = max - min || 1
  const step = (width - pad * 2) / (v.length - 1)
  return v.map((x, i) => {
    const px = pad + i * step
    const py = max === min ? height / 2 : pad + (1 - (x - min) / span) * (height - pad * 2)
    return `${Math.round(px * 10) / 10},${Math.round(py * 10) / 10}`
  }).join(' ')
}

/** The metric tile's state line. The one stale rule decided `stale`; this only words it. */
export function metricStateLabel(tile) {
  if (tile.declared === false) return 'No longer declared'
  if (tile.freshness === 'no_points' || !tile.latest) return 'Not measured yet'
  if (tile.stale === true) return 'Stale'
  return ''
}
