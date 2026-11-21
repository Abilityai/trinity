/*
 * Brain Orb — ambient mode, the DOM-free half (trinity-enterprise#1302).
 *
 * Everything that decides WHAT the watch-only screen shows, and what it withholds,
 * lives here so it can be unit-tested without WebGL (tests/unit/brainOrbAmbientCore.spec.js).
 * ambient-mode.js owns the drawing and the network loop.
 *
 * Shown: a state verb, note titles, a playbook's name, the agent's own step description.
 * Never shown: prompt text, reply text, reasoning, raw commands, search queries, file
 * names that are not notes, web addresses. The screen may be on a wall.
 */

/* state -> [verb, colour, the orb's own visual state to borrow] */
export const STATES = {
  resting:      ['resting',              '#4a6fa5', 'idle'],
  replaying:    ['replaying',            '#7a6cf0', 'idle'],
  listening:    ['listening',            '#aee9ff', 'listening'],
  thinking:     ['thinking',             '#8a7bff', 'thinking'],
  searching:    ['searching memory',     '#3cf0b0', 'thinking'],
  reading:      ['reading',              '#63b3ed', 'idle'],
  scanning:     ['scanning files',       '#63b3ed', 'idle'],
  writing:      ['writing a note',       '#f0b100', 'thinking'],
  building:     ['building',             '#ffae5c', 'idle'],
  working:      ['working',              '#ffae5c', 'idle'],
  browsing:     ['reading the web',      '#ff7eb6', 'idle'],
  delegating:   ['delegating',           '#a78bfa', 'idle'],
  coordinating: ['talking to the platform', '#a78bfa', 'idle'],
  skill:        ['running a playbook',   '#00e0a0', 'thinking'],
  committing:   ['saving work',          '#00bc7d', 'idle'],
  speaking:     ['answering',            '#ffd9a0', 'speaking'],
}

export const titleOf = (id) => String(id || '').split('/').pop().replace(/\.md$/i, '')

/* ------------------------------------------------------------------ notes */
/**
 * Maps a path a tool touched to a graph node id. Generic: the vault root is not
 * known, so a path matches the node id it ENDS with, at a "/" boundary. A name
 * that fits more than one node is discarded — a wrong note lit is worse than none.
 */
