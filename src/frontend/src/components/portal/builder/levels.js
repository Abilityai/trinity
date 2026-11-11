/**
 * Levels (core-agent AC-15 to AC-17): eight new abilities, each ticked only
 * from what the page read on the instance with the person's own session (`levelSignals.js`).
 * Each level says what reaching it means (`means`, for the tick and the celebration), what
 * to do to get there (`todo`, under the next level), and why it matters (`why`).
 * Only the person's own agents count, and nothing their guide did by itself ticks a level.
 * Level 1 is having an agent; Level 2 a playbook added after the page first saw the agent
 * (`base`, remembered on the saved answers); Level 3 (keys) is skipped when none of their
 * agents needs any. Level 5 is a schedule turned on with Autonomy on (or a scheduled run
 * already made), Level 6 three agents, and Level 7 one of them allowed to call another (or
 * a call already made). A level that rests on history (a scheduled run, a call) stays reached
 * while the agents behind it are still theirs, because the routes only show recent history;
 * the rest is read as it is now. Pure, so every rule is tested in node.
 */
import { isBuildCrew } from './portalBuild'

export const LEVELS = Object.freeze([
  {
    id: 1,
    name: 'First agent',
    means: 'You have an agent of your own on Trinity.',
    todo: 'Agree with your guide what your first agent does, then ask for it to be created.',
    why: "It's the moment an agent stops being an idea and becomes yours.",
    prompt: 'Help me reach Level 1: plan and build my first agent with me.',
  },
  {
    id: 2,
    name: 'A new playbook',
    means: 'You added a playbook to one of your agents after it was built: one more job it does the same way every time.',
    todo: 'Give one of your agents one more job: a playbook it runs the same way every time.',
    why: 'Your agents grow with your work, one job at a time.',
    prompt: 'Help me reach Level 2: add a new playbook to one of my agents.',
  },
  {
    id: 3,
    name: 'Connected',
    means: 'Every key one of your agents needs is set, and the connection was tested, so it can use your tools.',
    todo: 'Add the keys one of your agents needs, then have Atlas test the connection.',
    why: 'An agent can only reach your tools once it has their keys.',
    prompt: 'Help me reach Level 3: show me how to add the keys my agent needs.',
  },
  {
    id: 4,
    name: 'In control',
    means: 'One of your agents is shared with someone, or allowed to call another agent.',
    todo: 'Share one of your agents with someone, or let it call another of your agents.',
    why: 'You decide who can use your agents and what they can reach.',
    prompt: 'Help me reach Level 4: share one of my agents with someone, or let one of my agents call another.',
  },
  {
    id: 5,
    name: 'On a schedule',
    means: 'One of your agents has a schedule turned on, with Autonomy on, so it runs on its own.',
    todo: "Turn on one of your agents' schedules, and its Autonomy.",
    why: 'It does the work at the times you choose, without you starting it.',
    prompt: 'Help me reach Level 5: put one of my agents on a schedule.',
  },
  {
    id: 6,
    name: 'A small team',
    means: 'You have three or more agents of your own.',
    todo: 'Build up to three agents, each for a job you would give to a different person.',
    why: 'Like a team of people, each agent has its own job.',
    prompt: 'Help me reach Level 6: build up to three agents, each with its own job.',
  },
  {
    id: 7,
    name: 'Working together',
    means: 'One of your agents is allowed to call another, so they can work together.',
    todo: 'Let one of your agents call another of your agents.',
    why: 'Your agents start working as a team, not one at a time.',
    prompt: 'Help me reach Level 7: let one of my agents call another.',
  },
  {
    id: 8,
    name: 'Running on its own',
    means: 'You gave one agent a job, it handed parts of it to two of your other agents, and it asked for your OK before a step that mattered.',
    todo: 'Give one agent a job that needs two of your other agents. It hands each of them a part, then asks for your OK before the step that matters.',
    why: 'Your agents run the work, and only what needs you reaches you.',
    prompt: 'Help me reach Level 8: give one of my agents a job it hands out to two of my other agents, and have it ask for my OK on what matters.',
  },
].map((level) => Object.freeze(level)))

