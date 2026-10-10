// trinity-enterprise#631 — tagging a person in a conversation.
//
// A tag is a POINTER, not a seat: naming a colleague in a chat or a room puts
// one item in THEIR Inbox (kind Unread — it asks for attention, not an
// answer), carrying who tagged them, which conversation and which message. It
// grants no access: a colleague who cannot see the conversation is told so,
// and who can let them in. Delivery is in-app only — the item waits in their
// Workspace Inbox; nothing is emailed (that is ent#564).
//
// Every decision the composer, the Inbox and the transcript make about a tag
// lives here, pure, so a node test reaches it (the ent#392 house rule).

// The picker reads people only once there is something to match: a bare `@`
// lists the conversation's agents and never a directory of colleagues (the
// ent#450 bound — the server refuses an empty query too).
export const PEOPLE_QUERY_MIN = 1
export const PEOPLE_DEBOUNCE_MS = 150
// The server's per-message cap (`person_mention_service.MAX_TAGS_PER_MESSAGE`).
export const MAX_TAGS_PER_MESSAGE = 10

const norm = (e) => String(e || '').trim().toLowerCase()

// The text a pick inserts. The display name, so the transcript reads like
// prose — the address travels beside the text, never parsed out of it.
export function personToken(person) {
  return `@${String(person?.label || person?.email || '').trim()}`
}

// Picked people, deduplicated by address (the same person picked twice is one
// tag — the server dedupes too, this keeps the composer honest).
export function addPicked(picked, person) {
  const list = Array.isArray(picked) ? picked : []
  const email = norm(person?.email)
  if (!email) return list
  if (list.some((p) => norm(p.email) === email)) return list
  return [...list, { email, label: String(person.label || email) }]
}

// What a send carries: the picked people whose token is STILL in the text. A
// person whose `@Name` the writer deleted is not tagged — the text is what
// they see, so the text is what they meant.
export function tagsInText(text, picked) {
  const s = String(text || '')
  const out = []
  for (const p of Array.isArray(picked) ? picked : []) {
    const email = norm(p?.email)
    if (!email || out.includes(email)) continue
    if (s.includes(personToken(p))) out.push(email)
  }
  return out.slice(0, MAX_TAGS_PER_MESSAGE)
}

// Picker rows for people, in the typeahead's row shape. `kind: 'person'` lets
// the accept handler tell a person from an agent without re-matching labels.
export function peopleRows(people) {
  return (Array.isArray(people) ? people : [])
    .filter((p) => p && p.email)
    .map((p) => ({ key: `pp-${norm(p.email)}`, primary: p.label || p.email, secondary: p.email, kind: 'person', person: p }))
}

// The picker's named refusal: a query that matched no agent AND no person says
// so, by name, with the reason — never a silent empty box (AC: "refuses an
// unknown name by name, with a reason"). Only once the people read answered.
export function noOneCalled(query, agentNames = []) {
  const q = String(query || '').trim()
  if (!q) return ''
  const names = (Array.isArray(agentNames) ? agentNames : []).filter(Boolean)
  const who = names.length ? `people who work with ${names.join(', ')}` : 'people on this instance'
  return `No one called “${q}” can be tagged here — you can tag ${who}.`
}

// The typeahead's heading once people are on offer.
export function typeaheadHeading({ people = false } = {}) {
  return people ? 'Agents and people' : 'Agents'
}

// ---- the tagger's marks ----------------------------------------------------

// One line under the tagger's own message: who, and how far it got. Honest
// state: delivered (it is in their Inbox — in-app only), read, or not
// delivered (the write failed after the message landed).
export function tagMarkText(tag) {
  const who = String(tag?.label || 'Someone')
  if (tag?.state === 'read') return `${who} · read`
  if (tag?.state === 'failed') return `${who} · not delivered`
  return `${who} · in their Inbox`
}

export function tagMarksTitle(tags) {
  const list = Array.isArray(tags) ? tags : []
  if (!list.length) return ''
  return 'Tagged people are told in their Trinity Inbox (in-app only — no email is sent). "Read" means they opened it.'
}

// The marks to draw on a message: the room's whole-room map wins (it is
// refreshed on every poll, so an older tag turns `read` without a reload),
// then the message's own list.
export function tagsFor(message, ownTags) {
  const id = message?.id
  if (id && ownTags && Array.isArray(ownTags[id])) return ownTags[id]
  return Array.isArray(message?.tags) ? message.tags : []
}

// A refused send names the person and the reason (the server's sentence).
export function tagRefusalMessage(err) {
  const d = err?.response?.data?.detail
  if (d && typeof d === 'object' && d.message) return d.message
  return null
}

// ---- the tagged person's Inbox item ----------------------------------------

export function mentionTitle(m) {
  return `${String(m?.tagged_by || 'Someone')} mentioned you`
}

export function mentionWhere(m) {
  const c = m?.conversation || {}
  if (c.kind === 'chat') return `in ${c.label || 'a chat'}`
  return c.label ? `in the room “${c.label}”` : 'in a room'
}

// What the pane says when the reader cannot see the conversation, and who can
// let them in. A chat is its owner's alone; a room has a moderator.
export function cantSeeText(detail) {
  const c = detail?.conversation || {}
  const names = (Array.isArray(detail?.can_let_you_in) ? detail.can_let_you_in : []).filter(Boolean)
  if (c.kind === 'chat') {
    const who = names[0] || 'the person who tagged you'
    return {
      title: "You can't see this chat",
      body: `It is ${who}'s own chat with an agent, so its messages aren't shown here. Ask ${who} to share what they wanted you to see, or to bring you into a room.`,
    }
  }
  const who = names.length ? joinNames(names) : 'an admin'
  return {
    title: "You're not in this room",
    body: `Its messages aren't shown here — a tag doesn't add you. ${who} ${names.length > 1 ? 'run' : 'runs'} this room and can let you in.`,
  }
}

function joinNames(names) {
  if (names.length <= 1) return names[0] || ''
  if (names.length === 2) return `${names[0]} and ${names[1]}`
  return `${names.slice(0, -1).join(', ')} and ${names[names.length - 1]}`
}

// Where "Open the room" goes — only for a reader who can see it.
export function mentionOpenTarget(detail) {
  const c = detail?.conversation || {}
  if (!detail?.can_see || c.kind !== 'room' || !c.id) return null
  return `/workspace/r/${encodeURIComponent(c.id)}`
}
