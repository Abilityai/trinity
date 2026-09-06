/**
 * trinity-enterprise#474 — the Workspace conversation rail: shell, tab
 * contract, collapsed signal, room grouping, persistence, doors.
 *
 * Two halves, the house shape (`workspaceRoomsGate.spec.js`):
 *
 *   1. the pure rules in `portalRail.js` — the only unit-testable home, since
 *      `vitest.config.js` pins `environment: 'node'` with no mount harness;
 *   2. source-structure guards for the parts no unit test can reach — where
 *      the rail is mounted in the shell (a sibling of <main>, outside every
 *      remount), that the door gate is the ONE gate, that a session with no
 *      visible tab gets no rail chrome at all, that the signal cannot latch.
 *
 * Every guard strips comments first: a comment explaining what not to write
 * necessarily contains the offending string.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'
import { stripComments } from './helpers/stripComments'
import {
  RAIL_DEFAULT_TAB,
  RAIL_DOORS,
  RAIL_SCOPE_PARTICIPANTS,
  RAIL_SIGNAL_LIVE,
  RAIL_SIGNAL_UPDATED,
  RAIL_STORAGE_KEY,
  RAIL_TABS,
  RAIL_TAB_ORDER,
  activeTabFor,
  collapsedSignals,
  defaultRailState,
  emptySignal,
  groupByParticipant,
  hasLiveSignal,
  loadRailState,
  normalizeRailState,
  participantState,
  railEmptyCopy,
  railParticipantsFor,
  railTitle,
  railVisibleFor,
  saveRailState,
  signalFor,
  signalShape,
  stripSegments,
  tabPassesDoor,
  visibleTabs,
  workSignalFrom,
  workSignalFromRoom,
} from '@/components/portal/portalRail'

const src = (rel) => stripComments(readFileSync(fileURLToPath(new URL(`../../src/${rel}`, import.meta.url)), 'utf8'))

const PLATFORM = { isPlatform: true, participants: ['scout'] }
const CLIENT = { isPlatform: false, participants: ['scout'] }

// A synthetic 4-tab registry in the DESIGN's order, to test ordering and doors
// without waiting for slice 2 to register them.
const FOUR = [
  { id: 'files', label: 'Files', door: RAIL_DOORS.AGENT, scope: RAIL_SCOPE_PARTICIPANTS, signal: 'updated', empty: { title: 't' } },
  { id: 'canvas', label: 'Canvas', door: RAIL_DOORS.AUDIENCE, scope: RAIL_SCOPE_PARTICIPANTS, signal: 'updated', empty: { title: 't' } },
  { id: 'loops', label: 'Loops', door: RAIL_DOORS.PLATFORM, scope: RAIL_SCOPE_PARTICIPANTS, signal: 'live', empty: { title: 't' } },
  ...RAIL_TABS,
]

const memStorage = () => {
  const m = new Map()
  return { getItem: (k) => (m.has(k) ? m.get(k) : null), setItem: (k, v) => m.set(k, String(v)), _m: m }
}

// ---------------------------------------------------------------------------
// The contract

describe('ent#474 — the tab contract', () => {
  it('every registered tab declares label, door, scoping, empty state and signal', () => {
    expect(RAIL_TABS.length).toBeGreaterThan(0)
    for (const tab of RAIL_TABS) {
      expect(typeof tab.id).toBe('string')
      expect(typeof tab.label).toBe('string')
      expect(Object.values(RAIL_DOORS)).toContain(tab.door)
      expect(tab.scope).toBe(RAIL_SCOPE_PARTICIPANTS)
      expect([RAIL_SIGNAL_LIVE, RAIL_SIGNAL_UPDATED]).toContain(tab.signal)
      expect(typeof tab.empty.title).toBe('string')
      expect(typeof tab.empty.action).toBe('string')
      expect(RAIL_TAB_ORDER).toContain(tab.id)
    }
  })

  it('docks Work first, platform-only, as the design pass rules', () => {
    expect(RAIL_TAB_ORDER[0]).toBe('work')
    expect(RAIL_DEFAULT_TAB).toBe('work')
    const work = RAIL_TABS.find((t) => t.id === 'work')
    expect(work.door).toBe(RAIL_DOORS.PLATFORM)
    expect(work.signal).toBe(RAIL_SIGNAL_LIVE)
    expect(work.empty.title).toBe('Nothing running right now')
    expect(work.empty.action).toBe('See what you can ask')
  })

  it('keeps the fixed order Work · Loops · Canvas · Files', () => {
    expect(RAIL_TAB_ORDER).toEqual(['work', 'loops', 'canvas', 'files'])
    expect(visibleTabs(FOUR, PLATFORM).map((t) => t.id)).toEqual(['work', 'loops', 'canvas', 'files'])
  })

  it('persists under the one approved key', () => {
    expect(RAIL_STORAGE_KEY).toBe('trinity-workspace-rail')
  })
})

// ---------------------------------------------------------------------------
// Doors — the per-door test

describe('ent#474 — doors', () => {
  it('a platform-only tab needs a platform session, strictly', () => {
    const work = RAIL_TABS.find((t) => t.id === 'work')
    expect(tabPassesDoor(work, PLATFORM)).toBe(true)
    expect(tabPassesDoor(work, CLIENT)).toBe(false)
    // Truthy is not true: an accidental string or 1 must not open the door.
    expect(tabPassesDoor(work, { isPlatform: 'yes' })).toBe(false)
    expect(tabPassesDoor(work, {})).toBe(false)
  })

  it('an audience tab renders for every session (the server narrows the data)', () => {
    expect(tabPassesDoor(FOUR[1], CLIENT)).toBe(true)
    expect(tabPassesDoor(FOUR[1], {})).toBe(true)
  })

  it('an agent-scoped tab needs a participant', () => {
    expect(tabPassesDoor(FOUR[0], { participants: [] })).toBe(false)
    expect(tabPassesDoor(FOUR[0], { participants: ['scout'] })).toBe(true)
  })

  it('an unknown door fails closed', () => {
    expect(tabPassesDoor({ id: 'x', door: 'everyone' }, PLATFORM)).toBe(false)
    expect(tabPassesDoor({ id: 'x' }, PLATFORM)).toBe(false)
    expect(tabPassesDoor(null, PLATFORM)).toBe(false)
  })

  it('an external-client session gets NO platform-only tab — today, none at all', () => {
    // With Work the only registered tab, a client sees an empty list; the rail
    // renders nothing for an empty list (source guard below), so nothing a
    // platform tab would fetch is ever requested.
    expect(visibleTabs(RAIL_TABS, CLIENT)).toEqual([])
    expect(visibleTabs(RAIL_TABS, PLATFORM).map((t) => t.id)).toEqual(['work'])
    // ...and with the four-tab set, exactly the audience/agent tabs (design
    // artboard 6: external-client door = Canvas · Files).
    expect(visibleTabs(FOUR, CLIENT).map((t) => t.id)).toEqual(['canvas', 'files'])
  })

  it('ignores malformed registry entries', () => {
    expect(visibleTabs([null, { label: 'no id', door: RAIL_DOORS.AUDIENCE }, ...RAIL_TABS], PLATFORM)
      .map((t) => t.id)).toEqual(['work'])
    expect(visibleTabs(undefined, PLATFORM)).toEqual([])
  })
})

// ---------------------------------------------------------------------------
// State

describe('ent#474 — state: collapsed by default, persisted, validated', () => {
  it('defaults collapsed on Work', () => {
    expect(defaultRailState()).toEqual({ open: false, tab: 'work' })
  })

  it('normalizes: unknown tab → default tab; open is a strict boolean', () => {
    expect(normalizeRailState({ open: true, tab: 'state' })).toEqual({ open: true, tab: 'work' })
    expect(normalizeRailState({ open: 'true', tab: 'work' }).open).toBe(false)
    expect(normalizeRailState(null)).toEqual(defaultRailState())
    expect(normalizeRailState('garbage')).toEqual(defaultRailState())
  })

  it('round-trips through storage', () => {
    const s = memStorage()
    expect(saveRailState(s, { open: true, tab: 'work' })).toBe(true)
    expect(s._m.get(RAIL_STORAGE_KEY)).toBe(JSON.stringify({ open: true, tab: 'work' }))
    expect(loadRailState(s)).toEqual({ open: true, tab: 'work' })
  })

  it('reads bad JSON, a throwing storage and no storage as the default', () => {
    const bad = memStorage(); bad.setItem(RAIL_STORAGE_KEY, '{not json')
    expect(loadRailState(bad)).toEqual(defaultRailState())
    expect(loadRailState({ getItem: () => { throw new Error('private mode') } })).toEqual(defaultRailState())
    expect(loadRailState(null)).toEqual(defaultRailState())
    expect(saveRailState({ setItem: () => { throw new Error('quota') } }, defaultRailState())).toBe(false)
    expect(saveRailState(null, defaultRailState())).toBe(false)
  })

  it('a remembered tab this session may not see falls back to the first it may', () => {
    const visible = visibleTabs(FOUR, CLIENT)     // canvas, files
    expect(activeTabFor({ tab: 'work' }, visible)).toBe('canvas')
    expect(activeTabFor({ tab: 'files' }, visible)).toBe('files')
    expect(activeTabFor({ tab: 'work' }, [])).toBeNull()
    expect(activeTabFor(null, visible)).toBe('canvas')
  })
})

// ---------------------------------------------------------------------------
// Signals

describe('ent#474 — the collapsed signal', () => {
  const work = RAIL_TABS.find((t) => t.id === 'work')

  it('normalizes a signal entry and reads anything unreadable as "no signal"', () => {
    expect(signalFor({ work: { live: 2, agents: ['a', 'b'] } }, work)).toEqual({ live: 2, updated: false, agents: ['a', 'b'] })
    expect(signalFor({ work: { live: -1, updated: true, agents: 'nope' } }, work)).toEqual({ live: 0, updated: true, agents: [] })
    expect(signalFor({ work: { live: NaN } }, work)).toEqual(emptySignal())
    expect(signalFor({}, work)).toEqual(emptySignal())
    expect(signalFor(null, work)).toEqual(emptySignal())
  })

  it('live outranks updated; nothing is nothing', () => {
    expect(signalShape({ live: 1, updated: true })).toBe(RAIL_SIGNAL_LIVE)
    expect(signalShape({ live: 0, updated: true })).toBe(RAIL_SIGNAL_UPDATED)
    expect(signalShape({ live: 0, updated: false })).toBeNull()
    expect(signalShape(null)).toBeNull()
  })

  it('titles the way the design pass writes them', () => {
    expect(railTitle(work, { live: 1, updated: false, agents: ['scout'] })).toBe('Work · 1 running')
    expect(railTitle(work, { live: 0, updated: true, agents: [] })).toBe('Work · updated')
    expect(railTitle(work, emptySignal())).toBe('Work')
  })

  it('reports only VISIBLE tabs — a signal for a hidden tab never leaks', () => {
    const signals = { work: { live: 1 }, canvas: { updated: true }, files: { updated: true } }
    const forClient = collapsedSignals(signals, visibleTabs(FOUR, CLIENT))
    expect(forClient.map((s) => s.id)).toEqual(['canvas', 'files'])
    expect(forClient.every((s) => s.shape === RAIL_SIGNAL_UPDATED)).toBe(true)
    expect(hasLiveSignal({ work: { live: 1 } }, visibleTabs(RAIL_TABS, CLIENT))).toBe(false)
    expect(hasLiveSignal({ work: { live: 1 } }, visibleTabs(RAIL_TABS, PLATFORM))).toBe(true)
  })

  it('the mobile strip says the same thing in words, and names the tabs when there is nothing to say', () => {
    const visible = visibleTabs(FOUR, PLATFORM)
    const signals = { work: { live: 1 }, loops: { live: 1 }, canvas: { updated: true }, files: { updated: true } }
    expect(stripSegments(signals, visible)).toEqual([
      { id: 'work', shape: RAIL_SIGNAL_LIVE, text: 'Work · 1 running' },
      { id: 'loops', shape: RAIL_SIGNAL_LIVE, text: 'Loops · 1 running' },
      { id: 'canvas', shape: RAIL_SIGNAL_UPDATED, text: 'Canvas updated' },
      { id: 'files', shape: RAIL_SIGNAL_UPDATED, text: 'Files updated' },
    ])
    expect(stripSegments({}, visibleTabs(RAIL_TABS, PLATFORM))).toEqual([{ id: 'work', shape: null, text: 'Work' }])
  })

  it('the Work signal is derived from the in-flight flag and UNLATCHES with it', () => {
    expect(workSignalFrom({ sending: true, agent: 'scout' })).toEqual({ live: 1, updated: false, agents: ['scout'] })
    expect(workSignalFrom({ sending: false, agent: 'scout' })).toEqual({ live: 0, updated: false, agents: [] })
    expect(workSignalFrom({})).toEqual({ live: 0, updated: false, agents: [] })
    // Truthy is not true here either.
    expect(workSignalFrom({ sending: 'yes', agent: 'scout' }).live).toBe(0)
  })

  it('the room signal is the server\'s working list', () => {
    expect(workSignalFromRoom(['scout', 'sage'])).toEqual({ live: 2, updated: false, agents: ['scout', 'sage'] })
    expect(workSignalFromRoom([])).toEqual({ live: 0, updated: false, agents: [] })
    expect(workSignalFromRoom(null)).toEqual({ live: 0, updated: false, agents: [] })
  })
})

// ---------------------------------------------------------------------------
// Room grouping and copy

describe('ent#474 — a room shows every participant, in order, absence visible', () => {
  it('groups by participant, keeps empty rows, drops outsiders', () => {
    const items = [{ agent_name: 'sage' }, { agent_name: 'scout' }, { agent_name: 'stranger' }]
    const g = groupByParticipant(items, ['scout', 'sage', 'scribe'])
    expect(g.map(([a]) => a)).toEqual(['scout', 'sage', 'scribe'])
    expect(g[0][1]).toEqual([{ agent_name: 'scout' }])
    expect(g[2][1]).toEqual([])
    expect(g.flatMap(([, list]) => list).some((i) => i.agent_name === 'stranger')).toBe(false)
    expect(groupByParticipant(null, null)).toEqual([])
  })

  it('a participant\'s row state counts its own live work', () => {
    const sig = { live: 2, updated: false, agents: ['scout', 'scout'] }
    expect(participantState('scout', sig)).toEqual({ live: 2, label: '2 running' })
    expect(participantState('sage', sig)).toEqual({ live: 0, label: 'nothing in flight' })
    expect(participantState('sage', null)).toEqual({ live: 0, label: 'nothing in flight' })
  })

  it('the empty copy names the agent in a 1:1 and "an agent in this room" otherwise', () => {
    const work = RAIL_TABS.find((t) => t.id === 'work')
    expect(railEmptyCopy(work, ['scout']).body).toMatch(/^When scout takes on a longer job/)
    expect(railEmptyCopy(work, ['scout', 'sage']).body).toMatch(/^When an agent in this room takes on/)
    expect(railEmptyCopy(work, []).body).toMatch(/an agent in this room/)
    expect(railEmptyCopy(work, ['scout'])).toMatchObject({ action: 'See what you can ask', event: 'see-hints' })
    expect(railEmptyCopy(null, [])).toMatchObject({ title: 'Nothing here yet', action: null })
  })
})

// ---------------------------------------------------------------------------
// Shell placement

describe('ent#474 — where the rail is, and whom it is scoped to', () => {
  it('scopes to the active agent, the room\'s participants, and nobody on the agent page', () => {
    expect(railParticipantsFor({ activeAgent: 'scout' })).toEqual(['scout'])
    expect(railParticipantsFor({ roomId: 'r1', roomParticipants: ['a', 'b'], activeAgent: 'scout' })).toEqual(['a', 'b'])
    expect(railParticipantsFor({ agentPage: 'scout', activeAgent: 'scout' })).toEqual([])
    expect(railParticipantsFor({})).toEqual([])
  })

  it('exists only beside a conversation that can render', () => {
    const base = { stageState: 'ready', activeAgent: 'scout', roomsAvailable: true }
    expect(railVisibleFor(base)).toBe(true)
    expect(railVisibleFor({ ...base, agentPage: 'scout' })).toBe(false)
    for (const stageState of ['loading', 'failed', 'empty']) {
      expect(railVisibleFor({ ...base, stageState }), stageState).toBe(false)
    }
    expect(railVisibleFor({ ...base, unreachable: true })).toBe(false)
    expect(railVisibleFor({ ...base, activeAgent: null })).toBe(false)
    // A room follows the rooms capability (#2128), and needs no agent yet —
    // its participants arrive with its own fetch.
    expect(railVisibleFor({ stageState: 'ready', roomId: 'r1', roomsAvailable: true })).toBe(true)
    expect(railVisibleFor({ stageState: 'ready', roomId: 'r1', roomsAvailable: false, activeAgent: 'scout' })).toBe(false)
  })
})

// ---------------------------------------------------------------------------
// Source guards

describe('ent#474 — shell wiring (source guards)', () => {
  const portal = src('views/Portal.vue')

  it('mounts the rail as a sibling of <main>, outside every remount, without a key', () => {
    const mainEnd = portal.indexOf('</main>')
    const railAt = portal.indexOf('<PortalRail\n')
    expect(mainEnd).toBeGreaterThan(-1)
    expect(railAt, 'the rail column must exist').toBeGreaterThan(mainEnd)
    expect(railAt).toBeLessThan(portal.indexOf('<PortalAgentPicker'))
    const el = portal.slice(railAt, portal.indexOf('/>', railAt))
    expect(el).not.toContain(':key=')
    expect(el).toContain('v-if="railVisible"')
    expect(el).toContain(':tabs="railTabs"')
  })

  it('reads and persists rail state as a setup ref under the one key', () => {
    expect(portal).toContain('const railState = ref(loadRailState(safeStorage()))')
    expect(portal).toMatch(/watch\(railState, \(s\) => saveRailState\(safeStorage\(\), s\), \{ deep: true \}\)/)
  })

  it('derives the visible tabs from the session, never a hard-coded door', () => {
    expect(portal).toContain('visibleTabs(RAIL_TABS, {')
    expect(portal).toContain('isPlatform: store.isPlatformSession,')
    expect(portal).not.toContain('isPlatform: true')
    // The strip and the sheet read the same list.
    expect((portal.match(/:tabs="railTabs"/g) || []).length).toBeGreaterThanOrEqual(3)
  })

  it('keys visibility on the route and the stage verdict', () => {
    expect(portal).toContain('stageState: stage.value.state,')
    expect(portal).toContain('roomsAvailable: store.multiAgentChatAvailable,')
  })

  it('resets the Work signal on every chat switch, so it cannot read as still running', () => {
    const at = portal.indexOf('watch([convKey, activeRoomIdFromRoute]')
    expect(at).toBeGreaterThan(-1)
    const body = portal.slice(at, portal.indexOf('\n})', at))
    expect(body).toContain('workSignal.value = emptySignal()')
    expect(body).toContain('roomParticipants.value = []')
  })

  it('renders the mobile form as a sheet of the same component', () => {
    expect(portal).toMatch(/<PortalRail\n\s+v-if="railVisible && railSheetOpen"\n\s+sheet/)
  })
})

describe('ent#474 — the rail component (source guards)', () => {
  const rail = src('components/portal/PortalRail.vue')

  it('renders nothing at all for a session with no visible tab', () => {
    expect(rail).toMatch(/<div\n\s+v-if="tabs\.length"/)
    // ...and never widens the list from the registry.
    expect(rail).not.toMatch(/RAIL_TABS/)
  })

  it('mounts a body for exactly the active visible tab', () => {
    expect(rail).toContain('<div v-if="active" class="flex-1 min-h-0 overflow-y-auto p-4"')
    expect(rail).toContain(':name="`tab-${active.id}`"')
  })

  it('is the approved width in each form, with the sheet on the files-panel pattern', () => {
    expect(rail).toMatch(/collapsed: 'w-12 /)
    expect(rail).toMatch(/open: 'w-96 /)
    expect(rail).toContain('inset-x-0 bottom-0 max-h-[85vh] rounded-t-2xl')
    expect(rail).toContain(`:role="mode === 'sheet' ? 'dialog' : undefined"`)
    expect(rail).toContain("e.key === 'Escape'")
  })

  it('draws two signal shapes, and pulses only where motion is welcome', () => {
    expect(rail).toContain('ring-[3px] ring-action-primary-500/[.28] motion-safe:animate-pulse')
    expect(rail).toContain("RAIL_SIGNAL_UPDATED) return 'block w-1.5 h-1.5 rounded-full bg-action-primary-500'")
    expect(rail).not.toMatch(/(?<!motion-safe:)animate-pulse/)
  })

  it('a room renders a row per participant with its state', () => {
    expect(rail).toContain('data-testid="portal-rail-rows"')
    expect(rail).toContain('participantState(agent, activeSignal.value)')
  })
})

describe('ent#474 — the two conversations feed the rail (source guards)', () => {
  it('the conversation emits the Work signal from its in-flight flag, and clears it on unmount', () => {
    const conv = src('components/portal/PortalConversation.vue')
    expect(conv).toMatch(/watch\(\n\s+sending,\n\s+\(on\) => emit\('work-state', workSignalFrom\(\{ sending: on, agent: props\.agent\?\.name \}\)\)/)
    expect(conv).toContain("onBeforeUnmount(() => emit('work-state', workSignalFrom({ sending: false })))")
    expect(conv).toContain('<slot name="rail-strip" />')
    expect(conv.indexOf('<slot name="rail-strip" />')).toBeLessThan(conv.indexOf('v-if="offline"'))
  })

  it('the room reports its participants and its server-derived working list', () => {
    const room = src('components/portal/PortalRoom.vue')
    expect(room).toContain("watch(agentParticipants, (list) => emit('participants-changed', list), { immediate: true, deep: true })")
    expect(room).toContain("watch(workingAgents, (list) => emit('work-state', workSignalFromRoom(list)), { immediate: true, deep: true })")
    expect(room).toContain('<slot name="rail-strip" />')
  })

  it('the strip is the mobile form only, over the same segment rule', () => {
    const strip = src('components/portal/PortalRailStrip.vue')
    expect(strip).toContain('class="sm:hidden w-full')
    expect(strip).toContain('stripSegments(props.signals, props.tabs)')
  })

  it('OverflowTabs measures the signal dot in its mirror row', () => {
    const tabs = src('components/OverflowTabs.vue')
    const mirror = tabs.slice(tabs.indexOf('ref="measureNav"'))
    expect(mirror).toContain('v-if="tab.signal"')
    expect(tabs).toContain("${t.signal ?? ''}")
  })
})
