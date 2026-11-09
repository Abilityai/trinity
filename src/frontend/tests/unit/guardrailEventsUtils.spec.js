/**
 * trinity-enterprise#165 S4 — the shared coverage rule and copy
 * (`utils/guardrailEvents.js`). The fleet read decides `coverage` on the
 * backend; the per-agent read carries only `reporting`, so the UI applies the
 * same rule. These pin that rule's order: no hooks first, then the state.
 */
import { describe, expect, it } from 'vitest'
import {
  HOOKED_RUNTIMES,
  agentCoverage,
  coverageNotice,
  kindLabel,
  kindRows,
  runtimeHasHooks,
  staleCopy,
} from '../../src/utils/guardrailEvents'

describe('agentCoverage', () => {
  it.each([
    [{ runtime: 'codex', reporting: { state: 'reporting' } }, 'no_hooks'],
    [{ runtime: 'gemini', reporting: { state: 'never' } }, 'no_hooks'],
    [{ runtime: 'gemini-cli', reporting: null }, 'no_hooks'],
    [{ runtime: null, reporting: { state: 'reporting', runtime: 'codex' } }, 'no_hooks'],
    [{ runtime: 'claude-code', reporting: { state: 'reporting' } }, 'reporting'],
    [{ runtime: 'claude-code', reporting: { state: 'stale' } }, 'stale'],
    [{ runtime: 'claude-code', reporting: { state: 'never' } }, 'never'],
    [{ runtime: null, reporting: null }, 'never'],
    [{ runtime: 'other', reporting: { state: 'reporting' } }, 'reporting'],
  ])('%j → %s', (input, expected) => {
    expect(agentCoverage(input)).toBe(expected)
  })

  it('an unknown runtime is the platform default, Claude Code', () => {
    expect(runtimeHasHooks(undefined)).toBe(true)
    expect(runtimeHasHooks('CLAUDE-CODE')).toBe(true)
    expect(HOOKED_RUNTIMES).toEqual(['claude-code', 'claude'])
  })
})

describe('coverageNotice', () => {
  it('reporting earns no notice; every other state earns its own words', () => {
    expect(coverageNotice('reporting')).toBeNull()
    const texts = ['never', 'stale', 'no_hooks'].map((c) =>
      coverageNotice(c, { last_report_at: '2026-11-06T08:30:00Z' }).text)
    expect(new Set(texts).size).toBe(3)
  })

  it('a stale notice names when the agent went quiet', () => {
    expect(staleCopy('2026-11-06T08:30:00Z')).toMatch(/^Not reporting since .*2026/)
    expect(staleCopy(null)).toBe('Not reporting')
  })
})

describe('kinds', () => {
  it('labels known kinds and keeps an unknown one by name', () => {
    expect(kindLabel('deny')).toBe('Refused')
    expect(kindLabel('brand_new_kind')).toBe('brand_new_kind')
  })

  it('orders a breakdown largest first and drops zeros', () => {
    expect(kindRows({ deny: 1, credential_redacted: 3, hook_error: 0 }).map((r) => r.kind))
      .toEqual(['credential_redacted', 'deny'])
  })
})
