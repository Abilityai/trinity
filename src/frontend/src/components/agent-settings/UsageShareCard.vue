<template>
  <!--
    An agent's share of its subscription's usage (trinity-enterprise#1059).

    The share is a budget in the platform's own cost accounting — the numbers
    Settings › Subscriptions shows — over a rolling 5-hour and 7-day window.
    From the warning line the agent is told on every turn; at a budget every
    door refuses it until the window frees or a person raises the share. This
    card is the owner's view of both windows and the raise itself; it takes
    effect on the next turn, with no restart (unlike the guardrail rules).

    Rendered only when the instance is entitled to `usage_share` — the routes
    live in the enterprise module, so an unentitled build has nothing to call.
  -->
  <div class="divide-y divide-gray-200 dark:divide-gray-750" data-testid="usage-share-card">
    <div v-if="view.state === 'loading'" class="px-4 py-3 space-y-3" aria-busy="true">
      <span class="block h-3 w-56 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" />
      <span class="block h-3 w-56 rounded bg-gray-200 dark:bg-gray-750 animate-pulse motion-reduce:animate-none" />
      <span class="sr-only">Loading the usage share…</span>
    </div>

    <LoadFailed
      v-else-if="view.state === 'failed'"
      dense
      title="Couldn't load the usage share"
      message="The share and its usage could not be read. Try again; nothing about the agent changed."
      @retry="load"
    />

    <template v-else>
      <SettingRow
        v-for="w in windows"
        :key="w.id"
        :label="w.label"
        :hint="w.hint"
        :testid="`usage-share-${w.id}`"
      >
        <template #badge>
          <BaseBadge
            v-if="w.level !== 'ok'"
            :variant="w.level === 'stop' ? 'danger' : 'warning'"
            size="sm"
            dot
            :data-testid="`usage-share-${w.id}-state`"
          >{{ w.level === 'stop' ? 'Stopped' : 'Warning' }}</BaseBadge>
        </template>
        <template #hint>
          <p v-if="w.level === 'stop'" class="mt-0.5 text-[12.5px] text-gray-600 dark:text-gray-400">
            Every turn is refused until the share is raised or the window frees<span
              v-if="w.freesAt"
              :title="w.freesAtFull"
            > ({{ w.freesAt }})</span>.
            <template v-if="w.askFiledAt">An ask to raise it is pending.</template>
          </p>
        </template>
        <template #value>
          <div class="w-56">
            <span class="block text-[12.5px] tabular-nums text-gray-700 dark:text-gray-300">
              {{ w.used }} / {{ w.budget }} · {{ w.pct }}%
            </span>
            <div
              class="relative mt-1 h-1.5 rounded-full bg-gray-200 dark:bg-gray-750"
              role="meter"
              aria-valuemin="0"
              aria-valuemax="100"
              :aria-valuenow="w.pct"
              :aria-label="`${w.label}: ${w.pct}% of the budget used`"
            >
              <div
                class="h-full rounded-full"
                :class="FILL_CLASS[w.level]"
                :style="{ width: `${w.fillPct}%` }"
                data-testid="usage-share-fill"
              />
              <div
                class="absolute -top-0.5 -bottom-0.5 w-px bg-gray-900 dark:bg-gray-100"
                :style="{ left: `${share.warn_pct}%` }"
                aria-hidden="true"
                data-testid="usage-share-warn-mark"
              />
            </div>
          </div>
        </template>
      </SettingRow>

      <p class="px-4 py-3 text-[12.5px] text-gray-600 dark:text-gray-400">
        The agent is warned from {{ share.warn_pct }}% of either budget
        ({{ sourceLabel('warn_pct') }}) and asked to raise its ask; at a budget it is stopped on every door.
      </p>

      <div v-if="share.notice" class="px-4 py-3">
        <p class="text-[12.5px] text-gray-600 dark:text-gray-400">What the agent reads on its next turn</p>
        <p class="mt-1 rounded-md bg-gray-100 dark:bg-gray-750 px-3 py-2 text-[12.5px] text-gray-700 dark:text-gray-300" data-testid="usage-share-notice">
          {{ share.notice }}
        </p>
      </div>

      <SettingRow
        label="Change the share"
        hint="Cost units, as Settings › Subscriptions counts them. An empty field uses the platform default; empty all three to clear this agent's override. Applies from the next turn — no restart."
        testid="usage-share-change"
      >
        <template #action>
          <BaseButton
            variant="secondary"
            size="sm"
            :aria-expanded="String(editorOpen)"
            data-testid="usage-share-edit"
            @click="editorOpen ? closeEditor() : openEditor()"
          >{{ editorOpen ? 'Close' : 'Change share' }}</BaseButton>
        </template>
        <template v-if="editorOpen" #editor>
          <div class="grid gap-3 sm:grid-cols-3">
            <div v-for="f in FIELDS" :key="f.id" :data-testid="`usage-share-input-${f.id}`">
              <BaseInput
                v-model="draft[f.id]"
                type="number"
                :label="f.label"
                :placeholder="placeholderFor(f.id)"
                :error="errors[f.id]"
                :disabled="saving"
              />
            </div>
          </div>
          <InlineError class="mt-3" :message="saveError" @dismiss="saveError = ''" />
          <div class="mt-3 flex items-center gap-2">
            <BaseButton
              variant="primary"
              size="sm"
              :loading="saving"
              loading-label="Saving…"
              data-testid="usage-share-save"
              @click="save"
            >Save share</BaseButton>
            <BaseButton variant="ghost" size="sm" :disabled="saving" @click="closeEditor">Cancel</BaseButton>
          </div>
        </template>
      </SettingRow>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, reactive, ref, watch } from 'vue'
