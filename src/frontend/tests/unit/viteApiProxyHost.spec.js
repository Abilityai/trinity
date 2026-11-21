/**
 * The dev server's `/api/` proxy rule delivers the browser's Host to the
 * backend (abilityai/trinity#4476).
 *
 * Found in the v1.0.5 release testing: on the default local install, Settings →
 * MCP Keys advertised `http://backend:8080/mcp`, and a schedule's webhook URL
 * read `http://backend:8000/api/webhooks/<token>`. The backend builds both from
 * the request's Host, as it must behind the production nginx (`Host $host`).
 * The rule had `changeOrigin: true`, which replaces the browser's Host with the
 * target's — `backend:8000` in Docker. The `/a2a/` rule keeps the Host for the
 * same reason.
 *
 * Behaviour, not text. The spec resolves vite.config.js the way `vite` does,
 * keeps the proxy table as resolved but points every target at a stub server
 * (port 8000 is a real backend on a developer's machine), runs a real Vite dev
 * server with that table, and asserts what the stub receives. The whole table
 * goes in, so Vite's own first-match routing picks the rule: a rule added ahead
 * of `/api/` that rewrites the Host turns this red too. The config is resolved
 * twice: with DOCKER_ENV (the frontend container) and without (Vite on the host).
 *
 * The backend half — the URLs the real routes build from each delivered Host —
 * is tests/unit/test_4476_dev_proxy_host_urls.py at the repo root.
 */
