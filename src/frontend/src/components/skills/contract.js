/**
 * Shared helpers for rendering the #183 skill-package contract (SkillInfo
 * fields). Extracted from SkillsPanel.vue (ent#263) so the per-agent Skills
 * tab and the Library page's fleet browse render the contract from ONE seam
 * and can't drift apart.
 */

export function formatBytes(n) {
  if (n < 1024) return `${n} B`
  if (n < 1024 * 1024) return `${(n / 1024).toFixed(1)} KB`
  return `${(n / 1024 / 1024).toFixed(1)} MB`
}

/**
 * Declared dependencies as one display line — this is exactly what turns into
 * a missing_binary/missing_env warning at inject time, surfaced BEFORE
 * assignment.
 */
export function deps(s) {
  const r = s.requires || {}
  const parts = []
  if (r.binaries?.length) parts.push(r.binaries.join(', '))
  if (r.packages?.length) parts.push(r.packages.join(', '))
  if (r.env?.length) parts.push(r.env.join(', '))
  return parts.join(' · ') || null
}

/**
 * Strip embedded credentials from a library repo URL before display — the
 * clone path accepts and stores `https://user:token@host/...` verbatim, so a
 * rendered URL must never carry the userinfo through to the screen.
 *
 * Primary lane: `new URL()` (WHATWG — handles whitespace/tab tricks, `\`
 * normalization, multi-`@`, uppercase schemes), but ONLY trusted when the
 * strip verifiably took: a hostless parse (`user:token@host/...` reads as
 * scheme `user` with an opaque path) makes `.username`/`.password` assignment
 * a SILENT NO-OP, and some engines no-op it for `file:` URLs too. Every other
 * shape falls to the textual scrub, which covers what an earlier regex missed
 * (ent#263 review — each was a proven leak): schemes beyond `\w`
 * (`git+ssh://…` with an invalid host char), protocol-relative
 * `//user:token@host`, schemeless `user:token@host/...`, scp-style
 * `user:token@host:path`, and leading whitespace defeating the `^` anchor.
 * A bare scp-style user (`git@host:path`) is not a secret and is kept; a
 * colon in the pre-`@` segment is credential-shaped and only the user
 * survives.
 */
export function stripUserinfo(u) {
  const s = String(u).trim()
  try {
    const parsed = new URL(s)
    parsed.username = ''
    parsed.password = ''
    if (parsed.host && !parsed.username && !parsed.password) {
      return parsed.toString()
    }
    // Strip didn't take (hostless/opaque parse, or an engine that no-ops the
    // setter) — fall through to the textual scrub.
  } catch {
    // Not URL-parseable — textual scrub.
  }
  return s
    // scheme://userinfo@ (scheme charset incl. +.-) and protocol-relative
    // //userinfo@ — greedy [^/]+ so the LAST @ before the path wins,
    // matching WHATWG semantics.
    .replace(/^((?:[A-Za-z][\w+.-]*:)?\/\/)[^/]+@/, '$1')
    // A credential-shaped pre-@ segment anywhere schemeless/opaque
    // (`user:token@host...`, `scheme:user:token@host...`) — drop the token,
    // keep the user.
    .replace(/^((?:[A-Za-z][\w+.-]*:)?[^@/\\:\s]+):[^/\\]*@/, '$1@')
}
