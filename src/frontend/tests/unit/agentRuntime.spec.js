import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { dirname, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import {
  DEFAULT_RUNTIME,
  DEFAULT_RUNTIME_IDS,
  isDefaultRuntime,
  showsRuntimeBadgeInList,
} from '../../src/utils/agentRuntime.js'

/**
 * The list-row runtime-badge rule (#2358).
 *
 * The Dashboard List rendered a `RuntimeBadge` on EVERY name — on a
 * single-runtime fleet (the overwhelmingly common case) that is one identical
 * pill per row, i.e. background texture in the highest-value column. Moving it
 * one line down is not a reduction, so the rule is: badge the EXCEPTIONS.
 *
 * The rule is decidable, so it lives in a pure util where a spec can reach it —
 * `vitest.config.js` pins `environment: 'node'` with no mount harness, so a
 * predicate written inline in the SFC would be untestable.
 *
 * It is platform-anchored (the default runtime is a constant) rather than
 * derived from fleet majority: a majority rule would silently flip badges on
 * and off as agents are created and deleted.
 */
describe('agentRuntime — the default runtime', () => {
  it('names the platform default, matching RuntimeBadge.vue own prop default', () => {
    expect(DEFAULT_RUNTIME).toBe('claude-code')
  })

  it('accepts the SAME id set RuntimeBadge.vue treats as Claude', () => {
    // RuntimeBadge's own isClaude(): !runtime || 'claude-code' || 'claude'.
    // Diverging here would badge a `runtime: "claude"` row as an exception —
    // a Claude sunburst pill on a homogeneous Claude fleet, i.e. exactly the
    // noise this rule removes.
    expect([...DEFAULT_RUNTIME_IDS].sort()).toEqual(['claude', 'claude-code'])
  })

  /**
   * …and keeps accepting it. `RuntimeBadge.vue` is a shared component this
   * change deliberately does not edit, and its `isClaudeRuntime` predicate is
   * a `computed` inside an SFC — there is nothing to import, so the mirror is
   * DERIVED from its source rather than hand-copied. Hand-copying is what the
   * assertion above is, and a hand-copy has no way to notice the day the
   * original moves.
   *
   * Divergence is silent in both directions and neither is cosmetic. If the
   * badge learns a new Claude id this set does not, that runtime reads as an
   * exception here and wears a Claude sunburst pill on every row of a fleet
   * that is entirely Claude — the texture #2358 removed, back again. If this
   * set keeps an id the badge drops, a genuinely foreign runtime silently
   * loses its badge and the list stops marking the one thing it is for.
   */
  it('derives that set from RuntimeBadge.vue rather than trusting the copy', () => {
    const badge = readFileSync(
      resolve(dirname(fileURLToPath(import.meta.url)), '../../src/components/RuntimeBadge.vue'),
      'utf8'
    )
    const predicate = badge.match(/isClaudeRuntime\s*=\s*computed\([\s\S]*?\n\}\)/)
    expect(predicate, 'RuntimeBadge.vue still defines isClaudeRuntime').toBeTruthy()
    const ids = [...predicate[0].matchAll(/'([^']+)'/g)].map((m) => m[1])
    expect(ids.sort()).toEqual([...DEFAULT_RUNTIME_IDS].sort())
    // An absent/blank runtime is Claude there too — the arm that makes
    // `isDefaultRuntime` safe to fail open on an older backend's payload.
    expect(predicate[0]).toMatch(/!props\.runtime/)
  })
})

describe('isDefaultRuntime', () => {
  it('is true for the default runtime and its alias', () => {
    expect(isDefaultRuntime({ name: 'a', runtime: 'claude-code' })).toBe(true)
    expect(isDefaultRuntime({ name: 'a', runtime: 'claude' })).toBe(true)
  })

  it('treats an absent, null or blank runtime as the default (the payload of an older backend)', () => {
    expect(isDefaultRuntime({ name: 'a' })).toBe(true)
    expect(isDefaultRuntime({ name: 'a', runtime: null })).toBe(true)
    expect(isDefaultRuntime({ name: 'a', runtime: '' })).toBe(true)
    expect(isDefaultRuntime({ name: 'a', runtime: '   ' })).toBe(true)
  })

  it('tolerates surrounding whitespace on a real runtime id', () => {
    expect(isDefaultRuntime({ name: 'a', runtime: ' claude-code ' })).toBe(true)
    expect(isDefaultRuntime({ name: 'a', runtime: ' codex ' })).toBe(false)
  })

  it('is false for a non-default runtime', () => {
    expect(isDefaultRuntime({ name: 'a', runtime: 'gemini-cli' })).toBe(false)
    expect(isDefaultRuntime({ name: 'a', runtime: 'gemini' })).toBe(false)
    expect(isDefaultRuntime({ name: 'a', runtime: 'codex' })).toBe(false)
  })

  it('is true for a non-string runtime — an unreadable value is not evidence of an exception', () => {
    expect(isDefaultRuntime({ name: 'a', runtime: 42 })).toBe(true)
    expect(isDefaultRuntime({ name: 'a', runtime: { id: 'codex' } })).toBe(true)
  })

  it('is true for a bare-string or null agent (no runtime to read)', () => {
    expect(isDefaultRuntime('delivery-ops')).toBe(true)
    expect(isDefaultRuntime(null)).toBe(true)
    expect(isDefaultRuntime(undefined)).toBe(true)
  })
})

describe('showsRuntimeBadgeInList', () => {
  it('is exactly the negation of isDefaultRuntime', () => {
    const agents = [
      { name: 'a', runtime: 'claude-code' },
      { name: 'a', runtime: 'claude' },
      { name: 'a', runtime: 'codex' },
      { name: 'a', runtime: 'gemini-cli' },
      { name: 'a' },
      { name: 'a', runtime: null },
      { name: 'a', runtime: '' },
      'bare-slug',
      null,
    ]
    for (const agent of agents) {
      expect(showsRuntimeBadgeInList(agent)).toBe(!isDefaultRuntime(agent))
    }
  })

  it('badges the exceptions and nothing else', () => {
    expect(showsRuntimeBadgeInList({ name: 'a', runtime: 'codex' })).toBe(true)
    expect(showsRuntimeBadgeInList({ name: 'a', runtime: 'claude-code' })).toBe(false)
    expect(showsRuntimeBadgeInList({ name: 'a' })).toBe(false)
  })
})
