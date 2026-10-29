<template>
  <BaseModal
    :model-value="modelValue"
    panel-class="relative flex max-h-[90vh] w-full max-w-2xl flex-col rounded-lg bg-white shadow-xl dark:bg-gray-800"
    labelledby="metric-declaration-title"
    :close-on-backdrop="false"
    @update:model-value="(v) => emit('update:modelValue', v)"
    @close="close"
  >
    <div class="flex items-start justify-between gap-3 border-b border-gray-200 px-6 pb-4 pt-5 dark:border-gray-750">
      <div class="min-w-0">
        <h2 id="metric-declaration-title" class="text-lg font-semibold text-gray-900 dark:text-white">
          {{ isEdit ? `Edit ${metric?.label || metric?.name}` : 'Declare a metric' }}
        </h2>
        <p class="mt-1 text-xs text-gray-600 dark:text-gray-300" data-testid="metric-declaration-subtitle">
          {{ agentName }}<template v-if="editor"> · {{ editor.declared_count }} of {{ editor.cap }} declared</template>
        </p>
      </div>
      <BaseButton variant="ghost" size="sm" aria-label="Close" @click="close">
        <svg class="h-4 w-4" fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true">
          <path stroke-linecap="round" stroke-width="2" d="M6 6l12 12M18 6 6 18" />
        </svg>
      </BaseButton>
    </div>

    <div class="min-h-[240px] flex-1 overflow-y-auto px-6 py-5">
      <!-- First load: the form's own footprint, no spinner (p4, p12). -->
      <div v-if="phase === 'loading'" class="space-y-4" aria-busy="true" data-testid="metric-declaration-loading">
        <span class="sr-only">Reading template.yaml…</span>
        <div v-for="n in 4" :key="n" class="h-10 animate-pulse rounded-md bg-gray-100 motion-reduce:animate-none dark:bg-gray-750"></div>
      </div>

      <!-- A refusal that makes the form pointless replaces it, and says so. -->
      <div
        v-else-if="phase === 'blocked'"
        class="space-y-3 rounded-lg border border-gray-200 p-4 dark:border-gray-750"
        data-testid="metric-declaration-blocked"
        :data-code="blocked.code"
      >
        <p class="text-sm font-semibold text-gray-900 dark:text-white">{{ blocked.title }}</p>
        <p class="text-sm text-gray-700 dark:text-gray-300">{{ blocked.body }}</p>
        <details class="text-xs text-gray-600 dark:text-gray-300">
          <summary class="cursor-pointer">Details</summary>
          <code class="font-mono">{{ blocked.code }}</code>
          <span v-if="blocked.message"> · {{ blocked.message }}</span>
        </details>
        <BaseButton variant="secondary" size="sm" :loading="reading" loading-label="Checking…" @click="readEditor">
          Try again
        </BaseButton>
      </div>

      <form v-else class="space-y-5" data-testid="metric-declaration-form" novalidate @submit.prevent="submit">
        <InlineError
          v-if="submitError"
          :message="submitError.message"
          :detail="submitError.code"
          :retryable="submitError.code === 'template_changed'"
          retry-label="Reload and keep my entries"
          data-testid="metric-declaration-error"
          @retry="reloadKeepingEntries"
          @dismiss="submitError = null"
        />

        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <BaseInput
            v-model="form.label"
            label="Label"
            help="What people see on the tile."
            :error="fieldErrors.label"
            data-testid="metric-label"
            @update:model-value="suggestName"
          />
          <div v-if="isEdit" class="space-y-1">
            <span class="block text-sm font-medium text-gray-900 dark:text-white">Name</span>
            <div class="rounded-md border border-gray-200 bg-gray-100 px-3 py-2 font-mono text-sm text-gray-700 dark:border-gray-750 dark:bg-gray-900 dark:text-gray-300" data-testid="metric-name-locked">
              {{ form.name }}
            </div>
            <p class="text-xs text-gray-600 dark:text-gray-300">Points are stored under this name. To rename, retire this metric and declare a new one.</p>
          </div>
          <BaseInput
            v-else
            v-model="form.name"
            label="Name"
            help="Made from the label. The agent records under this name, so it can't change later."
            :error="fieldErrors.name"
            data-testid="metric-name"
            @update:model-value="form.nameEdited = true"
          />
        </div>

        <fieldset>
          <legend class="mb-2 text-sm font-medium text-gray-900 dark:text-white">
            {{ isEdit ? 'Kind of number' : 'What kind of number is it?' }}
          </legend>
          <p v-if="isEdit" class="text-sm text-gray-700 dark:text-gray-300" data-testid="metric-type-locked">
            {{ typeTitle(form.type) }}
            <span class="text-xs text-gray-600 dark:text-gray-300"> · fixed once declared, so earlier points keep their meaning.</span>
          </p>
          <div v-else class="grid grid-cols-1 gap-2 sm:grid-cols-3">
            <button
              v-for="t in TYPE_OPTIONS"
              :key="t.value"
              type="button"
              :aria-pressed="form.type === t.value"
              class="flex flex-col gap-0.5 rounded-lg border px-3 py-2 text-left focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
              :class="form.type === t.value
                ? 'border-action-primary-600 bg-action-primary-50 dark:border-action-primary-400 dark:bg-action-primary-500/15'
                : 'border-gray-200 bg-white hover:bg-gray-50 dark:border-gray-750 dark:bg-gray-800 dark:hover:bg-gray-750'"
              :data-testid="`metric-type-${t.value}`"
              @click="form.type = t.value"
            >
              <span class="text-sm font-medium text-gray-900 dark:text-white">{{ t.title }}</span>
              <span class="text-xs text-gray-600 dark:text-gray-300">{{ t.hint }}</span>
            </button>
          </div>
          <p v-if="fieldErrors.type" class="mt-1 text-xs text-status-danger-700 dark:text-status-danger-400">{{ fieldErrors.type }}</p>
        </fieldset>

        <div v-if="form.type === 'status'" class="space-y-2" data-testid="metric-status-values">
          <span class="block text-sm font-medium text-gray-900 dark:text-white">Status values</span>
          <p class="text-xs text-gray-600 dark:text-gray-300">The only values the agent may record. Each gets a colour on the tile.</p>
          <div v-for="(row, i) in form.values" :key="i" class="grid grid-cols-[minmax(0,1fr)_7rem_minmax(0,1fr)_auto] items-start gap-2">
            <BaseInput v-model="row.value" :aria-label="`Value ${i + 1}`" placeholder="healthy" />
            <BaseSelect v-model="row.color" :aria-label="`Colour ${i + 1}`">
              <option v-for="c in STATUS_COLORS" :key="c" :value="c">{{ c }}</option>
            </BaseSelect>
            <BaseInput v-model="row.label" :aria-label="`Label ${i + 1}`" placeholder="Healthy" />
            <BaseButton variant="ghost" size="sm" :aria-label="`Remove value ${i + 1}`" :disabled="form.values.length <= 1" @click="form.values.splice(i, 1)">
              Remove
            </BaseButton>
          </div>
          <BaseButton variant="ghost" size="sm" data-testid="metric-add-value" @click="form.values.push({ value: '', color: 'gray', label: '' })">
            + Add value
          </BaseButton>
          <p v-if="fieldErrors.values" class="text-xs text-status-danger-700 dark:text-status-danger-400">{{ fieldErrors.values }}</p>
        </div>

        <div class="grid grid-cols-1 gap-4 sm:grid-cols-2">
          <div class="space-y-2">
            <BaseSelect
              v-model="form.cadenceChoice"
              label="How often is it recorded?"
              :help="form.cadenceChoice === CUSTOM_CADENCE ? '' : staleHelp(form.cadenceChoice)"
              :error="form.cadenceChoice === CUSTOM_CADENCE ? '' : fieldErrors.cadence"
              data-testid="metric-cadence"
            >
              <option v-for="c in CADENCE_OPTIONS" :key="c.value" :value="c.value">{{ c.label }}</option>
              <option :value="CUSTOM_CADENCE">Custom…</option>
            </BaseSelect>
            <BaseInput
              v-if="form.cadenceChoice === CUSTOM_CADENCE"
              v-model="form.customCadence"
              aria-label="Custom cadence"
              placeholder="e.g. 12h, 3d, PT30M"
              :help="staleHelp(CUSTOM_CADENCE)"
              :error="fieldErrors.cadence"
              data-testid="metric-custom-cadence"
            />
          </div>
          <BaseInput
            v-if="form.type !== 'status'"
            v-model="form.unit"
            label="Unit (optional)"
            placeholder="e.g. USD, leads, ms"
            help="Shown after the number."
            :error="fieldErrors.unit"
          />
        </div>

        <div v-if="form.type !== 'status'" class="space-y-1">
          <span id="metric-direction-label" class="block text-sm font-medium text-gray-900 dark:text-white">Which way is good?</span>
          <div role="group" aria-labelledby="metric-direction-label" class="flex flex-wrap gap-2">
            <BaseButton
              v-for="d in DIRECTION_OPTIONS"
              :key="d.value"
              size="sm"
              :variant="form.direction === d.value ? 'primary' : 'secondary'"
              :aria-pressed="form.direction === d.value"
              @click="form.direction = d.value"
            >{{ d.label }}</BaseButton>
          </div>
          <p class="text-xs text-gray-600 dark:text-gray-300">Colours the trend arrow green or red.</p>
        </div>

        <BaseTextarea v-model="form.description" label="Description (optional)" :rows="2" :error="fieldErrors.description" />

        <div class="border-t border-gray-200 pt-3 dark:border-gray-750">
          <button
            type="button"
            class="flex items-center gap-2 rounded text-sm font-medium text-gray-900 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:text-white"
            :aria-expanded="advanced"
            data-testid="metric-more-options"
            @click="advanced = !advanced"
          >
            <span aria-hidden="true">{{ advanced ? '▾' : '▸' }}</span> More options
            <span class="text-xs font-normal text-gray-600 dark:text-gray-300">thresholds, dimensions, who records it</span>
          </button>
          <div v-if="advanced" class="mt-4 space-y-4 pl-4">
            <BaseInput
              v-model="form.recordedBy"
              label="Recorded by (optional note)"
              placeholder="e.g. the weekly-pipeline-review skill"
              help="For people reading the tile. Trinity doesn't use it to record anything."
              :error="fieldErrors.recorded_by || fieldErrors['x-recorded-by']"
              data-testid="metric-recorded-by"
            />
            <div v-if="form.type !== 'status'" class="grid grid-cols-1 gap-4 sm:grid-cols-2">
              <BaseInput v-model="form.warning" label="Warning threshold (optional)" placeholder="e.g. 25" inputmode="decimal" :error="fieldErrors.warning_threshold" data-testid="metric-warning" />
              <BaseInput v-model="form.critical" label="Critical threshold (optional)" placeholder="e.g. 20" inputmode="decimal" :error="fieldErrors.critical_threshold" />
            </div>
            <BaseInput
              v-model="form.dimensions"
              label="Dimensions (optional, up to 10)"
              placeholder="e.g. region, segment"
              :help="isEdit
                ? 'Removing a dimension the agent still records with makes those points fail. Add, don\'t remove, unless the agent has been updated.'
                : 'Lets the agent record one series per region, team or similar.'"
              :error="fieldErrors.dimensions"
            />
            <BaseSelect v-model="form.aggregation" label="When there are several series, the tile shows" :error="fieldErrors.aggregation">
              <option v-for="a in AGGREGATION_OPTIONS" :key="a.value" :value="a.value">{{ a.label }}</option>
            </BaseSelect>
          </div>
        </div>

        <div class="rounded-lg border border-gray-200 bg-gray-50 dark:border-gray-750 dark:bg-gray-900">
          <button
            type="button"
            class="flex w-full items-center gap-2 rounded-lg px-3 py-2 text-left text-xs text-gray-600 focus:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:text-gray-300"
            :aria-expanded="showYaml"
            @click="showYaml = !showYaml"
          >
            <span aria-hidden="true">{{ showYaml ? '▾' : '▸' }}</span>
            {{ isEdit ? 'Change to template.yaml' : 'What gets added to template.yaml' }}
          </button>
          <div v-if="showYaml" class="px-3 pb-3" data-testid="metric-yaml-preview">
            <template v-if="isEdit">
              <p v-if="!hasChanges" class="text-xs text-gray-600 dark:text-gray-300">No changes yet.</p>
              <pre v-else class="whitespace-pre-wrap font-mono text-xs leading-relaxed"><span
                v-for="(line, i) in diff"
                :key="i"
                class="block"
                :class="line.kind === '+'
                  ? 'text-status-success-700 dark:text-status-success-400'
                  : line.kind === '-' ? 'text-status-danger-700 dark:text-status-danger-400' : 'text-gray-700 dark:text-gray-300'"
              >{{ line.kind }} {{ line.text }}</span></pre>
            </template>
            <pre v-else class="whitespace-pre-wrap font-mono text-xs leading-relaxed text-gray-800 dark:text-gray-200">metrics:
{{ preview.map((l) => '  ' + l).join('\n') }}</pre>
          </div>
        </div>
      </form>
    </div>

    <div class="flex flex-wrap items-center gap-3 rounded-b-lg border-t border-gray-200 bg-gray-50 px-6 py-4 dark:border-gray-750 dark:bg-gray-900">
      <BaseButton
        v-if="isEdit && phase === 'ready'"
        variant="ghost"
        size="sm"
        data-testid="metric-retire-from-edit"
        @click="emit('retire', metric)"
      >Retire metric…</BaseButton>
      <span v-else class="flex-1 text-xs text-gray-600 dark:text-gray-300">
        Saved to template.yaml in the agent. The agent's git sync carries it to the repo.
      </span>
      <div class="ml-auto flex items-center gap-2">
        <BaseButton variant="secondary" @click="close">Cancel</BaseButton>
        <BaseButton
          v-if="phase === 'ready'"
          :loading="saving"
          :loading-label="isEdit ? 'Saving…' : 'Declaring…'"
          :disabled="!canSubmit"
          data-testid="metric-declaration-submit"
          @click="submit"
        >{{ isEdit ? 'Save changes' : 'Declare metric' }}</BaseButton>
      </div>
    </div>
  </BaseModal>
