import { describe, it, expect } from 'vitest'
import {
  agentDisplayName,
  hasDistinctLabel,
  agentNameParts,
  agentNameTooltip,
  agentOptionLabel,
} from '../../src/utils/agentName.js'

/**
 * Agent name resolution (ent#181), and the #2358 addition that makes the slug
 * visible again.
 *
 * An agent has two names and they are not interchangeable: the SLUG is the
 * identity (routes, container + volume names, MCP keys, every `agent_name`
 * column) and the LABEL is presentation. Requirements §1.3.1 FR-3 says one
 * helper resolves them everywhere — never a per-site `display_label || name`
 * chain — and FR-4 says the slug stays visible and copyable wherever the label
 * hides it.
 *
 * The Dashboard List and Grid tile shipped the label ALONE with the slug
 * `title`-only, so nothing on screen connected "Delivery Operations Manager"
 * to the `delivery-ops` that every URL and key is written against. #2358 adds
 * `agentNameParts()` as the single decidable rule behind that fix.
 *
 * These are the FIRST unit tests over this module — it had none, on all four
 * exported functions, despite being the resolution point for every agent name
 * the product renders.
 */

describe('agentDisplayName', () => {
  it('prefers the display label', () => {
    expect(agentDisplayName({ name: 'delivery-ops', display_label: 'Delivery Ops' })).toBe(
      'Delivery Ops'
    )
  })

  it('trims the label', () => {
    expect(agentDisplayName({ name: 'a', display_label: '  Padded  ' })).toBe('Padded')
  })

  it('falls back to the slug for an absent, null, empty or whitespace-only label', () => {
    expect(agentDisplayName({ name: 'delivery-ops' })).toBe('delivery-ops')
    expect(agentDisplayName({ name: 'delivery-ops', display_label: null })).toBe('delivery-ops')
    expect(agentDisplayName({ name: 'delivery-ops', display_label: '' })).toBe('delivery-ops')
    expect(agentDisplayName({ name: 'delivery-ops', display_label: '   ' })).toBe('delivery-ops')
  })

  it('falls back to the slug for a non-string label', () => {
    expect(agentDisplayName({ name: 'a', display_label: 42 })).toBe('a')
    expect(agentDisplayName({ name: 'a', display_label: { toString: () => 'x' } })).toBe('a')
  })

  it('passes a bare slug string through (legacy callers)', () => {
    expect(agentDisplayName('delivery-ops')).toBe('delivery-ops')
  })

  it('returns the empty string for null/undefined rather than throwing', () => {
    expect(agentDisplayName(null)).toBe('')
    expect(agentDisplayName(undefined)).toBe('')
  })
})

describe('hasDistinctLabel', () => {
  it('is true only when a label is set AND differs from the slug', () => {
    expect(hasDistinctLabel({ name: 'delivery-ops', display_label: 'Delivery Ops' })).toBe(true)
    expect(hasDistinctLabel({ name: 'delivery-ops', display_label: 'delivery-ops' })).toBe(false)
  })

  it('is false for an absent, empty, whitespace-only or non-string label', () => {
    expect(hasDistinctLabel({ name: 'a' })).toBe(false)
    expect(hasDistinctLabel({ name: 'a', display_label: null })).toBe(false)
    expect(hasDistinctLabel({ name: 'a', display_label: '' })).toBe(false)
    expect(hasDistinctLabel({ name: 'a', display_label: '   ' })).toBe(false)
    expect(hasDistinctLabel({ name: 'a', display_label: 7 })).toBe(false)
    expect(hasDistinctLabel({ name: 'a', display_label: {} })).toBe(false)
  })

  it('is false for a bare string or null input', () => {
    expect(hasDistinctLabel('delivery-ops')).toBe(false)
    expect(hasDistinctLabel(null)).toBe(false)
    expect(hasDistinctLabel(undefined)).toBe(false)
  })
})

