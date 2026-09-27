/**
 * What the MCP Keys panel shows, and how much of it (#2202).
 *
 * The panel rendered every key an instance had ever minted — measured on one
 * install: 306 keys, 294 of them revoked (96%), ~71KB of DOM text, no filter and
 * no bound. Agent-scoped keys accumulate structurally (one per agent, plus a new
 * one per #1854 rotation, plus every ephemeral ghost), and revoked keys are never
 * removed, so the page grows for the life of the instance and the 12 keys that
 * still work are buried in the 294 that do not.
 *
 * Pure because the panel's rules are decidable and `vitest` runs
 * `environment: 'node'` — a rule inside the SFC is one no test can reach (the
 * ent#392 precedent). The component keeps only the wiring.
 */

/** How many rows render before "Show more". Bounds DOM weight, not the fetch. */
export const PAGE_SIZE = 25

/**
 * The rows this viewer may see at all.
 *
 * A non-admin never sees agent-scoped keys — that predicate is the panel's
 * existing one and is kept EXACTLY, because it is an access rule wearing a
 * filter's clothes: widening it here would disclose the fleet's agent names to
 * any signed-in user.
 */
export function visibleKeys(keys, { isAdmin = false } = {}) {
  const rows = Array.isArray(keys) ? keys : []
  return isAdmin ? rows : rows.filter((k) => k?.scope !== 'agent')
}

/** Split the visible set by state, so the panel can say what it is hiding. */
export function keyCounts(keys, { isAdmin = false } = {}) {
  const rows = visibleKeys(keys, { isAdmin })
  const active = rows.filter((k) => k?.is_active).length
  return { total: rows.length, active, revoked: rows.length - active }
}

/**
 * The filtered set, newest first.
 *
 * Revoked keys are hidden by DEFAULT and reachable by an explicit toggle: a
 * revoked key is a historical record, not a thing you can act on, and on a
 * long-lived instance it is 96% of the list. `query` matches the fields a person
 * actually searches by — name, prefix, agent — never the hash.
 */
export function filterKeys(keys, { isAdmin = false, showRevoked = false, query = '' } = {}) {
  const q = String(query || '').trim().toLowerCase()
  return visibleKeys(keys, { isAdmin })
    .filter((k) => (showRevoked ? true : !!k?.is_active))
    .filter((k) => {
      if (!q) return true
      return [k?.name, k?.key_prefix, k?.agent_name, k?.scope]
        .some((f) => String(f || '').toLowerCase().includes(q))
    })
}

/**
 * The rows to render, bounded. Returns the slice plus what it is holding back,
 * so the footer can be honest about the difference rather than just stopping.
 */
export function pageOf(rows, { limit = PAGE_SIZE } = {}) {
  const all = Array.isArray(rows) ? rows : []
  const bound = Math.max(1, limit)
  return { rows: all.slice(0, bound), shown: Math.min(bound, all.length), total: all.length,
           hasMore: all.length > bound }
}

/**
 * Why the list is empty — the three reasons are three different next actions,
 * and a panel that renders one dead "No API keys" for all of them tells a person
 * with 294 revoked keys that they have none.
 */
export function emptyReason({ total, filtered, query, showRevoked }) {
  if (total === 0) return 'none'
  if (query) return 'no-match'
  if (!showRevoked && filtered === 0) return 'all-revoked'
  return null
}
