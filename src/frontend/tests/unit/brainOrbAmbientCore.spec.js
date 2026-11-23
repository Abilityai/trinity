import { describe, it, expect } from 'vitest'
import {
  createNoteMatcher,
  classifyTool,
  createClassifier,
  runName,
  summarizeRuns,
  deriveMood,
  MOODS,
} from '../../public/brain-orb/ambient-core.js'

// Brain Orb ambient mode (trinity-enterprise#1302). The orb page itself cannot be
// loaded by vitest (WebGL at module top level), so everything that decides WHAT is
// shown — and what is withheld — lives in this DOM-free module.

const IDS = [
  'Books/risk/Ambiguity Aversion.md',
  '02-Permanent/Dopamine.md',
  'Meetings/Notes.md',
  'Books/Notes.md',
  'Root Note.md',
]

describe('note matcher', () => {
  const m = createNoteMatcher(IDS)

  it('maps an absolute tool path to the graph node it ends with', () => {
    expect(m.match('/home/developer/Brain/02-Permanent/Dopamine.md')).toBe('02-Permanent/Dopamine.md')
    expect(m.match('/vault/Books/risk/Ambiguity Aversion.md')).toBe('Books/risk/Ambiguity Aversion.md')
  })

  it('accepts the exact id and a root-level note', () => {
    expect(m.match('02-Permanent/Dopamine.md')).toBe('02-Permanent/Dopamine.md')
    expect(m.match('/home/developer/Brain/Root Note.md')).toBe('Root Note.md')
  })

  it('discards an ambiguous bare name instead of guessing', () => {
    expect(m.match('Notes.md')).toBeNull()
    expect(m.match('/x/Meetings/Notes.md')).toBe('Meetings/Notes.md')
  })

  it('does not match on a partial folder name', () => {
    expect(m.match('/x/NotBooks/Notes.md')).toBeNull()
  })

  it('ignores non-notes and junk', () => {
    expect(m.match('/home/developer/.env')).toBeNull()
    expect(m.match('')).toBeNull()
    expect(m.match(null)).toBeNull()
  })

  it('finds only full node ids in free text, capped', () => {
    const text = [
      '0.91  /home/developer/Brain/02-Permanent/Dopamine.md',
      '0.80  Books/risk/Ambiguity Aversion.md  (chunk 2)',
      'see Notes.md and also Unknown/Thing.md',
    ].join('\n')
    expect(m.scan(text)).toEqual(['02-Permanent/Dopamine.md', 'Books/risk/Ambiguity Aversion.md'])
    expect(m.scan(text, 1)).toHaveLength(1)
    expect(m.scan('')).toEqual([])
  })
})

describe('tool classification — what reaches the screen', () => {
  const m = createNoteMatcher(IDS)

  it('names a note that is read or written', () => {
    expect(classifyTool('Read', { file_path: '/b/02-Permanent/Dopamine.md' }, m)).toMatchObject({
      kind: 'read', state: 'reading', label: 'Dopamine', notes: ['02-Permanent/Dopamine.md'],
    })
    expect(classifyTool('Edit', { file_path: '/b/02-Permanent/Dopamine.md' }, m)).toMatchObject({
      kind: 'write', state: 'writing', label: 'Dopamine',
    })
  })

  it('never shows the name of a file that is not a note', () => {
    const ev = classifyTool('Read', { file_path: '/home/developer/.env' }, m)
    expect(ev.label).toBe('a file')
    expect(JSON.stringify(ev)).not.toContain('.env')
  })

  it('shows the agent-written step description, never the command', () => {
    const ev = classifyTool('Bash', { command: 'curl -H "Authorization: Bearer TOKEN-PLACEHOLDER" https://x', description: 'Fetch the feed' }, m)
    expect(ev.label).toBe('Fetch the feed')
    expect(JSON.stringify(ev)).not.toContain('TOKEN-PLACEHOLDER')
    expect(classifyTool('Bash', { command: 'cat /etc/passwd' }, m).label).toBe('running a command')
  })

  it('withholds search queries, patterns and web targets', () => {
    for (const [name, input] of [
      ['Bash', { command: './run_search.sh "my private question"', description: 'Search memory' }],
      ['Grep', { pattern: 'my private question' }],
      ['WebSearch', { query: 'my private question' }],
      ['WebFetch', { url: 'https://example.com/my-private-question' }],
    ]) {
      expect(JSON.stringify(classifyTool(name, input, m))).not.toMatch(/private/)
    }
    expect(classifyTool('Bash', { command: './run_search.sh "q"' }, m).state).toBe('searching')
  })

  it('names a playbook by its name only', () => {
    expect(classifyTool('Skill', { skill: 'daily-digest', args: 'secret args' }, m)).toMatchObject({
      state: 'skill', label: '/daily-digest',
    })
    expect(JSON.stringify(classifyTool('Skill', { skill: 'daily-digest', args: 'secret args' }, m))).not.toContain('secret')
  })

  it('falls back to a humanised tool name', () => {
    expect(classifyTool('mcp__trinity__list_agents', {}, m)).toMatchObject({ state: 'coordinating', label: 'list agents' })
    expect(classifyTool('SomethingNew', {}, m)).toMatchObject({ state: 'working', label: 'SomethingNew' })
  })
})

