<!--
  Activation checklist (ent#238, epic ent#54).

  The ambient half of onboarding: the setup wizard gets a user to a first agent,
  this keeps the thread from there to chat → schedule → channel. Four items,
  each derived server-side from verified state — nothing here tracks progress
  locally, so an item unticks if the agent behind it is deleted.

  Never a mandatory tour (the ent#54 design principle): it is dismissible, it
  gates nothing, and it hides itself the moment the last item is done. On an OSS
  or unentitled build the store's fetch 404/403s, `visible` stays false, and this
  renders nothing at all.
-->
<template>
  <div
    v-if="store.visible"
    data-testid="activation-checklist"
    class="mx-4 mt-3 rounded-lg border border-gray-200 dark:border-gray-700 bg-white dark:bg-gray-800 shadow-sm"
  >
    <div class="flex items-start justify-between px-4 py-3">
      <div class="min-w-0">
        <h3 class="text-sm font-medium text-gray-900 dark:text-gray-100">
          Getting started
        </h3>
        <p class="text-xs text-gray-500 dark:text-gray-400">
          {{ store.completedCount }} of {{ store.totalCount }} done — pick up where you left off.
        </p>
      </div>
      <button
        @click="store.dismiss()"
        data-testid="activation-checklist-dismiss"
        class="ml-3 p-1 text-gray-400 hover:text-gray-600 dark:hover:text-gray-200 rounded"
        title="Dismiss"
        aria-label="Dismiss the getting-started checklist"
      >
        <svg class="w-4 h-4" fill="none" stroke="currentColor" viewBox="0 0 24 24">
          <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M6 18L18 6M6 6l12 12" />
        </svg>
      </button>
    </div>

    <ul class="px-4 pb-3 space-y-2">
      <li
        v-for="item in store.items"
        :key="item.key"
        class="flex items-center gap-3 text-sm"
        :data-testid="`activation-item-${item.key}`"
        :data-done="item.done ? 'true' : 'false'"
      >
        <!-- Done marker. Filled check vs empty ring: the state must be readable
             without relying on the text styling alone. -->
        <span
          class="flex-none w-4 h-4 rounded-full flex items-center justify-center"
          :class="item.done
            ? 'bg-status-success-500 text-white'
            : 'border border-gray-300 dark:border-gray-600'"
        >
          <svg v-if="item.done" class="w-3 h-3" fill="none" stroke="currentColor" stroke-width="3" viewBox="0 0 24 24">
            <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7" />
          </svg>
        </span>

        <span
          class="min-w-0 flex-1 truncate"
          :class="item.done
            ? 'text-gray-400 dark:text-gray-500 line-through'
            : 'text-gray-800 dark:text-gray-200'"
          :title="item.description"
        >
          {{ item.title }}
        </span>

        <!-- Only the NEXT undone item carries an action, so the card reads as one
             step to take rather than a wall of four competing buttons. -->
        <button
          v-if="!item.done && item.key === nextKey"
          @click="go(item)"
          :data-testid="`activation-action-${item.key}`"
          class="flex-none text-xs font-medium px-2.5 py-1 rounded-md text-white bg-action-primary-600 hover:bg-action-primary-700"
        >
          {{ item.action_label }}
        </button>
      </li>
    </ul>
  </div>
</template>

<script setup>
import { computed, onMounted } from 'vue'
import { useRouter } from 'vue-router'
import { useOnboardingStore } from '../../stores/onboarding'

const store = useOnboardingStore()
const router = useRouter()

// The first undone item, in catalog order — the one step being asked for.
const nextKey = computed(() => store.items.find((i) => !i.done)?.key)

const go = (item) => {
  if (item.action_route) router.push(item.action_route)
}

onMounted(() => {
  store.fetchChecklist()
})
</script>
