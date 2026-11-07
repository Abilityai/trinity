<template>
  <!--
    Person memory (trinity-enterprise#962): everyone this agent keeps memory
    about — notes, a conversation summary, a write history, or retrieval-memory
    facts in their own scope — newest change first. Opening one shows it in
    PersonMemoryModal. The owner's (and an admin's) view; the routes refuse
    everyone else, so this block only ever renders for them.

    One footprint, four faces (principles 4/15): a skeleton until the first
    read lands, LoadFailed for a failed first read, the empty copy only after
    a read that SUCCEEDED with nobody, the bounded list otherwise.
  -->
  <div data-testid="person-memory-section">
    <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Person memory</p>
    <p class="mt-0.5 mb-3 text-[12.5px] text-gray-600 dark:text-gray-400">
      What this agent remembers about each person it talks to, what changed it, and Undo.
    </p>

    <div class="min-h-[96px]">
      <SkeletonLoader v-if="view.state === 'loading'" variant="rows" :count="3" height="40px" gap="8px" />

      <LoadFailed
        v-else-if="view.state === 'failed'"
        dense
        title="Couldn't load person memory"
        :message="store.peopleError"
        data-testid="person-memory-failed"
        @retry="load"
      />

      <div v-else-if="view.state === 'empty'" data-testid="person-memory-empty">
        <p class="text-sm text-gray-900 dark:text-gray-100">No one has memory with this agent yet.</p>
        <p class="mt-0.5 text-[12.5px] text-gray-600 dark:text-gray-400">
          A person appears here once the agent writes notes about them, summarizes a conversation with them, or keeps a fact from one.
        </p>
      </div>

      <template v-else>
        <p class="mb-2 text-[12.5px] tabular-nums text-gray-600 dark:text-gray-400" data-testid="person-memory-total">
          {{ store.peopleTotal }} {{ store.peopleTotal === 1 ? 'person' : 'people' }} · newest change first<template v-if="store.people.length < store.peopleTotal"> · {{ store.people.length }} shown</template>
        </p>
        <ul
          class="max-h-80 overflow-y-auto divide-y divide-gray-200 dark:divide-gray-750 rounded-md border border-gray-200 dark:border-gray-750"
          aria-label="People with memory"
        >
          <li v-for="p in store.people" :key="p.user_email">
            <button
              type="button"
              class="flex w-full items-start gap-3 px-3 py-2 text-left hover:bg-gray-50 focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40 dark:hover:bg-gray-750/60 dark:focus-visible:ring-action-primary-400/40"
              data-testid="person-memory-person"
              @click="open(p.user_email)"
            >
              <span class="min-w-0 flex-1">
                <span class="block truncate text-sm text-gray-900 dark:text-gray-100">{{ p.user_email }}</span>
                <span v-if="identityLine(p)" class="block truncate text-[12.5px] text-gray-600 dark:text-gray-400">{{ identityLine(p) }}</span>
                <span class="block text-[12.5px] text-gray-600 dark:text-gray-400 tabular-nums">{{ countsLine(p) }}</span>
              </span>
              <span
                v-if="p.last_changed_at"
                class="flex-none whitespace-nowrap text-[12.5px] text-gray-600 dark:text-gray-400"
                :title="formatLocalDateTime(p.last_changed_at)"
              >{{ formatRelativeTime(p.last_changed_at) }}</span>
            </button>
          </li>
        </ul>
        <div v-if="store.people.length < store.peopleTotal" class="mt-2">
          <BaseButton
            variant="secondary"
            size="sm"
            :loading="store.peopleLoadingMore"
            loading-label="Loading…"
            data-testid="person-memory-more"
            @click="store.loadMorePeople()"
          >Load more</BaseButton>
        </div>
        <InlineError
          v-if="store.peopleMoreError"
          class="mt-2"
          :message="store.peopleMoreError"
          @dismiss="store.peopleMoreError = ''"
        />
      </template>
    </div>

    <PersonMemoryModal
      v-model="modalOpen"
      :agent-name="agentName"
      :notify="notify"
    />
  </div>
</template>

<script setup>
import { computed, onMounted, ref, watch } from 'vue'
import { usePersonMemoryStore } from '../../stores/personMemory'
import { viewState } from '../../utils/loadingState'
import { formatLocalDateTime, formatRelativeTime } from '../../utils/timestamps'
import BaseButton from '../base/BaseButton.vue'
import InlineError from '../InlineError.vue'
import LoadFailed from '../LoadFailed.vue'
import SkeletonLoader from '../SkeletonLoader.vue'
import PersonMemoryModal from './PersonMemoryModal.vue'

const props = defineProps({
  agentName: { type: String, required: true },
  notify: { type: Function, default: null },
})

const store = usePersonMemoryStore()
const modalOpen = ref(false)

// The store is shared across agents (AgentDetail is KeepAlive'd); only this
// agent's rows count as loaded here.
const mine = computed(() => store.agentName === props.agentName)
const view = computed(() => viewState({
  hasLoaded: mine.value && store.peopleLoaded,
  error: mine.value ? store.peopleError : '',
  count: mine.value ? store.people.length : 0,
}))

const CHANNELS = { telegram: 'Telegram', slack: 'Slack', whatsapp: 'WhatsApp' }

/** The channel identity an access request captured — as the channel REPORTED
 *  it; the verified email above it is the identity. */
function identityLine(p) {
  if (!p.channel) return ''
  const parts = [CHANNELS[p.channel] || p.channel]
  if (p.channel_handle) parts.push(`@${String(p.channel_handle).replace(/^@/, '')}`)
  if (p.channel_name) parts.push(p.channel_name)
  return parts.join(' · ')
}

function countsLine(p) {
  const parts = []
  if (p.has_notes) parts.push('Notes')
  if (p.has_summary) parts.push('Summary')
  if (p.write_count) parts.push(`${p.write_count} change${p.write_count === 1 ? '' : 's'}`)
  if (p.agent_scope) parts.push('Agent owner')
  else if (p.fact_count) parts.push(`${p.fact_count} fact${p.fact_count === 1 ? '' : 's'}`)
  return parts.join(' · ') || 'No memory left'
}

function load() {
  return store.loadPeople(props.agentName)
}

function open(email) {
  store.openPerson(email)
  modalOpen.value = true
}

watch(() => props.agentName, () => {
  modalOpen.value = false
  load()
})
watch(modalOpen, (isOpen) => {
  if (!isOpen) store.closePerson()
})
onMounted(load)
</script>
