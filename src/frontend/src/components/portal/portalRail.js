/**
 * Workspace conversation rail — the tab contract and every decidable rule
 * (trinity-enterprise#474, slice 1 of #472). Pure.
 *
 * The rail is the collapsible third column beside the conversation. It invents
 * no rules of its own; it collects ones already ruled on #472 and the approved
 * design pass (ent#474, 2026-09-06):
 *
 *   * a TAB declares its label, its DOOR, its participant SCOPING, its EMPTY
 *     STATE and its activity SIGNAL — nothing docks into the rail without all
 *     five, which is what stops each capability re-deciding its own placement;
 *   * the rail renders ONLY tabs whose door the current session passes, and a
 *     failed door means the tab's body is never mounted — so nothing it would
 *     fetch is ever requested (`visibleTabs` is the one gate, for render AND
 *     for mount);
 *   * collapsed by default (operator ruling 2026-09-02); open/collapsed and the
 *     active tab persist across chat switches under ONE localStorage key;
 *   * a collapsed rail still says which tabs have live activity, in two
 *     shapes and one hue (principle 24): "live now" (a ringed, pulsing dot) and
 *     "updated since last view" (a plain dot);
 *   * a room shows ONE rail; every tab groups its content by participating
 *     agent, and an agent with nothing in flight still gets its row.
 *
 * Every rule here is a pure function because `vitest.config.js` pins
 * `environment: 'node'` with no component-mount harness — a rule inside an SFC
 * is a rule no test can reach. `PortalRail.vue` and `PortalRailStrip.vue` are
 * dispatchers over this module.
 */

import { byAgent } from './portalLoopUtils'
import { parseUTC } from '@/utils/timestamps'
import { viewState } from '@/utils/loadingState'

// ---------------------------------------------------------------- vocabulary

/** Which session may see a tab (#472 rule 2; ent#78's auth-path invariant). */
export const RAIL_DOORS = Object.freeze({
  /** platform-authenticated sessions only — never an external client */
  PLATFORM: 'platform',
  /** rendered for every session; the SERVER narrows the data by audience */
  AUDIENCE: 'audience',
  /** per-agent scoping — needs at least one participant to have anything to show */
  AGENT: 'agent',
})

/** A tab's participant scoping. v1 has exactly one: the chat's participants. */
export const RAIL_SCOPE_PARTICIPANTS = 'participants'

/** The two signal shapes a tab can carry. `live` outranks `updated`. */
export const RAIL_SIGNAL_LIVE = 'live'
export const RAIL_SIGNAL_UPDATED = 'updated'

/** The one persisted key (design pass, "State & honesty"). */
export const RAIL_STORAGE_KEY = 'trinity-workspace-rail'
export const RAIL_DEFAULT_TAB = 'work'

/**
 * The fixed order every rail renders in (design pass, "Tab contract"): State
 * docks later, per #472. Loops, Canvas and Files are slice 2 — they are named
 * here so the order is a contract and not a side effect of registration order.
 */
export const RAIL_TAB_ORDER = Object.freeze(['work', 'loops', 'canvas', 'files'])

/**
 * The registry. Slice 1 (ent#474) docked ONE tab — Work (#457's Activity),
 * empty by the operator's own split. Slice 2 (ent#475) docks the three the
 * order already named: Loops (ent#458's panel), Canvas (ent#438's canvas) and
 * Files (the drawer). Each brings its body as a `#tab-<id>` slot of
 * `PortalRail`; the `empty` entry here is the contract every tab must state
 * even when its body renders its own copy, so the registry stays honest.
 *
 * `empty.body(participants)` is a function because the copy names the agent in
 * a 1:1 and "an agent in this room" otherwise; `empty.action` is the next step
 * it teaches (principle 16), emitted by the rail as an event the shell handles.
 */
