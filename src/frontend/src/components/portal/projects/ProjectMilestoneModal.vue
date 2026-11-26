<!--
  Add or edit one of the goal's milestones (trinity-enterprise#1233). Only for
  a person who can change the goal — the page shows it to `can.edit`, and the
  server refuses anyone else.

  A milestone has a name, what "reached" means in words, its place in the
  order, and up to five targets: a metric an agent on the project declares
  (the same options the Metrics card's picker reads, linked or not), a number,
  and which way is good — "As declared" leaves that to the metric's own
  registry direction. Saving writes the milestone first, then its targets;
  a refusal names its field. Marking it reached is the person's call here;
  the page only suggests it when every target is met on a fresh reading.
-->
<template>
  <BaseModal
    :model-value="modelValue"
    labelledby="project-milestone-title"
    :close-on-backdrop="false"
    panel-class="relative w-full max-w-2xl rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800 max-h-[90vh] overflow-y-auto"
    @update:model-value="$emit('update:modelValue', $event)"
  >
    <form class="space-y-4" data-testid="project-milestone-form" @submit.prevent="save">
      <h2 id="project-milestone-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">{{ milestone ? 'Edit milestone' : 'Add a milestone' }}</h2>
      <BaseInput id="milestone-name" v-model="form.name" label="Name" :error="errors.name" placeholder="Ten design partners live" data-testid="project-milestone-name" />
      <BaseTextarea
        id="milestone-reached-means"
        v-model="form.reached_means"
        label="Reached means (optional)"
        :rows="2"
        :error="errors.reached_means"
        placeholder="Ten companies use the beta every week"
        help="In words. Add a target below to say it as a number too."
      />
      <div class="grid grid-cols-1 gap-3 sm:grid-cols-2">
        <BaseSelect v-if="milestone && count > 1" id="milestone-position" v-model="form.position" label="Place in the list" data-testid="project-milestone-position">
          <option v-for="i in count" :key="i" :value="i - 1">{{ i }} of {{ count }}</option>
        </BaseSelect>
        <BaseToggle v-if="milestone" v-model="form.reached" label="Reached" data-testid="project-milestone-reached" />
      </div>

      <fieldset class="space-y-2">
        <legend class="text-sm font-[550] text-gray-900 dark:text-gray-100">Targets</legend>
        <SkeletonLoader v-if="optionsView.state === 'loading'" :count="1" height="2.5rem" />
        <LoadFailed v-else-if="optionsView.state === 'failed'" title="Couldn't load the metrics" :message="optionsError" dense @retry="loadOptions" />
        <p v-else-if="!agentsWithMetrics.length && !form.targets.length" class="text-sm text-gray-600 dark:text-gray-300" data-testid="project-milestone-no-metrics">
          None of this project's agents declares a metric yet. An agent's owner declares one from the agent's Dashboard tab; it shows up here right away.
        </p>
        <div
          v-for="(t, i) in form.targets"
          :key="t.key"
          class="grid grid-cols-2 items-start gap-2 rounded-lg border border-gray-200 p-3 dark:border-gray-750 sm:grid-cols-[1fr_1fr_8rem_7rem_auto]"
          :data-testid="`project-milestone-target-${i}`"
        >
          <BaseSelect :id="`milestone-target-agent-${i}`" v-model="t.agent_name" label="Agent" size="sm" @update:model-value="pickFirstMetric(t)">
            <option v-for="a in agentChoices(t)" :key="a" :value="a">{{ a }}</option>
          </BaseSelect>
          <BaseSelect :id="`milestone-target-metric-${i}`" v-model="t.metric" label="Metric" size="sm">
            <option v-for="m in metricChoices(t)" :key="m.name" :value="m.name">{{ m.label }}{{ m.unit ? ` (${m.unit})` : '' }}</option>
          </BaseSelect>
          <BaseSelect :id="`milestone-target-direction-${i}`" v-model="t.direction" label="Good is" size="sm">
            <option value="">As declared</option>
            <option value="up">Higher</option>
            <option value="down">Lower</option>
            <option value="hold">Exactly</option>
          </BaseSelect>
          <BaseInput :id="`milestone-target-value-${i}`" v-model="t.target" label="Target" type="number" step="any" placeholder="10" :data-testid="`project-milestone-target-value-${i}`" />
          <BaseButton class="self-end" size="sm" variant="ghost" :aria-label="`Remove target ${i + 1}`" @click="removeTarget(i)">Remove</BaseButton>
          <p v-if="errors[`target_${i}`]" class="col-span-full text-xs text-status-danger-700 dark:text-status-danger-400" role="alert">{{ errors[`target_${i}`] }}</p>
        </div>
        <BaseButton
          v-if="agentsWithMetrics.length && form.targets.length < MILESTONE_TARGETS_MAX"
          size="sm"
          variant="ghost"
          data-testid="project-milestone-target-add"
          @click="addTarget"
        >Add target</BaseButton>
      </fieldset>

      <InlineError v-if="saveError" :message="saveError" @dismiss="saveError = ''" />
      <div class="flex items-center gap-2">
        <BaseButton v-if="milestone" variant="ghost" data-testid="project-milestone-delete" @click="confirmDelete = true">Delete</BaseButton>
        <span class="flex-1"></span>
        <BaseButton variant="secondary" @click="$emit('update:modelValue', false)">Cancel</BaseButton>
        <BaseButton type="submit" :loading="saving" loading-label="Saving…" data-testid="project-milestone-save">{{ milestone ? 'Save' : 'Add milestone' }}</BaseButton>
      </div>
    </form>
    <ConfirmDialog
      v-model:visible="confirmDelete"
      title="Delete this milestone?"
      :message="`“${milestone ? milestone.name : ''}” and its targets go. Its tasks stay on the project, under no milestone.`"
      confirm-text="Delete milestone"
      cancel-text="Keep it"
      @confirm="remove"
    />
  </BaseModal>
</template>

<script setup>
import { computed, reactive, ref, watch } from 'vue'
import BaseModal from '@/components/base/BaseModal.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseInput from '@/components/base/BaseInput.vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import BaseTextarea from '@/components/base/BaseTextarea.vue'
import BaseToggle from '@/components/base/BaseToggle.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import InlineError from '@/components/InlineError.vue'
import ConfirmDialog from '@/components/ConfirmDialog.vue'
import { useProjectsStore } from '@/stores/projects'
import { viewState } from '@/utils/loadingState'
import { MILESTONE_TARGETS_MAX, projectErrorMessage, validateMilestone } from './projectsUtils'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  projectId: { type: String, required: true },
  // null: a new milestone.
  milestone: { type: Object, default: null },
  // How many milestones the project has — the choices for its place.
  count: { type: Number, default: 0 },
})
const emit = defineEmits(['update:modelValue'])

