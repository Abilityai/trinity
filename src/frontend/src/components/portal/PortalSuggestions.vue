<template>
  <!-- ent#465: suggestions computed for THIS viewer and THIS agent. Two
       placements read one store slice: the Info tab (full: heading, loading,
       failure, honest "nothing to suggest") and the empty chat (compact: renders
       only when there is something to say — no chrome over the hints). Every
       item names its evidence (`signal`), and Accept never sends anything:
       `prefill` fills the composer (ent#138), `link` opens the operator page. -->
  <section
    v-if="!compact || view.state === 'ready'"
    data-testid="portal-suggestions"
    :data-compact="compact || undefined"
    :class="compact ? 'mt-7 text-left' : ''"
  >
    <h2
      class="text-[11px] font-semibold uppercase tracking-wide mb-2"
      :class="compact ? 'text-gray-500 dark:text-gray-400' : 'text-gray-400'"
    >Suggested for you</h2>

    <div v-if="view.state === 'loading'" class="space-y-2" aria-busy="true">
      <div v-for="row in 2" :key="row" class="animate-pulse h-14 rounded-xl bg-gray-100 dark:bg-gray-800/60"></div>
      <span class="sr-only">Loading suggestions…</span>
    </div>

    <LoadFailed
      v-else-if="view.state === 'failed'"
      dense
      title="Couldn't load suggestions"
      :message="error"
      @retry="reload"
    />

    <template v-else>
      <p v-if="!compact && data.basis === 'capabilities_only' && hasCapabilityItems" class="mb-2 text-xs text-gray-500 dark:text-gray-400" data-testid="portal-suggestions-basis">
        You haven't talked to this agent yet — these are things it can do.
      </p>
      <p v-if="view.state === 'empty'" class="text-sm text-gray-500 dark:text-gray-400" data-testid="portal-suggestions-empty">
        Nothing to suggest right now.
      </p>
      <!-- Compact (the empty chat): one row per item inside one bordered list,
           so three suggestions cost about one hint card of height and the
           agent's identity and hints stay on screen. Full (the Info tab): a
           card per item, with the playbook's description. -->
      <ul
        v-else
        :class="compact
          ? 'rounded-xl border border-gray-200 dark:border-gray-800 divide-y divide-gray-100 dark:divide-gray-800'
          : 'space-y-2'"
      >
        <li
          v-for="s in items"
          :key="s.key"
          :class="compact
            ? 'flex flex-wrap items-center gap-x-3 gap-y-1 px-3 py-2'
            : 'rounded-xl border border-gray-200 dark:border-gray-800 px-3 py-2.5'"
          data-testid="portal-suggestion"
          :data-key="s.key"
        >
          <div :class="compact ? 'min-w-0 flex-1' : ''">
            <div class="text-sm font-medium" :class="{ truncate: compact }">{{ s.title }}</div>
            <div class="mt-0.5 text-xs text-gray-500 dark:text-gray-400" :class="{ truncate: compact }" data-testid="portal-suggestion-signal">{{ s.signal }}</div>
          </div>
          <div v-if="s.description && !compact" class="mt-0.5 text-xs text-gray-500 dark:text-gray-400">{{ s.description }}</div>
          <div class="flex items-center gap-2" :class="compact ? 'shrink-0' : 'mt-2'">
            <BaseButton size="sm" variant="secondary" data-testid="portal-suggestion-accept" @click="accept(s)">
              {{ acceptLabel(s) }}
            </BaseButton>
            <BaseButton
              size="sm"
              variant="ghost"
              data-testid="portal-suggestion-dismiss"
              :aria-label="`Dismiss: ${s.title}`"
              @click="dismiss(s)"
            >Dismiss</BaseButton>
          </div>
          <InlineError
            v-if="store.suggestionError && store.suggestionError.key === s.key"
            class="mt-2"
            :message="store.suggestionError.message"
            @dismiss="store.clearSuggestionError()"
          />
        </li>
      </ul>
      <p v-if="!compact && hiddenCount > 0" class="mt-2 text-xs text-gray-500 dark:text-gray-400">
        Showing {{ items.length }} of {{ data.total }}.
      </p>
      <p v-if="!compact && data.capabilities === 'unavailable'" class="mt-2 text-xs text-gray-500 dark:text-gray-400" data-testid="portal-suggestions-capabilities">
        Couldn't check what this agent can do right now.
      </p>
    </template>
  </section>
</template>

<script setup>
/**
 * Workspace suggestions (trinity-enterprise#465; requirement §5.39).
 * Platform sessions only — the parent gates the mount on
 * `store.isPlatformSession`, and the server answers a portal token with 404.
 */
import { computed, watch, onMounted } from 'vue'
import { useClientPortalStore } from '@/stores/clientPortal'
import BaseButton from '@/components/base/BaseButton.vue'
import InlineError from '@/components/InlineError.vue'
import LoadFailed from '@/components/LoadFailed.vue'
import { viewState } from '@/utils/loadingState'

const props = defineProps({
  agentName: { type: String, required: true },
  limit: { type: Number, default: 5 },
  compact: { type: Boolean, default: false },
})
const emit = defineEmits(['use-playbook', 'open-section', 'open-chat'])

const store = useClientPortalStore()

// The slice is a singleton keyed to one agent (the #2162 rule): read it only
// when it belongs to the agent on screen.
const mine = computed(() => store.suggestionsAgent === props.agentName)
const data = computed(() => (mine.value && store.suggestions) || { suggestions: [], total: 0 })
const items = computed(() => (data.value.suggestions || []).slice(0, props.limit))
// The "capabilities only" note is true only when a capability item is listed.
const hasCapabilityItems = computed(() => items.value.some((s) => s.source === 'capability'))
const hiddenCount = computed(() => Math.max(0, (data.value.total || 0) - items.value.length))
const error = computed(() => (mine.value ? store.suggestionsError : null))
const view = computed(() => viewState({
  hasLoaded: mine.value && store.suggestionsLoaded,
  error: error.value,
  count: items.value.length,
}))

const SECTION_LABELS = { asks: 'Show questions', decisions: 'Show decisions' }

function acceptLabel(s) {
  switch (s.action?.type) {
    case 'prefill': return 'Use'
    case 'open_section': return SECTION_LABELS[s.action.value] || 'Show'
    case 'open_chat': return 'Open chat'
    case 'link': return 'Open schedules'
    default: return 'Open'
  }
}

// Only operator agent paths are followed — the value is server-built, and this
// keeps a future class from turning Accept into an arbitrary navigation.
function safeOperatorPath(value) {
  return typeof value === 'string' && value.startsWith('/agents/') ? value : null
}

function accept(s) {
  store.acceptSuggestion(props.agentName, s.key)
  const { type, value } = s.action || {}
  if (type === 'prefill') emit('use-playbook', value)
  else if (type === 'open_section') emit('open-section', value)
  else if (type === 'open_chat') emit('open-chat')
  else if (type === 'link') {
    const path = safeOperatorPath(value)
    if (path && typeof window !== 'undefined') window.open(path, '_blank', 'noopener')
  }
}

function dismiss(s) {
  return store.dismissSuggestion(props.agentName, s.key)
}

function reload() {
  return store.loadAgentSuggestions(props.agentName, { force: true })
}

watch(() => props.agentName, (name) => { if (name) store.loadAgentSuggestions(name) })
onMounted(() => { store.loadAgentSuggestions(props.agentName) })
</script>
