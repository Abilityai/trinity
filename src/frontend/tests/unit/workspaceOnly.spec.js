/**
 * trinity-enterprise#837 — the `user` rung is Workspace-only.
 *
 * The server is the boundary (403 `workspace_only` on every operator route).
 * These helpers keep the SPA from walking a member into it: where to land after
 * sign-in, where an operator URL sends a member, and what a page does when the
 * server refuses anyway (a cached role gone stale, a demotion mid-session).
 */
import { describe, it, expect, vi } from 'vitest'
import {
  isWorkspaceOnlyRole,
  landingAfterSignIn,
  operatorRouteRedirect,
  isWorkspaceOnlyRefusal,
  reactToWorkspaceOnly,
  setWorkspaceOnlyHandler,
  notifyWorkspaceOnly,
} from '../../src/utils/workspaceOnly'

const refusal = { response: { status: 403, data: { detail: { code: 'workspace_only', message: 'x' } } } }

describe('isWorkspaceOnlyRole — only the server-reported `user` rung', () => {
  it.each([
    ['user', true],
    ['operator', false],
    ['creator', false],
    ['admin', false],
    // A role still loading must not bounce an operator out of a deep link;
    // the API refuses whatever it must.
    [undefined, false],
    [null, false],
    ['', false],
  ])('%s → %s', (role, expected) => {
    expect(isWorkspaceOnlyRole(role)).toBe(expected)
  })
})

describe('landingAfterSignIn', () => {
  it('lands a member in the Workspace', () => {
    expect(landingAfterSignIn(undefined, 'user')).toEqual({ path: '/workspace' })
  })

  it('keeps a member on a Workspace, shared-canvas or public-chat redirect', () => {
    for (const p of ['/workspace/inbox', '/workspace/a/scout', '/canvas/s/tok_1', '/chat/tok_2']) {
      expect(landingAfterSignIn(p, 'user')).toBe(p)
    }
  })

  it("sends a member's operator redirect to the Workspace, an agent page to that agent", () => {
    expect(landingAfterSignIn('/agents/scout/executions/e1', 'user'))
      .toEqual({ path: '/workspace', query: { agent: 'scout' } })
    expect(landingAfterSignIn('/settings', 'user')).toEqual({ path: '/workspace' })
  })

  it('a crafted target is still refused before the role is consulted', () => {
    expect(landingAfterSignIn('//evil.example', 'user')).toEqual({ path: '/workspace' })
    expect(landingAfterSignIn('//evil.example', 'operator')).toBe('/')
  })

  it('leaves everyone else exactly where #3406 sent them', () => {
    expect(landingAfterSignIn(undefined, 'operator')).toBe('/')
    expect(landingAfterSignIn('/agents/scout', 'admin')).toBe('/agents/scout')
    expect(landingAfterSignIn('/agents/scout', undefined)).toBe('/agents/scout')
  })
})

describe('operatorRouteRedirect — the router guard', () => {
  const op = (path, params = {}) => ({ path, params, meta: { requiresAuth: true } })

  it('sends a member on an operator route to the Workspace', () => {
    expect(operatorRouteRedirect(op('/'), 'user')).toEqual({ path: '/workspace' })
    expect(operatorRouteRedirect(op('/settings'), 'user')).toEqual({ path: '/workspace' })
  })

  it("opens the agent's conversation for an agent page", () => {
    expect(operatorRouteRedirect(op('/agents/scout', { name: 'scout' }), 'user'))
      .toEqual({ path: '/workspace', query: { agent: 'scout' } })
    expect(operatorRouteRedirect(op('/agents/my%20agent'), 'user'))
      .toEqual({ path: '/workspace', query: { agent: 'my agent' } })
  })

  it('lets a member through a route that is not an operator page', () => {
    expect(operatorRouteRedirect({ path: '/workspace', meta: {} }, 'user')).toBeNull()
    expect(operatorRouteRedirect({ path: '/canvas/s/x', meta: {} }, 'user')).toBeNull()
  })

  it('lets operators, and an unknown role, through', () => {
    expect(operatorRouteRedirect(op('/settings'), 'operator')).toBeNull()
    expect(operatorRouteRedirect(op('/settings'), undefined)).toBeNull()
  })
})

