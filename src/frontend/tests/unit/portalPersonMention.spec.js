/**
 * trinity-enterprise#631 — tagging a person in a conversation: the pure rules.
 *
 * What a send carries, how the picker refuses an unknown name, what the tagger
 * sees on their own message, and how a tag sits in the Inbox (Unread, never
 * Action; counted as one thing that came back). The mounted half — the
 * composer's picker, the Inbox item and its reading pane — is
 * `portalPersonMention.mount.spec.js`.
 */
import { describe, it, expect } from 'vitest'
import {
  MAX_TAGS_PER_MESSAGE, addPicked, cantSeeText, mentionOpenTarget, mentionWhere,
  noOneCalled, peopleRows, personToken, tagMarkText, tagsFor, tagsInText, typeaheadHeading,
} from '../../src/components/portal/portalMentions'
import {
  actionItems, allItems, inboxCounts, listHeadLabel, paneHeading, parseItemKey, resolveItem,
  stableRows, emptyVisit, unreadItems, inboxSelectedAgent,
} from '../../src/components/portal/portalInbox'

const bob = { email: 'Bob@Example.com', label: 'Bob Baker' }
const mention = (id, over = {}) => ({
  id, agent_name: 'scout', state: 'unread', created_at: '2026-10-10T10:00:00Z', read_at: null,
  tagged_by: 'Alice Archer', conversation: { kind: 'room', label: 'Pricing review' }, ...over,
})

describe('what a send carries', () => {
  it('the token is the name; the address travels beside the text', () => {
    expect(personToken(bob)).toBe('@Bob Baker')
  })

  it('picking the same person twice is one tag', () => {
    const once = addPicked([], bob)
    expect(addPicked(once, { ...bob, email: 'bob@example.com' })).toHaveLength(1)
    expect(once[0].email).toBe('bob@example.com')
  })

  it('only people whose token is still in the text are tagged — deleting the name untags', () => {
    const picked = addPicked(addPicked([], bob), { email: 'carol@example.com', label: 'Carol Chen' })
    expect(tagsInText('@Bob Baker and @Carol Chen, look', picked)).toEqual(['bob@example.com', 'carol@example.com'])
    expect(tagsInText('@Carol Chen, look', picked)).toEqual(['carol@example.com'])
    expect(tagsInText('no one', picked)).toEqual([])
  })

  it('never carries more than the server accepts', () => {
    let picked = []
    for (let i = 0; i < MAX_TAGS_PER_MESSAGE + 3; i++) picked = addPicked(picked, { email: `p${i}@x.io`, label: `P${i}` })
    const text = picked.map(personToken).join(' ')
    expect(tagsInText(text, picked)).toHaveLength(MAX_TAGS_PER_MESSAGE)
  })
})

describe('the picker', () => {
  it('people rows say who, with the address to tell two apart', () => {
    expect(peopleRows([bob])).toEqual([
      { key: 'pp-bob@example.com', primary: 'Bob Baker', secondary: 'Bob@Example.com', kind: 'person', person: bob },
    ])
  })

  it('refuses an unknown name by name, with the reason', () => {
    expect(noOneCalled('zed', ['scout', 'scribe'])).toBe(
      'No one called “zed” can be tagged here — you can tag people who work with scout, scribe.')
    expect(noOneCalled('', ['scout'])).toBe('')
  })

  it('names itself once people are on offer', () => {
    expect(typeaheadHeading({ people: true })).toBe('Agents and people')
    expect(typeaheadHeading({ people: false })).toBe('Agents')
  })
})

describe("the tagger's marks", () => {
  it('honest state: in their Inbox (in-app), read, or not delivered', () => {
    expect(tagMarkText({ label: 'Bob Baker', state: 'delivered' })).toBe('Bob Baker · in their Inbox')
    expect(tagMarkText({ label: 'Bob Baker', state: 'read' })).toBe('Bob Baker · read')
    expect(tagMarkText({ label: 'Bob Baker', state: 'failed' })).toBe('Bob Baker · not delivered')
  })

  it("the room's whole-room map wins, so an older tag turns read without a reload", () => {
    const m = { id: 'm1', tags: [{ label: 'Bob Baker', state: 'delivered' }] }
    expect(tagsFor(m, { m1: [{ label: 'Bob Baker', state: 'read' }] })[0].state).toBe('read')
    expect(tagsFor(m, {})[0].state).toBe('delivered')
    expect(tagsFor({ id: 'x' }, null)).toEqual([])
  })
})