export const RAIL_TABS = Object.freeze([
  Object.freeze({
    id: 'work',
    label: 'Work',
    door: RAIL_DOORS.PLATFORM,
    scope: RAIL_SCOPE_PARTICIPANTS,
    signal: RAIL_SIGNAL_LIVE,
    icon: 'bolt',
    empty: Object.freeze({
      title: 'Nothing running right now',
      body: (participants) => {
        const who = whoIs(participants)
        return `When ${who} takes on a longer job from this chat — a report, a research pass — it shows up here step by step, and reports back to the chat when it's done.`
      },
      action: 'See what you can ask',
      event: 'see-hints',
    }),
  }),
  // ent#475 — platform-only (ent#78's auth-path invariant, restated by
  // ent#458): the body calls the operator loop endpoints with the platform
  // JWT, and the shell feeds `stores/portalLoops.js` only when this tab
  // passes its door.
  Object.freeze({
    id: 'loops',
    label: 'Loops',
    door: RAIL_DOORS.PLATFORM,
    scope: RAIL_SCOPE_PARTICIPANTS,
    signal: RAIL_SIGNAL_LIVE,
    icon: 'refresh',
    empty: Object.freeze({
      title: 'No loops running',
      body: (participants) => {
        const who = whoIs(participants)
        return `A loop runs the same instruction several times in a row — working through a list, retrying until something succeeds, refining a draft. Start one on ${who} from here; it stops at its run limit, and you can stop it any time.`
      },
      action: 'Start a loop',
      event: 'start-loop',
    }),
  }),
  // ent#475 — rendered for every session; the SERVER narrows to the canvases
  // the agent published to the people it works with (ent#438: the Workspace
  // reads `audience='roster'` for every principal).
  Object.freeze({
    id: 'canvas',
    label: 'Canvas',
    door: RAIL_DOORS.AUDIENCE,
    scope: RAIL_SCOPE_PARTICIPANTS,
    signal: RAIL_SIGNAL_UPDATED,
    icon: 'template',
    empty: Object.freeze({
      title: 'No canvas yet',
      body: (participants) => {
        const who = whoIs(participants)
        return `A canvas is a surface ${who} keeps current — a status board, a running tally, the latest version of an analysis. Ask for one in the chat and it appears here.`
      },
      action: 'Ask for a canvas',
      event: 'ask-canvas',
    }),
  }),
  // ent#475 — per-agent inbox scoping (2026-08-07): the same client-portal
  // routes the drawer used, so who may send to and download from an agent is
  // unchanged.
  Object.freeze({
    id: 'files',
    label: 'Files',
    door: RAIL_DOORS.AGENT,
    scope: RAIL_SCOPE_PARTICIPANTS,
    signal: RAIL_SIGNAL_UPDATED,
    icon: 'paperclip',
    empty: Object.freeze({
      title: 'No files yet',
      body: (participants) => {
        const who = whoIs(participants)
        return `Send ${who} a file to work from, or download what it shares back with you.`
      },
      action: 'Send a file',
      event: 'send-file',
    }),
  }),
])

/** "scout" in a 1:1, "an agent in this room" otherwise — one rule for every empty copy. */
function whoIs(participants) {
  const list = participantList(participants)
  return list.length === 1 ? list[0] : 'an agent in this room'
}

// ---------------------------------------------------------------- doors

/**
 * Does this session pass the tab's door?
 *
 * An UNKNOWN door fails closed: a tab registered with a door this bundle has
 * never heard of is a tab whose audience nobody has decided, and the rail must
 * not decide it by rendering.
 */
export function tabPassesDoor(tab, session = {}) {
  const participants = participantList(session.participants)
  switch (tab && tab.door) {
    case RAIL_DOORS.PLATFORM: return session.isPlatform === true
    case RAIL_DOORS.AUDIENCE: return true
    case RAIL_DOORS.AGENT: return participants.length > 0
    default: return false
  }
}

/**
 * The tabs this session may see, in the fixed rail order.
 *
 * THE gate. The rail renders these and mounts a body for exactly one of them;
 * a tab absent from this list has no strip icon, no label, and — because its
 * body is what owns any fetch — no request. That is the per-door test #474
 * asks for: an external-client session never receives a platform-only tab's
 * data because the code that would ask for it never runs.
 */
export function visibleTabs(tabs, session = {}) {
  const list = Array.isArray(tabs) ? tabs : []
  return list
    .filter((t) => t && typeof t.id === 'string' && tabPassesDoor(t, session))
    .sort((a, b) => orderIndex(a.id) - orderIndex(b.id))
}

