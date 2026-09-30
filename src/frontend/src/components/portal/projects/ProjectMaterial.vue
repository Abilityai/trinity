<!--
  Files, reports and decisions on a project (trinity-enterprise#661 v2.3).

  A project links things that already exist in the Workspace. Members see
  everything linked; an invited outside client sees only files and reports the
  creator marked "Shared with guests", never decisions. "Add from…" offers what
  this person can already see on the project's agents — adding one shares it
  with the project's readers (a file's download link included), which the
  dialog says before anything happens (principle 26).
-->
<template>
  <div class="space-y-5" data-testid="project-material">
    <div class="flex items-center gap-2">
      <h2 class="text-base font-semibold text-gray-900 dark:text-gray-100">Files &amp; reports</h2>
      <span class="flex-1"></span>
      <BaseButton v-if="canContribute && !archived" size="sm" variant="secondary" data-testid="project-add-item" @click="openPicker">Add from…</BaseButton>
    </div>
    <InlineError v-if="error" :message="error" @dismiss="error = ''" />

    <p v-if="!files.length && !reports.length && !decisions.length" class="rounded-lg border border-dashed border-gray-300 dark:border-gray-700 p-6 text-center text-sm text-gray-600 dark:text-gray-300" data-testid="project-material-empty">
      {{ guest
        ? 'Nothing has been shared with you here yet.'
        : 'Nothing added yet. Agents put what they produce here, or use "Add from…" to add a file, report or decision you already have.' }}
    </p>

    <section v-if="files.length">
      <h3 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Files · <span class="tabular-nums">{{ files.length }}</span></h3>
      <ul class="mt-1.5 divide-y divide-gray-100 dark:divide-gray-750 rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800">
        <li v-for="f in files" :key="f.id" class="flex items-center gap-3 px-3 py-2 text-sm">
          <div class="min-w-0 flex-1">
            <p class="truncate text-gray-900 dark:text-gray-100">{{ f.filename }}</p>
            <p class="text-xs text-gray-500 dark:text-gray-400">{{ f.agent_name }} · {{ humanSize(f.size_bytes) }} · link expires {{ formatRelativeTime(f.expires_at) }}</p>
          </div>
          <BaseBadge v-if="!guest && f.audience === 'guests'" variant="info">Shared with guests</BaseBadge>
          <a :href="f.download_url" class="text-sm text-action-primary-600 dark:text-action-primary-400 hover:underline" :aria-label="`Download ${f.filename}`">Download</a>
          <ItemMenu v-if="!guest" :item="f" kind="file" :can-share="canShare" :can-remove="canRemove(f)" @share="toggleGuests('file', f)" @remove="remove('file', f)" />
        </li>
      </ul>
    </section>

    <section v-if="reports.length">
      <h3 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Reports · <span class="tabular-nums">{{ reports.length }}</span></h3>
      <ul class="mt-1.5 divide-y divide-gray-100 dark:divide-gray-750 rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800">
        <li v-for="r in reports" :key="r.id" class="flex items-center gap-3 px-3 py-2 text-sm">
          <button type="button" class="min-w-0 flex-1 text-left" @click="openReport(r)">
            <p class="truncate text-gray-900 dark:text-gray-100 hover:underline">{{ r.title || 'Untitled report' }}</p>
            <p class="text-xs text-gray-500 dark:text-gray-400">{{ r.agent_name }} · <span :title="formatLocalDateTime(r.created_at)">{{ formatRelativeTime(r.created_at) }}</span></p>
          </button>
          <BaseBadge v-if="!guest && r.audience === 'guests'" variant="info">Shared with guests</BaseBadge>
          <ItemMenu v-if="!guest" :item="r" kind="report" :can-share="canShare" :can-remove="canRemove(r)" @share="toggleGuests('report', r)" @remove="remove('report', r)" />
        </li>
      </ul>
    </section>

    <section v-if="decisions.length">
      <h3 class="text-[11px] font-semibold uppercase tracking-wide text-gray-500 dark:text-gray-400">Decisions · <span class="tabular-nums">{{ decisions.length }}</span></h3>
      <ul class="mt-1.5 space-y-2">
        <li v-for="d in decisions" :key="d.id" class="rounded-lg border border-gray-200 dark:border-gray-750 bg-white dark:bg-gray-800 px-3 py-2 text-sm">
          <div class="flex items-center gap-2">
            <BaseBadge :variant="d.outcome === 'approved' ? 'success' : d.outcome === 'killed' ? 'danger' : 'warning'">{{ d.outcome }}</BaseBadge>
            <p class="min-w-0 flex-1 truncate text-gray-900 dark:text-gray-100">{{ d.decided }}</p>
            <ItemMenu :item="d" kind="decision" :can-share="false" :can-remove="canRemove(d)" @remove="remove('decision', d)" />
          </div>
          <p class="mt-1 text-xs text-gray-500 dark:text-gray-400">
            {{ d.decided_by }} · {{ d.agent_name }} · <span :title="formatLocalDateTime(d.decided_at)">{{ formatRelativeTime(d.decided_at) }}</span>
            <template v-if="d.criterion"> · because {{ d.criterion }}</template>
          </p>
        </li>
      </ul>
    </section>

    <!-- Add from… -->
    <BaseModal v-model="pickerOpen" labelledby="add-item-title" panel-class="relative w-full max-w-lg rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800 max-h-[90vh] overflow-y-auto">
      <div class="space-y-4">
        <h2 id="add-item-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">Add to the project</h2>
        <p class="text-sm text-gray-600 dark:text-gray-300">Everyone who can see this project can open what you add, including a file's download link.</p>
        <BaseSelect id="add-item-agent" v-model="pickerAgent" label="From agent">
          <option value="" disabled>Choose an agent…</option>
          <option v-for="a in agents" :key="a" :value="a">{{ a }}</option>
        </BaseSelect>
        <SkeletonLoader v-if="pickerAgent && !mine" :count="3" height="2.25rem" gap="0.5rem" />
        <template v-else-if="mine">
          <p v-if="!pickable.length" class="text-sm text-gray-600 dark:text-gray-300">Nothing of yours on {{ pickerAgent }} that isn't already on a project.</p>
          <ul class="max-h-72 divide-y divide-gray-100 overflow-y-auto dark:divide-gray-750">
            <li v-for="it in pickable" :key="`${it.kind}:${it.id}`" class="flex items-center gap-3 py-2 text-sm">
              <BaseBadge>{{ it.kind }}</BaseBadge>
              <span class="min-w-0 flex-1 truncate text-gray-900 dark:text-gray-100">{{ it.label }}</span>
              <BaseButton size="sm" variant="secondary" :loading="adding === it.id" @click="add(it)">Add</BaseButton>
            </li>
          </ul>
        </template>
        <InlineError v-if="pickerError" :message="pickerError" @dismiss="pickerError = ''" />
        <div class="flex justify-end"><BaseButton variant="secondary" @click="pickerOpen = false">Done</BaseButton></div>
      </div>
    </BaseModal>

    <!-- A report -->
    <BaseModal v-model="reportOpen" labelledby="report-view-title" panel-class="relative w-full max-w-3xl rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800 max-h-[90vh] overflow-y-auto">
      <div v-if="report" class="space-y-3">
        <h2 id="report-view-title" class="text-lg font-semibold text-gray-900 dark:text-gray-100">{{ report.title || 'Report' }}</h2>
        <ReportRenderer :report-type="report.report_type" :display-hint="report.display_hint" :payload="report.payload" :fallback-component="ReportSummary" />
      </div>
      <SkeletonLoader v-else :count="4" height="2.5rem" gap="0.5rem" />
    </BaseModal>
  </div>
