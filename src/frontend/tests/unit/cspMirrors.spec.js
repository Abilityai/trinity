/**
 * The two Content-Security-Policy copies stay mirrored (abilityai/trinity-enterprise#967).
 *
 * Trinity ships its CSP twice: the Vite dev middleware (`vite.config.js`) and
 * the production nginx include (`security-headers.conf`). Their live consumer
 * is the browser, reading the header each one serves — neither is importable
 * here (the dev map is module-private; the prod copy is nginx syntax), so this
 * is a parity table over the two texts, the shape the 2026-07-01 / 2026-09-07
 * learnings asked for after one copy drifted.
 *
 * Pinned for the Nevermined card form (ent#967): `frame-src` lists the embed
 * host and `connect-src` both API hosts (the SDK refreshes its session with a
 * browser fetch), in BOTH copies, with no Nevermined staging `.dev` host, no new
 * `'unsafe-*'` source, and `Permissions-Policy: payment=()` untouched (TD-6).
 */
import { describe, it, expect } from 'vitest'
import { readFileSync } from 'node:fs'
import { fileURLToPath } from 'node:url'
import { dirname, resolve } from 'node:path'

const here = dirname(fileURLToPath(import.meta.url))
const FRONTEND = resolve(here, '../..')
const VITE = readFileSync(resolve(FRONTEND, 'vite.config.js'), 'utf8')
const NGINX = readFileSync(resolve(FRONTEND, 'security-headers.conf'), 'utf8')

/** The dev CSP: the concatenated string literals of the 'Content-Security-Policy' entry. */
function viteCsp(src) {
  const start = src.indexOf("'Content-Security-Policy':")
  expect(start, 'vite.config.js carries a Content-Security-Policy entry').toBeGreaterThan(-1)
  const end = src.indexOf('}', start)
  const body = src.slice(start + "'Content-Security-Policy':".length, end)
  return [...body.matchAll(/"([^"]*)"/g)].map((m) => m[1]).join('')
}

/** The prod CSP: the quoted value of the nginx add_header line. */
function nginxCsp(src) {
  const m = src.match(/^add_header Content-Security-Policy "([^"]*)" always;/m)
  expect(m, 'security-headers.conf carries an add_header Content-Security-Policy line').toBeTruthy()
  return m[1]
}

/** CSP string → { directive: [sources] }. */
function directives(csp) {
  const out = {}
  for (const part of csp.split(';')) {
    const [name, ...sources] = part.trim().split(/\s+/)
    if (name) out[name] = sources
  }
  return out
}

const DEV = directives(viteCsp(VITE))
const PROD = directives(nginxCsp(NGINX))

const EMBED = 'https://embed.nevermined.app'
const API_HOSTS = ['https://api.sandbox.nevermined.app', 'https://api.live.nevermined.app']

describe('CSP mirrors — dev (vite.config.js) and prod (security-headers.conf)', () => {
  it('both parse into real directive maps', () => {
    for (const map of [DEV, PROD]) {
      expect(map['default-src']).toEqual(["'self'"])
      expect(map['frame-ancestors']).toEqual(["'self'"])
    }
  })

  it('frame-src and connect-src are identical in both copies', () => {
    expect(DEV['frame-src']).toEqual(PROD['frame-src'])
    expect(DEV['connect-src']).toEqual(PROD['connect-src'])
  })

  it('the Nevermined embed host is framable and both Nevermined API hosts are reachable', () => {
    for (const map of [DEV, PROD]) {
      expect(map['frame-src']).toContain(EMBED)
      for (const host of API_HOSTS) expect(map['connect-src']).toContain(host)
    }
  })

  it("no Nevermined staging host (nevermined.dev) in either policy — the backend's environments are sandbox and live", () => {
    for (const csp of [viteCsp(VITE), nginxCsp(NGINX)]) expect(csp).not.toMatch(/nevermined\.dev/)
  })

  it("no 'unsafe-*' source beyond the ones each copy already carried", () => {
    const unsafe = (map) => Object.entries(map)
      .flatMap(([d, sources]) => sources.filter((s) => s.startsWith("'unsafe-")).map((s) => `${d} ${s}`))
      .sort()
    // Dev keeps Vite's HMR allowances on script-src; prod keeps inline styles only.
    expect(unsafe(DEV)).toEqual([
      "script-src 'unsafe-eval'",
      "script-src 'unsafe-inline'",
      "style-src 'unsafe-inline'",
    ])
    expect(unsafe(PROD)).toEqual(["style-src 'unsafe-inline'"])
  })

  it('Permissions-Policy still denies payment in both copies (TD-6)', () => {
    expect(VITE).toMatch(/'Permissions-Policy': '[^']*\bpayment=\(\)[^']*'/)
    expect(NGINX).toMatch(/^add_header Permissions-Policy "[^"]*\bpayment=\(\)[^"]*" always;/m)
  })
})
