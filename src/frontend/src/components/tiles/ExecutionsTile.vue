<template>
  <InfoTile
    scope="Fleet"
    title="Executions"
    :stamp="stamp"
    stamp-title="Rolling 24 hours, UTC hours"
    :state="state"
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
      <span class="ex-total">{{ head.total }}</span>
      <span class="ex-unit">runs</span>
      <span class="ex-sep">·</span>
      <span class="ex-ok">{{ head.successRate === null ? '—' : head.successRate + '%' }}</span>
      <span class="ex-unit">ok</span>
      <template v-if="head.failed">
        <span class="ex-sep">·</span>
        <span class="ex-fail">{{ head.failed }} failed</span>
      </template>
      <span class="ex-chips">
        <!-- Live, not windowed: /stats reports running/queued as of now, which
             is why they sit beside a 24h chart rather than inside it. -->
        <span v-if="live.running" class="ex-chip ex-chip-run">{{ live.running }} running</span>
        <span v-if="live.queued" class="ex-chip">{{ live.queued }} queued</span>
      </span>
    </div>

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
        <!-- The failure rail sits BELOW the stack on its own scale, so failures
             are never hidden inside a column and never steal height from it. -->
        <i class="ex-rail" :style="{ height: col.failPx + 'px' }"></i>
      </div>
    </div>

    <div class="ex-legend">
      <span v-for="b in legend" :key="b.name" class="ex-key" :title="`${b.name}: ${b.total}`">
        <i :class="'ex-dot ex-bk-' + b.token"></i>{{ b.name }}
      </span>
      <span v-if="head.failed" class="ex-key ex-key-fail"><i class="ex-dot ex-dot-fail"></i>failed</span>
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
import { useFleetGridStore } from '@/stores/fleetGrid'
import {
  chartColumns,
  headline,
  presentBuckets,
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
  chartColumns(buckets.value, { triggerOrder: gridStore.execTriggerOrder }),
)
const legend = computed(() => presentBuckets(gridStore.execTriggerOrder, buckets.value))

const state = computed(() =>
  tileState({
    loaded: gridStore.execTimelineLoaded,
    error: gridStore.execTimelineError,
    buckets: buckets.value,
  }),
)

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

.ex-chart {
  display: flex;
  align-items: flex-end;
  gap: 2px;
  height: 78px;
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

.ex-legend {
  display: flex;
  flex-wrap: wrap;
  gap: 2px 8px;
  margin-top: 6px;
  font-size: 10px;
  color: var(--gv-muted);
  overflow: hidden;
  max-height: 26px;
}
.ex-key {
  display: inline-flex;
  align-items: center;
  gap: 3px;
  white-space: nowrap;
}
.ex-key-fail {
  color: var(--gv-red-text);
}
.ex-dot {
  width: 6px;
  height: 6px;
  border-radius: 2px;
  display: inline-block;
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
