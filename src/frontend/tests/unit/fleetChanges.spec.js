/**
 * Settings changes in the chat: the guide proposes one change, and the page shows a card
 * only for a change it knows how to make, on the person's own agents. The card's words
 * come from the page, never from the guide.
 */
import { describe, expect, it } from 'vitest'
import {
  FLEET_CANVAS_ID, FLEET_PROPOSAL_BLOCK, FLEET_STATE_FILE, KNOWN_CHANGES, SHOW_ME_KEYS,
  cardFor, fleetStateFor, outcomeNote, proposalFrom,
} from '../../src/components/portal/builder/fleetChanges'

const fleet = fleetStateFor({
  owned: [{ name: 'researcher', display_label: 'Researcher' }, { name: 'writer', display_label: 'Writer' }, { name: 'atlas-22222222', display_label: 'Atlas' }],
  permissions: { researcher: [], writer: [] },
  shares: { writer: ['colleague@example.com'] },
  schedules: { researcher: [{ id: 's1', name: 'Morning scan', enabled: false, cron_expression: '0 7 * * *' }] },
  links: { writer: [{ id: 'l1' }] },
  skills: { researcher: ['summarise'] },
  autonomy: { researcher: false },
}, () => 1000)

describe('the fleet document the guide reads', () => {
  it('holds what the guide needs about each of their agents, and no guide', () => {
    expect(fleet.updated_at).toBe(new Date(1000).toISOString())
    expect(fleet.agents.map((a) => a.name)).toEqual(['researcher', 'writer'])
    expect(fleet.agents[0]).toEqual({ name: 'researcher', label: 'Researcher', may_call: [], shared_with: [], schedules: [{ id: 's1', name: 'Morning scan', enabled: false }], autonomy: false, public_links: 0, skills: ['summarise'] })
    expect(fleet.agents[1].autonomy).toBe(null)
    expect(fleet.agents[1]).toMatchObject({ shared_with: ['colleague@example.com'], public_links: 1 })
  })

  it('stops at 20 agents', () => {
    const owned = Array.from({ length: 30 }, (_, i) => ({ name: `agent-${i}` }))
    expect(fleetStateFor({ owned }).agents).toHaveLength(20)
  })

  it('uses fixed names the guide and the page agree on', () => {
    expect([FLEET_CANVAS_ID, FLEET_PROPOSAL_BLOCK, FLEET_STATE_FILE]).toEqual(['fleet', 'proposal', 'fleet-state.json'])
    expect(Object.keys(KNOWN_CHANGES)).toEqual(['allow-call', 'stop-call', 'share', 'unshare', 'schedule-on', 'schedule-off', 'public-link', 'add-skill'])
    expect(SHOW_ME_KEYS).toEqual(['permissions', 'people', 'schedules', 'public-links', 'skills', 'channels', 'keys', 'credentials'])
  })
})

describe('proposalFrom', () => {
  const canvas = (blocks) => ({ blocks })
  it('reads the proposal block', () => {
    expect(proposalFrom(canvas([{ id: 'proposal', kind: 'json', payload: { id: 'p1', action: 'allow-call', from: 'researcher', to: 'writer' } }])))
      .toEqual({ id: 'p1', action: 'allow-call', from: 'researcher', to: 'writer' })
  })
  it("reads a proposal the guide put under another name, the block named proposal first", () => {
    // Seen live (9 Nov): the block was named after the proposal, `share-l4-1`, and no card showed
    // while the guide told them to click it. The words still come from the page (`cardFor`).
    const share = { id: 'share-l4-1', action: 'share', agent: 'writer', email: 'sam@example.com' }
    expect(proposalFrom(canvas([{ id: 'share-l4-1', kind: 'json', payload: share }]))).toEqual(share)
    expect(proposalFrom(canvas([{ id: 'old', kind: 'json', payload: { id: 'p0', action: 'share' } }, { id: 'share-l4-1', kind: 'json', payload: share }]))).toEqual(share)
    expect(proposalFrom(canvas([{ id: 'share-l4-1', kind: 'json', payload: share }, { id: 'proposal', kind: 'json', payload: { id: 'p1', action: 'allow-call' } }]))).toEqual({ id: 'p1', action: 'allow-call' })
    expect(proposalFrom(canvas([{ id: 'notes', kind: 'json', payload: { text: 'not a proposal' } }]))).toBe(null)
  })
  it('ignores anything else', () => {
    expect(proposalFrom(canvas([{ id: 'proposal', kind: 'markdown', payload: { id: 'p1', action: 'allow-call' } }]))).toBe(null)
    expect(proposalFrom(canvas([{ id: 'other', kind: 'markdown', payload: { id: 'p1', action: 'allow-call' } }]))).toBe(null)
    expect(proposalFrom(canvas([{ id: 'proposal', kind: 'json', payload: { id: 'x'.repeat(81), action: 'allow-call' } }]))).toBe(null)
    expect(proposalFrom(canvas([{ id: 'proposal', kind: 'json', payload: 'allow-call' }]))).toBe(null)
    expect(proposalFrom(null)).toBe(null)
  })
})