</template>

<script setup>
import { computed, defineComponent, h, ref, watch } from 'vue'
import BaseBadge from '@/components/base/BaseBadge.vue'
import BaseButton from '@/components/base/BaseButton.vue'
import BaseModal from '@/components/base/BaseModal.vue'
import BaseSelect from '@/components/base/BaseSelect.vue'
import SkeletonLoader from '@/components/SkeletonLoader.vue'
import InlineError from '@/components/InlineError.vue'
import ReportRenderer from '@/components/reports/ReportRenderer.vue'
import ReportSummary from '@/components/reports/ReportSummary.vue'
import { useProjectsStore } from '@/stores/projects'
import { formatRelativeTime, formatLocalDateTime } from '@/utils/timestamps'
import { humanSize } from '@/components/portal/portalFiles'
import { projectErrorMessage } from './projectsUtils'

const props = defineProps({
  project: { type: Object, required: true },
  guest: { type: Boolean, default: false },
  myEmail: { type: String, default: '' },
  agents: { type: Array, default: () => [] },     // project agents this person can use
})

// A per-item overflow: share with guests (creator) and remove (whoever added it, or the creator).
const ItemMenu = defineComponent({
  props: { item: Object, kind: String, canShare: Boolean, canRemove: Boolean },
  emits: ['share', 'remove'],
  setup(p, { emit }) {
    return () => h('div', { class: 'flex items-center gap-1' }, [
      p.canShare ? h(BaseButton, { size: 'sm', variant: 'ghost', onClick: () => emit('share') },
        () => (p.item.audience === 'guests' ? 'Stop sharing' : 'Share with guests')) : null,
      p.canRemove ? h(BaseButton, { size: 'sm', variant: 'ghost', 'aria-label': `Remove ${p.kind} from the project`, onClick: () => emit('remove') },
        () => 'Remove') : null,
    ])
  },
})

