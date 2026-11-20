/**
 * Flight Deck (abilityai/trinity-enterprise#1307) — the rules behind the view.
 *
 * Everything the page decides without a DOM lives in `utils/flightDeck.js`:
 * who counts as working, the order and the cap of what is drawn, what the
 * privacy setting lets through, when the page sleeps, when it calls its data
 * stale. The view and the canvas only render what these return.
 */
import { describe, expect, it } from 'vitest'
import {
  STALE_AFTER_MS,
  agoLabel,
  costLabel,
  fleetTotals,
  upNext,
  deckAgents,
  deckFeed,
  fleetCounts,
  isAwake,
  isStale,
  popoutFeatures,
  taskLine,
} from '../../src/utils/flightDeck'
import { createDemo, seeded, stepDemo } from '../../src/utils/flightDeckDemo'

const NOW = Date.parse('2026-11-19T12:00:00Z')
const iso = (minsAgo) => new Date(NOW - minsAgo * 60000).toISOString()

const agents = [
  { name: 'alpha', status: 'running' },
  { name: 'bravo', status: 'running', display_label: 'Bravo Bot' },
  { name: 'charlie', status: 'stopped' },
  { name: 'delta', status: 'running' },
]

describe('who is working', () => {
  it('unions the socket-observed map with the polled activity state', () => {
    const rows = deckAgents({
      agents,
      workingState: { alpha: { since: iso(3) } },
      contextStats: { delta: { activityState: 'active', lastActivityTime: iso(1) } },
      activities: [],
      now: NOW,
    })
    expect(rows.filter((r) => r.working).map((r) => r.name).sort()).toEqual(['alpha', 'delta'])
  })

  it('ignores a working entry for an agent that is not in the visible set', () => {
    const rows = deckAgents({
      agents: [agents[0]],
      workingState: { ghost: { since: iso(1) } },
      contextStats: {},
      activities: [],
      now: NOW,
    })
    expect(rows.map((r) => r.name)).toEqual(['alpha'])
    expect(fleetCounts(rows)).toEqual({ working: 0, idle: 1, stopped: 0, total: 1 })
  })

  it('counts working, idle and stopped separately', () => {
    const rows = deckAgents({
      agents,
      workingState: { alpha: { since: iso(3) } },
      contextStats: {},
      activities: [],
      now: NOW,
    })
    expect(fleetCounts(rows)).toEqual({ working: 1, idle: 2, stopped: 1, total: 4 })
  })
})

describe('order and cap', () => {
  const rows = deckAgents({
    agents,
    workingState: { delta: { since: iso(2) } },
    contextStats: {
      alpha: { activityState: 'idle', lastActivityTime: iso(50) },
      bravo: { activityState: 'idle', lastActivityTime: iso(5) },
    },
    activities: [],
    now: NOW,
  })

  it('puts working agents first, then the most recently active, stopped last', () => {
    expect(rows.map((r) => r.name)).toEqual(['delta', 'bravo', 'alpha', 'charlie'])
  })

  it('uses the display name as the label and keeps the slug as the id', () => {
    expect(rows.find((r) => r.name === 'bravo').label).toBe('Bravo Bot')
  })

  it('reports seconds in the current state from the working start', () => {
    expect(rows[0].sinceSec).toBe(120)
  })
})

