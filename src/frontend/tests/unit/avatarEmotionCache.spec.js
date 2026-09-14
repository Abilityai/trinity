/**
 * #2374 — cycling emotion modes showed the PREVIOUS avatar's face.
 *
 * Emotion variants are served from a stable URL under
 * `Cache-Control: public, max-age=86400`, so the `?v=` the client appends is the
 * only thing separating a fresh variant from a day-old cached one. It was
 * `agent.avatar_url?.split('v=')[1] || '1'`:
 *
 *   1. read out of possibly-stale in-memory state, so an avatar regenerated in
 *      another tab left every emotion URL keyed to the old cache entry; and
 *   2. falling back to the CONSTANT `'1'` whenever `avatar_url` carried no
 *      `v=` — after which no regeneration could ever change the URL, and the
 *      browser served the old variant until the entry expired 24h later.
 *
 * `AgentDetail.vue` cannot be mounted here (`@vue/test-utils` is not a
 * dependency, vitest runs `environment: 'node'`), so the derivation lives in a
 * pure module and the wiring is asserted from source.
 */
import { describe, it, expect } from 'vitest'
import fs from 'fs'
import path from 'path'
import { fileURLToPath } from 'url'

import { emotionCacheVersion, emotionAvatarUrl } from '../../src/utils/avatarEmotion'

const __dirname = path.dirname(fileURLToPath(import.meta.url))
const agentDetail = fs.readFileSync(
  path.resolve(__dirname, '../../src/views/AgentDetail.vue'), 'utf8',
)

describe('#2374 — the version tracks the actual variants', () => {
  it('prefers the stamp the emotions endpoint returns', () => {
    expect(emotionCacheVersion({
      emotionsVersion: '1756300000',
      avatarUrl: '/api/agents/a/avatar?v=1750000000',
    })).toBe('1756300000')
  })

  it('a regenerated variant set changes the URL, so the cache is re-keyed', () => {
    const before = emotionAvatarUrl('a', 'happy', emotionCacheVersion({ emotionsVersion: '100' }))
    const after = emotionAvatarUrl('a', 'happy', emotionCacheVersion({ emotionsVersion: '200' }))
    expect(before).not.toBe(after)
  })

  it('falls back to the base avatar version when the backend predates the stamp', () => {
    expect(emotionCacheVersion({
      avatarUrl: '/api/agents/a/avatar?v=1750000000',
    })).toBe('1750000000')
  })

  it('treats "0" (no variants) as absent rather than as a new constant', () => {
    expect(emotionCacheVersion({
      emotionsVersion: '0',
      avatarUrl: '/api/agents/a/avatar?v=1750000000',
    })).toBe('1750000000')
  })
})

describe('#2374 — the constant fallback is gone', () => {
  it('never returns the literal 1 when nothing else is available', () => {
    // The whole defect: a constant means every emotion URL keys ONE cache entry
    // forever, and regeneration is invisible until it expires.
    const v = emotionCacheVersion({ fallback: 1756300000 })
    expect(v).toBe('1756300000')
    expect(v).not.toBe('1')
  })

  it('an avatar_url with no v= does not collapse to a constant', () => {
    const v = emotionCacheVersion({ avatarUrl: '/api/agents/a/avatar', fallback: 42 })
    expect(v).toBe('42')
  })

  it.each([
    ['empty v', '/api/agents/a/avatar?v='],
    ['v then another param', '/api/agents/a/avatar?v=&x=1'],
    ['no url at all', undefined],
    ['null url', null],
  ])('%s degrades to the per-load fallback, not a constant', (_l, url) => {
    expect(emotionCacheVersion({ avatarUrl: url, fallback: 77 })).toBe('77')
  })

  it('reads a v= that is followed by another param', () => {
    expect(emotionCacheVersion({ avatarUrl: '/api/agents/a/avatar?v=123&x=1' })).toBe('123')
  })

  it('defaults the fallback to now rather than to a constant', () => {
    const a = emotionCacheVersion({})
    expect(Number(a)).toBeGreaterThan(1_600_000_000_000)
  })
})

describe('#2374 — the URL is built from the version', () => {
  it('appends the version and escapes it', () => {
    expect(emotionAvatarUrl('scribe', 'happy', '123'))
      .toBe('/api/agents/scribe/avatar/emotion/happy?v=123')
  })
})

describe('#2374 — the view uses the rule (what only source can answer)', () => {
  it('no longer parses the version inline with a constant fallback', () => {
    expect(agentDetail).not.toMatch(/split\('v='\)\[1\]\s*\|\|\s*'1'/)
  })

  it('builds emotion URLs through the shared helpers', () => {
    expect(agentDetail).toContain('emotionCacheVersion(')
    expect(agentDetail).toContain('buildEmotionUrl(')
  })

  it('stores the stamp the endpoint returns', () => {
    expect(agentDetail).toMatch(/emotionVersion\.value = response\.data\.version/)
  })

  it('clears the stamp when the listing fails, rather than reusing a stale one', () => {
    // A failed poll that kept the old stamp would keep serving the old cache
    // entry — the same bug by a slower route.
    expect(agentDetail).toMatch(/catch[\s\S]{0,140}emotionVersion\.value = null/)
  })
})