describe('the tagged person', () => {
  it("can't see a room: says so, and names who runs it — never the content", () => {
    const t = cantSeeText({ can_see: false, conversation: { kind: 'room', label: 'R' }, can_let_you_in: ['Alice Archer'] })
    expect(t.title).toBe("You're not in this room")
    expect(t.body).toContain('Alice Archer runs this room and can let you in')
    expect(t.body).toContain("a tag doesn't add you")
  })

  it("can't see a chat: it is the tagger's own", () => {
    const t = cantSeeText({ can_see: false, conversation: { kind: 'chat' }, can_let_you_in: ['Alice Archer'] })
    expect(t.title).toBe("You can't see this chat")
    expect(t.body).toContain("Alice Archer's own chat")
  })

  it('offers the room only to a reader who can see it', () => {
    expect(mentionOpenTarget({ can_see: true, conversation: { kind: 'room', id: 'room_1' } })).toBe('/workspace/r/room_1')
    expect(mentionOpenTarget({ can_see: false, conversation: { kind: 'room', id: 'room_1' } })).toBeNull()
    expect(mentionOpenTarget({ can_see: true, conversation: { kind: 'chat', id: 's' } })).toBeNull()
  })

  it('says where', () => {
    expect(mentionWhere(mention('a'))).toBe('in the room “Pricing review”')
    expect(mentionWhere(mention('a', { conversation: { kind: 'chat', label: 'a chat with scout' } }))).toBe('in a chat with scout')
  })
})

describe('a tag in the Inbox — Unread, never Action', () => {
  const threads = [{ id: 't1', agent_name: 'scout', unread: 2, last_message_at: '2026-10-10T09:00:00Z' }]

  it('an unread tag is in Unread and counted as one thing that came back', () => {
    const rows = unreadItems(threads, {}, [mention('m1'), mention('m2', { state: 'read' })])
    expect(rows.map((r) => r.key)).toEqual(['mention:m1', 'thread:t1'])
    expect(inboxCounts(threads, [], [mention('m1'), mention('m2', { state: 'read' })])).toEqual({ needs: 0, came: 3 })
  })

  it('never reaches Action, whatever its state', () => {
    expect(actionItems([])).toEqual([])
    expect(inboxCounts([], [], [mention('m1')]).needs).toBe(0)
  })

  it('All keeps read tags too', () => {
    const rows = allItems([], [], {}, Date.now(), [mention('m1', { state: 'read' })])
    expect(rows.map((r) => r.key)).toEqual(['mention:m1'])
  })

  it('the list head names mentions in units', () => {
    const rows = unreadItems([], {}, [mention('m1'), mention('m2')])
    expect(listHeadLabel('unread', rows)).toBe('2 mentions')
    expect(listHeadLabel('unread', unreadItems(threads, {}, [mention('m1')]))).toBe('1 chat · 2 new · 1 mention')
    expect(listHeadLabel('unread', unreadItems(threads, {}, []))).toBe('1 chat · 2 new')
  })

  it('a mention key round-trips and resolves, and the rail follows its agent', () => {
    expect(parseItemKey('mention:abc')).toEqual({ type: 'mention', id: 'abc' })
    expect(resolveItem('mention:m1', { mentions: [mention('m1')] }).title).toBe('Alice Archer mentioned you')
    expect(inboxSelectedAgent({ item: 'mention:m1', mentions: [mention('m1')], agents: [{ name: 'scout' }] })).toEqual({ name: 'scout' })
    expect(paneHeading({ type: 'mention', title: 'Alice Archer mentioned you' })).toBe('Alice Archer mentioned you')
  })

  it('a tag just read keeps its place, drawn read, until the selection moves', () => {
    const fresh = unreadItems([], {}, [mention('m1')])
    const first = stableRows(fresh, emptyVisit(), {})
    const after = stableRows([], first.visit, { mention: () => mention('m1', { state: 'read' }) })
    expect(after.rows).toHaveLength(1)
    expect(after.rows[0]).toMatchObject({ key: 'mention:m1', ghost: true, readInPlace: true, status: 'read' })
  })
})