</template>

<script setup>
/**
 * Declare or edit one metric (trinity-enterprise#1006).
 *
 * The form writes the agent's `template.yaml` `metrics:` block through the
 * backend, which validates with the same parser a hand edit goes through and
 * returns named per-field errors; this component only maps them onto fields.
 * Every save carries the template hash this form read, so an agent that
 * edited its own template meanwhile is never overwritten — the person is
 * offered a reload that keeps what they typed.
 */
import { computed, reactive, ref, watch } from 'vue'
import { useAgentsStore } from '../stores/agents'
import BaseModal from './base/BaseModal.vue'
import BaseButton from './base/BaseButton.vue'
import BaseInput from './base/BaseInput.vue'
import BaseSelect from './base/BaseSelect.vue'
import BaseTextarea from './base/BaseTextarea.vue'
import InlineError from './InlineError.vue'
import {
  AGGREGATION_OPTIONS,
  BLOCKING_REFUSALS,
  CADENCE_OPTIONS,
  CUSTOM_CADENCE,
  DIRECTION_OPTIONS,
  STATUS_COLORS,
  TYPE_OPTIONS,
  blankForm,
  diffPreview,
  formFromDeclaration,
  nameFromLabel,
  payloadFromForm,
  previewLines,
  readRefusal,
  staleHelp,
  typeTitle,
} from '../utils/metricDeclaration'

