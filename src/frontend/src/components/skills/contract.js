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