describe('stream classifier', () => {
  const mk = () => createClassifier(createNoteMatcher(IDS))
  const assistant = (...content) => ({ type: 'assistant', message: { content } })
  const result = (id, content) => ({ type: 'user', message: { content: [{ type: 'tool_result', tool_use_id: id, content }] } })

  it('turns reasoning and replies into bare states with no text', () => {
    const evs = mk().feed(assistant(
      { type: 'thinking', thinking: 'the user secretly wants X' },
      { type: 'text', text: 'Here is the secret answer' },
    ))
    expect(evs.map(e => e.state)).toEqual(['thinking', 'speaking'])
    expect(JSON.stringify(evs)).not.toMatch(/secret/)
  })

  it('never carries prompt text', () => {
    const evs = mk().feed({ type: 'user', message: { content: 'tell me about my salary' } })
    expect(evs).toHaveLength(1)
    expect(evs[0].state).toBe('listening')
    expect(JSON.stringify(evs)).not.toContain('salary')
  })

  it('lights the notes a search reached, from its result', () => {
    const c = mk()
    c.feed(assistant({ type: 'tool_use', id: 't1', name: 'Bash', input: { command: './run_search.sh "q"' } }))
    const evs = c.feed(result('t1', [{ type: 'text', text: '0.9 /b/02-Permanent/Dopamine.md\n0.8 Books/risk/Ambiguity Aversion.md' }]))
    expect(evs).toEqual([expect.objectContaining({
      kind: 'recall-notes', state: 'searching', notes: ['02-Permanent/Dopamine.md', 'Books/risk/Ambiguity Aversion.md'],
    })])
  })

  it('does not scan the result of a plain read', () => {
    const c = mk()
    c.feed(assistant({ type: 'tool_use', id: 't2', name: 'Read', input: { file_path: '/b/02-Permanent/Dopamine.md' } }))
    expect(c.feed(result('t2', 'links to Books/risk/Ambiguity Aversion.md'))).toEqual([])
  })

  it('survives malformed entries', () => {
    const c = mk()
    for (const junk of [null, undefined, 'x', 42, {}, { type: 'assistant' }, { type: 'user', message: {} }, { type: 'assistant', message: { content: [null, 7] } }]) {
      expect(c.feed(junk)).toEqual([])
    }
  })
})

describe('run name redaction', () => {
  const schedules = new Map([['s1', 'Morning digest']])

  it('names a playbook by its command only', () => {
    expect(runName({ message: '/daily-digest for the board, confidential', triggered_by: 'manual' }, schedules)).toBe('/daily-digest')
  })
  it('names a scheduled run by its schedule', () => {
    expect(runName({ message: 'Summarise everything about ACME', schedule_id: 's1', triggered_by: 'schedule' }, schedules)).toBe('Morning digest')
  })
  it('never echoes a free-text prompt', () => {
    expect(runName({ message: 'what is my salary', triggered_by: 'chat' }, schedules)).toBe('a chat turn')
    expect(runName({ message: 'do the thing', triggered_by: 'schedule', schedule_id: 'gone' }, schedules)).toBe('a scheduled task')
    expect(runName({ message: 'do the thing', triggered_by: 'mcp' }, schedules)).toBe('a task')
    expect(runName({}, schedules)).toBe('a task')
  })
})

