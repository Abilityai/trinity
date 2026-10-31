<template>
  <BaseModal
    :model-value="modelValue"
    panel-class="relative w-full max-w-lg rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800"
    labelledby="retire-metric-title"
    @update:model-value="(v) => emit('update:modelValue', v)"
    @close="close"
  >
    <div class="space-y-4" data-testid="retire-metric-dialog">
      <h2 id="retire-metric-title" class="text-lg font-semibold text-gray-900 dark:text-white">
        Retire {{ metric?.label || metric?.name }}?
      </h2>
      <p class="text-sm text-gray-700 dark:text-gray-300">
        This removes <code class="font-mono">{{ metric?.name }}</code> from template.yaml.
        Recorded points are kept, but the agent can't record new ones under this name.
      </p>

      <div v-if="phase === 'loading'" class="space-y-2" aria-busy="true" data-testid="retire-metric-loading">
        <span class="sr-only">Checking what still uses this metric…</span>
        <div v-for="n in 2" :key="n" class="h-9 animate-pulse rounded-md bg-gray-100 motion-reduce:animate-none dark:bg-gray-750"></div>
      </div>

      <div
        v-else-if="inUseCount > 0"
        class="rounded-lg border border-status-warning-200 bg-status-warning-50 dark:border-status-warning-500/30 dark:bg-status-warning-500/10"
        data-testid="retire-metric-references"
      >
        <p class="px-3 pt-3 text-sm font-medium text-status-warning-800 dark:text-status-warning-300">
          Still in use in {{ inUseCount }} {{ inUseCount === 1 ? 'place' : 'places' }}. These stop updating and show the metric as retired.
        </p>
        <ul class="mt-2 divide-y divide-status-warning-200 dark:divide-status-warning-500/20">
          <li
            v-for="(ref, i) in references"
            :key="`r${i}`"
            class="flex flex-wrap items-baseline gap-x-2 gap-y-0.5 px-3 py-2 text-sm text-gray-900 dark:text-gray-100"
            data-testid="retire-metric-reference"
          >
            <span v-if="showKind(ref.kind)" class="whitespace-nowrap font-medium">{{ kindLabel(ref.kind) }}</span>
            <span class="min-w-0 break-words" :class="showKind(ref.kind) ? '' : 'font-medium'">{{ ref.label }}</span>
            <span v-if="refMeta(ref)" class="ml-auto text-xs text-status-warning-800 dark:text-status-warning-300" :title="refTitle(ref)">{{ refMeta(ref) }}</span>
          </li>
          <li
            v-for="source in unchecked"
            :key="`u${source}`"
            class="px-3 py-2 text-sm text-gray-900 dark:text-gray-100"
            data-testid="retire-metric-unchecked"
          >
            <span class="font-medium">{{ kindLabel(source) }}</span>
            <span class="text-xs text-status-warning-800 dark:text-status-warning-300"> · couldn't be checked, so it may still use this metric</span>
          </li>
        </ul>
        <label class="flex items-center gap-2 border-t border-status-warning-200 px-3 py-3 text-sm text-gray-900 dark:border-status-warning-500/20 dark:text-gray-100">
          <input v-model="acknowledged" type="checkbox" class="h-4 w-4 rounded border-gray-300 dark:border-gray-700" data-testid="retire-metric-acknowledge" />
          I understand {{ inUseCount === 1 ? 'this' : `these ${inUseCount}` }} will stop updating
        </label>
      </div>

      <p v-else-if="phase === 'ready'" class="text-sm text-gray-700 dark:text-gray-300" data-testid="retire-metric-unused">
        Nothing else uses this metric.
      </p>

      <p class="text-xs text-gray-600 dark:text-gray-300">
        You can declare it again later under the same name. The kept points come back with it.
      </p>

      <InlineError
        v-if="error"
        :message="error.message"
        :detail="error.code"
        data-testid="retire-metric-error"
        @dismiss="error = null"
      />

      <div class="flex justify-end gap-2">
        <BaseButton variant="secondary" data-testid="retire-metric-keep" @click="close">Keep metric</BaseButton>
        <BaseButton
          variant="danger"
          :disabled="!canRetire"
          :loading="retiring"
          loading-label="Retiring…"
          data-testid="retire-metric-confirm"
          data-destructive
          @click="retire"
        >Retire metric</BaseButton>
      </div>
    </div>
  </BaseModal>
