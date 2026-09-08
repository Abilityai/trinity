/**
 * ent#554 — the decidable rules behind sharing a canvas.
 *
 * Pure module, so these run in the node environment the suite uses. The rules
 * that matter here are about what a person is TOLD: which reach they are
 * choosing, and why a link they were sent will not open.
 */
import { describe, it, expect } from 'vitest'
import { SHARE_SCOPES, scopeCopy, shareProblem, shareSummary, shareUrl } from '../../src/components/canvas/canvasShare'

describe('scopeCopy', () => {
  it('names the wider reach as wider, at the point of choosing', () => {
    const wide = scopeCopy('public')
    expect(wide.wide).toBe(true)
    expect(wide.detail.toLowerCase()).toContain('no sign-in')
    expect(wide.detail.toLowerCase()).toContain('forward')
  })

  it('the narrow reach explains that access is re-checked', () => {
    const narrow = scopeCopy('authorized')
    expect(narrow.wide).toBe(false)
    expect(narrow.detail.toLowerCase()).toContain('signing in')
  })

  it('an unknown scope reads as the NARROW one, never the wide one', () => {
    // Fail-narrow: the failure direction on a sharing control is a link that
    // reaches further than the sharer understood.
    expect(scopeCopy('nonsense').wide).toBe(false)
    expect(scopeCopy(undefined).wide).toBe(false)
  })

  it('the narrow scope is first, so it is what a radio group preselects', () => {
    expect(SHARE_SCOPES[0]).toBe('authorized')
  })
})

describe('shareProblem', () => {
  it('a revoked link says it was turned off, and does not read as broken', () => {
    const p = shareProblem(410, { status: 'revoked' })
    expect(p.title.toLowerCase()).toContain('turned off')
    expect(p.body.toLowerCase()).toContain('new one')
  })

  it('an expired link is distinct from a revoked one', () => {
    expect(shareProblem(410, { status: 'expired' }).title.toLowerCase()).toContain('expired')
  })

  it('a signed-out viewer of an authorized link is offered a sign-in', () => {
    const p = shareProblem(401, { status: 'sign_in_required' })
    expect(p.action).toBe('sign-in')
  })

  it('a signed-in viewer without access is told so, with no dead action', () => {
    const p = shareProblem(403, { status: 'not_authorized' })
    expect(p.action).toBeNull()
    expect(p.title.toLowerCase()).toContain('not shared with you')
  })

  it('an unknown token and a deleted canvas read identically', () => {
    // The uniform-404 half of the contract: a stranger guessing tokens must not
    // be able to tell a real one from a fabricated one.
    const unknown = shareProblem(404, 'Invalid or expired link')
    const deleted = shareProblem(404, { status: 'not_found' })
    expect(unknown).toEqual(deleted)
  })

  it('falls back to the same not-found wording for an unrecognised failure', () => {
    expect(shareProblem(500, null).title).toBe(shareProblem(404, null).title)
  })
})

describe('shareUrl', () => {
  it('resolves the relative path the server returns against the current origin', () => {
    expect(shareUrl('/canvas/s/abc', 'https://trinity.example.dev'))
      .toBe('https://trinity.example.dev/canvas/s/abc')
  })

  it('does not double a slash, whatever the origin looks like', () => {
    expect(shareUrl('/canvas/s/abc', 'https://x.dev/')).toBe('https://x.dev/canvas/s/abc')
  })

  it('leaves an absolute URL alone', () => {
    expect(shareUrl('https://other.dev/canvas/s/a', 'https://x.dev'))
      .toBe('https://other.dev/canvas/s/a')
  })

  it('survives junk', () => {
    expect(shareUrl(null, null)).toBe('')
  })
})

describe('shareSummary', () => {
  const base = { scope: 'authorized', view_count: 0, created_at: '2026-01-01T00:00:00Z' }

  it('says the reach in words, never the stored value', () => {
    expect(shareSummary(base)).toContain('People who already have access')
    expect(shareSummary(base)).not.toContain('authorized')
  })

  it('counts views in plain language', () => {
    expect(shareSummary({ ...base, view_count: 1 })).toContain('1 view')
    expect(shareSummary({ ...base, view_count: 4 })).toContain('4 views')
  })

  it('marks a revoked link', () => {
    expect(shareSummary({ ...base, revoked_at: '2026-02-01T00:00:00Z' })).toContain('revoked')
  })

  it('marks an expired link, without calling it revoked', () => {
    const s = shareSummary({ ...base, expires_at: '2020-01-01T00:00:00Z' })
    expect(s).toContain('expired')
    expect(s).not.toContain('revoked')
  })

  it('a live link says neither', () => {
    const s = shareSummary({ ...base, expires_at: '2999-01-01T00:00:00Z' })
    expect(s).not.toContain('expired')
    expect(s).not.toContain('revoked')
  })

  it('survives junk', () => {
    expect(shareSummary(null)).toBeNull()
  })
})