function orderIndex(id) {
  const i = RAIL_TAB_ORDER.indexOf(id)
  return i === -1 ? RAIL_TAB_ORDER.length : i
}

// ---------------------------------------------------------------- state

/** Collapsed, on Work (operator ruling 2026-09-02). */
export function defaultRailState() {
  return { open: false, tab: RAIL_DEFAULT_TAB }
}

/**
 * Coerce whatever localStorage held into a valid state.
 *
 * A persisted tab that is no longer registered (a bundle rolled back, a tab
 * retired) falls back to the default rather than leaving the rail open on a
 * tab that does not exist; `open` is read as a strict boolean so a stale
 * `"true"` string from a hand edit reads as collapsed, the safe default.
 */
export function normalizeRailState(raw, tabs = RAIL_TABS) {
  const fallback = defaultRailState()
  if (!raw || typeof raw !== 'object') return fallback
  const known = new Set((Array.isArray(tabs) ? tabs : []).map((t) => t && t.id))
  return {
    open: raw.open === true,
    tab: typeof raw.tab === 'string' && known.has(raw.tab) ? raw.tab : fallback.tab,
  }
}

/** Read the persisted state; any failure (private mode, bad JSON) is the default. */
export function loadRailState(storage, tabs = RAIL_TABS) {
  try {
    const raw = storage && typeof storage.getItem === 'function'
      ? storage.getItem(RAIL_STORAGE_KEY)
      : null
    return normalizeRailState(raw ? JSON.parse(raw) : null, tabs)
  } catch {
    return defaultRailState()
  }
}

/** Persist the state; a storage that refuses (private mode) is session-only. */
export function saveRailState(storage, state) {
  try {
    if (!storage || typeof storage.setItem !== 'function') return false
    storage.setItem(RAIL_STORAGE_KEY, JSON.stringify(normalizeRailState(state)))
    return true
  } catch {
    return false
  }
}

/**
 * The tab the rail actually shows: the remembered one if this session may see
 * it, else the first it may, else nothing (and a rail with nothing to show
 * renders no chrome at all — an open panel with no tabs is a blank rail).
 */
export function activeTabFor(state, visible) {
  const list = Array.isArray(visible) ? visible : []
  const wanted = state && typeof state.tab === 'string' ? state.tab : null
  const hit = list.find((t) => t && t.id === wanted)
  if (hit) return hit.id
  return list.length ? list[0].id : null
}

// ---------------------------------------------------------------- signals

/** An empty signal — what every tab reads until something reports. */
export function emptySignal() {
  return { live: 0, updated: false, agents: [] }
}

/**
 * Normalize one tab's entry of the signals map, which is keyed by TAB ID
 * (`{ work: { live, updated, agents } }`); a tab's registry `signal` field
 * names the SHAPE it normally carries and is documentation, not a key.
 * Signals are DERIVED from the source that owns them on every render (the
 * conversation's in-flight turn, the room's server-reported `working` list)
 * — never a latched flag — so a stale shape here cannot become a stuck
 * indicator: anything unreadable is "no signal".
 */
export function signalFor(signals, tab) {
  const key = tab && typeof tab === 'object' ? tab.id : tab
  const raw = signals && typeof signals === 'object' && key ? signals[key] : null
  if (!raw || typeof raw !== 'object') return emptySignal()
  const live = Number.isInteger(raw.live) && raw.live > 0 ? raw.live : 0
  const agents = participantList(raw.agents)
  return { live, updated: raw.updated === true, agents }
}

/** Which shape the tab wears: live outranks updated; null = no dot. */
export function signalShape(sig) {
  if (!sig) return null
  if (sig.live > 0) return RAIL_SIGNAL_LIVE
  if (sig.updated) return RAIL_SIGNAL_UPDATED
  return null
}

/** The native-tooltip / accessible text for a tab with its signal. */
export function railTitle(tab, sig) {
  const label = tab && tab.label ? tab.label : ''
  const shape = signalShape(sig)
  if (shape === RAIL_SIGNAL_LIVE) return `${label} · ${sig.live} running`
  if (shape === RAIL_SIGNAL_UPDATED) return `${label} · updated`
  return label
}