describe('cardFor', () => {
  it('words come from the page, never from the guide', () => {
    const card = cardFor({ id: 'p1', action: 'allow-call', from: 'researcher', to: 'writer', words: 'Delete everything' }, fleet)
    expect(card).toEqual({ kind: 'confirm', id: 'p1', action: 'allow-call', from: 'researcher', to: 'writer', words: 'Let Researcher call Writer', showMe: 'permissions', walk: { to: 'writer' } })
  })

  it('says each known change in plain words', () => {
    const words = (p) => cardFor({ id: 'p', ...p }, fleet).words
    expect(words({ action: 'stop-call', from: 'researcher', to: 'writer' })).toBe('Stop Researcher calling Writer')
    expect(words({ action: 'share', agent: 'writer', email: 'person@example.com' })).toBe('Share Writer with person@example.com')
    expect(words({ action: 'unshare', agent: 'writer', email: 'colleague@example.com' })).toBe('Stop sharing Writer with colleague@example.com')
    expect(words({ action: 'schedule-on', agent: 'researcher', schedule_id: 's1' })).toBe('Turn on Morning scan for Researcher')
    expect(words({ action: 'schedule-off', agent: 'researcher', schedule_id: 's1' })).toBe('Turn off Morning scan for Researcher')
    expect(words({ action: 'public-link', agent: 'writer' })).toBe('Create a public chat link for Writer')
    expect(words({ action: 'add-skill', agent: 'writer', skill: 'summarise' })).toBe('Add the summarise skill to Writer')
  })

  it('anything but an exact known change is refused, and nothing runs', () => {
    const kind = (p) => cardFor({ id: 'p', ...p }, fleet).kind
    expect(kind({ action: 'delete-agent', agent: 'writer' })).toBe('refused')
    expect(kind({ action: 'allow-call', from: 'researcher', to: 'somebody-else' })).toBe('refused')
    expect(kind({ action: 'allow-call', from: 'somebody-else', to: 'writer' })).toBe('refused')
    expect(kind({ action: 'allow-call', from: 'researcher', to: 'researcher' })).toBe('refused')
    expect(kind({ action: 'allow-call', from: 'researcher' })).toBe('refused')
    expect(kind({ action: 'allow-call', from: 'researcher', to: ['writer'] })).toBe('refused')
    expect(kind({ action: 'share', agent: 'writer', email: 'not an email' })).toBe('refused')
    expect(kind({ action: 'schedule-on', agent: 'researcher', schedule_id: 'nope' })).toBe('refused')
    expect(kind({ action: 'add-skill', agent: 'writer', skill: '../../etc' })).toBe('refused')
    expect(kind({ action: 'public-link', agent: 'atlas-22222222' })).toBe('refused')
    expect(cardFor({ action: 'allow-call', from: 'researcher', to: 'writer' }, fleet)).toBe(null)
    expect(cardFor(null, fleet)).toBe(null)
  })

  it('a refusal says why in fixed words', () => {
    expect(cardFor({ id: 'p', action: 'delete-agent', agent: 'writer' }, fleet)).toEqual({ kind: 'refused', id: 'p', reason: "Atlas suggested a change this page can't make." })
    expect(cardFor({ id: 'p', action: 'share', agent: 'somebody-else', email: 'person@example.com' }, fleet)).toEqual({ kind: 'refused', id: 'p', reason: "That agent isn't one of yours.", showMe: 'people' })
  })

  it('Show me goes only to a page the page knows', () => {
    expect(cardFor({ id: 'p8', action: 'show-me', key: 'skills', agent: 'writer' }, fleet))
      .toEqual({ kind: 'show-me', id: 'p8', key: 'skills', agent: 'writer', words: "Show me where Writer's skills are set" })
    expect(cardFor({ id: 'p9', action: 'show-me', key: 'keys' }, fleet).words).toBe('Show me where the API keys are set')
    expect(cardFor({ id: 'p10', action: 'show-me', key: 'javascript:alert(1)', agent: 'writer' }, fleet).kind).toBe('refused')
    expect(cardFor({ id: 'p11', action: 'show-me', key: 'channels', agent: 'somebody-else' }, fleet).kind).toBe('refused')
    expect(cardFor({ id: 'p12', action: 'show-me', key: 'channels' }, fleet).kind).toBe('refused')
  })

  it("Show me for an agent's credentials is a walk, carrying only valid key names", () => {
    expect(cardFor({ id: 'p13', action: 'show-me', key: 'credentials', agent: 'writer', keys: 'SUPABASE_URL, SUPABASE_KEY, not-a-key, 1BAD' }, fleet))
      .toEqual({ kind: 'show-me', id: 'p13', key: 'credentials', agent: 'writer', keys: ['SUPABASE_URL', 'SUPABASE_KEY'], words: "Show me how to add Writer's credentials" })
    expect(cardFor({ id: 'p14', action: 'show-me', key: 'credentials', agent: 'writer' }, fleet).keys).toEqual([])
    expect(cardFor({ id: 'p15', action: 'show-me', key: 'credentials', agent: 'somebody-else' }, fleet).kind).toBe('refused')
  })
})