describe('run summary', () => {
  const now = Date.parse('2026-11-19T12:00:00Z')
  const run = (o) => ({ status: 'success', started_at: '2026-11-19T10:00:00Z', completed_at: '2026-11-19T10:05:00Z', ...o })

  it('aggregates the last 24h and finds the running and last runs', () => {
    const s = summarizeRuns([
      run({ id: 'a', status: 'running', started_at: '2026-11-19T11:59:00Z', completed_at: null, context_used: 90, context_max: 100 }),
      run({ id: 'b', cost: 0.5 }),
      run({ id: 'c', status: 'failed', cost: 0.25 }),
      run({ id: 'd', started_at: '2026-11-10T10:00:00Z', completed_at: '2026-11-10T10:01:00Z', cost: 9 }),
    ], now)
    expect(s.running.id).toBe('a')
    expect(s.last.id).toBe('b')
    expect(s.runs24h).toBe(3)
    expect(s.failed24h).toBe(1)
    expect(s.cost24h).toBeCloseTo(0.75)
    expect(s.contextPct).toBeCloseTo(0.9)
    expect(s.recentFailures).toBe(1)
  })

  it('treats naive timestamps as UTC', () => {
    const s = summarizeRuns([run({ id: 'b', started_at: '2026-11-19T10:00:00', completed_at: '2026-11-19T10:05:00' })], now)
    expect(s.lastEndedAt).toBe(Date.parse('2026-11-19T10:05:00Z'))
  })

  it('is empty, not broken, with no runs', () => {
    const s = summarizeRuns([], now)
    expect(s.running).toBeNull()
    expect(s.last).toBeNull()
    expect(s.runs24h).toBe(0)
    expect(s.contextPct).toBeNull()
  })
})

describe('mood ladder — first match wins', () => {
  const now = Date.parse('2026-11-19T12:00:00Z')
  const base = {
    readable: true, agentStatus: 'running', lastFailed: false, recentFailures: 0,
    contextPct: 0.2, running: false, queued: 0, lastWriteAt: null,
    lastRunAt: now - 3600e3, now,
  }
  const word = (o) => deriveMood({ ...base, ...o }).word

  it('reads unknown rather than guessing', () => {
    expect(word({ readable: false })).toBe('unknown')
    expect(word({ agentStatus: null })).toBe('unknown')
  })
  it('follows the approved order', () => {
    expect(word({ agentStatus: 'stopped', lastFailed: true })).toBe('asleep')
    expect(word({ lastFailed: true, running: true })).toBe('struggling')
    expect(word({ recentFailures: 2, contextPct: 0.9 })).toBe('struggling')
    expect(word({ contextPct: 0.85, running: true })).toBe('strained')
    expect(word({ running: true, queued: 3 })).toBe('working')
    expect(word({ queued: 1, lastWriteAt: now - 60e3 })).toBe('backed up')
    expect(word({ lastWriteAt: now - 14 * 60e3 })).toBe('digesting')
    expect(word({ lastWriteAt: now - 16 * 60e3 })).toBe('resting')
    expect(word({ lastRunAt: now - 8 * 86400e3 })).toBe('neglected')
    expect(word({ lastRunAt: null })).toBe('neglected')
    expect(word({})).toBe('resting')
  })
  it('says what it is derived from', () => {
    for (const o of [{}, { readable: false }, { running: true }, { lastFailed: true }, { contextPct: 0.9 }]) {
      const m = deriveMood({ ...base, ...o })
      expect(m.why.length).toBeGreaterThan(8)
      expect(MOODS[m.word]).toBeTruthy()
    }
    expect(deriveMood({ ...base, contextPct: 0.9 }).why).toContain('90%')
  })
})