describe('privacy', () => {
  const activity = {
    source_agent: 'alpha', activity_type: 'schedule_start', status: 'started',
    triggered_by: 'schedule', schedule_name: 'Quarterly numbers for Acme', timestamp: iso(2),
  }

  it('hides the task text by default and shows only the kind of trigger', () => {
    expect(taskLine(activity)).toBe('schedule')
    expect(taskLine(activity, { showText: false })).toBe('schedule')
  })

  it('shows the schedule name when text is allowed', () => {
    expect(taskLine(activity, { showText: true })).toBe('schedule · Quarterly numbers for Acme')
  })

  it('never leaks task text into a row when privacy is on', () => {
    const rows = deckAgents({
      agents: [agents[0]],
      workingState: { alpha: { since: iso(2) } },
      contextStats: {},
      activities: [{ ...activity, command_preview: 'summarise the Acme contract' }],
      now: NOW,
    })
    expect(JSON.stringify(rows)).not.toContain('Acme')
    const open = deckAgents({
      agents: [agents[0]],
      workingState: { alpha: { since: iso(2) } },
      contextStats: {},
      activities: [activity],
      now: NOW,
      showText: true,
    })
    expect(open[0].task).toContain('Acme')
  })

  it('gives an idle agent no task line', () => {
    const rows = deckAgents({ agents: [agents[0]], workingState: {}, contextStats: {}, activities: [activity], now: NOW, showText: true })
    expect(rows[0].task).toBe('')
  })
})

describe('feed', () => {
  const activities = [
    { source_agent: 'alpha', activity_id: 'a1', status: 'completed', timestamp: iso(9), duration_ms: 60000 },
    { source_agent: 'bravo', activity_id: 'a2', status: 'started', timestamp: iso(1) },
    { source_agent: 'delta', activity_id: 'a3', status: 'failed', timestamp: iso(4), duration_ms: 1000 },
    { source_agent: 'hidden', activity_id: 'a4', status: 'started', timestamp: iso(0) },
  ]

  it('lists the newest events first, limited, only for visible agents', () => {
    const feed = deckFeed(activities, { names: new Set(['alpha', 'bravo', 'delta']), limit: 2 })
    expect(feed.map((e) => [e.agent, e.kind])).toEqual([['bravo', 'started'], ['delta', 'failed']])
  })

  it('gives each event a key that changes when its state changes', () => {
    const [first] = deckFeed(activities, { names: new Set(['bravo']), limit: 5 })
    expect(first.key).toBe('a2:started')
  })
})

describe('sleep and staleness', () => {
  it('runs while the page can be seen and sleeps when hidden', () => {
    expect(isAwake('visible')).toBe(true)
    expect(isAwake('hidden')).toBe(false)
  })

  it('calls the data stale only after the limit, and never before a first load', () => {
    expect(isStale(null, NOW)).toBe(false)
    expect(isStale(NOW - STALE_AFTER_MS + 1, NOW)).toBe(false)
    expect(isStale(NOW - STALE_AFTER_MS - 1, NOW)).toBe(true)
  })
})

describe('small helpers', () => {
  it('formats time in state', () => {
    expect(agoLabel(5)).toBe('5s')
    expect(agoLabel(125)).toBe('2m')
    expect(agoLabel(3720)).toBe('1h 2m')
    expect(agoLabel(90000)).toBe('1d 1h')
    expect(agoLabel(-3)).toBe('0s')
  })

  it('asks for a window with no tabs or address bar, sized to the screen it has', () => {
    expect(popoutFeatures({ availWidth: 1920, availHeight: 1080 })).toBe('popup=yes,width=1920,height=1080,left=0,top=0')
    expect(popoutFeatures(undefined)).toBe('popup=yes,width=1280,height=800,left=0,top=0')
  })

})

