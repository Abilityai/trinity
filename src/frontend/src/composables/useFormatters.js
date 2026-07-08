// ===========================================================================
// Cost formatting (#92) — the single, canonical way to render a USD cost.
// Do NOT inline `toFixed` on cost values or add local `formatCost` helpers;
// import these instead. Both return a string WITH the `$` (no separate prefix).
// ===========================================================================
const _usdWhole = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', maximumFractionDigits: 0,
})
const _usdCompact = new Intl.NumberFormat('en-US', {
  style: 'currency', currency: 'USD', notation: 'compact', maximumFractionDigits: 1,
})

/**
 * Default cost format: whole dollars with US comma separators (`$1,200`, `$12`).
 * Sub-dollar amounts keep 2-decimal precision (`$0.04`) since per-execution costs
 * are usually < $1 — a tiny positive that rounds to $0.00 shows `<$0.01`. `$0`
 * for zero/blank/invalid.
 * @param {number} value
 * @returns {string}
 */
export const formatCost = (value) => {
  const n = Number(value)
  if (!Number.isFinite(n) || n === 0) return '$0'
  const abs = Math.abs(n)
  if (abs < 1) {
    if (abs < 0.005) return n > 0 ? '<$0.01' : '>-$0.01'
    return `${n < 0 ? '-' : ''}$${abs.toFixed(2)}`
  }
  return _usdWhole.format(n)
}

/**
 * Compact cost for tight surfaces (stat cards, table cells, node badges):
 * `$1.2K`, `$3.4M` from ≥ $1,000; below that it matches {@link formatCost}.
 * @param {number} value
 * @returns {string}
 */
export const formatCostCompact = (value) => {
  const n = Number(value)
  if (!Number.isFinite(n)) return '$0'
  if (Math.abs(n) >= 1000) return _usdCompact.format(n)
  return formatCost(n)
}

/**
 * Composable for shared formatting functions
 * Used across agent-related components
 */
