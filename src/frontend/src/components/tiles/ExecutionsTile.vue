<template>
  <InfoTile
    scope="Fleet"
    title="Executions"
    :stamp="stamp"
    stamp-title="Rolling 24 hours, UTC hours"
    :state="state"
    :owns-loading="true"
    empty-title="No executions in the last 24h"
    empty-hint="Scheduled work and chats will appear here."
    error-text="Couldn't read the execution timeline. This tile only — the rest of the board is unaffected."
    :on-retry="retry"
    :link-to="{ name: 'Operations', query: { tab: 'executions' } }"
    link-label="Open executions →"
  >
    <template #icon>
      <!-- No fill/stroke attributes: the peg sets `fill: none; stroke:
           currentColor`, so the glyph themes itself (ent#100's note). -->
      <svg viewBox="0 0 24 24" width="18" height="18">
        <path d="M4 19V5"></path>
        <path d="M4 19h16"></path>
        <path d="M8 19v-6"></path>
        <path d="M13 19V9"></path>
        <path d="M18 19v-9"></path>
      </svg>
    </template>

    <div class="ex-head">
      <!-- Through `face`, never `head`: this line renders DURING loading (it
           sits outside the zone below), so it needs a loading face of its own —
           "0 runs · 100% ok" before the first read is a claim about the fleet,
           not a placeholder. -->
      <span class="ex-total">{{ face.total }}</span>
      <span class="ex-unit">runs</span>
      <span class="ex-sep">·</span>
      <span class="ex-ok">{{ face.ok }}</span>
      <span class="ex-unit">ok</span>
      <template v-if="face.failed">
        <span class="ex-sep">·</span>
        <span class="ex-fail">{{ face.failed }} failed</span>
      </template>
      <span class="ex-chips">
        <!-- Live, not windowed: /stats reports running/queued as of now, which
             is why they sit beside a 24h chart rather than inside it. -->
        <span v-if="live.running" class="ex-chip ex-chip-run">{{ live.running }} running</span>
        <span v-if="live.queued" class="ex-chip">{{ live.queued }} queued</span>
      </span>
    </div>

    <!-- ent#449: ONE persistent ScanlineReveal around the chart zone. A beam
         sweeps the dimmed track while there is no timeline data yet; one 550ms
         wipe brings the columns in when the first read lands WITH data (an
         error or an empty window snaps — the reveal celebrates data arriving).
         Two rules the primitive's contract turns on:
         · the branch lives INSIDE the slot, never as sibling v-if arms around
           the component — a remount re-inits the phase machine from
           `loading = false` and the reveal never plays;
         · nothing renders in the slot while loading, so no content can ever be
           drawn under the track (learnings 2026-08-24).
         The headline stays ABOVE this zone: the wipe's first frame clips all
         slot content to nothing, so a headline inside it would blink at
         arrival — and `.ex-head ~` below needs that order anyway. -->
    <ScanlineReveal class="ex-zone" :loading="zone.loading" :reveal="zone.reveal">
      <template v-if="!zone.loading">
        <div class="ex-chart" role="img" :aria-label="chartLabel">
          <div v-for="col in columns" :key="col.bucket" class="ex-col" :title="tooltip(col)">
            <span class="ex-stack">
              <i v-if="col.stub" class="ex-stub"></i>
              <i
                v-for="seg in col.segments"
                :key="seg.name"
                :class="'ex-seg ex-bk-' + seg.token"
                :style="{ height: seg.px + 'px' }"
              ></i>
            </span>
            <!-- The failure rail sits BELOW the stack on its own scale, so
                 failures are never hidden inside a column and never steal
                 height from it. Rendered ONLY when there is something to draw:
                 `.ex-col` spaces its children with `gap`, and flex gap applies
                 to a zero-height item too — so an always-present empty rail
                 lifted every failure-free stack 2px off the baseline while a
                 real rail reached it, and the red read as hanging below the
                 chart. Every column now ends on one baseline. -->
            <i v-if="col.failPx" class="ex-rail" :style="{ height: col.failPx + 'px' }"></i>
          </div>
        </div>
      </template>
    </ScanlineReveal>

    <!-- Exactly LEGEND_ROWS whole rows (#2228). The clamp below hides a third
         row outright rather than slicing one, and `legendFit` guarantees there
         is nothing in it to hide — anything that would not fit is handed to the
         `+N` chip, which names it rather than dropping it silently. -->
    <div class="ex-legend">
      <span
        v-for="k in legend.shown"
        :key="k.fail ? '\u0000failed' : k.name"
        class="ex-key"
        :class="{ 'ex-key-fail': k.fail }"
        :title="`${k.name}: ${k.total}`"
      >
        <i class="ex-dot" :class="k.fail ? 'ex-dot-fail' : 'ex-bk-' + k.token"></i>{{ k.name }}
      </span>
      <span v-if="legend.hidden.length" class="ex-key ex-key-more" :title="hiddenTitle">
        <!-- Dots only while they still identify WHICH buckets are hidden. Past
             three the swatches stop being a mapping and become decoration, so
             the count carries it alone and the title stays authoritative. -->
        <template v-if="legend.hidden.length <= 3">
          <i
            v-for="k in legend.hidden"
            :key="k.name"
            class="ex-dot"
            :class="k.fail ? 'ex-dot-fail' : 'ex-bk-' + k.token"
          ></i>
        </template>
        +{{ legend.hidden.length }}
      </span>
    </div>
  </InfoTile>
</template>

<script setup>
/**
 * Fleet executions over 24h, hourly, stacked by trigger bucket (ent#96).
 *
 * The fleet-scope counterpart to the per-agent 14d charts on `AgentTile`: the
 * dashboard had no "what has the system been doing today" read at fleet scope.
 *
 * Three decisions worth keeping:
 *
 * 1. **One request, two dimensions.** The stack needs hour x trigger, and
 *    `GET /api/executions/timeline` grouped one way at a time, so ent#96
 *    extended it with `split=trigger` rather than issuing one call per bucket
 *    name. The per-bucket totals are re-summed from the split rows server-side,
 *    so a column and its segments cannot disagree.
 * 2. **Failures get their own rail, not a stack segment.** A "Failed" segment
 *    would have to be subtracted from its trigger's segment to keep the column
 *    honest, which silently redefines every other segment as "succeeded". The
 *    rail encodes failures beside the stack: the column still totals runs, and
 *    failures are visible rather than hidden inside it (AC2).
 * 3. **The vocabulary and its order come from the backend** (`trigger_order`),
 *    never from a copy here — so tile, legend and the #1107 Overview chart
 *    cannot order or name the same buckets differently (AC1).
 *
 * All shaping lives in `utils/executionsTile.js`: vitest runs node-environment
 * here, so a pure module is the only part of a tile that can be unit-tested.
 */
import { computed } from 'vue'
import InfoTile from '../InfoTile.vue'
import ScanlineReveal from '../ScanlineReveal.vue'
import { useFleetGridStore } from '@/stores/fleetGrid'
import {
  CHART_HEIGHT,
  chartColumns,
  headline,
  headlineFace,
  legendFit,
  legendKeys,
  RAIL_HEIGHT,
  scanlineProps,
  tileState,
} from '@/utils/executionsTile'

defineProps({
  /** The unfiltered fleet roster. Unused here — this tile's numbers come from
   *  the store — but declared because the chassis passes it to every tile. */
  agents: { type: Array, default: () => [] },
})

const gridStore = useFleetGridStore()

const buckets = computed(() => gridStore.execTimeline || [])
const head = computed(() => headline(buckets.value))
const columns = computed(() =>
  chartColumns(buckets.value, {
    triggerOrder: gridStore.execTriggerOrder,
    chartHeight: CHART_HEIGHT,
    railHeight: RAIL_HEIGHT,
  }),
)
const legend = computed(() =>
  legendFit(legendKeys(gridStore.execTriggerOrder, buckets.value)),
)

/**
 * What the `+N` chip is standing in for. Bucket names and counts only — the
 * same backend vocabulary the columns already report on hover.
 */
const hiddenTitle = computed(
  () => `Not shown: ${legend.value.hidden.map((k) => `${k.name}: ${k.total}`).join(' · ')}`,
)

const state = computed(() =>
  tileState({
    loaded: gridStore.execTimelineLoaded,
    error: gridStore.execTimelineError,
    buckets: buckets.value,
  }),
)

/**
 * The chart zone's loading motion and the headline's face (ent#449), both pure
 * functions of the state above — never of a request-in-flight flag. `loading`
 * is "no data yet": `execTimelineLoaded` latches on the first success, so the
 * 60s refresh swaps values in place and the beam never returns.
 */
const zone = computed(() => scanlineProps(state.value))
const face = computed(() => headlineFace(head.value, state.value))

/**
 * The chips degrade to nothing rather than to zero: `/stats` failing is not
 * evidence that nothing is running, and "0 running" is a claim about the fleet.
 */
const live = computed(() => {
  const s = gridStore.execLiveLoaded ? gridStore.execLive : null
  return { running: s?.running_count || 0, queued: s?.queued_count || 0 }
})

const stamp = computed(() => (gridStore.execTimelineError ? '24h · stale' : '24h'))

const chartLabel = computed(
  () => `Executions per hour over the last 24 hours: ${head.value.total} runs, ${head.value.failed} failed`,
)

function tooltip(col) {
  // Absolute detail on hover, relative on the face (design principle 22). Bound
  // as an attribute and built from numbers + backend bucket names only.
  const lines = [`${col.hour}:00 UTC — ${col.total} run${col.total === 1 ? '' : 's'}`]
  for (const seg of col.segments) {
    lines.push(`  ${seg.name}: ${seg.total}${seg.failed ? ` (${seg.failed} failed)` : ''}`)
  }
  if (!col.total) lines.push('  no executions')
  else if (col.failed) lines.push(`  ${col.failed} failed in total`)
  return lines.join('\n')
}

function retry() {
  gridStore.fetchExecutionsTimeline()
  gridStore.fetchExecutionsLive()
}
</script>

<style scoped>
/* Every colour is a --gv-* token from the cascade FleetGrid establishes, in
   both themes (gridTokens.spec.js). */
.ex-head {
  display: flex;
  align-items: baseline;
  gap: 4px;
  font-size: 12px;
  /* Pinned, not inherited: with the default line-height the baseline-aligned
     mix of 20px and 12px text made this box 22.1px, and every px here comes
     straight out of the legend at the other end of a fixed-height tile. */
  line-height: 1;
  color: var(--gv-muted);
  margin-bottom: 6px;
}
.ex-total {
  font-size: 20px;
  font-weight: 700;
  color: var(--gv-text);
  line-height: 1;
}
.ex-ok {
  font-weight: 600;
  color: var(--gv-green-text);
}
.ex-fail {
  font-weight: 600;
  color: var(--gv-red-text);
}
.ex-sep {
  color: var(--gv-faint);
}
.ex-chips {
  margin-left: auto;
  display: flex;
  gap: 4px;
}
.ex-chip {
  font-size: 10px;
  font-weight: 600;
  padding: 1px 6px;
  border-radius: 999px;
  background: var(--gv-seg-bg);
  color: var(--gv-muted);
  white-space: nowrap;
}
.ex-chip-run {
  background: var(--gv-badge-runner-bg);
  color: var(--gv-badge-runner-tx);
}

/* ONE footprint for the loading track and the loaded chart (ent#449), so the
   tile's body never moves between them. Both need it: the primitive's content
   wrapper is auto-height and EMPTY while loading, so the zone root must be
   sized or there is no track to sweep — and the inner box must be sized too,
   because a percentage height does not resolve inside that wrapper. One
   literal, so the two cannot drift.
   CHART_HEIGHT + COL_GAP + RAIL_HEIGHT + the padding below, from
   utils/executionsTile.js — the tallest a column can be, and nothing more.
   It carried 4px of dead space, which is part of how the body came to
   overflow (#2228). Pinned by executionsTile.spec.js. */
.ex-zone,
.ex-chart {
  height: 70px;
}

/* `.scanline` is `position: relative` but NOT a block-formatting-context root,
   and its first in-flow child `.scan-content` carries `margin: -4px`. That
   negative top margin collapses through this root and then with `.ex-head`'s
   6px bottom margin: measured in Chromium the head→zone gap drops to 2px and
   the chart renders 4px BELOW the track for the whole 550ms wipe, while the
   body loses 4px and pushes the legend up. `flow-root` makes the zone a BFC
   root and restores exact track↔chart registration. Never `overflow: hidden`
   — that would clip the primitive's deliberate 4px bleed. The same latent
   offset exists wherever the primitive is used; fixing its root is a recorded
   follow-up, not this tile's to make. */
.ex-zone {
  display: flow-root;
}

/* ent#449: the beam rides the grid's own palette (already theme-aware through
   FleetGrid's --gv-* definitions) rather than the primitive's Tailwind-token
   defaults, so light and dark come from one place. Anchored on this tile's own
   head: three classes + the scoped attribute reach (0,4,0) and beat the
   primitive's `.dark .scanline` (0,3,0) regardless of stylesheet injection
   order — a two-class selector merely ties with it and wins by import order.
   No `.dark` selector here: theming belongs in the token layer. */
.ex-head ~ .ex-zone.scanline {
  --scan-core: var(--gv-blue);
  --scan-track: var(--gv-bar-track);
}

.ex-chart {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  padding-bottom: 2px;
}
.ex-col {
  flex: 1 1 0;
  min-width: 0;
  display: flex;
  flex-direction: column;
  justify-content: flex-end;
  align-items: stretch;
  height: 100%;
  /* COL_GAP in utils/executionsTile.js. Only ever separates a stack from a
     RENDERED rail — the rail is `v-if`'d, because gap does not care that an
     item is 0px tall and would otherwise float every failure-free column. */
  gap: 2px;
}
.ex-stack {
  display: flex;
  flex-direction: column-reverse;
  justify-content: flex-start;
}
.ex-seg {
  display: block;
  border-radius: 1px;
}
/* Zero-execution hour: the faint baseline stub the AgentTile charts use, so an
   empty hour reads as data rather than as a gap. */
.ex-stub {
  display: block;
  height: 2px;
  background: var(--gv-bar-track);
  opacity: 0.7;
}
.ex-rail {
  display: block;
  background: var(--gv-red);
  border-radius: 1px;
}

/* The legend occupies a WHOLE number of rows (#2228).
   `max-height: 26px` was ~1.7 rows, so `overflow: hidden` cut row two through
   its glyphs instead of hiding it. Pitch is now pinned rather than inherited:
   `--ex-row` is set as `.ex-key`'s line-height, so the clamp is arithmetic over
   values this file owns and cannot drift with a font change.
   The row count MUST equal `LEGEND_ROWS` in utils/executionsTile.js — the
   packer decides what fits, this decides how much is shown, and a disagreement
   reintroduces silent truncation. Pinned by executionsTile.spec.js. */
.ex-legend {
  --ex-row: 13px;
  --ex-row-gap: 2px;
  display: flex;
  flex-wrap: wrap;
  column-gap: 8px;
  row-gap: var(--ex-row-gap);
  margin-top: 6px;
  font-size: 10px;
  color: var(--gv-muted);
  overflow: hidden;
  /* HEIGHT, not max-height: a ceiling grows and shrinks with the key count, so
     the chart above would resize every time a bucket appeared or went quiet.
     Reserved space is the price of a layout that never moves.
     2 rows: 13 + 2 + 13 = 28px. A third row would begin at 30px and is
     therefore hidden whole — rows are never partially rendered. */
  height: calc(var(--ex-row) * 2 + var(--ex-row-gap));
}
.ex-key {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  white-space: nowrap;
  /* Pins the row pitch the clamp is computed from. */
  line-height: var(--ex-row);
  /* A key wider than the whole row is force-placed by the packer rather than
     dropped; clip it here so it cannot widen the tile. */
  max-width: 100%;
  overflow: hidden;
}
.ex-key-fail {
  color: var(--gv-red-text);
}
/* The overflow chip. Zero vertical padding keeps it on the same 13px pitch as
   every other key, so it can never be the thing that creates a third row. */
.ex-key-more {
  padding: 0 5px;
  border-radius: 999px;
  background: var(--gv-seg-bg);
  font-weight: 600;
  font-variant-numeric: tabular-nums;
  cursor: default;
}
.ex-dot {
  width: 6px;
  height: 6px;
  border-radius: 2px;
  display: inline-block;
  /* A flex item by virtue of `.ex-key`; without this the swatch is the first
     thing squeezed when a long label meets `overflow: hidden`, and a legend
     whose colour chip has been compressed to a sliver decodes nothing. */
  flex: none;
}
.ex-dot-fail {
  background: var(--gv-red);
}

.ex-bk-sched { background: var(--gv-bk-sched); }
.ex-bk-man { background: var(--gv-bk-man); }
.ex-bk-ext { background: var(--gv-bk-ext); }
.ex-bk-mcp { background: var(--gv-bk-mcp); }
.ex-bk-public { background: var(--gv-bk-public); }
.ex-bk-loops { background: var(--gv-bk-loops); }
.ex-bk-reminders { background: var(--gv-bk-reminders); }
.ex-bk-a2a { background: var(--gv-bk-a2a); }
.ex-bk-voice { background: var(--gv-bk-voice); }
.ex-bk-other { background: var(--gv-bk-other); }
</style>
