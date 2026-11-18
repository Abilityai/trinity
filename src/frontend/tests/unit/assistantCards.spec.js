import { describe, it, expect, vi } from 'vitest'
import { CARD_ACTIONS, MAX_CARDS_PER_REPLY, acceptCards, buildOutcome, cardLabel, cardNote, outcomeLine, runCard, toolWords, validBuild, validCard, validJob, validTool } from '@/utils/assistantCards'

const fleet = [
  { name: 'issue-digest', schedules: [{ id: 's1', name: 'Mondays 09:00' }] },
  { name: 'scout', schedules: [] },
]
const DESIGN_ID = '8f8f7d58-8429-4c25-be28-1e6e6bfc82dc'
const build = (over = {}) => ({
  action: 'build', design_id: DESIGN_ID,
  design: { title: 'Front desk', summary: 'Reads the inbox.', to_do_after: ['Add the mailbox credential.'],
    agents: [{ name: 'shop-desk', job: 'Reads the inbox.', credentials: ['MAILBOX_READ_CREDENTIAL'], schedule: { name: 'Morning', cron: '0 8 * * 1-5' }, calls: [] }] },
  ...over,
})

describe('assistant cards: what a reply may propose', () => {
  it('accepts a listed change that names agents the page itself read', () => {
    const cards = acceptCards([{ action: 'schedule-on', agent: 'issue-digest', schedule_id: 's1' }], fleet)
    expect(cards).toEqual([{ action: 'schedule-on', agent: 'issue-digest', schedule_id: 's1' }])
    expect(cardLabel(cards[0], fleet)).toBe('Turn on the schedule "Mondays 09:00" for issue-digest')
    expect(cardNote(cards[0])).toBe('Runs as you, not as the assistant.')
  })

  it('drops an unknown action, an unknown agent, extra fields and anything that is not a list', () => {
    expect(acceptCards([{ action: 'delete-agent', agent: 'scout' }], fleet)).toEqual([])
    expect(acceptCards([{ action: 'start-agent', agent: 'nobody' }], fleet)).toEqual([])
    expect(acceptCards([{ action: 'start-agent', agent: 'scout', also: 'x' }], fleet)).toEqual([])
    expect(acceptCards('start-agent', fleet)).toEqual([])
    expect(acceptCards(null, fleet)).toEqual([])
  })

  it('keeps at most the per-reply limit and drops an exact repeat', () => {
    const start = { action: 'start-agent', agent: 'scout' }
    const many = [start, start, { action: 'stop-agent', agent: 'scout' }, { action: 'autonomy-on', agent: 'scout' }, { action: 'autonomy-off', agent: 'scout' }]
    expect(acceptCards(many, fleet)).toHaveLength(MAX_CARDS_PER_REPLY)
    expect(acceptCards([start, start], fleet)).toHaveLength(1)
  })

  it('accepts the subscription card only with no fields', () => {
    expect(acceptCards([{ action: 'upgrade' }], fleet)).toEqual([{ action: 'upgrade' }])
    expect(acceptCards([{ action: 'upgrade', plan: 'free-forever' }], fleet)).toEqual([])
    expect(cardLabel({ action: 'upgrade' })).toBe('Subscribe to Trinity Pro')
  })
})

describe('assistant cards: a design to build', () => {
  it('accepts a design with a well-formed id and keeps only what the person is shown', () => {
    const card = validBuild(build({ design: { ...build().design, files: { 'CLAUDE.md': 'x' }, agents: [{ ...build().design.agents[0], files: { a: 'b' } }] } }))
    expect(card.design_id).toBe(DESIGN_ID)
    expect(card.design.agents[0]).toEqual({ name: 'shop-desk', job: 'Reads the inbox.', credentials: ['MAILBOX_READ_CREDENTIAL'], schedule: { name: 'Morning', cron: '0 8 * * 1-5' }, calls: [] })
    expect(JSON.stringify(card)).not.toContain('CLAUDE.md')
    expect(cardLabel(card)).toBe('Build "Front desk"')
  })

  it('refuses an id that could be a path, and a design with no agents', () => {
    expect(validBuild(build({ design_id: '../../agents/x' }))).toBeNull()
    expect(validBuild(build({ design: { title: 't', agents: [] } }))).toBeNull()
    expect(acceptCards([build({ design_id: 'nope' })], fleet)).toEqual([])
  })

  it('tells the assistant what exists after a clean build, schedule id included', () => {
    const out = buildOutcome(validBuild(build()), { agents: [{ name: 'shop-desk', created: true, files: 11, schedule: { id: 's9', name: 'Morning' }, problem: '' }] })
    expect(out.kind).toBe('done')
    expect(out.line).toBe('Done: Build "Front desk". Built shop-desk (11 files, schedule "Morning" added switched off, id s9).')
  })

  it('is unsure when some agents were built and some were not, and says which', () => {
    const out = buildOutcome(validBuild(build()), { agents: [
      { name: 'one', created: true, files: 3, problem: '' },
      { name: 'two', created: false, files: 0, problem: 'the agent could not be created: name taken' }] })
    expect(out.kind).toBe('unsure')
    expect(out.line).toContain('Built one (3 files).')
    expect(out.line).toContain('two: the agent could not be created: name taken')
  })

  it('is refused when nothing was built, and unsure when nothing was reported', () => {
    expect(buildOutcome(validBuild(build()), { agents: [{ name: 'one', created: false, problem: 'no' }] }).kind).toBe('refused')
    expect(buildOutcome(validBuild(build()), {}).kind).toBe('unsure')
  })
})