describe('tile facts on a row', () => {
  const rows = deckAgents({
    agents: [
      { name: 'alpha', status: 'running', autonomy_enabled: true, runtime: 'codex' },
      { name: 'bravo', status: 'running' },
    ],
    workingState: {},
    contextStats: { alpha: { contextPercent: 41.6 } },
    executionStats: {
      alpha: { taskCount: 10, successCount: 8, failedCount: 2, successRate: 80, totalCost: 1.5 },
    },
    activities: [],
    now: NOW,
  })
  const alpha = rows.find((r) => r.name === 'alpha')
  const bravo = rows.find((r) => r.name === 'bravo')

  it('carries context, runs, success rate and cost for the last 24 hours', () => {
    expect(alpha).toMatchObject({ contextPercent: 42, runs: 10, failed: 2, successRate: 80, cost: 1.5, autonomous: true, runtime: 'codex' })
  })

  it('gives an agent with no runs no success reading, not 0%', () => {
    expect(bravo.successRate).toBeNull()
    expect(bravo.contextPercent).toBeNull()
    expect(bravo.runtime).toBe('claude-code')
  })

  it('totals the fleet and leaves the rate empty when nothing ran', () => {
    expect(fleetTotals(rows)).toEqual({ runs: 10, failed: 2, successRate: 80, cost: 1.5, autonomous: 1 })
    expect(fleetTotals([bravo]).successRate).toBeNull()
  })

  it('formats cost', () => {
    expect(costLabel(0)).toBe('$0')
    expect(costLabel(0.421)).toBe('$0.42')
    expect(costLabel(1234)).toBe('$1.2k')
  })
})

describe('up next', () => {
  const schedules = [
    { id: 1, agent_name: 'alpha', name: 'Acme digest', enabled: true, next_run_at: iso(-30) },
    { id: 2, agent_name: 'bravo', name: 'Sweep', enabled: true, next_run_at: iso(-5) },
    { id: 3, agent_name: 'alpha', name: 'Past', enabled: true, next_run_at: iso(5) },
    { id: 4, agent_name: 'hidden', name: 'Other', enabled: true, next_run_at: iso(-1) },
    { id: 5, agent_name: 'bravo', name: 'Off', enabled: false, next_run_at: iso(-2) },
  ]
  const names = new Set(['alpha', 'bravo'])

  it('lists future runs soonest first, for visible agents and enabled schedules only', () => {
    const next = upNext(schedules, { names, now: NOW })
    expect(next.map((n) => [n.agent, n.inSec])).toEqual([['bravo', 300], ['alpha', 1800]])
  })

  it('keeps schedule names out unless text is allowed', () => {
    expect(JSON.stringify(upNext(schedules, { names, now: NOW }))).not.toContain('Acme')
    expect(upNext(schedules, { names, now: NOW, showText: true })[1].name).toBe('Acme digest')
  })
})

describe('demo fleet', () => {
  it('starts with agents in flight and keeps moving: runs start, finish and are counted', () => {
    const rand = seeded(7)
    const state = createDemo(NOW, rand)
    expect(Object.keys(state.workingState).length).toBe(3)
    const runsBefore = Object.values(state.executionStats).reduce((n, e) => n + e.taskCount, 0)
    const started = new Set()
    for (let t = 1; t <= 600; t++) {
      stepDemo(state, NOW + t * 1000, rand)
      for (const a of state.activities) started.add(a.activity_id)
      expect(Object.keys(state.workingState).length).toBeLessThanOrEqual(6)
    }
    const runsAfter = Object.values(state.executionStats).reduce((n, e) => n + e.taskCount, 0)
    expect(runsAfter).toBeGreaterThan(runsBefore)
    expect(started.size).toBeGreaterThan(5)
    expect(state.activities.some((a) => a.status === 'completed')).toBe(true)
    expect(state.host.cpu.percent).toBeGreaterThan(0)
    expect(state.host.cpu.percent).toBeLessThanOrEqual(98)
  })

  it('never starts a stopped agent and feeds the same rules as real data', () => {
    const rand = seeded(3)
    const state = createDemo(NOW, rand)
    for (let t = 1; t <= 300; t++) stepDemo(state, NOW + t * 1000, rand)
    const stopped = state.agents.filter((a) => a.status !== 'running').map((a) => a.name)
    expect(stopped.length).toBe(1)
    expect(state.workingState[stopped[0]]).toBeUndefined()
    const rows = deckAgents({ ...state, now: NOW + 300000 })
    expect(fleetCounts(rows).total).toBe(state.agents.length)
    expect(JSON.stringify(rows)).not.toContain('digest')
  })
})
