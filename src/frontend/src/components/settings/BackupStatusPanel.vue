<!--
  Automatic database backups — status (#3454, backend #2216).

  `GET /api/settings/retention` has carried a `backup` block since #2216 and
  nothing rendered it, so an operator could not tell from the product whether a
  recovery point exists. This is the read-only view of that block; the parent
  owns the fetch and passes the block down, so there is one request for the tab.

  Honest state, in order of what the operator must not be misled about:
    - nothing is claimed before the response arrives (the rows are reserved,
      their values are placeholders);
    - a failed load is a failure, not "no backups";
    - a backend that sends no `backup` key, or one that could not build the
      block, says exactly that;
    - failed / skipped / stale / off / never-run each read as themselves — the
      last good backup is still shown, as the last good one.
-->
<template>
  <BaseCard flush>
    <div class="px-6 py-5">
      <div class="flex items-start justify-between gap-4">
        <div class="min-w-0">
          <h3 class="text-lg font-medium text-gray-900 dark:text-gray-100">Database backups</h3>
          <p class="mt-1 text-sm text-gray-500 dark:text-gray-400">
            Trinity copies its own database to a recovery point automatically. Read-only status.
          </p>
        </div>
        <BaseBadge v-if="headline" :variant="headline.variant" dot data-testid="backup-status">{{ headline.label }}</BaseBadge>
      </div>

      <LoadFailed
        v-if="view.state === 'failed'"
        dense
        title="Couldn't load backup status"
        message="The request for this tab's settings failed, so the state of your backups is unknown."
        :detail="String(error || '')"
        @retry="$emit('retry')"
      />

      <p v-else-if="notReported" class="mt-4 text-sm text-gray-600 dark:text-gray-300" data-testid="backup-not-reported">
        This backend does not report backup status. Update Trinity to see it here.
      </p>

      <p v-else-if="unavailable" class="mt-4 text-sm text-gray-600 dark:text-gray-300" data-testid="backup-unavailable">
        Backup status is unavailable right now — the server could not read it. This says nothing about
        whether backups are running; check the backend logs.
      </p>

      <div
        v-else
        class="mt-5"
        :aria-busy="awaitingData ? 'true' : null"
        :data-testid="awaitingData ? 'backup-loading' : 'backup-ready'"
      >
        <span v-if="awaitingData" class="sr-only">Loading backup status…</span>

        <dl class="grid grid-cols-1 sm:grid-cols-2 gap-x-6 gap-y-4" data-testid="backup-facts">
          <div v-for="row in rows" :key="row.key" class="min-w-0" :data-testid="`backup-${row.key}`">
            <dt class="text-sm font-medium text-gray-700 dark:text-gray-300">{{ row.label }}</dt>
            <!-- One line of value and, on the rows that have one, one line of
                 detail. The placeholders are the same two line boxes, so the
                 card is the same height before and after the data lands. -->
            <dd class="mt-1 text-sm leading-5 text-gray-900 dark:text-gray-100 tabular-nums">
              <span v-if="awaitingData" class="flex h-5 items-center" aria-hidden="true">
                <span class="h-3 w-32 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"></span>
              </span>
              <template v-else-if="row.key === 'last-success' && lastSuccess">
                <time :datetime="lastSuccess.iso" :title="lastSuccess.absolute">{{ lastSuccess.relative }}</time>
              </template>
              <span v-else :class="row.mono ? 'block truncate font-mono' : 'block'" :title="row.mono ? facts[row.key].value : null">{{ facts[row.key].value }}</span>
            </dd>
            <dd v-if="row.detail" class="text-xs leading-4 min-h-4 text-gray-500 dark:text-gray-400 tabular-nums">
              <span v-if="awaitingData" class="flex h-4 items-center" aria-hidden="true">
                <span class="h-2.5 w-48 max-w-full rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none"></span>
              </span>
              <span v-else class="block truncate" :title="facts[row.key].detail">{{ facts[row.key].detail }}</span>
            </dd>
          </div>
        </dl>

        <div
          v-if="problem"
          class="mt-4 rounded-md border border-gray-200 dark:border-gray-750 p-3"
          role="status"
          data-testid="backup-problem"
        >
          <p class="text-sm text-gray-700 dark:text-gray-300">{{ problem.message }}</p>
          <details v-if="problem.detail" class="mt-2">
            <summary class="text-xs text-gray-500 dark:text-gray-400 cursor-pointer select-none">Technical detail</summary>
            <p class="mt-1 text-xs font-mono break-words text-gray-600 dark:text-gray-400">{{ problem.detail }}</p>
          </details>
        </div>
      </div>
    </div>
  </BaseCard>
</template>

<script setup>
import { computed } from 'vue'
import BaseCard from '../base/BaseCard.vue'
import BaseBadge from '../base/BaseBadge.vue'
import LoadFailed from '../LoadFailed.vue'
import { viewState } from '../../utils/loadingState'
import { parseUTC, formatRelativeTime } from '../../utils/timestamps'
import { useFormatters } from '../../composables/useFormatters'

const props = defineProps({
  // The `backup` block of GET /api/settings/retention, as received. Absent on
  // a backend older than #2216; `{ error: 'unavailable' }` when the server
  // could not build it.
  backup: { type: Object, default: undefined },
  // True once the retention response has arrived at least once.
  hasLoaded: { type: Boolean, default: false },
  // The load error, if the request failed.
  error: { type: String, default: '' },
})
defineEmits(['retry'])

const { formatFileSize } = useFormatters()

// The rows are fixed, so the loading frame reserves exactly what the loaded
// one fills. `detail` marks the rows that carry a second line.
const rows = [
  { key: 'last-success', label: 'Last successful backup', detail: true },
  { key: 'recovery-points', label: 'Recovery points', detail: true },
  { key: 'location', label: 'Location', detail: true, mono: true },
  { key: 'schedule', label: 'Schedule' },
  { key: 'kept', label: 'Kept for' },
]

// Loading means "no data yet", never "fetch in flight" — a refresh after a
// save keeps the status on screen (utils/loadingState.js).
const view = computed(() => viewState({ hasLoaded: props.hasLoaded, error: props.error }))
const awaitingData = computed(() => view.value.state === 'loading')
const loaded = computed(() => view.value.state === 'ready')
const notReported = computed(() => loaded.value && props.backup == null)
const unavailable = computed(() => loaded.value && props.backup != null && Boolean(props.backup.error))
const block = computed(() => (loaded.value && !notReported.value && !unavailable.value ? props.backup : null))

const schedule = computed(() => (block.value?.schedule_utc ? `${block.value.schedule_utc} UTC` : ''))
const nextAttempt = computed(() => (schedule.value ? ` The next attempt runs at ${schedule.value}.` : ''))

// One word for the state. Order matters: what is wrong now outranks an old
// success, so a failing install never reads "Healthy" off a stale good row.
const headline = computed(() => {
  const b = block.value
  if (!b) return null
  if (b.enabled === false) return { kind: 'off', label: 'Off', variant: 'neutral' }
  if (b.last_status === 'failed') return { kind: 'failed', label: 'Last backup failed', variant: 'danger' }
  if (b.last_status === 'skipped_no_space') return { kind: 'no-space', label: 'Skipped — not enough disk space', variant: 'danger' }
  if (!b.last_status && !b.last_success_at) return { kind: 'never', label: 'No backup yet', variant: 'neutral' }
  if (b.stale) return { kind: 'stale', label: 'Stale', variant: 'warning' }
  if (b.last_status === 'ok') return { kind: 'ok', label: 'Healthy', variant: 'success' }
  // A status this bundle has not heard of is shown as itself, not as health.
  return { kind: 'unknown', label: String(b.last_status || 'Unknown'), variant: 'neutral' }
})

const ageDays = computed(() => {
  const d = block.value?.last_success_age_days
  return typeof d === 'number' ? Math.floor(d) : null
})

const problem = computed(() => {
  const b = block.value
  const kind = headline.value?.kind
  if (!b || !kind) return null
  const detail = b.last_error || ''
  if (kind === 'off') {
    return { message: 'Automatic backups are turned off on this instance (DB_BACKUP_ENABLED=false), so no new recovery points are being created.' }
  }
  if (kind === 'failed') {
    return { message: `The most recent backup attempt failed. Earlier recovery points are kept.${nextAttempt.value} If it keeps failing, check the backend logs.`, detail }
  }
  if (kind === 'no-space') {
    return { message: `The most recent backup was skipped because the disk does not have enough free space. Free up space on the data volume.${nextAttempt.value}`, detail }
  }
  if (kind === 'stale') {
    const age = ageDays.value != null ? `${ageDays.value} days old` : 'older than expected'
    return { message: `The last successful backup is ${age}. Backups are not completing on schedule — check the backend logs.`, detail }
  }
  return null
})

const lastSuccess = computed(() => {
  const ts = block.value?.last_success_at
  if (!ts) return null
  const at = parseUTC(ts)
  if (Number.isNaN(at.getTime())) return null
  return {
    iso: at.toISOString(),
    relative: formatRelativeTime(ts),
    // Absolute, with the zone it is rendered in (design-system principle 22).
    absolute: at.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'long' }),
  }
})