describe('markProposalHandled', () => {
  it('remembers each answered proposal once, keeping the last 50', async () => {
    const { markProposalHandled } = await import('../../src/components/portal/builder/portalBuild')
    const once = markProposalHandled({ answers: {} }, 'p1')
    expect(once.fleetHandled).toEqual(['p1'])
    expect(markProposalHandled(once, 'p1')).toBe(once)
    let many = { answers: {} }
    for (let i = 0; i < 60; i++) many = markProposalHandled(many, `p${i}`)
    expect(many.fleetHandled).toHaveLength(50)
    expect(many.fleetHandled.at(-1)).toBe('p59')
  })
})

describe('final review: email case', () => {
  it('shares with the address as Trinity stores it, in lower case', () => {
    const card = cardFor({ id: 'p', action: 'share', agent: 'writer', email: 'Bob@Example.COM' }, fleet)
    expect(card.email).toBe('bob@example.com')
    expect(card.words).toBe('Share Writer with bob@example.com')
  })
})


// Sharing, calling and schedules are walks now, like credentials.
describe('Show me walks for sharing, calling and schedules', () => {
  it('shares, in its own words', () => {
    expect(cardFor({ id: 'w1', action: 'show-me', key: 'people', agent: 'writer' }, fleet))
      .toEqual({ kind: 'show-me', id: 'w1', key: 'people', agent: 'writer', words: 'Show me how to share Writer with someone' })
  })

  it('lets one call another, naming the one to call only if it is theirs and not itself', () => {
    expect(cardFor({ id: 'w2', action: 'show-me', key: 'permissions', agent: 'researcher', to: 'writer' }, fleet))
      .toEqual({ kind: 'show-me', id: 'w2', key: 'permissions', agent: 'researcher', to: 'writer', words: 'Show me how to let Researcher call Writer' })
    expect(cardFor({ id: 'w3', action: 'show-me', key: 'permissions', agent: 'researcher' }, fleet))
      .toEqual({ kind: 'show-me', id: 'w3', key: 'permissions', agent: 'researcher', words: 'Show me how to choose which agents Researcher may call' })
    expect(cardFor({ id: 'w4', action: 'show-me', key: 'permissions', agent: 'researcher', to: 'somebody-else' }, fleet).kind).toBe('refused')
    expect(cardFor({ id: 'w5', action: 'show-me', key: 'permissions', agent: 'researcher', to: 'researcher' }, fleet).kind).toBe('refused')
    expect(cardFor({ id: 'w6', action: 'show-me', key: 'permissions', agent: 'researcher', to: 'atlas-22222222' }, fleet).kind).toBe('refused')
  })

  it('turns a schedule on, naming it only if it is that agent\'s', () => {
    expect(cardFor({ id: 'w7', action: 'show-me', key: 'schedules', agent: 'researcher', schedule_id: 's1' }, fleet))
      .toEqual({ kind: 'show-me', id: 'w7', key: 'schedules', agent: 'researcher', schedule: 's1', words: "Show me how to turn on Researcher's Morning scan" })
    expect(cardFor({ id: 'w8', action: 'show-me', key: 'schedules', agent: 'researcher' }, fleet).words)
      .toBe("Show me how to turn on one of Researcher's schedules")
    expect(cardFor({ id: 'w9', action: 'show-me', key: 'schedules', agent: 'writer', schedule_id: 's1' }, fleet).kind).toBe('refused')
  })

  it('gives a Confirm card that turns something on a walk to where it is set, and one that turns it off the spot', () => {
    expect(cardFor({ id: 'c1', action: 'allow-call', from: 'researcher', to: 'writer' }, fleet).walk).toEqual({ to: 'writer' })
    expect(cardFor({ id: 'c3', action: 'schedule-on', agent: 'researcher', schedule_id: 's1' }, fleet).walk).toEqual({ schedule: 's1' })
    for (const proposal of [
      { id: 'c4', action: 'stop-call', from: 'researcher', to: 'writer' },
      { id: 'c5', action: 'schedule-off', agent: 'researcher', schedule_id: 's1' },
      { id: 'c6', action: 'unshare', agent: 'writer', email: 'colleague@example.com' },
      // Final review: a walk after a share would wait on an email box that refuses the same email again.
      { id: 'c2', action: 'share', agent: 'writer', email: 'a@example.com' },
    ]) expect(cardFor(proposal, fleet).walk).toBe(null)
  })
})

