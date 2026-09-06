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
 * The registry. v1 of the shell docks ONE tab — Work (#457's Activity) — and
 * docks it empty by the operator's own split: the shell is the frame the other
 * slices dock into, and the Work tab's content lands with #457.
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
        const list = participantList(participants)
        const who = list.length === 1 ? list[0] : 'an agent in this room'
        return `When ${who} takes on a longer job from this chat — a report, a research pass — it shows up here step by step, and reports back to the chat when it's done.`
      },
      action: 'See what you can ask',
      event: 'see-hints',
    }),
  }),
])

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

// ---------------------------------------------------------------- internals

function participantList(value) {
  return (Array.isArray(value) ? value : [])
    .filter((v) => typeof v === 'string' && v.trim().length > 0)
    .map((v) => v.trim())
}
