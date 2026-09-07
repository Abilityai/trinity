/**
 * Workspace work — the live execution card and the rail's Work tab, every
 * decidable rule (trinity-enterprise#525, the visual half of ent#457). Pure.
 *
 * The user-facing noun is **work** (accepted 2026-09-02): "what you asked for
 * is running". `execution` stays the ledger's word on operator surfaces.
 *
 * What is ruled and restated here:
 *
 *   * the card is honest about how a job ended — failed, timed out, cancelled,
 *     lost sight of — and its lesser control is **Ask about it** (a prefill,
 *     never a send), because a step-level restart is a platform capability
 *     Trinity does not have (#919 territory; ruling 1, 2026-09-02);
 *   * an agent that publishes no steps SAYS so — "this agent doesn't report
 *     steps" — and an agent whose steps could not be read says THAT, which is
 *     a different sentence (ruling 2, as reviewed: three states, not two);
 *   * the Work signal is derived on every render from ONE merged set — the
 *     feed's running rows plus the conversation's in-flight turn, joined by
 *     execution id — never the sum of two signals (review A1);
 *   * *Earlier* is bounded and says so: "N in the last 30 days · latest 3
 *     shown", and "30+" when the page is full (principle 28).
 *
 * Every rule is a pure function because `vitest.config.js` pins
 * `environment: 'node'` with no component-mount harness. `PortalWorkCard.vue`,
 * `PortalWork.vue` and the two conversations are dispatchers over this module.
 */

import { parseUTC } from '@/utils/timestamps'
import { viewState } from '@/utils/loadingState'

// ---------------------------------------------------------------- vocabulary

/** Statuses that mean "still going" — mirrors the server's `IN_FLIGHT`. */
export const LIVE_STATUSES = Object.freeze(['running', 'queued', 'pending_retry'])

/** How many of *Earlier* show before "Show all" (design pass: latest 3). */
export const EARLIER_PREVIEW = 3

/** Poll only while something is live (the loops-store precedent). */
export const WORK_POLL_MS = 12000

/** The kind words, in the client's vocabulary. */
const KIND_LABELS = Object.freeze({
  turn: 'You asked',
  delegated: 'Handed on',
  loop: 'Loop run',
  schedule: 'Scheduled',
  room: 'Room turn',
  other: 'Background',
})

export function kindLabel(kind) {
  return KIND_LABELS[kind] || KIND_LABELS.other
}

/** "Still going" — the server's `stale` verdict wins over the status word. */
export function isLive(item) {
  if (!item || item.stale === true) return false
  return LIVE_STATUSES.includes(item.status)
}

export function liveItems(items) {
  return (Array.isArray(items) ? items : []).filter(isLive)
}

/**
 * The status word a person reads. Honest about WHY it ended (the
 * `loopStatusLabel` rule, applied to executions): a timeout, a cancel and a
 * failure are three situations with three next actions.
 */
export function workStatusLabel(item) {
  const outcome = item && item.outcome
  switch (outcome) {
    case 'queued': return 'Waiting for a slot'
    case 'running': return 'Working'
    case 'success': return 'Done'
    case 'failed': return 'Failed'
    case 'timeout': return 'Timed out'
    case 'cancelled': return 'Stopped by you'
    case 'skipped': return 'Skipped'
    case 'lost': return 'No longer tracked'
    default: return outcome ? String(outcome) : 'Unknown'
  }
}

/** Severity for the status word. Never a colour name — the SFC owns tokens. */
export function workTone(item) {
  const outcome = item && item.outcome
  if (outcome === 'running' || outcome === 'queued') return 'active'
  if (outcome === 'success') return 'ok'
  if (outcome === 'failed' || outcome === 'timeout') return 'danger'
  if (outcome === 'cancelled' || outcome === 'lost' || outcome === 'skipped') return 'warn'
  return 'neutral'
}

/** A terminal outcome the card must keep showing (AC 3) — not success, which IS the reply. */
export function isHonestTerminal(outcome) {
  return ['failed', 'timeout', 'cancelled', 'lost'].includes(outcome)
}

