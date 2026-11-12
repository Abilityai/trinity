/**
 * Workspace Projects — the decidable rules (trinity-enterprise#661).
 *
 * Pure functions over plain data so they are unit-tested without a mount; the
 * components are dispatchers over them. The server is the authority for every
 * permission — these only decide what to OFFER, never what is allowed.
 */

import { askUrgencyCompare } from '../portalInbox'

// ent#1235: the stored status keeps its values; people read Running / Stopped.
export const STATUS_BADGE = Object.freeze({ not_started: 'neutral', active: 'success', paused: 'warning', done: 'neutral' })
const STATUS_LABEL = { not_started: 'Not started', active: 'Running', paused: 'Stopped', done: 'Done' }
const VISIBILITY_LABEL = { members: 'Members only', company: 'Company' }
const AGENT_STATE_LABEL = {
  active: 'Can work on it', pending: 'Waiting for owner', declined: 'Owner declined', off: 'Not on the project',
}

export const NAME_MAX = 120
export const GOAL_MAX = 1000

// A task's text limits, mirroring `TITLE_MAX` / `TEXT_MAX` in
// `enterprise/backend/projects/work.py` — the server is still the authority;
// these stop the user before a paste reaches the outer Pydantic cap, whose
// refusal has no field to show it under. A test pins the two in step.
export const TASK_TITLE_MAX = 200
export const TASK_TEXT_MAX = 4000

// How a task field is named to a person, and which inline error a server
// refusal belongs under.
const TASK_FIELD_LABEL = { title: 'title', objective: 'objective', done_definition: 'definition of done' }
export const TASK_FIELD_ERROR_CODES = Object.freeze({
  invalid_title: 'title', invalid_objective: 'objective', invalid_done_definition: 'done_definition',
  // ent#1232: a task's shape — its parent and the tasks it waits on.
  invalid_parent: 'parent', parent_not_found: 'parent', parent_is_self: 'parent', parent_is_subtask: 'parent',
  has_subtasks: 'parent', invalid_blocked_by_add: 'blocked_by', invalid_blocked_by_remove: 'blocked_by',
  invalid_blocked_by: 'blocked_by', blocker_not_found: 'blocked_by', blocker_is_self: 'blocked_by',
  blocker_is_parent: 'blocked_by', blocker_cycle: 'blocked_by', too_many_blockers: 'blocked_by',
})

/** Length in CODE POINTS, which is what Python's `len` counts — `.length`
 *  would read 150 emoji as 300 and refuse a title the server accepts. */
const codePoints = (text) => [...String(text ?? '')].length

/**
 * The create dialog's and the detail form's shared check, so "the same limits
 * as at creation" holds in both directions. `only` narrows it to the fields a
 * save actually sends: a legacy stored value over the limit must not block an
 * unrelated change.
 */
