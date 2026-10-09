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
        :title="hookWarning ? `${hookWarning} (${gatesStore.hook})` : undefined"
        data-testid="skills-hook-warning"
      >{{ hookWarning }}</p>
      <!-- The gate map could not be read: said to everyone (every card's gate
           line depends on it), with its retry; the toggles are held meanwhile. -->
      <p
        v-if="gatesStore.error && !gatesStore.hasLoaded"
        class="mt-2 h-5 flex items-center gap-2 text-[12.5px] leading-5 text-status-warning-700 dark:text-status-warning-400"
        data-testid="skills-gates-error"
      >
        <span class="truncate" :title="gatesStore.error">Couldn't read which skills need approval</span>
        <button
          type="button"
          class="flex-none text-action-primary-600 dark:text-action-primary-400 hover:underline"
          data-testid="skills-gates-retry"
          @click="gatesStore.load(agentName, { probe: showOwnerRow })"
        >Retry</button>
      </p>
    </header>

    <!-- ===================== Own skills ===================== -->
    <section data-testid="skills-own">
      <div class="flex items-baseline gap-2">
        <h4 class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">Own skills</h4>
        <span class="text-[12.5px] tabular-nums text-gray-500 dark:text-gray-400" data-testid="skills-own-count">{{ ownCount }}</span>
      </div>
      <p class="mt-0.5 h-5 flex items-center gap-2 min-w-0 text-[12.5px] leading-5" data-testid="skills-own-meta">
        <span
          class="truncate"
          :class="ownBanner.tone === 'warn' ? 'text-status-warning-700 dark:text-status-warning-400' : 'text-gray-500 dark:text-gray-400'"
          :title="ownBanner.title || undefined"
        >{{ ownBanner.text }}</span>
        <button
          v-if="ownView.stale"
          type="button"
          class="flex-none text-action-primary-600 dark:text-action-primary-400 hover:underline"
          data-testid="skills-own-refresh"
          @click="store.loadAgentList()"
        >Retry</button>
      </p>

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
        >
          {{ ownEmptyText }}
          <!-- A running agent can answer a moment later: nothing else re-asks
               (no poll, no event), so the next step is offered here. -->
          <button
            v-if="store.agentListState === 'none' && running"
            type="button"
            class="ml-1 text-action-primary-600 dark:text-action-primary-400 hover:underline"
            data-testid="skills-own-retry"
            @click="store.loadAgentList()"
          >Check again</button>
        </p>
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
      <div class="flex flex-wrap items-center justify-between gap-2" :class="showManage ? 'min-h-[28px]' : ''">
        <div class="flex items-baseline gap-2">
          <h4 class="text-[14px] font-[550] text-gray-900 dark:text-gray-100">Shared skills</h4>
          <span class="text-[12.5px] tabular-nums text-gray-500 dark:text-gray-400">{{ store.assigned.length }}</span>
        </div>
        <!-- Only once this agent's assignments are known: a draft built from
             an unanswered (or failed) read would save over the real list. -->
        <div v-if="showManage && store.sharedLoaded && store.libraryStatus?.configured" class="flex items-center gap-1.5">
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
        <span v-else class="text-gray-500 dark:text-gray-400" data-testid="skills-sync-meta">{{ syncMeta }}</span>
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
            @unassign="unassignCard = card"
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

    <!-- Principle 19: a one-click verb on a card restates its consequence
         (the gate goes too) and opens on Cancel. -->
    <ConfirmDialog
      :visible="!!unassignCard"
      :title="unassignCard ? `Unassign /${unassignCard.name}?` : ''"
      :message="unassignMessage"
      confirm-text="Unassign"
      variant="warning"
      @confirm="confirmUnassign"
      @cancel="unassignCard = null"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, onUnmounted, ref, watch } from 'vue'
import BaseBadge from '../base/BaseBadge.vue'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import BaseModal from '../base/BaseModal.vue'
import ConfirmDialog from '../ConfirmDialog.vue'
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
import { staleBannerMessage, viewState } from '../../utils/loadingState'
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
const unassignCard = ref(null)     // the card whose Unassign awaits its confirm

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
  assignmentsKnown: store.sharedLoaded,
  gatesKnown: gatesStore.hasLoaded,
}))

function matches(card) {
  const q = filter.value.trim().toLowerCase()
  if (!q) return true
  return card.name.toLowerCase().includes(q) || String(card.description || '').toLowerCase().includes(q)
}

const ownCards = computed(() => [...cards.value.own, ...cards.value.unmatchedGates].filter(matches))
const sharedCards = computed(() => cards.value.shared.filter(matches))
// What the section lists: own skills, and gates kept for skills no longer listed.
const ownCount = computed(() => cards.value.own.length + cards.value.unmatchedGates.length)

// A section draws once every read it is built from has answered — or failed,
// which is a known state too. Own needs the assignments (which skills Shared
// renders instead) and the gate map (every card's gate line and toggle);
// Shared needs the same map. So a skill never moves between sections, and no
// toggle shows "off" before the gates are known.
const gatesKnown = computed(() => gatesStore.hasLoaded || !!gatesStore.error)
const sharedKnown = computed(() => store.sharedLoaded || (!!store.error && !store.loading))

const ownView = computed(() => {
  // "none" (stopped/unreachable, no kept copy) is a known state with nothing to
  // list — rendered as its own empty text, never as a failure.
  return viewState({
    hasLoaded: store.agentListLoaded && sharedKnown.value && gatesKnown.value,
    error: store.agentListError,
    count: store.agentListState === 'none' ? 0 : ownCards.value.length,
  })
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
  if (ownView.value.stale) {
    return { tone: 'warn', text: staleBannerMessage("this agent's skills", null), title: store.agentListError || '' }
  }
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
  hasLoaded: store.sharedLoaded && gatesKnown.value,
  error: store.sharedLoaded ? null : store.error,
  count: 1,   // the empty cases are named by store.emptyReason below
}))

