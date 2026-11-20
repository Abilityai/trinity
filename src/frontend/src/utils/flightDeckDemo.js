/**
 * Flight Deck demo data (abilityai/trinity-enterprise#1307).
 *
 * `?demo=1` swaps the deck's inputs for a simulated fleet that keeps moving:
 * agents start and finish runs, some fail, the host load follows how many are
 * working. It exists to show the view to someone whose own fleet is quiet. It
 * is labelled on screen, reads and writes nothing, and uses the same shapes
 * the real stores hold, so the deck draws it through the same rules.
 *
 * Pure: `createDemo` and `stepDemo` take the clock and a random source.
 */

const NAMES = [
  ['atlas', 'Atlas · Research', 'claude-code'], ['scribe', 'Scribe · Writing', 'claude-code'],
  ['ledger', 'Ledger · Finance', 'codex'], ['scout', 'Scout · Prospecting', 'claude-code'],
  ['relay', 'Relay · Support', 'gemini-cli'], ['forge', 'Forge · Engineering', 'claude-code'],
  ['sage', 'Sage · Analytics', 'claude-code'], ['herald', 'Herald · Comms', 'codex'],
  ['warden', 'Warden · Security', 'claude-code'], ['quill', 'Quill · Docs', 'gemini-cli'],
  ['pilot', 'Pilot · Ops', 'claude-code'], ['echo', 'Echo · Archive', 'claude-code'],
]
const TASKS = ['Morning digest', 'Inbox sweep', 'Weekly report', 'Lead scoring', 'Backlog grooming', 'Cost review', 'Release notes', 'Health check']
const TRIGGERS = ['schedule', 'schedule', 'agent', 'manual', 'webhook']

