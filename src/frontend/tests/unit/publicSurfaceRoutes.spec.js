// @vitest-environment jsdom
/**
 * #3406 — every page that opens without an operator session is one the 401
 * verdict knows about.
 *
 * `sessionLostVerdict` treats any path it does not recognise as an operator
 * surface: a 401 there ends the session and pushes `/login`. That is right for
 * the dashboard and wrong for a page a stranger opens — which is how a visitor
 * of `/chat/:token` was sent to "Sign in to manage your agents" for two
 * releases, and how `/canvas/s/:token`'s own sign-in card was pre-empted. The
 * verdict lives in `utils/platformSession.js`; the routes that need it live in
 * `router/index.js`, where nothing reminded the author of a new public page
 * that the other file exists.
 *
 * So: walk the REAL route table. A route reachable without `requiresAuth`
 * must be one the verdict classifies — an auth route, the Workspace, a public
 * chat link, a shared canvas. A new public token page fails here until it is
 * given an arm (or, if it really is an operator page, `requiresAuth: true`).
 */
import { describe, it, expect, vi } from 'vitest'

vi.mock('axios', () => {
  const instance = {
    get: vi.fn(async () => ({ data: {} })),
    post: vi.fn(async () => ({ data: {} })),
    defaults: { headers: { common: {} } },
    interceptors: { request: { use: vi.fn() }, response: { use: vi.fn() } },
  }
  return { default: { ...instance, create: vi.fn(() => instance) } }
})

import router from '../../src/router'
import {
  isAuthRoute,
  isPublicChatPath,
  isSharedCanvasPath,
  isWorkspacePath,
} from '../../src/utils/platformSession'

// A concrete URL for a route pattern: `/chat/:token` -> `/chat/x`.
const sample = (pattern) => pattern.replace(/:[A-Za-z_]+(\([^)]*\))?[?*+]?/g, 'x')

function classify(pattern) {
  const path = sample(pattern)
  if (isAuthRoute(path)) return 'auth route'
  if (isWorkspacePath(path)) return 'workspace'
  if (isPublicChatPath(path)) return 'public chat'
  if (isSharedCanvasPath(path)) return 'shared canvas'
  return null
}

describe('routes that open without an operator session (#3406)', () => {
  const open = router.getRoutes().filter((r) => r.meta?.requiresAuth !== true && !r.redirect)

  it('the table is the real one, not an empty list', () => {
    const paths = open.map((r) => r.path)
    expect(paths).toEqual(expect.arrayContaining(['/login', '/chat/:token', '/canvas/s/:token', '/workspace']))
  })

  it('every one of them is a surface the 401 verdict classifies', () => {
    const unclassified = open.map((r) => r.path).filter((p) => classify(p) === null)
    expect(unclassified, 'a public page the 401 verdict would treat as the operator dashboard').toEqual([])
  })

  it('the public token pages are classified as themselves', () => {
    expect(classify('/chat/:token')).toBe('public chat')
    expect(classify('/canvas/s/:token')).toBe('shared canvas')
  })
})