const size = (bytes) => (typeof bytes === 'number' && bytes >= 0 ? formatFileSize(bytes) : '')

const facts = computed(() => {
  const b = block.value || {}
  const artifacts = b.artifacts || {}
  const count = typeof artifacts.count === 'number' ? artifacts.count : 0
  const total = size(artifacts.total_bytes)
  return {
    'last-success': {
      // Only reached when there is no parseable success time.
      value: b.last_success_at ? String(b.last_success_at) : 'Never',
      detail: b.last_success_at
        ? [size(b.last_size_bytes), b.last_trigger].filter(Boolean).join(' · ')
        : (schedule.value && b.enabled !== false ? `First run at ${schedule.value}` : ''),
    },
    'recovery-points': {
      value: count === 0 ? 'None yet' : `${count}${total ? ` · ${total} total` : ''}`,
      detail: artifacts.newest ? `Newest: ${artifacts.newest}` : '',
    },
    location: {
      value: b.backup_dir || '—',
      detail: b.scope === 'same-disk'
        ? 'Same disk as the database — covers corruption and mistakes, not loss of the disk.'
        : '',
    },
    schedule: { value: schedule.value ? `Nightly at ${schedule.value}` : '—' },
    kept: {
      value: typeof b.retention_days === 'number'
        ? `${b.retention_days} days${typeof b.min_keep === 'number' ? ` · the newest ${b.min_keep} are always kept` : ''}`
        : '—',
    },
  }
})
</script>
