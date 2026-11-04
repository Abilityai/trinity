/**
 * Levels (core-agent AC-15 to AC-17): seven new abilities, each ticked only
 * from what the page read on the instance with the person's own session (`levelSignals.js`).
 * Only the person's own agents count, and nothing their guide did by itself ticks a level.
 * Level 1 is having an agent; Level 2 a playbook added after the page first saw the agent
 * (`base`, remembered on the saved answers); Level 3 (keys) is skipped when none of their
 * agents needs any. A level that rests on history (a scheduled run, a call) stays reached
 * while the agents behind it are still theirs, because the routes only show recent history;
 * the rest is read as it is now. Pure, so every rule is tested in node.
 */
import { isBuildCrew } from './portalBuild'

export const LEVELS = Object.freeze([
  {
    id: 1,
    name: 'First agent',
    means: 'You have an agent of your own on Trinity.',
    why: "It's the moment an agent stops being an idea and becomes yours.",
    prompt: 'Help me reach Level 1: plan and build my first agent with me.',
  },
  {
    id: 2,
    name: 'A new playbook',
    means: 'You added a playbook to one of your agents after it was built: one more job it does the same way every time.',
    why: 'Your agents grow with your work, one job at a time.',
    prompt: 'Help me reach Level 2: add a new playbook to one of my agents.',
  },
  {
    id: 3,
    name: 'Connected',
    means: 'Every key one of your agents needs is set, so it can use your tools.',
    why: 'An agent can only reach your tools once it has their keys.',
    prompt: 'Help me reach Level 3: show me how to add the keys my agent needs.',
  },
  {
    id: 4,
    name: 'In control',
    means: 'One of your agents is shared with someone, or allowed to call another agent.',
    why: 'You decide who can use your agents and what they can reach.',
    prompt: 'Help me reach Level 4: share one of my agents with someone, or let one of my agents call another.',
  },
  {
    id: 5,
    name: 'On a schedule',
    means: 'One of your agents has run on its own, on a schedule you set.',
    why: 'It does the work at the times you choose, without you starting it.',
    prompt: 'Help me reach Level 5: put one of my agents on a schedule.',
  },
  {
    id: 6,
    name: 'A small team',
    means: 'Three or more agents of yours, and one has really called another.',
    why: 'Your agents start working as a team, not one at a time.',
    prompt: 'Help me reach Level 6: set up a small team, where one of my agents calls another.',
  },
  {
    id: 7,
    name: 'Running on its own',
    means: 'In one job, one of your agents called two or more of your others, and you answered something it asked you to approve.',
    why: 'Your agents run the work, and only what needs you reaches you.',
    prompt: 'Help me reach Level 7: have one of my agents run a job across my other agents, and ask me to approve what matters.',
  },
].map((level) => Object.freeze(level)))

const MAX_AGENTS = 20
const SYSTEM_AGENT = 'trinity-system'
// Levels that rest on history the routes only show the recent part of.
const KEPT_LEVELS = new Set([5, 6, 7])
// The level a team needs three agents for, and the one skipped when no agent needs keys.
const TEAM_LEVEL = 6
const KEYS_LEVEL = 3
const MAX_PLAYBOOKS = 100
const MAX_BASE_AGENTS = 40
// Saved answers from the six-level ladder (before 4 Nov) used other numbers.
export const LEVELS_VERSION = 7
const SIX_TO_SEVEN = Object.freeze({ 1: 1, 2: 3, 3: 4, 4: 5, 5: 6, 6: 7 })
const objectOr = (value) => (value && typeof value === 'object' && !Array.isArray(value) ? value : {})

/** The agents that count for this person: their own, minus their guide, the system agent and the samples. */
export function levelAgents(roster, seeded = []) {
  const samples = new Set(Array.isArray(seeded) ? seeded : [])
  return (Array.isArray(roster) ? roster : [])
    .filter((a) => a?.owned === true && a.name && !isBuildCrew(a.name) && a.name !== SYSTEM_AGENT && !a.is_system && !samples.has(a.name))
    .map((a) => a.name)
    .slice(0, MAX_AGENTS)
}

const list = (value) => (Array.isArray(value) ? value : [])
const time = (iso) => {
  const t = Date.parse(iso || '')
  return Number.isNaN(t) ? null : t
}

