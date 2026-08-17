/**
 * #2213 — the `/` typeahead must be able to find every skill the agent offers.
 *
 * Reported from live use: typing `/` listed only part of an agent's skills. The
 * list passes four narrowing steps, and the measurement (on a real instance, with
 * 30 probe skills planted in the container) found which one:
 *
 *   agent-server `GET /api/skills`      33 skills, all user_invocable
 *   roster payload `playbooks[]`        24            <- `_MAX_BRIEFING_HINTS`
 *   popup, query "probe 27"             0 matches, nothing on screen saying why
 *
 * So the exposure filter dropped nothing (no allow-list, everything invocable),
 * the container scan found everything, and the popup's own limit of 8 was already
 * honest — it renders "N more — keep typing to filter". The defect was the payload
 * bound: 24 is right for the hint-card GRID it was written for (#2101) and wrong as
 * the search corpus, because search cannot reach what never shipped.
 *
 * The fix separates the two: `searchable_playbooks` (same client-visible set, its
 * own larger bound, no descriptions) feeds `/`, while `playbooks` keeps feeding the
 * cards, and `playbooks_total` makes any remaining truncation visible.
 *
 * These tests use a set larger than BOTH bounds, which is the case the AC names —
 * the old code passed every test that only used a handful of playbooks.
 */
import { describe, it, expect } from 'vitest'
import {
  TYPEAHEAD_LIMIT,
  boundCandidates,
  filterPlaybookCandidates,
  hiddenPlaybookCount,
  playbookSearchSource,
} from '../../src/components/portal/portalUtils'

const CARD_BOUND = 24        // server-side `_MAX_BRIEFING_HINTS`
const SEARCH_BOUND = 200     // server-side `_MAX_SEARCHABLE_PLAYBOOKS`

function playbook(n) {
  return { title: `Skill ${String(n).padStart(3, '0')}`, starter_prompt: `/skill-${n} ` }
}

/** An agent whose client-visible set is larger than both bounds. */
function agentWith(total) {
  const all = Array.from({ length: total }, (_, i) => playbook(i + 1))
  return {
    name: 'atlas',
    playbooks: all.slice(0, CARD_BOUND),                 // what the cards get
    searchable_playbooks: all.slice(0, SEARCH_BOUND),    // what search gets
    playbooks_total: total,
  }
}

describe('#2213 the search corpus', () => {
  it('is the searchable set, not the card-bounded one', () => {
    const agent = agentWith(300)
    expect(agent.playbooks).toHaveLength(CARD_BOUND)
    expect(playbookSearchSource(agent)).toHaveLength(SEARCH_BOUND)
  })

  it('finds a skill past the CARD bound — the reported bug', () => {
    const agent = agentWith(300)
    // Skill 027 shipped in `searchable_playbooks` but not in `playbooks`; before
    // the fix this query matched nothing at all.
    const { items } = filterPlaybookCandidates(playbookSearchSource(agent), 'Skill 027')
    expect(items.length).toBeGreaterThan(0)
    expect(items[0].title).toBe('Skill 027')

    // And the proof that the old corpus could not: same query, card list only.
    const old = filterPlaybookCandidates(agent.playbooks, 'Skill 027')
    expect(old.items).toHaveLength(0)
  })

  it('still renders at most TYPEAHEAD_LIMIT rows, with the rest reported', () => {
    const agent = agentWith(300)
    const { items } = filterPlaybookCandidates(playbookSearchSource(agent), '')
    const { visible, overflow } = boundCandidates(items)
    expect(visible).toHaveLength(TYPEAHEAD_LIMIT)
    expect(overflow).toBe(SEARCH_BOUND - TYPEAHEAD_LIMIT)
    // Bounded rendering is fine; it is bounded SEARCH that was the bug.
    expect(overflow).toBeGreaterThan(0)
  })

  it('falls back to the card list when the field is absent (older backend)', () => {
    const legacy = { playbooks: [playbook(1), playbook(2)] }
    expect(playbookSearchSource(legacy)).toHaveLength(2)
    expect(playbookSearchSource({})).toEqual([])
    expect(playbookSearchSource(undefined)).toEqual([])
  })

  it('prefers the card list over an EMPTY searchable list', () => {
    // A briefing that failed produces `searchable_playbooks: []`; treating that as
    // authoritative would silently disable `/` for an agent that has playbooks.
    const agent = { playbooks: [playbook(1)], searchable_playbooks: [] }
    expect(playbookSearchSource(agent)).toHaveLength(1)
  })
})

describe('#2213 the honest count of what never arrived', () => {
  it('reports skills truncated out of the payload', () => {
    const agent = agentWith(300)
    // 300 exist, 200 shipped: 100 cannot be searched at all, and saying so is the
    // difference between a bounded list and a dishonest one.
    expect(hiddenPlaybookCount(agent, SEARCH_BOUND)).toBe(100)
  })

  it('is zero when everything shipped', () => {
    const agent = agentWith(30)
    expect(agent.searchable_playbooks).toHaveLength(30)
    expect(hiddenPlaybookCount(agent, 30)).toBe(0)
  })

  it('never goes negative, and ignores a missing or junk total', () => {
    expect(hiddenPlaybookCount({ playbooks_total: 5 }, 10)).toBe(0)
    expect(hiddenPlaybookCount({}, 3)).toBe(0)
    expect(hiddenPlaybookCount({ playbooks_total: 'lots' }, 3)).toBe(0)
    expect(hiddenPlaybookCount(undefined, 3)).toBe(0)
  })

  it('derives the shipped count itself when not told', () => {
    const agent = agentWith(300)
    expect(hiddenPlaybookCount(agent)).toBe(100)
  })
})

describe('#2213 the two omissions stay distinct', () => {
  it('overflow is reachable by typing; hidden is not', () => {
    const agent = agentWith(300)
    const { items } = filterPlaybookCandidates(playbookSearchSource(agent), '')
    const { overflow } = boundCandidates(items)
    const hidden = hiddenPlaybookCount(agent, SEARCH_BOUND)

    // Both non-zero here, and they must not be summed into one number: typing
    // "Skill 150" reaches an overflow row, and no amount of typing reaches a
    // hidden one — which is why the popup words them differently.
    expect(overflow).toBeGreaterThan(0)
    expect(hidden).toBeGreaterThan(0)

    const reached = filterPlaybookCandidates(playbookSearchSource(agent), 'Skill 150')
    expect(reached.items[0].title).toBe('Skill 150')
    const unreachable = filterPlaybookCandidates(playbookSearchSource(agent), 'Skill 250')
    expect(unreachable.items).toHaveLength(0)
  })
})
