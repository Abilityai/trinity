<template>
  <div id="portal-briefing" class="py-8 sm:py-12 text-center">
    <PortalAvatar :name="agent.name" :avatar-url="agent.avatar_url" :size="64" class="mx-auto" />
    <h1 class="mt-4 text-xl font-semibold tracking-tight">{{ agent.name }}</h1>
    <!-- #2163: the briefing is hydrated AFTER the roster (it used to ship with
         it, which is what made the Workspace's first paint wait for the slowest
         agent in the fleet). So this zone shows a placeholder keyed on the
         card's own `briefing_state` — "no data yet", never a fetch-in-flight
         flag. #2540: the placeholder is a SKELETON of the hint zone, not the
         scanline beam — that motion is for charts; this is a list.

         Avatar and name stay OUTSIDE it: they come from the roster and are
         already correct, and a placeholder over an identity that is not
         loading would be motion about nothing.

         The wrapper owns the footprint: `min-h` reserves the header plus one
         hint row for BOTH the skeleton and the loaded zone, so the common
         shapes (no hints; a description and a row) do not jump on arrival. A
         taller grid grows DOWNWARD into the empty pane below — the composer is
         pinned to the bottom of the flex column, so nothing visible moves.

         `zone.state`, not `zone.loading`: a bare `<x>.loading` gate is what
         the #1927 ratchet counts. -->
    <div class="mt-1.5 mx-auto max-w-2xl min-h-[6.5rem]">
    <PortalSkeleton v-if="zone.state === 'pending'" variant="briefing" />
    <template v-else>
    <p v-if="agent.description" class="mx-auto max-w-lg text-sm text-gray-500 dark:text-gray-400">
      {{ agent.description }}
    </p>
    <!-- ONE element, two sentences. A failed hydration must say so — an agent
         whose briefing could not be fetched is not an agent with nothing to
         offer (ent#380's "never silently empty", and the same "looks complete"
         class `playbooks_total` guards one tier over). Reusing the element
         rather than adding a second keeps the raw-gray count flat. -->
    <p v-else class="text-sm text-gray-400">{{ fallbackLine }}</p>

    <!-- Capability hints as clickable cards (pre-fill the composer, no auto-run).
         Exposed playbooks when the operator curated a set; the template's
         "What You Can Ask" use-cases otherwise (ent#380 — backend ladder).
         #2101: bounded — described-first order, first 6 collapsed, counted toggle
         expands IN PLACE (no nested scroll region: the chat pane stays the single
         scroll axis, and the set itself is belted server-side at ≤24). -->
    <div v-if="playbooks.length" class="mt-7">
      <div class="text-[11px] font-semibold uppercase tracking-wide text-gray-400 mb-2 text-left">Things you can ask</div>
      <div class="grid grid-cols-1 sm:grid-cols-2 gap-2.5">
        <button
          v-for="(p, i) in hintPlan.visible"
          :key="i"
          class="text-left rounded-xl border border-gray-200 dark:border-gray-800 bg-white dark:bg-gray-900 p-3.5 hover:border-action-primary-400 dark:hover:border-action-primary-600 hover:shadow-sm transition"
          @click="$emit('use-playbook', starterFor(p))"
        >
          <div class="flex items-start gap-2">
            <svg class="w-4 h-4 mt-0.5 text-action-primary-500 shrink-0" fill="none" viewBox="0 0 24 24" stroke="currentColor"><path stroke-linecap="round" stroke-linejoin="round" stroke-width="2" d="M13 10V3L4 14h7v7l9-11h-7z" /></svg>
            <div class="min-w-0">
              <!-- ent#380: hints can be full-sentence use-case prompts, not just
                   short playbook names — wrap to two lines instead of clipping. -->
              <div class="text-sm font-medium line-clamp-2 break-words">{{ p.title }}</div>
              <div v-if="p.description" class="text-xs text-gray-500 dark:text-gray-400 mt-0.5 line-clamp-2">{{ p.description }}</div>
            </div>
          </div>
        </button>
      </div>
      <!-- #2101: ONE persistent toggle element (never two v-if-alternated buttons —
           that would drop keyboard focus on collapse). Sits outside the grid so the
           collapsed footprint never changes with the toggle's label. Instant, no
           animation. The count is the shipped list's, and the label never claims
           the agent's full skill set (the server belt may have trimmed it). -->
      <button
        v-if="hintPlan.collapsible"
        type="button"
        class="mt-2.5 text-xs font-medium text-action-primary-600 dark:text-action-primary-400 hover:underline"
        :aria-expanded="expanded"
        @click="expanded = !expanded"
      >
        {{ expanded ? 'Show fewer' : `Show all ${hintPlan.total}` }}
      </button>
    </div>
    </template>
    </div>
  </div>
</template>

<script setup>
import { computed, ref } from 'vue'
import PortalAvatar from './PortalAvatar.vue'
import PortalSkeleton from './PortalSkeleton.vue'
import { planHintDisplay, starterFor } from './portalUtils'
import { briefingZone } from './portalBriefingState'

const props = defineProps({
  agent: { type: Object, required: true },
})
defineEmits(['use-playbook'])

const playbooks = computed(() => props.agent.playbooks || [])

// #2163 — presentational: the fetch is driven by `Portal.vue`'s active-agent
// watcher, not from here. This component renders only in the conversation's
// `#empty` slot, so a deep link into an EXISTING thread never mounts it — a
// hydration call owned here would never fire for those agents, and their `/`
// typeahead reads the same `playbooks`.
const zone = computed(() => briefingZone(props.agent))
const fallbackLine = computed(() =>
  zone.value.unavailable
    ? "Couldn't load suggestions for this agent right now."
    : 'Start a conversation below.'
)

// #2101: local by design. Portal.vue keys PortalConversation (and this slot
// content with it) by `${agent}#${convGen}`, so an agent/thread switch remounts
// us and collapses for free; a same-agent roster refresh re-renders the same
// instance and correctly PRESERVES expansion (design contract, principle 5).
const expanded = ref(false)
const hintPlan = computed(() => planHintDisplay(playbooks.value, expanded.value))

// `starterFor` (starter_prompt → else title, never auto-run) moved to
// portalUtils.js in ent#392: the `/` typeahead inserts the same thing this card
// does, and two copies of that rule would let a hint and its typeahead row
// prefill different text.
</script>
