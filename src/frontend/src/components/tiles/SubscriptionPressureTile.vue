<template>
  <InfoTile
    scope="Fleet"
    title="Subscription pressure"
    :stamp="stamp"
    :stamp-title="stampTitle"
    :state="state"
    empty-title="No subscriptions configured"
    empty-hint="Register a Claude subscription in Settings → Integrations to track its limits here."
    error-text="Couldn't read subscriptions. This tile only — the rest of the board is unaffected."
    :on-retry="retry"
    :link-to="SUBSCRIPTION_SETTINGS_ROUTE"
    link-label="Open subscriptions →"
  >
    <template #icon>
      <!-- No fill/stroke attributes: `.it-peg :deep(svg)` sets
           `fill: none; stroke: currentColor`, so the glyph themes itself. -->
      <svg viewBox="0 0 24 24" width="18" height="18">
        <path d="M4 17.5a8 8 0 0 1 16 0"></path>
        <path d="M12 17.5 16 13"></path>
        <path d="M12 17.5v.01"></path>
      </svg>
    </template>

    <TileRowList :rows="rows" :track-count="SUBSCRIPTION_TILE_MAX_ROWS">
      <template #lead="{ row }">
        <span v-if="row.overflow" class="sp-chip sp-more">···</span>
        <span v-else-if="row.severity === 'crit'" class="sp-chip sp-crit">limit</span>
        <!-- "429s" already happened; "near" has not — different actions. -->
        <span v-else-if="row.severity === 'warn'" class="sp-chip sp-warn">
          {{ row.warnReason === 'events' ? '429s' : 'near' }}
        </span>
        <span v-else-if="row.severity === 'unknown'" class="sp-chip sp-unknown">?</span>
        <span v-else class="sp-dot" aria-hidden="true"></span>
      </template>

      <!-- How much of each rolling limit is spent, colour-banded. The two
           windows are Anthropic's 5-hour and 7-day rolling limits; they render
           side by side because either one can be the wall you hit, and a
           subscription can sit at 20% of its 5h while its weekly is nearly
           gone. When no fresh provider snapshot backs them, `row.windows` is
           null and the qualitative fallback renders instead — a percentage is
           never synthesized from consumption alone. -->
      <template #meta="{ row }">
        <span v-if="row.windows" class="sp-wins">
          <span v-for="w in row.windows" :key="w.label" class="sp-win">
            <span class="sp-wlab">{{ w.label }}</span>
            <span class="sp-wpct" :class="`sp-lvl-${w.level}`">{{ w.pct }}%</span>
          </span>
        </span>
        <span v-else>{{ row.meta }}</span>
      </template>
    </TileRowList>
  </InfoTile>
</template>

<script setup>
/**
 * Subscription pressure (ent#259) — per-SUBSCRIPTION headroom at a glance, so
 * "how much is left on any of my subscriptions?" is answerable from the board
 * instead of Settings → Integrations.
 *
 * Deliberately the inverse unit of #471's per-agent chip. That chip answers
 * "is THIS agent's funding under strain"; it structurally cannot answer which
 * subscription is the bottleneck, because agents share a subscription and burn
 * one 5h window between them — reading it off the board means grouping every
 * agent chip into buckets by eye, which is exactly the work a tile should do.
 *
 * ZERO backend change, per the operator's ruling on the issue (2026-08-19):
 * "a small build on GET /api/agents/subscription-pressure + the extended
 * GET /api/subscriptions/{id}/usage once #471 merges". Both endpoints exist;
 * `stores/subscriptions.js::fetchPressureData` composes them on the Grid's one
 * 60s visibility-aware batch poll. This component never fetches — viewport
 * culling UNMOUNTS a tile, so an `onMounted` fetch would re-issue on every pan
 * and the data must already be warm when the operator pans back.
 *
 * Every decision lives in `utils/subscriptionPressureTile.js`, because vitest
 * runs node-environment here — a rule inside this SFC is a rule no test can
 * reach. That includes the row counts: past the fixed track count a row is
 * clipped by `InfoTile`'s `overflow: hidden` without ellipsis or scroll, so the
 * pure function returns `visibleRows`/`totalRows` and the stamp discloses the
 * overflow rather than letting rows vanish silently.
 *
 * Honesty rules it carries (ent#259 AC#2/#5, and #471's source contract):
 *  - a real utilization % is shown ONLY when the provider snapshot is fresh
 *    (`source=anthropic`, age ≤ 30 min); otherwise the row falls back to the
 *    qualitative state and shows no number at all. #471 established the number
 *    is real — the AC's "never a fake-precise X% left" is about framing
 *    (consumed, sourced) rather than about hiding a genuine reading.
 *  - "429s" counts the `rate_limit` kind only, never the failure total, which
 *    also carries auth failures and pre-#471 unclassified rows.
 *  - a failed usage read renders that row as unavailable — never as zeroes,
 *    and never as a healthy subscription.
 */
