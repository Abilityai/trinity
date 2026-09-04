/**
 * #2446 — `/m` requests must be bounded.
 *
 * `/m` deliberately does not use the `api.js` instance (it owns its auth and
 * 401 story), so it called the raw `axios` global, which carries no timeout. A
 * POST that never settles never reaches its `catch`, so `sendQueueResponse`
 * never runs `respondingItems[id] = false` — that card's Send stays disabled
 * AND, since PR #2378 exempts an in-flight id from `pruneQueueItemState`, its
 * per-card state stays unprunable for the life of the tab.
 *
 * Two things are pinned: the wrapper actually bounds every verb through the
 * GLOBAL axios, and the view has no bare `axios.*` call left for a future edit
 * to reintroduce. The second is a discovery guard over the source, so an
 * unbounded call added to any other `/m` handler fails too — the respond POST
 * is only the instance the issue reported; every other in-flight guard on that
 * surface (`togglingAgents`, `togglingAutonomy`, `actionLoading`, …) is pinned
 * by exactly the same hang.
 */
import { describe, it, expect, vi, beforeEach } from 'vitest'
import { readFileSync } from 'node:fs'
import { resolve, dirname } from 'node:path'
import { fileURLToPath } from 'node:url'
import { stripComments } from './helpers/stripComments'

const calls = []
vi.mock('axios', () => {
  const record = (verb) => (...args) => {
    calls.push([verb, ...args])
    return Promise.resolve({ data: {} })
  }
  return {
    default: {
      get: record('get'),
      post: record('post'),
      put: record('put'),
      patch: record('patch'),
      delete: record('delete'),
    },
  }
})

const { http, REQUEST_TIMEOUT_MS } = await import('../../src/utils/boundedHttp')

const HERE = dirname(fileURLToPath(import.meta.url))
const MOBILE_ADMIN = resolve(HERE, '../../src/views/MobileAdmin.vue')

beforeEach(() => {
  calls.length = 0
})

describe('boundedHttp', () => {
  it('matches the api.js instance timeout', () => {
    const apiSrc = readFileSync(resolve(HERE, '../../src/api.js'), 'utf8')
    const declared = /timeout:\s*(\d+)/.exec(stripComments(apiSrc))
    expect(declared, 'api.js must declare an instance timeout').toBeTruthy()
    expect(REQUEST_TIMEOUT_MS).toBe(Number(declared[1]))
  })

  it('bounds every verb it exposes', () => {
    http.get('/a')
    http.post('/b', { x: 1 })
    http.put('/c', { x: 1 })
    http.patch('/d', { x: 1 })
    http.delete('/e')
    expect(calls.map(c => c[0])).toEqual(['get', 'post', 'put', 'patch', 'delete'])
    for (const call of calls) {
      const config = call[call.length - 1]
      expect(config.timeout, `${call[0]} was unbounded`).toBe(REQUEST_TIMEOUT_MS)
    }
  })

  it('keeps caller config and lets an explicit timeout win', () => {
    http.get('/a', { params: { limit: 100 } })
    expect(calls[0][2]).toEqual({ timeout: REQUEST_TIMEOUT_MS, params: { limit: 100 } })
    calls.length = 0
    http.post('/b', null, { timeout: 1 })
    expect(calls[0][3].timeout).toBe(1)
  })

  it('calls the global axios rather than a snapshot instance', () => {
    // stores/auth.js authenticates by mutating axios.defaults.headers.common at
    // login and deleting it at logout; axios.create() snapshots defaults at
    // construction, so an instance would miss a later sign-in. Proven by the
    // absence of a create() call, since a mocked instance would not surface here.
    const src = stripComments(readFileSync(resolve(HERE, '../../src/utils/boundedHttp.js'), 'utf8'))
    expect(src).not.toMatch(/axios\.create\s*\(/)
  })
})

describe('MobileAdmin has no unbounded request left', () => {
  it('makes every call through the bounded wrapper', () => {
    const src = stripComments(readFileSync(MOBILE_ADMIN, 'utf8'))
    const bare = [...src.matchAll(/\baxios\.(get|post|put|patch|delete)\s*\(/g)].map(m => m[0])
    expect(bare, `unbounded axios call(s) in MobileAdmin.vue: ${bare.join(', ')}`).toEqual([])
    expect(src).toMatch(/from\s+'\.\.\/utils\/boundedHttp'/)
  })

  it('actually still issues requests — the guard is not vacuous', () => {
    const src = stripComments(readFileSync(MOBILE_ADMIN, 'utf8'))
    const wrapped = [...src.matchAll(/\bhttp\.(get|post|put|patch|delete)\s*\(/g)]
    expect(wrapped.length).toBeGreaterThan(10)
  })

  it('releases the in-flight guard on a failed respond', () => {
    // The timeout is only useful because this line is reachable once the
    // promise settles; before the bound it never was.
    const src = stripComments(readFileSync(MOBILE_ADMIN, 'utf8'))
    const fn = src.slice(src.indexOf('async function sendQueueResponse'))
    const body = fn.slice(0, fn.indexOf('\n}\n'))
    expect(body).toMatch(/catch[\s\S]*respondingItems\[id\] = false/)
  })
})
