import { watch, unref } from 'vue'
import { usePortalDraftsStore } from '@/stores/portalDrafts'
import { reconcileDraftOnKeyChange } from '@/components/portal/portalDrafts'

/**
 * Bind a composer's text to the Workspace drafts store (trinity-enterprise#657).
 *
 * The composer IS the draft: every change is written through, so a send
 * (which empties the field) clears it in the same tick, and every path that
 * hands text back — a cancelled turn, a failed escalation, a room send that
 * failed — makes it a draft again with no extra plumbing. Shared by
 * `PortalConversation` and `PortalRoom` so the two composers cannot drift.
 *
 * `key` is a ref to the conversation's draft key (`draftKeyFor`), or null
 * while the thread is unresolved — nothing is written then. A key change on
 * the SAME instance (session adoption, a cold-root resolution, the `→ null`
 * hop before a remount) goes through `reconcileDraftOnKeyChange`.
 *
 * Returns `{ restored }` — true when a stored draft was put into an EMPTY
 * composer at setup. The component decides when to grow and focus the field
 * (the room's textarea sits behind `v-if="!isClosed"` until its load), which
 * is why this does not touch the DOM.
 */
export function useComposerDraft({ key, input }) {
  const drafts = usePortalDraftsStore()

  let restored = false
  const initialKey = unref(key)
  if (initialKey && !input.value) {
    const text = drafts.get(initialKey)
    if (text) {
      input.value = text
      restored = true
    }
  }

  watch(input, (text) => {
    const current = unref(key)
    if (current) drafts.set(current, text)
  })

  watch(() => unref(key), (newKey, oldKey) => {
    if (newKey === oldKey) return
    const { composer, writes } = reconcileDraftOnKeyChange({
      oldKey, newKey, composerText: input.value, read: (k) => drafts.get(k),
    })
    for (const [k, text] of writes) drafts.set(k, text)
    if (composer !== input.value) input.value = composer
  })

  return { restored }
}