// A job of theirs that worked, and wasn't started by their guide.
const realJob = (j) => j?.status === 'success' && !isBuildCrew(j.source_agent_name || '')

// For each level: the agents that make it true now (empty when it isn't).
function evidenceNow(s, base) {
  const own = list(s.own)
  const mine = new Set(own)
  const jobs = (name) => list(s.jobs?.[name]).filter(realJob)
  const calls = list(s.calls).filter((c) => mine.has(c?.from) && mine.has(c?.to) && c.from !== c.to)

  const first = own[0]
  // A playbook it has now that it didn't have when the page first saw it.
  const grown = own.find((name) => {
    const now = s.playbooks?.[name]
    const before = base?.[name]
    return Array.isArray(now) && Array.isArray(before) && now.some((p) => !before.includes(p))
  })
  const keyed = own.find((name) => s.keys?.[name] === 'all-set')
  const control = own.find((name) => list(s.shares?.[name]).length > 0
    || list(s.permissions?.[name]).some((to) => to && to !== name && !isBuildCrew(to)))
  const scheduled = own.find((name) => jobs(name).some((j) => j.triggered_by === 'schedule'))
  const teamCall = own.length >= 3 ? calls[0] : null
  let lead = null
  for (const name of own) {
    for (const j of jobs(name)) {
      const start = time(j.started_at)
      const end = time(j.completed_at)
      if (start === null || end === null) continue
      const targets = [...new Set(calls.filter((c) => c.from === name && time(c.at) !== null && time(c.at) >= start && time(c.at) <= end).map((c) => c.to))]
      if (targets.length >= 2) {
        lead = [name, ...targets]
        break
      }
    }
    if (lead) break
  }
  return {
    1: first ? [first] : [],
    2: grown ? [grown] : [],
    3: keyed ? [keyed] : [],
    4: control ? [control] : [],
    5: scheduled ? [scheduled] : [],
    6: teamCall ? [teamCall.from, teamCall.to] : [],
    7: lead && Number(s.answered) > 0 ? lead : [],
  }
}

/**
 * The seven levels, each with whether it is reached now, whether it is skipped (Level 3,
 * when none of their agents needs keys), and the agents that made it true. `kept` is
 * what the saved answers remember (`levelsKept`): a history level stays reached while
 * every agent behind it is still theirs (and, for a team, while they still have three).
 * `base` is each agent's playbooks when the page first saw it (`levelsBase`).
 */
export function levelsFrom(signals, kept = {}, base = {}) {
  const s = signals && typeof signals === 'object' ? signals : {}
  const own = list(s.own)
  const mine = new Set(own)
  const now = evidenceNow(s, base && typeof base === 'object' ? base : {})
  const keptFor = (id) => {
    const names = list(kept?.[id])
    if (!KEPT_LEVELS.has(id) || !names.length || !names.every((n) => mine.has(n))) return false
    return id !== TEAM_LEVEL || own.length >= 3
  }
  const keysNotNeeded = own.length > 0 && own.every((name) => s.keys?.[name] === 'needs-none')
  return LEVELS.map((level) => {
    const reached = now[level.id].length > 0 || keptFor(level.id)
    return {
      ...level,
      reached,
      skipped: level.id === KEYS_LEVEL && !reached && keysNotNeeded,
      evidence: now[level.id],
    }
  })
}

/**
 * Each agent's playbooks the first time the page saw them, added to `base` (the saved
 * answers' `levelsBase`) for agents it has no first look at yet: a first look is never
 * replaced, so a playbook added later counts for Level 2. Null when nothing is new.
 */
export function playbookBase(base, signals) {
  const before = base && typeof base === 'object' ? base : {}
  const next = { ...before }
  let changed = false
  for (const name of list(signals?.own)) {
    const now = signals?.playbooks?.[name]
    if (!Array.isArray(now) || Array.isArray(before[name])) continue
    next[name] = now.filter((p) => typeof p === 'string' && p).slice(0, MAX_PLAYBOOKS)
    changed = true
  }
  const own = list(signals?.own)
  const mine = new Set(own)
  // An agent no longer theirs is forgotten, so one built again under its name gets a new
  // first look. Not when the list may be cut short at the cap, or wasn't read at all.
  if (own.length > 0 && own.length < MAX_AGENTS) {
    for (const name of Object.keys(next)) {
      if (!mine.has(name)) {
        delete next[name]
        changed = true
      }
    }
  }
  if (!changed) return null
  // Bounded: the oldest first looks at agents that are no longer theirs go first.
  for (const name of Object.keys(next)) {
    if (Object.keys(next).length <= MAX_BASE_AGENTS) break
    if (!mine.has(name)) delete next[name]
  }
  return next
}

