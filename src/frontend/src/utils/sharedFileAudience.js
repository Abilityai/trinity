/**
 * Who a shared file is for, as the owner's Sharing panel says it
 * (trinity-enterprise#549).
 *
 * A row carries three facts from the backend: `addressed_to` (the email whose
 * Workspace Files tab lists the file), `addressed_to_channel` (the channel
 * identity, `whatsapp:+…`, display only) and `audience_source` (how the platform
 * decided). This turns them into `{ label, detail, ownerOnly }` without lying in
 * either direction — in particular "the turn had no person" and "the platform
 * could not tell which conversation the share came from" are both the owner
 * only, and are NOT the same claim: the second is recoverable, and an owner who
 * never sees it happen cannot fix the agent that caused it.
 *
 * Pure, so it is testable: `tests/unit/sharedFileAudience.spec.js`.
 */

const CHANNEL_NAMES = { whatsapp: 'WhatsApp', telegram: 'Telegram', slack: 'Slack' }

const OWNER_ONLY_REASONS = {
  none: 'No person in this turn',
  ambiguous: "Couldn't tell which conversation it came from",
}

const ADDRESSED_REASONS = {
  turn: 'The person in the conversation',
  override: 'Addressed by the agent',
}

/** `whatsapp:+15555550142` → `WhatsApp +15555550142`; an unknown channel stays readable. */
function describeChannel(address) {
  const text = String(address || '')
  const cut = text.indexOf(':')
  if (cut < 0) return text
  const channel = text.slice(0, cut)
  return `${CHANNEL_NAMES[channel] || channel} ${text.slice(cut + 1)}`
}

export function describeSharedFileAudience(file) {
  const email = file?.addressed_to || ''
  const channel = file?.addressed_to_channel || ''
  const source = file?.audience_source || ''

  if (email) {
    return {
      label: email,
      detail: channel ? describeChannel(channel) : (ADDRESSED_REASONS[source] || ''),
      ownerOnly: false,
    }
  }
  if (channel) {
    return {
      label: describeChannel(channel),
      detail: 'No verified email — not in any Files tab',
      ownerOnly: false,
    }
  }
  return {
    label: 'Owner only',
    detail: OWNER_ONLY_REASONS[source] || 'Shared before files had an addressee',
    ownerOnly: true,
  }
}