const props = defineProps({
  modelValue: { type: Boolean, default: false },
  agentName: { type: String, required: true },
  // The metric being edited (a tile's row); null declares a new one.
  metric: { type: Object, default: null },
})
const emit = defineEmits(['update:modelValue', 'saved', 'retire'])

const agentsStore = useAgentsStore()
const isEdit = computed(() => Boolean(props.metric))

const phase = ref('loading') // loading | blocked | ready
const editor = ref(null)
const blocked = ref({ code: '', title: '', body: '', message: '' })
const reading = ref(false)
const saving = ref(false)
const submitError = ref(null)
const fieldErrors = ref({})
const advanced = ref(false)
const showYaml = ref(true)
const form = reactive(blankForm())
const initialPreview = ref([])

const built = computed(() => payloadFromForm(form))
const preview = computed(() => previewLines(built.value.payload))
const diff = computed(() => diffPreview(initialPreview.value, preview.value))
const hasChanges = computed(() => diff.value.some((l) => l.kind !== ' '))
const canSubmit = computed(() => !saving.value && (!isEdit.value || hasChanges.value))

function suggestName(label) {
  if (!isEdit.value && !form.nameEdited) form.name = nameFromLabel(label)
}

function block(code, message) {
  const copy = BLOCKING_REFUSALS[code] || { title: 'The metrics could not be read', body: message || 'Try again in a moment.' }
  blocked.value = { code, message, ...copy }
  phase.value = 'blocked'
}