/**
 * The collapsed strip's view of the visible tabs. Built from `visible`, never
 * from the signals map's keys, so a signal for a tab this session may not see
 * cannot leak onto the strip as a dot with no tab behind it.
 */
export function collapsedSignals(signals, visible) {
  const list = Array.isArray(visible) ? visible : []
  return list.map((tab) => {
    const sig = signalFor(signals, tab)
    return {
      id: tab.id,
      label: tab.label,
      live: sig.live,
      updated: sig.updated,
      agents: sig.agents,
      shape: signalShape(sig),
      title: railTitle(tab, sig),
    }
  })
}

/** Does any visible tab carry a signal? (The mobile strip's "anything to say".) */
export function hasLiveSignal(signals, visible) {
  return collapsedSignals(signals, visible).some((s) => s.shape !== null)
}

/**
 * The mobile strip's text segments — the same vocabulary as the collapsed
 * column, as words: `● Work · 1 running · ● Canvas updated`. With nothing to
 * report the strip names the tabs instead, so it still says what opens.
 */
export function stripSegments(signals, visible) {
  const signalled = collapsedSignals(signals, visible).filter((s) => s.shape !== null)
  if (signalled.length) {
    return signalled.map((s) => ({
      id: s.id,
      shape: s.shape,
      text: s.shape === RAIL_SIGNAL_LIVE ? `${s.label} · ${s.live} running` : `${s.label} updated`,
    }))
  }
  return (Array.isArray(visible) ? visible : []).map((t) => ({ id: t.id, shape: null, text: t.label }))
}

/**
 * The Work signal for a 1:1 conversation: derived from the in-flight turn on
 * every change, so it clears the instant the turn ends — including a failed
 * one, whose `finally` flips the same flag.
 */
export function workSignalFrom({ sending = false, agent = null } = {}) {
  const on = sending === true
  return { live: on ? 1 : 0, updated: false, agents: on && agent ? [agent] : [] }
}

/**
 * The Work signal for a room: the SERVER's `working` list, which survives a
 * reload and is re-read by the room's own poll — the degrade path #474 asks
 * for (live push falls back to poll, never to a stuck indicator).
 */
export function workSignalFromRoom(workingAgents) {
  const agents = participantList(workingAgents)
  return { live: agents.length, updated: false, agents }
}

// ---------------------------------------------------------------- room grouping

/**
 * Group a tab's items by participating agent, in participant order, with a
 * row for EVERY participant even when it has nothing — absence is visible
 * (design pass, "Room"). Items naming a non-participant are dropped: a tab is
 * scoped to the chat's participants, and an item from an agent outside the
 * room is not this room's business.
 *
 * Reuses `portalLoopUtils.byAgent`, which already computes exactly this for
 * the loops strip (`PortalLoops` groups by participant the same way) — one
 * grouping rule, not two.
 */
export function groupByParticipant(items, participants) {
  const list = participantList(participants)
  const grouped = byAgent(Array.isArray(items) ? items : [], list)
  return list.map((agent) => [agent, grouped.get(agent) || []])
}

/**
 * A participant's right-hand state on its room row: how many of the tab's
 * live things it holds, or "nothing in flight".
 */
export function participantState(agent, sig) {
  const s = sig || emptySignal()
  const live = s.agents.filter((a) => a === agent).length
  return live > 0
    ? { live, label: `${live} running` }
    : { live: 0, label: 'nothing in flight' }
}

/** Copy for a tab's empty state, with the participant list folded in. */
export function railEmptyCopy(tab, participants) {
  const empty = tab && tab.empty ? tab.empty : {}
  const body = typeof empty.body === 'function' ? empty.body(participants) : (empty.body || '')
  return {
    title: empty.title || 'Nothing here yet',
    body,
    action: empty.action || null,
    event: empty.event || null,
  }
}

// ---------------------------------------------------------------- shell placement

/**
 * Whom the rail is scoped to. The agent page is a destination of its own with
 * no conversation beside it, so it has no rail (#474: the rail renders beside
 * `PortalConversation` and `PortalRoom`). A room's participants arrive with
 * the room's own fetch, so this can legitimately be empty for a beat — the
 * rail stays mounted on the ROUTE fact (`railVisibleFor`) and its tabs show
 * their empty states rather than the rail flickering in and out.
 */
