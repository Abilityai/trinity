// trinity-enterprise#584 — a capability is described once, in its skill's
// SKILL.md `description:` (requirements/skills.md §23). The backend already
// sends `null` for a missing or blank one; this is the display half of the same
// rule, so every surface that lists capabilities shows the same words for "no
// description": the Workspace rail and new-chat hints, the Skills tab, the
// Library and its assign dialog, and the connector picker.

export const NO_DESCRIPTION = 'No description yet'

// `{ text, missing }`: the description trimmed, or the empty state. Anything
// that is not a non-blank string is missing — an older payload may still carry
// a blank, and a render error is the wrong failure for a card.
export function describeSkill(description) {
  const text = typeof description === 'string' ? description.trim() : ''
  return text ? { text, missing: false } : { text: NO_DESCRIPTION, missing: true }
}
