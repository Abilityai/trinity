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
  <!-- A section of the systems sidebar, under its view labels. -->
  <div
    v-if="store.visible"
    data-testid="activation-checklist"
    class="mt-2 pt-2 border-t border-gray-200 dark:border-gray-700"
  >
    <!--
      The header is a disclosure, NOT a dismiss. Collapsing is "not now" and is
      remembered per browser (expanded on first start); retiring the checklist
      for good is its own labelled control below, which says so — there is no
      un-dismiss anywhere in the store or the UI.
    -->
    <button
      type="button"
      @click="toggleExpanded"
      data-testid="activation-checklist-toggle"
      :aria-expanded="expanded ? 'true' : 'false'"
      class="w-full flex items-center px-3 py-1.5 text-left rounded
             focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
    >
      <span class="flex-1 min-w-0 truncate text-xs font-semibold uppercase tracking-wider text-gray-500 dark:text-gray-400">
        Getting started
      </span>
      <span class="ml-2 text-xs tabular-nums text-gray-500 dark:text-gray-400">
        {{ store.completedCount }}/{{ store.totalCount }}
      </span>
      <svg
        class="ml-1 w-3.5 h-3.5 text-gray-400 transition-transform motion-reduce:transition-none"
        :class="expanded ? 'rotate-180' : ''"
        fill="none" stroke="currentColor" viewBox="0 0 24 24" aria-hidden="true"
      >
        <path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M19 9l-7 7-7-7" />
      </svg>
    </button>

    <template v-if="expanded">
      <ul class="px-3 pt-1 pb-2 space-y-2">
        <li
          v-for="item in store.items"
          :key="item.key"
          class="flex items-start gap-2 text-sm"
          :data-testid="`activation-item-${item.key}`"
          :data-done="item.done ? 'true' : 'false'"
        >
          <!-- Done marker. Filled check vs empty ring: the state must be readable
               without relying on the text styling alone. -->
          <span
            class="flex-none mt-0.5 w-4 h-4 rounded-full flex items-center justify-center"
            :class="item.done
              ? 'bg-status-success-500 text-white'
              : 'border border-gray-300 dark:border-gray-600'"
          >
            <svg v-if="item.done" class="w-3 h-3" fill="none" stroke="currentColor" stroke-width="3" viewBox="0 0 24 24">
              <path stroke-linecap="round" stroke-linejoin="round" d="M5 13l4 4L19 7" />
            </svg>
          </span>

          <div class="min-w-0 flex-1">
            <div
              class="truncate"
              :class="item.done
                ? 'text-gray-400 dark:text-gray-500 line-through'
                : 'text-gray-800 dark:text-gray-200'"
              :title="item.description"
            >
              {{ item.title }}
            </div>

            <!-- Only the NEXT undone item carries an action, so the list reads as
                 one step to take rather than a wall of four competing buttons.
                 Under the title, not beside it: the rail is too narrow for both. -->
            <button
              v-if="!item.done && item.key === nextKey"
              @click="go(item)"
              :data-testid="`activation-action-${item.key}`"
              class="mt-1 text-xs font-medium px-2.5 py-1 rounded-md text-white bg-action-primary-600 hover:bg-action-primary-700"
            >
              {{ item.action_label }}
            </button>
          </div>
        </li>
      </ul>

      <!--
        The permanent one. It names its consequence rather than relying on an
        icon, because it cannot be undone — nothing in the store or the UI
        restores a dismissed checklist, so a user who meant "later" and got
        "never" has no recovery. Quiet by default: this is the exit, not the
        action the list is asking for.
      -->
      <div class="px-3 pb-1">
        <button
          @click="store.dismiss()"
          data-testid="activation-checklist-dismiss"
          class="text-xs text-gray-500 dark:text-gray-400 hover:text-gray-700 dark:hover:text-gray-200
                 rounded focus-visible:outline-none focus-visible:ring-2 focus-visible:ring-action-primary-500/40"
          title="Hides these steps for good — they do not come back"
        >
          Don&rsquo;t show this again
        </button>
      </div>
    </template>
  </div>
</template>

<script setup>
import { computed, onMounted, ref } from 'vue'
import { useRouter } from 'vue-router'
import { useOnboardingStore } from '../../stores/onboarding'

const store = useOnboardingStore()
const router = useRouter()

// Expanded on first start; a saved preference wins after that.
const EXPANDED_KEY = 'trinity-checklist-expanded'
const expanded = ref(localStorage.getItem(EXPANDED_KEY) !== 'false')

function toggleExpanded() {
  expanded.value = !expanded.value
  localStorage.setItem(EXPANDED_KEY, String(expanded.value))
}

// The first undone item, in catalog order — the one step being asked for.
const nextKey = computed(() => store.items.find((i) => !i.done)?.key)

const go = (item) => {
  if (item.action_route) router.push(item.action_route)
}

onMounted(() => {
  store.fetchChecklist()
})
</script>