const store = useProjectsStore()
const error = ref('')
const pickerOpen = ref(false)
const pickerAgent = ref('')
const mine = ref(null)
const pickerError = ref('')
const adding = ref('')
const reportOpen = ref(false)
const report = ref(null)

const files = computed(() => props.project.files || [])
const reports = computed(() => props.project.reports || [])
const decisions = computed(() => (props.guest ? [] : props.project.decisions || []))
const canContribute = computed(() => props.project.can?.contribute === true)
const archived = computed(() => Boolean(props.project.archived_at))
const canShare = computed(() => props.project.can?.manage_members === true && !archived.value)
const linked = computed(() => new Set([...files.value, ...reports.value, ...decisions.value].map((i) => i.id)))
const pickable = computed(() => {
  if (!mine.value) return []
  const out = []
  for (const f of mine.value.files || []) out.push({ kind: 'file', id: f.id, label: f.filename })
  for (const r of mine.value.reports || []) out.push({ kind: 'report', id: r.id, label: r.title || 'Untitled report' })
  for (const d of mine.value.decisions || []) out.push({ kind: 'decision', id: d.id, label: d.decided })
  return out.filter((i) => !linked.value.has(i.id))
})

function canRemove(item) {
  return !archived.value && (props.project.can?.manage_members === true || item.linked_by === props.myEmail)
}

function openPicker() {
  pickerAgent.value = props.agents.length === 1 ? props.agents[0] : ''
  mine.value = null
  pickerError.value = ''
  pickerOpen.value = true
}

watch(pickerAgent, async (agent) => {
  mine.value = null
  if (!agent) return
  try {
    mine.value = await store.myItems(agent)
  } catch (err) {
    pickerError.value = projectErrorMessage(err)
  }
}, { immediate: false })

async function add(it) {
  adding.value = it.id
  pickerError.value = ''
  try {
    await store.addItem(props.project.id, it.kind, it.id)
  } catch (err) {
    pickerError.value = projectErrorMessage(err)
  } finally {
    adding.value = ''
  }
}

async function toggleGuests(kind, item) {
  error.value = ''
  try {
    await store.setItemAudience(props.project.id, kind, item.id, item.audience === 'guests' ? 'members' : 'guests')
  } catch (err) {
    error.value = projectErrorMessage(err)
  }
}

async function remove(kind, item) {
  error.value = ''
  try {
    await store.removeItem(props.project.id, kind, item.id)
  } catch (err) {
    error.value = projectErrorMessage(err)
  }
}

async function openReport(r) {
  report.value = null
  reportOpen.value = true
  try {
    report.value = await store.fetchReport(props.project.id, r.id)
  } catch (err) {
    reportOpen.value = false
    error.value = projectErrorMessage(err)
  }
}
</script>
