/**
 * The brain-orb page's vendored DOMPurify stays the locked one
 * (abilityai/trinity-enterprise#1117).
 *
 * `public/brain-orb/vendor/purify.min.js` is copied verbatim into `dist/` and
 * sanitises agent-authored markdown in `public/brain-orb/orb.js`. It is not a
 * package, so neither `npm audit` nor Dependabot ever sees it: it sat at 3.4.11
 * (inside the vulnerable ranges) while the locked dependency moved on. These
 * tests tie it to the lockfile so a DOMPurify bump that forgets the vendored
 * copy turns CI red instead of shipping a stale sanitiser.
 *
 * The copy must also be the UMD build (`dist/purify.min.js`). The ESM build
 * loaded through a classic `<script>` never sets `window.DOMPurify`, and
 * orb.js then silently falls back to plain text — no sanitiser, no error.
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'
import { JSDOM } from 'jsdom'

const here = dirname(fileURLToPath(import.meta.url))
const root = resolve(here, '../..')
const VENDORED = resolve(root, 'public/brain-orb/vendor/purify.min.js')
const vendored = readFileSync(VENDORED, 'utf8')
const lock = JSON.parse(readFileSync(resolve(root, 'package-lock.json'), 'utf8'))
const lockedVersion = lock.packages['node_modules/dompurify'].version

const bannerVersion = (src) => (src.match(/^\/\*! @license DOMPurify (\d+\.\d+\.\d+) /) || [])[1]

describe('vendored DOMPurify (brain-orb)', () => {
  it('banner version equals the lockfile dompurify version', () => {
    expect(lockedVersion, 'no dompurify entry in package-lock.json').toMatch(/^\d+\.\d+\.\d+$/)
    expect(bannerVersion(vendored), 'vendored purify.min.js has no DOMPurify license banner')
      .toBeDefined()
    expect(
      bannerVersion(vendored),
      'public/brain-orb/vendor/purify.min.js must be replaced with dist/purify.min.js ' +
        'from the locked dompurify tarball',
    ).toBe(lockedVersion)
  })

  it('is byte-identical to the installed dist/purify.min.js (the UMD build npm verified)', () => {
    // npm ci checks the tarball against the lock's integrity hash, so the
    // installed copy is the trusted reference for "the exact upstream file".
    const installed = readFileSync(
      resolve(root, 'node_modules/dompurify/dist/purify.min.js'), 'utf8',
    )
    expect(vendored === installed, 'vendored copy differs from the installed UMD build').toBe(true)
  })

  it('defines a working global DOMPurify when run as a classic script', () => {
    const { window } = new JSDOM('<!doctype html><body></body>', { runScripts: 'outside-only' })
    window.eval(vendored)
    expect(typeof window.DOMPurify, 'window.DOMPurify after loading the vendored file')
      .toBe('function')
    expect(window.DOMPurify.version).toBe(lockedVersion)
    expect(window.DOMPurify.isSupported).toBe(true)
    const out = window.DOMPurify.sanitize('<p>ok</p><img src=x onerror="alert(1)"><script>x()</script>')
    expect(out).toBe('<p>ok</p><img src="x">')
    window.close()
  })
})
