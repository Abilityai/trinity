<template>
  <InfoTile
    scope="Fleet"
    title="Recent failures"
    :stamp="stamp"
    :stamp-title="stampTitle"
    :state="tile.state"
    :empty-title="tile.emptyTitle"
    :empty-hint="tile.emptyHint"
    error-text="Couldn't read recent executions. This tile only — the rest of the board is unaffected."
    :on-retry="retry"
    :link-to="{ name: 'Operations', query: { tab: 'executions' } }"
    link-label="Open executions →"
  >
    <template #icon>
      <!-- No fill/stroke attributes: `.it-peg :deep(svg)` sets
           `fill: none; stroke: currentColor` and the peg's colour is
           `var(--gv-peg-fg)`, so the glyph themes itself. A hardcoded hex
           inside a path would be invisible to gridTokens.spec.js (it scans
           declarations, not attributes) and permanently unthemed. -->
      <svg viewBox="0 0 24 24" width="18" height="18">
        <path d="M12 3.5 2.8 20h18.4L12 3.5Z"></path>
        <path d="M12 10v4.2"></path>
        <path d="M12 17.4v.01"></path>
      </svg>
    </template>

    <p v-if="tile.note" class="rf-note">{{ tile.note }}</p>

    <TileRowList v-else :rows="rows" :track-count="MAX_ROWS">
      <template #lead="{ row }">
        <!-- The code chip REPLACES the glyph when the platform actually
             emitted a `[code]` marker, so the row upgrades by itself the day
             `error_code` is persisted. Until then the width goes to the real
             error message, which is data that exists today. -->
        <span v-if="row.code" class="rf-chip">{{ row.code }}</span>
        <svg v-else class="rf-glyph" viewBox="0 0 24 24" width="12" height="12" aria-hidden="true">
          <path d="M12 3.5 2.8 20h18.4L12 3.5Z"></path>
          <path d="M12 10v4.2"></path>
          <path d="M12 17.4v.01"></path>
        </svg>
      </template>
    </TileRowList>
  </InfoTile>
</template>

<script setup>
/**
 * Recent failures (ent#100) — the newest failed executions across the fleet,
 * one glance away instead of behind Operations → Executions plus a filter.
 *
 * ZERO backend change: `GET /api/executions?status=failed&hours=24&limit=4`
 * and `GET /api/executions/stats?hours=24` already exist, are paginated,
 * filtered and access-scoped. Both are fetched by `stores/fleetGrid.js` on the
 * ONE visibility-aware 60s batch poll the Grid already runs — this component
 * never fetches. That is not a style preference: viewport culling UNMOUNTS a
 * tile, so a fetch in `onMounted` would re-issue on every pan, and the data
 * must already be warm when the operator pans back.
 *
 * The tile's decisions all live in `utils/executionFailure.js` as pure
 * functions, because vitest runs in a node environment here — a pure module is
 * the only part of a tile that can be unit-tested. In particular
 * `failuresTileState` owns the rule that the green "No failures in 24h ✓" is a
 * POSITIVE claim requiring positive evidence, and can never be reached from a
 * failed list GET, a failed /stats GET, or an unenumerable fleet.
 *
 * ent#100's error-code-taxonomy AC is only partially met, deliberately and
 * named in the PR body: the code is not persisted, so the chip renders only
 * when the platform emitted a real `[code]` marker (today: never, since the
 * only writer is the dark pull path). See `utils/executionFailure.js`.
 */
import { computed } from 'vue'
import InfoTile from '../InfoTile.vue'
import TileRowList from './parts/TileRowList.vue'
import { useFleetGridStore } from '@/stores/fleetGrid'
import { agentDisplayName } from '@/utils/agentName'
import { formatCompactAge, getTimestampMs, formatLocalDateTime } from '@/utils/timestamps'
import {
  failureCodeFromSummary,
  failuresTileState,
  stripFailureCode,
  triggerLabel,
} from '@/utils/executionFailure'

