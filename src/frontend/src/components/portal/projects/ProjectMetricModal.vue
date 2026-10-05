<!--
  Add a metric to a project (trinity-enterprise#661 v3.1). Offers the metrics
  each agent on the project declares and records, minus the ones already on
  it. The values stay the agent's; the project only shows them.
-->
<template>
  <BaseModal
    :model-value="modelValue"
    labelledby="project-metric-title"
    :close-on-backdrop="false"
    panel-class="relative w-full max-w-md rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <form class="space-y-4" data-testid="project-metric-form" @submit.prevent="submit">
      <h2 id="project-metric-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">Add a metric</h2>
      <SkeletonLoader v-if="view.state === 'loading'" :count="2" height="2.5rem" gap="0.75rem" />
      <LoadFailed v-else-if="view.state === 'failed'" title="Couldn't load the metrics" :message="loadError" :retrying="loading" @retry="load" />
      <p v-else-if="view.state === 'empty'" class="text-sm text-gray-600 dark:text-gray-300" data-testid="project-metric-none">
        None of this project's agents declares a metric it hasn't already got. An agent declares its metrics in its template, under <code>metrics:</code>.
      </p>
      <template v-else>
        <BaseSelect id="project-metric-agent" v-model="agent" label="Agent">
          <option v-for="a in withMetrics" :key="a.agent_name" :value="a.agent_name">{{ a.agent_name }}</option>
        </BaseSelect>
        <BaseSelect id="project-metric-name" v-model="metric" label="Metric">
          <option v-for="m in metricsOf(agent)" :key="m.name" :value="m.name">{{ m.label }}{{ m.unit ? ` (${m.unit})` : '' }}</option>
        </BaseSelect>
      </template>
      <InlineError v-if="submitError" :message="submitError" @dismiss="submitError = ''" />
      <div class="flex justify-end gap-2">
        <BaseButton variant="secondary" @click="$emit('update:modelValue', false)">{{ view.state === 'ready' ? 'Cancel' : 'Close' }}</BaseButton>
        <BaseButton v-if="view.state === 'ready'" type="submit" :loading="saving" loading-label="Adding…" :disabled="!metric" data-testid="project-metric-save">Add</BaseButton>
      </div>
    </form>
  </BaseModal>
</template>

<script setup>
import { computed, ref, watch } from 'vue'
import BaseModal from '@/components/base/BaseModal.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import { useProjectsStore } from '@/stores/projects'
import { viewState } from '@/utils/loadingState'
import { projectErrorMessage } from './projectsUtils'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  projectId: { type: String, required: true },
})
const emit = defineEmits(['update:modelValue'])

const store = useProjectsStore()
const options = ref([])
const loaded = ref(false)
const loading = ref(false)
const loadError = ref(null)
const agent = ref('')
const metric = ref('')
const saving = ref(false)
const submitError = ref('')

const withMetrics = computed(() => options.value.filter((a) => a.metrics.length))
const metricsOf = (name) => (options.value.find((a) => a.agent_name === name)?.metrics || [])
const view = computed(() => viewState({ hasLoaded: loaded.value, error: loadError.value, count: withMetrics.value.length }))

async function load() {
  loading.value = true
  try {
    options.value = await store.metricOptions(props.projectId)
    loaded.value = true
    loadError.value = null
    agent.value = withMetrics.value[0]?.agent_name || ''
  } catch (err) {
    loadError.value = projectErrorMessage(err)
  } finally {
    loading.value = false
  }
}

watch(() => props.modelValue, (open) => {
  if (!open) return
  loaded.value = false
  submitError.value = ''
  void load()
}, { immediate: true })

// A new agent picks its first metric, so the form is never in a no-choice state.
watch(agent, (name) => { metric.value = metricsOf(name)[0]?.name || '' })

async function submit() {
  saving.value = true
  submitError.value = ''
  try {
    await store.linkMetric(props.projectId, agent.value, metric.value)
    emit('update:modelValue', false)
  } catch (err) {
    submitError.value = projectErrorMessage(err)
  } finally {
    saving.value = false
  }
}
</script>