const store = useProjectsStore()
const form = reactive({ name: '', reached_means: '', position: 0, reached: false, targets: [] })
const errors = ref({})
const saveError = ref('')
const saving = ref(false)
const confirmDelete = ref(false)
// A new milestone whose targets were refused is kept: Save again writes the
// targets onto it instead of adding the milestone a second time.
const createdId = ref(null)
let nextKey = 0

// The metric choices: every declared metric of each agent active on the project.
const options = ref([])
const optionsLoaded = ref(false)
const optionsError = ref(null)
const agentsWithMetrics = computed(() => options.value.filter((a) => a.metrics.length))
const optionsView = computed(() => viewState({ hasLoaded: optionsLoaded.value, error: optionsError.value, count: agentsWithMetrics.value.length }))
const metricsOf = (agent) => options.value.find((a) => a.agent_name === agent)?.metrics || []
// A saved target whose agent left the project is kept as a choice, so editing
// the rest never silently drops it.
const agentChoices = (t) => {
  const names = agentsWithMetrics.value.map((a) => a.agent_name)
  return t.agent_name && !names.includes(t.agent_name) ? [t.agent_name, ...names] : names
}
const metricChoices = (t) => {
  const list = metricsOf(t.agent_name)
  return t.metric && !list.some((m) => m.name === t.metric) ? [{ name: t.metric, label: t.metric }, ...list] : list
}

