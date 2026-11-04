/**
 * What the levels are worked out from (`levels.js`), read with the person's own session
 * (`http` sends their platform token) through routes the main app already uses.
 *
 * A read that fails keeps what the last good read saw (`previous`), so a passing hiccup
 * never unticks a level; before any good read it counts as not seen, so a level is never
 * reached by mistake. The page says nothing either way: levels are a help.
 *
 * Keys come from `credential-requirements`, which is rate limited, audited and runs in
 * the agent's container, so an agent's keys are read once they are settled (all set, or
 * none needed) only on a new visit, and otherwise only when the caller asks to recheck.
 * Playbooks come from the list the agent page's Skills tab shows (`/playbooks`), its last
 * known one when the agent is stopped; null until a read has worked.
 */
const enc = encodeURIComponent
const agentPath = (name) => `/api/agents/${enc(name)}`
const list = (value) => (Array.isArray(value) ? value : [])

const TIMELINE = '/api/activities/timeline?activity_types=agent_collaboration&limit=200'
const APPROVALS = (status) => `/api/operator-queue?type=approval&status=${status}&limit=200`
const CALL_ACTIONS = new Set(['chat', 'parallel_task'])
const SETTLED_KEYS = new Set(['all-set', 'needs-none'])
const FAILED = Symbol('failed')

async function read(http, url) {
  try {
    const { data } = await http.get(url)
    return data
  } catch {
    return FAILED
  }
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

/**
 * Everything `levelsFrom` needs, for the person's own agents. `previous` is the last
 * result, kept wherever a read fails; `recheckKeys` reads keys that aren't settled yet.
 * Never throws.
 */
export async function readLevelSignals(http, { own = [], email = '', previous = null, recheckKeys = false } = {}) {
  const agents = list(own)
  const before = previous && typeof previous === 'object' ? previous : {}
  const signals = { own: agents, jobs: {}, shares: {}, permissions: {}, keys: {}, playbooks: {}, calls: [], answered: 0 }
  const or = (value, part, name, parse, empty) => (value === FAILED ? (before[part]?.[name] ?? empty) : parse(value))

  const [timeline, responded, acknowledged] = await Promise.all([
    read(http, TIMELINE),
    read(http, APPROVALS('responded')),
    read(http, APPROVALS('acknowledged')),
    ...agents.map(async (name) => {
      const base = agentPath(name)
      const knownKeys = before.keys?.[name]
      const readKeys = !SETTLED_KEYS.has(knownKeys) && (recheckKeys || knownKeys === undefined)
      const [executions, shares, permissions, keys, playbooks] = await Promise.all([
        read(http, `${base}/executions?limit=100`),
        read(http, `${base}/shares`),
        read(http, `${base}/permissions`),
        readKeys ? read(http, `${base}/credential-requirements`) : null,
        read(http, `${base}/playbooks?last_known=true`),
      ])
      signals.jobs[name] = or(executions, 'jobs', name, (v) => list(v).map(job), [])
      signals.shares[name] = or(shares, 'shares', name, (v) => list(v).map((s) => String(s?.shared_with_email || '').toLowerCase()).filter(Boolean), [])
      signals.permissions[name] = or(permissions, 'permissions', name, (v) => list(v?.permitted_agents).map((a) => a?.name).filter(Boolean), [])
      signals.keys[name] = readKeys ? or(keys, 'keys', name, keysFrom, 'unknown') : (knownKeys ?? 'unknown')
      signals.playbooks[name] = or(playbooks, 'playbooks', name, (v) => list(v?.skills).map((p) => p?.name).filter((n) => typeof n === 'string' && n), null)
    }),
  ])
  signals.calls = timeline === FAILED ? list(before.calls) : callsFrom(timeline)
  const pagesFailed = responded === FAILED || acknowledged === FAILED
  const counted = answeredCount([responded, acknowledged].filter((p) => p !== FAILED), agents, email)
  signals.answered = pagesFailed && Number.isFinite(before.answered) ? before.answered : counted
  return signals
}