export function railParticipantsFor({
  agentPage = null,
  roomId = null,
  roomParticipants = [],
  activeAgent = null,
} = {}) {
  if (agentPage) return []
  if (roomId) return participantList(roomParticipants)
  return activeAgent ? [String(activeAgent)] : []
}

/**
 * Should the rail column exist at all right now? Keyed on facts that are
 * synchronous (the route) or verdicts (the stage state), never on data that is
 * still arriving — so a live update can never remount it.
 *
 *   agentPage     `/workspace/a/:name` — a page, not a conversation
 *   stageState    `stageZone(...).state`: only a `ready` stage holds a
 *                 conversation; loading / failed / empty stages show no rail
 *   roomId        `/workspace/r/:id` — the rail follows the room only where
 *                 the room can render (`roomsAvailable`, #2128)
 *   activeAgent   the 1:1 has an agent to scope to
 *   unreachable   a deep link named an agent this caller cannot reach — that
 *                 refusal card is not a conversation
 */
export function railVisibleFor({
  agentPage = null,
  stageState = 'loading',
  roomId = null,
  roomsAvailable = false,
  activeAgent = null,
  unreachable = false,
} = {}) {
  if (agentPage) return false
  if (stageState !== 'ready') return false
  if (roomId) return roomsAvailable === true
  return Boolean(activeAgent) && !unreachable
}

// ---------------------------------------------------------------- slice 2 (ent#475): feeds, seen markers, openers

/**
 * Which feeds the shell must keep alive for THIS session — read off the
 * visible tab list, never the registry, so the door gate extends from
 * "mount" to "fetch": an external client (Canvas · Files) never causes a
 * loops request, and a session that fails a door never fetches that tab's
 * data.
 */
export function feedsFor(visible) {
  const ids = new Set((Array.isArray(visible) ? visible : []).map((t) => t && t.id))
  return { loops: ids.has('loops'), canvas: ids.has('canvas'), files: ids.has('files') }
}

/** The Loops signal: the store's active loops, derived on every render. */
export function loopsSignalFrom(activeLoops) {
  const agents = (Array.isArray(activeLoops) ? activeLoops : [])
    .map((l) => (l && typeof l.agent_name === 'string' ? l.agent_name.trim() : ''))
    .filter(Boolean)
  return { live: agents.length, updated: false, agents }
}

/**
 * A server timestamp as epoch ms, or null. Never compared as a string:
 * `created_at` / `updated_at` arrive in more than one ISO spelling
 * (Invariant #16), and a client marker written by `toISOString()` is a third.
 * `parseUTC` reads a naive value as UTC, which is what the backend writes.
 */
export function timestampMs(value) {
  if (typeof value !== 'string' || !value.trim()) return null
  const ms = parseUTC(value.trim()).getTime()
  return Number.isFinite(ms) ? ms : null
}

/** The newest parseable `field` across `items`, as the server wrote it, or null. */
export function newestTimestamp(items, field) {
  let best = null
  let bestMs = null
  for (const it of Array.isArray(items) ? items : []) {
    const ms = it && typeof it === 'object' ? timestampMs(it[field]) : null
    if (ms === null) continue
    if (bestMs === null || ms > bestMs) { bestMs = ms; best = it[field].trim() }
  }
  return best
}

/**
 * The "updated since last view" signal for a feed-backed tab (Canvas, Files).
 *
 * Per participant: the newest SERVER timestamp of that agent's items is newer
 * than the agent's seen marker — or nothing was ever seen and the feed is
 * non-empty (there is something you have not looked at). An empty feed never
 * signals, a null timestamp never signals, and the marker is itself the last
 * server timestamp observed (`markSeen`), so clock skew between the browser
 * and the server cannot keep a dot lit after the tab was viewed.
 */
export function updatedSignal({ itemsByAgent = {}, seen = {}, field, participants = [] } = {}) {
  const agents = participantList(participants).filter((agent) => {
    const newest = timestampMs(newestTimestamp(itemsByAgent && itemsByAgent[agent], field))
    if (newest === null) return false
    const mark = timestampMs(seen && seen[agent])
    return mark === null || newest > mark
  })
  return { live: 0, updated: agents.length > 0, agents }
}

