/**
 * trinity-enterprise#610 (D11) — the conversation's one-shot `?anchor=`.
 *
 * The Inbox pane's "Open in chat" navigates to `/workspace/c/:id?anchor=…` so
 * the chat opens AT the arrival the reader was looking at, not at the bottom.
 * Two anchor kinds, because the two arrival kinds live in different places:
 *
 *   `m:<messageId>`  a message — a `[data-message-id]` wrapper in the
 *                    transcript, resolvable once the history has rendered;
 *   `d:<reportId>`   a deliverable — a `[data-report-id]` card in the trailing
 *                    "Delivered here" section, which fetches AFTER the history,
 *                    so it resolves on that section's `loaded` event instead.
 *
 * Resolution is one-shot and honest either way:
 *   found      → stop following (`detach`, or the stick-to-bottom observer
 *                re-pins on the next re-flow and undoes the jump), scroll the
 *                target to the top of the viewport, outline it briefly;
 *   not found  → the bottom (where the thread already opened), plus a one-line
 *                notice that the message is further up — never a silent miss.
 * The `anchor` query key is stripped with `router.replace` in both cases, so a
 * reload or a back-navigation does not jump again.
 *
 * Kept out of `PortalConversation.vue` so the rule can be mounted and driven
 * on its own (`tests/unit/portalConversationAnchor.mount.spec.js`); the
 * component only supplies its scroll container, its stick-to-bottom controls,
 * and calls the two `after*` hooks.
 */
import { ref, watch, nextTick, onScopeDispose } from 'vue'

export const ANCHOR_QUERY_KEY = 'anchor'
export const ANCHOR_MISSING_NOTICE = 'That message is further up'
export const ANCHOR_HIGHLIGHT_MS = 1600
// Token classes only (design contract): the focus-ring accent, both themes.
export const ANCHOR_HIGHLIGHT_CLASSES = ['ring-2', 'ring-action-primary-500', 'dark:ring-action-primary-400', 'rounded-xl']

/** `m:<id>` / `d:<id>` → `{kind: 'message'|'deliverable', id}`, else null. */
export function parseAnchor(raw) {
  if (typeof raw !== 'string') return null
  const m = /^([md]):(.+)$/.exec(raw.trim())
  if (!m) return null
  return { kind: m[1] === 'm' ? 'message' : 'deliverable', id: m[2] }
}

/** The element carrying `data-<attr>` equal to `id` (no CSS escaping needed). */
function findByData(root, attr, key, id) {
  if (!root || typeof root.querySelectorAll !== 'function') return null
  for (const el of root.querySelectorAll(`[${attr}]`)) {
    if (el.dataset?.[key] === id) return el
  }
  return null
}

/**
 * @param {object} opts
 * @param {import('vue').Ref<HTMLElement|null>} opts.scrollEl  the transcript's scroll container
 * @param {() => void} opts.detach                             stick-to-bottom's detach()
 * @param {() => Promise<void>|void} [opts.pinToBottom]         for the not-found fallback
 * @param {object|null} opts.route                              vue-router route (reactive)
 * @param {object|null} opts.router                             vue-router instance
 */
export function useConversationAnchor({ scrollEl, detach, pinToBottom, route, router }) {
  const pending = ref(null)
  const notice = ref('')
  let highlightTimer = null
  let noticeTimer = null

  if (route) {
    watch(() => route.query?.[ANCHOR_QUERY_KEY], (raw) => {
      const parsed = parseAnchor(Array.isArray(raw) ? raw[0] : raw)
      if (parsed) { pending.value = parsed; notice.value = '' }
    }, { immediate: true })
  }

  function strip() {
    if (!router || !route || route.query?.[ANCHOR_QUERY_KEY] === undefined) return
    const query = { ...route.query }
    delete query[ANCHOR_QUERY_KEY]
    // A failed replace (a navigation superseded by another) is not the
    // reader's problem; the anchor is already consumed in memory.
    Promise.resolve(router.replace({ path: route.path, query, hash: route.hash })).catch(() => {})
  }

  function highlight(el) {
    clearTimeout(highlightTimer)
    el.classList.add(...ANCHOR_HIGHLIGHT_CLASSES)
    highlightTimer = setTimeout(() => el.classList.remove(...ANCHOR_HIGHLIGHT_CLASSES), ANCHOR_HIGHLIGHT_MS)
  }

  async function settle(target) {
    pending.value = null
    if (target) {
      // A spoken turn sits inside a collapsed voice-call block — open it, or
      // the scroll lands on an element with no box.
      const details = target.closest?.('details')
      if (details && !details.open) details.open = true
      detach()
      target.scrollIntoView?.({ block: 'start' })
      highlight(target)
    } else {
      notice.value = ANCHOR_MISSING_NOTICE
      clearTimeout(noticeTimer)
      noticeTimer = setTimeout(() => { notice.value = '' }, 8000)
      if (pinToBottom) await pinToBottom()
    }
    strip()
  }

  /** Call once the history has rendered (after the thread's own pin). */
  async function afterHistory() {
    if (pending.value?.kind !== 'message') return
    await nextTick()
    await settle(findByData(scrollEl.value, 'data-message-id', 'messageId', pending.value.id))
  }

  /** Bind to PortalDeliverables' `loaded` event. */
  async function afterDeliverables() {
    if (pending.value?.kind !== 'deliverable') return
    await nextTick()
    await settle(findByData(scrollEl.value, 'data-report-id', 'reportId', pending.value.id))
  }

  function dismissNotice() { notice.value = ''; clearTimeout(noticeTimer) }

  onScopeDispose(() => { clearTimeout(highlightTimer); clearTimeout(noticeTimer) })

  return { pending, notice, afterHistory, afterDeliverables, dismissNotice }
}