// ---------------------------------------------------------------- clocks

/** `4s` · `2m 05s` · `1h 12m` — tabular, never negative. */
export function formatElapsed(seconds) {
  if (!Number.isFinite(seconds) || seconds < 0) return null
  const s = Math.floor(seconds)
  if (s < 60) return `${s}s`
  const m = Math.floor(s / 60)
  if (m < 60) return `${m}m ${String(s % 60).padStart(2, '0')}s`
  const h = Math.floor(m / 60)
  return `${h}h ${String(m % 60).padStart(2, '0')}m`
}

/**
 * Seconds a live item has been going, advanced from the SERVER's reading:
 * `elapsed_seconds` was measured at `fetchedAtMs`, and the client only adds
 * the time since. A row with no reading falls back to `started_at` (still the
 * server's clock, parsed as an instant — Invariant #16); a terminal or stale
 * item has no clock at all.
 */
export function liveElapsedSeconds(item, { fetchedAtMs = null, nowMs = Date.now() } = {}) {
  if (!isLive(item)) return null
  if (Number.isFinite(item.elapsed_seconds) && Number.isFinite(fetchedAtMs)) {
    return Math.max(0, item.elapsed_seconds + Math.floor((nowMs - fetchedAtMs) / 1000))
  }
  if (typeof item.started_at === 'string' && item.started_at.trim()) {
    const ms = parseUTC(item.started_at.trim()).getTime()
    if (Number.isFinite(ms)) return Math.max(0, Math.floor((nowMs - ms) / 1000))
  }
  return null
}

// ---------------------------------------------------------------- steps

/**
 * The steps sentence for a card — three states, three sentences (ruling 2,
 * reviewed). Returns `{ kind, text }`: `stages` renders the list, the other
 * two render the sentence in tertiary ink.
 */
export function stepsLine(steps, agentName = null) {
  const who = agentName || 'This agent'
  // `undefined` = not read yet (the chat's card before the feed has the row):
  // say nothing rather than "could not be read", which is a different claim.
  if (steps === undefined) return { kind: 'pending', text: '' }
  if (!steps || typeof steps !== 'object' || steps.state === 'unknown') {
    return { kind: 'unknown', text: 'Steps could not be read right now.' }
  }
  if (steps.state === 'none') {
    return { kind: 'none', text: `${who} doesn't report steps.` }
  }
  if (steps.state === 'reported') {
    if (Array.isArray(steps.stages) && steps.stages.length) return { kind: 'stages', text: '' }
    return { kind: 'none', text: `${who} doesn't report steps.` }
  }
  return { kind: 'unknown', text: 'Steps could not be read right now.' }
}

/** The stages, each with the name a person reads and who holds it (masked names read "another agent"). */
export function stageRows(steps) {
  const stages = steps && Array.isArray(steps.stages) ? steps.stages : []
  return stages.map((s) => ({
    id: s.id,
    name: s.name || s.id,
    state: ['done', 'current', 'pending'].includes(s.state) ? s.state : 'pending',
    holder: typeof s.holder === 'string' && s.holder ? s.holder : null,
  }))
}

/**
 * "held by scout" / "held by another agent" — for the current stage, or for a
 * delegated child. `null` when there is nothing to say (a single-agent job
 * held by the agent you are talking to).
 */
export function holderLine(holder, { agentName = null, masked = false } = {}) {
  if (masked) return 'held by another agent'
  if (!holder || holder === agentName) return null
  return `held by ${holder}`
}

// ---------------------------------------------------------------- chat scoping

/**
 * The children of ONE chat's turn: in-flight items bound to `chatId` other
 * than the turn itself. This is what "which agent holds it now" renders in a
 * 1:1 — a delegated child lives on the delegate, and the server found it by
 * the chat, not by the participant (review A2).
 */
export function childrenForChat(items, chatId, excludeId = null) {
  if (!chatId) return []
  return liveItems(items).filter((it) => it.chat_id === chatId && it.id !== excludeId)
}

