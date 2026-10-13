import { describe, it, expect } from 'vitest'
import { publicLinkUrl } from '../../src/utils/publicLinkUrl.js'

/**
 * #3170 — with FRONTEND_URL unset the backend returns `url` as `/chat/<token>`,
 * and the panel copied that path verbatim. The shown and copied link must be
 * absolute, resolved against the page origin.
 */
const ORIGIN = 'http://localhost:8090'

describe('publicLinkUrl', () => {
  it('resolves a relative url against the page origin', () => {
    expect(publicLinkUrl({ url: '/chat/abc' }, ORIGIN)).toBe('http://localhost:8090/chat/abc')
  })

  it('keeps an absolute url from FRONTEND_URL unchanged', () => {
    expect(publicLinkUrl({ url: 'https://trinity.example.com/chat/abc' }, ORIGIN))
      .toBe('https://trinity.example.com/chat/abc')
  })

  it('prefers external_url when set', () => {
    expect(publicLinkUrl({ url: '/chat/abc', external_url: 'https://public.example.com/chat/abc' }, ORIGIN))
      .toBe('https://public.example.com/chat/abc')
  })

  it('returns empty string when the link has no url', () => {
    expect(publicLinkUrl({}, ORIGIN)).toBe('')
  })
})
