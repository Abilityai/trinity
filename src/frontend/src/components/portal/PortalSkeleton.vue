<!--
  Workspace skeleton placeholders (#2540).

  The scanline beam is the CHART-loading motion (design-system §6, principle 12
  as amended 2026-09-06). Pages, panels, lists and message threads load with a
  skeleton placeholder keyed on "no data yet" — the shape the `#2159` sidebar
  and `PortalAgentPage` already use. This file is the one recipe for the three
  Workspace zones that used to wrap themselves in `ScanlineReveal` (#2163):

    stage     the whole conversation frame, while the roster and the deep link
              resolve — header, a thread, a composer, so the loaded surface
              lands on the same footprint (principle 4)
    thread    the transcript, while a thread's history loads
    briefing  the new-chat hint zone, while the agent's briefing hydrates

  Rules the recipe carries so its consumers cannot forget them:
    * `aria-busy` on the placeholder root and ONE `sr-only` line — a screen
      reader hears "loading", not three pulsing rectangles;
    * `animate-pulse motion-reduce:animate-none` — `prefers-reduced-motion`
      gets a static placeholder (§6);
    * the consumer keys `v-if` on a VERDICT (`stage.state === 'loading'`,
      `!historyLoaded`, `zone.state === 'pending'`), never on a fetch-in-flight
      flag (#1927), so a background refetch with data on screen never re-enters
      this branch — and never on a bare `<x>.loading` path, which the #1927
      ratchet counts as a bare gate;
    * the FOOTPRINT (`min-h`, `max-w`) belongs to the consumer's wrapper that
      both this placeholder and the loaded content sit inside, so the
      reservation survives the swap (principle 4);
    * gray-100 / gray-800-60 for content blocks, gray-200 / gray-800 for the
      heavier "heading" bars — the same two fills the sidebar rows use.

  Presentational: no store, no fetch, no decision.
-->
<template>
  <!-- ================================ STAGE ================================ -->
  <div
    v-if="variant === 'stage'"
    class="flex-1 min-h-0 flex flex-col"
    aria-busy="true"
    data-testid="portal-skeleton-stage"
  >
    <div class="shrink-0 flex items-center gap-2.5 px-3 sm:px-4 h-14 border-b border-gray-200 dark:border-gray-800">
      <div :class="[BLOCK_STRONG, 'w-[26px] h-[26px] rounded-full']"></div>
      <div :class="[BLOCK_STRONG, 'h-3 w-28 rounded']"></div>
    </div>
    <div class="flex-1 min-h-0 overflow-hidden px-3 sm:px-6 py-5">
      <div class="max-w-4xl mx-auto">
        <PortalSkeleton variant="thread" :announce="false" />
      </div>
    </div>
    <div class="shrink-0 border-t border-gray-200 dark:border-gray-800 px-3 sm:px-6 py-3">
      <div :class="[BLOCK, 'max-w-4xl mx-auto h-11 rounded-2xl']"></div>
    </div>
    <span class="sr-only">Loading your workspace…</span>
  </div>

  <!-- ================================ THREAD =============================== -->
  <!-- Three message-shaped rows: agent, you, agent. -->
  <div
    v-else-if="variant === 'thread'"
    class="space-y-6"
    :aria-busy="announce ? 'true' : undefined"
    data-testid="portal-skeleton-thread"
  >
    <div
      v-for="(row, i) in THREAD_ROWS"
      :key="i"
      :class="row.user ? 'flex justify-end' : 'flex items-start gap-2.5'"
    >
      <div v-if="!row.user" :class="[BLOCK_STRONG, 'w-7 h-7 rounded-full shrink-0 mt-0.5']"></div>
      <div :class="[BLOCK, row.user ? 'rounded-2xl rounded-br-md' : 'rounded-2xl rounded-bl-md', row.size]"></div>
    </div>
    <span v-if="announce" class="sr-only">Loading this conversation…</span>
  </div>

  <!-- =============================== BRIEFING ============================== -->
  <!-- What the hint zone's wrapper reserves room for: a description line, the
       overline, and one row of two hint cards. -->
  <div
    v-else-if="variant === 'briefing'"
    aria-busy="true"
    data-testid="portal-skeleton-briefing"
  >
    <div :class="[BLOCK, 'h-3.5 w-3/4 mx-auto rounded']"></div>
    <div class="mt-7">
      <div :class="[BLOCK_STRONG, 'h-2.5 w-24 rounded mb-3']"></div>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
        <div v-for="card in 2" :key="card" :class="[BLOCK, 'h-16 rounded-xl']"></div>
      </div>
    </div>
    <span class="sr-only">Loading suggestions…</span>
  </div>
</template>

<script setup>
defineProps({
  // 'stage' | 'thread' | 'briefing'
  variant: { type: String, required: true },
  // The `thread` variant is also nested inside `stage`, which already carries
  // the live-region text; the nested copy stays silent so a screen reader
  // hears one line, not two.
  announce: { type: Boolean, default: true },
})

// One pulse recipe, two weights. Written once here rather than per block so a
// motion or fill change lands on every placeholder at the same time.
const PULSE = 'animate-pulse motion-reduce:animate-none'
const BLOCK = `${PULSE} bg-gray-100 dark:bg-gray-800/60`
const BLOCK_STRONG = `${PULSE} bg-gray-200 dark:bg-gray-800`

// agent · you · agent — widths vary so the placeholder reads as a
// conversation rather than a stack of identical bars.
const THREAD_ROWS = [
  { user: false, size: 'h-16 w-3/5' },
  { user: true, size: 'h-10 w-2/5' },
  { user: false, size: 'h-12 w-1/2' },
]
</script>