describe('isWorkspaceOnlyRefusal', () => {
  it('recognises only the floor’s named 403', () => {
    expect(isWorkspaceOnlyRefusal(refusal)).toBe(true)
    expect(isWorkspaceOnlyRefusal({ response: { status: 403, data: { detail: 'Admin access required' } } })).toBe(false)
    expect(isWorkspaceOnlyRefusal({ response: { status: 401, data: { detail: { code: 'workspace_only' } } } })).toBe(false)
    expect(isWorkspaceOnlyRefusal(undefined)).toBe(false)
  })
})

describe('reactToWorkspaceOnly — a stale page gives way to the Workspace', () => {
  // The page believed `operator`; the re-read profile says `refreshedRole`.
  function collaborators(path, refreshedRole = 'user') {
    let role = 'operator'
    return {
      currentPath: () => path,
      refreshProfile: vi.fn(async () => { role = refreshedRole }),
      currentRole: () => role,
      replace: vi.fn(),
    }
  }

  it("an agent page moves to that agent's Workspace conversation once the re-read role is `user`", async () => {
    const c = collaborators('/agents/scout')
    await reactToWorkspaceOnly(c)
    expect(c.refreshProfile).toHaveBeenCalledTimes(1)
    expect(c.replace).toHaveBeenCalledWith({ path: '/workspace', query: { agent: 'scout' } })
  })

  it('any other operator page moves to the Workspace', async () => {
    const c = collaborators('/settings')
    await reactToWorkspaceOnly(c)
    expect(c.replace).toHaveBeenCalledWith({ path: '/workspace' })
  })

  // The body alone is not proof: an agent's own server can answer a proxied
  // file call with that 403, and an operator's tab must not move on it.
  it.each(['operator', 'creator', 'admin'])('a re-read role of %s stays put', async (role) => {
    const c = collaborators('/agents/scout', role)
    await reactToWorkspaceOnly(c)
    expect(c.refreshProfile).toHaveBeenCalledTimes(1)
    expect(c.replace).not.toHaveBeenCalled()
  })

  it('a failed re-read moves nothing and does not throw', async () => {
    const c = collaborators('/agents/scout')
    c.refreshProfile = vi.fn(async () => { throw new Error('offline') })
    await expect(reactToWorkspaceOnly(c)).resolves.toBeUndefined()
    expect(c.replace).not.toHaveBeenCalled()
  })

  it.each(['/workspace', '/workspace/inbox', '/canvas/s/x', '/chat/x', '/login'])(
    'stays put on %s',
    async (path) => {
      const c = collaborators(path)
      await reactToWorkspaceOnly(c)
      expect(c.replace).not.toHaveBeenCalled()
      expect(c.refreshProfile).not.toHaveBeenCalled()
    },
  )
})

describe('notifyWorkspaceOnly — one reaction per burst', () => {
  it('reports the named refusal once per window, and nothing else', () => {
    const handler = vi.fn()
    setWorkspaceOnlyHandler(handler)
    notifyWorkspaceOnly({ response: { status: 403, data: { detail: 'nope' } } }, 1_000)
    expect(handler).not.toHaveBeenCalled()
    notifyWorkspaceOnly(refusal, 10_000)
    notifyWorkspaceOnly(refusal, 10_500)   // the same page's other refused requests
    expect(handler).toHaveBeenCalledTimes(1)
    notifyWorkspaceOnly(refusal, 13_000)
    expect(handler).toHaveBeenCalledTimes(2)
  })

  it('a failing reaction never replaces the error being rejected', () => {
    setWorkspaceOnlyHandler(() => { throw new Error('boom') })
    expect(() => notifyWorkspaceOnly(refusal, 50_000)).not.toThrow()
    setWorkspaceOnlyHandler(() => Promise.reject(new Error('navigation aborted')))
    expect(() => notifyWorkspaceOnly(refusal, 60_000)).not.toThrow()
    setWorkspaceOnlyHandler(null)
  })
})
