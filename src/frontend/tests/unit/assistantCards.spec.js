import { describe, it, expect, vi } from 'vitest'
import { CARD_ACTIONS, MAX_CARDS_PER_REPLY, cardLabel, cardNote, outcomeLine, parseReply, runCard, validCard } from '@/utils/assistantCards'

const fleet = [
  { name: 'issue-digest', schedules: [{ id: 's1', name: 'Mondays 09:00' }] },
  { name: 'scout', schedules: [] },
]
const block = (obj) => '```trinity-card\n' + JSON.stringify(obj) + '\n```'

describe('assistant cards: what a reply may propose', () => {
  it('turns a valid block into a card and takes it out of the text', () => {
    const reply = `I added the schedule.\n\n${block({ action: 'schedule-on', agent: 'issue-digest', schedule_id: 's1' })}\n\nConfirm above.`
    const { text, cards } = parseReply(reply, fleet)
    expect(cards).toEqual([{ action: 'schedule-on', agent: 'issue-digest', schedule_id: 's1' }])
    expect(text).toBe('I added the schedule.\n\nConfirm above.')
    expect(cardLabel(cards[0], fleet)).toBe('Turn on the schedule "Mondays 09:00" for issue-digest')
  })

  it.each([
    ['an action outside the catalog', { action: 'delete-agent', agent: 'scout' }],
    ['an inherited property name as the action', { action: 'constructor', agent: 'scout' }],
    ['an agent the page did not read', { action: 'start-agent', agent: 'someone-elses' }],
    ['a schedule that agent does not have', { action: 'schedule-on', agent: 'scout', schedule_id: 's1' }],
    ['an extra field', { action: 'start-agent', agent: 'scout', force: 'yes' }],
    ['a missing field', { action: 'share', agent: 'scout' }],
    ['a non-string field', { action: 'start-agent', agent: ['scout'] }],
    ['an address that is not an email', { action: 'share', agent: 'scout', email: 'everyone' }],
    ['an agent calling itself', { action: 'allow-call', from: 'scout', to: 'scout' }],
  ])('refuses %s, and still hides the block', (_, proposal) => {
    const { text, cards } = parseReply(`Here.\n${block(proposal)}`, fleet)
    expect(cards).toEqual([])
    expect(text).toBe('Here.')
  })

  it('refuses a block that is not JSON without showing it', () => {
    const { text, cards } = parseReply('Hi\n```trinity-card\n{not json\n```', fleet)
    expect(cards).toEqual([])
    expect(text).toBe('Hi')
  })

  it('keeps at most the per-reply limit and drops an exact repeat', () => {
    const a = block({ action: 'start-agent', agent: 'scout' })
    const b = block({ action: 'autonomy-on', agent: 'scout' })
    const c = block({ action: 'stop-agent', agent: 'issue-digest' })
    expect(parseReply([a, a, b, c].join('\n'), fleet).cards).toHaveLength(MAX_CARDS_PER_REPLY)
    expect(parseReply([a, a].join('\n'), fleet).cards).toHaveLength(1)
  })

  it('accepts the upgrade card, which takes no fields, and refuses it with any', () => {
    const ok = parseReply('Out of free builds.\n```trinity-card\n{"action": "upgrade"}\n```', fleet)
    expect(ok.cards).toEqual([{ action: 'upgrade' }])
    expect(cardLabel(ok.cards[0], fleet)).toBe('Upgrade this instance to Trinity Pro')
    expect(cardNote(ok.cards[0])).toBe('Lifts the limit on agents Trinity builds for you.')
    expect(cardNote({ action: 'start-agent', agent: 'scout' })).toBe('Runs as you, not as the assistant.')
    expect(parseReply(block({ action: 'upgrade', plan: 'enterprise' }), fleet).cards).toEqual([])
  })

  it('leaves an ordinary code block alone', () => {
    const reply = 'Run:\n```bash\nls\n```'
    expect(parseReply(reply, fleet).text).toBe(reply)
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

  it('upgrades through the instance, and is done only when the plan reads pro', async () => {
    const h = http({ get: vi.fn(async () => ({ data: { plan: 'pro' } })) })
    expect(await runCard(h, { action: 'upgrade' }, fleet)).toEqual({ kind: 'done' })
    expect(h.post.mock.calls[0][0]).toBe('/api/enterprise/assistant/upgrade')
    expect(h.get).toHaveBeenCalledWith('/api/enterprise/assistant/status')

    const still = http({ get: vi.fn(async () => ({ data: { plan: 'free' } })) })
    expect((await runCard(still, { action: 'upgrade' }, fleet)).kind).toBe('unsure')
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
      expect(spec.call(sample)[1], name).toMatch(name === 'upgrade' ? /^\/api\/enterprise\/assistant\/upgrade$/ : /^\/api\/agents\/x/)
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
