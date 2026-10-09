/**
 * The `user` rung is Workspace-only (trinity-enterprise#837).
 *
 * The server is the boundary: every operator route refuses a Workspace-only
 * principal with 403 `{code: 'workspace_only'}`. These helpers only keep the
 * SPA from walking a member into that wall — where to land after signing in,
 * where the router sends a member who opens an operator URL, and what a page
 * does when the server refuses anyway (a cached role gone stale, a demotion
 * mid-session).
 */
import { safeRedirect } from './safeRedirect'
import {
  isAuthRoute,
  isPublicChatPath,
  isSharedCanvasPath,
  isWorkspacePath,
} from './platformSession'

const WORKSPACE_HOME = '/workspace'

/** How the role pickers name the rung (ruled 2026-10-09; the other options keep their names). */
export const WORKSPACE_ONLY_ROLE_LABEL = 'user — Workspace only'

/**
 * Only the server-reported `user` rung. An unknown or missing role is NOT
 * Workspace-only here: a role still loading must not bounce an operator out of
 * a deep link, and the API refuses whatever it must.
 */
export function isWorkspaceOnlyRole(role) {
  return role === 'user'
}

/** The pages a member may stay on: the Workspace, a shared canvas, a public chat. */
function isMemberPage(path) {
  return isWorkspacePath(path) || isSharedCanvasPath(path) || isPublicChatPath(path)
}

/** An operator URL's Workspace counterpart: an agent page opens that agent's
 *  conversation (ent#358's `?agent=`), anything else the Workspace itself. */
function workspaceLocationFor(path) {
  const match = /^\/agents\/([^/?#]+)/.exec(path || '')
  if (!match) return { path: WORKSPACE_HOME }
  let agent = match[1]
  try { agent = decodeURIComponent(agent) } catch { /* keep it as typed */ }
  return { path: WORKSPACE_HOME, query: { agent } }
}

/** Where to go after signing in: #3406's `?redirect=`, role-aware. */
export function landingAfterSignIn(rawRedirect, role) {
  const target = safeRedirect(rawRedirect)
  if (!isWorkspaceOnlyRole(role)) return target
  return isMemberPage(target) ? target : workspaceLocationFor(target)
}

/** The router guard's answer on an operator route (`meta.requiresAuth`): the
 *  Workspace for a member, `null` (proceed) for everyone else. */
export function operatorRouteRedirect(to, role) {
  if (!isWorkspaceOnlyRole(role) || !to?.meta?.requiresAuth) return null
  return workspaceLocationFor(to.path)
}

/** A 403 the operator floor answered. */
export function isWorkspaceOnlyRefusal(error) {
  return error?.response?.status === 403 && error.response.data?.detail?.code === 'workspace_only'
}

/**
 * What a page does with that refusal: re-read the role and, when it is a
 * member's, give way to the Workspace — unless the page already is a member's
 * page or a sign-in page. The body alone is not proof (an agent's own server
 * can answer a proxied call with it), so only the re-read role moves the tab.
 * Collaborators are arguments so a test can watch what is called.
 */
export async function reactToWorkspaceOnly({ currentPath, refreshProfile, currentRole, replace }) {
  const path = currentPath()
  if (isMemberPage(path) || isAuthRoute(path)) return undefined
  try {
    await refreshProfile()
  } catch {
    return undefined
  }
  if (!isWorkspaceOnlyRole(currentRole())) return undefined
  return replace(workspaceLocationFor(path))
}

// One handler, set by main.js (which owns the router), fed by both HTTP
// clients' response interceptors. A page that fires several requests at once
// reports its burst of refusals once.
const NOTIFY_WINDOW_MS = 2000
let _handler = null
let _lastNotified = -Infinity

export function setWorkspaceOnlyHandler(fn) {
  _handler = fn
}

export function notifyWorkspaceOnly(error, now = Date.now()) {
  if (!_handler || !isWorkspaceOnlyRefusal(error)) return
  if (now - _lastNotified < NOTIFY_WINDOW_MS) return
  _lastNotified = now
  try {
    const result = _handler(error)
    // Vue Router rejects a redundant or aborted navigation; that must not
    // surface as an unhandled rejection (the #2791 rule for the 401 handler).
    if (result && typeof result.catch === 'function') result.catch(() => {})
  } catch {
    /* a failure to react must never replace the error being rejected */
  }
}
