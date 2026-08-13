function formatPercent(value) {
  if (!Number.isFinite(value)) return '—'
  return Number.isInteger(value) ? String(value) : value.toFixed(1)
}

function formatTimestamp(value) {
  if (!value) return 'never'
  const date = new Date(value)
  if (Number.isNaN(date.getTime())) return 'unknown'
  return date.toLocaleString([], {
    month: 'short',
    day: 'numeric',
    hour: 'numeric',
    minute: '2-digit',
  })
}

export function shouldShowMeteredCost(usage) {
  return usage?.billing_mode === 'api'
}

export function usageBadgeLabel(usage) {
  if (usage?.billing_mode === 'subscription') {
    return `Claude subscription · ${usage.subscription?.name || 'Unnamed'}`
  }
  if (usage?.billing_mode === 'api') return 'Anthropic API · metered'
  return 'Claude auth not configured'
}

export function dashboardUsageText(usage) {
  if (usage?.billing_mode === 'subscription') {
    const name = usage.subscription?.name || 'Subscription'
    const utilization = usage.utilization
    if (utilization?.status === 'available') {
      return `${name} · ${formatPercent(utilization.percent)}% used`
    }
    return `${name} · usage unavailable`
  }
  if (usage?.billing_mode === 'api') return 'API · metered'
  return 'Usage not configured'
}

export function usageDetail(usage) {
  if (usage?.billing_mode === 'subscription') {
    const utilization = usage.utilization
    if (utilization?.status === 'available') {
      const reset = utilization.resets_at
        ? ` · Resets ${formatTimestamp(utilization.resets_at)}`
        : ''
      return `${formatPercent(utilization.percent)}% of ${utilization.window}${reset} · Updated ${formatTimestamp(utilization.last_updated_at)}`
    }
    return `Usage unavailable · Last updated: ${formatTimestamp(utilization?.last_updated_at)}`
  }
  if (usage?.billing_mode === 'api') {
    return 'Metered Anthropic API usage; dollar values are cost-oriented.'
  }
  return 'No Claude subscription or Anthropic API billing identity is configured.'
}
