/**
 * The top-nav link set and its active-state rules, as pure data (#1925).
 *
 * NavBar's row became a priority+ strip — visible links plus a counted
 * "More ▾" menu — and a strip that can hide a link has to render the SAME link
 * three times: inline, in the overflow menu, and in the hidden mirror row that
 * measures it. Three hand-written copies of six `<router-link>`s is three places
 * for an active-class predicate or a badge to drift, so the set is derived once
 * here and rendered by a `v-for`.
 *
 * Pure by necessity as well as by taste: vitest runs `environment: 'node'`, so
 * a predicate living inside the SFC is untestable (the ent#392 precedent). The
 * rules that actually decide something — which links exist, which one is lit,
 * what the badge says — are therefore all in this file and covered by
 * `tests/unit/navLinks.spec.js`.
 */

/**
 * @param {object}  o
 * @param {string}  o.path              current `route.path`
 * @param {boolean} o.hasAnyEnterprise  #847 — any entitled enterprise module
 * @param {number}  o.opsCount          operator-queue + notifications pending
 * @param {boolean} o.opsCritical       any critical/urgent item pending
 * @returns {Array<{id,label,to,active,target?,rel?,badge?,badgeCritical?,pill?}>}
 */
export function buildNavLinks({ path = '/', hasAnyEnterprise = false, opsCount = 0, opsCritical = false } = {}) {
  const links = [
    {
      id: 'dashboard',
      label: 'Dashboard',
      to: '/',
      // trinity-enterprise#260 — the Agents entry is gone (the page is now the
      // Dashboard's List mode), so Dashboard inherits the agent-detail
      // highlight and /agents/:name lights this up.
      active: path === '/' || path.startsWith('/agents'),
    },
    { id: 'library', label: 'Library', to: '/library', active: path.startsWith('/library') },
    {
      // #1109 — one Operations entry replaces the former Health / Ops /
      // Executions links, carrying one unified badge.
      id: 'operations',
      label: 'Operations',
      to: '/operations',
      active: path === '/operations',
      badge: opsCount > 0 ? (opsCount > 99 ? '99+' : String(opsCount)) : null,
      badgeCritical: opsCount > 0 && opsCritical,
    },
    // Settings is visible to ALL authenticated users (#302) — a non-admin sees
    // only the MCP Keys tab inside it.
    { id: 'settings', label: 'Settings', to: '/settings', active: path === '/settings' },
    {
      // ent#456 — a new tab, because the Workspace is a place you go and stay.
      // `target` is enough: Vue Router's guardEvent declines to intercept a
      // _blank click, so the link still resolves its href.
      id: 'workspace',
      label: 'Workspace',
      to: '/workspace',
      target: '_blank',
      rel: 'noopener',
      active: path.startsWith('/workspace'),
    },
  ]
  if (hasAnyEnterprise) {
    // #847 Phase 0 — visible iff ANY enterprise feature is entitled. OSS-only
    // builds have an empty feature list, so this link is absent entirely.
    links.push({
      id: 'enterprise',
      label: 'Enterprise',
      to: '/enterprise',
      active: path.startsWith('/enterprise'),
      pill: 'PRO',
    })
  }
  return links
}

/** The counted overflow trigger label — the design contract's "N more". */
export function moreLabel(hiddenCount) {
  return hiddenCount > 0 ? `${hiddenCount} more` : 'More'
}
