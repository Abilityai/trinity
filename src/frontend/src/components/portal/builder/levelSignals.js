/**
 * What the levels are worked out from (`levels.js`), read with the person's own session
 * (`http` sends their platform token) through routes the main app already uses.
 *
 * A read that fails keeps what the last good read saw (`previous`), so a passing hiccup
 * never unticks a level; before any good read it counts as not seen, so a level is never
 * reached by mistake. The page says nothing either way: levels are a help.
 *
 * Keys come from `credential-requirements`, which is rate limited, audited and runs in
 * the agent's container, so an agent's keys are read once they are all set only on a new
 * visit, and otherwise only when the caller asks to recheck. "None needed" is rechecked
 * too: keys get declared and set while the person watches. They are also read again when
 * the agent's playbooks change, since a playbook can bring a key with it.
 * A test of an agent's connections is one of its runs (`CONNECTION_TEST_PREFIX`), read in
 * full once for what its reply says (`connectionsVerdict`): Level 3 rests on that run.
 * Schedules and Autonomy are the agent page's own two reads: a schedule that is on, on
 * an agent whose Autonomy is on, is set to run by itself.
 * Playbooks come from the list the agent page's Skills tab shows (`/playbooks`), its last
 * known one when the agent is stopped; null until a read has worked.
 */
const enc = encodeURIComponent
const agentPath = (name) => `/api/agents/${enc(name)}`
const list = (value) => (Array.isArray(value) ? value : [])

const TIMELINE = '/api/activities/timeline?activity_types=agent_collaboration&limit=200'
const APPROVALS = (status) => `/api/operator-queue?type=approval&status=${status}&limit=200`
const CALL_ACTIONS = new Set(['chat', 'parallel_task'])
const SETTLED_KEYS = new Set(['all-set'])
const FAILED = Symbol('failed')

// A test of an agent's connections is a run on that agent whose request starts with these
// words and whose reply ends with one of these lines (the guide's `build-agent` skill sends
// exactly that request). The page reads the run itself: nobody's word ticks Level 3.
export const CONNECTION_TEST_PREFIX = 'Test your connections'
export const CONNECTIONS_OK = 'CONNECTIONS: OK'
export const CONNECTIONS_FAILED = 'CONNECTIONS: FAILED'