const MAX_AGENTS = 20
const SYSTEM_AGENT = 'trinity-system'
// Levels that rest on history the routes only show the recent part of.
const KEPT_LEVELS = new Set([3, 5, 7, 8])
// The level that needs a team of three behind it, and the one skipped when no agent needs keys.
const TEAM_SIZE = 3
const TEAM_LEVEL = 7
const KEYS_LEVEL = 3
const MAX_PLAYBOOKS = 100
const MAX_BASE_AGENTS = 40
// Saved answers from the six-level ladder (before 4 Nov) and the seven-level one (before
// 10 Nov) used other numbers. Seven's A small team (6) was three agents and a call: now
// Level 6 (three agents) and Level 7 (one may call another); its Running on its own is 8.
export const LEVELS_VERSION = 8
const SIX_TO_SEVEN = Object.freeze({ 1: [1], 2: [3], 3: [4], 4: [5], 5: [6], 6: [7] })
const SEVEN_TO_EIGHT = Object.freeze({ 1: [1], 2: [2], 3: [3], 4: [4], 5: [5], 6: [6, 7], 7: [8] })
// Where a kept level's agents go: the last of the new numbers (the one that rests on history).
const moved = (map, id) => list(map[id])
const movedKept = (map, id) => moved(map, id).slice(-1)[0]
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
  // Set and tested: a key that is in is not yet a key that works, so the newest test of
  // that agent's connections, a run of its own that the page read (`levelSignals.js`), has
  // to say every one answered. Nobody's word counts, the guide's included.
  const keyed = own.find((name) => s.keys?.[name] === 'all-set' && s.tested?.[name]?.ok === true)
  const control = own.find((name) => list(s.shares?.[name]).length > 0
    || list(s.permissions?.[name]).some((to) => to && to !== name && !isBuildCrew(to)))
  // Set to run by itself (a schedule on, and Autonomy on, which every schedule needs), or
  // already run on one: the first run can be a day away, and the step is theirs today.
  const scheduled = own.find((name) => (s.scheduled?.[name] === true && s.autonomy?.[name] === true)
    || jobs(name).some((j) => j.triggered_by === 'schedule'))
  // A team that can talk (three agents, one allowed to call another of theirs), or a call
  // already made: like Level 5, the step is theirs once it is set, not once it has happened.
  const allowed = own.flatMap((from) => list(s.permissions?.[from]).filter((to) => to !== from && mine.has(to)).map((to) => ({ from, to })))[0]
  const team = own.length >= TEAM_SIZE ? own.slice(0, TEAM_SIZE) : []
  const teamCall = team.length ? (calls[0] || allowed || null) : null
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
    6: team,
    7: teamCall ? [teamCall.from, teamCall.to] : [],
    8: lead && Number(s.answered) > 0 ? lead : [],
  }
}

// Level 3 while none of their agents needs a key and they haven't moved on yet: still a
// step, so the guide says there is nothing to connect and they agree to move on.
const KEYS_NOT_NEEDED = Object.freeze({
  todo: 'None of your agents needs a key, so there is nothing to connect. Tell Atlas, and you move on to Level 4.',
  why: 'When an agent needs a key to reach one of your tools, this is where it gets it.',
  prompt: "Level 3: none of my agents needs a key, so let's move on.",
})

// Level 3 while an agent's keys are all in but its connections have not been tested yet.
const KEYS_UNTESTED = Object.freeze({
  todo: 'Your keys are in. Ask Atlas to test the connection, so you know they work.',
  why: 'A key that is set is not yet a key that works: one test call tells you.',
  prompt: 'Level 3: my keys are in. Please test the connections.',
})

/**
 * The eight levels, each with whether it is reached now, whether it is skipped (Level 3,
 * once none of their agents needs keys and they agreed with the guide to move on:
 * `keysPassed`, the world's `keys_not_needed`), and the agents that made it true. Level 3
 * is reached once an agent's keys are all set and the newest test of its connections, a
 * run the page read itself (`signals.tested`), says every one answered. It is kept like a
 * history level (the run scrolls out of the recent list), but only while those keys stay set. `kept` is
 * what the saved answers remember (`levelsKept`): a history level stays reached while
 * every agent behind it is still theirs (and, for a team, while they still have three).
 * `base` is each agent's playbooks when the page first saw it (`levelsBase`).
 */