import { computed } from 'vue'
import InfoTile from '../InfoTile.vue'
import TileRowList from './parts/TileRowList.vue'
import { useSubscriptionsStore } from '@/stores/subscriptions'
import {
  SUBSCRIPTION_SETTINGS_ROUTE,
  SUBSCRIPTION_TILE_MAX_ROWS,
  subscriptionPressureRows,
  subscriptionPressureTileState,
  subscriptionTileStamp,
} from '@/utils/subscriptionPressureTile'

defineProps({
  /** The UNFILTERED fleet roster, passed by the chassis to every tile. Unused
   *  here — subscriptions are not agents — but declared so `inheritAttrs:false`
   *  has a prop to bind rather than leaking it onto the DOM. */
  agents: { type: Array, default: () => [] },
})

const subsStore = useSubscriptionsStore()

const model = computed(() =>
  subscriptionPressureRows(subsStore.subscriptions, subsStore.usageBySub),
)

const rows = computed(() => model.value.rows)

const state = computed(() =>
  subscriptionPressureTileState({
    loaded: subsStore.listLoaded,
    error: subsStore.listError,
    totalRows: model.value.totalRows,
  }),
)

const stamp = computed(() =>
  subscriptionTileStamp({
    visibleRows: model.value.visibleRows,
    totalRows: model.value.totalRows,
    // A poll that failed AFTER a good one keeps its rows on screen rather than
    // replacing real data with an error panel — the staleness is disclosed here
    // instead (stale-while-revalidate, the discipline the sibling tiles use).
    stale: subsStore.listLoaded && subsStore.listError,
  }),
)

const stampTitle = computed(() => {
  const { totalRows, visibleRows } = model.value
  const parts = []
  if (totalRows > visibleRows) {
    parts.push(
      `${visibleRows} of ${totalRows} subscriptions shown — most pressured first. `
      + 'Open Settings → Integrations for the full list.',
    )
  } else {
    parts.push('Every registered Claude subscription.')
  }
  if (subsStore.listLoaded && subsStore.listError) {
    parts.push('The last refresh failed — showing the previous reading.')
  }
  return parts.join(' ')
})

function retry() {
  subsStore.fetchPressureData()
}
</script>

<style scoped>
/* Tokens only, and no `var(--x, #hex)` fallbacks: `gridTokens.spec.js` already
   proves every --gv-* consumed here is defined in BOTH themes, so a fallback
   would be permanently dead code that still counts as a hardcoded color under
   the design-system contract's "new code starts at zero" rule. */
.sp-chip {
  max-width: 60px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 0 5px;
  border-radius: 5px;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: uppercase;
}
.sp-crit {
  background: var(--gv-badge-fail-bg);
  color: var(--gv-badge-fail-tx);
}
.sp-warn {
  background: var(--gv-badge-warn-bg);
  color: var(--gv-badge-warn-tx);
}
/* A failed read is NOT a healthy subscription and must not read as one, but it
   is not an alarm either — muted chrome, distinct from both green and red. */
.sp-unknown {
  background: var(--gv-seg-bg);
  color: var(--gv-muted);
}
.sp-more {
  background: var(--gv-seg-bg);
  color: var(--gv-muted);
  letter-spacing: 0;
}
.sp-dot {
  display: inline-block;
  width: 7px;
  height: 7px;
  margin-left: 2px;
  border-radius: 50%;
  background: var(--gv-green);
}

/* Limit-spend readings. Colour carries the "do I need to act" answer, so the
   percentage itself is tinted rather than a separate swatch — and the `5h`/`7d`
   label stays muted so the eye lands on the number. */
.sp-wins {
  display: inline-flex;
  align-items: baseline;
  gap: 8px;
}
.sp-win {
  display: inline-flex;
  align-items: baseline;
  gap: 3px;
}
.sp-wlab {
  font-size: 10px;
  color: var(--gv-faint);
  text-transform: uppercase;
  letter-spacing: 0.03em;
}
.sp-wpct {
  font-variant-numeric: tabular-nums;
  font-weight: 650;
}
/* Colour is never the ONLY channel: the row also carries a severity chip, and
   the percentage is read as text by a screen reader either way. */
.sp-lvl-ok {
  color: var(--gv-green-text);
}
.sp-lvl-warm {
  color: var(--gv-yellow-text);
}
.sp-lvl-high {
  color: var(--gv-red-text);
}
</style>