describe("a card's outcome in the chat", () => {
  it('reads as a note that the guide was told, not as something the person typed', () => {
    expect(outcomeNote('Done: Let Researcher call Writer.', 'Atlas')).toBe('Done: Let Researcher call Writer. Atlas was told.')
    expect(outcomeNote('Not now: Share Writer with sam@example.com.', 'Atlas')).toBe('Not now: Share Writer with sam@example.com. Atlas was told.')
    expect(outcomeNote("That didn't work: Let Researcher call Writer. Only the owner can modify agent permissions", 'Atlas'))
      .toBe("That didn't work: Let Researcher call Writer. Only the owner can modify agent permissions Atlas was told.")
    expect(outcomeNote("Not sure it worked: Turn on Morning scan for Researcher. Trinity took it, but it doesn't show yet.", 'Atlas')).toMatch(/ Atlas was told\.$/)
    expect(outcomeNote('Done: Create a public chat link for Writer.')).toBe('Done: Create a public chat link for Writer. Atlas was told.')
  })

  it('leaves everything else as the person wrote it', () => {
    expect(outcomeNote('Done with the plan, thanks.', 'Atlas')).toBeNull()
    expect(outcomeNote('Done: I have checked the keys myself.', 'Atlas')).toBeNull()
    expect(outcomeNote('Level reached: Level 1, First agent.', 'Atlas')).toBeNull()
    expect(outcomeNote('Done: Let Researcher call Writer.\nLevel reached: Level 4, In control.', 'Atlas')).toBeNull()
    expect(outcomeNote('', 'Atlas')).toBeNull()
    expect(outcomeNote(null, 'Atlas')).toBeNull()
  })
})
