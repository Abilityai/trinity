/**
 * Bounded briefing hint grid (#2101).
 *
 * The Workspace new-chat briefing renders the agent's capability hints as a
 * card grid. With no connector allow-list configured, every user_invocable
 * skill becomes a card — so the grid must be bounded: described-cards-first
 * deterministic order, first HINT_COLLAPSE_LIMIT visible collapsed, a counted
 * toggle expanding the rest in place. This spec pins the fold math at its
 * boundaries — the classic off-by-one lives at exactly-limit vs limit+1.
 */
import { describe, it, expect } from 'vitest'
import { planHintDisplay, HINT_COLLAPSE_LIMIT } from '@/components/portal/portalUtils'

const hint = (title, description = null) => ({ title, description, starter_prompt: title })
const hints = (n) => Array.from({ length: n }, (_, i) => hint(`h${i}`))

describe('planHintDisplay', () => {
  it('empty and non-array inputs yield no cards and no toggle', () => {
    for (const input of [[], null, undefined, 'nope']) {
      const plan = planHintDisplay(input, false)
      expect(plan.visible).toEqual([])
      expect(plan.total).toBe(0)
      expect(plan.collapsible).toBe(false)
    }
  })

  it('at or under the limit: everything visible, no toggle (6 → no chrome)', () => {
    for (const n of [1, 5, HINT_COLLAPSE_LIMIT]) {
      const plan = planHintDisplay(hints(n), false)
      expect(plan.visible).toHaveLength(n)
      expect(plan.total).toBe(n)
      expect(plan.collapsible).toBe(false)
    }
  })

  it('one past the limit: collapsed shows the limit, toggle counts the real total', () => {
    const plan = planHintDisplay(hints(HINT_COLLAPSE_LIMIT + 1), false)
    expect(plan.visible).toHaveLength(HINT_COLLAPSE_LIMIT)
    expect(plan.total).toBe(HINT_COLLAPSE_LIMIT + 1)
    expect(plan.collapsible).toBe(true)
  })

  it('expanded shows the whole list and stays collapsible (toggle can re-collapse)', () => {
    const plan = planHintDisplay(hints(30), true)
    expect(plan.visible).toHaveLength(30)
    expect(plan.total).toBe(30)
    expect(plan.collapsible).toBe(true)
  })

  it('described cards come first; relative order is preserved within each group', () => {
    const input = [
      hint('bare-1'),
      hint('desc-1', 'does a thing'),
      hint('bare-2'),
      hint('desc-2', 'does another'),
      hint('blank-desc', '   '), // whitespace-only description counts as bare
    ]
    const plan = planHintDisplay(input, true)
    expect(plan.visible.map((h) => h.title)).toEqual([
      'desc-1', 'desc-2', 'bare-1', 'bare-2', 'blank-desc',
    ])
  })

  it('the collapsed window is taken AFTER ordering — described cards win the visible slots', () => {
    const input = [...hints(HINT_COLLAPSE_LIMIT), hint('described-last', 'real description')]
    const plan = planHintDisplay(input, false)
    expect(plan.visible[0].title).toBe('described-last')
    expect(plan.visible).toHaveLength(HINT_COLLAPSE_LIMIT)
  })
})