export function createNoteMatcher(ids) {
  const all = new Set()
  const byBase = new Map()
  for (const id of ids || []) {
    if (typeof id !== 'string' || !id) continue
    all.add(id)
    const base = id.split('/').pop()
    if (!byBase.has(base)) byBase.set(base, [])
    byBase.get(base).push(id)
  }

  function match(path) {
    if (typeof path !== 'string' || !path) return null
    const p = path.replace(/\\/g, '/')
    if (all.has(p)) return p
    const cands = byBase.get(p.split('/').pop())
    if (!cands) return null
    const hits = cands.filter((id) => p.endsWith('/' + id))
    return hits.length === 1 ? hits[0] : null
  }

  /** Full node ids that appear in free text (a search result), in order, capped. */
  function scan(text, cap = 12) {
    const out = []
    if (typeof text !== 'string' || !text) return out
    const lines = text.split('\n', 400)
    for (const line of lines) {
      let from = 0
      for (;;) {
        const at = line.toLowerCase().indexOf('.md', from)
        if (at < 0) break
        from = at + 3
        const seg = line.slice(Math.max(0, at - 400), at + 3)
        const slash = seg.lastIndexOf('/')
        if (slash < 0) continue
        const cands = byBase.get(seg.slice(slash + 1))
        if (!cands) continue
        const hits = cands.filter((id) => {
          if (!seg.endsWith(id)) return false
          const before = seg[seg.length - id.length - 1]
          return before === undefined || /[\s/"'`(\[|:=,]/.test(before)
        })
        if (hits.length === 1 && !out.includes(hits[0])) {
          out.push(hits[0])
          if (out.length >= cap) return out
        }
      }
    }
    return out
  }

  return { match, scan, size: all.size }
}

/* ------------------------------------------------------------------ tools */
const SEARCH_CMD = /\b(run_search|run_connections|search\.py|brain[-_]search|local-brain-search)\b/
const SAVE_CMD = /\bgit\s+(commit|push|add)\b/
const human = (name) => String(name || 'a tool').split('__').pop().replace(/_/g, ' ')
const clip = (s, n = 80) => { s = String(s || '').split('\n')[0].trim(); return s.length > n ? s.slice(0, n - 1) + '…' : s }

/** One tool call -> {kind, state, label, notes, scan}. `scan`: its result may list notes. */
export function classifyTool(name, input, matcher) {
  const inp = input && typeof input === 'object' ? input : {}
  const ev = (kind, state, label, notes = [], scan = false) => ({ kind, state, label, notes, scan })
  const note = () => (matcher ? matcher.match(inp.file_path || inp.notebook_path || inp.path) : null)

  if (name === 'Read' || name === 'NotebookRead') {
    const id = note()
    return id ? ev('read', 'reading', titleOf(id), [id]) : ev('read', 'reading', 'a file')
  }
  if (name === 'Write' || name === 'Edit' || name === 'MultiEdit' || name === 'NotebookEdit') {
    const id = note()
    return id ? ev('write', 'writing', titleOf(id), [id]) : ev('build', 'building', 'a file')
  }
  if (name === 'Bash') {
    const cmd = String(inp.command || '')
    const desc = clip(inp.description)
    if (SEARCH_CMD.test(cmd)) return ev('recall', 'searching', '', [], true)
    if (SAVE_CMD.test(cmd)) return ev('commit', 'committing', desc)
    return ev('work', 'working', desc || 'running a command', [], true)
  }
  if (name === 'Grep' || name === 'Glob') return ev('scan', 'scanning', '', [], true)
  if (name === 'Skill') return ev('skill', 'skill', '/' + clip(String(inp.skill || 'playbook').split(/\s/)[0], 40))
  if (name === 'WebSearch' || name === 'WebFetch') return ev('web', 'browsing', '')
  if (name === 'Agent' || name === 'Task') return ev('delegate', 'delegating', clip(inp.description) || 'a sub-agent')
  if (name === 'TodoWrite') return ev('plan', 'thinking', 'updating its plan')
  if (typeof name === 'string' && name.startsWith('mcp__trinity__')) {
    const tool = human(name)
    if (/^(chat with agent|fan out|send message|call a2a agent)$/.test(tool)) return ev('delegate', 'delegating', tool)
    return ev('platform', 'coordinating', tool)
  }
  if (typeof name === 'string' && (name.startsWith('mcp__playwright__') || /browser/i.test(name))) return ev('web', 'browsing', '')
  return ev('work', 'working', clip(human(name), 40), [], typeof name === 'string' && name.startsWith('mcp__'))
}

const resultText = (c) => {
  if (typeof c === 'string') return c
  if (!Array.isArray(c)) return ''
  return c.map((b) => (b && typeof b.text === 'string' ? b.text : '')).join('\n')
}

/**
 * Feed raw execution-log entries (Claude Code stream-json), get typed events.
 * Holds a small map of open tool calls so a search's RESULT can light the notes it found.
 */
export function createClassifier(matcher) {
  const open = new Map()   // tool_use_id -> state, only for calls whose result is worth scanning

  function feed(entry) {
    const out = []
    if (!entry || typeof entry !== 'object') return out
    const content = entry.message && entry.message.content
    if (entry.type === 'assistant' && Array.isArray(content)) {
      for (const b of content) {
        if (!b || typeof b !== 'object') continue
        if (b.type === 'thinking') out.push({ kind: 'think', state: 'thinking', label: '', notes: [] })
        else if (b.type === 'text' && String(b.text || '').trim()) out.push({ kind: 'speak', state: 'speaking', label: '', notes: [] })
        else if (b.type === 'tool_use') {
          const { scan, ...ev } = classifyTool(b.name, b.input, matcher)
          if (scan && b.id) {
            open.set(b.id, ev.state === 'searching' || ev.state === 'scanning' ? ev.state : 'searching')
            if (open.size > 200) open.delete(open.keys().next().value)
          }
          out.push(ev)
        }
      }
    } else if (entry.type === 'user') {
      if (typeof content === 'string') {
        if (content.trim()) out.push({ kind: 'listen', state: 'listening', label: '', notes: [] })
      } else if (Array.isArray(content)) {
        let prompt = false
        for (const b of content) {
          if (!b || typeof b !== 'object') continue
          if (b.type === 'tool_result') {
            if (!open.has(b.tool_use_id)) continue
            open.delete(b.tool_use_id)
            const notes = matcher ? matcher.scan(resultText(b.content)) : []
            if (notes.length) out.push({ kind: 'recall-notes', state: 'searching', label: '', notes })
          } else if (b.type === 'text') prompt = true
        }
        if (prompt && !out.length) out.push({ kind: 'listen', state: 'listening', label: '', notes: [] })
      }
    }
    return out
  }

  return { feed }
}

/* ------------------------------------------------------------------- runs */
/** ISO string (naive = UTC, the backend's historical shape) -> epoch ms, or null. */
export function parseTs(s) {
  if (!s || typeof s !== 'string') return null
  const iso = /[zZ]|[+-]\d\d:?\d\d$/.test(s) ? s : s + 'Z'
  const t = Date.parse(iso)
  return Number.isFinite(t) ? t : null
}

/** A run's display name. A free-text prompt is never echoed — it may be someone's words. */
export function runName(run, scheduleNames) {
  const r = run || {}
  const first = String(r.message || '').trim().split('\n')[0]
  if (first.startsWith('/')) return clip(first.split(/\s/)[0], 48)
  const sched = scheduleNames && r.schedule_id ? scheduleNames.get(r.schedule_id) : null
  if (sched) return clip(sched, 48)
  const by = String(r.triggered_by || '')
  if (by === 'schedule') return 'a scheduled task'
  if (by === 'chat' || by === 'user' || by === 'voice' || by === 'public') return 'a chat turn'
  return 'a task'
}

const LIVE = new Set(['running', 'pending', 'queued'])

export function summarizeRuns(runs, now) {
  const list = Array.isArray(runs) ? runs : []
  const day = now - 86400e3
  const running = list.find((r) => r.status === 'running') || null
  const done = list.filter((r) => !LIVE.has(r.status))
  const last = done[0] || null
  const recent = list.filter((r) => (parseTs(r.started_at) || 0) > day)
  const ctxOf = (r) => (r && r.context_used && r.context_max ? r.context_used / r.context_max : null)
  return {
    running,
    last,
    lastEndedAt: last ? parseTs(last.completed_at) || parseTs(last.started_at) : null,
    lastRunAt: list.length ? parseTs(list[0].started_at) : null,
    runs24h: recent.length,
    failed24h: recent.filter((r) => r.status === 'failed').length,
    cost24h: recent.reduce((s, r) => s + (r.cost || 0), 0),
    contextPct: ctxOf(running) ?? ctxOf(last),
    lastFailed: !!last && last.status === 'failed',
    recentFailures: done.slice(0, 5).filter((r) => r.status === 'failed').length,
    queuedInList: list.filter((r) => r.status === 'pending' || r.status === 'queued').length,
  }
}

/* ------------------------------------------------------------------- mood */
/* word -> [colour, what it means]. One explainable ladder; first match wins. */
export const MOODS = {
  unknown:     ['#55607a', 'the signals could not be read'],
  asleep:      ['#55607a', 'the agent is stopped'],
  struggling:  ['#ff5c7a', 'recent runs failed'],
  strained:    ['#f0b100', 'the context window is nearly full'],
  working:     ['#00e0a0', 'a run is in progress'],
  'backed up': ['#ffae5c', 'work is waiting in the queue'],
  digesting:   ['#3cf0b0', 'it wrote notes in the last 15 minutes'],
  neglected:   ['#7f97c2', 'no run in the last 7 days'],
  resting:     ['#4a6fa5', 'idle and healthy'],
}

export const MOOD_LEGEND =
  'Mood is derived, first match wins: unknown (signals unreadable) · asleep (agent stopped) · ' +
  'struggling (last run failed, or 2 of the last 5) · strained (context at 85% or more) · ' +
  'working (a run in progress) · backed up (work queued) · digesting (wrote notes in the last 15 min) · ' +
  'neglected (no run in 7 days) · resting.'

export function deriveMood(sig) {
  const s = sig || {}
  const mood = (word, why) => ({ word, tone: MOODS[word][0], why })
  if (!s.readable || !s.agentStatus) return mood('unknown', 'the platform signals could not be read')
  if (s.agentStatus !== 'running') return mood('asleep', `the agent is ${s.agentStatus}`)
  if (s.lastFailed) return mood('struggling', 'the last run failed')
  if ((s.recentFailures || 0) >= 2) return mood('struggling', `${s.recentFailures} of the last 5 runs failed`)
  if (s.contextPct != null && s.contextPct >= 0.85) return mood('strained', `context is ${Math.round(s.contextPct * 100)}% full`)
  if (s.running) return mood('working', 'a run is in progress')
  if ((s.queued || 0) > 0) return mood('backed up', `${s.queued} waiting in the queue`)
  if (s.lastWriteAt && s.now - s.lastWriteAt < 15 * 60e3) return mood('digesting', 'it wrote notes in the last 15 minutes')
  if (!s.lastRunAt || s.now - s.lastRunAt > 7 * 86400e3) return mood('neglected', s.lastRunAt ? 'no run in the last 7 days' : 'no run on record')
  return mood('resting', 'idle, last runs healthy')
}

/* ------------------------------------------------------------- formatting */
export const ago = (ms) => {
  const s = Math.max(0, ms / 1000)
  return s < 90 ? `${Math.max(1, Math.round(s))}s` : s < 5400 ? `${Math.round(s / 60)}m` : s < 129600 ? `${Math.round(s / 3600)}h` : `${Math.round(s / 86400)}d`
}
export const fmt = (n) => (n == null ? '–' : Number(n).toLocaleString('en-US'))