</template>

<script setup>
/**
 * Retire one declared metric (trinity-enterprise#1006).
 *
 * Lists everything that still reads the metric — recorded points, objectives,
 * dashboard widgets and anything a registered source reports — and requires
 * an explicit acknowledgement before retiring one that is in use. A source
 * that could not be checked counts as a use: a failed lookup is not evidence
 * that nothing reads the metric (#2196).
 */
import { computed, ref, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'
import BaseModal from './base/BaseModal.vue'
import BaseButton from './base/BaseButton.vue'
import InlineError from './InlineError.vue'
import { readRefusal } from '../utils/metricDeclaration'
import { formatRelativeTime, formatLocalDateTime } from '../utils/timestamps'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  agentName: { type: String, required: true },
  metric: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'retired'])

const agentsStore = useAgentsStore()
const phase = ref('loading')
const references = ref([])
const unchecked = ref([])
const acknowledged = ref(false)
const retiring = ref(false)
const error = ref(null)

const inUseCount = computed(() => references.value.length + unchecked.value.length)
const canRetire = computed(() => phase.value === 'ready' && !retiring.value
  && (inUseCount.value === 0 || acknowledged.value))

const KIND_LABELS = {
  points: 'Recorded points',
  objective: 'Objective',
  objectives: 'Objectives',
  widget: 'Dashboard widget',
  dashboard: 'Dashboard widgets',
  project: 'Workspace project',
}
const kindLabel = (kind) => KIND_LABELS[kind] || kind
// A label that already names its kind ("412 recorded points", "Linked in 2
// Workspace projects") reads twice with the kind in front of it.
const showKind = (kind) => kind !== 'points' && kind !== 'project'

function refMeta(ref) {
  if (ref.kind === 'points' && ref.last_point_at) return `last ${formatRelativeTime(ref.last_point_at)}`
  return ref.detail || ''
}
function refTitle(ref) {
  return ref.kind === 'points' && ref.last_point_at ? formatLocalDateTime(ref.last_point_at) : undefined
}

async function open() {
  phase.value = 'loading'
  references.value = []
  unchecked.value = []
  acknowledged.value = false
  error.value = null
  try {
    const data = await agentsStore.getMetricReferences(props.agentName, props.metric.name)
    references.value = data.references || []
    unchecked.value = data.unchecked || []
  } catch (e) {
    // The check itself failed: treat every source as unchecked rather than
    // claiming the metric is unused.
    unchecked.value = ['points', 'objectives', 'dashboard']
    error.value = { code: readRefusal(e).code, message: "Couldn't check what uses this metric. Retiring it may break something that still reads it." }
  }
  phase.value = 'ready'
}

async function retire() {
  if (!canRetire.value) return
  retiring.value = true
  error.value = null
  try {
    const editor = await agentsStore.getMetricDeclarations(props.agentName)
    const result = await agentsStore.retireMetricDeclaration(
      props.agentName, props.metric.name, editor.template_hash,
      { acknowledgeReferences: inUseCount.value > 0 && acknowledged.value },
    )
    emit('retired', { ...result, label: props.metric.label || props.metric.name })
    emit('update:modelValue', false)
  } catch (e) {
    const refusal = readRefusal(e)
    if (refusal.code === 'metric_in_use') {
      // Something new started using it since the list was read: show the
      // fresh list and ask again.
      references.value = refusal.detail?.references || []
      unchecked.value = refusal.detail?.unchecked || []
      acknowledged.value = false
    }
    error.value = { code: refusal.code, message: refusal.message }
  } finally {
    retiring.value = false
  }
}

function close() {
  emit('update:modelValue', false)
}

watch(() => props.modelValue, (isOpen) => { if (isOpen && props.metric) open() }, { immediate: true })
</script>