import { useAgentsStore } from '../../stores/agents'
import { viewState } from '../../utils/loadingState'
import { formatResetTime } from '../../utils/subscriptionPressure'
import { formatLocalDateTime } from '../../utils/timestamps'
import SettingRow from './SettingRow.vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  notify: { type: Function, default: null },
})

// Bounds mirror the enterprise route's (UsageShareUpdate) so a bad value is
// named here, before a request; the route still refuses with its own 422.
const BUDGET_MAX = 100000
const WARN_MIN = 50
const WARN_MAX = 99

const FIELDS = [
  { id: 'budget_7d', label: '7-day budget', kind: 'budget' },
  { id: 'budget_5h', label: '5-hour budget', kind: 'budget' },
  { id: 'warn_pct', label: 'Warn from (%)', kind: 'pct' },
]

const FILL_CLASS = {
  ok: 'bg-gray-500 dark:bg-gray-400',
  warn: 'bg-status-warning-500 dark:bg-status-warning-400',
  stop: 'bg-status-danger-500 dark:bg-status-danger-400',
}

const agentsStore = useAgentsStore()

const share = ref(null)
const loadError = ref(null)
const view = computed(() => viewState({ hasLoaded: !!share.value, error: loadError.value }))

function fmt(n) {
  return Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 2 })
}

function sourceLabel(key) {
  return share.value?.sources?.[key] === 'agent' ? 'set for this agent' : 'platform default'
}

function windowRow(id, label, budgetKey) {
  const w = share.value[`window_${id}`] || {}
  const pct = Number(w.pct) || 0
  const level = w.at_budget ? 'stop' : pct >= share.value.warn_pct ? 'warn' : 'ok'
  return {
    id,
    label,
    hint: `Rolling ${label.toLowerCase().replace('last ', '')} · ${sourceLabel(budgetKey)}`,
    level,
    pct,
    fillPct: Math.min(Math.max(pct, 0), 100),
    used: fmt(w.used),
    budget: fmt(w.budget),
    freesAt: formatResetTime(w.frees_at),
    freesAtFull: w.frees_at ? formatLocalDateTime(w.frees_at) : '',
    askFiledAt: w.ask_filed_at || null,
  }
}

const windows = computed(() => share.value
  ? [windowRow('7d', 'Last 7 days', 'budget_7d'), windowRow('5h', 'Last 5 hours', 'budget_5h')]
  : [])

async function load() {
  loadError.value = null
  try {
    share.value = await agentsStore.getUsageShare(props.agentName)
  } catch (e) {
    // A failed refresh keeps the numbers on screen; only a first read fails the card.
    loadError.value = e || new Error('failed')
  }
}

// ---- the owner's change -------------------------------------------------------
const editorOpen = ref(false)
const saving = ref(false)
const saveError = ref('')
const draft = reactive({ budget_7d: '', budget_5h: '', warn_pct: '' })

function currentValue(id) {
  if (id === 'warn_pct') return share.value.warn_pct
  return share.value[`window_${id.slice('budget_'.length)}`]?.budget
}

function placeholderFor(id) {
  return share.value?.sources?.[id] === 'agent'
    ? 'empty = platform default'
    : `platform default: ${fmt(currentValue(id))}`
}

// Pre-filled with the agent's own override only: a platform-sourced number
// stays empty (and shows as the placeholder), so saving without touching it
// keeps following the platform default instead of pinning today's value.
function openEditor() {
  for (const f of FIELDS) {
    draft[f.id] = share.value.sources?.[f.id] === 'agent' ? String(currentValue(f.id)) : ''
  }
  saveError.value = ''
  editorOpen.value = true
}

function closeEditor() {
  editorOpen.value = false
  saveError.value = ''
}

function fieldError(f, raw) {
  const text = String(raw ?? '').trim()
  if (text === '') return ''
  const n = Number(text)
  if (f.kind === 'budget') {
    if (!Number.isFinite(n) || n <= 0 || n > BUDGET_MAX) {
      return `Enter a budget above 0 and at most ${BUDGET_MAX} cost units — e.g. 100. Empty uses the platform default.`
    }
    return ''
  }
  if (!Number.isInteger(n) || n < WARN_MIN || n > WARN_MAX) {
    return `Enter a whole percent from ${WARN_MIN} to ${WARN_MAX} — e.g. 80. Empty uses the platform default.`
  }
  return ''
}

const errors = computed(() => Object.fromEntries(FIELDS.map((f) => [f.id, fieldError(f, draft[f.id])])))

function describeFailure(e) {
  const detail = e?.response?.data?.detail
  if (detail && typeof detail === 'object' && detail.message) return detail.message
  if (typeof detail === 'string' && detail) return detail
  return "Couldn't save the share. Nothing changed — try again."
}

async function save() {
  if (saving.value || FIELDS.some((f) => errors.value[f.id])) return
  const body = Object.fromEntries(FIELDS.map((f) => {
    const text = String(draft[f.id] ?? '').trim()
    return [f.id, text === '' ? null : Number(text)]
  }))
  saving.value = true
  saveError.value = ''
  try {
    share.value = await agentsStore.setUsageShare(props.agentName, body)
    editorOpen.value = false
    const cleared = FIELDS.every((f) => body[f.id] === null)
    if (props.notify) {
      props.notify(
        cleared
          ? 'Usage share back to the platform default — it applies from the next turn.'
          : 'Usage share saved — it applies from the next turn, no restart needed.',
        'success',
      )
    }
  } catch (e) {
    saveError.value = describeFailure(e)
  } finally {
    saving.value = false
  }
}

function reset() {
  share.value = null
  loadError.value = null
  editorOpen.value = false
  load()
}

onMounted(load)
watch(() => props.agentName, reset)
</script>