describe('assistant cards: making a confirmed change', () => {
  const http = (over = {}) => ({
    get: vi.fn(async () => ({ data: [{ id: 's1', enabled: true }] })),
    post: vi.fn(async () => ({ data: {} })),
    put: vi.fn(async () => ({ data: {} })),
    delete: vi.fn(async () => ({ data: {} })),
    ...over,
  })
  const card = { action: 'schedule-on', agent: 'issue-digest', schedule_id: 's1' }

  it('calls the app route once and reports done only after reading it back', async () => {
    const h = http()
    expect(await runCard(h, card, fleet)).toEqual({ kind: 'done' })
    expect(h.post).toHaveBeenCalledTimes(1)
    expect(h.post.mock.calls[0][0]).toBe('/api/agents/issue-digest/schedules/s1/enable')
    expect(h.get).toHaveBeenCalledWith('/api/agents/issue-digest/schedules')
  })

  it('says it is unsure when the write answered but the read-back disagrees', async () => {
    const h = http({ get: vi.fn(async () => ({ data: [{ id: 's1', enabled: false }] })) })
    expect((await runCard(h, card, fleet)).kind).toBe('unsure')
  })

  it('reports a 4xx as refused, with the server words', async () => {
    const h = http({ post: vi.fn(async () => { throw { response: { status: 403, data: { detail: 'Not allowed' } } } }) })
    expect(await runCard(h, card, fleet)).toEqual({ kind: 'refused', reason: 'Not allowed' })
    expect(h.get).not.toHaveBeenCalled()
  })

  it.each([[{ response: { status: 502 } }], [new Error('network')]])('reports a 5xx or no answer as unsure', async (err) => {
    const h = http({ post: vi.fn(async () => { throw err }) })
    expect((await runCard(h, card, fleet)).kind).toBe('unsure')
  })

  it('never calls anything for a card that is no longer valid', async () => {
    const h = http()
    const result = await runCard(h, { action: 'start-agent', agent: 'gone' }, fleet)
    expect(result.kind).toBe('refused')
    expect(h.post).not.toHaveBeenCalled()
  })

  it('escapes names in the path', async () => {
    const odd = [{ name: 'a/b', schedules: [] }]
    const h = http({ get: vi.fn(async () => ({ data: { status: 'running' } })) })
    await runCard(h, { action: 'start-agent', agent: 'a/b' }, odd)
    expect(h.post.mock.calls[0][0]).toBe('/api/agents/a%2Fb/start')
  })

  it('every action in the catalog has a route, a label and a read-back', () => {
    for (const [name, spec] of Object.entries(CARD_ACTIONS)) {
      const sample = Object.fromEntries(spec.fields.map((f) => [f, f === 'email' ? 'a@b.co' : 'x']))
      expect(spec.call(sample)[1], name).toMatch(/^\/api\/agents\/x/)
      expect(spec.label(sample, []), name).toBeTruthy()
      expect(typeof spec.held, name).toBe('function')
    }
  })
})

describe('assistant cards: what the assistant is told', () => {
  it('writes the four outcome lines the assistant was taught', () => {
    expect(outcomeLine('done', 'Start scout')).toBe('Done: Start scout.')
    expect(outcomeLine('declined', 'Start scout')).toBe('Not now: Start scout.')
    expect(outcomeLine('refused', 'Start scout', 'Not allowed')).toBe("That didn't work: Start scout. Not allowed")
    expect(outcomeLine('unsure', 'Start scout', 'No answer')).toBe('Not sure it worked: Start scout. No answer')
  })

  it('validCard returns a copy holding only the catalog fields', () => {
    const raw = { action: 'start-agent', agent: 'scout' }
    const card = validCard(raw, fleet)
    expect(card).toEqual(raw)
    expect(card).not.toBe(raw)
  })
})

describe('assistant cards: a change that cannot be undone', () => {
  it('accepts a tool by name and arguments and words it for the person', () => {
    const card = validTool({ action: 'tool', tool: 'delete_agent', args: { name: 'scout' } })
    expect(card).toEqual({ action: 'tool', tool: 'delete_agent', args: { name: 'scout' } })
    expect(toolWords(card)).toEqual({ title: 'Delete agent', details: [{ key: 'name', value: 'scout' }] })
    expect(cardLabel(card)).toBe('Delete agent: scout')
    expect(cardNote(card)).toBe('This cannot be undone. It runs as you.')
  })

  it('shows nothing the model wrote as the description of what will run', () => {
    expect(validTool({ action: 'tool', tool: 'delete_agent', args: { name: 'scout' }, label: 'Tidy up harmlessly' })).toBeNull()
  })

  it('refuses a name that is not a tool name, arguments that are not an object, and oversized arguments', () => {
    expect(validTool({ action: 'tool', tool: '../admin', args: {} })).toBeNull()
    expect(validTool({ action: 'tool', tool: 'delete_agent', args: ['scout'] })).toBeNull()
    expect(validTool({ action: 'tool', tool: 'delete_agent', args: { name: 'x'.repeat(5000) } })).toBeNull()
    expect(acceptCards([{ action: 'tool', tool: 'delete_agent', args: { name: 'scout' } }], [])).toHaveLength(1)
  })
})

describe('assistant cards: a build in the making', () => {
  it('accepts a job by a well-formed id and names it', () => {
    const card = validJob({ action: 'job', job_id: DESIGN_ID, title: 'Review summary', agents: 1 })
    expect(card).toEqual({ action: 'job', job_id: DESIGN_ID, title: 'Review summary' })
    expect(cardLabel(card)).toBe('Making "Review summary"')
    expect(validJob({ action: 'job', job_id: '../x', title: 't' })).toBeNull()
  })
})