// #2703: loud only when the button can act; a stopped agent gets the files at start.
const syncNeedsAttention = computed(() => pendingSync.value && running.value && !store.injecting)

const syncMeta = computed(() => {
  if (store.lastInjectionAt) return `Last sync ${new Date(store.lastInjectionAt).toLocaleString()}`
  if (store.assigned.length) {
    return 'Not synced from this screen yet: statuses appear after a sync. Skills are also copied in when the agent starts.'
  }
  return ''
})
const sharedLineTitle = computed(() => (notice.value?.text
  ? [notice.value.text, notice.value.deprecation].filter(Boolean).join(' ')
  : syncMeta.value))

// ---- In-agent enforcement (ent#752 hook) ---------------------------------
// Every state the agent reports, in words (the raw code is on hover only).
const HOOK_WARNINGS = {
  missing: "This agent's image has no in-agent gate check; rebuild the base image and recreate the agent to enforce gates inside it.",
  not_root_owned: "The in-agent gate check on this agent isn't protected; recreate the agent to restore it.",
  writable: 'The in-agent gate check on this agent can be changed from inside it; recreate the agent to restore it.',
  unsupported_runtime: "This agent's runtime can't enforce gates inside the agent; requests that name a gated skill still wait for approval.",
  predates: "This agent's image predates the in-agent gate check; recreate the agent to enforce gates inside it.",
}
const enforcementSlot = computed(() => showOwnerRow.value && gatesStore.gates.length > 0)
const hookWarning = computed(() => {
  const h = gatesStore.hook
  if (!enforcementSlot.value || !h || h === 'ok' || h === 'unknown') return ''
  return HOOK_WARNINGS[h]
    || "The in-agent gate check isn't intact on this agent; recreate the agent to restore it."
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
  const agent = props.agentName
  runningId.value = card.id
  setCardError(card, '')
  try {
    const out = await store.runSkill(card.name)
    // The page moved to another agent meanwhile: this answer is the previous
    // agent's run, so it neither opens this agent's Tasks nor toasts here.
    if (props.agentName !== agent) return
    if (out.held) {
      // trinity#3274: a gated skill — nothing ran, an approval was raised.
      if (props.notify) props.notify(out.held, 'info', PENDING_NOTICE_TOAST)
      return
    }
    emit('run-with-instructions', `__NAVIGATE_TASKS__:${out.executionId || ''}`)
  } catch (e) {
    if (props.agentName !== agent) return
    // A gate refusal names something to act on: the page's toast keeps an
    // error until it is dismissed (principle 18). Anything else stays on the card.
    if (isGateRefusal(e) && props.notify) {
      props.notify(apiErrorMessage(e), 'error')
      return
    }
    setCardError(card, apiErrorMessage(e, `Could not run /${card.name}`))
  } finally {
    if (props.agentName === agent) runningId.value = null
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
  const agent = props.agentName
  notice.value = null
  const next = [...store.individualNames].filter((n) => n !== card.name)
  const saved = await store.saveAssignments(next)
  if (props.agentName !== agent || saved === null) return
  if (saved) {
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

const unassignMessage = computed(() => {
  const c = unassignCard.value
  if (!c) return ''
  // Said before anything happens, so in the future tense.
  const gate = c.gate ? ', along with its approval requirement' : ''
  return `The library skill will be removed from this agent${gate}. You can assign it again from Assign skills.`
})

function confirmUnassign() {
  const c = unassignCard.value
  unassignCard.value = null
  onUnassign(c)
}

function onSaved(verdict) {
  notice.value = verdict
  pendingSync.value = Boolean(verdict.needsSync)
  store.loadAgentList()
}

async function onSync() {
  const agent = props.agentName
  notice.value = null
  const result = await store.inject()
  if (props.agentName !== agent) return
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

// The tab outlives an agent switch (AgentDetail is KeepAlive'd and Skills is
// open to everyone), so nothing from the previous agent's verbs carries over:
// its outcome line, card errors, a pending sync, an open dialog, a run still
// starting, the filter.
function resetView() {
  filter.value = ''
  runningId.value = null
  cardErrors.value = {}
  notice.value = null
  pendingSync.value = false
  assignOpen.value = false
  setsOpen.value = false
  detailsCard.value = null
  unassignCard.value = null
}

onMounted(loadAll)
// One watcher for the agent and its state, so a switch to an agent in another
// state is one load: one list read, one probe (PR review).
watch([() => props.agentName, () => props.agentStatus], ([name, status], [prevName, prevStatus]) => {
  if (name !== prevName) {
    resetView()
    loadAll()
    return
  }
  // Started or stopped: the own list switches between live and last-known, and
  // the in-agent check can now (or no longer) answer.
  if (status !== prevStatus) {
    store.loadAgentList()
    if (showOwnerRow.value) gatesStore.probeHook(name)
  }
})

// #2703: a skill was assigned / unassigned / synced on this agent (the thin
// `agent_skills_changed` trigger, ticked per agent in the store): re-read the
// agent's own list through the access-controlled route. A switch is the
// watcher above's: the next agent's own tick is not a change.
watch([() => props.agentName, () => store.changedAt[props.agentName]], ([name, tick], [prevName, prevTick]) => {
  if (name === prevName && tick && tick !== prevTick) store.loadAgentList()
})

// A set added or removed in the dialog delivers or drops skills.
watch(setsOpen, (open, was) => { if (was && !open) store.loadAgentList() })

onUnmounted(() => {
  store.clear()
  gatesStore.clear()
})
</script>