async function loadOptions() {
  try {
    options.value = await store.metricOptions(props.projectId, { all: true })
    optionsLoaded.value = true
    optionsError.value = null
  } catch (err) {
    optionsError.value = projectErrorMessage(err)
  }
}

const targetRow = (t = {}) => ({
  key: nextKey++,
  agent_name: t.agent_name || '',
  metric: t.metric || '',
  direction: t.direction_set || '',
  target: t.target === undefined || t.target === null ? '' : String(t.target),
  tolerance: t.tolerance ?? null,
})

function reset() {
  const m = props.milestone
  Object.assign(form, {
    name: m?.name || '',
    reached_means: m?.reached_means || '',
    position: m?.position ?? 0,
    reached: m?.state === 'reached',
    targets: (m?.targets || []).map(targetRow),
  })
  errors.value = {}
  saveError.value = ''
  createdId.value = null
}

watch(() => props.modelValue, (open) => {
  if (!open) return
  reset()
  optionsLoaded.value = false
  void loadOptions()
}, { immediate: true })

function addTarget() {
  const first = agentsWithMetrics.value[0]
  form.targets.push(targetRow({ agent_name: first?.agent_name, metric: first?.metrics[0]?.name }))
}
function removeTarget(i) { form.targets.splice(i, 1) }
function pickFirstMetric(t) { t.metric = metricsOf(t.agent_name)[0]?.name || '' }

const sentTargets = () => form.targets.map((t) => ({
  agent_name: t.agent_name, metric: t.metric, target: Number(t.target),
  direction: t.direction || null, tolerance: t.direction === 'hold' ? t.tolerance : null,
}))
const sameTargets = () => {
  const before = (props.milestone?.targets || []).map((t) => [t.agent_name, t.metric, t.target, t.direction_set || null])
  const after = sentTargets().map((t) => [t.agent_name, t.metric, t.target, t.direction])
  return JSON.stringify(before) === JSON.stringify(after)
}

async function save() {
  errors.value = validateMilestone(form)
  if (Object.keys(errors.value).length) return
  saving.value = true
  saveError.value = ''
  try {
    const name = form.name.trim()
    const reachedMeans = form.reached_means.trim()
    let id = props.milestone?.id || createdId.value
    if (!id) {
      const made = await store.createMilestone(props.projectId, reachedMeans ? { name, reached_means: reachedMeans } : { name })
      id = createdId.value = made.id
    } else if (props.milestone) {
      const body = {}
      if (name !== props.milestone.name) body.name = name
      if (reachedMeans !== (props.milestone.reached_means || '')) body.reached_means = reachedMeans || null
      if (Number(form.position) !== props.milestone.position) body.position = Number(form.position)
      if (form.reached !== (props.milestone.state === 'reached')) body.reached = form.reached
      if (Object.keys(body).length) await store.updateMilestone(props.projectId, id, body)
    }
    if (form.targets.length || props.milestone) {
      if (!props.milestone || !sameTargets()) await store.setMilestoneTargets(props.projectId, id, sentTargets())
    }
    emit('update:modelValue', false)
  } catch (err) {
    saveError.value = projectErrorMessage(err)
  } finally {
    saving.value = false
  }
}

async function remove() {
  saveError.value = ''
  try {
    await store.deleteMilestone(props.projectId, props.milestone.id)
    emit('update:modelValue', false)
  } catch (err) {
    saveError.value = projectErrorMessage(err)
  }
}
</script>
