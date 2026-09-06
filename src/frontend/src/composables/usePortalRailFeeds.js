import { computed, ref, watch } from 'vue'
import { usePortalLoopsStore } from '@/stores/portalLoops'
import { usePortalRailFeedsStore } from '@/stores/portalRailFeeds'
import {
  feedsFor,
  loadSeen,
  loopsSignalFrom,
  markSeen,
  saveSeen,
  updatedSignal,
} from '@/components/portal/portalRail'

/**
 * The ONE owner of what the Workspace rail's tabs read (trinity-enterprise#475).
 *
 * Slice 1 (ent#474) left the Work signal riding component emits and said the
 * store-derived signal is the shape every later tab uses. This is that shape:
 * the shell hands in what it already knows — is the rail on screen, which
 * tabs pass their door, who is in the chat, which tab is open — and gets back
 * the signals for Loops / Canvas / Files plus the refresh and reset hooks.
 *
 * Three rules the ent#458 review paid for, kept by construction here:
 *
 *   * ONE owner of `stores/portalLoops.js`. The strip used to own its own
 *     participants from inside two mount points, and the incoming/outgoing
 *     panels raced over one singleton (`ownedKey`). The shell is a single
 *     mount, so there is nothing to race.
 *   * The watch is keyed on the JOINED participant key and the FEED SET, not
 *     on array identity — `isPlatformSession` settles late, so `loops` can
 *     appear in the visible set after mount and the watch must re-fire for
 *     it; a re-render with the same participants must not.
 *   * Nothing is fetched behind a door or ahead of the stage verdict: `visible`
 *     false (the agent page, a loading / failed / empty stage, a deep link to
 *     an agent this caller cannot reach) clears both stores and returns; an
 *     empty participant list (a room's first beat) returns WITHOUT clearing,
 *     so the rail does not blank and refill while the room's own fetch lands.
 *
 * "Seen" markers: re-marked on every successful load while a feed-backed tab
 * is the open active tab — the design's "changed while that tab was NOT the
 * open, active tab" read literally — and persisted under the second key.
 *
 * @param {object} opts  reactive inputs from the shell
 *   visible      Ref<boolean>   `railVisibleFor(...)`
 *   tabs         Ref<Array>     `visibleTabs(...)` — THE door gate
 *   participants Ref<string[]>  `railParticipantsFor(...)`
 *   activeTab    Ref<string>    `railState.tab`
 *   open         Ref<boolean>   the column is open
 *   sheetOpen    Ref<boolean>   the mobile sheet is open
 *   storage      () => Storage|null
 */
export function usePortalRailFeeds({
  visible,
  tabs,
  participants,
  activeTab,
  open,
  sheetOpen,
  storage = () => null,
}) {
  const loops = usePortalLoopsStore()
  const feeds = usePortalRailFeedsStore()

  const wants = computed(() => feedsFor(tabs.value))
  const participantsKey = computed(() => participants.value.join(' '))
  const wantsKey = computed(() => `${wants.value.loops}|${wants.value.canvas}|${wants.value.files}`)

  // Which feed-backed tab is on screen right now, if any.
  const shown = computed(() => (open.value === true || sheetOpen.value === true) ? activeTab.value : null)
  const filesShown = computed(() => shown.value === 'files')

  watch([visible, participantsKey, wantsKey], ([isVisible]) => {
    if (!isVisible) { reset(); return }
    const names = participants.value
    if (!names.length) return
    const w = wants.value
    if (w.loops) {
      loops.setParticipants(names)
      loops.fetchLoops()
    } else if (loops.participants.length) {
      loops.clear()
    }
    feeds.setParticipants(names)
    feeds.setFeeds({ canvas: w.canvas, files: w.files })
    if (w.canvas || w.files) feeds.refresh({ uploads: filesShown.value })
  }, { immediate: true })

  // Opening a feed-backed tab is itself a trigger ("since last view"), and
  // the only time the container-backed inbox is read.
  watch(shown, (tab) => {
    if (tab === 'files') feeds.refresh({ uploads: true })
    else if (tab === 'canvas') feeds.refresh()
  })

  // ---- seen markers ---------------------------------------------------------
  const seen = ref(loadSeen(storage()))
  watch(seen, (s) => saveSeen(storage(), s), { deep: true })

  watch([shown, () => feeds.version], ([tab]) => {
    if (!feeds.hasLoaded) return
    if (tab === 'canvas') {
      seen.value = markSeen(seen.value, 'canvas', {
        itemsByAgent: feeds.canvases, field: 'updated_at', participants: participants.value,
      })
    } else if (tab === 'files') {
      seen.value = markSeen(seen.value, 'files', {
        itemsByAgent: feeds.documents, field: 'created_at', participants: participants.value,
      })
    }
  })

  // ---- signals ------------------------------------------------------------------
  // Derived on every render from the stores — never a latched flag.
  const signals = computed(() => ({
    loops: loopsSignalFrom(loops.active),
    canvas: updatedSignal({
      itemsByAgent: feeds.canvases, seen: seen.value.canvas, field: 'updated_at', participants: participants.value,
    }),
    files: updatedSignal({
      itemsByAgent: feeds.documents, seen: seen.value.files, field: 'created_at', participants: participants.value,
    }),
  }))

  /** A conversation turn ended / a room went idle: re-read what may have changed. */
  function refresh() {
    if (!visible.value || !participants.value.length) return
    if (wants.value.loops) loops.fetchLoops()
    feeds.refresh({ uploads: filesShown.value })
  }

  /** A chat switch: the reporters are gone, and so is their data. */
  function reset() {
    loops.clear()
    feeds.clear()
  }

  return { signals, seen, loops, feeds, refresh, reset, filesShown }
}