import { describe, it, expect, beforeAll, afterAll, vi } from 'vitest'
import http from 'node:http'
import { createHash, randomBytes } from 'node:crypto'
import { mkdtempSync, rmSync } from 'node:fs'
import { tmpdir } from 'node:os'
import { dirname, join, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'
import { createServer, resolveConfig } from 'vite'

const here = dirname(fileURLToPath(import.meta.url))
const FRONTEND = resolve(here, '../..')
const WS_GUID = '258EAFA5-E914-47DA-95CA-C5AB0DC85B11'

const acceptFor = (key) => createHash('sha1').update(key + WS_GUID).digest('base64')

/** A stand-in backend that records what each request carried. */
async function startStub() {
  const seen = []
  const upgraded = new Set()
  const server = http.createServer((req, res) => {
    seen.push({ kind: 'http', url: req.url, headers: req.headers })
    res.writeHead(200, { 'content-type': 'application/json' })
    res.end('{}')
  })
  server.on('upgrade', (req, socket) => {
    upgraded.add(socket)
    seen.push({ kind: 'ws', url: req.url, headers: req.headers })
    socket.write(
      'HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n' +
        `Sec-WebSocket-Accept: ${acceptFor(req.headers['sec-websocket-key'])}\r\n\r\n`,
    )
  })
  await new Promise((done) => server.listen(0, '127.0.0.1', done))
  return {
    seen,
    origin: `http://127.0.0.1:${server.address().port}`,
    async close() {
      for (const socket of upgraded) socket.destroy()
      server.closeAllConnections()
      await new Promise((done) => server.close(done))
    },
  }
}

/**
 * The resolved proxy table with every target pointed at `origin`, nothing else
 * changed. Vite's proxy reads a string rule as `{ target, changeOrigin: true }`,
 * so a string is spelled out that way here — rewriting `/api/` as a string must
 * turn this red, not pass because a spread dropped the flag.
 */
function pointedAt(table, origin) {
  return Object.fromEntries(
    Object.entries(table).map(([context, rule]) => {
      if (!rule) return [context, rule]
      if (typeof rule === 'string') return [context, { target: origin, changeOrigin: true }]
      expect(typeof rule, `proxy rule ${context} is an options object`).toBe('object')
      return [context, { ...rule, target: origin }]
    }),
  )
}

/** GET through the dev server, sending exactly this Host. */
function get(port, path, host) {
  return new Promise((done, fail) => {
    const req = http.request({ host: '127.0.0.1', port, path, headers: { host } }, (res) => {
      res.resume()
      res.on('end', () => done(res.statusCode))
    })
    req.on('error', fail)
    req.end()
  })
}

/** A WebSocket opening handshake through the dev server, sending exactly this Host. */
function upgrade(port, path, host) {
  const key = randomBytes(16).toString('base64')
  return new Promise((done, fail) => {
    const req = http.request({
      host: '127.0.0.1',
      port,
      path,
      headers: {
        host,
        connection: 'Upgrade',
        upgrade: 'websocket',
        'sec-websocket-version': '13',
        'sec-websocket-key': key,
      },
    })
    req.on('upgrade', (res, socket) => {
      socket.destroy()
      done({ status: res.statusCode, accepted: res.headers['sec-websocket-accept'] === acceptFor(key) })
    })
    req.on('response', (res) => {
      res.resume()
      done({ status: res.statusCode, accepted: false })
    })
    req.on('error', fail)
    req.end()
  })
}

describe.each([
  ['the frontend container (DOCKER_ENV)', 'true', 'http://backend:8000'],
  ['Vite run on the host', '', 'http://localhost:8000'],
])('dev proxy, %s', (_label, dockerEnv, configuredTarget) => {
  let resolved
  let stub
  let dev
  let port
  let root

  beforeAll(async () => {
    vi.stubEnv('DOCKER_ENV', dockerEnv)
    resolved = await resolveConfig(
      { root: FRONTEND, configFile: resolve(FRONTEND, 'vite.config.js'), logLevel: 'error' },
      'serve',
    )
    stub = await startStub()
    root = mkdtempSync(join(tmpdir(), 'trinity-4476-'))
    dev = await createServer({
      configFile: false,
      root,
      cacheDir: join(root, '.vite'),
      logLevel: 'silent',
      plugins: [],
      optimizeDeps: { noDiscovery: true, include: [] },
      server: {
        host: '127.0.0.1',
        port: 0,
        strictPort: true,
        ws: false,
        watch: null,
        allowedHosts: resolved.server.allowedHosts,
        proxy: pointedAt(resolved.server.proxy, stub.origin),
      },
    })
    await dev.listen()
    port = dev.httpServer.address().port
  }, 30_000)

  afterAll(async () => {
    await dev?.close()
    await stub?.close()
    if (root) rmSync(root, { recursive: true, force: true })
    vi.unstubAllEnvs()
  })

  it('resolves vite.config.js for this mode', () => {
    // Harness check: the /api/ rule exists and DOCKER_ENV reached the config.
    expect(resolved.server.proxy['/api/']?.target).toBe(configuredTarget)
  })

  it.each([
    ['localhost', 'the page at http://localhost'],
    ['localhost:5173', 'a page served on a port'],
    ['203.0.113.20', 'the dev install opened from another machine'],
  ])('delivers Host %s to the backend unchanged (%s)', async (host) => {
    stub.seen.length = 0
    expect(await get(port, '/api/settings/mcp-external-access', host)).toBe(200)
    expect(stub.seen.map((r) => [r.kind, r.url])).toEqual([['http', '/api/settings/mcp-external-access']])
    expect(stub.seen[0].headers.host).toBe(host)
  })

  it('adds no X-Forwarded-* header: with one the backend advertises <origin>/mcp, which the dev server does not serve', async () => {
    stub.seen.length = 0
    await get(port, '/api/settings/mcp-external-access', 'localhost')
    expect(Object.keys(stub.seen[0].headers).filter((name) => name.startsWith('x-forwarded-'))).toEqual([])
  })

  it('carries the agent terminal WebSocket with the browser Host', async () => {
    stub.seen.length = 0
    const path = '/api/agents/demo/terminal?mode=claude'
    expect(await upgrade(port, path, 'localhost')).toEqual({ status: 101, accepted: true })
    expect(stub.seen.map((r) => [r.kind, r.url])).toEqual([['ws', path]])
    expect(stub.seen[0].headers.host).toBe('localhost')
  })
})