/** The feed's row for the turn on screen, by execution id — never "latest running" (review E5). */
export function itemById(items, id) {
  if (!id) return null
  return (Array.isArray(items) ? items : []).find((it) => it && it.id === id) || null
}

// ---------------------------------------------------------------- the signal

/**
 * The Work signal from ONE merged set. `emit` is the conversation's in-flight
 * turn (`{ executionId, agent }`) — a synchronous turn has a row before the
 * feed has read it, so it joins the set; but joined BY ID, so once the feed
 * has the row it counts once. A room contributes its server `working` list
 * as `extraAgents` the same way (agents, not ids: a room turn's row is found
 * by the feed under the participant's name, so a name already live in the
 * feed is not counted twice).
 */
export function workSignalFromItems(items, { emit = null, extraAgents = [] } = {}) {
  const live = liveItems(items)
  const ids = new Set(live.map((it) => it.id))
  const agents = live.map((it) => it.agent_name).filter((a) => typeof a === 'string' && a)
  if (emit && emit.executionId && !ids.has(emit.executionId)) {
    ids.add(emit.executionId)
    if (emit.agent) agents.push(emit.agent)
  } else if (emit && !emit.executionId && emit.agent && !agents.includes(emit.agent)) {
    // A turn that fell back to the synchronous send has no id: it is live, once.
    ids.add(`emit:${emit.agent}`)
    agents.push(emit.agent)
  }
  for (const a of Array.isArray(extraAgents) ? extraAgents : []) {
    if (typeof a === 'string' && a && !agents.includes(a)) { ids.add(`room:${a}`); agents.push(a) }
  }
  return { live: ids.size, updated: false, agents }
}

// ---------------------------------------------------------------- earlier

/**
 * "N in the last 30 days · latest 3 shown" — and "30+" when the server's page
 * is full, because a bounded page must never pose as the total (principle 28).
 */
export function earlierSummary({ total = 0, shown = 0, limit = EARLIER_PREVIEW, windowDays = 30, pageLimit = null } = {}) {
  const n = Number.isFinite(total) ? Math.max(0, total) : 0
  const full = Number.isFinite(pageLimit) && pageLimit > 0 && n >= pageLimit
  const count = full && n === pageLimit ? `${pageLimit}+` : String(n)
  const head = `${count} in the last ${windowDays} days`
  if (n === 0) return head
  const visible = Math.min(shown, n)
  return visible < n ? `${head} · latest ${visible} shown` : head
}

/** The preview slice, or everything once expanded. */
export function earlierSlice(items, expanded) {
  const list = Array.isArray(items) ? items : []
  return expanded ? list : list.slice(0, EARLIER_PREVIEW)
}

// ---------------------------------------------------------------- the tab's verdict

/** The Work body reads a VERDICT (#1927): loading while a room's participants have not landed. */
export function workView({ participants = [], hasLoaded = false, error = null, count = 0 } = {}) {
  const list = (Array.isArray(participants) ? participants : []).filter((p) => typeof p === 'string' && p.trim())
  if (!list.length) return { state: 'loading', stale: false }
  return viewState({ hasLoaded, error, count })
}

// ---------------------------------------------------------------- ask about it

/**
 * The composer text **Ask about it** pre-fills — never sent by the card. Names
 * the job so the agent can find it; a room addresses the agent first.
 */
export function askAboutItPrefill(item, { participants = [] } = {}) {
  const title = item && typeof item.title === 'string' ? item.title.trim() : ''
  const what = title ? `"${title.length > 60 ? `${title.slice(0, 59).trimEnd()}…` : title}"` : 'the last job'
  const ended = workStatusLabel(item).toLowerCase()
  const body = `What happened with ${what}? It shows as ${ended} — what got done, and what would you need from me to finish it?`
  const list = (Array.isArray(participants) ? participants : []).filter(Boolean)
  const target = item && item.agent_name
  return list.length > 1 && target ? `${target}, ${body.charAt(0).toLowerCase()}${body.slice(1)}` : body
}
