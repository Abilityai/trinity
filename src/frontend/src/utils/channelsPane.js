/**
 * Settings › Channels (trinity-enterprise#340): what the pane decides, kept pure
 * so a spec executes it.
 */

/** Channels `&channel=` may open. Anything else opens nothing. */
export const DEEP_LINK_CHANNELS = Object.freeze(['slack', 'telegram', 'whatsapp', 'voip'])

export function deepLinkChannel(value) {
  return DEEP_LINK_CHANNELS.includes(value) ? value : null
}

// Each maps a channel's GET response to { connected, label, warn } for its row.
export const slackStatus = (d) => ({
  connected: !!d?.bound,
  label: d?.bound ? `#${d.channel_name}` : '',
})
export const telegramStatus = (d) => ({
  connected: !!d?.configured,
  label: d?.configured && d.bot_username ? `@${d.bot_username}` : '',
  warn: !!d?.configured && !d?.webhook_url,
})
export const whatsappStatus = (d) => ({
  connected: !!d?.configured,
  label: d?.configured ? (d.from_number || '') : '',
})
export const voipStatus = (d) => ({
  connected: !!d?.configured,
  label: d?.configured ? `${d.from_number || ''}${d.enabled ? '' : ' (disabled)'}` : '',
  warn: !!d?.configured && !d?.enabled,
})
export const publicLinksStatus = (links) => {
  const live = (links || []).filter((l) => l.enabled !== false).length
  return { connected: live > 0, label: live === 1 ? '1 link' : `${live} links` }
}

/** "18 people · 3 active this week". */
export function rosterSummary(clients, now = Date.now()) {
  const list = clients || []
  if (!list.length) return 'No one yet'
  const week = 7 * 24 * 60 * 60 * 1000
  const active = list.filter((c) => {
    const t = Date.parse(c.last_active)
    return !Number.isNaN(t) && now - t <= week
  }).length
  return `${list.length} ${list.length === 1 ? 'person' : 'people'} · ${active} active this week`
}

/**
 * The pane's header line. Each part is null until it's known, and a part that
 * failed or hasn't arrived is left out rather than guessed.
 * `reachable` is the names of connected surfaces; null means still checking.
 */
export function paneSummary({ mode = null, pending = null, reachable = null } = {}) {
  const parts = []
  if (mode) parts.push(mode === 'open' ? 'Open' : 'Restricted')
  if (pending !== null) parts.push(pending > 0 ? `${pending} waiting` : 'No one waiting')
  if (reachable !== null) {
    parts.push(reachable.length ? `Reachable on ${reachable.join(', ')}` : 'Not reachable anywhere yet')
  }
  return parts.join(' · ')
}