/**
 * The levels part of the saved answers, in the seven-level numbers: answers saved by the
 * six-level version are moved up (Connected was 2, now 3; and so on), and the levels that
 * are now read as they are (the old 1) are no longer kept.
 */
export function levelsOf(intake) {
  const i = objectOr(intake)
  const seen = list(i.levelsSeen)
  const kept = objectOr(i.levelsKept)
  const levelsBase = objectOr(i.levelsBase)
  if (i.levelsVersion === LEVELS_VERSION) return { levelsSeen: seen, levelsKept: kept, levelsBase, levelsVersion: LEVELS_VERSION }
  return {
    levelsSeen: [...new Set(seen.map((id) => SIX_TO_SEVEN[id]).filter(Boolean))],
    levelsKept: Object.fromEntries(Object.entries(kept)
      .map(([id, names]) => [SIX_TO_SEVEN[id], names])
      .filter(([id]) => KEPT_LEVELS.has(id))),
    levelsBase,
    levelsVersion: LEVELS_VERSION,
  }
}

/** The saved answers with their levels part in the seven-level numbers, then `patch`. */
export function withLevels(intake, patch = {}) {
  return { ...objectOr(intake), ...levelsOf(intake), ...patch, levelsVersion: LEVELS_VERSION }
}

const sameBase = (a, b) => {
  const keys = Object.keys(a)
  return keys.length === Object.keys(b).length
    && keys.every((k) => Array.isArray(b[k]) && b[k].length === a[k].length && b[k].every((p, i) => p === a[k][i]))
}

/**
 * The first looks to compare with now (`base`), and the saved answers to save, or null
 * when nothing needs saving. It builds on the newest copy (another tab may have saved
 * since this page loaded), takes a first look already saved over its own, and saves again
 * when a save in between dropped one.
 */
export function intakeWithBase(current, stored, localBase, signals) {
  const cur = objectOr(current)
  const sto = objectOr(stored)
  const fresher = (Number(sto.savedAt) || 0) > (Number(cur.savedAt) || 0) ? sto : cur
  const known = { ...objectOr(localBase), ...objectOr(cur.levelsBase), ...objectOr(sto.levelsBase) }
  const base = playbookBase(known, signals) || known
  return { base, intake: sameBase(base, objectOr(fresher.levelsBase)) ? null : withLevels(fresher, { levelsBase: base }) }
}

/** The lowest level neither reached nor skipped, or null when there is none. */
export function nextLevel(levels) {
  return list(levels).find((l) => !l.reached && !l.skipped) || null
}

/** Levels reached now that the page hasn't celebrated before. A skipped level is never one. */
export function newlyReached(levels, seen) {
  const done = new Set(list(seen))
  return list(levels).filter((l) => l.reached && !done.has(l.id)).map((l) => l.id)
}

/** One line for a level reached. */
export function levelLine(level) {
  return `Level reached: Level ${level.id}, ${level.name}.`
}

/** What the guide is told: each level reached, then the one that comes next. */
export function levelMessage(reachedNow, levels) {
  const next = nextLevel(levels)
  const lines = list(reachedNow).map(levelLine)
  lines.push(next ? `Next: Level ${next.id}, ${next.name}.` : 'That was the last level.')
  return lines.join('\n')
}

/** Remembers celebrated levels once each, and the agents behind each history level. */
export function markLevelsSeen(intake, ids, levels = []) {
  const seen = list(intake?.levelsSeen)
  const next = [...seen]
  const keptBefore = intake?.levelsKept && typeof intake.levelsKept === 'object' ? intake.levelsKept : {}
  const levelsKept = { ...keptBefore }
  for (const id of list(ids)) {
    if (!next.includes(id)) next.push(id)
    const evidence = list(list(levels).find((l) => l.id === id)?.evidence)
    if (KEPT_LEVELS.has(id) && evidence.length) levelsKept[id] = evidence
  }
  return { ...intake, levelsSeen: next, levelsKept, levelsVersion: LEVELS_VERSION }
}