async function readEditor() {
  reading.value = true
  try {
    const data = await agentsStore.getMetricDeclarations(props.agentName)
    editor.value = data
    if (!data.editable) {
      block('template_not_editable', data.reason)
      return null
    }
    phase.value = 'ready'
    return data
  } catch (error) {
    const refusal = readRefusal(error)
    block(refusal.code, refusal.message)
    return null
  } finally {
    reading.value = false
  }
}

async function open() {
  phase.value = 'loading'
  submitError.value = null
  fieldErrors.value = {}
  editor.value = null
  Object.assign(form, blankForm())
  const data = await readEditor()
  if (!data) return
  if (isEdit.value) {
    const current = data.declarations.find((d) => d.name === props.metric.name)
    if (!current) {
      block('metric_not_found', `${props.metric.name} is no longer declared in template.yaml.`)
      return
    }
    Object.assign(form, formFromDeclaration(current))
    advanced.value = Boolean(current.recorded_by || current.dimensions?.length
      || current.warning_threshold !== null || current.critical_threshold !== null)
  }
  initialPreview.value = previewLines(payloadFromForm(form).payload)
}

async function reloadKeepingEntries() {
  submitError.value = null
  await readEditor()
}

async function submit() {
  if (!canSubmit.value || phase.value !== 'ready') return
  const { payload, errors } = built.value
  fieldErrors.value = errors
  if (Object.keys(errors).length) {
    advanced.value = true
    return
  }
  saving.value = true
  submitError.value = null
  try {
    const hash = editor.value.template_hash
    const result = isEdit.value
      ? await agentsStore.updateMetricDeclaration(props.agentName, props.metric.name, payload, hash)
      : await agentsStore.declareMetric(props.agentName, payload, hash)
    emit('saved', { ...result, label: payload.label || payload.name })
    emit('update:modelValue', false)
  } catch (error) {
    const refusal = readRefusal(error)
    if (BLOCKING_REFUSALS[refusal.code]) {
      block(refusal.code, refusal.message)
    } else {
      fieldErrors.value = refusal.fieldErrors
      if (Object.keys(refusal.fieldErrors).some((f) => ['recorded_by', 'x-recorded-by', 'warning_threshold',
        'critical_threshold', 'dimensions', 'aggregation'].includes(f))) {
        advanced.value = true
      }
      const count = Object.keys(refusal.fieldErrors).length
      submitError.value = {
        code: refusal.code,
        message: refusal.code === 'metric_invalid'
          ? `${count === 1 ? 'One field needs' : `${count} fields need`} a fix before this can be saved. Nothing was written to template.yaml.`
          : (refusal.fieldErrors._form || refusal.message),
      }
    }
  } finally {
    saving.value = false
  }
}

function close() {
  emit('update:modelValue', false)
}

watch(() => props.modelValue, (isOpen) => { if (isOpen) open() }, { immediate: true })
</script>
