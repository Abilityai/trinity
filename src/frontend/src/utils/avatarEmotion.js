/**
 * Cache-busting for emotion avatar variants (#2374).
 *
 * Variants are served from a STABLE url — `/avatar/emotion/{emotion}` — under
 * `Cache-Control: public, max-age=86400`. The only thing separating a fresh
 * variant from a day-old cached one is the `?v=` the client appends, so getting
 * that version wrong shows the PREVIOUS avatar's face after a regeneration.
 *
 * It was `agent.avatar_url?.split('v=')[1] || '1'`, which fails two ways:
 *
 *   1. It reads the version out of possibly-stale in-memory state. An avatar
 *      regenerated in another tab, or before `agent` was refreshed, leaves
 *      `avatar_url` unchanged — so every emotion url keys the same day-old
 *      cache entry.
 *   2. When `avatar_url` carries no `v=` the fallback collapses to the CONSTANT
 *      `'1'`. Every emotion url is then permanently `?v=1`, and no regeneration
 *      can ever change it: the browser serves the old variant until the entry
 *      expires 24 hours later.
 *
 * The rule lives here because `AgentDetail.vue` cannot be mounted in this
 * project's test setup (`@vue/test-utils` is not a dependency, vitest runs
 * `environment: 'node'`), so a version derivation kept inline is one no test
 * can reach.
 */

/**
 * The cache-busting version for an agent's emotion URLs.
 *
 * Sources, best first:
 *   1. `emotionsVersion` — the stamp `GET /avatar/emotions` returns, taken from
 *      the newest variant file's mtime. It tracks the files these URLs actually
 *      serve, so it moves as background regeneration replaces them.
 *   2. the `v=` already on `avatarUrl` — the base avatar's `avatar_updated_at`.
 *      Correct whenever the agent payload is fresh; kept as the fallback for a
 *      backend that predates the stamp.
 *   3. `fallback` (default: now) — NEVER a constant. A constant is precisely
 *      what pinned the browser to one cache entry for 24 hours. A per-load
 *      value costs one refetch per page load and cannot go stale, which is the
 *      safe direction for a control whose failure mode is showing the wrong
 *      face.
 *
 * `"0"` from the endpoint means "no variants exist", which is not a version —
 * it falls through rather than becoming a new constant.
 */
export function emotionCacheVersion({ emotionsVersion, avatarUrl, fallback } = {}) {
  const stamp = emotionsVersion == null ? '' : String(emotionsVersion).trim()
  if (stamp && stamp !== '0') return stamp

  const fromUrl = typeof avatarUrl === 'string' && avatarUrl.includes('v=')
    ? avatarUrl.split('v=')[1]
    : ''
  // Guard against a `v=` that is empty or is followed by another param.
  const parsed = (fromUrl || '').split('&')[0].trim()
  if (parsed) return parsed

  return String(fallback != null ? fallback : Date.now())
}

/** The emotion variant URL, cache-busted by `version`. */
export function emotionAvatarUrl(agentName, emotion, version) {
  return `/api/agents/${agentName}/avatar/emotion/${emotion}?v=${encodeURIComponent(version)}`
}