/** What a test run's reply says: 'ok', 'failed', or 'unknown' when it says neither. A failed line wins. */
export function connectionsVerdict(reply) {
  const lines = String(reply || '').split('\n').map((l) => l.replace(/[`*_>\s]+/g, ' ').trim().toUpperCase())
  if (lines.includes(CONNECTIONS_FAILED)) return 'failed'
  return lines.includes(CONNECTIONS_OK) ? 'ok' : 'unknown'
}

async function read(http, url) {
  try {
    const { data } = await http.get(url)
    return data
  } catch {
    return FAILED
  }
}

const isTest = (j) => typeof j?.message === 'string' && j.message.trimStart().startsWith(CONNECTION_TEST_PREFIX)
// The newest finished test of its connections among an agent's runs, or null.
function latestTest(rows) {
  const done = list(rows).filter((j) => isTest(j) && j.id && (j.status === 'success' || j.status === 'failed'))
  return done.sort((a, b) => String(b.started_at || '').localeCompare(String(a.started_at || '')))[0] || null
}

const job = (j) => ({
  status: j?.status,
  triggered_by: j?.triggered_by,
  source_agent_name: j?.source_agent_name || null,
  started_at: j?.started_at || null,
  completed_at: j?.completed_at || null,
})

/** An agent's keys, from its credential checklist: all set, missing, not needed, or unknown. */
function keysFrom(report) {
  const state = report?.state
  if (state === 'no_credentials_required') return 'needs-none'
  if (state !== 'ok' && state !== 'declaration_incomplete') return 'unknown'
  const summary = report.summary || {}
  if (Number(summary.blocking) > 0) return 'missing'
  // A key the agent declared is one it needs unless it is marked optional: Trinity calls
  // an unmarked one "unknown" and doesn't count it as blocking. Advisory rows were only
  // seen in its files, never declared.
  const rows = Array.isArray(report.requirements) ? report.requirements : []
  if (rows.some((r) => r && !r.advisory && r.required !== false && r.status === 'missing')) return 'missing'
  return Number(summary.set) > 0 ? 'all-set' : 'needs-none'
}

function detailsOf(activity) {
  const d = activity?.details
  if (d && typeof d === 'object') return d
  if (typeof d !== 'string') return {}
  try {
    const parsed = JSON.parse(d)
    return parsed && typeof parsed === 'object' ? parsed : {}
  } catch {
    return {}
  }
}

function callsFrom(timeline) {
  const rows = Array.isArray(timeline) ? timeline : list(timeline?.activities)
  return rows.flatMap((a) => {
    const d = detailsOf(a)
    if (a?.activity_state !== 'completed' || !CALL_ACTIONS.has(d.action) || !d.target_agent) return []
    return [{ from: d.source_agent || a.agent_name, to: d.target_agent, at: a.started_at || a.timestamp || null }]
  })
}

function answeredCount(pages, own, email) {
  const mine = new Set(own)
  const who = String(email || '').toLowerCase()
  return pages.flatMap((page) => list(page?.items))
    .filter((item) => mine.has(item?.agent_name) && (!who || String(item.responded_by_email || '').toLowerCase() === who))
    .length
}

function playbooksChanged(before, now) {
  if (!Array.isArray(before) || !Array.isArray(now)) return false
  return before.length !== now.length || now.some((name) => !before.includes(name))
}

/**
 * Everything `levelsFrom` needs, for the person's own agents. `previous` is the last
 * result, kept wherever a read fails; `recheckKeys` reads keys that aren't settled yet.
 * Never throws.
 */
export async function readLevelSignals(http, { own = [], email = '', previous = null, recheckKeys = false } = {}) {
  const agents = list(own)
  const before = previous && typeof previous === 'object' ? previous : {}
  const signals = { own: agents, jobs: {}, shares: {}, permissions: {}, keys: {}, playbooks: {}, scheduled: {}, autonomy: {}, tested: {}, calls: [], answered: 0 }
  const or = (value, part, name, parse, empty) => (value === FAILED ? (before[part]?.[name] ?? empty) : parse(value))
  const keysReadNow = new Set()

  const [timeline, responded, acknowledged] = await Promise.all([
    read(http, TIMELINE),
    read(http, APPROVALS('responded')),
    read(http, APPROVALS('acknowledged')),
    ...agents.map(async (name) => {
      const base = agentPath(name)
      const knownKeys = before.keys?.[name]
      const readKeys = !SETTLED_KEYS.has(knownKeys) && (recheckKeys || knownKeys === undefined)
      if (readKeys) keysReadNow.add(name)
      const [executions, shares, permissions, keys, playbooks, schedules, autonomy] = await Promise.all([
        read(http, `${base}/executions?limit=100`),
        read(http, `${base}/shares`),
        read(http, `${base}/permissions`),
        readKeys ? read(http, `${base}/credential-requirements`) : null,
        read(http, `${base}/playbooks?last_known=true`),
        read(http, `${base}/schedules`),
        read(http, `${base}/autonomy`),
      ])
      signals.jobs[name] = or(executions, 'jobs', name, (v) => list(v).map(job), [])
      // The newest test of its connections, and what its reply said. The list carries no
      // reply, so that one run is read in full, once: the same run is not read again.
      const known = before.tested?.[name] || null
      const test = executions === FAILED ? null : latestTest(executions)
      if (executions === FAILED || (test && known?.id === test.id)) signals.tested[name] = known
      else if (!test) signals.tested[name] = null
      else {
        const full = test.status === 'success' ? await read(http, `${base}/executions/${enc(test.id)}`) : null
        // A reply that could not be read is tried again next time, and counts for nothing meanwhile.
        signals.tested[name] = full === FAILED ? known : { id: test.id, ok: connectionsVerdict(full?.response) === 'ok' }
      }
      signals.shares[name] = or(shares, 'shares', name, (v) => list(v).map((s) => String(s?.shared_with_email || '').toLowerCase()).filter(Boolean), [])
      signals.permissions[name] = or(permissions, 'permissions', name, (v) => list(v?.permitted_agents).map((a) => a?.name).filter(Boolean), [])
      signals.keys[name] = readKeys ? or(keys, 'keys', name, keysFrom, 'unknown') : (knownKeys ?? 'unknown')
      signals.scheduled[name] = or(schedules, 'scheduled', name, (v) => list(v).some((s) => s?.enabled === true || s?.enabled === 1), false)
      signals.autonomy[name] = or(autonomy, 'autonomy', name, (v) => v?.autonomy_enabled === true, false)
      signals.playbooks[name] = or(playbooks, 'playbooks', name, (v) => list(v?.skills).map((p) => p?.name).filter((n) => typeof n === 'string' && n), null)
    }),
  ])
  await Promise.all(agents.filter((name) => !keysReadNow.has(name) && playbooksChanged(before.playbooks?.[name], signals.playbooks[name]) && signals.keys[name] === 'needs-none')
    .map(async (name) => {
      const keys = await read(http, `${agentPath(name)}/credential-requirements`)
      // A read that fails is tried again at the next recheck.
      signals.keys[name] = keys === FAILED ? 'unknown' : keysFrom(keys)
    }))
  signals.calls = timeline === FAILED ? list(before.calls) : callsFrom(timeline)
  const pagesFailed = responded === FAILED || acknowledged === FAILED
  const counted = answeredCount([responded, acknowledged].filter((p) => p !== FAILED), agents, email)
  signals.answered = pagesFailed && Number.isFinite(before.answered) ? before.answered : counted
  return signals
}