export function validateTaskFields(values, only = null) {
  const fields = only || Object.keys(TASK_FIELD_LABEL)
  const errors = {}
  for (const f of fields) {
    if (!(f in TASK_FIELD_LABEL)) continue
    const text = String(values?.[f] ?? '').trim()
    const n = codePoints(text)
    if (f === 'title' && !n) {
      errors.title = 'Give the task a title, e.g. "Draft the launch brief".'
      continue
    }
    const max = f === 'title' ? TASK_TITLE_MAX : TASK_TEXT_MAX
    if (n > max) errors[f] = `Keep the ${TASK_FIELD_LABEL[f]} to ${max} characters (it's ${n} now).`
  }
  return errors
}

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
 * The Continue action (trinity-enterprise#896): ONE primary — my newest linked
 * chat across every agent (reopen), else the first agent I could start with
 * (new) — and the other actionable agents one click away. Pending, archived and
 * unavailable rows are not offered here; the Work on it list explains them.
 */
export function continueChoice(options) {
  const actionable = (options || []).filter((o) => o.kind === 'reopen' || o.kind === 'new')
  const reopen = actionable.filter((o) => o.kind === 'reopen')
    .sort((a, b) => String(b.chat?.last_message_at || '').localeCompare(String(a.chat?.last_message_at || '')))
  const primary = reopen[0] || actionable[0] || null
  return { primary, others: actionable.filter((o) => o !== primary) }
}

/** The Steward card's schedule line for the server's `{state, next_run_at}`. */
export function stewardScheduleLine(schedule) {
  if (!schedule) return ''
  if (schedule.state === 'none') return 'No sweep scheduled'
  if (schedule.state === 'scheduled') return schedule.next_run_at ? 'Next run' : 'Scheduled'
  if (schedule.state === 'hidden') return 'Scheduled'
  return "Schedule couldn't be read"
}

/** The one-time "how a project moves" line (ent#896): remembered per browser. */
export const HOW_IT_MOVES_KEY = 'trinity.workspace.projects.howItMovesDismissed'

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

/**
 * A linked room's agents and where each stands on the project
 * (trinity-enterprise#1131), from the server's `room_agents` block — null
 * when the read has none (a thread, a guest, a participant who can't open the
 * project). The chip counts the agents not on it (`off` / `declined`) before
 * the ones waiting on their owner, and is null when every agent is active.
 * Add is offered only to a MANAGE_AGENTS holder on a row the server marked
 * `can_add`; a row it can't add says why.
 */
const ROOM_AGENT_BADGE = { active: 'success', pending: 'warning', declined: 'neutral', off: 'neutral' }
const ROOM_AGENT_NOTE = { not_on_your_list: 'Not on your list' }
const REQUEST_NOTE = {
  pending: 'Asked — waiting on the creator',
  blocked: 'You have a request waiting on the creator',
}

export function roomAgentRows(project) {
  if (!project || !('room_agents' in project)) return null
  const canManage = project.can?.manage_agents === true
  if (project.room_agents === null) {
    return { rows: [], canManage, degraded: true, chip: { label: "Couldn't read agents", variant: 'neutral' } }
  }
  const rows = (project.room_agents || []).map((a) => ({
    name: a.agent_name,
    state: a.state,
    label: agentStateLabel(a.state),
    badge: ROOM_AGENT_BADGE[a.state] || 'neutral',
    action: canManage && a.can_add === true ? 'add'
      // ent#1131 part 2: a member who can't add asks the creator instead —
      // one request at a time, so none beside a pending one (`blocked`).
      : (a.can_request === true && !['pending', 'blocked'].includes(a.my_request) ? 'request' : null),
    // My request for it: `pending` reads as waiting; a decline offers Ask again.
    requested: a.can_request === true ? (a.my_request || null) : null,
    note: canManage ? ROOM_AGENT_NOTE[a.reason] || null
      : (a.can_request === true ? REQUEST_NOTE[a.my_request] || null : null),
  }))
  const notOn = rows.filter((r) => r.state === 'off' || r.state === 'declined').length
  const waiting = rows.filter((r) => r.state === 'pending').length
  const chip = notOn ? { label: `${notOn} not on it`, variant: 'warning' }
    : waiting ? { label: `${waiting} waiting on owner`, variant: 'neutral' } : null
  return { rows, canManage, degraded: false, chip }
}

const CODE_MESSAGE = {
  owner_unreachable: "This agent's owner can't be asked here. Ask them to add it to the project themselves.",
  already_linked: 'This chat is already in a project. Detach it there first.',
  agent_not_on_project: "Add this chat's agent to the project first, then try again.",
  main_chat: "The Main chat can't belong to a project. Start a new chat for it.",
  archived: 'This project is archived. Restore it to add to it.',
  not_permitted: 'Only the person who created this project can change that.',
  unknown_person: 'Only people at your company can be added. Check the email, e.g. name@your-company.com.',
  owner_not_on_project: "A task's owner must be the project's creator or one of its members. Add them to the project first.",
  invalid_owner: "Pick the owner from the project's people, or write a role as role:<id>. A name on its own reaches nobody.",
  session_required: 'Looking people up needs you signed in to Trinity. Reload the page and try again.',
  context_too_large: 'A brief can be at most 512 KB. Split it into several files.',
  project_context_full: "This project's briefs can total at most 5 MB. Remove one you no longer need first.",
  unsupported_type: 'A brief is markdown (.md) or plain text (.txt). Link other files from Add from… instead.',
  not_text: "This file isn't text. Save it as .md or .txt and try again.",
  empty_brief: 'The brief is empty.',
  agent_not_found: "That agent isn't on your list, so you can't add it.",
  too_many_members: 'This project has reached its member limit.',
  too_many_links: 'This project has reached its link limit.',
  creator_is_permanent: "The project's creator can't be removed.",
  project_not_found: "This project isn't available to you.",
  // ent#1235: why a task's run doesn't start, in the words of the page.
  project_not_started: "This project hasn't been started, so no run starts yet. Someone with edit rights starts it.",
  project_stopped: 'This project is stopped, so no new run starts. Someone with edit rights starts it again.',
  project_done: 'This project is done. Reopen it before starting a run.',
  task_run_live: 'A run is already under way on this task. Open it from the task, or wait for it to end.',
  start_already_proposed: 'A run for this task is already waiting for a go.',
  task_needs_decision: 'This task was rejected twice and needs a person\'s decision first.',
  task_is_for_a_person: "This task is a person's, so no agent is started for it.",
  needs_go_is_human_only: "Only a person holds a task for their go.",
  lifecycle_is_a_persons: 'Only a person starts or stops a project.',
  mode_is_a_persons: "Only a person sets the project's mode.",
  cannot_unstart: "A started project can't go back to not started. Stop it instead.",
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

/**
 * ent#1169 — the dialog's one line under Steward: what picking it does about
 * the library's project-management skills. `options` is the steward-options
 * read: null while it loads ("Checking…"), false when it failed (no line,
 * nothing guessed).
 */
export function stewardSkillHint(steward, options) {
  if (!String(steward || '').startsWith('agent:')) {
    return 'A person steward checks finished tasks and health by hand. An agent steward with the project-management skills does it on its own schedule.'
  }
  if (options === false) return ''        // the read failed: say nothing rather than guess
  if (!options) return 'Checking the project-management skills…'
  const name = steward.slice(6)
  const row = (options.agents || []).find((a) => a.agent_name === name)
  if (row?.holds_set) return `${name} already has the project-management skills, so it can keep this project's record.`
  if (row && row.holds_set === null) return `Trinity couldn't check ${name}'s skills. You can still make it the steward.`
  if (options.set && !options.set.available) return options.set.message || "The project-management skills aren't available on this Trinity yet."
  if (row?.can_assign) return `${name} gets the project-management skills, so it can verify finished tasks, record health and write the log.`
  return `Only ${name}'s owner or an admin can give it the project-management skills. It can be the steward, but it can't keep the record until they do.`
}

/** The API's steward for the form's fields; null leaves the server default. */
export function stewardPayload(form, myEmail) {
  if (String(form.steward || '').startsWith('agent:')) return { kind: 'agent', ref: form.steward.slice(6) }
  if (form.steward === 'person') return { kind: 'person', ref: String(form.steward_email || '').trim().toLowerCase() }
  return myEmail ? { kind: 'person', ref: myEmail } : null
}

// --- v2: tasks and the log ------------------------------------------------------

export const TASK_STATUSES = ['todo', 'active', 'blocked', 'needs-decision', 'paused', 'pending-verification', 'done']
const TASK_STATUS_LABEL = {
  todo: 'To do', active: 'Active', blocked: 'Blocked', 'needs-decision': 'Needs a decision', paused: 'Paused',
  'pending-verification': 'Awaiting verification', done: 'Done',
}
export const TASK_STATUS_BADGE = Object.freeze({
  todo: 'neutral', active: 'info', blocked: 'danger', 'needs-decision': 'warning', paused: 'neutral',
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
  // ent#1232: a subtask sorts straight after its parent (one level deep).
  const num = (id) => String(id || '')
  const cmp = (a, b) => num(a).localeCompare(num(b), undefined, { numeric: true })
  const byId = (a, b) => cmp(a.parent || a.id, b.parent || b.id) || (a.parent ? 1 : 0) - (b.parent ? 1 : 0) || cmp(a.id, b.id)
  const list = [...(tasks || [])].sort(byId)
  return {
    open: list.filter(isOpenTask),
    verifying: list.filter((t) => t.status === 'pending-verification'),
    done: list.filter((t) => t.status === 'done'),
  }
}

// --- ent#1232: a task's shape, who made it and who it is for ---------------------

const TASK_ID = /^T-(\d{1,6})$/

/** Task ids typed with commas or spaces, normalised to T-NNN; the rest named. */
export function parseTaskIds(text) {
  const ids = []
  const bad = []
  for (const raw of String(text || '').split(/[\s,]+/).filter(Boolean)) {
    const m = raw.toUpperCase().match(TASK_ID)
    if (!m) bad.push(raw)
    else {
      const id = `T-${m[1].padStart(3, '0')}`
      if (!ids.includes(id)) ids.push(id)
    }
  }
  return { ids, bad }
}

/** "You", "scout (agent)", or the person's name. */
export function creatorLabel(creator, myEmail, labelOf = (e) => e) {
  if (!creator || !creator.name) return ''
  if (creator.kind === 'agent') return `${creator.name} (agent)`
  if (sameEmail(creator.name, myEmail)) return 'You'
  return labelOf(creator.name) || creator.name
}

/** Who a task is for: the person who owns it and the agent doing it. */
export function assignedLabel(task, labelOf = (e) => e) {
  const parts = []
  if (task?.owner) parts.push(labelOf(task.owner) || task.owner)
  if (task?.agent) parts.push(`${task.agent} (agent)`)
  return parts.join(' · ')
}

/** "2/5 subtasks" on a parent. Finishing them does not finish the parent. */
export function subtaskLabel(task) {
  const s = task?.subtasks
  return s && s.total ? `${s.done}/${s.total} subtasks` : ''
}

/** Waiting on other tasks — derived, and never the Blocked status. */
export function waitingLabel(task) {
  const w = task?.waiting_for || []
  return w.length ? `Waiting on ${w.join(', ')}` : ''
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
  note: 'Note', imported: 'Imported', edited: 'Edited', owner: 'Owner',
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

// #3574 — a project's "Needs you" rows. The Overview is a list, not a second
// Inbox pane: a row is the Inbox's own list row (agent, when, one-line title)
// and the full ask card (`PortalAsks`) mounts only for a row the person opened.
// Three rules, in one place so the hub cannot drift from the Inbox:
//   * pending asks among `mineIds` (the server's project read, already the
//     viewer's own), in the Inbox's urgency order (`askUrgencyCompare`);
//   * a row the person opened stays while its ask is in the list even after it
//     ends, drawn by PortalAsks as ended (the Inbox pane's own rule — the
//     ent#468 confirmation must not vanish the instant the answer lands);
//   * a row inside its Undo window (ent#748, `askDismissals`) is forced OPEN:
//     Undo lives on the card, and a collapsed row would offer "Answer" to a
//     dismissed ask and no way back.
export function projectNeedsYouRows(asks, mineIds, { expanded = {}, dismissals = {} } = {}, now = Date.now()) {
  const ids = new Set((Array.isArray(mineIds) ? mineIds : []).filter(Boolean))
  if (!ids.size) return []
  return (Array.isArray(asks) ? asks : [])
    .filter((a) => a && ids.has(a.id))
    .filter((a) => a.status === 'pending' || Boolean(expanded[a.id]) || Boolean(dismissals[a.id]))
    .sort((a, b) => askUrgencyCompare(a, b, now))
    .map((a) => ({
      id: a.id,
      agent_name: a.agent_name,
      kind: a.kind,
      status: a.status,
      title: a.title || a.question || '',
      at: a.created_at,
      open: Boolean(expanded[a.id]) || Boolean(dismissals[a.id]),
      ask: a,
    }))
}

// --- trinity-enterprise#1132: where a person stands on a project, in plain words ---
// One place for the wording, so the header, the withheld-control lines and the
// task rule never disagree. Roles come from the read (`my_role`: creator /
// member / null for a company viewer); a guest has its own narrow page.

/** The one email comparison (case-insensitive), shared by every project control. */
export const sameEmail = (a, b) => Boolean(a) && Boolean(b) && String(a).toLowerCase() === String(b).toLowerCase()
const same = sameEmail

/** The creator, as the reader should see them. */
export function creatorName(project, myEmail) {
  const c = project?.created_by || ''
  return same(c, myEmail) ? 'you' : (c || 'the creator')
}

/** The header's one line: my role on this project and what it lets me do. */
export function roleLine(project, myEmail) {
  const role = project?.my_role || null
  const creator = creatorName(project, myEmail)
  if (project?.archived_at) {
    return role === 'creator' || creator === 'you'
      ? 'This project is archived. Nothing on it changes until you restore it.'
      : `This project is archived. Nothing on it changes until ${creator} restores it.`
  }
  if (role === 'creator') {
    return 'You created this project. You can change its name, goal, steward, members, visibility and tasks, and write in the log.'
  }
  if (role === 'member') {
    return `You're a member. You can add and change tasks, write in the log and add files. Only ${creator} changes the name, goal, steward, members and visibility.`
  }
  if (!role && project?.visibility === 'company') {
    // ent#1132 part 2: with Ask to join offered, the line points at it.
    const ask = project?.my_request?.can_ask_to_join ? `Ask to join, and ${creator} decides.` : `Ask ${creator} to add you as a member.`
    return `You can view this project because it's open to everyone at the company. You can chat with its agents about it, but only members add or change tasks, the log and files. ${ask}`
  }
  return `You can view this project. ${creator === 'you' ? 'You decide' : `${creator} decides`} who else can change it.`
}

/**
 * The line shown IN PLACE of a withheld control (no lost layout). `what` names
 * the thing: "tasks", "log entries", "files and reports". Decided from the
 * SAME inputs the control's own v-if uses (`canContribute`, `archived`), and
 * only for someone who is not a member (a member has nobody to ask to be
 * added; an archived project already says why nothing changes).
 */
export function withheldLine({ project, myEmail, canContribute, archived }, what) {
  if (canContribute || archived || !project) return ''
  if (project.my_role === 'creator' || project.my_role === 'member') return ''
  if (project.my_request?.can_ask_to_join) return `Only members add or change ${what}. Use Ask to join at the top of the page.`
  return `Only members add or change ${what}. Ask ${creatorName(project, myEmail)} to add you.`
}

/**
 * Who confirms a finished task: a member, or the steward agent when it is an
 * ACTIVE agent on the project (only then can it verify — projects/work.py).
 */
export function verificationRule(project) {
  const s = project?.steward
  const active = s?.kind === 'agent'
    && (project?.agents || []).some((a) => a.agent_name === s.ref && a.state === 'active')
  const who = active ? `a member, or the steward agent ${s.ref},` : 'a member'
  return `An agent's completion claim waits here until ${who} confirms it.`
}

// --- trinity-enterprise#1193: context for agents ------------------------------------
/** One agent read returns at most this many bytes; past it the agent reads on by offset. */
export const AGENT_READ_BYTES = 200 * 1024
/** A file an agent can read as text (the server's `context._is_text`). */
export const isTextMime = (mime) => {
  const m = String(mime || '')
  return m.startsWith('text/') || m === 'application/json' || m === 'application/x-yaml'
}
export const CONTEXT_SHARED_NOTICE = 'Everyone who can read this project can read it.'

/**
 * What an agent receives from one read of `text`: the first part, and whether it
 * is cut. `budget` is the server's `context_limits.agent_read_bytes` (the page
 * read carries it); the constant is only the fallback for an older read. The cut
 * mirrors `context._part` on the server (a character is never split).
 */
export function agentPreview(text, budget = AGENT_READ_BYTES) {
  const bytes = new TextEncoder().encode(text || '')
  if (bytes.length <= budget) return { text: text || '', truncated: false, total: bytes.length, shown: bytes.length }
  let end = budget
  while (end > 0 && (bytes[end] & 0xc0) === 0x80) end -= 1     // never split a character
  return { text: new TextDecoder().decode(bytes.slice(0, end)), truncated: true, total: bytes.length, shown: end }
}

// --- trinity-enterprise#1132 part 2: Ask to join ------------------------------------
/**
 * What the page shows a company viewer about joining, from the read's
 * `my_request`: `ask` (offer the button), `pending` (waiting on the creator),
 * `declined` (the reason, and Ask again), or null (nothing to say — a member,
 * a guest, an older backend, or a read that failed).
 */
export function joinRequestState(project) {
  const mine = project?.my_request
  if (!mine || mine.unknown) return null
  const last = mine.last && mine.last.request === 'join' ? mine.last : null
  if (last && last.state === 'pending') return { kind: 'pending', waitingOn: last.waiting_on || project.created_by }
  if (!mine.can_ask_to_join) return null
  if (last && last.state === 'declined') return { kind: 'declined', reason: last.reason || null }
  return { kind: 'ask' }
}

// --- ent#1235: the lifecycle, the mode and a task's run -----------------------------
// Read tolerantly: a value this page doesn't know never breaks it — it reads as
// "Unknown" and offers nothing, rather than guessing a state that lets work start.

const STATE_OF_STATUS = { not_started: 'not_started', active: 'running', paused: 'stopped', done: 'done' }
const STATE_LABEL = { not_started: 'Not started', running: 'Running', stopped: 'Stopped', done: 'Done', archived: 'Archived' }
export const STATE_BADGE = Object.freeze({
  not_started: 'neutral', running: 'success', stopped: 'warning', done: 'neutral', archived: 'locked',
})

/** `not_started | running | stopped | done | archived`, or `unknown`. */
export function projectState(project) {
  if (!project) return 'unknown'
  if (project.archived_at) return 'archived'
  if (STATE_LABEL[project.state]) return project.state
  return STATE_OF_STATUS[project.status] || 'unknown'
}

export function stateLabel(state) {
  return STATE_LABEL[state] || 'Unknown'
}

const STATUS_VERB = { active: 'Started', paused: 'Stopped', done: 'Marked done' }

/** Who last changed the lifecycle, as "Started by you" — '' when no one has yet. */
export function stateBy(project, myEmail) {
  const by = project?.status_by
  const verb = STATUS_VERB[project?.status]
  if (!by || !verb) return ''
  const who = sameEmail(by, myEmail) ? 'you' : String(by).startsWith('agent:') ? String(by).slice(6) : String(by)
  return `${verb} by ${who}`
}

/** What the header offers a person with edit rights: Start or Stop, and Mark done. */
export function lifecycleActions(project) {
  if (!project?.can?.edit || project.archived_at) return { primary: null, markDone: false }
  const state = projectState(project)
  const primary = {
    not_started: { verb: 'start', label: 'Start project' },
    running: { verb: 'stop', label: 'Stop project' },
    stopped: { verb: 'start', label: 'Start project' },
    done: { verb: 'start', label: 'Reopen project' },
  }[state] || null
  return { primary, markDone: ['not_started', 'running', 'stopped'].includes(state) }
}

/** The stop dialog's words: the runs still finishing, named, or said honestly when unknown. */
export function stopMessage(project) {
  const head = 'Nothing new starts until someone starts it again.'
  const live = project?.runs_live
  if (!Array.isArray(live)) return `${head} Runs already under way finish.`
  if (!live.length) return `${head} No run is under way.`
  const named = live.map((r) => `${r.task_id || 'a task'}${r.agent_name ? ` on ${r.agent_name}` : ''}`).join(', ')
  return `${head} ${live.length === 1 ? 'This run is' : 'These runs are'} still finishing: ${named}.`
}

/** Why a task's run can't start on a project that isn't running, or ''. */
export function notRunningLine(state) {
  return {
    not_started: "This project hasn't been started, so no run starts yet. Someone with edit rights starts it.",
    stopped: 'This project is stopped, so no new run starts. Runs already under way finish.',
    done: 'This project is done. Reopen it to start a run.',
    unknown: "This project's state isn't known here, so no run is offered. Reload the page.",
  }[state] || ''
}

export const PROJECT_MODES = ['track', 'propose', 'auto']
const MODE_LABEL = { track: 'Track', propose: 'Propose', auto: 'Auto' }
const MODE_HINT = {
  track: "A person starts each task's run. The steward keeps the record.",
  propose: 'The steward proposes runs; each one waits for a person\'s go.',
  auto: "The steward starts runs itself, except tasks held for a person's go.",
}

/** The project's mode; a project read without one is Track (the safe default). */
export function projectMode(project) {
  return project?.mode == null ? 'track' : String(project.mode)
}

export function modeLabel(mode) {
  return MODE_LABEL[mode] || 'Unknown'
}

export function modeHint(mode) {
  return MODE_HINT[mode] || ''
}

const RUN_STATE_LABEL = {
  proposed: 'Waiting for a go', started: 'Running', declined: 'Declined', withdrawn: 'Withdrawn', ended: 'Ended',
}
const RUN_END_LABEL = {
  completed: 'Finished', failed: 'Failed', skipped: 'Skipped', gated: 'Stopped by a gate',
  dispatch_lost: 'Never started', dispatch_failed: "Couldn't start",
}
export const RUN_BADGE = Object.freeze({
  proposed: 'warning', started: 'info', declined: 'neutral', withdrawn: 'neutral', ended: 'neutral',
})

/** A run in words: its state, or how it ended. */
export function runLabel(run) {
  if (!run) return ''
  if (run.state === 'ended') return RUN_END_LABEL[run.end_status] || 'Ended'
  return RUN_STATE_LABEL[run.state] || 'Unknown'
}

/** A task's run that is still live (proposed or running), or null. */
export function liveRun(task) {
  const run = task?.run
  return run && (run.state === 'proposed' || run.state === 'started') ? run : null
}

const RUN_OUTCOME = {
  started: 'Started', declined: 'Declined', proposed: 'Still waiting',
  run_not_found: 'Not found on this project', already_decided: 'Already decided by someone else',
  mode_track: 'The project is in Track now, so a person starts it',
  agent_not_on_project: 'Its agent is no longer on the project',
  origin_cannot_view: "You can't see this project's work for that agent",
  agent_consent_predates_runs: "Its agent's owner hasn't approved runs yet",
  dispatch_failed: "Couldn't start right now; try again",
  task_waiting: 'The task waits on another task', task_blocked: 'The task is blocked',
  task_done: 'The task is done', task_awaiting_check: 'The task is waiting for its check',
  archived: 'The project is archived',
}

/** One id's outcome from Approve / Decline, in words — every code has a line. */
export function runOutcomeLabel(outcome) {
  if (!outcome) return ''
  return RUN_OUTCOME[outcome] || CODE_MESSAGE[outcome] || `Not done (${outcome})`
}

/** Why a task needs a person's decision: the steward's last send-back, or the rule. */
export function needsDecisionReason(task) {
  if (!task?.needs_decision) return ''
  const line = [...(task.log || [])].reverse().find((l) => /needs a decision/i.test(l.body || ''))
  const why = line?.body?.replace(/\s*\(rejected twice — needs a decision\)\s*$/i, '').trim()
  return why
    ? `Rejected twice. The last reason: ${why}`
    : 'Rejected twice. A person decides what happens next.'
}
