<template>
  <!--
    Search what this agent remembers (trinity-enterprise#1028). The owner's
    read over the same ranked store the agent's prompt is fed from — a query
    comes back as the facts most relevant to it, each with the (date, source)
    line the model sees and the relevance the gate saw.

    One footprint, four faces (principle 4/15): idle before any search,
    skeleton while a query has no data yet, the empty copy only after a fetch
    that SUCCEEDED and returned nothing, the bounded list otherwise. A failed
    search is a failed verb: InlineError beside the box, earlier hits stay
    (principle 18, #1926). A re-search keeps the previous hits on screen
    rather than re-flashing the skeleton.
  -->
  <div data-testid="retrieval-memory-search">
    <p class="text-sm font-medium text-gray-900 dark:text-gray-100">Search memory</p>
    <p class="mt-0.5 mb-3 text-[12.5px] text-gray-600 dark:text-gray-400">
      What the agent would be reminded of for a given question, and where each fact came from.
    </p>

    <form class="flex items-start gap-2" @submit.prevent="run">
      <BaseInput
        v-model="query"
        class="min-w-0 flex-1"
        placeholder="Search what this agent remembers…"
        aria-label="Search memory"
        :disabled="inFlight"
        data-testid="retrieval-memory-search-input"
        @keydown.enter.prevent="run"
      />
      <BaseButton
        type="submit"
        variant="secondary"
        size="sm"
        class="mt-px"
        :loading="inFlight"
        loading-label="Searching…"
        data-testid="retrieval-memory-search-submit"
      >
        Search
      </BaseButton>
    </form>

    <InlineError
      v-if="error"
      class="mt-2"
      :message="error"
      retryable
      retry-label="Try again"
      data-testid="retrieval-memory-search-error"
      @retry="run"
      @dismiss="error = ''"
    />

    <!-- The results region reserves its height whatever the face, so the box
         above stays put when hits arrive (principle 4/29). -->
    <div class="mt-3 min-h-[96px]">
      <p
        v-if="view.state === 'idle'"
        class="text-[12.5px] text-gray-600 dark:text-gray-400"
        data-testid="retrieval-memory-search-idle"
      >
        Type a question or a name, a deadline, a decision — the facts most relevant to it come back with when and where they came from.
      </p>

      <SkeletonLoader
        v-else-if="view.state === 'loading'"
        variant="rows"
        :count="3"
        height="28px"
        gap="8px"
      />

      <p
        v-else-if="view.state === 'empty'"
        class="text-[12.5px] text-gray-600 dark:text-gray-400"
        data-testid="retrieval-memory-search-empty"
      >
        Nothing matched. Try other words, or a name or date you expect to be remembered.
      </p>

      <template v-else>
        <p class="mb-2 text-[12.5px] text-gray-600 dark:text-gray-400 tabular-nums">
          {{ hits.length }} fact{{ hits.length === 1 ? '' : 's' }} · most relevant first
          <span v-if="view.stale"> · showing the last result that loaded</span>
        </p>
        <ul
          class="max-h-72 overflow-y-auto divide-y divide-gray-200 dark:divide-gray-750 rounded-md border border-gray-200 dark:border-gray-750"
          aria-label="Matching facts"
        >
          <li
            v-for="hit in hits"
            :key="hit.id"
            class="flex items-start gap-3 px-3 py-2"
            data-testid="retrieval-memory-search-hit"
          >
            <div class="min-w-0 flex-1">
              <p class="text-sm text-gray-900 dark:text-gray-100 break-words">{{ hit.text }}</p>
              <p class="mt-0.5 text-[12.5px] text-gray-600 dark:text-gray-400">{{ meta(hit) }}</p>
            </div>
            <span
              class="flex-none text-[12.5px] tabular-nums text-gray-600 dark:text-gray-400"
              :title="`Relevance ${Math.round((hit.relevance || 0) * 100)}%`"
            >{{ Math.round((hit.relevance || 0) * 100) }}%</span>
          </li>
        </ul>
      </template>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import api from '../../api'
import BaseButton from '../base/BaseButton.vue'
import BaseInput from '../base/BaseInput.vue'
import InlineError from '../InlineError.vue'
import SkeletonLoader from '../SkeletonLoader.vue'
import { viewState } from '../../utils/loadingState'

const props = defineProps({
  agentName: { type: String, required: true },
  notify: { type: Function, default: null },
})

const K = 20

const query = ref('')
const hits = ref([])
const inFlight = ref(false)
const hasSearched = ref(false)   // a search has been sent at least once
const hasLoaded = ref(false)     // a search has SUCCEEDED at least once
const error = ref('')

// Loading means "no data yet for a query that was sent" — never the in-flight
// flag (#1927). Before the first search the region is idle, not loading.
const view = computed(() => {
  if (!hasSearched.value) return { state: 'idle', stale: false }
  return viewState({ loading: inFlight.value, hasLoaded: hasLoaded.value, error: error.value, count: hits.value.length })
})

function meta(hit) {
  const line = String(hit.line || '')
  const m = line.match(/^\((.*?)\)\s/)
  if (m) return m[1]
  const day = String(hit.updated_at || hit.created_at || '').slice(0, 10)
  return [day, hit.source].filter(Boolean).join(', ')
}

async function run() {
  const q = query.value.trim()
  if (!q || inFlight.value) return
  inFlight.value = true
  hasSearched.value = true
  error.value = ''
  try {
    const { data } = await api.get(`/api/agents/${props.agentName}/retrieval-memory/search`, { params: { q, k: K } })
    hits.value = Array.isArray(data) ? data : []
    hasLoaded.value = true
  } catch (e) {
    const detail = e?.response?.data?.detail
    error.value = (detail && typeof detail === 'object' && detail.message)
      ? detail.message
      : 'Could not search this agent\'s memory. Check the connection and try again.'
  } finally {
    inFlight.value = false
  }
}
</script>
