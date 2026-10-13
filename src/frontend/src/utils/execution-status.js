/**
 * Execution Status Utility (THINK-001)
 *
 * Maps Claude Code stream-json events to human-readable status labels
 * for the dynamic thinking status indicator in Chat.
 *
 * trinity-enterprise#620: the label is now COMPOSED by `utils/workActivity.js`
 * — the one vocabulary the Workspace card and Agent Detail share — so
 * "Reading `…/router.py`" reads the same in the operator Chat tab as on the
 * Work card. This module keeps its API (the two chat surfaces consume it)
 * and owns only the chat-specific framing: the `init` label, the trailing
 * ellipsis the loading indicator has always carried, and the
 * "Processing results…" beat after a tool result.
 */
import { activityFromStreamEvent, activityLine } from './workActivity'

/**
 * Extract a human-readable status label from a stream-json event.
 *
 * @param {Object} event - Parsed stream-json event from Claude Code
 * @returns {string|null} Status label, or null if no status change
 */
export function getStatusFromStreamEvent(event) {
  if (!event) return null

  // Init event
  if (event.type === 'init') return 'Starting session...'

  // Result / stream end / error = no status change here
  if (event.type === 'result' || event.type === 'stream_end' || event.type === 'error') return null

  // A tool result is a beat of its own on this surface (the card says "Thinking").
  const content = event.message?.content || event.content || []
  if (Array.isArray(content) && content.some((b) => b && b.type === 'tool_result')) {
    return 'Processing results...'
  }

  const facts = activityFromStreamEvent(event)
  if (!facts) return null
  if (facts.tool === 'Reply') return 'Responding...'
  const line = activityLine(facts)
  return line ? `${line.text}...` : null
}

/**
 * Minimum display time (ms) per label to prevent flicker on fast tool calls.
 */
export const MIN_LABEL_DISPLAY_MS = 500

/**
 * Heartbeat timeout (ms) - fall back to "Working..." if no events received.
 */
export const HEARTBEAT_TIMEOUT_MS = 10000