/**
 * Two-line rows against InfoTile's ~139px body: four tracks is what fits, and
 * ent#100's AC asks for 3-5. The count is FIXED rather than derived from how
 * many rows arrived — `.it-body` is `overflow: hidden`, so an overrun does not
 * scroll or ellipsize, the last row silently vanishes, and the node-environment
 * test suite structurally cannot see it.
 */
const MAX_ROWS = 4

const props = defineProps({
  /** The UNFILTERED fleet roster (`orgAgents || agents` at the chassis). Used
   *  as a display-label lookup and as the fleet-enumerability signal — never as
   *  a data source. */
  agents: { type: Array, default: () => [] },
  /** The Grid's shared 1s tick. Declared via `wantsTick` in the catalog, so a
   *  tile that renders no clock does not pay for one. */
  now: { type: Number, default: () => Date.now() },
})

const gridStore = useFleetGridStore()

const byName = computed(() => {
  const map = {}
  for (const a of props.agents || []) if (a && a.name) map[a.name] = a
  return map
})

const tile = computed(() =>
  failuresTileState({
    listLoaded: gridStore.failuresListLoaded,
    listError: gridStore.failuresListError,
    statsLoaded: gridStore.failuresStatsLoaded,
    statsError: gridStore.failuresStatsError,
    itemCount: (gridStore.recentFailures || []).length,
    failed24h: gridStore.failures24h,
    rosterSize: (props.agents || []).length,
  }),
)

const rows = computed(() =>
  (gridStore.recentFailures || []).slice(0, MAX_ROWS).map((r) => {
    const startedMs = getTimestampMs(r.started_at)
    // Age is measured between the SERVER's instant and the BROWSER's clock, so
    // the skew read from the response's `Date` header is applied before the
    // subtraction. Without it a laptop three minutes fast renders every recent
    // failure three minutes younger — presented as a fact about the platform.
    const ageMs = props.now + gridStore.serverClockSkewMs - startedMs
    const summary = stripFailureCode(r.error_summary)
    const code = failureCodeFromSummary(r.error_summary)
    return {
      key: r.id,
      to: { name: 'ExecutionDetail', params: { name: r.agent_name, executionId: r.id } },
      // Full message + absolute local time on hover: relative on the face,
      // absolute on detail (design principle 22). Bound as an ATTRIBUTE, and
      // the body is rendered as text interpolation — `error_summary` is
      // agent/LLM-authored, prompt-injectable content and never reaches v-html.
      // The code rides along because the chip is width-capped and ellipsizes
      // (`EPHEMERAL_EXHAUSTED` does not fit a 384px tile), so hover is where
      // the full code has to be recoverable.
      title: [
        code ? `[${code}]` : null,
        summary || 'No error detail recorded',
        formatLocalDateTime(r.started_at),
      ].filter(Boolean).join('\n'),
      code,
      primary: agentDisplayName(byName.value[r.agent_name]) || r.agent_name,
      meta: `${triggerLabel(r.triggered_by)} · ${formatCompactAge(ageMs)}`,
      sub: summary,
    }
  }),
)

const stamp = computed(() => {
  const n = gridStore.failures24h
  return typeof n === 'number' ? `${n} in 24h` : ''
})

const stampTitle = computed(() =>
  'Failed executions across the agents you can access, last 24 hours. '
  + "Counts legacy 'error' rows too, which the list below filters out.",
)

function retry() {
  gridStore.refreshBatchData()
}
</script>

<style scoped>
.rf-note {
  margin: 0;
  font-size: 12px;
  line-height: 1.35;
  color: var(--gv-muted, #6b7280);
}

.rf-glyph {
  fill: none;
  stroke: currentColor;
  stroke-width: 2;
  stroke-linecap: round;
  stroke-linejoin: round;
  color: var(--gv-red-text, #dc2626);
}

.rf-chip {
  max-width: 92px;
  overflow: hidden;
  text-overflow: ellipsis;
  white-space: nowrap;
  padding: 0 5px;
  border-radius: 5px;
  font-size: 10px;
  font-weight: 600;
  letter-spacing: 0.02em;
  text-transform: uppercase;
  background: var(--gv-badge-fail-bg, #fee2e2);
  color: var(--gv-badge-fail-tx, #b91c1c);
}
</style>
