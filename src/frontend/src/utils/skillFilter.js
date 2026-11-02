/**
 * The one rule behind "Filter skills" (trinity-enterprise#660, trinity#3448):
 * what a query matches, which sources and categories a list offers, and how
 * the three narrow it. The Skills tab, the Assign skills dialog and the Library
 * all read it, so they cannot disagree. Pure, so `tests/unit/skillFilter.spec.js`
 * runs it without a mount; `composables/useSkillFilter.js` holds the state.
 *
 * Skills come from a synced repository, so every value here is the author's
 * text. A surface interpolates it, never `v-html`.
 */

/** A name or description contains the query, ignoring case and outer spaces. */
export function matchesQuery(skill, query) {
  const q = String(query || '').trim().toLowerCase()
  if (!q) return true
  return skill.name.toLowerCase().includes(q) || String(skill.description || '').toLowerCase().includes(q)
}

function sourceKey(skill) {
  return skill?.source_id || skill?.source_name || ''
}

/**
 * The value's identity, folded: nothing binds a custom source to the library's
 * `catalog.yaml` enum, so "Project Management" and "project-management" are one
 * category, not two options. Empty for anything that is not a non-blank string.
 */
export function categoryKey(value) {
  if (typeof value !== 'string') return ''
  return value.trim().toLowerCase().replace(/[\s_-]+/g, '-').replace(/^-+|-+$/g, '')
}

/** "project-management" as a person reads it: "Project management". */
export function categoryLabel(key) {
  const text = String(key || '').replace(/-/g, ' ')
  return text ? text[0].toUpperCase() + text.slice(1) : ''
}

function sortedByLabel(options) {
  return [...options.values()].sort((a, b) => a.label.localeCompare(b.label))
}

/** @returns {Array<{value: string, label: string}>} the sources present, each once */
export function sourceOptions(skills = []) {
  const options = new Map()
  for (const skill of skills) {
    const value = sourceKey(skill)
    if (value && !options.has(value)) options.set(value, { value, label: skill.source_name || value })
  }
  return sortedByLabel(options)
}

/** @returns {Array<{value: string, label: string}>} the categories present, each once */
export function categoryOptions(skills = []) {
  const options = new Map()
  for (const skill of skills) {
    const value = categoryKey(skill.category)
    if (value && !options.has(value)) options.set(value, { value, label: categoryLabel(value) })
  }
  return sortedByLabel(options)
}

/** The rows the three filters keep, in their order. An empty filter keeps all. */
export function filterSkills(skills = [], { query = '', source = '', category = '' } = {}) {
  return skills.filter((skill) => matchesQuery(skill, query)
    && (!source || sourceKey(skill) === source)
    && (!category || categoryKey(skill.category) === category))
}