export function levelsFrom(signals, kept = {}, base = {}, { keysPassed = false, keysLater = false } = {}) {
  const s = signals && typeof signals === 'object' ? signals : {}
  const own = list(s.own)
  const mine = new Set(own)
  const now = evidenceNow(s, base && typeof base === 'object' ? base : {})
  const keysUntested = own.some((name) => s.keys?.[name] === 'all-set' && s.tested?.[name]?.ok !== true)
  const keptFor = (id) => {
    const names = list(kept?.[id])
    if (!KEPT_LEVELS.has(id) || !names.length || !names.every((n) => mine.has(n))) return false
    // A tested connection stays tested only while its keys are still all in, and until a newer test fails.
    if (id === KEYS_LEVEL) return names.every((n) => s.keys?.[n] === 'all-set' && s.tested?.[n]?.ok !== false)
    return id !== TEAM_LEVEL || own.length >= TEAM_SIZE
  }
  const keysNotNeeded = own.length > 0 && own.every((name) => s.keys?.[name] === 'needs-none')
  return LEVELS.map((level) => {
    const reached = now[level.id].length > 0 || keptFor(level.id)
    const keysOpen = level.id === KEYS_LEVEL && !reached && keysNotNeeded
    return {
      ...level,
      ...(keysOpen && !keysPassed ? KEYS_NOT_NEEDED : {}),
      ...(level.id === KEYS_LEVEL && !reached && keysUntested ? KEYS_UNTESTED : {}),
      reached,
      skipped: keysOpen && keysPassed,
      // Left for later with the guide (the world's `keys_later`): no working keys at hand yet.
      // Not a tick and not counted; it only lets the next level be the one after.
      deferred: level.id === KEYS_LEVEL && !reached && !keysOpen && Boolean(keysLater) && own.length > 0,
      // The card offers "Do this later" while Level 3 is open and has keys to wait for.
      canDefer: level.id === KEYS_LEVEL && !reached && !keysOpen && !keysLater && own.length > 0,
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
 * The levels part of the saved answers, in the eight-level numbers: answers saved by the
 * six-level and seven-level versions are moved up (Connected was 2, now 3; Running on its
 * own was 7, now 8; and so on), and the levels that are now read as they are are no longer kept.
 */
export function levelsOf(intake) {
  const i = objectOr(intake)
  const seen = list(i.levelsSeen)
  const kept = objectOr(i.levelsKept)
  const levelsBase = objectOr(i.levelsBase)
  if (i.levelsVersion === LEVELS_VERSION) return { levelsSeen: seen, levelsKept: kept, levelsBase, levelsVersion: LEVELS_VERSION }
  const steps = i.levelsVersion === 7 ? [SEVEN_TO_EIGHT] : [SIX_TO_SEVEN, SEVEN_TO_EIGHT]
  let nextSeen = seen
  let nextKept = kept
  for (const map of steps) {
    nextSeen = [...new Set(nextSeen.flatMap((id) => moved(map, id)))]
    nextKept = Object.fromEntries(Object.entries(nextKept).map(([id, names]) => [movedKept(map, id), names]).filter(([id]) => id))
  }
  return {
    levelsSeen: nextSeen,
    levelsKept: Object.fromEntries(Object.entries(nextKept).filter(([id]) => KEPT_LEVELS.has(Number(id)))),
    levelsBase,
    levelsVersion: LEVELS_VERSION,
  }
}

/** The saved answers with their levels part in the eight-level numbers, then `patch`. */
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

/** The lowest level neither reached, skipped nor left for later, or null when there is none. */
export function nextLevel(levels) {
  return list(levels).find((l) => !l.reached && !l.skipped && !l.deferred) || null
}

/** Levels reached now that the page hasn't celebrated before. A skipped level is never one. */
export function newlyReached(levels, seen) {
  const done = new Set(list(seen))
  return list(levels).filter((l) => l.reached && !done.has(l.id)).map((l) => l.id)
}

/**
 * Where they are, for the guide (`fleet-state.json`): it reads this rather than counting,
 * since levels are not reached in order. Null before the page has read them.
 */
export function levelsSummary(levels) {
  const all = list(levels)
  if (!all.length) return null
  const ids = (pick) => all.filter(pick).map((l) => l.id)
  return { reached: ids((l) => l.reached), later: ids((l) => l.deferred), not_needed: ids((l) => l.skipped), next: nextLevel(all)?.id ?? null }
}

/** What the guide is told when they leave the keys for later on the card: the level, then the real next one. */
export function laterMessage(levels) {
  const level = LEVELS.find((l) => l.id === KEYS_LEVEL)
  const next = nextLevel(list(levels).map((l) => (l.id === KEYS_LEVEL ? { ...l, deferred: true } : l)))
  return `Level ${level.id}, ${level.name}: left for later.\n${next ? `Next: Level ${next.id}, ${next.name}.` : 'That was the last level.'}`
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