describe('agentNameParts (#2358)', () => {
  it('returns the label as primary and the slug as secondary when a label hides it', () => {
    expect(agentNameParts({ name: 'delivery-ops', display_label: 'Delivery Ops' })).toEqual({
      primary: 'Delivery Ops',
      secondary: 'delivery-ops',
    })
  })

  it('returns no secondary when there is nothing hidden', () => {
    expect(agentNameParts({ name: 'delivery-ops' })).toEqual({
      primary: 'delivery-ops',
      secondary: null,
    })
    expect(agentNameParts({ name: 'delivery-ops', display_label: 'delivery-ops' })).toEqual({
      primary: 'delivery-ops',
      secondary: null,
    })
    expect(agentNameParts({ name: 'delivery-ops', display_label: '   ' })).toEqual({
      primary: 'delivery-ops',
      secondary: null,
    })
    expect(agentNameParts({ name: 'delivery-ops', display_label: null })).toEqual({
      primary: 'delivery-ops',
      secondary: null,
    })
  })

  it('trims the primary but leaves the secondary as the literal slug', () => {
    expect(agentNameParts({ name: 'delivery-ops', display_label: '  Delivery Ops  ' })).toEqual({
      primary: 'Delivery Ops',
      secondary: 'delivery-ops',
    })
  })

  /**
   * The load-bearing property. `secondary` is what a surface renders as the
   * agent's IDENTITY — the string a reader will paste into a URL, a `docker`
   * command or an MCP key lookup. If it could ever be the label, the fix would
   * be worse than the bug it replaces: the row would show two presentation
   * names and still hide the identity.
   */
  it('secondary is always either null or EXACTLY agent.name — never the label', () => {
    const cases = [
      { name: 'delivery-ops', display_label: 'Delivery Ops' },
      { name: 'delivery-ops', display_label: '  Delivery Ops  ' },
      { name: 'delivery-ops', display_label: 'delivery-ops' },
      { name: 'delivery-ops', display_label: '' },
      { name: 'delivery-ops', display_label: null },
      { name: 'delivery-ops', display_label: 42 },
      { name: 'delivery-ops' },
      { name: 'trinity-system', display_label: 'Platform Orchestrator' },
    ]
    for (const agent of cases) {
      const { secondary } = agentNameParts(agent)
      expect(secondary === null || secondary === agent.name).toBe(true)
    }
  })

  it('handles a bare slug string (legacy callers) and null without throwing', () => {
    expect(agentNameParts('delivery-ops')).toEqual({ primary: 'delivery-ops', secondary: null })
    expect(agentNameParts(null)).toEqual({ primary: '', secondary: null })
    expect(agentNameParts(undefined)).toEqual({ primary: '', secondary: null })
  })

  it('agrees with the two helpers it composes', () => {
    const cases = [
      { name: 'a', display_label: 'A Label' },
      { name: 'a', display_label: 'a' },
      { name: 'a' },
      'a',
      null,
    ]
    for (const agent of cases) {
      const parts = agentNameParts(agent)
      expect(parts.primary).toBe(agentDisplayName(agent))
      expect(parts.secondary === null).toBe(!hasDistinctLabel(agent))
    }
  })
})

describe('agentNameTooltip', () => {
  it('carries BOTH names when they differ (a belt for a truncated label)', () => {
    expect(agentNameTooltip({ name: 'delivery-ops', display_label: 'Delivery Ops' })).toBe(
      'Delivery Ops · delivery-ops'
    )
  })

  it('is the bare display name when nothing is hidden', () => {
    expect(agentNameTooltip({ name: 'delivery-ops' })).toBe('delivery-ops')
    expect(agentNameTooltip('delivery-ops')).toBe('delivery-ops')
    expect(agentNameTooltip(null)).toBe('')
  })
})

describe('agentOptionLabel', () => {
  it('disambiguates inline in a picker `<option>` (#1642)', () => {
    expect(agentOptionLabel({ name: 'delivery-ops', display_label: 'Delivery Ops' })).toBe(
      'Delivery Ops (delivery-ops)'
    )
  })

  it('is the bare display name when nothing is hidden', () => {
    expect(agentOptionLabel({ name: 'delivery-ops' })).toBe('delivery-ops')
    expect(agentOptionLabel('delivery-ops')).toBe('delivery-ops')
  })
})
