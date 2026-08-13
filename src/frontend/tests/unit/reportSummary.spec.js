/**
 * The fallback summariser (#2162).
 *
 * The Workspace Reports tab rendered `JSON.stringify(payload, null, 2)` to
 * external clients. Routing it through the shared typed renderers fixes the
 * well-shaped case; this module is the other half — what an UNRECOGNISED
 * payload becomes, on a surface where a raw dump is not an acceptable answer.
 *
 * It is a pure module for a reason beyond taste: there is no component-mount
 * harness in this project (no `@vue/test-utils`), so logic living inside the
 * `.vue` would have exactly zero coverage. Everything worth asserting about the
 * fallback is asserted here.
 *
 * The two tests that matter most are negatives:
 *
 *   * **No output path ever stringifies the payload.** That would reintroduce
 *     the bug inside the component written to fix it, and it would do so
 *     silently — the card would still look "handled".
 *   * **A credential-shaped token is redacted at VALUE level, anywhere in the
 *     string.** A key-name allow-list would miss `{"status": "failed: sk-…"}`,
 *     which is precisely the shape that motivated the redaction.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'fs'
import { fileURLToPath } from 'url'

import { stripComments } from './helpers/stripComments'
import {
  summarizePayload,
  describeValue,
  humaniseKey,
  redactSecrets,
  MAX_ENTRIES,
  MAX_VALUE_CHARS,
  REDACTED,
} from '@/components/reports/reportSummary'

const MODULE = fileURLToPath(
  new URL('../../src/components/reports/reportSummary.js', import.meta.url),
)

// ---------------------------------------------------------------------------
// Shape description — depth 1, never a serialisation
// ---------------------------------------------------------------------------

describe('describeValue', () => {
  it('describes a nested object by its field count', () => {
    expect(describeValue({ a: 1, b: 2, c: 3 })).toEqual({ value: '3 fields', hint: 'count' })
  })

  it('describes an array by its item count', () => {
    expect(describeValue([1, 2])).toEqual({ value: '2 items', hint: 'count' })
  })

  it('is singular for one', () => {
    expect(describeValue([1]).value).toBe('1 item')
    expect(describeValue({ only: true }).value).toBe('1 field')
  })

  it('renders null and undefined as an em dash, not as "null"', () => {
    expect(describeValue(null)).toEqual({ value: '—', hint: 'empty' })
    expect(describeValue(undefined)).toEqual({ value: '—', hint: 'empty' })
  })

  it('renders a whitespace-only string as empty rather than as blank content', () => {
    expect(describeValue('   ').hint).toBe('empty')
  })

  it('renders booleans as words', () => {
    expect(describeValue(true).value).toBe('Yes')
    expect(describeValue(false).value).toBe('No')
  })

  it('keeps a non-finite number readable instead of printing nothing', () => {
    expect(describeValue(Number.NaN).value).toBe('NaN')
    expect(describeValue(Infinity).value).toBe('Infinity')
  })

  it('truncates a long string with an ellipsis', () => {
    const long = 'x'.repeat(MAX_VALUE_CHARS + 50)
    const out = describeValue(long).value
    expect(out.length).toBe(MAX_VALUE_CHARS + 1) // + the ellipsis
    expect(out.endsWith('…')).toBe(true)
  })
})

// ---------------------------------------------------------------------------
// Key humanisation
// ---------------------------------------------------------------------------

describe('humaniseKey', () => {
  it('turns snake_case into a sentence', () => {
    expect(humaniseKey('total_leads')).toBe('Total leads')
  })

  it('turns camelCase into a sentence', () => {
    expect(humaniseKey('createdAt')).toBe('Created at')
  })

  it('handles kebab-case and repeated separators', () => {
    expect(humaniseKey('last--run-at')).toBe('Last run at')
  })

  it('survives a key that is only separators', () => {
    expect(humaniseKey('__')).toBe('__')
  })

  it('redacts and bounds a key, because keys are agent-authored too', () => {
    expect(humaniseKey('ghp_abcdef123456')).not.toContain('abcdef123456')
  })
})

// ---------------------------------------------------------------------------
// Redaction — the part a key-name allow-list cannot do
// ---------------------------------------------------------------------------

describe('redactSecrets', () => {
  const SECRETS = [
    'sk-abcdef1234567890',
    'sk-ant-api03-abcdef1234',
    'sk_live_abcdef1234567890',
    'ghp_abcdefghijklmnop1234',
    'gho_abcdefghijklmnop1234',
    'ghs_abcdefghijklmnop1234',
    'ghu_abcdefghijklmnop1234',
    'github_pat_11ABCDEFG_abcdefgh',
    'xoxb-123456789-abcdef',
    'xoxp-123456789-abcdef',
    'AKIAIOSFODNN7EXAMPLE',
    'AIzaSyD-abcdefghijklmnopqrstuvwxyz12345',
  ]

  it.each(SECRETS)('redacts %s as a whole value', (secret) => {
    const out = redactSecrets(secret)
    expect(out).toBe(REDACTED)
  })

  it.each(SECRETS)('redacts %s embedded mid-string under an innocuous key', (secret) => {
    // The case a key-name allow-list misses entirely: the key is `status`, which
    // any sane allow-list would permit, and the secret is in the VALUE.
    const { entries } = summarizePayload({ status: `failed: ${secret} rejected` })
    expect(entries[0].value).toContain(REDACTED)
    expect(entries[0].value).not.toContain(secret)
  })

  it('redacts the whole token, not just its prefix', () => {
    const out = redactSecrets('key=ghp_SUPERSECRETTAIL9999 end')
    expect(out).not.toContain('SUPERSECRETTAIL')
    expect(out).toBe('key=[redacted] end')
  })

  it('redacts a value whose secret straddles the truncation point', () => {
    const padding = 'a'.repeat(MAX_VALUE_CHARS - 10)
    const { entries } = summarizePayload({ note: `${padding} ghp_LEAKEDTAILVALUE1234` })
    expect(entries[0].value).not.toContain('LEAKEDTAIL')
  })

  it('redacts BEFORE truncating — proven with a pattern that needs a minimum tail', () => {
    // The `ghp_` case above passes under EITHER order and therefore pins
    // nothing: `\bghp_[A-Za-z0-9]+` needs one character after the prefix, so
    // the head that survives a cut still matches and is still replaced. (Broken
    // deliberately during review to check; it stayed green.)
    //
    // `\bAKIA[A-Z0-9]{4}` needs FOUR, so cutting the token mid-way leaves a
    // head the pattern no longer recognises — the residue that truncate-first
    // ships to the screen. `AKIAIO` here carries two characters of a real AWS
    // key id, which is the whole difference between the two orders.
    const padding = 'a'.repeat(MAX_VALUE_CHARS - 7)
    const { entries } = summarizePayload({ note: `${padding} AKIAIOSFODNN7EXAMPLE` })
    expect(entries[0].value).not.toContain('AKIA')
  })

  it('redacts every occurrence, not only the first', () => {
    const out = redactSecrets('a sk-one1111 b sk-two2222 c')
    expect(out).toBe('a [redacted] b [redacted] c')
  })

  it('is stable across repeated calls (module-level /g regex lastIndex)', () => {
    const input = 'ghp_abcdefghijkl'
    expect(redactSecrets(input)).toBe(redactSecrets(input))
    expect(redactSecrets(input)).toBe(REDACTED)
  })

  it('does not fire on ordinary words that contain a prefix as a substring', () => {
    // "task-force" contains "sk-"; there is no word boundary before it.
    const out = redactSecrets('task-force risk-register github_patch notes')
    expect(out).toBe('task-force risk-register github_patch notes')
  })
})

// ---------------------------------------------------------------------------
// summarizePayload
// ---------------------------------------------------------------------------

describe('summarizePayload', () => {
  it('lists top-level keys as humanised label/value pairs', () => {
    const { entries, truncated } = summarizePayload({
      total_leads: 42,
      source: 'linkedin',
      breakdown: { a: 1, b: 2 },
      recent: [1, 2, 3],
      missing: null,
    })

    expect(truncated).toBe(0)
    expect(entries.map((e) => [e.label, e.value])).toEqual([
      ['Total leads', '42'],
      ['Source', 'linkedin'],
      ['Breakdown', '2 fields'],
      ['Recent', '3 items'],
      ['Missing', '—'],
    ])
  })

  it('caps the entry list and counts the remainder', () => {
    const payload = {}
    for (let i = 0; i < MAX_ENTRIES + 7; i += 1) payload[`key_${i}`] = i

    const { entries, truncated } = summarizePayload(payload)

    expect(entries).toHaveLength(MAX_ENTRIES)
    expect(truncated).toBe(7)
  })

  it('describes a root-level array as N items, not as numbered keys', () => {
    // Exploding it into `0`, `1`, `2` … is a JSON dump with the braces removed.
    const { entries } = summarizePayload([{ a: 1 }, { a: 2 }, { a: 3 }])

    expect(entries).toHaveLength(1)
    expect(entries[0].label).toBe('Items')
    expect(entries[0].value).toBe('3 items')
  })

  it('returns nothing readable for null, undefined, {} and []', () => {
    for (const payload of [null, undefined, {}, []]) {
      expect(summarizePayload(payload)).toEqual({ entries: [], truncated: 0 })
    }
  })

  it('describes a scalar root payload as a single value', () => {
    const { entries } = summarizePayload('all clear')
    expect(entries).toEqual([{ key: '', label: 'Value', value: 'all clear', hint: 'text' }])
  })

  it('does not throw on a payload holding odd values', () => {
    expect(() => summarizePayload({
      fn: () => {}, sym: Symbol('x'), big: 10n, nested: { deep: { deeper: {} } },
    })).not.toThrow()
  })

  // -------------------------------------------------------------------------
  // The negative that defines the feature
  // -------------------------------------------------------------------------

  it('never emits a JSON serialisation of the payload', () => {
    const payload = {
      customer: { name: 'ACME', contact: { email: 'ops@acme.test' } },
      rows: [{ id: 1, secret: 'sk-deadbeef1234' }],
    }

    const { entries } = summarizePayload(payload)
    const rendered = entries.map((e) => `${e.label}${e.value}`).join('|')

    // Not the whole payload…
    expect(rendered).not.toContain(JSON.stringify(payload))
    // …and not any nested value verbatim either: depth is 1, so a nested object
    // is described by shape and its contents never reach the screen.
    expect(rendered).not.toContain('ACME')
    expect(rendered).not.toContain('ops@acme.test')
    expect(rendered).not.toContain('deadbeef')
  })

  it('has no JSON serialisation call anywhere in the module source', () => {
    // The behavioural test above proves the shipped paths are clean; this one
    // stops a future "just show the nested object" edit from reopening the bug
    // through a path no test happens to exercise.
    //
    // Comments stripped first: the module's own docstring explains why it must
    // never serialise, and an unstripped scan would fail on that explanation —
    // the trap `helpers/stripComments` exists for.
    const src = stripComments(readFileSync(MODULE, 'utf8'))
    expect(src).not.toContain('JSON.stringify')
  })
})