export function useFormatters() {
  // ===========================================================================
  // Timezone-Aware Timestamp Utilities
  // ===========================================================================
  //
  // IMPORTANT: Backend timestamps are in UTC with 'Z' suffix.
  // These utilities ensure consistent parsing regardless of user timezone.
  //
  // For display: Use toLocaleString() which auto-converts to user's timezone
  // For math/positioning: Use getTime() which returns timezone-agnostic Unix ms
  // ===========================================================================

  /**
   * Parse a UTC timestamp from the backend.
   *
   * Handles timestamps with or without timezone indicator:
   * - "2026-01-15T10:30:00Z" -> parsed as UTC (correct)
   * - "2026-01-15T10:30:00+00:00" -> parsed as UTC (correct)
   * - "2026-01-15T10:30:00" -> ASSUMED to be UTC (adds Z suffix)
   *
   * @param {string} timestamp - ISO 8601 timestamp string
   * @returns {Date} Date object in local timezone (but representing correct UTC time)
   */
  const parseUTCTimestamp = (timestamp) => {
    if (!timestamp) return new Date()

    // If no timezone indicator, assume UTC by adding 'Z'
    const hasTimezone = timestamp.endsWith('Z') ||
                       timestamp.includes('+') ||
                       /\d{2}:\d{2}$/.test(timestamp.slice(-6))

    if (!hasTimezone) {
      timestamp = timestamp + 'Z'
    }

    return new Date(timestamp)
  }

  /**
   * Get Unix timestamp (ms) from a UTC timestamp string.
   * Use this for timeline positioning and duration calculations.
   *
   * @param {string} timestamp - ISO 8601 timestamp string
   * @returns {number} Unix timestamp in milliseconds
   */
  const getTimestampMs = (timestamp) => {
    return parseUTCTimestamp(timestamp).getTime()
  }

  /**
   * Format a UTC timestamp for display in user's local timezone.
   *
   * @param {string} timestamp - ISO 8601 timestamp string (UTC)
   * @param {object} options - Intl.DateTimeFormat options
   * @returns {string} Formatted date string in user's local timezone
   */
  const formatLocalTime = (timestamp, options = {}) => {
    if (!timestamp) return ''
    const date = parseUTCTimestamp(timestamp)
    return date.toLocaleString(undefined, {
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit',
      ...options
    })
  }

  /**
   * Format a UTC timestamp as local date and time.
   *
   * @param {string} timestamp - ISO 8601 timestamp string (UTC)
   * @returns {string} Formatted as "Jan 15, 2026 10:30:00 AM" in user's timezone
   */
  const formatLocalDateTime = (timestamp) => {
    if (!timestamp) return ''
    const date = parseUTCTimestamp(timestamp)
    return date.toLocaleString(undefined, {
      month: 'short',
      day: 'numeric',
      year: 'numeric',
      hour: '2-digit',
      minute: '2-digit',
      second: '2-digit'
    })
  }

  /**
   * Format bytes to human readable (e.g., 1024 -> "1 KB")
   * Uses whole numbers for stable UI display (no decimals)
   */
  const formatBytes = (bytes) => {
    if (!bytes && bytes !== 0) return '0 B'
    const units = ['B', 'KB', 'MB', 'GB']
    let value = bytes
    let unitIndex = 0
    while (value >= 1024 && unitIndex < units.length - 1) {
      value /= 1024
      unitIndex++
    }
    return `${Math.round(value)} ${units[unitIndex]}`
  }

  /**
   * Format uptime to human readable (e.g., 3661 -> "1h 1m")
   */
  const formatUptime = (seconds) => {
    if (!seconds && seconds !== 0) return '0s'
    if (seconds < 60) return `${seconds}s`
    if (seconds < 3600) return `${Math.floor(seconds / 60)}m`
    if (seconds < 86400) {
      const hours = Math.floor(seconds / 3600)
      const mins = Math.floor((seconds % 3600) / 60)
      return mins > 0 ? `${hours}h ${mins}m` : `${hours}h`
    }
    const days = Math.floor(seconds / 86400)
    const hours = Math.floor((seconds % 86400) / 3600)
    return hours > 0 ? `${days}d ${hours}h` : `${days}d`
  }

  /**
   * Format relative time (e.g., "2 hours ago")
   * Correctly handles UTC timestamps from backend
   */
  const formatRelativeTime = (dateString) => {
    if (!dateString) return 'Unknown'
    const date = parseUTCTimestamp(dateString)
    const now = new Date()
    const diffSeconds = Math.floor((now - date) / 1000)

    if (diffSeconds < 60) return 'just now'
    if (diffSeconds < 3600) return `${Math.floor(diffSeconds / 60)} minutes ago`
    if (diffSeconds < 86400) return `${Math.floor(diffSeconds / 3600)} hours ago`
    if (diffSeconds < 604800) return `${Math.floor(diffSeconds / 86400)} days ago`
    return date.toLocaleDateString()
  }

  /**
   * Format token count for display (e.g., 45000 -> "45K")
   */
  const formatTokenCount = (count) => {
    if (!count && count !== 0) return '0'
    if (count < 1000) return count.toString()
    if (count < 1000000) return `${(count / 1000).toFixed(1)}K`
    return `${(count / 1000000).toFixed(2)}M`
  }

  /**
   * Format file size (same as formatBytes but using different precision)
   */
  const formatFileSize = (bytes) => {
    if (bytes === 0) return '0 B'
    const k = 1024
    const sizes = ['B', 'KB', 'MB', 'GB']
    const i = Math.floor(Math.log(bytes) / Math.log(k))
    return Math.round((bytes / Math.pow(k, i)) * 100) / 100 + ' ' + sizes[i]
  }

  /**
   * Format duration for display (e.g., 1500 -> "1.5s")
   */
  const formatDuration = (ms) => {
    if (!ms && ms !== 0) return ''
    if (ms < 1000) return `${ms}ms`
    if (ms < 60000) return `${(ms / 1000).toFixed(1)}s`
    return `${Math.floor(ms / 60000)}m ${Math.floor((ms % 60000) / 1000)}s`
  }

  /**
   * Format date with relative time (e.g., "5m ago")
   * Correctly handles UTC timestamps from backend
   */
  const formatDate = (dateString) => {
    const date = parseUTCTimestamp(dateString)
    const now = new Date()
    const diffMs = now - date
    const diffMins = Math.floor(diffMs / 60000)
    const diffHours = Math.floor(diffMs / 3600000)
    const diffDays = Math.floor(diffMs / 86400000)

    if (diffMins < 1) return 'Just now'
    if (diffMins < 60) return `${diffMins}m ago`
    if (diffHours < 24) return `${diffHours}h ago`
    if (diffDays < 7) return `${diffDays}d ago`
    return date.toLocaleDateString()
  }

  /**
   * Format credential source for display
   */
  const formatSource = (source) => {
    if (!source) return 'Unknown source'

    if (source.startsWith('mcp:')) {
      const mcpServer = source.replace('mcp:', '')
      return `MCP Server: ${mcpServer}`
    }

    if (source === 'env_file' || source === 'template:env_file') {
      return 'Environment variable'
    }

    if (source.startsWith('template:mcp:')) {
      const mcpServer = source.replace('template:mcp:', '')
      return `MCP Server: ${mcpServer}`
    }

    return source
  }

  /**
   * Get context bar color based on percentage
   */
  const getContextBarColor = (percent) => {
    if (percent >= 90) return 'bg-status-danger-500'
    if (percent >= 70) return 'bg-status-warning-500'
    return 'bg-status-success-500'
  }

  return {
    // Timezone utilities
    parseUTCTimestamp,
    getTimestampMs,
    formatLocalTime,
    formatLocalDateTime,
    // Formatting utilities
    formatBytes,
    formatUptime,
    formatRelativeTime,
    formatTokenCount,
    formatFileSize,
    formatDuration,
    formatDate,
    formatSource,
    getContextBarColor,
    // Cost (#92)
    formatCost,
    formatCostCompact
  }
}
