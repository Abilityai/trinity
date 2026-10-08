<template>
  <!--
    The agent's Skills tab (trinity-enterprise#754): one place to see, run and
    configure everything the agent can do. It replaced the Playbooks tab (the
    agent's own skills, with Run) and the library-only Skills tab (assignment).

    Own skills — the agent's `.claude/skills/`, live; when the agent is stopped,
    its last-known list (never an empty tab), with Run disabled.
    Shared skills — the platform library's assignments and sets.

    Rules live in utils/skillCards.js; data in stores/skills.js and
    stores/skillGates.js. Everyone with access sees and runs; the owner or an
    admin also sees the approval row and the assignment controls.
  -->
  <div class="space-y-6" data-testid="skills-tab">
    <header>
      <div class="flex flex-wrap items-start justify-between gap-4">
        <div class="min-w-0">
          <h3 class="text-[18px] font-[650] text-gray-900 dark:text-gray-100">Skills</h3>
          <p class="mt-1 text-[12.5px] text-gray-600 dark:text-gray-300">
            Everything this agent can do: its own skills and the ones it uses from the shared library.
          </p>
        </div>
        <BaseInput
          v-model="filter"
          class="w-full sm:w-64"
          placeholder="Filter skills"
          aria-label="Filter skills"
          data-testid="skills-filter"
        />
      </div>
      <!-- The in-agent enforcement line (owner, gates exist): its footprint is
           reserved as soon as there are gates, so the probe landing later
           swaps text in place instead of pushing the grid down. -->
      <p
        v-if="enforcementSlot"
        class="mt-2 h-5 text-[12.5px] leading-5 truncate text-status-warning-700 dark:text-status-warning-400"
        :title="hookWarning || undefined"
        data-testid="skills-hook-warning"
      >{{ hookWarning }}</p>
    </header>

    <!-- ===================== Own skills ===================== -->
    <section data-testid="skills-own">
      <div class="flex items-baseline gap-2">
        <h4 class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">Own skills</h4>
        <span class="text-[12.5px] tabular-nums text-gray-500 dark:text-gray-400">{{ ownCount }}</span>
      </div>
      <p
        class="mt-0.5 h-5 text-[12.5px] leading-5 truncate"
        :class="ownBanner.tone === 'warn' ? 'text-status-warning-700 dark:text-status-warning-400' : 'text-gray-500 dark:text-gray-400'"
        :title="ownBanner.title || undefined"
        data-testid="skills-own-meta"
      >{{ ownBanner.text }}</p>

      <div class="mt-3">
        <SkeletonLoader v-if="ownView.state === 'loading'" :count="2" height="200px" gap="14px" />
        <LoadFailed
          v-else-if="ownView.state === 'failed'"
          title="Couldn't load this agent's skills"
          :message="store.agentListError || 'The request failed. Try again.'"
          @retry="store.loadAgentList()"
        />
        <p
          v-else-if="ownView.state === 'empty'"
          class="text-[12.5px] text-gray-600 dark:text-gray-300"
          data-testid="skills-own-empty"
        >{{ ownEmptyText }}</p>
        <div v-else class="grid gap-3.5 grid-cols-1 md:grid-cols-2 xl:grid-cols-3">
          <SkillCard
            v-for="card in ownCards"
            :key="card.id"
            :card="card"
            :show-owner-row="showOwnerRow"
            :approvers="gatesStore.approvers"
            :busy="!!(card.gateKey && gatesStore.busy[card.gateKey]) || (card.missing && !!gatesStore.busy[gateKeyOf(card)])"
            :running="runningId === card.id"
            :error="cardError(card)"
            @run="onRun(card)"
            @edit-run="onEditRun(card)"
            @set-gate="(kind) => onSetGate(card, kind)"
            @clear-gate="onClearGate(card)"
            @dismiss-error="dismissCardError(card)"
          />
        </div>
      </div>
    </section>

    <!-- ===================== Shared skills ===================== -->
    <section data-testid="skills-shared">
      <div class="flex flex-wrap items-center justify-between gap-2">
        <div class="flex items-baseline gap-2">
          <h4 class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">Shared skills</h4>
          <span class="text-[12.5px] tabular-nums text-gray-500 dark:text-gray-400">{{ store.assigned.length }}</span>
        </div>
        <div v-if="showManage && store.libraryStatus && store.libraryStatus.configured" class="flex items-center gap-1.5">
          <BaseButton size="sm" data-testid="skills-assign-open" @click="assignOpen = true">Assign skills</BaseButton>
          <BaseButton size="sm" variant="secondary" data-testid="skills-sets-open" @click="setsOpen = true">Manage sets</BaseButton>
          <BaseButton
            size="sm"
            :variant="syncNeedsAttention ? 'primary' : 'secondary'"
            :disabled="!running"
            :loading="store.injecting"
            loading-label="Syncing…"
            :title="running ? 'Re-copy every assigned skill into the agent' : 'The agent is stopped — start it to sync skills'"
            data-testid="skills-sync"
            @click="onSync"
          >Sync now</BaseButton>
        </div>
      </div>

      <!-- Sets, one line: what the agent holds as a family, each coloured by
           its honest status; the details and the set verbs are in the dialog. -->
      <div v-if="store.sets.length || store.setsError" class="mt-1 h-6 flex items-center gap-1.5 overflow-hidden" data-testid="skills-set-chips">
        <span class="flex-none text-[12.5px] text-gray-500 dark:text-gray-400">Sets</span>
        <!-- A failed read is named here, with its retry — the line must not
             simply vanish for a viewer who can't open the sets dialog's error. -->
        <template v-if="store.setsError">
          <span class="truncate text-[12.5px] text-status-warning-700 dark:text-status-warning-400" :title="store.setsError" data-testid="skills-sets-error">Couldn't read this agent's sets</span>
          <button
            type="button"
            class="flex-none text-[12.5px] text-action-primary-600 dark:text-action-primary-400 hover:underline"
            data-testid="skills-sets-retry"
            @click="store.loadSets()"
          >Retry</button>
        </template>
        <template v-else>
        <button
          v-for="set in visibleSets"
          :key="set.name"
          type="button"
          class="flex-none rounded-full focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
          :title="setTitle(set)"
          @click="setsOpen = true"
        >
          <BaseBadge :variant="setVariant(set)" :dot="setVariant(set) !== 'purple'">{{ set.name }}</BaseBadge>
        </button>
        <button
          v-if="hiddenSetCount"
          type="button"
          class="flex-none text-[12.5px] text-gray-600 dark:text-gray-300 hover:underline"
          @click="setsOpen = true"
        >+{{ hiddenSetCount }} more</button>
        <span
          v-if="setsNeedCredentials"
          class="truncate text-[12.5px] text-status-warning-700 dark:text-status-warning-400"
          data-testid="skills-sets-credentials"
        >Some sets need credentials</span>
        </template>
      </div>

      <!-- One line: the last verb's outcome, else when skills were last synced. -->
      <p class="mt-0.5 h-5 text-[12.5px] leading-5 truncate" :title="sharedLineTitle || undefined">
        <template v-if="notice && notice.text">
          <span data-testid="skills-saved-note" :data-tone="notice.tone" :class="noticeClass(notice.tone)">{{ notice.text }}</span>
          <span
            v-if="notice.deprecation"
            data-testid="skills-saved-deprecation"
            class="ml-2 text-status-warning-700 dark:text-status-warning-400"
          >{{ notice.deprecation }}</span>
        </template>
        <span v-else class="text-gray-500 dark:text-gray-400">{{ syncMeta }}</span>
      </p>

      <div class="mt-3">
        <SkeletonLoader v-if="sharedView.state === 'loading'" :count="1" height="240px" />
        <LoadFailed
          v-else-if="sharedView.state === 'failed'"
          title="Couldn't load the shared skills"
          :message="store.error || 'The request failed. Try again.'"
          @retry="store.load(agentName)"
        />
        <!-- Each empty state says what is wrong AND what to do next. -->
        <div
          v-else-if="store.emptyReason === 'library_unconfigured'"
          class="rounded-lg border border-gray-200 dark:border-gray-750 p-5 text-[12.5px]"
          data-testid="skills-library-unconfigured"
        >
          <p class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">No skills library is configured</p>
          <p class="mt-1 text-gray-600 dark:text-gray-300">
            Skills come from a git repository shared across the fleet. Once it's configured,
            every agent can be assigned skills from it.
          </p>
          <router-link v-if="isAdmin" to="/settings?tab=agents" class="mt-3 inline-block text-action-primary-600 dark:text-action-primary-400 hover:underline">
            Configure the library
          </router-link>
          <p v-else class="mt-3 text-gray-600 dark:text-gray-300">Ask an admin to configure it in Settings.</p>
        </div>
        <div
          v-else-if="store.emptyReason === 'library_empty'"
          class="rounded-lg border border-gray-200 dark:border-gray-750 p-5 text-[12.5px]"
        >
          <p class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">The library is configured but has no skills yet</p>
          <p class="mt-1 text-gray-600 dark:text-gray-300">Add a skill directory to the repository, then re-sync the library.</p>
        </div>
        <p
          v-else-if="store.emptyReason === 'none_assigned'"
          class="text-[12.5px] text-gray-600 dark:text-gray-300"
          data-testid="skills-shared-empty"
        >
          No shared skills yet.
          <template v-if="showManage">Use <span class="font-[550]">Assign skills</span> to pick some from the library.</template>
          <template v-else>The agent's owner can assign them from the library.</template>
        </p>
        <p v-else-if="!sharedCards.length" class="text-[12.5px] text-gray-600 dark:text-gray-300">No shared skill matches the filter.</p>
        <div v-else class="grid gap-3.5 grid-cols-1 md:grid-cols-2 xl:grid-cols-3">
          <SkillCard
            v-for="card in sharedCards"
            :key="card.id"
            :card="card"
            :show-owner-row="showOwnerRow"
            :approvers="gatesStore.approvers"
            :busy="!!(card.gateKey && gatesStore.busy[card.gateKey]) || store.saving"
            :running="runningId === card.id"
            :error="cardError(card)"
            @run="onRun(card)"
            @edit-run="onEditRun(card)"
            @set-gate="(kind) => onSetGate(card, kind)"
            @clear-gate="onClearGate(card)"
            @unassign="onUnassign(card)"
            @details="detailsCard = card"
            @dismiss-error="dismissCardError(card)"
          />
        </div>
      </div>
    </section>

    <SkillAssignModal v-model="assignOpen" :notice="notice" @saved="onSaved" />

    <BaseModal v-model="setsOpen" aria-label="Skill sets" panel-class="relative w-full max-w-2xl rounded-lg bg-white p-6 shadow-xl dark:bg-gray-800">
      <AgentSkillSets :can-manage="showManage" />
      <div class="mt-5 flex justify-end">
        <BaseButton variant="secondary" @click="setsOpen = false">Close</BaseButton>
      </div>
    </BaseModal>

    <SkillDetailsModal
      :model-value="!!detailsCard"
      :card="detailsCard"
      :can-manage="showManage"
      :busy="store.saving"
      @update:model-value="(open) => { if (!open) detailsCard = null }"
      @unassign="onUnassign(detailsCard, { fromConflict: true })"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseModal from '../base/BaseModal.vue'
import LoadFailed from '../LoadFailed.vue'
import SkeletonLoader from '../SkeletonLoader.vue'
import AgentSkillSets from './AgentSkillSets.vue'
import SkillAssignModal from './SkillAssignModal.vue'
import SkillCard from './SkillCard.vue'
import SkillDetailsModal from './SkillDetailsModal.vue'
import { useSkillsStore } from '../../stores/skills'
import { useSkillGatesStore } from '../../stores/skillGates'
import { useRole } from '../../composables/useRole'
import { buildSkillCards } from '../../utils/skillCards'
import { viewState } from '../../utils/loadingState'
import { apiErrorMessage } from '../../utils/apiError'
import { isGateRefusal, PENDING_NOTICE_TOAST } from '../../utils/skillGate'
import { formatLocalDateTime, formatRelativeTime } from '../../utils/timestamps'

const props = defineProps({
  agentName: { type: String, required: true },
  agentStatus: { type: String, default: 'stopped' },
  // The agent payload's `can_share` — the owner or an admin.
  canManage: { type: Boolean, default: false },
  isSystem: { type: Boolean, default: false },
  isEphemeral: { type: Boolean, default: false },
  // AgentDetail's toast host (`useNotification`).
  notify: { type: Function, default: null },
})

const emit = defineEmits(['run-with-instructions'])

const store = useSkillsStore()
const gatesStore = useSkillGatesStore()
const { isAdmin } = useRole()

const filter = ref('')
const runningId = ref(null)
const cardErrors = ref({})
const notice = ref(null)          // {text, tone, deprecation} — the last verb's outcome
const pendingSync = ref(false)    // #2703: a save that did not deliver — Sync is the repair
const assignOpen = ref(false)
const setsOpen = ref(false)
const detailsCard = ref(null)

const running = computed(() => props.agentStatus === 'running')
// The approval row: owner or admin, never on a ghost or the system agent.
const showOwnerRow = computed(() => props.canManage && !props.isSystem && !props.isEphemeral)
// Assignment controls: owner or admin, never on the system agent.
const showManage = computed(() => props.canManage && !props.isSystem)

const cards = computed(() => buildSkillCards({
  agentList: store.agentList,
  agentListState: store.agentListState,
  running: running.value,
  assigned: store.assigned,
  library: store.library,
  conflictNames: store.conflictNames,
  injectionResults: store.injectionResults,
  gates: gatesStore.gates,
  approvers: gatesStore.approvers,
  canManage: props.canManage,
  isSystem: props.isSystem,
  isEphemeral: props.isEphemeral,
}))

function matches(card) {
  const q = filter.value.trim().toLowerCase()
  if (!q) return true
  return card.name.toLowerCase().includes(q) || String(card.description || '').toLowerCase().includes(q)
}

const ownCards = computed(() => [...cards.value.own, ...cards.value.unmatchedGates].filter(matches))
const sharedCards = computed(() => cards.value.shared.filter(matches))
const ownCount = computed(() => cards.value.own.length)

const ownView = computed(() => {
  // "none" (stopped/unreachable, no kept copy) is a known state with nothing to
  // list — rendered as its own empty text, never as a failure.
  const view = viewState({
    hasLoaded: store.agentListLoaded,
    error: store.agentListError,
    count: store.agentListState === 'none' ? 0 : ownCards.value.length,
  })
  return view
})

const ownEmptyText = computed(() => {
  if (store.agentListState === 'none') {
    return running.value
      ? "The agent isn't answering right now; its own skills show here when it does."
      : 'Start the agent to see its own skills.'
  }
  if (filter.value.trim()) return 'No own skill matches the filter.'
  return 'This agent has no skills of its own yet. A skill is a folder with a SKILL.md under .claude/skills/ in its repository.'
})

const ownBanner = computed(() => {
  if (store.agentListState === 'last_known') {
    const when = formatRelativeTime(store.agentListAt)
    const title = `Listed ${formatLocalDateTime(store.agentListAt)}`
    if (store.agentListReason === 'unreachable') {
      return { tone: 'warn', text: `The agent isn't answering. Showing its skills as of ${when}.`, title }
    }
    return { tone: 'warn', text: `The agent is stopped. Start the agent to run. Showing its skills as of ${when}.`, title }
  }
  if (store.agentListState === 'live') {
    const paths = store.agentListPaths.length ? store.agentListPaths.join(', ') : '.claude/skills'
    return { tone: 'muted', text: `From ${paths}`, title: '' }
  }
  return { tone: 'muted', text: '', title: '' }
})

const sharedView = computed(() => viewState({
  hasLoaded: store.libraryStatus !== null,
  error: store.libraryStatus === null ? store.error : null,
  count: 1,   // the empty cases are named by store.emptyReason below
}))

// #2703: loud only when the button can act; a stopped agent gets the files at start.
const syncNeedsAttention = computed(() => pendingSync.value && running.value && !store.injecting)

const syncMeta = computed(() => {
  if (store.lastInjectionAt) return `Last sync ${new Date(store.lastInjectionAt).toLocaleString()}`
  if (store.assigned.length) return 'Not synced from this screen yet: skills are also copied in when the agent starts.'
  return ''
})
const sharedLineTitle = computed(() => (notice.value?.text
  ? [notice.value.text, notice.value.deprecation].filter(Boolean).join(' ')
  : syncMeta.value))

// ---- In-agent enforcement (ent#752 hook) ---------------------------------
const HOOK_WARNINGS = {
  unsupported_runtime: "This agent's runtime can't enforce gates inside the agent; requests that name a gated skill still wait for approval.",
  predates: "This agent's image predates the in-agent gate check; recreate the agent to enforce gates inside it.",
}
const enforcementSlot = computed(() => showOwnerRow.value && gatesStore.gates.length > 0)
const hookWarning = computed(() => {
  const h = gatesStore.hook
  if (!enforcementSlot.value || !h || h === 'ok' || h === 'unknown') return ''
  return HOOK_WARNINGS[h]
    || `The in-agent gate check isn't intact on this agent (${h}); recreate the agent to restore it.`
})

// ---- Sets chips -----------------------------------------------------------
const MAX_SET_CHIPS = 3
const visibleSets = computed(() => store.sets.slice(0, MAX_SET_CHIPS))
const hiddenSetCount = computed(() => Math.max(0, store.sets.length - MAX_SET_CHIPS))
function setNeedsCredentials(set) {
  return set?.prerequisites?.state === 'missing'
}
const setsNeedCredentials = computed(() => store.sets.some(setNeedsCredentials))
function setVariant(set) {
  if (set.status === 'ok' && !setNeedsCredentials(set)) return 'purple'
  return 'warning'
}
function setTitle(set) {
  if (setNeedsCredentials(set)) return `${set.name}: needs credentials`
  if (set.status === 'partial') return `${set.name}: partial — open for details`
  if (set.status === 'unresolved') return `${set.name}: can't be read right now`
  return `${set.name}: complete`
}

// ---- Verbs ----------------------------------------------------------------
function gateKeyOf(card) {
  return String(card.gateKey || card.gate?.skill_name || '').toLowerCase()
}
function cardError(card) {
  return cardErrors.value[card.id] || (gateKeyOf(card) && gatesStore.errors[gateKeyOf(card)]) || ''
}
function setCardError(card, message) {
  cardErrors.value = { ...cardErrors.value, [card.id]: message }
}
function dismissCardError(card) {
  setCardError(card, '')
  if (gateKeyOf(card)) gatesStore.dismissError(gateKeyOf(card))
}

async function onRun(card) {
  if (runningId.value) return
  runningId.value = card.id
  setCardError(card, '')
  try {
    const out = await store.runSkill(card.name)
    if (out.held) {
      // trinity#3274: a gated skill — nothing ran, an approval was raised.
      if (props.notify) props.notify(out.held, 'info', PENDING_NOTICE_TOAST)
      return
    }
    emit('run-with-instructions', `__NAVIGATE_TASKS__:${out.executionId || ''}`)
  } catch (e) {
    // A gate refusal names something to act on: the page's toast keeps an
    // error until it is dismissed (principle 18). Anything else stays on the card.
    if (isGateRefusal(e) && props.notify) {
      props.notify(apiErrorMessage(e), 'error')
      return
    }
    setCardError(card, apiErrorMessage(e, `Could not run /${card.name}`))
  } finally {
    runningId.value = null
  }
}

function onEditRun(card) {
  emit('run-with-instructions', `/${card.name} `)
}

async function onSetGate(card, kind) {
  if (!card.gateKey) return
  await gatesStore.setGate(card.gateKey, kind)
}

async function onClearGate(card) {
  const key = gateKeyOf(card)
  if (key) await gatesStore.clearGate(key)
}

async function onUnassign(card, { fromConflict = false } = {}) {
  if (!card) return
  notice.value = null
  const next = [...store.individualNames].filter((n) => n !== card.name)
  if (await store.saveAssignments(next)) {
    notice.value = {
      text: fromConflict ? `Unassigned ${card.name} — the agent's own skill stays.` : `Unassigned ${card.name}.`,
      tone: 'ok',
      deprecation: '',
    }
    detailsCard.value = null
    store.loadAgentList()
  } else {
    setCardError(card, store.error || `Could not unassign ${card.name}`)
  }
}

function onSaved(verdict) {
  notice.value = verdict
  pendingSync.value = Boolean(verdict.needsSync)
  store.loadAgentList()
}

async function onSync() {
  notice.value = null
  const result = await store.inject()
  if (result && !store.error) pendingSync.value = false
  else if (store.error) notice.value = { text: store.error, tone: 'bad', deprecation: '' }
  store.loadAgentList()
}

function noticeClass(tone) {
  if (tone === 'bad') return 'text-status-danger-700 dark:text-status-danger-400'
  if (tone === 'pending') return 'text-status-warning-700 dark:text-status-warning-400'
  return 'text-status-success-700 dark:text-status-success-400'
}

// ---- Loading --------------------------------------------------------------
function loadAll() {
  const name = props.agentName
  store.load(name)
  store.loadAgentList(name)
  gatesStore.load(name, { probe: showOwnerRow.value })
}

onMounted(loadAll)
watch(() => props.agentName, loadAll)

// Started or stopped: the own list switches between live and last-known, and
// the in-agent check can now (or no longer) answer.
watch(() => props.agentStatus, (status, prev) => {
  if (status === prev) return
  store.loadAgentList()
  if (showOwnerRow.value) gatesStore.probeHook(props.agentName)
})

// #2703: a skill was assigned / unassigned / synced on this agent (the thin
// `agent_skills_changed` trigger, ticked per agent in the store): re-read the
// agent's own list through the access-controlled route.
watch(() => store.changedAt[props.agentName], (tick, prev) => {
  if (tick && tick !== prev) store.loadAgentList()
})

// A set added or removed in the dialog delivers or drops skills.
watch(setsOpen, (open, was) => { if (was && !open) store.loadAgentList() })

onUnmounted(() => {
  store.clear()
  gatesStore.clear()
})
</script>
