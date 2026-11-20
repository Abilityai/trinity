/**
 * Flight Deck (abilityai/trinity-enterprise#1307) — the rules behind the view.
 *
 * The Flight Deck is a read-only picture of the fleet in motion: a page in the
 * app that can also be opened in its own window and left on a second screen.
 * Everything it decides without a DOM is here, so it can be tested without one:
 * who counts as working, the order of what is drawn, what the privacy
 * setting lets through, when the page sleeps and when its data is stale.
 *
 * Inputs are the Dashboard's own store shapes (`stores/network.js`): the
 * visible agents, the socket-observed working map, the polled context stats
 * and the activity list. Nothing here fetches.
 */
import { agentDisplayName } from './agentName'

/** Data older than this, with the page awake, is shown as stale. */
export const STALE_AFTER_MS = 45000

const ms = (t) => {
  const n = t ? new Date(t).getTime() : NaN
  return Number.isFinite(n) ? n : 0
}

/** "5s", "2m", "1h 2m", "1d 1h" — time in a state, never negative. */
export function agoLabel(sec) {
  const s = Math.max(0, Math.round(sec || 0))
  if (s < 60) return `${s}s`
  if (s < 3600) return `${Math.floor(s / 60)}m`
  if (s < 86400) return `${Math.floor(s / 3600)}h ${Math.floor((s % 3600) / 60)}m`
  return `${Math.floor(s / 86400)}d ${Math.floor((s % 86400) / 3600)}h`
}

/**
 * What an agent is doing, in one line. With `showText` off (the default — the
 * screen is meant to be seen by people walking past) only the kind of trigger
 * is returned, never a schedule name or any text a person typed.
 */
export function taskLine(activity, { showText = false } = {}) {
  if (!activity) return ''
  const kind = activity.triggered_by || (activity.activity_type || '').replace(/_(start|end)$/, '') || 'task'
  if (!showText) return kind
  const text = activity.schedule_name || activity.command_preview || ''
  return text ? `${kind} · ${text}` : kind
}

/**
 * The rows the deck draws, in drawing order: working first (longest-running
 * first is wrong for a glance — newest start first), then idle by most recent
 * activity, stopped last. Only agents in `agents` appear: that list is the
 * store's access-scoped, filter-applied set, and a working entry for a name
 * outside it is dropped rather than drawn.
 */
export function deckAgents({ agents = [], workingState = {}, contextStats = {}, executionStats = {}, activities = [], now = Date.now(), showText = false } = {}) {
  const running = new Map()
  for (const a of activities) {
    if (a.status !== 'started' || !a.source_agent) continue
    const prev = running.get(a.source_agent)
    if (!prev || ms(a.timestamp) > ms(prev.timestamp)) running.set(a.source_agent, a)
  }
  const rows = agents.map((agent) => {
    const name = agent.name
    const stats = contextStats[name] || {}
    const ws = workingState[name]
    const exec = executionStats[name] || {}
    const stopped = String(agent.status || '').toLowerCase() !== 'running'
    const working = !stopped && (Boolean(ws) || stats.activityState === 'active')
    const current = working ? running.get(name) : null
    const sinceMs = working
      ? ms(ws?.since) || ms(current?.timestamp) || ms(stats.lastActivityTime)
      : ms(stats.lastActivityTime)
    return {
      name,
      label: agentDisplayName(agent) || name,
      working,
      stopped,
      sinceMs,
      sinceSec: sinceMs ? Math.max(0, Math.round((now - sinceMs) / 1000)) : null,
      task: working ? taskLine(current, { showText }) : '',
      // The agent tile's own facts (24h window), so a row reads like a tile.
      contextPercent: Number.isFinite(stats.contextPercent) ? Math.round(stats.contextPercent) : null,
      runs: exec.taskCount || 0,
      succeeded: exec.successCount || 0,
      failed: exec.failedCount || 0,
      // No runs is "no reading", not 0% — a quiet agent must not look broken.
      successRate: exec.taskCount ? Math.round(exec.successRate ?? 0) : null,
      cost: exec.totalCost || 0,
      autonomous: agent.autonomy_enabled === true,
      system: agent.is_system === true,
      avatarUrl: agent.avatar_url || null,
      runtime: agent.runtime || 'claude-code',
    }
  })
  const rank = (r) => (r.working ? 0 : r.stopped ? 2 : 1)
  return rows.sort((a, b) => rank(a) - rank(b) || b.sinceMs - a.sinceMs || a.name.localeCompare(b.name))
}

export function fleetCounts(rows) {
  const working = rows.filter((r) => r.working).length
  const stopped = rows.filter((r) => r.stopped).length
  return { working, stopped, idle: rows.length - working - stopped, total: rows.length }
}

/** Fleet-wide 24h totals over the rows drawn. `successRate` is null with no runs. */
export function fleetTotals(rows) {
  const sum = (k) => rows.reduce((n, r) => n + (r[k] || 0), 0)
  const runs = sum('runs')
  const judged = sum('succeeded') + sum('failed')
  return {
    runs,
    failed: sum('failed'),
    successRate: judged ? Math.round((sum('succeeded') / judged) * 100) : null,
    cost: sum('cost'),
    autonomous: rows.filter((r) => r.autonomous).length,
  }
}

/** "$0", "$0.42", "$12.30", "$1.2k". */
export function costLabel(n) {
  const v = Number(n) || 0
  if (v >= 1000) return `$${(v / 1000).toFixed(1)}k`
  return v ? `$${v.toFixed(2)}` : '$0'
}

/**
 * The next scheduled runs, soonest first, for visible agents. A schedule's
 * name is task text, so it is returned only when text is allowed.
 */
export function upNext(schedules = [], { names, now = Date.now(), limit = 4, showText = false } = {}) {
  return schedules
    .filter((sc) => sc.enabled !== false && sc.next_run_at && (!names || names.has(sc.agent_name)))
    .map((sc) => ({ key: `${sc.agent_name}:${sc.id ?? sc.name}`, agent: sc.agent_name, name: showText ? sc.name || '' : '', at: ms(sc.next_run_at) }))
    .filter((sc) => sc.at > now)
    .sort((a, b) => a.at - b.at)
    .slice(0, limit)
    .map((sc) => ({ ...sc, inSec: Math.round((sc.at - now) / 1000) }))
}

/**
 * The last events, newest first, for agents the viewer can see. `key` changes
 * when an event changes state, which is what the canvas ripples on.
 */
export function deckFeed(activities = [], { names, limit = 7 } = {}) {
  return activities
    .filter((a) => a.source_agent && (!names || names.has(a.source_agent)))
    .map((a) => {
      const kind = a.status === 'started' ? 'started' : a.status === 'failed' ? 'failed' : 'completed'
      const at = kind === 'started' ? ms(a.timestamp) : ms(a.timestamp) + (a.duration_ms || 0)
      return { key: `${a.activity_id}:${kind}`, agent: a.source_agent, kind, at }
    })
    .sort((a, b) => b.at - a.at)
    .slice(0, limit)
}

/** The page draws and fetches only while it can be seen. */
export function isAwake(visibilityState) {
  return visibilityState === 'visible'
}

/** Stale = a load succeeded once and nothing has succeeded since the limit. */
export function isStale(lastOkAt, now = Date.now()) {
  return lastOkAt != null && now - lastOkAt > STALE_AFTER_MS
}

/** `window.open` features for the separate window: no tabs, no address bar. */
export function popoutFeatures(screen) {
  const w = screen?.availWidth || 1280
  const h = screen?.availHeight || 800
  return `popup=yes,width=${w},height=${h},left=0,top=0`
}