/** The second persisted key (ent#475): per-tab, per-agent "last seen" markers. */
export const RAIL_SEEN_STORAGE_KEY = 'trinity-workspace-rail-seen'
export const RAIL_SEEN_TABS = Object.freeze(['canvas', 'files'])

/** `{ canvas: {agent: iso}, files: {agent: iso} }` — unknown tabs, non-string agents and unparseable stamps dropped. */
export function normalizeSeen(raw) {
  const out = {}
  for (const tab of RAIL_SEEN_TABS) {
    out[tab] = {}
    const entry = raw && typeof raw === 'object' && raw[tab] && typeof raw[tab] === 'object' ? raw[tab] : {}
    for (const [agent, ts] of Object.entries(entry)) {
      if (typeof agent !== 'string' || !agent.trim()) continue
      if (timestampMs(ts) === null) continue
      out[tab][agent.trim()] = String(ts).trim()
    }
  }
  return out
}

export function loadSeen(storage) {
  try {
    const raw = storage && typeof storage.getItem === 'function'
      ? storage.getItem(RAIL_SEEN_STORAGE_KEY)
      : null
    return normalizeSeen(raw ? JSON.parse(raw) : null)
  } catch {
    return normalizeSeen(null)
  }
}

export function saveSeen(storage, seen) {
  try {
    if (!storage || typeof storage.setItem !== 'function') return false
    storage.setItem(RAIL_SEEN_STORAGE_KEY, JSON.stringify(normalizeSeen(seen)))
    return true
  } catch {
    return false
  }
}

/**
 * Mark every participant's newest item as seen for `tab`. Returns the SAME
 * object when nothing moved, so a watcher keyed on identity does not loop;
 * a participant with no items keeps whatever marker it had.
 */
export function markSeen(seen, tab, { itemsByAgent = {}, field, participants = [] } = {}) {
  if (!RAIL_SEEN_TABS.includes(tab)) return seen
  const base = normalizeSeen(seen)
  let changed = false
  for (const agent of participantList(participants)) {
    const newest = newestTimestamp(itemsByAgent && itemsByAgent[agent], field)
    if (!newest) continue
    if (base[tab][agent] !== newest) { base[tab][agent] = newest; changed = true }
  }
  return changed ? base : seen
}

/**
 * How a feed-backed tab body reads its store: `viewState` over the verdict —
 * and `loading` while a room's participants have not landed yet, because a
 * body with nobody to show is not "empty", it is "not yet".
 */
export function feedView({ participants = [], hasLoaded = false, error = null, count = 0 } = {}) {
  if (!participantList(participants).length) return { state: 'loading', stale: false }
  return viewState({ hasLoaded, error, count })
}

/**
 * How "open the rail on <tab>" lands on this viewport: the column at and above
 * Tailwind's `sm` (640px), the sheet below it. The mobile plan never persists
 * `open: true` — that is the desktop layout's preference, and a phone tap
 * must not change what the same account sees on a laptop.
 */
export function railOpenPlan({ wide = true } = {}) {
  return wide === true ? { open: true, sheet: false } : { open: null, sheet: true }
}

export const RAIL_WIDE_QUERY = '(min-width: 640px)'

/** `matchMedia` is absent in node and in some embedded webviews: the column wins then. */
export function isWideViewport(win) {
  try {
    if (!win || typeof win.matchMedia !== 'function') return true
    return win.matchMedia(RAIL_WIDE_QUERY).matches === true
  } catch {
    return true
  }
}

/** The composer text "Ask for a canvas" pre-fills — never sent by the rail. */
export function askCanvasPrefill(participants) {
  const list = participantList(participants)
  const ask = 'an you publish a canvas with where things stand right now — a short status board I can keep coming back to?'
  return list.length > 1 ? `${list[0]}, c${ask}` : `C${ask}`
}

// ---------------------------------------------------------------- internals

function participantList(value) {
  return (Array.isArray(value) ? value : [])
    .filter((v) => typeof v === 'string' && v.trim().length > 0)
    .map((v) => v.trim())
}