/** A small seeded generator, so a demo run can be replayed in a test. */
export function seeded(seed = 1) {
  let a = seed >>> 0
  return () => {
    a = (a + 0x6d2b79f5) >>> 0
    let t = a
    t = Math.imul(t ^ (t >>> 15), t | 1)
    t ^= t + Math.imul(t ^ (t >>> 7), t | 61)
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

const iso = (t) => new Date(t).toISOString()
const pick = (list, rand) => list[Math.floor(rand() * list.length)]

export function createDemo(now = Date.now(), rand = Math.random) {
  const agents = NAMES.map(([name, display_label, runtime], i) => ({
    name, display_label, runtime,
    status: i === NAMES.length - 1 ? 'stopped' : 'running',
    autonomy_enabled: i % 3 !== 2,
  }))
  const state = {
    agents, workingState: {}, contextStats: {}, executionStats: {}, activities: [], seq: 0,
    schedules: agents.filter((a) => a.status === 'running').slice(0, 6).map((a, i) => ({
      id: i, agent_name: a.name, name: TASKS[i % TASKS.length], enabled: true,
      next_run_at: iso(now + (2 + i * 7) * 60000),
    })),
    host: { cpu: { percent: 12, count: 8 }, memory: { percent: 38, used_gb: 12.2, total_gb: 32 }, disk: { percent: 54, used_gb: 270, total_gb: 500 } },
    days: Array.from({ length: 30 }, (_, i) => {
      const total = Math.round(20 + rand() * 60 + (i % 7 > 4 ? -15 : 10))
      return { bucket: iso(now - (29 - i) * 86400000).slice(0, 10), total: Math.max(0, total), failed: Math.round(rand() * 3) }
    }),
    hours: Array.from({ length: 24 }, (_, i) => ({
      bucket: iso(now - (23 - i) * 3600000).slice(0, 13) + ':00',
      total: Math.round(rand() * 9), failed: rand() < 0.12 ? 1 : 0,
    })),
    ends: {},
  }
  for (const a of agents) {
    const runs = a.status === 'running' ? Math.round(4 + rand() * 30) : 0
    const failed = Math.round(runs * rand() * rand() * 0.25)
    state.executionStats[a.name] = {
      taskCount: runs, successCount: runs - failed, failedCount: failed,
      successRate: runs ? Math.round(((runs - failed) / runs) * 100) : 0, totalCost: +(runs * (0.05 + rand() * 0.3)).toFixed(2),
    }
    state.contextStats[a.name] = {
      contextPercent: a.status === 'running' ? Math.round(8 + rand() * 60) : 0,
      activityState: 'idle', lastActivityTime: iso(now - Math.round(rand() * 5400) * 1000),
    }
  }
  // Start with a few in flight so the first frame is not an empty deck.
  for (const a of agents.slice(0, 3)) start(state, a.name, now - Math.round(rand() * 90000), rand)
  return state
}

function start(state, name, at, rand) {
  const id = `demo-${++state.seq}`
  state.workingState = { ...state.workingState, [name]: { since: iso(at) } }
  state.ends[name] = { id, at: at + (15 + rand() * 75) * 1000, fail: rand() < 0.06 }
  state.activities = [{
    source_agent: name, activity_id: id, status: 'started', activity_type: 'schedule_start',
    triggered_by: pick(TRIGGERS, rand), schedule_name: pick(TASKS, rand), timestamp: iso(at), duration_ms: null,
  }, ...state.activities].slice(0, 60)
}

function finish(state, name, now) {
  const end = state.ends[name]
  const act = state.activities.find((a) => a.activity_id === end.id)
  if (act) {
    act.status = end.fail ? 'failed' : 'completed'
    act.duration_ms = now - new Date(act.timestamp).getTime()
  }
  state.activities = [...state.activities]
  const ws = { ...state.workingState }
  delete ws[name]
  state.workingState = ws
  delete state.ends[name]
  const ex = { ...state.executionStats[name] }
  ex.taskCount += 1
  if (end.fail) ex.failedCount += 1
  else ex.successCount += 1
  ex.successRate = Math.round((ex.successCount / ex.taskCount) * 100)
  ex.totalCost = +(ex.totalCost + 0.08).toFixed(2)
  state.executionStats = { ...state.executionStats, [name]: ex }
  const cs = state.contextStats[name]
  state.contextStats = {
    ...state.contextStats,
    [name]: { ...cs, contextPercent: cs.contextPercent > 85 ? 12 : cs.contextPercent + 4, lastActivityTime: iso(now) },
  }
  const last = state.hours[state.hours.length - 1]
  state.hours = [...state.hours.slice(0, -1), { ...last, total: last.total + 1, failed: last.failed + (end.fail ? 1 : 0) }]
  const day = state.days[state.days.length - 1]
  state.days = [...state.days.slice(0, -1), { ...day, total: day.total + 1 }]
}

/** Advance the simulation to `now`. Call about once a second. */
export function stepDemo(state, now = Date.now(), rand = Math.random) {
  const running = state.agents.filter((a) => a.status === 'running').map((a) => a.name)
  for (const name of running) {
    if (state.ends[name]) {
      if (now >= state.ends[name].at) finish(state, name, now)
    } else if (Object.keys(state.ends).length < 6 && rand() < 0.035) {
      start(state, name, now, rand)
    }
  }
  const busy = Object.keys(state.ends).length
  const drift = (v, target, k) => v + (target - v) * k
  state.host = {
    cpu: { count: 8, percent: +Math.max(2, Math.min(98, drift(state.host.cpu.percent, 8 + busy * 14 + rand() * 10, 0.35))).toFixed(1) },
    memory: { ...state.host.memory, percent: +drift(state.host.memory.percent, 34 + busy * 4, 0.15).toFixed(1), used_gb: +(32 * (34 + busy * 4) / 100).toFixed(1) },
    disk: state.host.disk,
  }
  state.schedules = state.schedules.map((sc) => (
    new Date(sc.next_run_at).getTime() <= now ? { ...sc, next_run_at: iso(now + (20 + rand() * 40) * 60000) } : sc
  ))
  return state
}
